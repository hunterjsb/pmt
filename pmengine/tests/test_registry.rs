//! The strategy registry across BOTH build flavors of the private split.
//!
//! Deliberately key-only: `registry()` entries are lazy factories, and
//! `Updown::new()` reads (and would rewrite) the live `~/.pmt/engine`
//! arm state, so a test must never call one.
//!
//! Public flavor (no pmt-strategies submodule): exactly `["example"]` —
//! proving the public artifact carries no private strategy. Private flavor:
//! exactly `["example", "updown", "updown2"]` — the example is a permanent
//! canary for the strategy plumbing, `updown` the live strategy, `updown2`
//! the Strategy 2.0 pricer, registered but SHADOW by default (it places
//! nothing until an operator arms it live with a second key).

use pmengine::engine::EngineError;
use pmengine::strategies::registry;

/// Every strategy deleted in the 2026-08 dead-strategy cleanup, plus their
/// DSL sources in pmstrat. A name reappearing here means a
/// `pmstrat transpile --all` resurrected it — check that the Python source
/// really is gone, not just the Rust.
const REMOVED: &[&str] = &[
    "alert_test",
    "dynamic_market_maker",
    "hanta_maker",
    "market_maker",
    "momentum_fade",
    "order_test",
    "spread_watcher",
    "sure_bets",
];

#[test]
fn registry_carries_example_in_every_flavor() {
    // The inert in-tree strategy is the proof the transpile -> registry ->
    // engine plumbing works in a clone with no submodule access.
    let reg = registry();
    assert!(
        reg.contains_key("example"),
        "example is registered unconditionally — registry has {:?}",
        reg.keys().collect::<Vec<_>>()
    );
}

#[cfg(private_strategies)]
#[test]
fn private_registry_is_exactly_the_surviving_set() {
    let reg = registry();
    let mut names: Vec<&str> = reg.keys().copied().collect();
    names.sort_unstable();
    assert_eq!(names, vec!["example", "updown", "updown2"]);
}

#[cfg(private_strategies)]
#[test]
fn private_registry_carries_updown() {
    let reg = registry();
    assert!(
        reg.contains_key("updown"),
        "updown is the live strategy — registry has {:?}",
        reg.keys().collect::<Vec<_>>()
    );
}

#[cfg(private_strategies)]
#[test]
fn private_registry_carries_updown2() {
    let reg = registry();
    assert!(
        reg.contains_key("updown2"),
        "updown2 is the Strategy 2.0 shadow — registry has {:?}",
        reg.keys().collect::<Vec<_>>()
    );
}

#[cfg(not(private_strategies))]
#[test]
fn public_registry_is_exactly_example() {
    let reg = registry();
    let mut names: Vec<&str> = reg.keys().copied().collect();
    names.sort_unstable();
    assert_eq!(names, vec!["example"]);
}

#[cfg(not(private_strategies))]
#[test]
fn public_artifact_carries_no_private_strategy() {
    // The whole point of the split: a public build must not even KNOW the
    // private strategy's name.
    let reg = registry();
    for name in ["updown", "updown2"] {
        assert!(!reg.contains_key(name), "{name} leaked into the public registry");
    }
}

#[test]
fn removed_strategies_are_gone_from_the_registry() {
    let reg = registry();
    for name in REMOVED {
        assert!(
            !reg.contains_key(name),
            "{name} was deleted but is registered again"
        );
    }
}

#[test]
fn a_removed_name_reads_as_an_unknown_strategy() {
    // What `pmengine run sure_bets` now produces: the lookup misses, and
    // load_strategies turns that miss into UnknownStrategy naming the
    // strategy the operator typed.
    let reg = registry();
    for name in REMOVED {
        assert!(!reg.contains_key(name));
        let err = EngineError::UnknownStrategy((*name).to_string());
        assert_eq!(err.to_string(), format!("Unknown strategy: {name}"));
    }
}
