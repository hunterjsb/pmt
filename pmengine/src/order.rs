//! Order management wrapping the Polymarket SDK.

use crate::client::{PolymarketClient, Side};
use crate::position::Fill;
use crate::strategy::{Signal, Urgency};
use rust_decimal::Decimal;
use std::collections::HashMap;
use std::sync::Arc;
use tokio::sync::mpsc;

/// Order state.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum OrderStatus {
    Pending,
    Open,
    PartiallyFilled,
    Filled,
    Cancelled,
    Rejected,
}

/// Tracked order.
#[derive(Debug, Clone)]
pub struct Order {
    pub id: String,
    pub token_id: String,
    pub is_buy: bool,
    pub price: Decimal,
    pub size: Decimal,
    pub filled_size: Decimal,
    pub status: OrderStatus,
    pub created_at: chrono::DateTime<chrono::Utc>,
}

impl Order {
    pub fn remaining(&self) -> Decimal {
        self.size - self.filled_size
    }

    pub fn is_active(&self) -> bool {
        matches!(self.status, OrderStatus::Pending | OrderStatus::Open | OrderStatus::PartiallyFilled)
    }
}

/// Order manager wraps the SDK and tracks orders.
pub struct OrderManager {
    client: Arc<PolymarketClient>,
    orders: HashMap<String, Order>,
    fill_sender: mpsc::Sender<Fill>,
}

impl OrderManager {
    pub fn new(client: Arc<PolymarketClient>, fill_sender: mpsc::Sender<Fill>) -> Self {
        Self {
            client,
            orders: HashMap::new(),
            fill_sender,
        }
    }

    /// Check if running in dry-run mode.
    pub fn is_dry_run(&self) -> bool {
        self.client.is_dry_run()
    }

    /// Execute a signal by placing/canceling orders.
    pub async fn execute(&mut self, signal: Signal) -> Result<Option<String>, OrderError> {
        match signal {
            Signal::Hold => Ok(None),

            Signal::Cancel { token_id } => {
                self.cancel_all(&token_id).await?;
                Ok(None)
            }

            Signal::Buy { token_id, price, size, urgency } => {
                self.place_order(&token_id, true, price, size, urgency).await
            }

            Signal::Sell { token_id, price, size, urgency } => {
                self.place_order(&token_id, false, price, size, urgency).await
            }

            // These are engine-level signals; the engine consumes them
            // before the order pipeline ever sees them. Listed here only
            // for exhaustiveness.
            Signal::Shutdown { .. }
            | Signal::StrategyComplete { .. }
            | Signal::Subscribe { .. }
            | Signal::Unsubscribe { .. }
            | Signal::Alert { .. }
            | Signal::CancelOrder { .. } => Ok(None),
        }
    }

    async fn place_order(
        &mut self,
        token_id: &str,
        is_buy: bool,
        price: Decimal,
        size: Decimal,
        _urgency: Urgency,
    ) -> Result<Option<String>, OrderError> {
        // Round price to the market's actual tick size (Polymarket rejects
        // orders that don't sit on a tick). Default to 2 dp if the tick
        // lookup fails — better to attempt at coarser precision than to
        // drop the order entirely.
        let dp = self
            .client
            .tick_decimals_for(token_id)
            .await
            .unwrap_or(2);
        let price = price.round_dp(dp);
        let size = size.round_dp(2);

        // Skip if size rounds to zero
        if size.is_zero() {
            tracing::debug!(token_id = token_id, "Order size rounded to zero, skipping");
            return Ok(None);
        }

        let side = if is_buy { Side::Buy } else { Side::Sell };

        // Place order via SDK (handles dry-run internally)
        let order_id = self
            .client
            .place_limit_order(token_id, side, price, size)
            .await
            .map_err(|e| OrderError::SdkError(e.to_string()))?;

        // Track order locally
        let order = Order {
            id: order_id.clone(),
            token_id: token_id.to_string(),
            is_buy,
            price,
            size,
            filled_size: Decimal::ZERO,
            status: OrderStatus::Open,
            created_at: chrono::Utc::now(),
        };

        self.orders.insert(order_id.clone(), order);
        Ok(Some(order_id))
    }

