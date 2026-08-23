//! Main event loop for the trading engine.

use crate::alerts::{AlertQueue, Notifier};
use crate::client::PolymarketClient;
use crate::config::Config;
use crate::control::{
    self, EngineCommand, OrderInfo, StatusReport, StrategyInfo, TradeInfo,
};
use crate::gamma::GammaClient;
use crate::order::OrderManager;
use crate::orderbook::MarketDataHub;
use crate::position::{Fill, PositionTracker};
use crate::risk::{RiskCheckResult, RiskLimits, RiskManager};
use crate::strategy::{Signal, StrategyContext, StrategyRuntime};
use crate::wsfeed::{WsFeed, WsHealth};

#[cfg(feature = "sigv4")]
use crate::sigv4::SigV4Signer;

use rust_decimal::Decimal;
use std::collections::HashMap;
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
    /// The authoritative market-data feed. Subscriptions are incremental, so
    /// a runtime arm/roll adds and drops tokens without touching the socket —
    /// the old `ws_needs_reconnect` flag existed because the WS was rebuilt
    /// wholesale, and only the since-deleted market-discovery branch acted on it.
    ws_feed: WsFeed,
    /// Shared health of `ws_feed`, read by the REST poller and `/status`.
    ws_health: Arc<WsHealth>,
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
    /// Gamma client used to resolve token_id → condition_id on subscription.
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

/// One quote the strategies want live this tick, carrying its Phase 7
/// decision id from the moment it is bucketed — so a fire the delta matcher
/// suppresses and a fire that reaches the wire are both identifiable on the
/// same tape.
struct Desired {
    signal: Signal,
    token_id: String,
    price: Decimal,
    size: Decimal,
    decision_id: String,
}

/// Tape label for a signal's side.
fn signal_side(signal: &Signal) -> &'static str {
    match signal {
        Signal::Sell { .. } => "sell",
        _ => "buy",
    }
}

