//! Auto-generated integration tests for momentum_fade
//! DO NOT EDIT - regenerate with `pmstrat transpile`


use pmengine::strategies::MomentumFade;
use pmengine::strategy::Strategy;




// No filter tests for non-market-discovery strategies



#[test]
fn test_strategy_instantiation() {
    let strategy = MomentumFade::new();
    assert_eq!(strategy.id(), "momentum_fade");
}
