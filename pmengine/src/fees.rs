//! The exchange's fee schedule, measured off our own wallet.
//!
//! ONE definition. Every decision path, every replay and every grade prices a
//! fee through here so the engine's arithmetic and the money it is actually
//! charged cannot drift.
//!
//! # The schedule, and how we know
//!
//! Recovering the realized fee from `~/.pmt/corpus/activity.jsonl` as
//! `usdcSize - price*size` over every fee-bearing TRADE row on the trading
//! wallet gives, with **zero contradicting rows**:
//!
//! ```text
//!     fee = rate * p * (1 - p) * shares          taker
//!     fee = 0                                    maker (we were resting)
//! ```
//!
//! 1017 of 1017 fee-bearing updown fills match `0.07 * p * (1-p)` to the
//! API's 4-decimal quantisation; the 526 fills where we were the resting side
//! are charged exactly 0.0, and 409 of those would have owed more than half a
//! cent had they been takes. The shape holds beyond updown: across the whole
//! wallet the implied `rate` lands on 0.030 / 0.040 / 0.050 / 0.070 with no
//! row further than 0.002 off one of them — the RATE is per-market (it comes
//! off the arm, never re-typed here), the SHAPE is universal. There is no
//! size dependence (flat over four decades of clip size) and no per-series
//! difference (every updown series prices at 0.0700).
//!
//! # What this replaced, and why it mattered
//!
//! Every decision path used to subtract `rate * min(p, 1-p)`, which
//! over-charges by `1/max(p, 1-p)` — **2.0x at p = 0.50**, 1.11x at 0.90,
//! 1.02x at 0.98. Worst exactly at mid prices, which is the lane
//! `peer_intel.md` says is the only earning one: the engine was pricing
//! itself out of entries it could afford. Over this wallet's realized fill
//! mix the old shape modelled $189.99 against $157.24 actually paid.
//!
//! `analysis/strat15_search.md` §0a in the vault is the study; the check is
//! reproducible from the corpus dump at any time.

use rust_decimal::prelude::{FromPrimitive, ToPrimitive};
use rust_decimal::Decimal;

/// Per-share taker fee at `px` for a market whose schedule is `fee_rate`.
///
/// Symmetric about 0.5 by construction, which is what makes it the same
/// number on both legs of a pair: buying UP at `p` and buying DOWN at `1-p`
/// cost the identical fee, as the wallet shows.
///
/// `fee_rate` of 0.0 returns exactly 0.0 for any price — a fee-free series is
/// bit-identical to one that never had a fee term.
#[inline]
pub fn taker_fee(px: f64, fee_rate: f64) -> f64 {
    fee_rate * px * (1.0 - px)
}

/// Per-share fee on a fill we were the RESTING side of: zero, always.
///
/// A function rather than a bare `0.0` at each call site so a maker path
/// states which schedule it is on, and so the day a rebate or a maker fee
/// appears there is one place to put it.
#[inline]
pub fn maker_fee(_px: f64, _fee_rate: f64) -> f64 {
    0.0
}

/// `maker_fee` in the money type the live fill path speaks.
///
/// The realized-fill accounting (`Engine`'s trades poller -> `process_fill`)
/// works in `Decimal`, and it computes the fee the caller passes. Before this
/// it ran EVERY fill through the taker arithmetic — including the ones where
/// the exchange's own `trader_side` says we were resting — so a maker fill
/// booked a fee the wallet never charged and the ledger drifted off the
/// scoreboard by exactly that amount (`analysis/bracket_exit.md` §9.4).
///
/// Delegating to the same schedule rather than writing `Decimal::ZERO` at the
/// call site is the L18 rule: one definition, so a rebate or a maker fee
/// lands in one place and cannot be half-applied.
#[inline]
pub fn maker_fee_dec(px: Decimal, fee_rate: Decimal) -> Decimal {
    let px = px.to_f64().unwrap_or(0.0);
    let rate = fee_rate.to_f64().unwrap_or(0.0);
    Decimal::from_f64(maker_fee(px, rate)).unwrap_or(Decimal::ZERO)
}

#[cfg(test)]
mod tests {
    use super::*;

    /// The curve, pinned at three prices. These are the wallet's numbers, not
    /// a re-derivation: 0.07 * p * (1-p) per share.
    #[test]
    fn taker_fee_is_rate_times_p_times_one_minus_p() {
        assert!((taker_fee(0.05, 0.07) - 0.07 * 0.05 * 0.95).abs() < 1e-12);
        assert!((taker_fee(0.50, 0.07) - 0.0175).abs() < 1e-12);
        assert!((taker_fee(0.95, 0.07) - 0.07 * 0.95 * 0.05).abs() < 1e-12);
    }

    /// The correction's whole point: at mid price the old `min(p, 1-p)` shape
    /// charged double. A regression to it fails here.
    #[test]
    fn taker_fee_at_mid_is_half_the_old_min_shape() {
        let old = 0.07 * f64::min(0.5, 0.5);
        assert!((taker_fee(0.50, 0.07) - old / 2.0).abs() < 1e-12);
        // and only ~5% cheap at 0.95, where the two shapes nearly agree
        let old_95 = 0.07 * f64::min(0.95, 0.05);
        assert!((taker_fee(0.95, 0.07) / old_95 - 0.95).abs() < 1e-12);
    }

    /// Both legs of a pair pay the same fee — the symmetry the wallet shows.
    #[test]
    fn taker_fee_is_symmetric_about_a_half() {
        for p in [0.01, 0.13, 0.37, 0.5, 0.62, 0.88, 0.99] {
            assert!((taker_fee(p, 0.07) - taker_fee(1.0 - p, 0.07)).abs() < 1e-15);
        }
    }

    /// A fee-free series must be bit-identical everywhere.
    #[test]
    fn zero_rate_is_exactly_zero_at_every_price() {
        for p in [0.0, 0.01, 0.5, 0.99, 1.0] {
            assert_eq!(taker_fee(p, 0.0), 0.0);
        }
    }

    /// Resting fills pay nothing — 526 of 526 wallet rows.
    #[test]
    fn maker_fee_is_zero_at_every_price_and_rate() {
        for p in [0.05, 0.5, 0.95] {
            assert_eq!(maker_fee(p, 0.07), 0.0);
            assert_eq!(maker_fee(p, 0.0), 0.0);
        }
    }

    /// The `Decimal` face of the same schedule. The live fill path books in
    /// this type, and a resting SELL is the first order shape that makes the
    /// maker branch reachable from `process_fill` — it has to charge the
    /// wallet's zero, not the taker curve's cents.
    #[test]
    fn the_decimal_maker_fee_is_the_same_zero() {
        use rust_decimal_macros::dec;
        for p in [dec!(0.05), dec!(0.50), dec!(0.95), dec!(0.99)] {
            assert_eq!(maker_fee_dec(p, dec!(0.07)), Decimal::ZERO, "{p}");
            assert_eq!(maker_fee_dec(p, dec!(0.0)), Decimal::ZERO, "{p}");
        }
        // …and it is a real zero, not a rounding of the taker number: at 0.50
        // the taker schedule is 1.75 cents a share, the widest the curve gets.
        assert!(taker_fee(0.50, 0.07) > 0.017);
    }
}