/// Read a `u64` tunable from the environment, falling back to `default`.
fn env_ms(var: &str, default: u64) -> u64 {
    std::env::var(var)
        .ok()
        .and_then(|s| s.parse().ok())
        .unwrap_or(default)
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
            max_loss: Decimal::from_f64_retain(config.max_loss)
                .unwrap_or(Decimal::from(25)),
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

        // Start the WS feed before any strategy registers — registration
        // subscribes its static tokens, and there must be somewhere for
        // those subscriptions to land.
        let ws_feed = WsFeed::spawn(market_data.clone());
        let ws_health = ws_feed.health();

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
            ws_feed,
            ws_health,
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

    /// Set whether to skip warmup period.
    ///
    /// When true, the engine will start trading immediately without waiting
    /// for WebSocket order book data. Useful when WS connection is unavailable.
    pub fn set_skip_warmup(&mut self, skip: bool) {
        self.skip_warmup = skip;
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
    /// a no-op. Initialises an empty book in the MarketDataHub (which both
    /// enrolls it in the REST health poll and makes it a valid target for WS
    /// writes) and streams it on the market WebSocket.
    ///
    /// This is the path the updown arm/roll takes every five minutes, and it
    /// is where the WS used to lose: the old code set a reconnect flag that
    /// only the since-deleted market-discovery branch read.
    pub async fn subscribe_token(&mut self, token_id: &str) {
        if self.subscribed_tokens.iter().any(|t| t == token_id) {
            return;
        }
        self.market_data.init_book(token_id).await;
        self.subscribed_tokens.push(token_id.to_string());
        self.ws_feed.subscribe(token_id);
        self.ensure_condition_id(token_id).await;
        // Pull the token's tick size and neg-risk flag now, off the tick —
        // the order path would otherwise pay for them inside decision->ack.
        // Spawned, not awaited: nothing this tick needs the answer.
        let client = self.client.clone();
        let token = token_id.to_string();
        tokio::spawn(async move { client.prewarm_token(&token).await });
        tracing::info!(token_id = %token_id, "Subscribed to token");
    }

    /// Stop watching a token at runtime. Cancels any open orders on the
    /// token first (locally + on Polymarket), releases their risk-manager
    /// reservations, drops the position from the exposure ledger, removes
    /// the book from the hub, and stops streaming it.
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
        self.ws_feed.unsubscribe(token_id);
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
                self.ws_feed.subscribe(&token_id);
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
                    // The WS feed must hear about these too — recovery-restored
                    // arms come back subscribed=true and never re-emit
                    // Signal::Subscribe, so this sync path was leaving every
                    // recovered token REST-only (found on the first live boot:
                    // 8 books, ws_tokens=0). subscribe() is a channel send,
                    // safe from sync context.
                    self.ws_feed.subscribe(&token_id);
                }
                futures::executor::block_on(self.ensure_condition_id(&token_id));
            }
            self.strategy_runtime.register(strategy);

            tracing::info!(strategy = name.as_str(), "Loaded strategy");
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
                    // strategy's matched markets, one highest-certainty
                    // token per market — never the cheap losing side.
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
                    // happily drop a statically-armed token the moment it
                    // doesn't match some other strategy's filter.
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

        // Spawn the REST book poller — a health check and a fallback, no
        // longer the book source.
        //
        // While the WS is connected it runs at the slow cadence: it costs a
        // request per token per cycle, and every snapshot it takes loses the
        // hub's staleness compare against live WS state anyway. The moment
        // the socket drops it returns to the 2s cadence, which is exactly the
        // behaviour that was shipping before the WS became authoritative —
        // degrade, don't starve. (The 30s in `WsHealth::degraded` is the
        // warn/report threshold, deliberately NOT the cadence trigger: at a
        // 10s poll, waiting 30s to speed up means trading a 10s-old book.)
        let poller_handle = {
            let client = self.client.clone();
            let hub = self.market_data.clone();
            let health = self.ws_health.clone();
            let fast = Duration::from_millis(env_ms("PMENGINE_BOOK_POLL_MS", 2000));
            let slow = Duration::from_millis(env_ms("PMENGINE_BOOK_POLL_SLOW_MS", 10_000));
            tokio::spawn(async move {
                loop {
                    let cadence = if health.is_connected() { slow } else { fast };
                    tokio::time::sleep(cadence).await;
                    let tokens: Vec<String> =
                        hub.get_all_books().await.keys().cloned().collect();
                    for token in tokens {
                        match client.get_book(&token).await {
                            Ok(book) => {
                                if !hub.apply_rest_book(book).await {
                                    tracing::debug!(
                                        token = %token,
                                        "REST snapshot dropped: WS book is fresher"
                                    );
                                }
                            }
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
        let mut halt_logged = false;

        // Book-health heartbeat: one INFO line carrying the WS state and the
        // book-age distribution. This is the number the operator watches — a
        // p50 in the hundreds of ms means the WS is carrying the book, a p50
        // near the REST cadence means it isn't, whatever the connection flag
        // claims.
        let mut book_health_timer = interval(Duration::from_secs(
            env_ms("PMENGINE_BOOK_HEALTH_S", 60),
        ));
        book_health_timer.tick().await;
        let mut last_health_events: u64 = 0;
        let mut last_health_at = Instant::now();

        tracing::info!("Entering event loop");

        // Warmup: wait for books to sync. Any ONE of three exits is
        // sufficient — `--skip-warmup`, WARMUP_WS_UPDATES streamed events,
        // or WARMUP_DEADLINE wall clock. A still-missing book is the
        // strategy's own `if book is None` guard's problem.
        // See docs/LESSONS.md#L35.
        const WARMUP_WS_UPDATES: u64 = 100;
        const WARMUP_DEADLINE: std::time::Duration = std::time::Duration::from_secs(10);
        let warmup_started_at = std::time::Instant::now();
        let mut warmup_complete = false;

        loop {
            tokio::select! {

                // Periodic book-health line.
                _ = book_health_timer.tick() => {
                    let stats = self.market_data.book_age_stats().await;
                    let events = self.ws_health.events();
                    let secs = last_health_at.elapsed().as_secs_f64().max(0.001);
                    let rate = (events - last_health_events) as f64 / secs;
                    last_health_events = events;
                    last_health_at = Instant::now();
                    tracing::info!(
                        ws_connected = self.ws_health.is_connected(),
                        ws_tokens = self.ws_health.tokens(),
                        ws_events = events,
                        ws_events_per_s = format!("{:.1}", rate),
                        ws_last_event_age_ms = ?self.ws_health.last_event_age_ms(),
                        books = stats.books,
                        from_ws = stats.from_ws,
                        from_rest = stats.from_rest,
                        never_fed = stats.never_fed,
                        book_age_p50_ms = ?stats.p50_ms,
                        book_age_p90_ms = ?stats.p90_ms,
                        book_age_max_ms = ?stats.max_ms,
                        "Book health"
                    );
                }

                // Tick timer for strategy evaluation
                _ = tick_timer.tick() => {
                    tick_count += 1;
                    let elapsed = last_tick.elapsed();
                    last_tick = Instant::now();

                    // Every tick at INFO was ~20 lines/s and ~10MB of log an
                    // hour; a 30s heartbeat proves the loop is alive just as
                    // well. Liveness checks read the control plane, not this.
                    if tick_count.is_multiple_of(600) {
                        tracing::info!(tick = tick_count, elapsed_ms = elapsed.as_millis(), "Tick");
                    }

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
                        break;
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
                        } else if self.ws_health.events() >= WARMUP_WS_UPDATES {
                            warmup_complete = true;
                            tracing::info!(
                                ws_updates = self.ws_health.events(),
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
                                ws_updates = self.ws_health.events(),
                                required = WARMUP_WS_UPDATES,
                                "Warmup in progress, skipping trading"
                            );
                            continue;
                        }
                    }

                    // Check P&L for circuit breaker
                    self.risk_manager.check_pnl(&self.positions);

                    if self.risk_manager.is_halted() {
                        // Once per halt, not per 50ms tick — the 2026-08-23
                        // halt wrote ~20 lines/s of the same fact.
                        if !halt_logged {
                            halt_logged = true;
                            tracing::error!(
                                "ENGINE HALTED by circuit breaker — no evals, fires or rolls until restart"
                            );
                        }
                        continue;
                    }

                    // Mark positions off the live book. This used to ride on
                    // the WS select arm; now that the feed owns its own task,
                    // the tick is where engine state is mutable — and it runs
                    // at 50ms, far tighter than the marks ever needed.
                    let books = self.market_data.get_all_books().await;
                    let marks: HashMap<String, Decimal> = books
                        .iter()
                        .filter_map(|(t, b)| b.mid_price().map(|m| (t.clone(), m)))
                        .collect();
                    if !marks.is_empty() {
                        self.positions.update_prices(&marks);
                    }

                    // Build strategy context with full-depth order books
                    let ctx = StrategyContext {
                        timestamp: chrono::Utc::now(),
                        order_books: books,
                        trade_history: self.market_data.get_all_trade_history().await,
                        positions: self.positions.clone(),
                        // Empty since the legacy gamma-discovery refresh was
                        // removed — it was the only thing that ever filled
                        // this map, and only the deleted `sure_bets` /
                        // `dynamic_market_maker` ever read it. A strategy
                        // that wants market metadata declares `market_filter()`
                        // and works off `order_books`.
                        markets: HashMap::new(),
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
                    let mut desired: Vec<Desired> = Vec::new();

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
                                let signal = Signal::Buy {
                                    token_id: token_id.clone(),
                                    price,
                                    size,
                                    urgency,
                                };
                                desired.push(Desired {
                                    signal,
                                    token_id,
                                    price,
                                    size,
                                    decision_id: crate::order_tape::next_decision_id(),
                                });
                            }
                            Signal::Sell { token_id, price, size, urgency } => {
                                let size = size.round_dp(2);
                                let signal = Signal::Sell {
                                    token_id: token_id.clone(),
                                    price,
                                    size,
                                    urgency,
                                };
                                desired.push(Desired {
                                    signal,
                                    token_id,
                                    price,
                                    size,
                                    decision_id: crate::order_tape::next_decision_id(),
                                });
                            }
                        }
                    }

                    // For each token a Cancel signal touched: look at currently open
                    // orders. If an open order matches one of the desired orders for
                    // that token (same side + price + size), keep it alive and remove
                    // it from the "to place" list — that order stays aged. Otherwise
                    // cancel it.
                    let mut stale: Vec<String> = Vec::new();
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
                            let matched_idx = desired.iter().position(|d| {
                                d.token_id == *token_id
                                    && (d.price - price).abs() <= price_tol
                                    && d.size == size
                                    && matches!(
                                        (&d.signal, is_buy),
                                        (Signal::Buy { .. }, true) | (Signal::Sell { .. }, false)
                                    )
                            });
                            if let Some(i) = matched_idx {
                                // Already have this order — keep it, drop from desired.
                                let kept = desired.remove(i);
                                tracing::debug!(
                                    order_id = %id,
                                    token_id = %token_id,
                                    price = %price,
                                    size = %size,
                                    "Keeping aged order (matches desired quote)"
                                );
                                // Phase 7: a fire that never reaches the wire is
                                // still a decision. Section 7 could only count
                                // these (8.7% of firings) by their ABSENCE from
                                // the log; the tape names them.
                                crate::order_tape::record_suppressed(
                                    &kept.decision_id,
                                    &kept.token_id,
                                    signal_side(&kept.signal),
                                    kept.price,
                                    kept.size,
                                    &id,
                                );
                            } else {
                                stale.push(id);
                            }
                        }
                    }

                    // Retire the whole stale set in ONE request — a cancel per
                    // order was a full round trip each (~119ms to clob) inside
                    // the decision->ack window. The batch still completes
                    // BEFORE any replacement is sent: the exit path emits
                    // Cancel(token) + Sell(token @ bid) in the same tick, so a
                    // sell released while our own resting BUY is still live
                    // would cross it (see the order-path commit for the full
                    // pricing of this ordering).
                    if !stale.is_empty() {
                        let refs: Vec<&str> = stale.iter().map(|s| s.as_str()).collect();
                        match self.client.cancel_orders(&refs).await {
                            Ok(report) => {
                                for id in &report.cancelled {
                                    self.order_manager.mark_cancelled(id);
                                    self.risk_manager.release_order(id);
                                }
                                // Refusals stay active locally on purpose — a
                                // locally-cancelled order still on the book is
                                // the ghost of docs/LESSONS.md#L6.
                                for (id, why) in &report.failed {
                                    tracing::warn!(order_id = %id, reason = %why, "Stale order refused cancellation");
                                }
                            }
                            Err(e) => {
                                tracing::warn!(error = %e, count = stale.len(), "Batch cancel of stale orders failed");
                            }
                        }
                    }

                    // Drop the original `signals` iteration and place remaining desired.
                    for d in desired {
                        let Desired { signal, decision_id, .. } = d;
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

                                let side = signal_side(s);
                                match self.order_manager.execute(s.clone()).await {
                                    Ok(Some(placed)) => {
                                        // Confirm the reservation as an open order
                                        self.risk_manager.confirm_reservation(&reservation_id, &placed.order_id);
                                        // Phase 7 tape line. Nothing is held here
                                        // — the write must stay outside every lock.
                                        crate::order_tape::record_placed(
                                            &decision_id,
                                            &token_id,
                                            side,
                                            placed.price,
                                            placed.size,
                                            &placed.order_id,
                                            placed.post_only,
                                            &placed.timings,
                                        );
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
                        break;
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

                    // Phase 7: close the decision->fill leg locally — a
                    // monotonic delta against the same anchor the ack used.
                    crate::order_tape::record_fill(&fill.order_id, fill.price, fill.size);

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

                // Control plane commands. Built inline against live
                // engine state so we never need to share the state via
                // Arc/RwLock — the select! is the synchronization point.
                Some(cmd) = cmd_rx.recv() => {
                    match cmd {
                        EngineCommand::GetStatus(reply) => {
                            let books = self.market_data.book_age_stats().await;
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
                                ws_connected: self.ws_health.is_connected(),
                                ws_degraded: self.ws_health.degraded(),
                                ws_tokens: self.ws_health.tokens(),
                                ws_events: self.ws_health.events(),
                                ws_last_event_age_ms: self.ws_health.last_event_age_ms(),
                                ws_down_for_ms: self.ws_health.down_for_ms(),
                                book_age_p50_ms: books.p50_ms,
                                book_age_p90_ms: books.p90_ms,
                                book_age_max_ms: books.max_ms,
                                books_from_ws: books.from_ws,
                                books_from_rest: books.from_rest,
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
                    break;
                }
            }
        }

        self.ws_feed.abort();
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
            Ok(Some(placed)) => {
                self.risk_manager.confirm_reservation(&reservation_id, &placed.order_id);
                crate::order_tape::record_placed(
                    &crate::order_tape::next_decision_id(),
                    &token_id,
                    signal_side(&sig),
                    placed.price,
                    placed.size,
                    &placed.order_id,
                    placed.post_only,
                    &placed.timings,
                );
                Ok(placed.order_id)
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
