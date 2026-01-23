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
use polymarket_client_sdk::clob::ws::Client as WsClient;
use polymarket_client_sdk::types::U256;
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

    /// Run the main event loop.
    ///
    /// # Arguments
    /// * `max_ticks` - Maximum number of ticks before automatic shutdown (0 = unlimited)
    pub async fn run(&mut self, max_ticks: u64) -> Result<(), EngineError> {
        tracing::info!(max_ticks = max_ticks, "Starting engine event loop");

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

                        // Skip trading during warmup period (unless skip_warmup is set)
                        if !warmup_complete {
                            if self.skip_warmup {
                                warmup_complete = true;
                                tracing::info!("Warmup skipped (--skip-warmup flag)");
                            } else if ws_update_count >= WARMUP_WS_UPDATES {
                                warmup_complete = true;
                                tracing::info!(
                                    ws_updates = ws_update_count,
                                    "Warmup complete, trading enabled"
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
                            // TODO: Fetch actual USDC balance from CTF contract via RPC
                            usdc_balance: Decimal::ZERO,
                        };

                        // Run strategies
                        let signals = self.strategy_runtime.tick(&ctx);

                        // Process signals through risk manager and execute
                        let mut shutdown_requested = false;
                        for signal in signals {
                            if matches!(signal, Signal::Hold) {
                                continue;
                            }

                            // Handle shutdown signal from strategies
                            if let Signal::Shutdown { reason } = &signal {
                                tracing::info!(reason = reason.as_str(), "Strategy requested shutdown");
                                shutdown_requested = true;
                                continue;
                            }

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
