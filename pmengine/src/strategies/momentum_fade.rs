//! Auto-generated from Python strategy: momentum_fade
//! DO NOT EDIT - regenerate with `pmstrat transpile`

use crate::strategy::{Signal, Strategy, StrategyContext, Urgency};
use crate::position::Fill;
#[allow(unused_imports)]
use rust_decimal::Decimal;
use rust_decimal_macros::dec;

// Strategy parameters (generated from Python params)
const SHORT_WINDOW: i64 = 60;
const LONG_WINDOW: i64 = 900;
const SPIKE_MULTIPLIER: Decimal = dec!(5.0);
const MIN_SHORT_VOLUME: Decimal = dec!(100);
const SUGGESTED_SIZE: Decimal = dec!(100);
const ALERT_TTL_SECS: i64 = 600;

// Module-level list[str] constants from the strategy file
const WATCH_TOKENS: &[&str] = &["95212449865986159112377413335252801281670333750637442556685159781445406848396"];

pub struct MomentumFade {
    id: String,
    tokens: Vec<String>,
}

impl MomentumFade {
    pub fn new() -> Self {
        Self {
            id: "momentum_fade".to_string(),
            tokens: vec!["95212449865986159112377413335252801281670333750637442556685159781445406848396".to_string()],
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
        let mut signals = vec![];
        for &token_id in WATCH_TOKENS {
            let book = match ctx.order_books.get(token_id) {
                Some(v) => v,
                None => continue,
            };
            let best_bid = match book.best_bid() {
                Some(v) => v.price,
                None => continue,
            };
            let best_ask = match book.best_ask() {
                Some(v) => v.price,
                None => continue,
            };
            let short_vol = ctx.volume_in_window(token_id, SHORT_WINDOW);
            if short_vol < MIN_SHORT_VOLUME {
                continue;
            }
            let long_vol = ctx.volume_in_window(token_id, LONG_WINDOW);
            let baseline_vol = long_vol - short_vol;
            if baseline_vol <= dec!(0) {
                continue;
            }
            let baseline_secs = LONG_WINDOW - SHORT_WINDOW;
            let baseline_rate = baseline_vol / Decimal::from(baseline_secs);
            let short_rate = short_vol / Decimal::from(SHORT_WINDOW);
            if short_rate < baseline_rate * SPIKE_MULTIPLIER {
                continue;
            }
            let mid = (best_bid + best_ask) / dec!(2);
            let mut suggested_price = mid - dec!(0.005);
            if suggested_price <= dec!(0.01) {
                suggested_price = dec!(0.01);
            }
            let bucket = (ctx.timestamp.timestamp() as i64) / 60;
            signals.push(Signal::Alert { reason: format!("vol spike: short {} / baseline {:.0} ({:.1}x)", short_vol, baseline_vol, short_rate / baseline_rate).to_string(), suggested: Box::new(Signal::Sell { token_id: token_id.to_string(), price: suggested_price, size: SUGGESTED_SIZE, urgency: Urgency::Medium }), ttl_secs: ALERT_TTL_SECS, dedupe_key: format!("momentum_fade-{}-{}", token_id.chars().take(16).collect::<String>(), bucket).to_string() });
        }
        if signals.is_empty() {
            return vec![Signal::Hold];
        }
        return signals;
    }

    fn on_fill(&mut self, _fill: &Fill) {}
    fn on_shutdown(&mut self) {}
}
