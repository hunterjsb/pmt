//! Risk management and circuit breaker.

use crate::position::PositionTracker;
use crate::strategy::Signal;
use rust_decimal::Decimal;
use std::collections::HashMap;

/// Tracked open order for exposure calculation.
#[derive(Debug, Clone)]
pub struct TrackedOrder {
    pub token_id: String,
    pub notional: Decimal,
}

/// Risk limits configuration.
#[derive(Debug, Clone)]
pub struct RiskLimits {
    /// Maximum position size per token (in USDC notional)
    pub max_position_size: Decimal,
    /// Maximum total exposure across all positions AND open orders (in USDC)
    pub max_total_exposure: Decimal,
    /// Maximum loss before circuit breaker triggers (in USDC)
    pub max_loss: Decimal,
    /// Maximum number of open orders
    pub max_open_orders: usize,
    /// Maximum order size (in USDC notional)
    pub max_order_size: Decimal,
}

impl Default for RiskLimits {
    fn default() -> Self {
        Self {
            max_position_size: Decimal::from(50),
            max_total_exposure: Decimal::from(50),
            max_loss: Decimal::from(25),
            max_open_orders: 10,
            max_order_size: Decimal::from(25),
        }
    }
}

/// Result of risk check on a signal.
#[derive(Debug)]
pub enum RiskCheckResult {
    /// Signal approved as-is
    Approved(Signal),
    /// Signal approved with reduced size
    Reduced(Signal, String),
    /// Signal rejected
    Rejected(String),
}

/// Risk manager enforces trading limits.
pub struct RiskManager {
    limits: RiskLimits,
    circuit_breaker_triggered: bool,
    /// Open orders tracked by order_id -> TrackedOrder
    open_orders: HashMap<String, TrackedOrder>,
}

impl RiskManager {
    pub fn new(limits: RiskLimits) -> Self {
        Self {
            limits,
            circuit_breaker_triggered: false,
            open_orders: HashMap::new(),
        }
    }

    /// Check if circuit breaker is active.
    pub fn is_halted(&self) -> bool {
        self.circuit_breaker_triggered
    }

    /// Trigger circuit breaker (halt all trading).
    pub fn trigger_circuit_breaker(&mut self, reason: &str) {
        tracing::error!(reason, "CIRCUIT BREAKER TRIGGERED");
        self.circuit_breaker_triggered = true;
    }

    /// Reset circuit breaker (manual intervention).
    pub fn reset_circuit_breaker(&mut self) {
        tracing::warn!("Circuit breaker reset");
        self.circuit_breaker_triggered = false;
    }

    /// Check P&L and trigger circuit breaker if needed.
    pub fn check_pnl(&mut self, positions: &PositionTracker) {
        let total_pnl = positions.total_realized_pnl() + positions.total_unrealized_pnl();
        if total_pnl < -self.limits.max_loss {
            self.trigger_circuit_breaker(&format!(
                "Max loss exceeded: {} < -{}",
                total_pnl, self.limits.max_loss
            ));
        }
    }

    /// Check a signal against risk limits.
    pub fn check_signal(&self, signal: &Signal, positions: &PositionTracker) -> RiskCheckResult {
        // Circuit breaker check
        if self.circuit_breaker_triggered {
            return RiskCheckResult::Rejected("Circuit breaker active".to_string());
        }

        match signal {
            Signal::Hold | Signal::Cancel { .. } => RiskCheckResult::Approved(signal.clone()),

            Signal::Buy { token_id, price, size, urgency } => {
                self.check_order(token_id, *price, *size, true, *urgency, positions)
            }

            Signal::Sell { token_id, price, size, urgency } => {
                self.check_order(token_id, *price, *size, false, *urgency, positions)
            }
        }
    }

