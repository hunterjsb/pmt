//! Shared test fixtures for strategy integration tests.
//!
//! These helpers are used by auto-generated strategy tests. DO NOT EDIT.
//! Regenerate tests with `pmstrat transpile --all`.

use pmengine::orderbook::{Level, OrderBook};
use pmengine::position::PositionTracker;
use pmengine::strategy::{MarketInfo, Signal, StrategyContext};
use chrono::{Duration, Utc};
use rust_decimal::Decimal;
use rust_decimal_macros::dec;
use std::collections::HashMap;
use std::sync::Arc;

/// Create an order book with a single bid and ask level.
pub fn create_order_book(token_id: &str, best_bid: Decimal, best_ask: Decimal) -> OrderBook {
    let mut book = OrderBook::new(token_id.to_string());
    book.bids = vec![Level { price: best_bid, size: dec!(100) }];
    book.asks = vec![Level { price: best_ask, size: dec!(100) }];
    book
}

/// Create market info with specified expiry and liquidity.
pub fn create_market_info(token_id: &str, hours_until_expiry: f64, liquidity: f64) -> MarketInfo {
    MarketInfo::with_liquidity(
        format!("Test market {}", token_id),
        "Yes".to_string(),
        format!("test-market-{}", token_id),
        Some(Utc::now() + Duration::hours(hours_until_expiry as i64)),
        Some(liquidity),
    )
}

/// Create a StrategyContext with multiple markets.
///
/// Each tuple contains: (token_id, bid, ask, hours_until_expiry, liquidity, position_size)
pub fn create_context_with_markets(
    markets_data: Vec<(&str, Decimal, Decimal, f64, f64, Decimal)>,
) -> StrategyContext {
    let mut order_books = HashMap::new();
    let mut markets = HashMap::new();
    let mut positions = PositionTracker::new();

    for (token_id, bid, ask, hours, liquidity, position) in markets_data {
        let book = create_order_book(token_id, bid, ask);
        order_books.insert(token_id.to_string(), Arc::new(book));

        let market_info = create_market_info(token_id, hours, liquidity);
        markets.insert(token_id.to_string(), market_info);

        if position != dec!(0) {
            let pos = positions.get_or_create(token_id);
            pos.size = position;
            pos.avg_entry_price = dec!(0.50);
        }
    }

    StrategyContext {
        timestamp: Utc::now(),
        order_books,
        positions,
        markets,
        unrealized_pnl: dec!(0),
        realized_pnl: dec!(0),
        usdc_balance: dec!(10000),
    }
}

/// Count the number of each signal type in a list of signals.
///
/// Returns: (cancels, buys, sells, holds)
pub fn count_signal_types(signals: &[Signal]) -> (usize, usize, usize, usize) {
    let mut cancels = 0;
    let mut buys = 0;
    let mut sells = 0;
    let mut holds = 0;
    for signal in signals {
        match signal {
            Signal::Cancel { .. } => cancels += 1,
            Signal::Buy { .. } => buys += 1,
            Signal::Sell { .. } => sells += 1,
            Signal::Hold => holds += 1,
            _ => {}
        }
    }
    (cancels, buys, sells, holds)
}
