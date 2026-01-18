//! Auto-generated from Python strategy: spread_watcher
//! Fixed for Rust Option handling

use crate::strategy::{Signal, Strategy, StrategyContext, Urgency};
use crate::position::Fill;
use rust_decimal_macros::dec;

pub struct SpreadWatcher {
    id: String,
    tokens: Vec<String>,
}

impl SpreadWatcher {
    pub fn new() -> Self {
        Self {
            id: "spread_watcher".to_string(),
            tokens: vec!["41583919731714354912849507182398941127545694257513505398713274521520484370640".to_string()],
        }
    }
}

impl Default for SpreadWatcher {
    fn default() -> Self {
        Self::new()
    }
}

impl Strategy for SpreadWatcher {
    fn id(&self) -> &str {
        &self.id
    }

    fn subscriptions(&self) -> Vec<String> {
        self.tokens.clone()
    }

    fn on_tick(&mut self, ctx: &StrategyContext) -> Vec<Signal> {
        let token = "41583919731714354912849507182398941127545694257513505398713274521520484370640";
        let mut signals = vec![];

        let book = match ctx.order_books.get(token) {
            Some(b) => b,
            None => return signals,
        };

        let (bid, ask) = match (book.best_bid, book.best_ask) {
            (Some(b), Some(a)) => (b, a),
            _ => return signals,
        };

        let spread = ask - bid;

        // If spread is wide (> 0.50), place a bid in the middle
        if spread > dec!(0.50) {
            let mid = (bid + ask) / dec!(2);
            tracing::info!(
                spread = %spread,
                bid = %bid,
                ask = %ask,
                mid = %mid,
                "Wide spread detected, placing order"
            );
            signals.push(Signal::Buy {
                token_id: token.to_string(),
                price: mid,
                size: dec!(1),
                urgency: Urgency::Low,
            });
        }

        signals
    }

    fn on_fill(&mut self, _fill: &Fill) {}
    fn on_shutdown(&mut self) {}
}