    fn check_order(
        &self,
        token_id: &str,
        price: Decimal,
        size: Decimal,
        is_buy: bool,
        urgency: crate::strategy::Urgency,
        positions: &PositionTracker,
    ) -> RiskCheckResult {
        let notional = price * size;

        // Check order size limit
        if notional > self.limits.max_order_size {
            let max_size = self.limits.max_order_size / price;
            return RiskCheckResult::Reduced(
                if is_buy {
                    Signal::Buy {
                        token_id: token_id.to_string(),
                        price,
                        size: max_size,
                        urgency,
                    }
                } else {
                    Signal::Sell {
                        token_id: token_id.to_string(),
                        price,
                        size: max_size,
                        urgency,
                    }
                },
                format!("Order size reduced from {} to {} (max order size)", size, max_size),
            );
        }

        // Check position limit for this token
        if let Some(pos) = positions.get(token_id) {
            let projected_size = if is_buy {
                pos.size + size
            } else {
                pos.size - size
            };
            let projected_notional = projected_size.abs() * price;

            if projected_notional > self.limits.max_position_size {
                let allowed_change = self.limits.max_position_size / price - pos.size.abs();
                if allowed_change <= Decimal::ZERO {
                    return RiskCheckResult::Rejected(format!(
                        "Position limit reached for {}",
                        token_id
                    ));
                }
                return RiskCheckResult::Reduced(
                    if is_buy {
                        Signal::Buy {
                            token_id: token_id.to_string(),
                            price,
                            size: allowed_change,
                            urgency,
                        }
                    } else {
                        Signal::Sell {
                            token_id: token_id.to_string(),
                            price,
                            size: allowed_change,
                            urgency,
                        }
                    },
                    format!("Order size reduced to {} (position limit)", allowed_change),
                );
            }
        }

        // Check total exposure limit (positions + open orders + this new order)
        let position_notional = positions.total_notional();
        let open_order_notional = self.open_order_notional();
        let current_exposure = position_notional + open_order_notional;

        if current_exposure + notional > self.limits.max_total_exposure {
            let allowed = self.limits.max_total_exposure - current_exposure;
            if allowed <= Decimal::ZERO {
                return RiskCheckResult::Rejected(format!(
                    "Total exposure limit reached (positions: {}, open orders: {}, limit: {})",
                    position_notional, open_order_notional, self.limits.max_total_exposure
                ));
            }
            let allowed_size = allowed / price;
            return RiskCheckResult::Reduced(
                if is_buy {
                    Signal::Buy {
                        token_id: token_id.to_string(),
                        price,
                        size: allowed_size,
                        urgency,
                    }
                } else {
                    Signal::Sell {
                        token_id: token_id.to_string(),
                        price,
                        size: allowed_size,
                        urgency,
                    }
                },
                format!(
                    "Order size reduced to {} (total exposure: {} + {} = {}, limit: {})",
                    allowed_size, position_notional, open_order_notional, current_exposure, self.limits.max_total_exposure
                ),
            );
        }

        // All checks passed, return approved signal
        RiskCheckResult::Approved(if is_buy {
            Signal::Buy {
                token_id: token_id.to_string(),
                price,
                size,
                urgency,
            }
        } else {
            Signal::Sell {
                token_id: token_id.to_string(),
                price,
                size,
                urgency,
            }
        })
    }

    /// Track an open order with its notional value.
    pub fn order_placed(&mut self, order_id: &str, token_id: &str, notional: Decimal) {
        tracing::debug!(
            order_id = order_id,
            token_id = token_id,
            notional = %notional,
            "Tracking order"
        );
        self.open_orders.insert(
            order_id.to_string(),
            TrackedOrder {
                token_id: token_id.to_string(),
                notional,
            },
        );
    }

    /// Remove order tracking on fill/cancel.
    pub fn order_closed(&mut self, order_id: &str) {
        if let Some(order) = self.open_orders.remove(order_id) {
            tracing::debug!(
                order_id = order_id,
                token_id = order.token_id,
                notional = %order.notional,
                "Untracking order"
            );
        }
    }

    /// Get total notional value of open orders.
    pub fn open_order_notional(&self) -> Decimal {
        self.open_orders.values().map(|o| o.notional).sum()
    }

    /// Get total open order count.
    pub fn total_open_orders(&self) -> usize {
        self.open_orders.len()
    }

    /// Get current exposure (positions + open orders).
    pub fn current_exposure(&self, positions: &PositionTracker) -> Decimal {
        positions.total_notional() + self.open_order_notional()
    }

    /// Get remaining capacity before hitting exposure limit.
    pub fn remaining_capacity(&self, positions: &PositionTracker) -> Decimal {
        let exposure = self.current_exposure(positions);
        (self.limits.max_total_exposure - exposure).max(Decimal::ZERO)
    }
}

impl Clone for RiskManager {
    fn clone(&self) -> Self {
        Self {
            limits: self.limits.clone(),
            circuit_breaker_triggered: self.circuit_breaker_triggered,
            open_orders: self.open_orders.clone(),
        }
    }
}
