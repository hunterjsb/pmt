//! The strategy registry after the 2026-08 dead-strategy cleanup.
//!
//! Deliberately key-only: `registry()` entries are lazy factories, and
//! `Updown::new()` reads (and would rewrite) the live `~/.pmt/engine`
//! arm state, so a test must never call one.

use pmengine::engine::EngineError;
use pmengine::strategies::registry;

/// Every strategy deleted in the cleanup, plus their DSL sources in pmstrat.
/// A name reappearing here means a `pmstrat transpile --all` resurrected it —
/// check that the Python source really is gone, not just the Rust.
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
fn registry_carries_updown() {
    let reg = registry();
    assert!(
        reg.contains_key("updown"),
        "updown is the only live strategy — registry has {:?}",
        reg.keys().collect::<Vec<_>>()
    );
}

#[test]
fn registry_is_exactly_the_surviving_set() {
    let reg = registry();
    let mut names: Vec<&str> = reg.keys().copied().collect();
    names.sort_unstable();
    assert_eq!(names, vec!["updown"]);
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
