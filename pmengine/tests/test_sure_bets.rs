//! Auto-generated integration tests for sure_bets
//! DO NOT EDIT - regenerate with `pmstrat transpile`


mod fixtures;

use fixtures::*;
use pmengine::strategies::SureBets;
use pmengine::strategy::Strategy;
use rust_decimal::Decimal;
use rust_decimal_macros::dec;


// Strategy constants (from transpiled strategy)
#[allow(dead_code)]
const MAX_HOURS_TO_EXPIRY: f64 = 48.0;
#[allow(dead_code)]
const MIN_LIQUIDITY: f64 = 500.0;



#[test]
fn test_filters_out_low_liquidity() {
    let mut strategy = SureBets::new();
    let ctx = create_context_with_markets(vec![
        ("token1", dec!(0.68), dec!(0.72), 48.0, 250.0, dec!(0)),
    ]);
    let signals = strategy.on_tick(&ctx);
    let (_, _, _, holds) = count_signal_types(&signals);
    assert_eq!(holds, 1, "Should hold for low liquidity");
}


#[test]
fn test_no_markets() {
    let mut strategy = SureBets::new();
    let ctx = create_context_with_markets(vec![]);
    let signals = strategy.on_tick(&ctx);
    let (_, _, _, holds) = count_signal_types(&signals);
    assert_eq!(holds, 1, "Should hold when no markets");
}


#[test]
fn test_quotes_qualifying_market() {
    let mut strategy = SureBets::new();
    // High certainty market: ask=0.96, expiring in 24h
    let ctx = create_context_with_markets(vec![
        ("token1", dec!(0.94), dec!(0.96), 24.0, 1000.0, dec!(0)),
    ]);
    let signals = strategy.on_tick(&ctx);
    let (_, buys, _, holds) = count_signal_types(&signals);
    assert!(buys >= 1, "Should place buy order");
    assert_eq!(holds, 0, "Should not hold");
}


#[test]
fn test_quotes_multiple_markets() {
    let mut strategy = SureBets::new();
    // Multiple high certainty markets
    let ctx = create_context_with_markets(vec![
        ("token1", dec!(0.94), dec!(0.96), 24.0, 1000.0, dec!(0)),
        ("token2", dec!(0.95), dec!(0.97), 12.0, 2000.0, dec!(0)),
        ("token3", dec!(0.93), dec!(0.95), 36.0, 1500.0, dec!(0)),
    ]);
    let signals = strategy.on_tick(&ctx);
    let (_, buys, _, _) = count_signal_types(&signals);
    assert!(buys >= 1, "Should buy for at least 1 market");
}


#[test]
fn test_strategy_instantiation() {
    let strategy = SureBets::new();
    assert_eq!(strategy.id(), "sure_bets");
}
