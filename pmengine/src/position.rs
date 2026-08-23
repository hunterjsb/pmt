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

    /// Drop a token's position from the ledger, returning its notional.
    /// For tokens the engine stops managing (e.g. a resolved binary whose
    /// shares redeem on-chain) — a preserved entry would count as live
    /// exposure forever and starve the risk manager.
    pub fn remove(&mut self, token_id: &str) -> Decimal {
        self.positions
            .remove(token_id)
            .map(|p| p.notional())
            .unwrap_or(Decimal::ZERO)
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

    /// Reconcile a position's size + avg entry against the authoritative
    /// on-chain holding (from the data-api). Corrects drift caused by fills
    /// that incremental detection missed — the data-api is ground truth.
    ///
    /// Preserves accumulated `realized_pnl` and `last_price` (those aren't
    /// recoverable from a position snapshot). Returns the size delta applied,
    /// so the caller can log meaningful corrections.
    pub fn reconcile(&mut self, token_id: &str, true_size: Decimal, avg_price: Decimal) -> Decimal {
        let position = self.get_or_create(token_id);
        let delta = true_size - position.size;
        if delta != Decimal::ZERO {
            position.size = true_size;
            // Only adopt the data-api avg when we actually hold something;
            // a flat position has no meaningful entry price.
            if true_size != Decimal::ZERO {
                position.avg_entry_price = avg_price;
            }
            if let Some(p) = position.last_price {
                position.update_price(p);
            }
        }
        delta
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

    #[test]
    fn test_reconcile_corrects_missed_fill() {
        let mut tracker = PositionTracker::new();
        // Engine thinks it holds 50 (seeded at startup).
        tracker.reconcile("t", dec!(50), dec!(0.90));
        assert_eq!(tracker.get("t").unwrap().size, dec!(50));

        // A 5-share sell filled but the trades-poll missed it; data-api now
        // reports 45. Reconcile corrects the drift and returns the delta.
        let delta = tracker.reconcile("t", dec!(45), dec!(0.90));
        assert_eq!(delta, dec!(-5));
        assert_eq!(tracker.get("t").unwrap().size, dec!(45));

        // No change → zero delta, no-op.
        let delta2 = tracker.reconcile("t", dec!(45), dec!(0.90));
        assert_eq!(delta2, dec!(0));
    }

    #[test]
    fn test_reconcile_preserves_realized_pnl() {
        let mut tracker = PositionTracker::new();
        tracker.apply_fill(&Fill {
            order_id: "1".to_string(), token_id: "t".to_string(), is_buy: true,
            price: dec!(0.50), size: dec!(10), timestamp: chrono::Utc::now(), fee: Decimal::ZERO,
        });
        tracker.apply_fill(&Fill {
            order_id: "2".to_string(), token_id: "t".to_string(), is_buy: false,
            price: dec!(0.60), size: dec!(5), timestamp: chrono::Utc::now(), fee: Decimal::ZERO,
        });
        let pnl_before = tracker.get("t").unwrap().realized_pnl;
        assert_eq!(pnl_before, dec!(0.50));

        // Reconcile to a different size — realized P&L must survive.
        tracker.reconcile("t", dec!(8), dec!(0.55));
        assert_eq!(tracker.get("t").unwrap().size, dec!(8));
        assert_eq!(tracker.get("t").unwrap().realized_pnl, dec!(0.50));
    }
}
