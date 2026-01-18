//! Risk management and circuit breaker.

use crate::position::PositionTracker;
use crate::strategy::Signal;
use rust_decimal::Decimal;
use std::collections::HashMap;

/// Risk limits configuration.
#[derive(Debug, Clone)]
pub struct RiskLimits {
    /// Maximum position size per token (in USDC notional)
    pub max_position_size: Decimal,
    /// Maximum total exposure across all positions (in USDC)
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
            max_position_size: Decimal::from(1000),
            max_total_exposure: Decimal::from(5000),
            max_loss: Decimal::from(500),
            max_open_orders: 50,
            max_order_size: Decimal::from(500),
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
    open_orders: HashMap<String, usize>, // token_id -> count
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

        // Check total exposure limit
        let current_exposure = positions.total_notional();
        if current_exposure + notional > self.limits.max_total_exposure {
            let allowed = self.limits.max_total_exposure - current_exposure;
            if allowed <= Decimal::ZERO {
                return RiskCheckResult::Rejected("Total exposure limit reached".to_string());
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
                format!("Order size reduced to {} (total exposure limit)", allowed_size),
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

    /// Update open order count.
    pub fn order_placed(&mut self, token_id: &str) {
        *self.open_orders.entry(token_id.to_string()).or_insert(0) += 1;
    }

    /// Update open order count on fill/cancel.
    pub fn order_closed(&mut self, token_id: &str) {
        if let Some(count) = self.open_orders.get_mut(token_id) {
            *count = count.saturating_sub(1);
        }
    }

    /// Get total open order count.
    pub fn total_open_orders(&self) -> usize {
        self.open_orders.values().sum()
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
