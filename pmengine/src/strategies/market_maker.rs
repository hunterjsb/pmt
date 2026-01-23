//! Auto-generated from Python strategy: market_maker
//! DO NOT EDIT - regenerate with `pmstrat transpile`

use crate::strategy::{Signal, Strategy, StrategyContext, Urgency};
use crate::position::Fill;
#[allow(unused_imports)]
use rust_decimal::Decimal;
use rust_decimal_macros::dec;

// Strategy parameters (generated from Python params)
const TOKEN_ID: &str = "21742633143463906290569050155826241533067272736897614950488156847949938836455";
const SPREAD_BPS: Decimal = dec!(200);
const SKEW_FACTOR: Decimal = dec!(0.001);
const MAX_POSITION: Decimal = dec!(100);
const ORDER_SIZE: Decimal = dec!(10);
const MIN_EDGE: Decimal = dec!(0.005);

pub struct MarketMaker {
    id: String,
    tokens: Vec<String>,
}

impl MarketMaker {
    pub fn new() -> Self {
        Self {
            id: "market_maker".to_string(),
            tokens: vec!["21742633143463906290569050155826241533067272736897614950488156847949938836455".to_string()],
        }
    }
}

impl Default for MarketMaker {
    fn default() -> Self {
        Self::new()
    }
}

impl Strategy for MarketMaker {
    fn id(&self) -> &str {
        &self.id
    }

    fn subscriptions(&self) -> Vec<String> {
        self.tokens.clone()
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
        let position = ctx.positions.get(token_id);
        let mut position_size = dec!(0);
        if let Some(position) = position {
            position_size = position.size;
        }
        let half_spread_pct = SPREAD_BPS / dec!(20000);
        let half_spread = mid * half_spread_pct;
        let skew = position_size * SKEW_FACTOR;
        let mut my_bid = (mid - half_spread) - skew;
        let mut my_ask = (mid + half_spread) - skew;
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
        let can_buy = position_size < MAX_POSITION;
        let can_sell = position_size > -MAX_POSITION;
        let mut buy_size = ORDER_SIZE;
        let remaining_buy = MAX_POSITION - position_size;
        if remaining_buy < buy_size {
            buy_size = remaining_buy;
        }
        let mut sell_size = ORDER_SIZE;
        let remaining_sell = MAX_POSITION + position_size;
        if remaining_sell < sell_size {
            sell_size = remaining_sell;
        }
        if can_buy {
            if buy_size > dec!(0) {
                signals.push(Signal::Buy { token_id: token_id.to_string(), price: my_bid, size: buy_size, urgency: Urgency::Low });
            }
        }
        if can_sell {
            if sell_size > dec!(0) {
                signals.push(Signal::Sell { token_id: token_id.to_string(), price: my_ask, size: sell_size, urgency: Urgency::Low });
            }
        }
        return signals;
    }

    fn on_fill(&mut self, _fill: &Fill) {}
    fn on_shutdown(&mut self) {}
}
