//! Auto-generated integration tests for dynamic_market_maker
//! DO NOT EDIT - regenerate with `pmstrat transpile`


mod fixtures;

use fixtures::*;
use pmengine::strategies::DynamicMarketMaker;
use pmengine::strategy::Strategy;
use rust_decimal::Decimal;
use rust_decimal_macros::dec;


// Strategy constants (from transpiled strategy)
#[allow(dead_code)]
const MIN_LIQUIDITY: f64 = 10000.0;
#[allow(dead_code)]
const MIN_PRICE: Decimal = dec!(0.20);
#[allow(dead_code)]
const MAX_PRICE: Decimal = dec!(0.80);
#[allow(dead_code)]
const MIN_SPREAD_PCT: Decimal = dec!(0.02);
#[allow(dead_code)]
const MAX_SPREAD_PCT: Decimal = dec!(0.15);
#[allow(dead_code)]
const MIN_HOURS_TO_EXPIRY: f64 = 24.0;
#[allow(dead_code)]
const MAX_TOKENS: i64 = 5;
#[allow(dead_code)]
const MAX_POSITION: Decimal = dec!(75);
#[allow(dead_code)]
const ORDER_SIZE: Decimal = dec!(10);
#[allow(dead_code)]
const SPREAD_BPS: Decimal = dec!(150);
#[allow(dead_code)]
const SKEW_FACTOR: Decimal = dec!(0.001);
#[allow(dead_code)]
const MIN_EDGE: Decimal = dec!(0.005);



#[test]
fn test_filters_out_low_liquidity() {
    let mut strategy = DynamicMarketMaker::new();
    let ctx = create_context_with_markets(vec![
        ("token1", dec!(0.68), dec!(0.72), 48.0, 5000.0, dec!(0)),
    ]);
    let signals = strategy.on_tick(&ctx);
    let (_, _, _, holds) = count_signal_types(&signals);
    assert_eq!(holds, 1, "Should hold for low liquidity");
}


#[test]
fn test_filters_out_low_price() {
    let mut strategy = DynamicMarketMaker::new();
    let ctx = create_context_with_markets(vec![
        ("token1", dec!(0.10), dec!(0.15), 48.0, 50000.0, dec!(0)),
    ]);
    let signals = strategy.on_tick(&ctx);
    let (_, _, _, holds) = count_signal_types(&signals);
    assert_eq!(holds, 1, "Should hold for low price");
}


#[test]
fn test_filters_out_high_price() {
    let mut strategy = DynamicMarketMaker::new();
    let ctx = create_context_with_markets(vec![
        ("token1", dec!(0.85), dec!(0.90), 48.0, 50000.0, dec!(0)),
    ]);
    let signals = strategy.on_tick(&ctx);
    let (_, _, _, holds) = count_signal_types(&signals);
    assert_eq!(holds, 1, "Should hold for high price");
}


#[test]
fn test_filters_out_near_expiry() {
    let mut strategy = DynamicMarketMaker::new();
    let ctx = create_context_with_markets(vec![
        ("token1", dec!(0.68), dec!(0.72), 12.0, 50000.0, dec!(0)),
    ]);
    let signals = strategy.on_tick(&ctx);
    let (_, _, _, holds) = count_signal_types(&signals);
    assert_eq!(holds, 1, "Should hold for near expiry");
}


#[test]
fn test_no_markets() {
    let mut strategy = DynamicMarketMaker::new();
    let ctx = create_context_with_markets(vec![]);
    let signals = strategy.on_tick(&ctx);
    let (_, _, _, holds) = count_signal_types(&signals);
    assert_eq!(holds, 1, "Should hold when no markets");
}


#[test]
fn test_quotes_qualifying_market() {
    let mut strategy = DynamicMarketMaker::new();
    let ctx = create_context_with_markets(vec![
        ("token1", dec!(0.68), dec!(0.72), 48.0, 50000.0, dec!(0)),
    ]);
    let signals = strategy.on_tick(&ctx);
    let (cancels, buys, sells, holds) = count_signal_types(&signals);
    assert_eq!(cancels, 1, "Should cancel existing orders");
    assert_eq!(buys, 1, "Should place buy order");
    assert_eq!(sells, 1, "Should place sell order");
    assert_eq!(holds, 0, "Should not hold");
}


#[test]
fn test_max_position_only_sells() {
    let mut strategy = DynamicMarketMaker::new();
    let ctx = create_context_with_markets(vec![
        ("token1", dec!(0.68), dec!(0.72), 48.0, 50000.0, dec!(75)),
    ]);
    let signals = strategy.on_tick(&ctx);
    let (cancels, buys, sells, _) = count_signal_types(&signals);
    assert_eq!(cancels, 1, "Should cancel");
    assert_eq!(buys, 0, "Should not buy at max position");
    assert_eq!(sells, 1, "Should still sell");
}

#[test]
fn test_max_short_position_only_buys() {
    let mut strategy = DynamicMarketMaker::new();
    let ctx = create_context_with_markets(vec![
        ("token1", dec!(0.68), dec!(0.72), 48.0, 50000.0, dec!(-75)),
    ]);
    let signals = strategy.on_tick(&ctx);
    let (cancels, buys, sells, _) = count_signal_types(&signals);
    assert_eq!(cancels, 1, "Should cancel");
    assert_eq!(buys, 1, "Should still buy");
    assert_eq!(sells, 0, "Should not sell at max short");
}


#[test]
fn test_quotes_multiple_markets() {
    let mut strategy = DynamicMarketMaker::new();
    let ctx = create_context_with_markets(vec![
        ("token1", dec!(0.68), dec!(0.72), 48.0, 50000.0, dec!(0)),
        ("token2", dec!(0.73), dec!(0.77), 72.0, 30000.0, dec!(0)),
        ("token3", dec!(0.66), dec!(0.70), 96.0, 40000.0, dec!(0)),
    ]);
    let signals = strategy.on_tick(&ctx);
    let (cancels, buys, sells, _) = count_signal_types(&signals);
    assert!(cancels >= 1, "Should cancel for at least 1 market");
    assert!(buys >= 1, "Should buy for at least 1 market");
    assert!(sells >= 1, "Should sell for at least 1 market");
}


#[test]
fn test_strategy_instantiation() {
    let strategy = DynamicMarketMaker::new();
    assert_eq!(strategy.id(), "dynamic_market_maker");
}
