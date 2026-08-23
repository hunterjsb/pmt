//! Main event loop for the trading engine.

use crate::alerts::{AlertQueue, Notifier};
use crate::client::PolymarketClient;
use crate::config::Config;
use crate::control::{
    self, EngineCommand, OrderInfo, StatusReport, StrategyInfo, TradeInfo,
};
use crate::gamma::{GammaClient, GammaMarket};
use crate::order::OrderManager;
use crate::orderbook::MarketDataHub;
use crate::position::{Fill, PositionTracker};
use crate::risk::{RiskCheckResult, RiskLimits, RiskManager};
use crate::strategy::{MarketInfo, Signal, StrategyContext, StrategyRuntime};

#[cfg(feature = "sigv4")]
use crate::sigv4::SigV4Signer;

use futures::StreamExt;
use polymarket_client_sdk_v2::clob::ws::Client as WsClient;
use polymarket_client_sdk_v2::types::U256;
use rust_decimal::Decimal;
use std::collections::HashMap;
use std::pin::Pin;
use std::str::FromStr;
use std::sync::Arc;
use std::time::Duration;
use tokio::sync::mpsc;
use tokio::time::{interval, Instant};

/// The main trading engine.
pub struct Engine {
    config: Config,
    client: Arc<PolymarketClient>,
    strategy_runtime: StrategyRuntime,
    order_manager: OrderManager,
    risk_manager: RiskManager,
    positions: PositionTracker,
    /// Market data hub with full-depth order books and broadcast channel
    market_data: Arc<MarketDataHub>,
    /// Token IDs we're subscribed to
    subscribed_tokens: Vec<String>,
    fill_receiver: mpsc::Receiver<Fill>,
    shutdown: bool,
    /// Gamma API client for market discovery
    gamma_client: Option<GammaClient>,
    /// Market metadata by token ID
    market_info: HashMap<String, MarketInfo>,
    /// Whether market discovery is enabled
    market_discovery_enabled: bool,
    /// Flag indicating WebSocket needs reconnection due to new market discovery
    ws_needs_reconnect: bool,
    /// Skip warmup period (useful when WS connection is unavailable)
    skip_warmup: bool,
    /// Cached free USDC collateral balance, refreshed periodically by the
    /// balance poller task. Surfaced to strategies via StrategyContext.
    usdc_balance: Arc<tokio::sync::RwLock<Decimal>>,
    /// Receiver for fill events detected by the trades-poller task.
    /// The poller emits one event per (order_id, price, size, fee) tuple
    /// for trades involving our open orders. The main loop processes them
    /// through `order_manager.process_fill`, which then propagates to
    /// positions / strategies / risk via the existing `fill_receiver`.
    fill_event_receiver: mpsc::Receiver<FillEvent>,
    /// Sender half kept on Engine so the trades poller (spawned in `run`)
    /// can clone it.
    fill_event_sender: mpsc::Sender<FillEvent>,
    /// Maps subscribed token_id → on-chain condition_id (market id).
    /// Populated on subscribe via gamma lookup; consumed by the public-
    /// trade poller, which uses condition_id as the `market=` filter for
    /// the data API. Shared via Arc so the spawned poller can read it
    /// without holding a borrow on the engine.
    token_to_condition: Arc<tokio::sync::RwLock<HashMap<String, String>>>,
    /// Always-on gamma client used to resolve token_id → condition_id on
    /// subscription. Independent from the optional `gamma_client` above,
    /// which is gated by market-discovery mode.
    gamma_resolver: GammaClient,
    /// Queue of `PendingAlert` awaiting human approval.
    alert_queue: AlertQueue,
    /// Notifier for outbound push notifications (ntfy / Discord / noop).
    notifier: Arc<Notifier>,
    /// Orders the engine knows about but didn't place itself — registered
    /// via `POST /orders/external` after a CLI buy/sell. Combined with
    /// engine-placed orders for the unified `/orders/all` view, and
    /// available as cancel targets via `POST /orders/:id/cancel`.
    external_orders: HashMap<String, crate::control::ExternalOrder>,
    /// (deadline, order_id) pairs scheduled via `POST /orders/:id/schedule-cancel`.
    /// Drained at the top of every tick — entries whose deadline has passed
    /// trigger a `cancel_order` call. Backs `pmt buy/sell --ttl`.
    pending_cancellations: Vec<(chrono::DateTime<chrono::Utc>, String)>,
}

/// Internal event emitted by the trades poller for the main loop to process.
#[derive(Debug, Clone)]
pub struct FillEvent {
    pub order_id: String,
    pub price: Decimal,
    pub size: Decimal,
    pub fee: Decimal,
}

impl Engine {
    /// Create a new engine instance.
    pub async fn new(config: Config, dry_run: bool) -> Result<Self, EngineError> {
        // Create and authenticate client (SigV4-signing proxy requests).
        // Missing credentials with a proxy configured is a hard error — the
        // IAM Function URL 403s every unsigned request, so running without
        // a signer can only fail later and less legibly.
        #[cfg(feature = "sigv4")]
        let client = {
            let sigv4 = if std::env::var("PMPROXY_URL").is_ok() {
                tracing::info!("Proxy detected, initializing SigV4 signing...");
                Some(Arc::new(SigV4Signer::from_env().await.map_err(|e| {
                    EngineError::SdkError(e.to_string())
                })?))
            } else {
                None
            };
            Arc::new(
                PolymarketClient::new_with_sigv4(&config, dry_run, sigv4)
                    .await
                    .map_err(|e| EngineError::SdkError(e.to_string()))?,
            )
        };

        #[cfg(not(feature = "sigv4"))]
        let client = Arc::new(
            PolymarketClient::new(&config, dry_run)
                .await
                .map_err(|e| EngineError::SdkError(e.to_string()))?,
        );

        // Create fill channel
        let (fill_sender, fill_receiver) = mpsc::channel(1000);
        let (fill_event_sender, fill_event_receiver) = mpsc::channel::<FillEvent>(100);

        // Create order manager with client
        let order_manager = OrderManager::new(client.clone(), fill_sender);

        // Create risk manager with limits from config
        let risk_limits = RiskLimits {
            max_position_size: Decimal::from_f64_retain(config.max_position_size)
                .unwrap_or(Decimal::from(50)),
            max_total_exposure: Decimal::from_f64_retain(config.max_total_exposure)
                .unwrap_or(Decimal::from(50)),
            max_order_size: Decimal::from_f64_retain(config.max_total_exposure / 2.0)
                .unwrap_or(Decimal::from(25)),
            ..Default::default()
        };

        tracing::info!(
            max_position_size = %risk_limits.max_position_size,
            max_total_exposure = %risk_limits.max_total_exposure,
            max_order_size = %risk_limits.max_order_size,
            max_loss = %risk_limits.max_loss,
            "Risk limits configured"
        );

        let risk_manager = RiskManager::new(risk_limits);

        // Create strategy runtime (empty, strategies added via register)
        let strategy_runtime = StrategyRuntime::new();

        // Create market data hub with broadcast channel
        let market_data = Arc::new(MarketDataHub::new(1000));

        Ok(Self {
            config,
            client,
            strategy_runtime,
            order_manager,
            risk_manager,
            positions: PositionTracker::new(),
            market_data,
            subscribed_tokens: Vec::new(),
            fill_receiver,
            shutdown: false,
            gamma_client: None,
            market_info: HashMap::new(),
            market_discovery_enabled: false,
            ws_needs_reconnect: false,
            skip_warmup: false,
            usdc_balance: Arc::new(tokio::sync::RwLock::new(Decimal::ZERO)),
            fill_event_receiver,
            fill_event_sender,
            token_to_condition: Arc::new(tokio::sync::RwLock::new(HashMap::new())),
            gamma_resolver: GammaClient::new(),
            alert_queue: AlertQueue::new(),
            notifier: Arc::new(Notifier::from_env()),
            external_orders: HashMap::new(),
            pending_cancellations: Vec::new(),
        })
    }

    /// Enable market discovery with Gamma API.
    ///
    /// This allows the engine to dynamically discover markets and subscribe
    /// to tokens that meet certain criteria (e.g., high-certainty expiring markets).
    pub fn enable_market_discovery(&mut self) {
        self.gamma_client = Some(GammaClient::new());
        self.market_discovery_enabled = true;
        tracing::info!("Market discovery enabled");
    }

    /// Set whether to skip warmup period.
    ///
    /// When true, the engine will start trading immediately without waiting
    /// for WebSocket order book data. Useful when WS connection is unavailable.
    pub fn set_skip_warmup(&mut self, skip: bool) {
        self.skip_warmup = skip;
    }

