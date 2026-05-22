//! Momentum-Fade Spike Detector (hand-written; pmstrat source is the spec).
//!
//! Mirrors `pmstrat/strategies/momentum_fade.py`. Written by hand because the
//! transpiler doesn't yet handle a few patterns the Python uses (f-strings,
//! Option-unwrapping flow, Decimal-from-int coercion). Treat the Python file
//! as the source of truth for intent; this is the executable Rust copy.
//!
//! On each tick, for every watched token:
//!   - Compute volume in the last SHORT_WINDOW seconds (from MarketDataHub
//!     rolling buffer, exposed via ctx.volume_in_window).
//!   - Skip if volume is below MIN_SHORT_VOLUME (filter dust).
//!   - Compute baseline rate over LONG_WINDOW − SHORT_WINDOW prior seconds.
//!   - If short-window rate > baseline_rate × SPIKE_MULTIPLIER, emit a
//!     Signal::Alert proposing a Sell at mid − 0.005 (fade the move).
//!   - Dedupe by token + minute bucket so the same spike doesn't re-alert
//!     every 5s tick.

use crate::position::Fill;
use crate::strategy::{Signal, Strategy, StrategyContext, Urgency};
use rust_decimal::Decimal;
use rust_decimal_macros::dec;

const SHORT_WINDOW: i64 = 60;
const LONG_WINDOW: i64 = 900;
const SPIKE_MULTIPLIER: Decimal = dec!(5);
const MIN_SHORT_VOLUME: Decimal = dec!(100);
const SUGGESTED_SIZE: Decimal = dec!(100);
const ALERT_TTL_SECS: i64 = 600;

const HANTA_NO_TOKEN: &str =
    "95212449865986159112377413335252801281670333750637442556685159781445406848396";

pub struct MomentumFade {
    id: String,
    tokens: Vec<String>,
}

impl MomentumFade {
    pub fn new() -> Self {
        Self {
            id: "momentum_fade".to_string(),
            tokens: vec![HANTA_NO_TOKEN.to_string()],
        }
    }
}

impl Default for MomentumFade {
    fn default() -> Self {
        Self::new()
    }
}

impl Strategy for MomentumFade {
    fn id(&self) -> &str {
        &self.id
    }

    fn subscriptions(&self) -> Vec<String> {
        self.tokens.clone()
    }

    fn tick_interval_ms(&self) -> u64 {
        5000
    }

    fn on_tick(&mut self, ctx: &StrategyContext) -> Vec<Signal> {
        let mut signals: Vec<Signal> = Vec::new();
        for token_id in &self.tokens {
            let book = match ctx.order_books.get(token_id) {
                Some(b) => b,
                None => continue,
            };
            let (Some(best_bid), Some(best_ask)) = (book.best_bid(), book.best_ask()) else {
                continue;
            };

            let short_vol = ctx.volume_in_window(token_id, SHORT_WINDOW);
            if short_vol < MIN_SHORT_VOLUME {
                continue;
            }
            let long_vol = ctx.volume_in_window(token_id, LONG_WINDOW);
            let baseline_vol = long_vol - short_vol;
            if baseline_vol <= Decimal::ZERO {
                continue;
            }
            let baseline_secs = Decimal::from(LONG_WINDOW - SHORT_WINDOW);
            let baseline_rate = baseline_vol / baseline_secs;
            let short_rate = short_vol / Decimal::from(SHORT_WINDOW);
            if short_rate < baseline_rate * SPIKE_MULTIPLIER {
                continue;
            }

            let mid = (best_bid.price + best_ask.price) / dec!(2);
            let mut suggested_price = mid - dec!(0.005);
            if suggested_price <= dec!(0.01) {
                suggested_price = dec!(0.01);
            }

            let bucket = ctx.timestamp.timestamp() / 60;
            let short_prefix: String = token_id.chars().take(16).collect();
            let ratio = if baseline_rate.is_zero() {
                Decimal::ZERO
            } else {
                short_rate / baseline_rate
            };
            let reason = format!(
                "vol spike: short {} / baseline {} ({:.1}x)",
                short_vol, baseline_vol, ratio
            );
            let suggested = Signal::Sell {
                token_id: token_id.clone(),
                price: suggested_price,
                size: SUGGESTED_SIZE,
                urgency: Urgency::Medium,
            };
            signals.push(Signal::Alert {
                reason,
                suggested: Box::new(suggested),
                ttl_secs: ALERT_TTL_SECS,
                dedupe_key: format!("momentum_fade-{}-{}", short_prefix, bucket),
            });
        }
        if signals.is_empty() {
            return vec![Signal::Hold];
        }
        signals
    }

    fn on_fill(&mut self, _fill: &Fill) {}
}
