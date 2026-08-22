//! Auto-generated from Python strategy: hanta_maker
//! DO NOT EDIT - regenerate with `pmstrat transpile`

use crate::strategy::{Signal, Strategy, StrategyContext, Urgency};
use crate::position::Fill;
#[allow(unused_imports)]
use rust_decimal::Decimal;
use rust_decimal_macros::dec;

// Strategy parameters (generated from Python params)
const TOKEN_ID: &str = "95212449865986159112377413335252801281670333750637442556685159781445406848396";
const HALF_SPREAD: Decimal = dec!(0.002);
const ORDER_SIZE: Decimal = dec!(400);
const MAX_POSITION: Decimal = dec!(2000);
const MIN_EDGE: Decimal = dec!(0.001);

pub struct HantaMaker {
    id: String,
    tokens: Vec<String>,
}

impl HantaMaker {
    pub fn new() -> Self {
        Self {
            id: "hanta_maker".to_string(),
            tokens: vec!["95212449865986159112377413335252801281670333750637442556685159781445406848396".to_string()],
        }
    }
}

impl Default for HantaMaker {
    fn default() -> Self {
        Self::new()
    }
}

impl Strategy for HantaMaker {
    fn id(&self) -> &str {
        &self.id
    }

    fn subscriptions(&self) -> Vec<String> {
        self.tokens.clone()
    }

    fn tick_interval_ms(&self) -> u64 {
        10000
    }

    fn on_tick(&mut self, ctx: &StrategyContext) -> Vec<Signal> {
        let mut signals = vec![];
        let token_id = TOKEN_ID;
        let book = match ctx.order_books.get(token_id) {
            Some(v) => v,
            None => return vec![Signal::Hold],
        };
        let bid = match book.best_bid() {
            Some(v) => v.price,
            None => return vec![Signal::Hold],
        };
        let ask = match book.best_ask() {
            Some(v) => v.price,
            None => return vec![Signal::Hold],
        };
        let mid = (bid + ask) / dec!(2);
        let mut my_bid = mid - HALF_SPREAD;
        let mut my_ask = mid + HALF_SPREAD;
        if my_ask - my_bid < MIN_EDGE * dec!(2) {
            return vec![Signal::Hold];
        }
        if my_bid < dec!(0.01) {
            my_bid = dec!(0.01);
        }
        if my_ask > dec!(0.99) {
            my_ask = dec!(0.99);
        }
        signals.push(Signal::Cancel { token_id: token_id.to_string() });
        let position = ctx.positions.get(token_id);
        let mut position_size = dec!(0);
        if let Some(position) = position {
            position_size = position.size;
        }
        if position_size < MAX_POSITION {
            signals.push(Signal::Buy { token_id: token_id.to_string(), price: my_bid, size: ORDER_SIZE, urgency: Urgency::Medium });
        }
        signals.push(Signal::Sell { token_id: token_id.to_string(), price: my_ask, size: ORDER_SIZE, urgency: Urgency::Medium });
        signals
    }

    fn on_fill(&mut self, _fill: &Fill) {}
    fn on_shutdown(&mut self) {}
}