    /// Build market info map from Gamma markets.
    ///
    /// IMPORTANT: Only adds the HIGH-CERTAINTY token from each market.
    /// This prevents the strategy from accidentally buying the wrong outcome
    /// (e.g., buying "No" at 0.05 instead of "Yes" at 0.95).
    fn build_market_info(&self, markets: &[GammaMarket]) -> HashMap<String, MarketInfo> {
        let mut info_map = HashMap::new();

        for market in markets {
            // Only add the highest-certainty outcome token
            // This prevents buying the wrong side of a market
            if let Some(high_cert_idx) = market.highest_certainty_index() {
                if let (Some(token_id), Some(outcome)) = (
                    market.clob_token_ids.get(high_cert_idx),
                    market.outcomes.get(high_cert_idx),
                ) {
                    let info = MarketInfo::with_liquidity(
                        market.question.clone(),
                        outcome.clone(),
                        market.slug.clone(),
                        market.end_date,
                        market.liquidity,
                    );

                    tracing::debug!(
                        question = market.question.as_str(),
                        outcome = outcome.as_str(),
                        token_id = token_id.as_str(),
                        price = ?market.outcome_prices.get(high_cert_idx),
                        "Adding high-certainty token to market info"
                    );

                    info_map.insert(token_id.clone(), info);
                }
            }
        }

        info_map
    }

    /// Maximum hours to expiry for market discovery.
    /// This is a broader window - strategies will do their own time filtering.
    const MAX_HOURS_TO_EXPIRY: f64 = 72.0;

    /// Minimum certainty threshold for fetching markets (broad filter).
    /// Strategies will apply their own stricter filters.
    const MIN_CERTAINTY: rust_decimal::Decimal = rust_decimal_macros::dec!(0.90);

    /// Refresh markets from Gamma API.
    ///
    /// This fetches markets from two sources:
    /// 1. Events endpoint - for general high-certainty expiring markets
    /// 2. Series endpoint - for recurring markets (BTC 4h, SPX daily, etc.)
    ///
    /// NOTE: The engine provides ALL markets to strategies. Strategies do their
    /// own filtering based on keywords, liquidity, certainty thresholds, etc.
    async fn refresh_markets(&mut self) -> Result<(), EngineError> {
        let gamma = match &self.gamma_client {
            Some(c) => c,
            None => return Ok(()),
        };

        // Fetch from events endpoint (general markets)
        let event_markets = gamma
            .fetch_sure_bet_candidates(Self::MAX_HOURS_TO_EXPIRY, Self::MIN_CERTAINTY)
            .await
            .map_err(|e| EngineError::SdkError(format!("Gamma API error (events): {}", e)))?;

        tracing::info!(
            count = event_markets.len(),
            "Discovered markets from events endpoint"
        );

        // Fetch from series endpoint (recurring markets like BTC 4h, SPX daily)
        let recurring_markets = gamma
            .fetch_recurring_markets(Self::MAX_HOURS_TO_EXPIRY, Self::MIN_CERTAINTY)
            .await
            .map_err(|e| EngineError::SdkError(format!("Gamma API error (series): {}", e)))?;

        tracing::info!(
            count = recurring_markets.len(),
            "Discovered markets from recurring series"
        );

        // Merge both sources, deduplicating by slug
        let mut seen_slugs = std::collections::HashSet::new();
        let mut markets = Vec::new();

        for market in event_markets.into_iter().chain(recurring_markets) {
            if seen_slugs.insert(market.slug.clone()) {
                markets.push(market);
            }
        }

        tracing::info!(
            count = markets.len(),
            "Total unique markets discovered"
        );

        // Subscribe ONLY to high-certainty tokens (matching build_market_info logic)
        let mut new_tokens_found = false;

        for market in &markets {
            // Only subscribe to the highest-certainty outcome token
            // This matches build_market_info() and prevents wrong-side subscriptions
            if let Some(high_cert_idx) = market.highest_certainty_index() {
                if let Some(token_id) = market.clob_token_ids.get(high_cert_idx) {
                    if !self.subscribed_tokens.contains(token_id) {
                        self.market_data.init_book(token_id).await;
                        self.subscribed_tokens.push(token_id.clone());
                        new_tokens_found = true;
                        tracing::debug!(
                            token_id = token_id.as_str(),
                            outcome = ?market.outcomes.get(high_cert_idx),
                            price = ?market.outcome_prices.get(high_cert_idx),
                            "New high-certainty token discovered"
                        );
                    }
                }
            }
        }

        // Update market info with ALL markets (strategies filter themselves)
        self.market_info = self.build_market_info(&markets);

        tracing::info!(
            token_count = self.subscribed_tokens.len(),
            market_count = self.market_info.len(),
            "Market info updated"
        );

        // Signal WebSocket reconnection if new tokens were discovered
        if new_tokens_found {
            tracing::info!(
                token_count = self.subscribed_tokens.len(),
                "New tokens discovered, WebSocket reconnection needed"
            );
            self.ws_needs_reconnect = true;
        }

        Ok(())
    }

    /// Check if running in dry-run mode.
    pub fn is_dry_run(&self) -> bool {
        self.client.is_dry_run()
    }

    /// Resolve a token_id to its parent market's condition_id via gamma,
    /// caching the result. Best-effort: a failure is logged and the map
    /// stays empty for that token, which only means the public-trade
    /// poller won't watch that market — strategies still receive book
    /// updates from the REST poller.
    async fn ensure_condition_id(&self, token_id: &str) {
        {
            let map = self.token_to_condition.read().await;
            if map.contains_key(token_id) {
                return;
            }
        }
        match self.gamma_resolver.fetch_market_by_token(token_id).await {
            Ok(Some(m)) if !m.condition_id.is_empty() => {
                let mut map = self.token_to_condition.write().await;
                map.insert(token_id.to_string(), m.condition_id.clone());
                tracing::info!(
                    token_id = %token_id,
                    condition_id = %m.condition_id,
                    "Resolved condition_id for token"
                );
            }
            Ok(_) => tracing::warn!(
                token_id = %token_id,
                "Gamma lookup returned no market — public trades disabled for this token"
            ),
            Err(e) => tracing::warn!(
                token_id = %token_id,
                error = %e,
                "Gamma lookup failed — public trades disabled for this token"
            ),
        }
    }

    /// Begin watching a token at runtime. Idempotent: a duplicate request is
    /// a no-op. Initialises an empty book in the MarketDataHub (the REST
    /// poller picks it up on its next tick) and flags the WebSocket for
    /// reconnect so the new asset id is included in the subscription.
    pub async fn subscribe_token(&mut self, token_id: &str) {
        if self.subscribed_tokens.iter().any(|t| t == token_id) {
            return;
        }
        self.market_data.init_book(token_id).await;
        self.subscribed_tokens.push(token_id.to_string());
        self.ws_needs_reconnect = true;
        self.ensure_condition_id(token_id).await;
        tracing::info!(token_id = %token_id, "Subscribed to token");
    }

    /// Stop watching a token at runtime. Cancels any open orders on the
    /// token first (locally + on Polymarket), releases their risk-manager
    /// reservations, drops the position from the exposure ledger, removes
    /// the book from the hub, and flags the WebSocket for reconnect.
    /// Order history is preserved.
    pub async fn unsubscribe_token(&mut self, token_id: &str) {
        if !self.subscribed_tokens.iter().any(|t| t == token_id) {
            return;
        }
        match self.order_manager.cancel_all(token_id).await {
            Ok(n) if n > 0 => tracing::info!(
                token_id = %token_id,
                cancelled = n,
                "Unsubscribe: cancelled open orders"
            ),
            Ok(_) => {}
            Err(e) => tracing::warn!(
                token_id = %token_id,
                error = %e,
                "Unsubscribe: cancel-all failed; proceeding anyway"
            ),
        }
        self.risk_manager.release_orders_for_token(token_id);
        // Unmanaged token => its exposure must leave the ledger too (see docs/LESSONS.md#L5).
        let released = self.positions.remove(token_id);
        if released > rust_decimal::Decimal::ZERO {
            tracing::info!(token_id = %token_id, notional = %released, "Unsubscribe: released position exposure");
        }
        self.market_data.remove_book(token_id).await;
        self.subscribed_tokens.retain(|t| t != token_id);
        // Drop the condition_id mapping so the public-trade poller
        // stops fetching for this market once no other watched token
        // points to it (the poller iterates unique condition_ids each tick).
        {
            let mut map = self.token_to_condition.write().await;
            map.remove(token_id);
        }
        self.ws_needs_reconnect = true;
        tracing::info!(token_id = %token_id, "Unsubscribed from token");
    }

    /// Register a strategy.
    pub async fn register_strategy(&mut self, strategy: Box<dyn crate::strategy::Strategy>) {
        // Initialize order books for subscriptions and resolve their
        // condition_ids for the public-trade poller.
        for token_id in strategy.subscriptions() {
            if !self.subscribed_tokens.contains(&token_id) {
                self.market_data.init_book(&token_id).await;
                self.subscribed_tokens.push(token_id.clone());
            }
            self.ensure_condition_id(&token_id).await;
        }
        self.strategy_runtime.register(strategy);
    }

