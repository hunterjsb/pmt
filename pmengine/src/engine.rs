//! Main event loop for the trading engine.

use crate::client::PolymarketClient;
use crate::config::Config;
use crate::gamma::{GammaClient, GammaMarket};
use crate::order::OrderManager;
use crate::orderbook::MarketDataHub;
use crate::position::{Fill, PositionTracker};
use crate::risk::{RiskCheckResult, RiskLimits, RiskManager};
use crate::strategy::{DummyStrategy, MarketInfo, Signal, StrategyContext, StrategyRuntime};

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
}

impl Engine {
    /// Create a new engine instance.
    pub async fn new(config: Config, dry_run: bool) -> Result<Self, EngineError> {
        // Create and authenticate client
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

    /// Check if market discovery is enabled.
    pub fn is_market_discovery_enabled(&self) -> bool {
        self.market_discovery_enabled
    }

    /// Build market info map from Gamma markets.
    fn build_market_info(&self, markets: &[GammaMarket]) -> HashMap<String, MarketInfo> {
        let mut info_map = HashMap::new();

        for market in markets {
            // Each market has multiple tokens (one per outcome)
            for (i, token_id) in market.clob_token_ids.iter().enumerate() {
                let outcome = market.outcomes.get(i).cloned().unwrap_or_default();
                let info = MarketInfo::new(
                    market.question.clone(),
                    outcome,
                    market.end_date,
                );
                info_map.insert(token_id.clone(), info);
            }
        }

        info_map
    }

    /// Refresh markets from Gamma API.
    ///
    /// This fetches high-certainty expiring markets and subscribes to new tokens.
    async fn refresh_markets(&mut self) -> Result<(), EngineError> {
        let gamma = match &self.gamma_client {
            Some(c) => c,
            None => return Ok(()),
        };

        // Fetch sure bet candidates: expiring within 2 hours, 95%+ certainty
        let markets = gamma
            .fetch_sure_bet_candidates(2.0, rust_decimal_macros::dec!(0.95))
            .await
            .map_err(|e| EngineError::SdkError(format!("Gamma API error: {}", e)))?;

        tracing::info!(
            count = markets.len(),
            "Discovered high-certainty expiring markets"
        );

        // Log discovered opportunities
        for market in &markets {
            if let Some(hours) = market.hours_until_expiry() {
                if let Some(idx) = market.highest_certainty_index() {
                    let price = market.outcome_prices.get(idx).copied().unwrap_or_default();
                    let outcome = market.outcomes.get(idx).cloned().unwrap_or_default();
                    tracing::info!(
                        question = market.question.as_str(),
                        outcome = outcome.as_str(),
                        price = %price,
                        hours_left = format!("{:.2}", hours).as_str(),
                        "Sure bet opportunity"
                    );
                }
            }
        }

        // Subscribe to new tokens
        for market in &markets {
            for token_id in &market.clob_token_ids {
                if !self.subscribed_tokens.contains(token_id) {
                    self.market_data.init_book(token_id).await;
                    self.subscribed_tokens.push(token_id.clone());
                    tracing::debug!(token_id = token_id.as_str(), "Subscribed to new token");
                }
            }
        }

        // Update market info
        self.market_info = self.build_market_info(&markets);

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

    /// Get a market data subscriber for external consumers.
    pub fn subscribe_market_data(&self) -> async_broadcast::Receiver<crate::orderbook::MarketEvent> {
        self.market_data.subscribe()
    }

    /// Get the market data hub for direct access.
    pub fn market_data(&self) -> Arc<MarketDataHub> {
        self.market_data.clone()
    }

    /// Run the main event loop.
    pub async fn run(&mut self) -> Result<(), EngineError> {
        tracing::info!("Starting engine event loop");

        // Get tick interval
        let tick_duration = Duration::from_millis(self.config.tick_interval_ms);
        let mut tick_timer = interval(tick_duration);

        // Token IDs for WebSocket subscriptions
        let subscribed_tokens = self.subscribed_tokens.clone();

        // Connect to WebSocket for market data if we have subscriptions
        // Keep ws_client alive since the stream borrows from it
        let ws_client = WsClient::default();
        let mut ws_stream: Option<Pin<Box<dyn futures::Stream<Item = Result<_, _>> + Send>>> =
            if !subscribed_tokens.is_empty() {
                let asset_ids: Result<Vec<U256>, _> = subscribed_tokens
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

        // Set up ctrl-c handler
        let (shutdown_tx, mut shutdown_rx) = mpsc::channel::<()>(1);
        tokio::spawn(async move {
            tokio::signal::ctrl_c().await.ok();
            tracing::info!("Received shutdown signal");
            shutdown_tx.send(()).await.ok();
        });

        let mut last_tick = Instant::now();
        let mut tick_count: u64 = 0;
        let mut ws_update_count: u64 = 0;

        // Market discovery timer (60 seconds)
        let mut market_refresh_timer = interval(Duration::from_secs(60));
        // Skip the first immediate tick
        market_refresh_timer.tick().await;

        // Do initial market discovery if enabled
        if self.market_discovery_enabled {
            if let Err(e) = self.refresh_markets().await {
                tracing::warn!(error = %e, "Initial market discovery failed");
            }
        }

        loop {
            tokio::select! {
                // Market discovery refresh (if enabled)
                _ = market_refresh_timer.tick(), if self.market_discovery_enabled => {
                    if let Err(e) = self.refresh_markets().await {
                        tracing::warn!(error = %e, "Market discovery refresh failed");
                    }
                }
                // Tick timer for strategy evaluation
                _ = tick_timer.tick() => {
                    tick_count += 1;
                    let elapsed = last_tick.elapsed();
                    last_tick = Instant::now();

                    tracing::debug!(tick = tick_count, elapsed_ms = elapsed.as_millis(), "Tick");

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
                    };

                    // Run strategies
                    let signals = self.strategy_runtime.tick(&ctx);

                    // Process signals through risk manager and execute
                    for signal in signals {
                        if matches!(signal, Signal::Hold) {
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

                                match self.order_manager.execute(s.clone()).await {
                                    Ok(Some(order_id)) => {
                                        // Track order notional in risk manager
                                        let notional = price * size;
                                        self.risk_manager.order_placed(&order_id, &token_id, notional);
                                    }
                                    Ok(None) => {}
                                    Err(e) => {
                                        tracing::error!(error = %e, "Order execution failed");
                                    }
                                }
                            }
                            RiskCheckResult::Rejected(reason) => {
                                tracing::warn!(reason = reason, "Signal rejected by risk manager");
                            }
                        }
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
                    break;
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
}

impl std::fmt::Display for EngineError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            EngineError::ConfigError(e) => write!(f, "Config error: {}", e),
            EngineError::SdkError(e) => write!(f, "SDK error: {}", e),
            EngineError::OrderError(e) => write!(f, "Order error: {}", e),
            EngineError::WebSocketError(e) => write!(f, "WebSocket error: {}", e),
        }
    }
}

impl std::error::Error for EngineError {}

impl From<crate::config::ConfigError> for EngineError {
    fn from(e: crate::config::ConfigError) -> Self {
        EngineError::ConfigError(e.to_string())
    }
}
