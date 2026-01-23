//! Auto-generated from Python strategy: dynamic_market_maker
//! DO NOT EDIT - regenerate with `pmstrat transpile`

use crate::strategy::{Signal, Strategy, StrategyContext, Urgency};
use crate::position::Fill;
#[allow(unused_imports)]
use rust_decimal::Decimal;
use rust_decimal_macros::dec;

// Strategy parameters (generated from Python params)
const MIN_LIQUIDITY: f64 = 10000.0;
const MIN_PRICE: Decimal = dec!(0.20);
const MAX_PRICE: Decimal = dec!(0.80);
const MIN_SPREAD_PCT: Decimal = dec!(0.02);
const MAX_SPREAD_PCT: Decimal = dec!(0.15);
const MIN_HOURS_TO_EXPIRY: f64 = 24.0;
const MAX_TOKENS: i64 = 5;
const MAX_POSITION: Decimal = dec!(75);
const ORDER_SIZE: Decimal = dec!(10);
const SPREAD_BPS: Decimal = dec!(150);
const SKEW_FACTOR: Decimal = dec!(0.001);
const MIN_EDGE: Decimal = dec!(0.005);

pub struct DynamicMarketMaker {
    id: String,
    tokens: Vec<String>,
}

impl DynamicMarketMaker {
    pub fn new() -> Self {
        Self {
            id: "dynamic_market_maker".to_string(),
            tokens: vec![],
        }
    }
}

impl Default for DynamicMarketMaker {
    fn default() -> Self {
        Self::new()
    }
}

impl Strategy for DynamicMarketMaker {
    fn id(&self) -> &str {
        &self.id
    }

    fn subscriptions(&self) -> Vec<String> {
        self.tokens.clone()
    }

    fn on_tick(&mut self, ctx: &StrategyContext) -> Vec<Signal> {
        let mut signals = vec![];
        let mut tokens_quoted = 0;
        for (token_id, market) in ctx.markets.iter() {
            if tokens_quoted >= MAX_TOKENS {
                break;
            }
            let liquidity = match market.liquidity {
                Some(v) => v,
                None => continue,
            };
            if liquidity < MIN_LIQUIDITY {
                continue;
            }
            let hours_left = match market.hours_until_expiry {
                Some(v) => v,
                None => continue,
            };
            if hours_left < MIN_HOURS_TO_EXPIRY {
                continue;
            }
            let book = match ctx.order_books.get(token_id) {
                Some(v) => v,
                None => continue,
            };
            let bid = match book.best_bid() {
                Some(v) => v.price,
                None => continue,
            };
            let ask = match book.best_ask() {
                Some(v) => v.price,
                None => continue,
            };
            let mid = (bid + ask) / dec!(2);
            if mid < MIN_PRICE {
                continue;
            }
            if mid > MAX_PRICE {
                continue;
            }
            let market_spread = ask - bid;
            let spread_pct = market_spread / mid;
            if spread_pct < MIN_SPREAD_PCT {
                continue;
            }
            if spread_pct > MAX_SPREAD_PCT {
                continue;
            }
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
                continue;
            }
            if my_bid < dec!(0.01) {
                my_bid = dec!(0.01);
            }
            if my_ask > dec!(0.99) {
                my_ask = dec!(0.99);
            }
            let can_buy = position_size < MAX_POSITION;
            let neg_max_position = dec!(0) - MAX_POSITION;
            let can_sell = position_size > neg_max_position;
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
            signals.push(Signal::Cancel { token_id: token_id.to_string() });
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
            tokens_quoted = tokens_quoted + 1;
        }
        return if !signals.is_empty() { signals } else { vec![Signal::Hold] };
    }

    fn on_fill(&mut self, _fill: &Fill) {}
    fn on_shutdown(&mut self) {}
}