    /// Load strategies by name from the auto-generated registry.
    ///
    /// This method looks up strategies in the registry (generated by pmstrat transpile)
    /// and registers them with the engine.
    pub fn load_strategies(&mut self, names: &[String]) -> Result<(), EngineError> {
        use crate::strategies::registry;

        let reg = registry();

        for name in names {
            let info = reg.get(name.as_str()).ok_or_else(|| {
                let available: Vec<_> = reg.keys().collect();
                tracing::error!(
                    strategy = name.as_str(),
                    available = ?available,
                    "Unknown strategy"
                );
                EngineError::UnknownStrategy(name.clone())
            })?;

            // Enable market discovery if required
            if info.requires_market_discovery {
                self.enable_market_discovery();
            }

            // Create and register the strategy
            let strategy = (info.factory)();

            if let Some(w) = crate::config::slow_tick_warning(
                name,
                strategy.tick_interval_ms(),
                self.config.tick_interval_ms,
            ) {
                tracing::warn!("{}", w);
            }

            // Initialize order books for subscriptions and resolve their
            // condition_ids for the public-trade poller.
            for token_id in strategy.subscriptions() {
                if !self.subscribed_tokens.contains(&token_id) {
                    // Use blocking approach for sync context
                    futures::executor::block_on(self.market_data.init_book(&token_id));
                    self.subscribed_tokens.push(token_id.clone());
                }
                futures::executor::block_on(self.ensure_condition_id(&token_id));
            }
            self.strategy_runtime.register(strategy);

            tracing::info!(
                strategy = name.as_str(),
                requires_market_discovery = info.requires_market_discovery,
                "Loaded strategy"
            );
        }

        Ok(())
    }

    /// True iff every subscribed token has both a best bid and best ask.
    /// Used to short-circuit warmup when REST polling has filled the books.
    async fn all_books_populated(&self) -> bool {
        let books = self.market_data.get_all_books().await;
        self.subscribed_tokens.iter().all(|t| {
            books
                .get(t)
                .map(|b| b.best_bid().is_some() && b.best_ask().is_some())
                .unwrap_or(false)
        })
    }

