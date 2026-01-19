//! Sure Bets Strategy - Low-risk betting on high-certainty expiring markets.
//!
//! Strategy:
//!     - Find markets priced at 95%+ that are expiring within 2 hours
//!     - Buy the high-certainty outcome
//!     - Wait for resolution, collect 1-5% profit
//!
//! Risk profile:
//!     - Very low: Only bet on near-certain outcomes
//!     - Main risk: Market doesn't resolve as expected (rare at 95%+)
//!     - Expected win rate: 95%+

use crate::position::Fill;
use crate::strategy::{Signal, Strategy, StrategyContext, Urgency};
use rust_decimal::Decimal;
use rust_decimal_macros::dec;

/// Strategy parameters
const MIN_CERTAINTY: Decimal = dec!(0.95); // 95% minimum price
const MAX_HOURS_TO_EXPIRY: f64 = 2.0; // Only markets expiring within 2 hours
const MAX_POSITION_SIZE: Decimal = dec!(100); // Max shares per position
const MIN_EXPECTED_RETURN: Decimal = dec!(0.01); // 1% minimum expected return
const MIN_ORDER_SIZE: Decimal = dec!(10); // Minimum order size
const MAX_SINGLE_ORDER: Decimal = dec!(50); // Cap single order at 50

pub struct SureBets {
    id: String,
}

impl SureBets {
    pub fn new() -> Self {
        Self {
            id: "sure_bets".to_string(),
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
        // Dynamic subscriptions - managed by engine's market discovery
        vec![]
    }

    fn on_tick(&mut self, ctx: &StrategyContext) -> Vec<Signal> {
        let mut signals = Vec::new();

        // Iterate over all discovered markets
        for (token_id, market) in ctx.markets.iter() {
            // Skip if no end date
            if market.end_date.is_none() {
                continue;
            }

            // Check if expiring soon
            let hours_left = match market.hours_until_expiry {
                Some(h) if h > 0.0 && h <= MAX_HOURS_TO_EXPIRY => h,
                _ => continue,
            };

            // Get order book
            let book = match ctx.order_books.get(token_id) {
                Some(b) => b,
                None => continue,
            };

            // Check if high certainty (ask price >= 95%)
            let ask = match book.best_ask() {
                Some(level) => level,
                None => continue,
            };

            let ask_price = ask.price;
            if ask_price < MIN_CERTAINTY {
                continue;
            }

            // Calculate expected return
            // If we buy at ask and it resolves to 1.00, profit = (1.00 - ask) / ask
            let expected_return = (dec!(1.00) - ask_price) / ask_price;
            if expected_return < MIN_EXPECTED_RETURN {
                continue;
            }

            // Check current position
            let current_size = ctx
                .positions
                .get(token_id)
                .map(|p| p.size)
                .unwrap_or(Decimal::ZERO);

            // Don't exceed max position
            if current_size >= MAX_POSITION_SIZE {
                continue;
            }

            // Calculate how much more we can buy
            let remaining = MAX_POSITION_SIZE - current_size;
            let ask_size = ask.size;
            let size = remaining.min(ask_size).min(MAX_SINGLE_ORDER);

            if size < MIN_ORDER_SIZE {
                continue;
            }

            tracing::info!(
                token_id = token_id.as_str(),
                question = market.question.as_str(),
                outcome = market.outcome.as_str(),
                ask_price = %ask_price,
                size = %size,
                hours_left = format!("{:.2}", hours_left).as_str(),
                expected_return_pct = format!("{:.2}", expected_return * dec!(100)).as_str(),
                "Generating sure bet signal"
            );

            // Generate buy signal
            signals.push(Signal::Buy {
                token_id: token_id.clone(),
                price: ask_price,
                size,
                urgency: Urgency::Medium,
            });
        }

        if signals.is_empty() {
            vec![Signal::Hold]
        } else {
            signals
        }
    }

    fn on_fill(&mut self, fill: &Fill) {
        tracing::info!(
            order_id = fill.order_id.as_str(),
            token_id = fill.token_id.as_str(),
            price = %fill.price,
            size = %fill.size,
            "Sure bet fill received"
        );
    }

    fn on_shutdown(&mut self) {
        tracing::info!("Sure bets strategy shutting down");
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_sure_bets_creation() {
        let strategy = SureBets::new();
        assert_eq!(strategy.id(), "sure_bets");
        assert!(strategy.subscriptions().is_empty());
    }
}
