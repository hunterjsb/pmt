//! Main event loop for the trading engine.

use crate::client::PolymarketClient;
use crate::config::Config;
use crate::gamma::{GammaClient, GammaMarket};
use crate::order::OrderManager;
use crate::orderbook::MarketDataHub;
use crate::position::{Fill, PositionTracker};
use crate::risk::{RiskCheckResult, RiskLimits, RiskManager};
use crate::strategy::{DummyStrategy, MarketInfo, Signal, StrategyContext, StrategyRuntime};

#[cfg(feature = "cognito")]
use crate::cognito::create_cognito_auth;

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
        // Create and authenticate client (with Cognito auth if using proxy)
        #[cfg(feature = "cognito")]
        let client = {
            let cognito_auth = if std::env::var("PMPROXY_URL").is_ok() {
                tracing::info!("Proxy detected, initializing Cognito auth...");
                create_cognito_auth().await
            } else {
                None
            };
            Arc::new(
                PolymarketClient::new_with_cognito(&config, dry_run, cognito_auth)
                    .await
                    .map_err(|e| EngineError::SdkError(e.to_string()))?,
            )
        };

        #[cfg(not(feature = "cognito"))]
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

    /// Check if market discovery is enabled.
    pub fn is_market_discovery_enabled(&self) -> bool {
        self.market_discovery_enabled
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

        for market in event_markets.into_iter().chain(recurring_markets.into_iter()) {
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

    /// Register a strategy.
    pub async fn register_strategy(&mut self, strategy: Box<dyn crate::strategy::Strategy>) {
        // Initialize order books for subscriptions
        for token_id in strategy.subscriptions() {
            if !self.subscribed_tokens.contains(&token_id) {
                self.market_data.init_book(&token_id).await;
                self.subscribed_tokens.push(token_id);
            }
        }
        self.strategy_runtime.register(strategy);
    }

    /// Register a dummy strategy for testing.
    pub async fn register_dummy_strategy(&mut self, tokens: Vec<String>) {
        self.register_strategy(Box::new(DummyStrategy::new("dummy", tokens))).await;
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

            // Initialize order books for subscriptions
            for token_id in strategy.subscriptions() {
                if !self.subscribed_tokens.contains(&token_id) {
                    // Use blocking approach for sync context
                    futures::executor::block_on(self.market_data.init_book(&token_id));
                    self.subscribed_tokens.push(token_id);
                }
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

    /// Get a market data subscriber for external consumers.
    pub fn subscribe_market_data(&self) -> async_broadcast::Receiver<crate::orderbook::MarketEvent> {
        self.market_data.subscribe()
    }

    /// Get the market data hub for direct access.
    pub fn market_data(&self) -> Arc<MarketDataHub> {
        self.market_data.clone()
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

        // Get tick interval
        let tick_duration = Duration::from_millis(self.config.tick_interval_ms);
        let mut tick_timer = interval(tick_duration);

        // Set up ctrl-c handler
        let (shutdown_tx, mut shutdown_rx) = mpsc::channel::<()>(1);
        tokio::spawn(async move {
            tokio::signal::ctrl_c().await.ok();
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

            // Warmup: wait for order books to sync before trading
            // Require at least 100 WebSocket updates before allowing trades
            const WARMUP_WS_UPDATES: u64 = 100;
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
                            match &signal {
                                Signal::Hold => continue,
                                Signal::Shutdown { reason } => {
                                    tracing::info!(reason = reason.as_str(), "Strategy requested shutdown");
                                    shutdown_requested = true;
                                }
                                Signal::Cancel { token_id } => {
                                    cancel_tokens.push(token_id.clone());
                                }
                                Signal::Buy { token_id, price, size, .. }
                                | Signal::Sell { token_id, price, size, .. } => {
                                    // Don't pre-round here. OrderManager rounds to the
                                    // per-market tick when actually placing; the delta-quote
                                    // matcher below uses a half-tick tolerance so any
                                    // sub-tick wobble in the strategy's target still
                                    // matches an existing aged order.
                                    let size = size.round_dp(2);
                                    desired.push((signal.clone(), token_id.clone(), *price, size));
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
        tracing::debug!("Background pollers stopped");

        Ok(())
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
    WebSocketError(String),
    UnknownStrategy(String),
}

impl std::fmt::Display for EngineError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            EngineError::ConfigError(e) => write!(f, "Config error: {}", e),
            EngineError::SdkError(e) => write!(f, "SDK error: {}", e),
            EngineError::OrderError(e) => write!(f, "Order error: {}", e),
            EngineError::WebSocketError(e) => write!(f, "WebSocket error: {}", e),
            EngineError::UnknownStrategy(name) => write!(f, "Unknown strategy: {}", name),
        }
    }
}

impl std::error::Error for EngineError {}

impl From<crate::config::ConfigError> for EngineError {
    fn from(e: crate::config::ConfigError) -> Self {
        EngineError::ConfigError(e.to_string())
    }
}
