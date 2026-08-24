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
    ///
    /// Reads `breaker_pnl`, NOT `total_realized + total_unrealized`: positions
    /// whose mark has gone stale (a resolving binary whose book went one-sided
    /// or dark) are excluded, because a frozen mid is not a loss and not a
    /// price we could trade out at. Everything the engine can currently price
    /// counts in full and immediately — the threshold and the sensitivity to
    /// real losses are unchanged.
    pub fn check_pnl(&mut self, positions: &PositionTracker) {
        // Trigger once, not per 50ms tick — an already-tripped breaker
        // re-announcing the same fact wrote ~20 log lines/s on 2026-08-23.
        if self.circuit_breaker_triggered {
            return;
        }
        let total_pnl = positions.breaker_pnl();
        if total_pnl < -self.limits.max_loss {
            let unpriceable = positions.stale_marked_tokens().len();
            tracing::error!(
                marked_pnl = %total_pnl,
                max_loss = %self.limits.max_loss,
                unpriceable_positions = unpriceable,
                "Circuit breaker: marked loss over the limit"
            );
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


    /// Remove order tracking on cancel, or on an order that is fully done.
    ///
    /// Callers holding a PARTIAL fill must use `order_filled` instead — this
    /// releases the whole reservation, and a partial that releases the whole
    /// reservation hands back exposure that is still standing on the book.
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

    /// Release the filled FRACTION of an order's reserved exposure.
    ///
    /// A fill moves notional out of "reserved for an order on the book" and
    /// into "a position the tracker holds" — so the reservation must shrink
    /// by exactly what filled, and by nothing else. Releasing it whole on
    /// the first partial (which is what happened before this existed) says
    /// the book is clear while 25 of 30 shares are still working: the
    /// remainder is then exposure nothing accounts for, and `max_total_
    /// exposure` waves through a signal that should have been refused.
    ///
    /// The entry is untracked only once nothing is left of it, so a cancel
    /// arriving later still finds an order to close and a fully-filled one
    /// leaves no residue behind.
    pub fn order_filled(&mut self, order_id: &str, filled_notional: Decimal) {
        let Some(order) = self.open_orders.get_mut(order_id) else { return };
        order.notional = (order.notional - filled_notional).max(Decimal::ZERO);
        let (token_id, left) = (order.token_id.clone(), order.notional);
        if left <= Decimal::ZERO {
            self.open_orders.remove(order_id);
        }
        tracing::debug!(
            order_id = order_id,
            token_id = token_id,
            filled = %filled_notional,
            still_reserved = %left,
            "Releasing the filled share of an order's exposure"
        );
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

#[cfg(test)]
mod tests {
    use super::*;
    use crate::position::MARK_STALE_PASSES;
    use rust_decimal_macros::dec;

    fn breaker(max_loss: Decimal) -> RiskManager {
        RiskManager::new(RiskLimits {
            max_position_size: Decimal::from(2500),
            max_total_exposure: Decimal::from(2500),
            max_loss,
            max_order_size: Decimal::from(1250),
        })
    }

    fn held(tracker: &mut PositionTracker, token: &str, size: Decimal, avg: Decimal) {
        tracker.reconcile(token, size, avg);
    }

    fn mark(tracker: &mut PositionTracker, token: &str, price: Decimal) {
        tracker.update_prices(&HashMap::from([(token.to_string(), price)]));
    }

    fn go_dark(tracker: &mut PositionTracker, passes: u64) {
        for _ in 0..passes {
            tracker.update_prices(&HashMap::new());
        }
    }

    /// SENSITIVITY GATE. The breaker must be exactly as quick on real losses
    /// as it ever was: a live, priceable, marked-down book crosses the
    /// threshold and halts on the very tick it crosses.
    #[test]
    fn real_marked_losses_still_trip_at_the_threshold() {
        let mut tracker = PositionTracker::new();
        held(&mut tracker, "live", dec!(1000), dec!(0.90));

        // -$390: under the limit, no halt.
        let mut rm = breaker(dec!(400));
        mark(&mut tracker, "live", dec!(0.51));
        assert_eq!(tracker.breaker_pnl(), dec!(-390.00));
        rm.check_pnl(&tracker);
        assert!(!rm.is_halted(), "must not trip above the threshold");

        // -$410: over the limit, halt on this tick.
        mark(&mut tracker, "live", dec!(0.49));
        assert_eq!(tracker.breaker_pnl(), dec!(-410.00));
        rm.check_pnl(&tracker);
        assert!(rm.is_halted(), "a real -$410 marked loss MUST still halt");
    }

    /// A realized loss counts in full even after the window that produced it
    /// has been unsubscribed and its exposure released.
    #[test]
    fn banked_realized_losses_still_trip() {
        use crate::position::Fill;
        let mut tracker = PositionTracker::new();
        tracker.apply_fill(&Fill {
            order_id: "1".into(), token_id: "gone".into(), is_buy: true,
            price: dec!(0.90), size: dec!(1000), timestamp: chrono::Utc::now(), fee: Decimal::ZERO,
        });
        tracker.apply_fill(&Fill {
            order_id: "2".into(), token_id: "gone".into(), is_buy: false,
            price: dec!(0.49), size: dec!(1000), timestamp: chrono::Utc::now(), fee: Decimal::ZERO,
        });
        tracker.remove("gone");

        let mut rm = breaker(dec!(400));
        rm.check_pnl(&tracker);
        assert!(
            rm.is_halted(),
            "a -$410 realized loss must reach the breaker even after the \
             token was unsubscribed"
        );
    }

    /// THE INCIDENT, end to end. Held, genuinely-owned inventory in windows
    /// that have just ended: the books have gone one-sided so the marks are
    /// frozen at the collapsed prices that printed on the way down, a burst
    /// of reconciles doubles the share counts off missed fills, and the
    /// data-api starts zeroing the resolved rows.
    ///
    /// Nothing here is a trade and nothing here is a price. The breaker must
    /// not fire.
    #[test]
    fn the_2026_08_24_reconcile_burst_does_not_halt_the_engine() {
        let mut tracker = PositionTracker::new();
        // Positions and avg costs as the engine held them at 00:09:30Z.
        held(&mut tracker, "btc-down", dec!(88), dec!(0.5548));
        held(&mut tracker, "eth-down", dec!(99), dec!(0.8016));
        held(&mut tracker, "sol-down", dec!(81), dec!(0.504));
        held(&mut tracker, "xrp-down", dec!(5), dec!(0.97));

        // Last two-sided mids before the down books went dark.
        tracker.update_prices(&HashMap::from([
            ("btc-down".to_string(), dec!(0.025)),
            ("eth-down".to_string(), dec!(0.19)),
            ("sol-down".to_string(), dec!(0.025)),
            ("xrp-down".to_string(), dec!(0.045)),
        ]));

        // Windows end; every book loses its quote.
        go_dark(&mut tracker, MARK_STALE_PASSES + 1);

        // The burst: genuine missed fills roughly double the share counts.
        assert_eq!(
            tracker.reconcile("sol-down", dec!(169), dec!(0.504)).delta(),
            dec!(88)
        );
        assert_eq!(
            tracker.reconcile("btc-down", dec!(184), dec!(0.5548)).delta(),
            dec!(96)
        );
        assert_eq!(
            tracker.reconcile("eth-down", dec!(120), dec!(0.8016)).delta(),
            dec!(21)
        );
        // ...and the data-api starts zeroing the resolved rows.
        assert!(matches!(
            tracker.reconcile("xrp-down", dec!(0), dec!(0)),
            crate::position::ReconcileOutcome::RefusedSettling(_)
        ));

        assert_eq!(
            tracker.total_realized_pnl(),
            dec!(0),
            "not one dollar of this is realized — no trade happened"
        );
        assert_eq!(tracker.breaker_pnl(), dec!(0));

        let mut rm = breaker(dec!(400));
        rm.check_pnl(&tracker);
        assert!(
            !rm.is_halted(),
            "the engine must NOT halt on frozen marks and accounting corrections"
        );

        // And the moment a real book comes back, the real mark counts again.
        mark(&mut tracker, "btc-down", dec!(0.02));
        assert_eq!(
            tracker.breaker_pnl(),
            dec!(184) * (dec!(0.02) - dec!(0.5548))
        );
    }

    /// The halt is one-shot: an already-tripped breaker does not re-announce.
    #[test]
    fn check_pnl_is_idempotent_once_tripped() {
        let mut tracker = PositionTracker::new();
        held(&mut tracker, "live", dec!(1000), dec!(0.90));
        mark(&mut tracker, "live", dec!(0.10));

        let mut rm = breaker(dec!(400));
        rm.check_pnl(&tracker);
        assert!(rm.is_halted());
        rm.check_pnl(&tracker);
        assert!(rm.is_halted());
    }

    // --- a partial fill releases a partial reservation -------------------

    /// An order for 30 shares at 0.90, reserved and confirmed the way the
    /// engine's signal path does it.
    fn with_open_order(rm: &mut RiskManager) {
        let positions = PositionTracker::new();
        let id = rm
            .reserve_exposure("t", dec!(27), &positions)
            .expect("a fresh manager has room");
        rm.confirm_reservation(&id, "o1");
        assert_eq!(rm.open_order_notional(), dec!(27));
    }

    #[test]
    fn a_partial_fill_releases_only_its_own_share_of_the_reservation() {
        // 5 of 30 shares land. 25 are still working on the book, so 22.50 of
        // the reservation is still real exposure. `order_closed` — what this
        // path called — released all 27 and told the manager the book was
        // clear, which is exposure nothing accounted for and a ceiling that
        // waved through the next signal.
        let mut rm = breaker(dec!(400));
        with_open_order(&mut rm);

        rm.order_filled("o1", dec!(4.5));
        assert_eq!(rm.open_order_notional(), dec!(22.5));
        assert_eq!(rm.total_open_orders(), 1, "the order is still on the book");

        // The rest lands and nothing is left behind.
        rm.order_filled("o1", dec!(22.5));
        assert_eq!(rm.open_order_notional(), Decimal::ZERO);
        assert_eq!(rm.total_open_orders(), 0);
    }

    #[test]
    fn an_overfilled_or_unknown_order_never_goes_negative() {
        let mut rm = breaker(dec!(400));
        with_open_order(&mut rm);
        // A fill priced above the reservation (a pay-up, a rounding) may not
        // credit the manager with exposure it never had.
        rm.order_filled("o1", dec!(99));
        assert_eq!(rm.open_order_notional(), Decimal::ZERO);
        // And a fill for an order this manager never tracked is a no-op,
        // not a panic — external and CLI orders exist.
        rm.order_filled("never-seen", dec!(10));
        assert_eq!(rm.open_order_notional(), Decimal::ZERO);
    }

    /// `order_closed` still exists and still means "this order is gone" —
    /// it is what a CANCEL calls, and a cancel really does free the lot.
    #[test]
    fn a_cancel_still_frees_the_whole_reservation() {
        let mut rm = breaker(dec!(400));
        with_open_order(&mut rm);
        rm.order_closed("o1");
        assert_eq!(rm.open_order_notional(), Decimal::ZERO);
    }
}
