//! Position and P&L tracking.

use rust_decimal::Decimal;
use serde::{Deserialize, Serialize};
use std::collections::HashMap;

/// How many marking passes a position may go unmarked before its mark is
/// STALE and stops feeding the circuit breaker.
///
/// `update_prices` runs once per engine tick (50ms live), and only marks
/// tokens whose book has a two-sided quote — `OrderBook::mid_price` returns
/// `None` otherwise. A binary that is resolving loses one side of its book
/// and then goes dark entirely, so its last mid FREEZES at whatever the
/// market printed on the way down. On 2026-08-24 the sol-updown-5m down
/// token lost its bid at 00:09:25 and the breaker was still marking 169
/// held shares against that frozen 00:09:24 price six seconds later.
///
/// A price we have not seen in 5 seconds is not a price we can trade at,
/// and a position we cannot price is settlement's business, not the loss
/// gauge's. 100 passes = 5s at the live tick.
pub const MARK_STALE_PASSES: u64 = 100;

/// A single position in a token.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct Position {
    pub token_id: String,
    pub size: Decimal,
    pub avg_entry_price: Decimal,
    pub realized_pnl: Decimal,
    pub unrealized_pnl: Decimal,
    pub last_price: Option<Decimal>,
    /// Tracker mark-pass number this position was last marked on. Compared
    /// against `PositionTracker::mark_pass` to decide whether `last_price`
    /// is still fresh enough to risk-manage off. A pass counter rather than
    /// a clock so the staleness rule is deterministic in tests.
    #[serde(default)]
    pub marked_at_pass: u64,
}

impl Position {
    pub fn new(token_id: String) -> Self {
        Self {
            token_id,
            size: Decimal::ZERO,
            avg_entry_price: Decimal::ZERO,
            realized_pnl: Decimal::ZERO,
            unrealized_pnl: Decimal::ZERO,
            last_price: None,
            marked_at_pass: 0,
        }
    }

    /// Apply a fill to this position.
    pub fn apply_fill(&mut self, fill: &Fill) {
        let old_size = self.size;
        let fill_value = fill.price * fill.size;

        if fill.is_buy {
            // Buying: increase position
            if old_size >= Decimal::ZERO {
                // Adding to long position - update average
                let old_value = self.avg_entry_price * old_size;
                let new_size = old_size + fill.size;
                if new_size > Decimal::ZERO {
                    self.avg_entry_price = (old_value + fill_value) / new_size;
                }
                self.size = new_size;
            } else {
                // Covering short position
                let cover_size = fill.size.min(-old_size);
                let new_long = fill.size - cover_size;

                // Realize P&L on covered portion
                self.realized_pnl += cover_size * (self.avg_entry_price - fill.price);

                self.size = old_size + fill.size;
                if new_long > Decimal::ZERO && self.size > Decimal::ZERO {
                    self.avg_entry_price = fill.price;
                }
            }
        } else {
            // Selling: decrease position
            if old_size <= Decimal::ZERO {
                // Adding to short position - update average
                let old_value = self.avg_entry_price * (-old_size);
                let new_size = old_size - fill.size;
                if new_size < Decimal::ZERO {
                    self.avg_entry_price = (old_value + fill_value) / (-new_size);
                }
                self.size = new_size;
            } else {
                // Closing long position
                let close_size = fill.size.min(old_size);
                let new_short = fill.size - close_size;

                // Realize P&L on closed portion
                self.realized_pnl += close_size * (fill.price - self.avg_entry_price);

                self.size = old_size - fill.size;
                if new_short > Decimal::ZERO && self.size < Decimal::ZERO {
                    self.avg_entry_price = fill.price;
                }
            }
        }
    }

    /// Update unrealized P&L with current price.
    pub fn update_price(&mut self, price: Decimal) {
        self.last_price = Some(price);
        if self.size > Decimal::ZERO {
            self.unrealized_pnl = self.size * (price - self.avg_entry_price);
        } else if self.size < Decimal::ZERO {
            self.unrealized_pnl = (-self.size) * (self.avg_entry_price - price);
        } else {
            self.unrealized_pnl = Decimal::ZERO;
        }
    }

    /// Get notional value of position.
    pub fn notional(&self) -> Decimal {
        self.size.abs() * self.last_price.unwrap_or(self.avg_entry_price)
    }
}

