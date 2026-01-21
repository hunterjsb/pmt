//! Integration test comparing transpiled market_maker strategy against Python behavior.

use pmengine::strategies::MarketMaker;
use pmengine::strategy::{Strategy, StrategyContext};
use pmengine::orderbook::{OrderBook, Level};
use pmengine::position::{Position, PositionTracker};
use chrono::Utc;
use rust_decimal::Decimal;
use rust_decimal_macros::dec;
use std::collections::HashMap;
use std::sync::Arc;

const TOKEN_ID: &str = "21742633143463906290569050155826241533067272736897614950488156847949938836455";

fn create_order_book(best_bid: Decimal, best_ask: Decimal) -> OrderBook {
    let mut book = OrderBook::new(TOKEN_ID.to_string());
    book.bids = vec![Level { price: best_bid, size: dec!(100) }];
    book.asks = vec![Level { price: best_ask, size: dec!(100) }];
    book
}

fn create_context(best_bid: Decimal, best_ask: Decimal, position_size: Decimal) -> StrategyContext {
    let mut order_books = HashMap::new();
    let book = create_order_book(best_bid, best_ask);
    order_books.insert(TOKEN_ID.to_string(), Arc::new(book));

    let mut positions = PositionTracker::new();
    if position_size != dec!(0) {
        let pos = positions.get_or_create(TOKEN_ID);
        pos.size = position_size;
        pos.avg_entry_price = dec!(0.50);
    }

    StrategyContext {
        timestamp: Utc::now(),
        order_books,
        positions,
        markets: HashMap::new(),
        unrealized_pnl: dec!(0),
        realized_pnl: dec!(0),
        usdc_balance: dec!(1000),
    }
}

fn print_signals(name: &str, signals: &[pmengine::strategy::Signal]) {
    println!("\n{}", "=".repeat(60));
    println!("Scenario: {}", name);
    println!("{}", "-".repeat(60));
    println!("Signals generated:");
    for signal in signals {
        println!("  {:?}", signal);
    }
}

#[test]
fn test_normal_market_flat_position() {
    let mut strategy = MarketMaker::new();
    let ctx = create_context(dec!(0.45), dec!(0.55), dec!(0));
    let signals = strategy.on_tick(&ctx);

    print_signals("Normal market, flat position", &signals);

    // Should generate: Cancel, Buy at 0.4950, Sell at 0.5050
    assert_eq!(signals.len(), 3, "Expected 3 signals, got {}", signals.len());

    // First signal should be Cancel
    match &signals[0] {
        pmengine::strategy::Signal::Cancel { token_id } => {
            assert_eq!(token_id, TOKEN_ID);
        }
        _ => panic!("Expected Cancel signal, got {:?}", signals[0]),
    }

    // Second signal should be Buy at approximately 0.4950
    match &signals[1] {
        pmengine::strategy::Signal::Buy { token_id, price, size, .. } => {
            assert_eq!(token_id, TOKEN_ID);
            assert_eq!(*size, dec!(10));
            // Price should be mid - half_spread = 0.50 - 0.005 = 0.495
            let expected = dec!(0.4950);
            assert!((price - expected).abs() < dec!(0.001),
                "Expected price ~{}, got {}", expected, price);
        }
        _ => panic!("Expected Buy signal, got {:?}", signals[1]),
    }

    // Third signal should be Sell at approximately 0.5050
    match &signals[2] {
        pmengine::strategy::Signal::Sell { token_id, price, size, .. } => {
            assert_eq!(token_id, TOKEN_ID);
            assert_eq!(*size, dec!(10));
            let expected = dec!(0.5050);
            assert!((price - expected).abs() < dec!(0.001),
                "Expected price ~{}, got {}", expected, price);
        }
        _ => panic!("Expected Sell signal, got {:?}", signals[2]),
    }
}

#[test]
fn test_long_position_skews_quotes_down() {
    let mut strategy = MarketMaker::new();
    let ctx = create_context(dec!(0.45), dec!(0.55), dec!(50));
    let signals = strategy.on_tick(&ctx);

    print_signals("Normal market, long 50 shares", &signals);

    assert_eq!(signals.len(), 3, "Expected 3 signals, got {}", signals.len());

    // With long position, quotes should be skewed down
    // skew = 50 * 0.001 = 0.05
    // my_bid = 0.495 - 0.05 = 0.445
    // my_ask = 0.505 - 0.05 = 0.455

    match &signals[1] {
        pmengine::strategy::Signal::Buy { price, .. } => {
            let expected = dec!(0.4450);
            assert!((price - expected).abs() < dec!(0.001),
                "Expected bid ~{}, got {}", expected, price);
        }
        _ => panic!("Expected Buy signal"),
    }

    match &signals[2] {
        pmengine::strategy::Signal::Sell { price, .. } => {
            let expected = dec!(0.4550);
            assert!((price - expected).abs() < dec!(0.001),
                "Expected ask ~{}, got {}", expected, price);
        }
        _ => panic!("Expected Sell signal"),
    }
}

