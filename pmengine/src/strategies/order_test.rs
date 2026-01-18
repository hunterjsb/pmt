//! Test strategy that places a single order and stops.

use crate::strategy::{Signal, Strategy, StrategyContext, Urgency};
use crate::position::Fill;
use rust_decimal_macros::dec;

pub struct OrderTest {
    id: String,
    tokens: Vec<String>,
    order_placed: bool,
}

impl OrderTest {
    pub fn new() -> Self {
        // Vermont Governor 2026 - active market
        Self {
            id: "order_test".to_string(),
            tokens: vec!["41583919731714354912849507182398941127545694257513505398713274521520484370640".to_string()],
            order_placed: false,
        }
    }
}

impl Default for OrderTest {
    fn default() -> Self {
        Self::new()
    }
}

impl Strategy for OrderTest {
    fn id(&self) -> &str {
        &self.id
    }

    fn subscriptions(&self) -> Vec<String> {
        self.tokens.clone()
    }

    fn on_tick(&mut self, _ctx: &StrategyContext) -> Vec<Signal> {
        // Only place one order
        if self.order_placed {
            return vec![Signal::Hold];
        }

        self.order_placed = true;
        let token_id = "41583919731714354912849507182398941127545694257513505398713274521520484370640".to_string();

        tracing::info!("Placing test order: BUY 5 @ $0.01");

        vec![Signal::Buy {
            token_id,
            price: dec!(0.01),
            size: dec!(5),
            urgency: Urgency::Low,
        }]
    }

    fn on_fill(&mut self, fill: &Fill) {
        tracing::info!(order_id = %fill.order_id, "Order filled!");
    }
}