    /// Run the main event loop.
    ///
    /// # Arguments
    /// * `max_ticks` - Maximum number of ticks before automatic shutdown (0 = unlimited)
    pub async fn run(&mut self, max_ticks: u64) -> Result<(), EngineError> {
        tracing::info!(max_ticks = max_ticks, "Starting engine event loop");

        // Origin for uptime exposed via the control plane. std (not tokio)
        // because StrategyRuntime tracks last-tick instants with std::time.
        let start_instant = std::time::Instant::now();

        // Spawn the local control plane HTTP server. Binds to loopback by
        // default; override with PMENGINE_CONTROL_BIND. Commands flow back
        // here via cmd_rx and are dispatched inline in the select! loop
        // below so no engine state ever needs to be Arc-shared.
        let (cmd_tx, mut cmd_rx) = mpsc::channel::<EngineCommand>(64);
        let control_bind: std::net::SocketAddr = std::env::var("PMENGINE_CONTROL_BIND")
            .ok()
            .and_then(|s| s.parse().ok())
            .unwrap_or_else(|| "127.0.0.1:7531".parse().expect("default bind addr is valid"));
        // Bind the control plane synchronously. If the port is already held
        // (another engine instance is running), fail fast rather than trade
        // headless — two engines quoting the same token is a real hazard.
        let control_handle = control::spawn(control_bind, cmd_tx.clone())
            .await
            .map_err(|e| {
                EngineError::ControlPlaneError(format!(
                    "could not bind {} ({}). Another engine is likely already running.",
                    control_bind, e
                ))
            })?;

        // Startup reconcile: clear any orphan orders left by a previous engine
        // session on the strategies' subscribed tokens. The engine treats
        // itself as the sole order manager for those markets — anything
        // already on the book on these tokens would race against new
        // engine-placed orders and trip the Polymarket balance check.
        //
        // Set PMENGINE_RECONCILE_ON_STARTUP=false to disable (e.g. if a
        // human is also placing manual maker orders on the same tokens).
        if self.config.reconcile_on_startup && !self.subscribed_tokens.is_empty() {
            tracing::info!(
                tokens = ?self.subscribed_tokens,
                "Reconciling: cancelling pre-existing orders on subscribed tokens"
            );
            for token_id in self.subscribed_tokens.clone() {
                match self.client.cancel_all_orders_on_token(&token_id).await {
                    Ok(n) if n > 0 => tracing::info!(
                        token_id = %token_id,
                        cancelled = n,
                        "Reconcile: cancelled orphans"
                    ),
                    Ok(_) => tracing::debug!(token_id = %token_id, "Reconcile: nothing to cancel"),
                    Err(e) => tracing::warn!(
                        token_id = %token_id,
                        error = %e,
                        "Reconcile cancel failed — engine will proceed but may hit balance errors"
                    ),
                }
            }
        }

        // Repopulate external_orders from Polymarket so any orders left over
        // from a previous engine session (manual CLI placements, prior crash)
        // show up in /orders/all and can be cancelled via /orders/:id/cancel.
        //
        // Runs after the destructive reconcile above so we don't pick up
        // orders that were just cancelled. Failure here is non-fatal — the
        // engine still starts, the unified view just won't include legacy
        // orders until the user touches them via the CLI (which re-registers).
        match self.client.get_open_orders().await {
            Ok(orders) => {
                let mut loaded = 0;
                for o in orders {
                    let ext = crate::control::ExternalOrder {
                        id: o.id.clone(),
                        token_id: o.asset_id.to_string(),
                        side: o.side.to_string().to_lowercase(),
                        price: o.price,
                        size: o.original_size,
                        source: "reconciled".to_string(),
                        created_at: o.created_at,
                    };
                    self.external_orders.insert(o.id, ext);
                    loaded += 1;
                }
                tracing::info!(
                    count = loaded,
                    "Startup reconcile: loaded existing open orders into external_orders"
                );
            }
            Err(e) => {
                tracing::warn!(
                    error = %e,
                    "Startup reconcile: failed to fetch open orders; /orders/all will be \
                     incomplete until CLI re-registers them"
                );
            }
        }

        // Seed positions from the authoritative data-api holdings for every
        // subscribed token. The PositionTracker otherwise starts empty, so the
        // strategy + risk manager would be blind to inventory we already hold
        // before the engine launched (e.g. a manual 208-share position). With
        // this seed, MAX_POSITION is enforced against true total holdings.
        for token_id in self.subscribed_tokens.clone() {
            match self.client.get_position(&token_id).await {
                Ok(Some((size, avg))) => {
                    let delta = self.positions.reconcile(&token_id, size, avg);
                    tracing::info!(
                        token_id = %token_id, size = %size, avg_price = %avg, delta = %delta,
                        "Startup reconcile: seeded position from data-api"
                    );
                }
                Ok(None) => {}
                Err(e) => tracing::warn!(
                    error = %e, token_id = %token_id,
                    "Startup reconcile: failed to seed position; starting from zero"
                ),
            }
        }

        // Get tick interval
        let tick_duration = Duration::from_millis(self.config.tick_interval_ms);
        let mut tick_timer = interval(tick_duration);

        // Graceful shutdown on BOTH SIGINT and SIGTERM (see docs/LESSONS.md#L4).
        let (shutdown_tx, mut shutdown_rx) = mpsc::channel::<()>(1);
        tokio::spawn(async move {
            let mut sigterm = tokio::signal::unix::signal(
                tokio::signal::unix::SignalKind::terminate(),
            )
            .expect("sigterm handler install");
            tokio::select! {
                _ = tokio::signal::ctrl_c() => {}
                _ = sigterm.recv() => {}
            }
            tracing::info!("Received shutdown signal");
            shutdown_tx.send(()).await.ok();
        });

        // Spawn the trades poller. Watches the user-trades REST endpoint for
        // fills against engine-placed orders and emits a FillEvent for each.
        // Tracked-order IDs are read from order_manager via a cloned Arc; the
        // poller never modifies engine state directly.
        let trades_handle = {
            let client = self.client.clone();
            let tx = self.fill_event_sender.clone();
            // Track which trade IDs we've already processed to dedupe across polls.
            let mut seen: std::collections::HashSet<String> = std::collections::HashSet::new();
            let mut last_ts: Option<i64> = None;
            // We need to know which order_ids belong to the engine. Wrap the
            // OrderManager's `orders` map behind a shared snapshot — but easier
            // for now: have the poller query trades, and emit events for ALL
            // user trades. The main loop filters by checking if order_id is
            // tracked before calling process_fill.
            tokio::spawn(async move {
                let mut timer = tokio::time::interval(Duration::from_secs(15));
                timer.tick().await; // skip immediate
                loop {
                    timer.tick().await;
                    let trades = match client.get_user_trades_since(last_ts).await {
                        Ok(t) => t,
                        Err(e) => {
                            tracing::debug!(error = %e, "Trades poll failed");
                            continue;
                        }
                    };
                    for t in trades {
                        if !seen.insert(t.id.clone()) {
                            continue; // already processed
                        }
                        let ts = t.match_time.timestamp();
                        if last_ts.map(|prev| ts > prev).unwrap_or(true) {
                            last_ts = Some(ts);
                        }
                        // Compute fee: price * size * fee_rate_bps / 10_000.
                        let fee = t.price * t.size * t.fee_rate_bps / Decimal::from(10_000u32);
                        use polymarket_client_sdk_v2::clob::types::TraderSide;
                        // For our side of the trade we want the order id WE placed.
                        // If we were the taker, that's taker_order_id and size = t.size.
                        // If we were the maker, find our maker_orders entry (there
                        // may be multiple makers per trade, but only one per user).
                        let our_fills: Vec<(String, Decimal)> = match t.trader_side {
                            TraderSide::Taker => vec![(t.taker_order_id.clone(), t.size)],
                            TraderSide::Maker => t
                                .maker_orders
                                .iter()
                                .map(|m| (m.order_id.clone(), m.matched_amount))
                                .collect(),
                            _ => {
                                tracing::debug!(trade_id = %t.id, side = ?t.trader_side, "Unknown trader_side, skipping");
                                continue;
                            }
                        };
                        for (order_id, size) in our_fills {
                            let ev = FillEvent {
                                order_id,
                                price: t.price,
                                size,
                                fee,
                            };
                            if tx.send(ev).await.is_err() {
                                tracing::warn!("Fill event channel closed; trades poller exiting");
                                return;
                            }
                        }
                    }
                }
            })
        };

        // Spawn the USDC balance poller. Polymarket caches balance server-side
        // so polling every 30s is plenty. Strategies read this through
        // StrategyContext.usdc_balance to size against real cash and avoid the
        // "not enough balance" rejects you get from over-quoting.
        let balance_handle = {
            let client = self.client.clone();
            let balance = self.usdc_balance.clone();
            tokio::spawn(async move {
                // Fire one immediate fetch so the first tick has a real value.
                let mut timer = tokio::time::interval(Duration::from_secs(30));
                loop {
                    timer.tick().await;
                    match client.get_collateral_balance().await {
                        Ok(b) => {
                            let mut w = balance.write().await;
                            if *w != b {
                                tracing::info!(usdc_balance = %b, prev = %*w, "Balance updated");
                                *w = b;
                            }
                        }
                        Err(e) => tracing::debug!(error = %e, "Balance poll failed"),
                    }
                }
            })
        };

        // Spawn the market scanner, if any registered strategy declares a
        // market_filter. Iterates all filters every PMENGINE_SCAN_INTERVAL_S
        // seconds, queries gamma, and reconciles the watched token set:
        // newcomers get SubscribeToken commands, drop-outs get
        // UnsubscribeToken. Each filter contributes its share up to
        // `max_subscriptions`; the union is the desired set.
        let scanner_filters: Vec<crate::gamma::MarketFilter> =
            self.strategy_runtime.market_filters();
        // Tokens statically declared by ANY strategy — the scanner must never
        // unsubscribe these (see docs/LESSONS.md#L31).
        let static_subscriptions: std::collections::HashSet<String> =
            self.strategy_runtime.all_static_subscriptions();
        let scanner_handle = if !scanner_filters.is_empty() {
            let cmd_tx = cmd_tx.clone();
            let interval_s = std::env::var("PMENGINE_SCAN_INTERVAL_S")
                .ok()
                .and_then(|s| s.parse().ok())
                .unwrap_or(60u64);
            tracing::info!(
                interval_s = interval_s,
                filter_count = scanner_filters.len(),
                protected_tokens = static_subscriptions.len(),
                "Market scanner enabled"
            );
            Some(tokio::spawn(async move {
                use crate::gamma::GammaClient;
                let gamma = GammaClient::new();
                let mut timer = tokio::time::interval(Duration::from_secs(interval_s));
                // Don't skip the first tick — we WANT an immediate
                // scan-and-subscribe at startup so the strategy isn't
                // running blind for interval_s seconds.
                loop {
                    timer.tick().await;
                    // Build the desired token set by unioning each
                    // strategy's matched markets. Choose the
                    // highest-certainty token per market — that's what
                    // existing discovery does and what strategies expect.
                    let mut desired: std::collections::HashSet<String> = std::collections::HashSet::new();
                    for filter in &scanner_filters {
                        let markets = match gamma.fetch_markets_matching(filter).await {
                            Ok(m) => m,
                            Err(e) => {
                                tracing::warn!(error = %e, "Scanner gamma query failed");
                                continue;
                            }
                        };
                        for m in markets {
                            if let Some(idx) = m.highest_certainty_index() {
                                if let Some(t) = m.clob_token_ids.get(idx) {
                                    desired.insert(t.clone());
                                }
                            }
                        }
                    }
                    // Read current subscriptions via the control plane.
                    let (tx, rx) = tokio::sync::oneshot::channel();
                    if cmd_tx.send(EngineCommand::ListSubscriptions(tx)).await.is_err() {
                        return;
                    }
                    let current: std::collections::HashSet<String> = match rx.await {
                        Ok(v) => v.into_iter().collect(),
                        Err(_) => return,
                    };
                    let to_add: Vec<String> = desired.difference(&current).cloned().collect();
                    // Skip Unsubscribe for any token a sibling strategy
                    // statically declared. Otherwise the scanner would
                    // happily drop hanta_maker's Hantavirus token the
                    // moment it doesn't match momentum_fade's filter.
                    let to_drop: Vec<String> = current
                        .difference(&desired)
                        .filter(|t| !static_subscriptions.contains(*t))
                        .cloned()
                        .collect();
                    tracing::info!(
                        added = to_add.len(),
                        dropped = to_drop.len(),
                        watched = desired.len(),
                        "Scanner reconciled"
                    );
                    for token_id in to_add {
                        let (tx, _rx) = tokio::sync::oneshot::channel();
                        let _ = cmd_tx
                            .send(EngineCommand::SubscribeToken { token_id, reply: tx })
                            .await;
                    }
                    for token_id in to_drop {
                        let (tx, _rx) = tokio::sync::oneshot::channel();
                        let _ = cmd_tx
                            .send(EngineCommand::UnsubscribeToken { token_id, reply: tx })
                            .await;
                    }
                }
            }))
        } else {
            None
        };

        // Spawn the public-trade tape poller. Watches Polymarket's /trades
        // data API for every market we're subscribed to and broadcasts each
        // new trade through the MarketDataHub. Independent from the
        // user-trades poller above, which is filtered to our orders only.
        //
        // Per-condition cursor (last_ts_per_cond) narrows each poll to new
        // trades. A composite dedup key catches the boundary case where two
        // trades share a timestamp and the API's strict-greater-than cursor
        // would drop one. Both maps are local to the task; nothing escapes.
        let trade_tape_handle = {
            let client = self.client.clone();
            let hub = self.market_data.clone();
            let token_to_condition = self.token_to_condition.clone();
            let interval = Duration::from_millis(
                std::env::var("PMENGINE_TRADE_POLL_MS")
                    .ok()
                    .and_then(|s| s.parse().ok())
                    .unwrap_or(3000),
            );
            let limit: usize = std::env::var("PMENGINE_TRADE_POLL_LIMIT")
                .ok()
                .and_then(|s| s.parse().ok())
                .unwrap_or(100);
            tokio::spawn(async move {
                let mut timer = tokio::time::interval(interval);
                timer.tick().await; // skip immediate first
                let mut seen: std::collections::HashSet<String> = std::collections::HashSet::new();
                let mut last_ts_per_cond: HashMap<String, i64> = HashMap::new();
                loop {
                    timer.tick().await;
                    let conditions: std::collections::HashSet<String> = {
                        let map = token_to_condition.read().await;
                        map.values().cloned().collect()
                    };
                    if conditions.is_empty() {
                        continue;
                    }
                    for cid in conditions {
                        let after = last_ts_per_cond.get(&cid).copied();
                        let trades = match client
                            .get_market_trades_since(&cid, after, limit)
                            .await
                        {
                            Ok(t) => t,
                            Err(e) => {
                                // warn, not debug: this poller feeds the
                                // print-flow corpus (see docs/LESSONS.md#L7).
                                tracing::warn!(
                                    condition_id = %cid,
                                    error = %e,
                                    "Public trade poll failed"
                                );
                                continue;
                            }
                        };
                        for t in trades {
                            if !seen.insert(t.dedup_key()) {
                                continue;
                            }
                            let entry =
                                last_ts_per_cond.entry(cid.clone()).or_insert(t.timestamp);
                            if t.timestamp > *entry {
                                *entry = t.timestamp;
                            }
                            hub.broadcast_trade(
                                t.asset.clone(),
                                t.price,
                                t.size,
                                t.side.clone(),
                                t.timestamp,
                            )
                            .await;
                            tracing::debug!(
                                token_id = %t.asset,
                                price = %t.price,
                                size = %t.size,
                                side = %t.side,
                                "Public trade observed"
                            );
                        }
                    }
                    // Prune the dedup set if it grows large. The cursor does
                    // most of the work; this is a safety net.
                    if seen.len() > 10_000 {
                        seen.clear();
                        tracing::debug!("Trade dedup set reset (>10k entries)");
                    }
                }
            })
        };

        // Spawn the REST book poller. Runs alongside the WebSocket subscription
        // so books stay current even when WS is unavailable (e.g. from a US IP
        // without a WS-capable proxy). Polls every book currently tracked by
        // the MarketDataHub.
        let poller_handle = {
            let client = self.client.clone();
            let hub = self.market_data.clone();
            let poll_interval = Duration::from_millis(
                std::env::var("PMENGINE_BOOK_POLL_MS")
                    .ok()
                    .and_then(|s| s.parse().ok())
                    .unwrap_or(2000),
            );
            tokio::spawn(async move {
                let mut timer = tokio::time::interval(poll_interval);
                // Skip immediate first tick
                timer.tick().await;
                loop {
                    timer.tick().await;
                    let tokens: Vec<String> =
                        hub.get_all_books().await.keys().cloned().collect();
                    for token in tokens {
                        match client.get_book(&token).await {
                            Ok(book) => hub.set_book(book).await,
                            Err(e) => tracing::debug!(
                                token = %token,
                                error = %e,
                                "REST book poll failed"
                            ),
                        }
                    }
                }
            })
        };

        let mut last_tick = Instant::now();
        let mut tick_count: u64 = 0;

        // Market discovery timer (60 seconds)
        let mut market_refresh_timer = interval(Duration::from_secs(60));
        // Skip the first immediate tick
        market_refresh_timer.tick().await;

        // Do initial market discovery if enabled
        if self.market_discovery_enabled {
            if let Err(e) = self.refresh_markets().await {
                tracing::warn!(error = %e, "Initial market discovery failed");
            }
            // Clear the reconnect flag - we'll connect WebSocket in the main loop
            self.ws_needs_reconnect = false;
        }

        // Use labeled loop to support WebSocket reconnection
        // When new tokens are discovered, we break the inner loop and reconnect
        'reconnect: loop {
            // Reset WebSocket update count on each reconnection
            let mut ws_update_count: u64 = 0;

            // Connect to WebSocket for market data if we have subscriptions
            // Keep ws_client alive since the stream borrows from it
            let ws_client = WsClient::default();
            let mut ws_stream: Option<Pin<Box<dyn futures::Stream<Item = Result<_, _>> + Send>>> =
                if !self.subscribed_tokens.is_empty() {
                    let asset_ids: Result<Vec<U256>, _> = self
                        .subscribed_tokens
                        .iter()
                        .map(|t| U256::from_str(t))
                        .collect();

                    match asset_ids {
                        Ok(ids) => {
                            tracing::info!(count = ids.len(), "Subscribing to orderbook updates");
                            match ws_client.subscribe_orderbook(ids) {
                                Ok(stream) => Some(Box::pin(stream)),
                                Err(e) => {
                                    tracing::error!(error = %e, "Failed to subscribe to orderbook");
                                    None
                                }
                            }
                        }
                        Err(e) => {
                            tracing::error!(error = %e, "Invalid token ID format");
                            None
                        }
                    }
                } else {
                    tracing::info!("No subscriptions, running without WebSocket");
                    None
                };

            tracing::info!("Entering event loop");

            // Warmup: wait for books to sync. Any ONE of three exits is
            // sufficient — `--skip-warmup`, WARMUP_WS_UPDATES streamed diffs
            // (never fires under a geoblocked WS), or WARMUP_DEADLINE wall
            // clock. A still-missing book is the strategy's own `if book is
            // None` guard's problem. See docs/LESSONS.md#L35.
            const WARMUP_WS_UPDATES: u64 = 100;
            const WARMUP_DEADLINE: std::time::Duration = std::time::Duration::from_secs(10);
            let warmup_started_at = std::time::Instant::now();
            let mut warmup_complete = false;

            loop {
                tokio::select! {

                    // Market discovery refresh (if enabled)
                    _ = market_refresh_timer.tick(), if self.market_discovery_enabled => {
                        if let Err(e) = self.refresh_markets().await {
                            tracing::warn!(error = %e, "Market discovery refresh failed");
                        }

                        // Break to reconnect WebSocket if new tokens were discovered
                        if self.ws_needs_reconnect {
                            tracing::info!(
                                token_count = self.subscribed_tokens.len(),
                                "Reconnecting WebSocket with new tokens"
                            );
                            self.ws_needs_reconnect = false;
                            continue 'reconnect;
                        }
                    }

                    // Tick timer for strategy evaluation
                    _ = tick_timer.tick() => {
                        tick_count += 1;
                        let elapsed = last_tick.elapsed();
                        last_tick = Instant::now();

                        tracing::info!(tick = tick_count, elapsed_ms = elapsed.as_millis(), "Tick");

                        // Periodic position reconcile against the data-api, every
                        // 30 ENGINE TICKS — so ~1.5s under the launcher's 50ms
                        // PMENGINE_TICK_INTERVAL_MS, 30s at the binary's own
                        // 1000ms default. Incremental fill detection can miss
                        // fills — notably partials that land in the MM's
                        // cancel/replace window get attributed to an order the
                        // engine no longer tracks and are dropped. The data-api
                        // is ground truth, so we correct drift here. This keeps
                        // MAX_POSITION enforcement honest even when a fill slips
                        // past the trades poller.
                        if tick_count.is_multiple_of(30) {
                            for token_id in self.subscribed_tokens.clone() {
                                if let Ok(Some((size, avg))) = self.client.get_position(&token_id).await {
                                    let delta = self.positions.reconcile(&token_id, size, avg);
                                    if delta != Decimal::ZERO {
                                        tracing::warn!(
                                            token_id = %token_id, corrected_to = %size, delta = %delta,
                                            "Position reconcile: corrected drift from missed fill(s)"
                                        );
                                    }
                                }
                            }
                        }

                        // Drain TTL-scheduled cancels whose deadline has passed.
                        // Runs before warmup checks so externally-placed orders
                        // with TTLs still expire even if the engine isn't trading.
                        if !self.pending_cancellations.is_empty() {
                            let now = chrono::Utc::now();
                            let (due, remaining): (Vec<_>, Vec<_>) = self
                                .pending_cancellations
                                .drain(..)
                                .partition(|(at, _)| *at <= now);
                            self.pending_cancellations = remaining;
                            for (_, order_id) in due {
                                match self.client.cancel_order(&order_id).await {
                                    Ok(_) => {
                                        self.external_orders.remove(&order_id);
                                        self.risk_manager.release_order(&order_id);
                                        tracing::info!(
                                            order_id = %order_id,
                                            "TTL cancel: order cancelled"
                                        );
                                    }
                                    Err(e) => tracing::warn!(
                                        error = %e,
                                        order_id = %order_id,
                                        "TTL cancel: cancel failed (order may already be filled or cancelled)"
                                    ),
                                }
                            }
                        }

                        // Check max_ticks limit
                        if max_ticks > 0 && tick_count >= max_ticks {
                            tracing::info!(tick_count = tick_count, max_ticks = max_ticks, "Max ticks reached, shutting down");
                            self.shutdown().await?;
                            break 'reconnect;
                        }

                        // Skip trading during warmup period (unless skip_warmup is set
                        // or REST polling has already populated every subscribed book).
                        if !warmup_complete {
                            if self.skip_warmup {
                                warmup_complete = true;
                                tracing::info!("Warmup skipped (--skip-warmup flag)");
                            } else if warmup_started_at.elapsed() >= WARMUP_DEADLINE {
                                warmup_complete = true;
                                tracing::info!(
                                    elapsed_s = warmup_started_at.elapsed().as_secs(),
                                    "Warmup deadline reached, trading enabled (strategies guard their own None books)"
                                );
                            } else if ws_update_count >= WARMUP_WS_UPDATES {
                                warmup_complete = true;
                                tracing::info!(
                                    ws_updates = ws_update_count,
                                    "Warmup complete via WS, trading enabled"
                                );
                            } else if !self.subscribed_tokens.is_empty()
                                && self.all_books_populated().await
                            {
                                warmup_complete = true;
                                tracing::info!(
                                    book_count = self.subscribed_tokens.len(),
                                    "Warmup complete via REST polling, trading enabled"
                                );
                            } else {
                                tracing::info!(
                                    ws_updates = ws_update_count,
                                    required = WARMUP_WS_UPDATES,
                                    "Warmup in progress, skipping trading"
                                );
                                continue;
                            }
                        }

                        // Check P&L for circuit breaker
                        self.risk_manager.check_pnl(&self.positions);

                        if self.risk_manager.is_halted() {
                            tracing::warn!("Engine halted by circuit breaker");
                            continue;
                        }

                        // Build strategy context with full-depth order books
                        let ctx = StrategyContext {
                            timestamp: chrono::Utc::now(),
                            order_books: self.market_data.get_all_books().await,
                            trade_history: self.market_data.get_all_trade_history().await,
                            positions: self.positions.clone(),
                            markets: self.market_info.clone(),
                            unrealized_pnl: self.positions.total_unrealized_pnl(),
                            realized_pnl: self.positions.total_realized_pnl(),
                            usdc_balance: *self.usdc_balance.read().await,
                        };

                        // Run strategies
                        let signals = self.strategy_runtime.tick(&ctx);

                        // Bucket signals so we can do delta quoting. Strategies typically
                        // emit Cancel + Buy + Sell every tick, but if the desired prices
                        // haven't moved we want to LEAVE the existing orders alone — they
                        // need to age before Polymarket counts them toward rewards
                        // (`are_orders_scoring` returns false on orders <60s old, roughly).
                        let mut shutdown_requested = false;
                        let mut cancel_tokens: Vec<String> = Vec::new();
                        let mut desired: Vec<(Signal, String, Decimal, Decimal)> = Vec::new(); // (signal, token, price, size)

                        for signal in signals {
                            match signal {
                                Signal::Hold => continue,
                                Signal::Shutdown { reason } => {
                                    tracing::info!(reason = reason.as_str(), "Strategy requested shutdown");
                                    shutdown_requested = true;
                                }
                                // Defensive arm: StrategyRuntime::tick handles
                                // and consumes StrategyComplete before signals
                                // ever reach the engine main loop. If one
                                // somehow leaks through, treat it as a no-op
                                // rather than panicking — the retirement was
                                // already logged inside tick().
                                Signal::StrategyComplete { .. } => continue,
                                Signal::Subscribe { token_id } => {
                                    self.subscribe_token(&token_id).await;
                                }
                                Signal::Unsubscribe { token_id } => {
                                    self.unsubscribe_token(&token_id).await;
                                }
                                Signal::Alert { reason, suggested, ttl_secs, dedupe_key } => {
                                    // The Box<Signal> inside the variant carries
                                    // the order to dispatch on approval. We push
                                    // it onto the queue (dedup by key); if the
                                    // queue accepts it, fire a notifier — spawned
                                    // so a slow/down ntfy.sh doesn't stall the
                                    // tick.
                                    let suggested_signal = *suggested;
                                    match self.alert_queue.push(
                                        reason.clone(),
                                        suggested_signal,
                                        ttl_secs,
                                        dedupe_key.clone(),
                                    ) {
                                        Some(_id) => {
                                            // Notify on the freshly inserted alert.
                                            let alert = self
                                                .alert_queue
                                                .list()
                                                .into_iter()
                                                .find(|a| a.dedupe_key == dedupe_key);
                                            if let Some(alert) = alert {
                                                tracing::info!(
                                                    alert_id = %alert.id,
                                                    reason = %reason,
                                                    "Alert raised"
                                                );
                                                let notifier = self.notifier.clone();
                                                tokio::spawn(async move {
                                                    notifier.notify(&alert).await;
                                                });
                                            }
                                        }
                                        None => {
                                            tracing::debug!(
                                                dedupe_key = %dedupe_key,
                                                "Alert deduped (active key already present)"
                                            );
                                        }
                                    }
                                }
                                Signal::Cancel { token_id } => {
                                    cancel_tokens.push(token_id);
                                }
                                Signal::CancelOrder { order_id } => {
                                    match self.client.cancel_order(&order_id).await {
                                        Ok(_) => {
                                            tracing::info!(order_id = %order_id, "CancelOrder signal: cancelled");
                                            self.risk_manager.release_order(&order_id);
                                        }
                                        Err(e) => {
                                            tracing::warn!(error = %e, order_id = %order_id, "CancelOrder signal: cancel failed");
                                        }
                                    }
                                }
                                Signal::Buy { token_id, price, size, urgency } => {
                                    // Don't pre-round price here. OrderManager rounds to the
                                    // per-market tick when actually placing; the delta-quote
                                    // matcher below uses a half-tick tolerance so any
                                    // sub-tick wobble in the strategy's target still matches
                                    // an existing aged order.
                                    let size = size.round_dp(2);
                                    let sig = Signal::Buy {
                                        token_id: token_id.clone(),
                                        price,
                                        size,
                                        urgency,
                                    };
                                    desired.push((sig, token_id, price, size));
                                }
                                Signal::Sell { token_id, price, size, urgency } => {
                                    let size = size.round_dp(2);
                                    let sig = Signal::Sell {
                                        token_id: token_id.clone(),
                                        price,
                                        size,
                                        urgency,
                                    };
                                    desired.push((sig, token_id, price, size));
                                }
                            }
                        }

                        // For each token a Cancel signal touched: look at currently open
                        // orders. If an open order matches one of the desired orders for
                        // that token (same side + price + size), keep it alive and remove
                        // it from the "to place" list — that order stays aged. Otherwise
                        // cancel it.
                        for token_id in &cancel_tokens {
                            let active: Vec<(String, bool, Decimal, Decimal)> = self
                                .order_manager
                                .active_orders_for_token(token_id)
                                .iter()
                                .map(|o| (o.id.clone(), o.is_buy, o.price, o.size))
                                .collect();
                            for (id, is_buy, price, size) in active {
                                // Find a desired order that matches this open order.
                                // Tolerance: half a tick (0.0005) so a 0.1¢ mid wiggle
                                // that shifts the target by 0.1¢ doesn't force a
                                // cancel+replace and re-age. The reward score weight
                                // changes very little inside ±0.5 tick anyway, but a
                                // re-quote restarts the order-age timer from zero,
                                // costing far more than the precision gained.
                                let price_tol = rust_decimal::Decimal::new(5, 4); // 0.0005
                                let matched_idx = desired.iter().position(|(s, t, p, sz)| {
                                    t == token_id
                                        && (*p - price).abs() <= price_tol
                                        && *sz == size
                                        && matches!(
                                            (s, is_buy),
                                            (Signal::Buy { .. }, true) | (Signal::Sell { .. }, false)
                                        )
                                });
                                if let Some(i) = matched_idx {
                                    // Already have this order — keep it, drop from desired.
                                    desired.remove(i);
                                    tracing::debug!(
                                        order_id = %id,
                                        token_id = %token_id,
                                        price = %price,
                                        size = %size,
                                        "Keeping aged order (matches desired quote)"
                                    );
                                } else {
                                    // No matching desired — cancel it.
                                    if let Err(e) = self.order_manager.cancel_order(&id).await {
                                        tracing::warn!(error = %e, "Cancel failed for stale order");
                                    } else {
                                        self.risk_manager.release_order(&id);
                                    }
                                }
                            }
                        }

                        // Drop the original `signals` iteration and place remaining desired.
                        let signals_to_place: Vec<Signal> =
                            desired.into_iter().map(|(s, _, _, _)| s).collect();

                        for signal in signals_to_place {
                            match self.risk_manager.check_signal(&signal, &self.positions) {
                                RiskCheckResult::Approved(ref s) | RiskCheckResult::Reduced(ref s, _) => {
                                    if let RiskCheckResult::Reduced(_, ref reason) = self.risk_manager.check_signal(&signal, &self.positions) {
                                        tracing::warn!(reason = reason.as_str(), "Signal reduced by risk manager");
                                    }

                                    // Extract order details for tracking
                                    let (token_id, price, size) = match s {
                                        Signal::Buy { token_id, price, size, .. } => (token_id.clone(), *price, *size),
                                        Signal::Sell { token_id, price, size, .. } => (token_id.clone(), *price, *size),
                                        _ => continue,
                                    };

                                    let notional = price * size;

                                    // CRITICAL: Reserve exposure BEFORE placing order
                                    // This prevents race conditions where multiple signals
                                    // pass the risk check in the same tick
                                    let reservation_id = match self.risk_manager.reserve_exposure(
                                        &token_id,
                                        notional,
                                        &self.positions,
                                    ) {
                                        Some(id) => id,
                                        None => {
                                            tracing::warn!(
                                                token_id = token_id.as_str(),
                                                notional = %notional,
                                                "Skipping order: exposure reservation rejected"
                                            );
                                            continue;
                                        }
                                    };

                                    match self.order_manager.execute(s.clone()).await {
                                        Ok(Some(order_id)) => {
                                            // Confirm the reservation as an open order
                                            self.risk_manager.confirm_reservation(&reservation_id, &order_id);
                                        }
                                        Ok(None) => {
                                            // Order was not placed (e.g., dry-run mode)
                                            // Release the reservation
                                            self.risk_manager.release_reservation(&reservation_id);
                                        }
                                        Err(e) => {
                                            tracing::error!(error = %e, "Order execution failed");
                                            // Release the reservation on failure
                                            self.risk_manager.release_reservation(&reservation_id);
                                        }
                                    }
                                }
                                RiskCheckResult::Rejected(reason) => {
                                    tracing::warn!(reason = reason, "Signal rejected by risk manager");
                                }
                            }
                        }

                        // Handle shutdown request from strategies
                        if shutdown_requested {
                            self.shutdown().await?;
                            break 'reconnect;
                        }
                    }

                    // Process fills
                    Some(fill) = self.fill_receiver.recv() => {
                        tracing::info!(
                            order_id = fill.order_id,
                            token_id = fill.token_id,
                            price = %fill.price,
                            size = %fill.size,
                            "Processing fill"
                        );

                        // Update positions
                        self.positions.apply_fill(&fill);

                        // Notify strategies
                        self.strategy_runtime.on_fill(&fill);

                        // Update risk manager - close tracked order
                        self.risk_manager.order_closed(&fill.order_id);

                        // Log current exposure
                        let exposure = self.risk_manager.current_exposure(&self.positions);
                        let remaining = self.risk_manager.remaining_capacity(&self.positions);
                        tracing::info!(
                            exposure = %exposure,
                            remaining_capacity = %remaining,
                            "Exposure after fill"
                        );
                    }

                    // Fill events from the trades poller — feed them to the
                    // order manager so it can update its tracked order state
                    // and emit a downstream Fill event for the arm above.
                    Some(ev) = self.fill_event_receiver.recv() => {
                        // Skip trades not involving an engine-placed order.
                        if self.order_manager.get_order(&ev.order_id).is_none() {
                            tracing::debug!(
                                order_id = %ev.order_id,
                                "Fill for untracked order (manual or other session); ignoring"
                            );
                            continue;
                        }
                        if let Err(e) = self
                            .order_manager
                            .process_fill(&ev.order_id, ev.price, ev.size, ev.fee)
                            .await
                        {
                            tracing::warn!(error = %e, order_id = %ev.order_id, "process_fill failed");
                        } else {
                            tracing::info!(
                                order_id = %ev.order_id,
                                price = %ev.price,
                                size = %ev.size,
                                fee = %ev.fee,
                                "Fill detected via trades poll"
                            );
                        }
                    }

                    // WebSocket market data
                    Some(book_result) = async {
                        match ws_stream.as_mut() {
                            Some(stream) => stream.next().await,
                            None => std::future::pending().await,
                        }
                    } => {
                        match book_result {
                            Ok(book) => {
                                ws_update_count += 1;
                                let token_id = book.asset_id.to_string();

                                // Log periodically to show WebSocket is receiving data
                                if ws_update_count % 100 == 1 {
                                    tracing::info!(
                                        ws_update_count = ws_update_count,
                                        books_populated = self.market_data.book_count().await,
                                        "WebSocket updates received"
                                    );
                                }

                                tracing::debug!(
                                    token_id = %token_id,
                                    best_bid = ?book.bids.first().map(|b| b.price),
                                    best_ask = ?book.asks.first().map(|a| a.price),
                                    bid_levels = book.bids.len(),
                                    ask_levels = book.asks.len(),
                                    update_count = ws_update_count,
                                    "Orderbook update"
                                );

                                // Process through market data hub (full depth + broadcast)
                                self.market_data.process_book_update(book).await;

                                // Update position prices for P&L tracking
                                if let Some(book) = self.market_data.get_book(&token_id).await {
                                    if let Some(mid) = book.mid_price() {
                                        let mut prices = HashMap::new();
                                        prices.insert(token_id, mid);
                                        self.positions.update_prices(&prices);
                                    }
                                }
                            }
                            Err(e) => {
                                tracing::error!(error = %e, "WebSocket orderbook error");
                            }
                        }
                    }

                    // Control plane commands. Built inline against live
                    // engine state so we never need to share the state via
                    // Arc/RwLock — the select! is the synchronization point.
                    Some(cmd) = cmd_rx.recv() => {
                        match cmd {
                            EngineCommand::GetStatus(reply) => {
                                let report = StatusReport {
                                    uptime_secs: start_instant.elapsed().as_secs(),
                                    tick_count,
                                    dry_run: self.is_dry_run(),
                                    balance_usdc: *self.usdc_balance.read().await,
                                    subscribed_tokens: self.subscribed_tokens.len(),
                                    strategies: self.strategy_runtime.summaries().len(),
                                    open_orders: self.risk_manager.total_open_orders(),
                                    total_exposure_usd: self
                                        .risk_manager
                                        .current_exposure(&self.positions),
                                    realized_pnl: self.positions.total_realized_pnl(),
                                    unrealized_pnl: self.positions.total_unrealized_pnl(),
                                    halted: self.risk_manager.is_halted(),
                                };
                                let _ = reply.send(report);
                            }
                            EngineCommand::ListStrategies(reply) => {
                                let _ = reply.send(self.strategy_infos());
                            }
                            EngineCommand::ListOrders(reply) => {
                                let orders: Vec<OrderInfo> = self
                                    .order_manager
                                    .active_orders_snapshot()
                                    .into_iter()
                                    .map(|o| OrderInfo {
                                        id: o.id,
                                        token_id: o.token_id,
                                        side: if o.is_buy { "buy" } else { "sell" },
                                        price: o.price,
                                        size: o.size,
                                        filled: o.filled_size,
                                        status: format!("{:?}", o.status),
                                        created_at: o.created_at,
                                    })
                                    .collect();
                                let _ = reply.send(orders);
                            }
                            EngineCommand::ListAlerts(reply) => {
                                self.alert_queue.prune_expired();
                                let _ = reply.send(self.alert_queue.list());
                            }
                            EngineCommand::ApproveAlert { id, reply } => {
                                let res = self.approve_alert(&id).await;
                                let _ = reply.send(res);
                            }
                            EngineCommand::RejectAlert { id, reply } => {
                                let res = match self.alert_queue.take(&id) {
                                    None => Err(format!("alert {} not found", id)),
                                    Some(_) => {
                                        tracing::info!(alert_id = %id, "Alert rejected");
                                        Ok(())
                                    }
                                };
                                let _ = reply.send(res);
                            }
                            EngineCommand::ListSubscriptions(reply) => {
                                let _ = reply.send(self.subscribed_tokens.clone());
                            }
                            EngineCommand::SubscribeToken { token_id, reply } => {
                                self.subscribe_token(&token_id).await;
                                let _ = reply.send(());
                            }
                            EngineCommand::UnsubscribeToken { token_id, reply } => {
                                self.unsubscribe_token(&token_id).await;
                                let _ = reply.send(());
                            }
                            EngineCommand::ListTrades { token_id, since_ts, reply } => {
                                let cutoff = since_ts.unwrap_or(i64::MIN);
                                let trades: Vec<TradeInfo> = self
                                    .market_data
                                    .recent_trades(&token_id)
                                    .await
                                    .into_iter()
                                    .filter(|t| t.timestamp >= cutoff)
                                    .map(|t| TradeInfo {
                                        token_id: t.token_id,
                                        price: t.price,
                                        size: t.size,
                                        side: t.side,
                                        timestamp: t.timestamp,
                                    })
                                    .collect();
                                let _ = reply.send(trades);
                            }
                            EngineCommand::RegisterExternalOrder { order, reply } => {
                                tracing::info!(
                                    order_id = %order.id,
                                    token_id = %order.token_id,
                                    source = %order.source,
                                    "External order registered"
                                );
                                self.external_orders.insert(order.id.clone(), order);
                                let _ = reply.send(());
                            }
                            EngineCommand::MarkExternalCancelled { order_id, reply } => {
                                let removed = self.external_orders.remove(&order_id).is_some();
                                if removed {
                                    tracing::info!(
                                        order_id = %order_id,
                                        "External order marked cancelled"
                                    );
                                }
                                let _ = reply.send(());
                            }
                            EngineCommand::ListAllOrders(reply) => {
                                let _ = reply.send(self.all_orders());
                            }
                            EngineCommand::CancelOrderById { order_id, reply } => {
                                let res = self.cancel_order_by_id(&order_id).await;
                                let _ = reply.send(res);
                            }
                            EngineCommand::ScheduleCancel { order_id, at, reply } => {
                                tracing::info!(
                                    order_id = %order_id,
                                    at = %at,
                                    "TTL cancel scheduled"
                                );
                                self.pending_cancellations.push((at, order_id));
                                let _ = reply.send(());
                            }
                            EngineCommand::PlaceOrder { token_id, side, price, size, reply } => {
                                let res = self.place_order_for_cli(&token_id, &side, price, size).await;
                                let _ = reply.send(res);
                            }
                            EngineCommand::PauseStrategy { id, reply } => {
                                match self.strategy_runtime.pause(&id) {
                                    Some(tokens) => {
                                        // Pull the strategy's resting quotes so it
                                        // stops sitting on the book (and stops the
                                        // bid-erroring loop when cash is starved).
                                        for token in &tokens {
                                            let _ = self.order_manager.cancel_all(token).await;
                                        }
                                        tracing::info!(strategy_id = %id, "Strategy paused via control plane");
                                        let _ = reply.send(Ok(()));
                                    }
                                    None => { let _ = reply.send(Err(format!("no strategy '{}'", id))); }
                                }
                            }
                            EngineCommand::ResumeStrategy { id, reply } => {
                                if self.strategy_runtime.resume(&id) {
                                    tracing::info!(strategy_id = %id, "Strategy resumed via control plane");
                                    let _ = reply.send(Ok(()));
                                } else {
                                    let _ = reply.send(Err(format!("no strategy '{}'", id)));
                                }
                            }
                            EngineCommand::StopStrategy { id, reply } => {
                                match self.strategy_runtime.stop(&id) {
                                    Some(tokens) => {
                                        // Full unsubscribe, not just cancels: stop is
                                        // permanent, and skipping the exposure-ledger
                                        // release re-created the a102366 ghost-exposure
                                        // freeze through this side door (risk manager
                                        // counting a dead strategy's positions forever).
                                        for token in &tokens {
                                            self.unsubscribe_token(token).await;
                                        }
                                        tracing::info!(strategy_id = %id, "Strategy stopped + removed via control plane");
                                        let _ = reply.send(Ok(()));
                                    }
                                    None => { let _ = reply.send(Err(format!("no strategy '{}'", id))); }
                                }
                            }
                            EngineCommand::StrategyCommand { id, body, reply } => {
                                let res = self.strategy_runtime.command(&id, &body);
                                if let Ok(ref v) = res {
                                    tracing::info!(strategy_id = %id, reply = %v, "Strategy command handled");
                                }
                                let _ = reply.send(res);
                            }
                        }
                    }

                    // Shutdown signal
                    _ = shutdown_rx.recv() => {
                        tracing::info!("Shutting down engine");
                        self.shutdown().await?;
                        break 'reconnect;
                    }
                }
            }
        }

