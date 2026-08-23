//! Auto-generated integration tests for example
//! DO NOT EDIT - regenerate with `pmstrat transpile`


use pmengine::strategies::Example;
use pmengine::strategy::Strategy;




// No filter tests for non-market-discovery strategies



#[test]
fn test_strategy_instantiation() {
    let strategy = Example::new();
    assert_eq!(strategy.id(), "example");
}