/// A fill event from order execution.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct Fill {
    pub order_id: String,
    pub token_id: String,
    pub is_buy: bool,
    pub price: Decimal,
    pub size: Decimal,
    pub timestamp: chrono::DateTime<chrono::Utc>,
    pub fee: Decimal,
}

/// What a reconcile pass did to a position.
///
/// Reconcile is ACCOUNTING, not trading: it corrects the engine's share count
/// against the data-api. Distinguishing "corrected" from "refused" matters
/// because a data-api zero on a position we cannot price is a redemption,
/// not a missed sell — see `PositionTracker::reconcile`.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum ReconcileOutcome {
    /// data-api agrees with the engine — nothing to do.
    Unchanged,
    /// Drift corrected; carries the size delta applied.
    Corrected(Decimal),
    /// data-api reported ZERO for a position whose mark has gone stale — the
    /// settlement signature. Refused: the held size stands. Carries what we
    /// still believe we hold.
    RefusedSettling(Decimal),
}

impl ReconcileOutcome {
    /// Size delta actually applied (zero unless a correction happened).
    pub fn delta(&self) -> Decimal {
        match self {
            ReconcileOutcome::Corrected(d) => *d,
            _ => Decimal::ZERO,
        }
    }
}

/// Tracks all positions.
#[derive(Debug, Clone, Default)]
pub struct PositionTracker {
    positions: HashMap<String, Position>,
    /// Monotonic marking-pass counter, bumped once per `update_prices`.
    mark_pass: u64,
    /// Realized P&L of positions already dropped from the map.
    ///
    /// `remove` used to delete a Position outright — and with it every
    /// realized dollar that token had booked. On a 5-minute roll cadence
    /// that reset the session ledger every window, so `max_loss` could
    /// never see a session at all. Realized P&L is banked here instead;
    /// only the EXPOSURE leaves.
    banked_realized_pnl: Decimal,
}

impl PositionTracker {
    pub fn new() -> Self {
        Self {
            positions: HashMap::new(),
            mark_pass: 0,
            banked_realized_pnl: Decimal::ZERO,
        }
    }

    /// Whether this position's mark is fresh enough to risk-manage off.
    fn mark_is_fresh(&self, position: &Position) -> bool {
        position.last_price.is_some()
            && self.mark_pass.saturating_sub(position.marked_at_pass) <= MARK_STALE_PASSES
    }

    /// Whether a token currently carries a fresh, tradeable mark.
    pub fn has_fresh_mark(&self, token_id: &str) -> bool {
        self.positions
            .get(token_id)
            .is_some_and(|p| self.mark_is_fresh(p))
    }

    /// Get position for a token, creating if needed.
    pub fn get_or_create(&mut self, token_id: &str) -> &mut Position {
        self.positions
            .entry(token_id.to_string())
            .or_insert_with(|| Position::new(token_id.to_string()))
    }

    /// Get position for a token (read-only).
    pub fn get(&self, token_id: &str) -> Option<&Position> {
        self.positions.get(token_id)
    }

    /// Drop a token's position from the ledger, returning its notional.
    /// For tokens the engine stops managing (e.g. a resolved binary whose
    /// shares redeem on-chain) — a preserved entry would count as live
    /// exposure forever and starve the risk manager.
    ///
    /// Releasing EXPOSURE must not erase HISTORY: whatever this token
    /// realized is banked into the session total on the way out.
    pub fn remove(&mut self, token_id: &str) -> Decimal {
        self.positions
            .remove(token_id)
            .map(|p| {
                self.banked_realized_pnl += p.realized_pnl;
                p.notional()
            })
            .unwrap_or(Decimal::ZERO)
    }

    /// Apply a fill.
    pub fn apply_fill(&mut self, fill: &Fill) {
        let position = self.get_or_create(&fill.token_id);
        position.apply_fill(fill);
        tracing::info!(
            token_id = fill.token_id,
            size = %position.size,
            avg_entry = %position.avg_entry_price,
            realized_pnl = %position.realized_pnl,
            "Position updated"
        );
    }

