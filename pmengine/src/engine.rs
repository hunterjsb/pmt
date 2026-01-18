//! Main event loop for the trading engine.

use crate::config::Config;
use crate::order::OrderManager;
use crate::position::{Fill, PositionTracker};
use crate::risk::{RiskCheckResult, RiskLimits, RiskManager};
use crate::strategy::{DummyStrategy, OrderBookSnapshot, Signal, StrategyContext, StrategyRuntime};

use rust_decimal::Decimal;
use std::collections::HashMap;
use std::time::Duration;
use tokio::sync::mpsc;
use tokio::time::{interval, Instant};

/// The main trading engine.
pub struct Engine {
    config: Config,
    strategy_runtime: StrategyRuntime,
    order_manager: OrderManager,
    risk_manager: RiskManager,
    positions: PositionTracker,
    order_books: HashMap<String, OrderBookSnapshot>,
    fill_receiver: mpsc::Receiver<Fill>,
    dry_run: bool,
    shutdown: bool,
}

impl Engine {
    /// Create a new engine instance.
    pub async fn new(config: Config, dry_run: bool) -> Result<Self, EngineError> {
        // Create fill channel
        let (fill_sender, fill_receiver) = mpsc::channel(1000);

        // Create order manager
        let order_manager = OrderManager::new(fill_sender, dry_run);

        // Create risk manager with limits from config
        let risk_limits = RiskLimits {
            max_position_size: Decimal::from_f64_retain(config.max_position_size)
                .unwrap_or(Decimal::from(1000)),
            max_total_exposure: Decimal::from_f64_retain(config.max_total_exposure)
                .unwrap_or(Decimal::from(5000)),
            ..Default::default()
        };
        let risk_manager = RiskManager::new(risk_limits);

        // Create strategy runtime (empty, strategies added via register)
        let strategy_runtime = StrategyRuntime::new();

        Ok(Self {
            config,
            strategy_runtime,
            order_manager,
            risk_manager,
            positions: PositionTracker::new(),
            order_books: HashMap::new(),
            fill_receiver,
            dry_run,
            shutdown: false,
        })
    }

    /// Register a strategy.
    pub fn register_strategy(&mut self, strategy: Box<dyn crate::strategy::Strategy>) {
        // Initialize order book snapshots for subscriptions
        for token_id in strategy.subscriptions() {
            self.order_books
                .entry(token_id.clone())
                .or_insert_with(|| OrderBookSnapshot::new(token_id));
        }
        self.strategy_runtime.register(strategy);
    }

    /// Register a dummy strategy for testing.
    pub fn register_dummy_strategy(&mut self, tokens: Vec<String>) {
        self.register_strategy(Box::new(DummyStrategy::new("dummy", tokens)));
    }

    /// Run the main event loop.
    pub async fn run(&mut self) -> Result<(), EngineError> {
        tracing::info!("Starting engine event loop");

        // Get tick interval
        let tick_duration = Duration::from_millis(self.config.tick_interval_ms);
        let mut tick_timer = interval(tick_duration);

        // TODO: Connect to WebSocket for market data
        // let ws_stream = connect_ws(&self.config.ws_url).await?;

        // Set up ctrl-c handler
        let (shutdown_tx, mut shutdown_rx) = mpsc::channel::<()>(1);
        tokio::spawn(async move {
            tokio::signal::ctrl_c().await.ok();
            tracing::info!("Received shutdown signal");
            shutdown_tx.send(()).await.ok();
        });

        let mut last_tick = Instant::now();
        let mut tick_count: u64 = 0;

        loop {
            tokio::select! {
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

                    // Build strategy context
                    let ctx = StrategyContext {
                        timestamp: chrono::Utc::now(),
                        order_books: self.order_books.clone(),
                        positions: self.positions.clone(),
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
                            RiskCheckResult::Approved(s) => {
                                if let Err(e) = self.order_manager.execute(s).await {
                                    tracing::error!(error = %e, "Order execution failed");
                                }
                            }
                            RiskCheckResult::Reduced(s, reason) => {
                                tracing::warn!(reason = reason, "Signal reduced by risk manager");
                                if let Err(e) = self.order_manager.execute(s).await {
                                    tracing::error!(error = %e, "Order execution failed");
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

                    // Update risk manager
                    self.risk_manager.order_closed(&fill.token_id);
                }

                // TODO: WebSocket market data
                // Some(msg) = ws_stream.next() => {
                //     self.handle_ws_message(msg)?;
                // }

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

    /// Update order book from market data.
    pub fn update_order_book(
        &mut self,
        token_id: &str,
        best_bid: Option<Decimal>,
        best_ask: Option<Decimal>,
        bid_size: Decimal,
        ask_size: Decimal,
    ) {
        if let Some(book) = self.order_books.get_mut(token_id) {
            book.update(best_bid, best_ask, bid_size, ask_size);
        }

        // Update position prices for P&L
        if let Some(mid) = self.order_books.get(token_id).and_then(|b| b.mid_price) {
            let mut prices = HashMap::new();
            prices.insert(token_id.to_string(), mid);
            self.positions.update_prices(&prices);
        }
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