    /// Cancel all orders for a token.
    pub async fn cancel_all(&mut self, token_id: &str) -> Result<usize, OrderError> {
        let to_cancel: Vec<String> = self
            .orders
            .iter()
            .filter(|(_, o)| o.token_id == token_id && o.is_active())
            .map(|(id, _)| id.clone())
            .collect();

        // Keep going past individual failures — `?` here abandoned every
        // order AFTER the first failed cancel, silently leaving live quotes
        // on the book while pause/stop reported success (2026-08-23 sweep).
        let count = to_cancel.len();
        let mut failed = 0usize;
        let mut last_err: Option<OrderError> = None;
        for order_id in to_cancel {
            if let Err(e) = self.cancel_order(&order_id).await {
                failed += 1;
                tracing::warn!(order_id = %order_id, error = %e, "Cancel failed — continuing");
                last_err = Some(e);
            }
        }
        if let Some(e) = last_err {
            tracing::warn!(token_id, failed, count, "cancel_all left orders live");
            return Err(e);
        }
        tracing::info!(token_id = token_id, count = count, "Cancelled orders");
        Ok(count)
    }

    /// Cancel a specific order.
    pub async fn cancel_order(&mut self, order_id: &str) -> Result<(), OrderError> {
        if let Some(order) = self.orders.get_mut(order_id) {
            if order.is_active() {
                // Cancel via SDK (handles dry-run internally)
                self.client
                    .cancel_order(order_id)
                    .await
                    .map_err(|e| OrderError::SdkError(e.to_string()))?;

                order.status = OrderStatus::Cancelled;
            }
        }
        Ok(())
    }

    /// Cancel all active orders (for shutdown).
    pub async fn cancel_all_orders(&mut self) -> Result<usize, OrderError> {
        let active: Vec<String> = self
            .orders
            .iter()
            .filter(|(_, o)| o.is_active())
            .map(|(id, _)| id.clone())
            .collect();

        let count = active.len();
        if count > 0 {
            // Batch cancel via SDK
            let order_refs: Vec<&str> = active.iter().map(|s| s.as_str()).collect();
            self.client
                .cancel_orders(&order_refs)
                .await
                .map_err(|e| OrderError::SdkError(e.to_string()))?;

            // Update local state
            for order_id in &active {
                if let Some(order) = self.orders.get_mut(order_id) {
                    order.status = OrderStatus::Cancelled;
                }
            }
        }

        tracing::info!(count = count, "Cancelled all orders on shutdown");
        Ok(count)
    }

    /// Process a fill event from the user-trades stream.
    ///
    /// `fee` is the realized trading fee in USDC (not basis points). Compute
    /// it from the Polymarket TradeMessage as
    /// `price * size * fee_rate_bps / 10_000` before calling — the engine
    /// doesn't recompute it here, so callers must pass the actual fee.
    pub async fn process_fill(
        &mut self,
        order_id: &str,
        price: Decimal,
        size: Decimal,
        fee: Decimal,
    ) -> Result<(), OrderError> {
        if let Some(order) = self.orders.get_mut(order_id) {
            order.filled_size += size;
            if order.filled_size >= order.size {
                order.status = OrderStatus::Filled;
            } else {
                order.status = OrderStatus::PartiallyFilled;
            }

            let fill = Fill {
                order_id: order_id.to_string(),
                token_id: order.token_id.clone(),
                is_buy: order.is_buy,
                price,
                size,
                timestamp: chrono::Utc::now(),
                fee,
            };

            tracing::info!(
                order_id = order_id,
                token_id = fill.token_id,
                side = if fill.is_buy { "BUY" } else { "SELL" },
                price = %fill.price,
                size = %fill.size,
                "Order filled"
            );

            self.fill_sender.send(fill).await.map_err(|_| OrderError::ChannelClosed)?;
        }
        Ok(())
    }

    /// Get an order by ID.
    pub fn get_order(&self, order_id: &str) -> Option<&Order> {
        self.orders.get(order_id)
    }

    /// Owned snapshot of currently-active orders, for introspection by the
    /// control plane. Cloned so the caller can serialize without holding a
    /// reference into the manager.
    pub fn active_orders_snapshot(&self) -> Vec<Order> {
        self.orders
            .values()
            .filter(|o| o.is_active())
            .cloned()
            .collect()
    }

    pub fn active_orders(&self) -> Vec<&Order> {
        self.orders.values().filter(|o| o.is_active()).collect()
    }

    /// Get active orders for a token.
    pub fn active_orders_for_token(&self, token_id: &str) -> Vec<&Order> {
        self.orders
            .values()
            .filter(|o| o.token_id == token_id && o.is_active())
            .collect()
    }
}

#[derive(Debug)]
pub enum OrderError {
    SdkError(String),
    ChannelClosed,
    InvalidOrder(String),
}

impl std::fmt::Display for OrderError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            OrderError::SdkError(e) => write!(f, "SDK error: {}", e),
            OrderError::ChannelClosed => write!(f, "Fill channel closed"),
            OrderError::InvalidOrder(e) => write!(f, "Invalid order: {}", e),
        }
    }
}

impl std::error::Error for OrderError {}
