//! Auto-generated from Python strategy: sure_bets
//! DO NOT EDIT - regenerate with `pmstrat transpile`

use crate::strategy::{Signal, Strategy, StrategyContext, Urgency};
use crate::position::Fill;
#[allow(unused_imports)]
use rust_decimal::Decimal;
use rust_decimal_macros::dec;

// Strategy parameters (generated from Python params)
const MIN_CERTAINTY: Decimal = dec!(0.95);
const MAX_CERTAINTY: Decimal = dec!(0.99);
const MAX_HOURS_TO_EXPIRY: f64 = 48.0;
const MIN_LIQUIDITY: f64 = 500.0;
const MAX_POSITION_SIZE: Decimal = dec!(100);
const MIN_ORDER_SIZE: Decimal = dec!(10);
const MAX_SINGLE_ORDER: Decimal = dec!(50);
const MIN_EXPECTED_RETURN: Decimal = dec!(0.01);
const EXCLUDE_KEYWORDS: &[&str] = &["dota", "counter-strike", "valorant", "league of legends", "overwatch", "csgo", "cs2", "lol", "pubg", "fortnite", "rocket league", "starcraft", "kill handicap", "map handicap", "game handicap", "games total", "bo3", "bo5", "esports", "e-sports", " vs ", " vs. ", " fc", " afc", " cf", "united fc", "city fc", "o/u 2.5", "o/u 3.5", "o/u 4.5", "o/u 1.5", "o/u 0.5", "over/under", "over 0.5", "over 1.5", "over 2.5", "over 3.5", "over 4.5", "under 0.5", "under 1.5", "under 2.5", "under 3.5", "under 4.5", "premier league", "epl", "champions league", "la liga", "bundesliga", "serie a", "ligue 1", "eredivisie", "championship", "league one", "league two", "copa america", "euros", "euro 2024", "euro 2025", "world cup", "nfl", "nba", "mlb", "nhl", "mls", "ufc", "wwe", "ncaa", "super bowl", "stanley cup", "world series", "fifa", "olympics", "tennis", "golf", "boxing", "mma", "f1", "nascar", "cricket", "rugby", "atp", "wta", "pga"];

pub struct SureBets {
    id: String,
    tokens: Vec<String>,
}

impl SureBets {
    pub fn new() -> Self {
        Self {
            id: "sure_bets".to_string(),
            tokens: vec![],
        }
    }
}

impl Default for SureBets {
    fn default() -> Self {
        Self::new()
    }
}

impl Strategy for SureBets {
    fn id(&self) -> &str {
        &self.id
    }

    fn subscriptions(&self) -> Vec<String> {
        self.tokens.clone()
    }

    fn on_tick(&mut self, ctx: &StrategyContext) -> Vec<Signal> {
        let mut signals = vec![];
        for (token_id, market) in ctx.markets.iter() {
            let q_lower = market.question.clone().to_lowercase();
            let mut excluded = false;
            for keyword in EXCLUDE_KEYWORDS {
                if q_lower.contains(keyword) {
                    excluded = true;
                }
            }
            if excluded {
                continue;
            }
            let liquidity = market.liquidity;
            if let Some(liquidity) = liquidity {
                if liquidity < MIN_LIQUIDITY {
                    continue;
                }
            }
            if market.end_date.is_none() {
                continue;
            }
            let hours_left = match market.hours_until_expiry {
                Some(v) => v,
                None => continue,
            };
            if hours_left < 0.0 {
                continue;
            }
            if hours_left > MAX_HOURS_TO_EXPIRY {
                continue;
            }
            let book = match ctx.order_books.get(token_id) {
                Some(v) => v,
                None => continue,
            };
            let ask_price = match book.best_ask() {
                Some(v) => v.price,
                None => continue,
            };
            if ask_price < MIN_CERTAINTY {
                continue;
            }
            if ask_price > MAX_CERTAINTY {
                continue;
            }
            let expected_return = (dec!(1.00) - ask_price) / ask_price;
            if expected_return < MIN_EXPECTED_RETURN {
                continue;
            }
            let position = ctx.positions.get(token_id);
            let mut current_size = dec!(0);
            if let Some(position) = position {
                current_size = position.size;
            }
            if current_size >= MAX_POSITION_SIZE {
                continue;
            }
            let remaining = MAX_POSITION_SIZE - current_size;
            let ask_size = book.ask_size();
            let mut size = remaining;
            if ask_size < size {
                size = ask_size;
            }
            if MAX_SINGLE_ORDER < size {
                size = MAX_SINGLE_ORDER;
            }
            if size < MIN_ORDER_SIZE {
                continue;
            }
            signals.push(Signal::Buy { token_id: token_id.to_string(), price: ask_price, size: size, urgency: Urgency::Medium });
        }
        return if !signals.is_empty() { signals } else { vec![Signal::Hold] };
    }

    fn on_fill(&mut self, _fill: &Fill) {}
    fn on_shutdown(&mut self) {}
}
