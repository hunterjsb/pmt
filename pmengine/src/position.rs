//! Position and P&L tracking.

use rust_decimal::Decimal;
use serde::{Deserialize, Serialize};
use std::collections::HashMap;

/// A single position in a token.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct Position {
    pub token_id: String,
    pub size: Decimal,
    pub avg_entry_price: Decimal,
    pub realized_pnl: Decimal,
    pub unrealized_pnl: Decimal,
    pub last_price: Option<Decimal>,
}

impl Position {
    pub fn new(token_id: String) -> Self {
        Self {
            token_id,
            size: Decimal::ZERO,
            avg_entry_price: Decimal::ZERO,
            realized_pnl: Decimal::ZERO,
            unrealized_pnl: Decimal::ZERO,
            last_price: None,
        }
    }

    /// Apply a fill to this position.
    pub fn apply_fill(&mut self, fill: &Fill) {
        let old_size = self.size;
        let fill_value = fill.price * fill.size;

        if fill.is_buy {
            // Buying: increase position
            if old_size >= Decimal::ZERO {
                // Adding to long position - update average
                let old_value = self.avg_entry_price * old_size;
                let new_size = old_size + fill.size;
                if new_size > Decimal::ZERO {
                    self.avg_entry_price = (old_value + fill_value) / new_size;
                }
                self.size = new_size;
            } else {
                // Covering short position
                let cover_size = fill.size.min(-old_size);
                let new_long = fill.size - cover_size;

                // Realize P&L on covered portion
                self.realized_pnl += cover_size * (self.avg_entry_price - fill.price);

                self.size = old_size + fill.size;
                if new_long > Decimal::ZERO && self.size > Decimal::ZERO {
                    self.avg_entry_price = fill.price;
                }
            }
        } else {
            // Selling: decrease position
            if old_size <= Decimal::ZERO {
                // Adding to short position - update average
                let old_value = self.avg_entry_price * (-old_size);
                let new_size = old_size - fill.size;
                if new_size < Decimal::ZERO {
                    self.avg_entry_price = (old_value + fill_value) / (-new_size);
                }
                self.size = new_size;
            } else {
                // Closing long position
                let close_size = fill.size.min(old_size);
                let new_short = fill.size - close_size;

                // Realize P&L on closed portion
                self.realized_pnl += close_size * (fill.price - self.avg_entry_price);

                self.size = old_size - fill.size;
                if new_short > Decimal::ZERO && self.size < Decimal::ZERO {
                    self.avg_entry_price = fill.price;
                }
            }
        }
    }

    /// Update unrealized P&L with current price.
    pub fn update_price(&mut self, price: Decimal) {
        self.last_price = Some(price);
        if self.size > Decimal::ZERO {
            self.unrealized_pnl = self.size * (price - self.avg_entry_price);
        } else if self.size < Decimal::ZERO {
            self.unrealized_pnl = (-self.size) * (self.avg_entry_price - price);
        } else {
            self.unrealized_pnl = Decimal::ZERO;
        }
    }

    /// Get notional value of position.
    pub fn notional(&self) -> Decimal {
        self.size.abs() * self.last_price.unwrap_or(self.avg_entry_price)
    }
}

/// A fill event from order execution.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct Fill {
    pub order_id: String,
    pub token_id: String,
    pub is_buy: bool,
    pub price: Decimal,
    pub size: Decimal,
    pub timestamp: chrono::DateTime<chrono::Utc>,
    pub fee: Decimal,
}

/// Tracks all positions.
#[derive(Debug, Clone, Default)]
pub struct PositionTracker {
    positions: HashMap<String, Position>,
}

impl PositionTracker {
    pub fn new() -> Self {
        Self {
            positions: HashMap::new(),
        }
    }

    /// Get position for a token, creating if needed.
    pub fn get_or_create(&mut self, token_id: &str) -> &mut Position {
        self.positions
            .entry(token_id.to_string())
            .or_insert_with(|| Position::new(token_id.to_string()))
    }

    /// Get position for a token (read-only).
    pub fn get(&self, token_id: &str) -> Option<&Position> {
        self.positions.get(token_id)
    }

    /// Apply a fill.
    pub fn apply_fill(&mut self, fill: &Fill) {
        let position = self.get_or_create(&fill.token_id);
        position.apply_fill(fill);
        tracing::info!(
            token_id = fill.token_id,
            size = %position.size,
            avg_entry = %position.avg_entry_price,
            realized_pnl = %position.realized_pnl,
            "Position updated"
        );
    }

    /// Update prices for all positions.
    pub fn update_prices(&mut self, prices: &HashMap<String, Decimal>) {
        for (token_id, price) in prices {
            if let Some(position) = self.positions.get_mut(token_id) {
                position.update_price(*price);
            }
        }
    }

    /// Get total realized P&L across all positions.
    pub fn total_realized_pnl(&self) -> Decimal {
        self.positions.values().map(|p| p.realized_pnl).sum()
    }

    /// Get total unrealized P&L across all positions.
    pub fn total_unrealized_pnl(&self) -> Decimal {
        self.positions.values().map(|p| p.unrealized_pnl).sum()
    }

    /// Get total notional exposure.
    pub fn total_notional(&self) -> Decimal {
        self.positions.values().map(|p| p.notional()).sum()
    }

    /// Get all positions with non-zero size.
    pub fn active_positions(&self) -> Vec<&Position> {
        self.positions
            .values()
            .filter(|p| p.size != Decimal::ZERO)
            .collect()
    }

    /// Get all positions.
    pub fn all_positions(&self) -> impl Iterator<Item = &Position> {
        self.positions.values()
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use rust_decimal_macros::dec;

    #[test]
    fn test_position_long() {
        let mut pos = Position::new("token1".to_string());

        // Buy 10 at 0.50
        pos.apply_fill(&Fill {
            order_id: "1".to_string(),
            token_id: "token1".to_string(),
            is_buy: true,
            price: dec!(0.50),
            size: dec!(10),
            timestamp: chrono::Utc::now(),
            fee: Decimal::ZERO,
        });
        assert_eq!(pos.size, dec!(10));
        assert_eq!(pos.avg_entry_price, dec!(0.50));

        // Sell 5 at 0.60 (realize profit)
        pos.apply_fill(&Fill {
            order_id: "2".to_string(),
            token_id: "token1".to_string(),
            is_buy: false,
            price: dec!(0.60),
            size: dec!(5),
            timestamp: chrono::Utc::now(),
            fee: Decimal::ZERO,
        });
        assert_eq!(pos.size, dec!(5));
        assert_eq!(pos.realized_pnl, dec!(0.50)); // 5 * (0.60 - 0.50)
    }
}
