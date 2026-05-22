//! Auto-generated integration tests for momentum_fade
//! DO NOT EDIT - regenerate with `pmstrat transpile`


mod fixtures;

use fixtures::*;
use pmengine::strategies::MomentumFade;
use pmengine::strategy::Strategy;
use rust_decimal::Decimal;
use rust_decimal_macros::dec;


// Strategy constants (from transpiled strategy)



#[test]
fn test_no_markets() {
    let mut strategy = MomentumFade::new();
    let ctx = create_context_with_markets(vec![]);
    let signals = strategy.on_tick(&ctx);
    let (_, _, _, holds) = count_signal_types(&signals);
    assert_eq!(holds, 1, "Should hold when no markets");
}


#[test]
fn test_quotes_qualifying_market() {
    let mut strategy = MomentumFade::new();
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
fn test_quotes_multiple_markets() {
    let mut strategy = MomentumFade::new();
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
    let strategy = MomentumFade::new();
    assert_eq!(strategy.id(), "momentum_fade");
}