    /// Reconcile a position's size + avg entry against the authoritative
    /// on-chain holding (from the data-api). Corrects drift caused by fills
    /// that incremental detection missed — the data-api is ground truth.
    ///
    /// Preserves accumulated `realized_pnl` and `last_price` (those aren't
    /// recoverable from a position snapshot).
    ///
    /// Two invariants, both bought with the 2026-08-24 halt:
    ///
    /// 1. **A correction is not a trade.** Reconcile moves SHARES, never
    ///    `realized_pnl`. It re-marks only when the position still carries a
    ///    fresh price; re-deriving `size * (last_price - avg)` off a FROZEN
    ///    mark is how a burst of genuine missed fills turned into a $412
    ///    paper loss and halted the engine.
    /// 2. **A zero on an unpriceable position is settlement, not drift.**
    ///    After a window resolves the data-api moves the row to
    ///    redeemable-with-size-0 before the engine's own cleanup unsubscribes
    ///    it. Zeroing there would delete genuinely-held (often winning)
    ///    inventory from the ledger that `MAX_POSITION` and the strategies'
    ///    budgets read. A stale mark is exactly that post-end pre-cleanup
    ///    state, so the correction is REFUSED and reported.
    ///
    /// Mid-window drift — the case reconcile exists for — has a fresh mark by
    /// definition (the book is two-sided and quoting), so it still corrects,
    /// in either direction, including all the way to zero.
    pub fn reconcile(
        &mut self,
        token_id: &str,
        true_size: Decimal,
        avg_price: Decimal,
    ) -> ReconcileOutcome {
        // Freshness must be read before the mutable borrow.
        let fresh = self
            .positions
            .get(token_id)
            .is_some_and(|p| self.mark_is_fresh(p));
        let pass = self.mark_pass;

        let position = self.get_or_create(token_id);
        let delta = true_size - position.size;
        if delta == Decimal::ZERO {
            return ReconcileOutcome::Unchanged;
        }

        // Invariant 2: settlement is not drift.
        if true_size == Decimal::ZERO && position.size != Decimal::ZERO && !fresh {
            return ReconcileOutcome::RefusedSettling(position.size);
        }

        position.size = true_size;
        // Only adopt the data-api avg when we actually hold something;
        // a flat position has no meaningful entry price.
        if true_size != Decimal::ZERO {
            position.avg_entry_price = avg_price;
        }
        // Invariant 1: re-mark only off a price we can still trade at. A
        // stale-marked position is excluded from the breaker anyway
        // (`breaker_pnl`), and leaving its figure untouched keeps a frozen
        // price from being restated as a fresh loss.
        if fresh {
            if let Some(p) = position.last_price {
                position.update_price(p);
                position.marked_at_pass = pass;
            }
        }
        ReconcileOutcome::Corrected(delta)
    }

    /// Update prices for all positions.
    ///
    /// One call = one marking pass, whether or not any price came with it:
    /// an empty map means every book went dark, which is precisely when
    /// marks must be allowed to go stale.
    pub fn update_prices(&mut self, prices: &HashMap<String, Decimal>) {
        self.mark_pass += 1;
        let pass = self.mark_pass;
        for (token_id, price) in prices {
            if let Some(position) = self.positions.get_mut(token_id) {
                position.update_price(*price);
                position.marked_at_pass = pass;
            }
        }
    }

    /// Get total realized P&L for the session — live positions plus every
    /// position already released from the ledger.
    pub fn total_realized_pnl(&self) -> Decimal {
        self.banked_realized_pnl + self.positions.values().map(|p| p.realized_pnl).sum::<Decimal>()
    }

    /// Get total unrealized P&L across all positions. Reporting figure: it
    /// includes stale marks, because a report should show the whole book.
    /// The circuit breaker reads `breaker_pnl` instead.
    pub fn total_unrealized_pnl(&self) -> Decimal {
        self.positions.values().map(|p| p.unrealized_pnl).sum()
    }

    /// Session P&L as the circuit breaker must see it: realized in full,
    /// plus mark-to-market on ONLY those positions currently carrying a
    /// fresh, tradeable mark.
    ///
    /// A position whose book has gone one-sided or dark cannot be priced and
    /// cannot be exited; its outcome is decided by settlement, not by the
    /// last mid that happened to print on the way there. Counting a frozen
    /// mark as a loss is what tripped a $400 breaker on a book whose entire
    /// cost basis was $288.
    ///
    /// Every position the engine can actually price is counted in full, so
    /// real losses reach the breaker undiminished and undelayed.
    pub fn breaker_pnl(&self) -> Decimal {
        self.total_realized_pnl()
            + self
                .positions
                .values()
                .filter(|p| self.mark_is_fresh(p))
                .map(|p| p.unrealized_pnl)
                .sum::<Decimal>()
    }

