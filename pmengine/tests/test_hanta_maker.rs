//! Auto-generated integration tests for hanta_maker
//! DO NOT EDIT - regenerate with `pmstrat transpile`


use pmengine::strategies::HantaMaker;
use pmengine::strategy::Strategy;




// No filter tests for non-market-discovery strategies



#[test]
fn test_strategy_instantiation() {
    let strategy = HantaMaker::new();
    assert_eq!(strategy.id(), "hanta_maker");
}
