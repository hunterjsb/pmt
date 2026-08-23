//! Auto-generated from Python strategy: example
//! DO NOT EDIT - regenerate with `pmstrat transpile`

use crate::strategy::{Signal, Strategy, StrategyContext};
// Only order-placing strategies reach these; an inert one must still compile
// warning-free under the clippy gate.
#[allow(unused_imports)]
use crate::strategy::Urgency;
use crate::position::Fill;
#[allow(unused_imports)]
use rust_decimal::Decimal;
#[allow(unused_imports)]
use rust_decimal_macros::dec;

pub struct Example {
    id: String,
    tokens: Vec<String>,
}

impl Example {
    pub fn new() -> Self {
        Self {
            id: "example".to_string(),
            tokens: vec!["0000000000000000000000000000000000000000000000000000000000000000".to_string()],
        }
    }
}

impl Default for Example {
    fn default() -> Self {
        Self::new()
    }
}

impl Strategy for Example {
    fn id(&self) -> &str {
        &self.id
    }

    fn subscriptions(&self) -> Vec<String> {
        self.tokens.clone()
    }

    fn tick_interval_ms(&self) -> u64 {
        60000
    }

    fn on_tick(&mut self, ctx: &StrategyContext) -> Vec<Signal> {
        let mut signals = vec![];
        let _book = match ctx.order_books.get("0000000000000000000000000000000000000000000000000000000000000000") {
            Some(v) => v,
            None => return signals,
        };
        signals.push(Signal::Hold);
        signals
    }

    fn on_fill(&mut self, _fill: &Fill) {}
    fn on_shutdown(&mut self) {}
}