    /// Tokens holding shares whose mark has gone stale — excluded from
    /// `breaker_pnl`. Surfaced so a halt (or its absence) is explainable.
    pub fn stale_marked_tokens(&self) -> Vec<&str> {
        self.positions
            .values()
            .filter(|p| p.size != Decimal::ZERO && !self.mark_is_fresh(p))
            .map(|p| p.token_id.as_str())
            .collect()
    }

    /// Get total notional exposure.
    pub fn total_notional(&self) -> Decimal {
        self.positions.values().map(|p| p.notional()).sum()
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use rust_decimal_macros::dec;

    #[test]
    fn test_position_long() {
        let mut pos = Position::new("token1".to_string());

        // Buy 10 at 0.50
        pos.apply_fill(&Fill {
            order_id: "1".to_string(),
            token_id: "token1".to_string(),
            is_buy: true,
            price: dec!(0.50),
            size: dec!(10),
            timestamp: chrono::Utc::now(),
            fee: Decimal::ZERO,
        });
        assert_eq!(pos.size, dec!(10));
        assert_eq!(pos.avg_entry_price, dec!(0.50));

        // Sell 5 at 0.60 (realize profit)
        pos.apply_fill(&Fill {
            order_id: "2".to_string(),
            token_id: "token1".to_string(),
            is_buy: false,
            price: dec!(0.60),
            size: dec!(5),
            timestamp: chrono::Utc::now(),
            fee: Decimal::ZERO,
        });
        assert_eq!(pos.size, dec!(5));
        assert_eq!(pos.realized_pnl, dec!(0.50)); // 5 * (0.60 - 0.50)
    }

    /// Mark `token` at `price`, on a fresh pass.
    fn mark(tracker: &mut PositionTracker, token: &str, price: Decimal) {
        tracker.update_prices(&HashMap::from([(token.to_string(), price)]));
    }

    /// Let `passes` marking passes go by with no price for anything — what
    /// happens when a resolving binary's book loses its two-sided quote.
    fn go_dark(tracker: &mut PositionTracker, passes: u64) {
        for _ in 0..passes {
            tracker.update_prices(&HashMap::new());
        }
    }

    #[test]
    fn test_reconcile_corrects_missed_fill() {
        let mut tracker = PositionTracker::new();
        // Engine thinks it holds 50 (seeded at startup).
        tracker.reconcile("t", dec!(50), dec!(0.90));
        assert_eq!(tracker.get("t").unwrap().size, dec!(50));
        // Live, quoting book.
        mark(&mut tracker, "t", dec!(0.90));

        // A 5-share sell filled but the trades-poll missed it; data-api now
        // reports 45. Reconcile corrects the drift and returns the delta.
        let outcome = tracker.reconcile("t", dec!(45), dec!(0.90));
        assert_eq!(outcome, ReconcileOutcome::Corrected(dec!(-5)));
        assert_eq!(tracker.get("t").unwrap().size, dec!(45));

        // No change → Unchanged, no-op.
        let outcome2 = tracker.reconcile("t", dec!(45), dec!(0.90));
        assert_eq!(outcome2, ReconcileOutcome::Unchanged);
        assert_eq!(outcome2.delta(), dec!(0));
    }

    /// The reconcile's REAL job, which must keep working: a taker fill the
    /// WS user feed never delivered, caught mid-window against a live book.
    #[test]
    fn mid_window_missed_fill_still_corrects_and_reports() {
        let mut tracker = PositionTracker::new();
        tracker.reconcile("t", dec!(51), dec!(0.98));
        mark(&mut tracker, "t", dec!(0.97));

        // 37 more shares filled; the engine never saw the fill.
        let outcome = tracker.reconcile("t", dec!(88), dec!(0.98));
        assert_eq!(
            outcome,
            ReconcileOutcome::Corrected(dec!(37)),
            "a mid-window missed fill must still be corrected AND reported \
             — this is the drift-warning path the engine logs"
        );
        assert_eq!(tracker.get("t").unwrap().size, dec!(88));
        // Fresh mark, so the position re-marks against it immediately.
        assert_eq!(
            tracker.get("t").unwrap().unrealized_pnl,
            dec!(88) * (dec!(0.97) - dec!(0.98))
        );
    }

    /// The settling refusal must not over-block: a genuine mid-window
    /// sell-out to zero, against a live book, still reconciles to zero.
    #[test]
    fn mid_window_sellout_to_zero_still_corrects() {
        let mut tracker = PositionTracker::new();
        tracker.reconcile("t", dec!(40), dec!(0.60));
        mark(&mut tracker, "t", dec!(0.61));

        let outcome = tracker.reconcile("t", dec!(0), dec!(0));
        assert_eq!(outcome, ReconcileOutcome::Corrected(dec!(-40)));
        assert_eq!(tracker.get("t").unwrap().size, dec!(0));
    }

    /// THE INCIDENT, part 1 — 2026-08-24T00:09:30Z.
    ///
    /// A held, winning position whose window has ended. The data-api moves
    /// the row to redeemable-with-size-0 before the engine's own cleanup
    /// unsubscribes the token. That zero is settlement, not drift: it must
    /// not delete the shares, and above all it must not manufacture a loss.
    #[test]
    fn settling_zero_is_refused_and_books_no_loss() {
        let mut tracker = PositionTracker::new();
        // 184 shares at 0.5548 — the btc-updown-5m down leg.
        tracker.reconcile("btc-down", dec!(184), dec!(0.5548));
        // Marked while the book was still two-sided and the position winning.
        mark(&mut tracker, "btc-down", dec!(0.945));
        assert!(tracker.get("btc-down").unwrap().unrealized_pnl > Decimal::ZERO);

        // Window ends. The book loses its quote; nothing marks it again.
        go_dark(&mut tracker, MARK_STALE_PASSES + 1);

        // data-api now reports the row at zero.
        let outcome = tracker.reconcile("btc-down", dec!(0), dec!(0));
        assert_eq!(
            outcome,
            ReconcileOutcome::RefusedSettling(dec!(184)),
            "a zero on a position we can no longer price is a redemption"
        );

        let pos = tracker.get("btc-down").unwrap();
        assert_eq!(pos.size, dec!(184), "genuinely-held shares must survive");
        assert_eq!(
            pos.realized_pnl,
            dec!(0),
            "an accounting correction is not a trade — it books NO realized P&L"
        );
        assert_eq!(
            tracker.total_realized_pnl(),
            dec!(0),
            "no phantom realized loss anywhere in the session ledger"
        );
    }

    /// THE INCIDENT, part 2 — the frozen mark.
    ///
    /// sol-updown-5m's down token lost its bid at 00:09:25; `mid_price`
    /// returned None from then on, so its mark froze at the 0.025 that
    /// printed on the way down. Six seconds later the breaker was still
    /// valuing 169 held shares against it.
    #[test]
    fn a_frozen_mark_is_excluded_from_the_breaker() {
        let mut tracker = PositionTracker::new();
        tracker.reconcile("sol-down", dec!(169), dec!(0.504));
        mark(&mut tracker, "sol-down", dec!(0.025));

        // While the mark is fresh it counts in full — no special pleading.
        let marked = dec!(169) * (dec!(0.025) - dec!(0.504));
        assert_eq!(tracker.breaker_pnl(), marked);
        assert!(tracker.stale_marked_tokens().is_empty());

        // The book goes dark. The mark stops being a price.
        go_dark(&mut tracker, MARK_STALE_PASSES + 1);

        assert_eq!(
            tracker.breaker_pnl(),
            dec!(0),
            "a position the engine cannot price is settlement's business"
        );
        assert_eq!(tracker.stale_marked_tokens(), vec!["sol-down"]);
        assert_eq!(
            tracker.total_unrealized_pnl(),
            marked,
            "the REPORTING figure still shows the whole book"
        );
    }

    /// THE INCIDENT, part 3 — the amplifier.
    ///
    /// Every share that session arrived through reconcile (the WS user feed
    /// was flapping and on_fill never ran). In the last twelve seconds the
    /// corrections roughly DOUBLED the share counts. Unrealized P&L is
    /// linear in size, so re-deriving it off the frozen mark doubled the
    /// paper loss in one step, with no trade and no new price.
    #[test]
    fn a_reconcile_burst_does_not_restate_pnl_off_a_frozen_mark() {
        let mut tracker = PositionTracker::new();
        tracker.reconcile("sol-down", dec!(81), dec!(0.504));
        mark(&mut tracker, "sol-down", dec!(0.025));
        let before = tracker.get("sol-down").unwrap().unrealized_pnl;

        go_dark(&mut tracker, MARK_STALE_PASSES + 1);

        // The 00:09:24 correction: 81 -> 169, all genuine missed fills.
        let outcome = tracker.reconcile("sol-down", dec!(169), dec!(0.504));
        assert_eq!(outcome, ReconcileOutcome::Corrected(dec!(88)));
        assert_eq!(tracker.get("sol-down").unwrap().size, dec!(169));
        assert_eq!(
            tracker.get("sol-down").unwrap().unrealized_pnl,
            before,
            "a correction must not restate P&L against a price we can no \
             longer see — that restatement is what halted the engine"
        );
        assert_eq!(tracker.breaker_pnl(), dec!(0));
    }

    /// Reconcile moves shares. It never moves realized P&L, in any direction,
    /// on any book state. This is the invariant the breaker depends on.
    #[test]
    fn reconcile_never_books_realized_pnl() {
        for (from, to, fresh) in [
            (dec!(50), dec!(90), true),
            (dec!(50), dec!(10), true),
            (dec!(50), dec!(0), true),
            (dec!(50), dec!(90), false),
            (dec!(50), dec!(0), false),
        ] {
            let mut tracker = PositionTracker::new();
            tracker.reconcile("t", from, dec!(0.70));
            mark(&mut tracker, "t", dec!(0.30));
            if !fresh {
                go_dark(&mut tracker, MARK_STALE_PASSES + 1);
            }
            tracker.reconcile("t", to, dec!(0.70));
            assert_eq!(
                tracker.total_realized_pnl(),
                dec!(0),
                "reconcile {from} -> {to} (fresh={fresh}) booked realized P&L"
            );
        }
    }

    /// Releasing a token's EXPOSURE must not erase its HISTORY. `remove`
    /// used to drop the Position outright, so on a 5-minute roll cadence the
    /// session's realized ledger reset every window and `max_loss` could
    /// never see a session at all.
    #[test]
    fn remove_banks_realized_pnl_instead_of_deleting_it() {
        let mut tracker = PositionTracker::new();
        tracker.apply_fill(&Fill {
            order_id: "1".to_string(), token_id: "t".to_string(), is_buy: true,
            price: dec!(0.50), size: dec!(10), timestamp: chrono::Utc::now(), fee: Decimal::ZERO,
        });
        tracker.apply_fill(&Fill {
            order_id: "2".to_string(), token_id: "t".to_string(), is_buy: false,
            price: dec!(0.20), size: dec!(10), timestamp: chrono::Utc::now(), fee: Decimal::ZERO,
        });
        assert_eq!(tracker.total_realized_pnl(), dec!(-3.0));

        tracker.remove("t");
        assert_eq!(
            tracker.total_realized_pnl(),
            dec!(-3.0),
            "a realized loss must survive the window that produced it"
        );
        assert_eq!(tracker.total_notional(), dec!(0), "exposure is still released");
    }

    /// A mark that keeps arriving never goes stale, however long the engine
    /// runs — staleness is about silence, not age.
    #[test]
    fn a_continuously_marked_position_never_goes_stale() {
        let mut tracker = PositionTracker::new();
        tracker.reconcile("t", dec!(100), dec!(0.80));
        for _ in 0..(MARK_STALE_PASSES * 3) {
            mark(&mut tracker, "t", dec!(0.20));
        }
        assert!(tracker.stale_marked_tokens().is_empty());
        assert_eq!(tracker.breaker_pnl(), dec!(100) * (dec!(0.20) - dec!(0.80)));
    }

    #[test]
    fn test_reconcile_preserves_realized_pnl() {
        let mut tracker = PositionTracker::new();
        tracker.apply_fill(&Fill {
            order_id: "1".to_string(), token_id: "t".to_string(), is_buy: true,
            price: dec!(0.50), size: dec!(10), timestamp: chrono::Utc::now(), fee: Decimal::ZERO,
        });
        tracker.apply_fill(&Fill {
            order_id: "2".to_string(), token_id: "t".to_string(), is_buy: false,
            price: dec!(0.60), size: dec!(5), timestamp: chrono::Utc::now(), fee: Decimal::ZERO,
        });
        let pnl_before = tracker.get("t").unwrap().realized_pnl;
        assert_eq!(pnl_before, dec!(0.50));

        // Reconcile to a different size — realized P&L must survive.
        tracker.reconcile("t", dec!(8), dec!(0.55));
        assert_eq!(tracker.get("t").unwrap().size, dec!(8));
        assert_eq!(tracker.get("t").unwrap().realized_pnl, dec!(0.50));
    }
}