        poller_handle.abort();
        balance_handle.abort();
        trades_handle.abort();
        trade_tape_handle.abort();
        control_handle.abort();
        if let Some(h) = scanner_handle {
            h.abort();
        }
        tracing::debug!("Background pollers stopped");

        Ok(())
    }

    // --- control-plane command handlers ---
    //
    // The long EngineCommand arms live here rather than inline in run()'s
    // select!, which keeps that loop readable as a dispatch table. Each takes
    // &mut self and returns what the arm sends down its oneshot; none of them
    // touch the reply channel.

    /// `pmt engine strategies`. `last_tick_at` is an Instant, so it becomes a
    /// wall-clock time by aligning the monotonic delta against `now_utc` read
    /// at the same moment.
    fn strategy_infos(&self) -> Vec<StrategyInfo> {
        let now_instant = std::time::Instant::now();
        let now_utc = chrono::Utc::now();
        self.strategy_runtime.summaries().into_iter().map(|s| StrategyInfo {
            id: s.id,
            tick_interval_ms: s.tick_interval_ms,
            subscribed_tokens: s.subscriptions,
            paused: s.paused,
            last_tick_at: s.last_tick_at.map(|t| {
                let elapsed = now_instant.saturating_duration_since(t);
                now_utc - chrono::Duration::from_std(elapsed)
                    .unwrap_or_else(|_| chrono::Duration::zero())
            }),
        }).collect()
    }

    /// Engine-placed and externally-registered orders in one list, so
    /// `pmt orders` sees everything resting under this account.
    fn all_orders(&self) -> Vec<crate::control::UnifiedOrderInfo> {
        let mut combined: Vec<crate::control::UnifiedOrderInfo> = self
            .order_manager
            .active_orders_snapshot()
            .into_iter()
            .map(|o| crate::control::UnifiedOrderInfo {
                id: o.id,
                token_id: o.token_id,
                side: if o.is_buy { "buy" } else { "sell" }.to_string(),
                price: o.price,
                size: o.size,
                filled: o.filled_size,
                status: format!("{:?}", o.status),
                created_at: o.created_at,
                source: "engine".to_string(),
            })
            .collect();
        combined.extend(self.external_orders.values().map(|ext| crate::control::UnifiedOrderInfo {
            id: ext.id.clone(),
            token_id: ext.token_id.clone(),
            side: ext.side.clone(),
            price: ext.price,
            size: ext.size,
            filled: Decimal::ZERO,
            status: "external".to_string(),
            created_at: ext.created_at,
            source: ext.source.clone(),
        }));
        combined
    }

    /// Cancel any order by id, engine-placed or external. Both cleanup calls
    /// are no-ops when the id isn't present on that side.
    async fn cancel_order_by_id(&mut self, order_id: &str) -> Result<(), String> {
        match self.client.cancel_order(order_id).await {
            Ok(_) => {
                self.external_orders.remove(order_id);
                self.risk_manager.release_order(order_id);
                tracing::info!(order_id = %order_id, "CancelOrderById succeeded");
                Ok(())
            }
            Err(e) => {
                tracing::warn!(error = %e, order_id = %order_id, "CancelOrderById failed");
                Err(format!("cancel failed: {}", e))
            }
        }
    }

    /// Run an approved alert's suggested signal through the normal risk +
    /// order pipeline, exactly as if the strategy had emitted it directly.
    /// Every refusal reason bubbles up to the HTTP response.
    async fn approve_alert(&mut self, id: &str) -> Result<String, String> {
        self.alert_queue.prune_expired();
        let (_, sig) = self.alert_queue.take(id)
            .ok_or_else(|| format!("alert {} not found", id))?;
        let signal = match self.risk_manager.check_signal(&sig, &self.positions) {
            RiskCheckResult::Approved(s) | RiskCheckResult::Reduced(s, _) => s,
            RiskCheckResult::Rejected(reason) => return Err(format!("risk rejected: {}", reason)),
        };
        let (token_id, price, size) = match &signal {
            Signal::Buy { token_id, price, size, .. }
            | Signal::Sell { token_id, price, size, .. } => (token_id.clone(), *price, *size),
            _ => return Err("suggested is not Buy/Sell".to_string()),
        };
        let reservation_id = self
            .risk_manager
            .reserve_exposure(&token_id, price * size, &self.positions)
            .ok_or_else(|| "exposure reservation rejected".to_string())?;
        match self.order_manager.execute(signal).await {
            Ok(Some(order_id)) => {
                self.risk_manager.confirm_reservation(&reservation_id, &order_id);
                Ok(order_id)
            }
            Ok(None) => {
                self.risk_manager.release_reservation(&reservation_id);
                Ok("dry-run".to_string())
            }
            Err(e) => {
                self.risk_manager.release_reservation(&reservation_id);
                Err(format!("execute failed: {}", e))
            }
        }
    }

    /// `pmt buy/sell` routed through the engine so the account-wide rate limit
    /// and the tick-size cache are shared with the strategies. The cached tick
    /// lookup is free after first touch, which is what lets the CLI drop its
    /// own tick_size REST call.
    async fn place_order_for_cli(
        &mut self, token_id: &str, side: &str, price: Decimal, size: Decimal,
    ) -> Result<String, String> {
        let sdk_side = match side {
            "buy" => crate::client::Side::Buy,
            "sell" => crate::client::Side::Sell,
            _ => return Err(format!("invalid side '{}'", side)),
        };
        let rounded_price = match self.client.tick_decimals_for(token_id).await {
            Ok(decimals) => price.round_dp(decimals),
            Err(e) => return Err(format!("tick lookup failed: {}", e)),
        };
        match self.client.place_limit_order(token_id, sdk_side, rounded_price, size).await {
            Ok(order_id) => {
                self.external_orders.insert(order_id.clone(), crate::control::ExternalOrder {
                    id: order_id.clone(),
                    token_id: token_id.to_string(),
                    side: side.to_string(),
                    price: rounded_price,
                    size,
                    source: "cli-via-engine".to_string(),
                    created_at: chrono::Utc::now(),
                });
                tracing::info!(
                    order_id = %order_id, token_id = %token_id, side = %side,
                    price = %rounded_price, size = %size,
                    "PlaceOrder (CLI via engine) succeeded"
                );
                Ok(order_id)
            }
            Err(e) => {
                tracing::warn!(error = %e, token_id = %token_id, "PlaceOrder (CLI via engine) failed");
                Err(format!("place failed: {}", e))
            }
        }
    }

    /// Graceful shutdown: cancel all orders and cleanup.
    async fn shutdown(&mut self) -> Result<(), EngineError> {
        self.shutdown = true;

        // Cancel all open orders
        let cancelled = self.order_manager.cancel_all_orders().await
            .map_err(|e| EngineError::OrderError(e.to_string()))?;
        tracing::info!(count = cancelled, "Cancelled orders on shutdown");

        // Shutdown strategies
        self.strategy_runtime.shutdown();

        // Log final P&L
        let realized = self.positions.total_realized_pnl();
        let unrealized = self.positions.total_unrealized_pnl();
        tracing::info!(
            realized_pnl = %realized,
            unrealized_pnl = %unrealized,
            total_pnl = %(realized + unrealized),
            "Final P&L"
        );

        Ok(())
    }

}

#[derive(Debug)]
pub enum EngineError {
    ConfigError(String),
    SdkError(String),
    OrderError(String),
    UnknownStrategy(String),
    ControlPlaneError(String),
}

impl std::fmt::Display for EngineError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            EngineError::ConfigError(e) => write!(f, "Config error: {}", e),
            EngineError::SdkError(e) => write!(f, "SDK error: {}", e),
            EngineError::OrderError(e) => write!(f, "Order error: {}", e),
            EngineError::UnknownStrategy(name) => write!(f, "Unknown strategy: {}", name),
            EngineError::ControlPlaneError(e) => write!(f, "Control plane error: {}", e),
        }
    }
}

impl std::error::Error for EngineError {}

impl From<crate::config::ConfigError> for EngineError {
    fn from(e: crate::config::ConfigError) -> Self {
        EngineError::ConfigError(e.to_string())
    }
}