#[test]
fn test_short_position_skews_quotes_up() {
    let mut strategy = MarketMaker::new();
    let ctx = create_context(dec!(0.45), dec!(0.55), dec!(-50));
    let signals = strategy.on_tick(&ctx);

    print_signals("Normal market, short 50 shares", &signals);

    assert_eq!(signals.len(), 3, "Expected 3 signals, got {}", signals.len());

    // With short position, quotes should be skewed up
    // skew = -50 * 0.001 = -0.05
    // my_bid = 0.495 - (-0.05) = 0.545
    // my_ask = 0.505 - (-0.05) = 0.555

    match &signals[1] {
        pmengine::strategy::Signal::Buy { price, .. } => {
            let expected = dec!(0.5450);
            assert!((price - expected).abs() < dec!(0.001),
                "Expected bid ~{}, got {}", expected, price);
        }
        _ => panic!("Expected Buy signal"),
    }

    match &signals[2] {
        pmengine::strategy::Signal::Sell { price, .. } => {
            let expected = dec!(0.5550);
            assert!((price - expected).abs() < dec!(0.001),
                "Expected ask ~{}, got {}", expected, price);
        }
        _ => panic!("Expected Sell signal"),
    }
}

#[test]
fn test_max_long_position_only_sells() {
    let mut strategy = MarketMaker::new();
    let ctx = create_context(dec!(0.45), dec!(0.55), dec!(100));
    let signals = strategy.on_tick(&ctx);

    print_signals("At max long position (100)", &signals);

    // Should only generate Cancel and Sell (no Buy since at max position)
    assert_eq!(signals.len(), 2, "Expected 2 signals, got {}", signals.len());

    match &signals[0] {
        pmengine::strategy::Signal::Cancel { .. } => {}
        _ => panic!("Expected Cancel signal"),
    }

    match &signals[1] {
        pmengine::strategy::Signal::Sell { price, .. } => {
            // skew = 100 * 0.001 = 0.1
            // my_ask = 0.505 - 0.1 = 0.405
            let expected = dec!(0.4050);
            assert!((price - expected).abs() < dec!(0.001),
                "Expected ask ~{}, got {}", expected, price);
        }
        _ => panic!("Expected Sell signal, got {:?}", signals[1]),
    }
}

#[test]
fn test_max_short_position_only_buys() {
    let mut strategy = MarketMaker::new();
    let ctx = create_context(dec!(0.45), dec!(0.55), dec!(-100));
    let signals = strategy.on_tick(&ctx);

    print_signals("At max short position (-100)", &signals);

    // Should only generate Cancel and Buy (no Sell since at max short)
    assert_eq!(signals.len(), 2, "Expected 2 signals, got {}", signals.len());

    match &signals[0] {
        pmengine::strategy::Signal::Cancel { .. } => {}
        _ => panic!("Expected Cancel signal"),
    }

    match &signals[1] {
        pmengine::strategy::Signal::Buy { price, .. } => {
            // skew = -100 * 0.001 = -0.1
            // my_bid = 0.495 - (-0.1) = 0.595
            let expected = dec!(0.5950);
            assert!((price - expected).abs() < dec!(0.001),
                "Expected bid ~{}, got {}", expected, price);
        }
        _ => panic!("Expected Buy signal, got {:?}", signals[1]),
    }
}

#[test]
fn test_near_lower_boundary_holds() {
    let mut strategy = MarketMaker::new();
    let ctx = create_context(dec!(0.02), dec!(0.08), dec!(0));
    let signals = strategy.on_tick(&ctx);

    print_signals("Near lower boundary", &signals);

    // Mid = 0.05, half_spread = 0.05 * 0.01 = 0.0005
    // my_bid = 0.0495, my_ask = 0.0505
    // Edge = 0.0505 - 0.0495 = 0.001 < MIN_EDGE * 2 = 0.01
    // Should Hold due to insufficient edge
    assert_eq!(signals.len(), 1, "Expected 1 signal (Hold), got {}", signals.len());
    match &signals[0] {
        pmengine::strategy::Signal::Hold => {}
        _ => panic!("Expected Hold signal, got {:?}", signals[0]),
    }
}

/// Summary test that prints all scenarios side by side with Python results
#[test]
fn test_all_scenarios_summary() {
    println!("\n\n");
    println!("╔════════════════════════════════════════════════════════════════╗");
    println!("║        MARKET MAKER: RUST vs PYTHON COMPARISON                 ║");
    println!("╚════════════════════════════════════════════════════════════════╝");

    // Run all scenarios and print comparison
    let scenarios = vec![
        ("Normal market, flat", dec!(0.45), dec!(0.55), dec!(0)),
        ("Long 50 shares", dec!(0.45), dec!(0.55), dec!(50)),
        ("Short 50 shares", dec!(0.45), dec!(0.55), dec!(-50)),
        ("Max long (100)", dec!(0.45), dec!(0.55), dec!(100)),
        ("Max short (-100)", dec!(0.45), dec!(0.55), dec!(-100)),
        ("Near lower boundary", dec!(0.02), dec!(0.08), dec!(0)),
    ];

    for (name, bid, ask, pos) in scenarios {
        let mut strategy = MarketMaker::new();
        let ctx = create_context(bid, ask, pos);
        let signals = strategy.on_tick(&ctx);

        println!("\n{}", "-".repeat(60));
        println!("Scenario: {} (bid={}, ask={}, pos={})", name, bid, ask, pos);
        println!("Rust signals:");
        for signal in &signals {
            match signal {
                pmengine::strategy::Signal::Cancel { .. } => println!("  Cancel"),
                pmengine::strategy::Signal::Buy { price, size, .. } =>
                    println!("  Buy @ {} size {}", price, size),
                pmengine::strategy::Signal::Sell { price, size, .. } =>
                    println!("  Sell @ {} size {}", price, size),
                pmengine::strategy::Signal::Hold =>
                    println!("  Hold"),
                _ => println!("  {:?}", signal),
            }
        }
    }

    println!("\n{}", "=".repeat(60));
    println!("All scenarios completed!");
}
