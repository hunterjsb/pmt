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
    // NOTE: there is deliberately no max_open_orders here. One existed,
    // defaulted to 10, and was never read by check_signal or reserve_exposure
    // — an unenforced limit that read as a guarantee. Order count is bounded
    // indirectly by max_total_exposure; add a real one only with enforcement.
    /// Maximum order size (in USDC notional)
    pub max_order_size: Decimal,
}

impl Default for RiskLimits {
    fn default() -> Self {
        Self {
            max_position_size: Decimal::from(50),
            max_total_exposure: Decimal::from(50),
            max_loss: Decimal::from(25),
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

/// Pending exposure reservation (before order is placed).
#[derive(Debug, Clone)]
pub struct PendingReservation {
    pub token_id: String,
    pub notional: Decimal,
}

/// Risk manager enforces trading limits.
pub struct RiskManager {
    limits: RiskLimits,
    circuit_breaker_triggered: bool,
    /// Open orders tracked by order_id -> TrackedOrder
    open_orders: HashMap<String, TrackedOrder>,
    /// Pending exposure reservations (reserved before order placed, keyed by temp ID)
    pending_reservations: HashMap<String, PendingReservation>,
    /// Counter for generating unique reservation IDs
    reservation_counter: u64,
}

impl RiskManager {
    pub fn new(limits: RiskLimits) -> Self {
        Self {
            limits,
            circuit_breaker_triggered: false,
            open_orders: HashMap::new(),
            pending_reservations: HashMap::new(),
            reservation_counter: 0,
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

    /// Check P&L and trigger circuit breaker if needed.
    pub fn check_pnl(&mut self, positions: &PositionTracker) {
        // Trigger once, not per 50ms tick — an already-tripped breaker
        // re-announcing the same fact wrote ~20 log lines/s on 2026-08-23.
        if self.circuit_breaker_triggered {
            return;
        }
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
            Signal::Hold
            | Signal::Cancel { .. }
            | Signal::CancelOrder { .. }
            | Signal::Shutdown { .. }
            | Signal::StrategyComplete { .. }
            | Signal::Subscribe { .. }
            | Signal::Unsubscribe { .. }
            | Signal::Alert { .. } => RiskCheckResult::Approved(signal.clone()),

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

    /// Get total notional value of open orders (excluding pending reservations).
    pub fn open_order_notional(&self) -> Decimal {
        self.open_orders.values().map(|o| o.notional).sum()
    }

    /// Get total notional value of pending reservations.
    pub fn pending_reservation_notional(&self) -> Decimal {
        self.pending_reservations.values().map(|r| r.notional).sum()
    }

    /// Get total reserved exposure (open orders + pending reservations).
    pub fn total_reserved_notional(&self) -> Decimal {
        self.open_order_notional() + self.pending_reservation_notional()
    }

    /// Get total open order count.
    pub fn total_open_orders(&self) -> usize {
        self.open_orders.len()
    }

    /// Reserve exposure BEFORE placing an order.
    ///
    /// Returns a reservation ID if successful, None if exposure limit would be exceeded.
    /// This prevents race conditions where multiple signals pass risk checks before
    /// any orders are tracked.
    pub fn reserve_exposure(
        &mut self,
        token_id: &str,
        notional: Decimal,
        positions: &PositionTracker,
    ) -> Option<String> {
        // Calculate current exposure including pending reservations
        let position_notional = positions.total_notional();
        let reserved_notional = self.total_reserved_notional();
        let current_exposure = position_notional + reserved_notional;

        // Check if this reservation would exceed the limit
        if current_exposure + notional > self.limits.max_total_exposure {
            tracing::warn!(
                token_id = token_id,
                requested_notional = %notional,
                position_notional = %position_notional,
                reserved_notional = %reserved_notional,
                current_exposure = %current_exposure,
                limit = %self.limits.max_total_exposure,
                "Exposure reservation rejected: would exceed limit"
            );
            return None;
        }

        // Generate unique reservation ID
        self.reservation_counter += 1;
        let reservation_id = format!("res_{}", self.reservation_counter);

        tracing::debug!(
            reservation_id = reservation_id.as_str(),
            token_id = token_id,
            notional = %notional,
            new_total_exposure = %(current_exposure + notional),
            "Exposure reserved"
        );

        self.pending_reservations.insert(
            reservation_id.clone(),
            PendingReservation {
                token_id: token_id.to_string(),
                notional,
            },
        );

        Some(reservation_id)
    }

    /// Confirm a reservation after order is successfully placed.
    ///
    /// Converts the pending reservation into a tracked open order.
    pub fn confirm_reservation(&mut self, reservation_id: &str, order_id: &str) {
        if let Some(reservation) = self.pending_reservations.remove(reservation_id) {
            tracing::debug!(
                reservation_id = reservation_id,
                order_id = order_id,
                token_id = reservation.token_id.as_str(),
                notional = %reservation.notional,
                "Reservation confirmed as order"
            );

            self.open_orders.insert(
                order_id.to_string(),
                TrackedOrder {
                    token_id: reservation.token_id,
                    notional: reservation.notional,
                },
            );
        } else {
            tracing::warn!(
                reservation_id = reservation_id,
                order_id = order_id,
                "Attempted to confirm unknown reservation"
            );
        }
    }

    /// Release a reservation if order placement fails.
    pub fn release_reservation(&mut self, reservation_id: &str) {
        if let Some(reservation) = self.pending_reservations.remove(reservation_id) {
            tracing::debug!(
                reservation_id = reservation_id,
                token_id = reservation.token_id.as_str(),
                notional = %reservation.notional,
                "Reservation released (order failed)"
            );
        } else {
            tracing::warn!(
                reservation_id = reservation_id,
                "Attempted to release unknown reservation"
            );
        }
    }

    /// Release a tracked open order after it has been cancelled or filled.
    ///
    /// Without this the exposure tally grows monotonically with every order
    /// placement and the engine eventually rejects all new orders once
    /// `max_total_exposure` is reached.
    pub fn release_order(&mut self, order_id: &str) {
        if let Some(order) = self.open_orders.remove(order_id) {
            tracing::debug!(
                order_id = order_id,
                token_id = order.token_id.as_str(),
                notional = %order.notional,
                "Open order released (cancelled or filled)"
            );
        }
    }

    /// Release all tracked open orders for a given token. Returns the count
    /// of orders released. Called when a Cancel signal has cancelled every
    /// order on a token.
    pub fn release_orders_for_token(&mut self, token_id: &str) -> usize {
        let to_remove: Vec<String> = self
            .open_orders
            .iter()
            .filter(|(_, o)| o.token_id == token_id)
            .map(|(id, _)| id.clone())
            .collect();
        let count = to_remove.len();
        for id in to_remove {
            self.open_orders.remove(&id);
        }
        if count > 0 {
            tracing::debug!(
                token_id = token_id,
                count = count,
                "Released open-order tracking for token"
            );
        }
        count
    }

    /// Get current exposure (positions + open orders + pending reservations).
    pub fn current_exposure(&self, positions: &PositionTracker) -> Decimal {
        positions.total_notional() + self.total_reserved_notional()
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
            pending_reservations: self.pending_reservations.clone(),
            reservation_counter: self.reservation_counter,
        }
    }
}
