//! The committed characterization suite: every fixture under
//! `pmengine/src/strategies/private/fixtures/` (the pmt-strategies
//! submodule) replayed through decide() on every `cargo test`.
//!
//! A failure here is a finding, not a chore. It means the decision core
//! changed behaviour on a trade that actually happened, with real money —
//! triage it before merging. Regenerating an expectation (`pmt crypto
//! fixture <slug> --regen`) is a deliberate act that has to name, in the
//! commit message, which numbers moved and what moved them.
//!
//! Offline by construction: a fixture carries its own klines, so the
//! fixture runner never reaches `klines_for_window` — replay's only network
//! path. `characterization_offline.rs` proves the `~/.pmt` half.

// No pmt-strategies submodule -> no updown, no replay tree, no fixtures:
// this whole crate compiles to zero tests and skips cleanly. The documented
// private gate sets PMENGINE_EXPECT_PRIVATE=1 so tests/flavor.rs fails
// loudly if this skip ever happens where the private flavor was expected.
#![cfg(private_strategies)]

use pmengine::replay::fixtures::{self, Fixture};
use std::path::PathBuf;

fn fixtures_dir() -> PathBuf {
    // Inside the pmt-strategies submodule mount: the fixtures embed as-armed
    // params/tunables — live alpha — so they live with the private code.
    PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("src/strategies/private/fixtures")
}

fn all_fixtures() -> Vec<(PathBuf, Fixture)> {
    fixtures::fixture_paths(&fixtures_dir())
        .expect("fixtures directory")
        .into_iter()
        .map(|p| {
            let fx = fixtures::load_fixture(&p).unwrap_or_else(|e| panic!("{}", e));
            (p, fx)
        })
        .collect()
}

/// An empty suite must not read as a green suite — the exact way a
/// regression net goes quiet without anyone noticing.
#[test]
fn the_suite_is_not_empty() {
    let n = all_fixtures().len();
    assert!(n >= 10, "expected the seeded catalog, found {} fixture(s)", n);
}

#[test]
fn every_committed_fixture_still_replays_to_its_expectations() {
    let mut failures: Vec<String> = Vec::new();
    for (path, fx) in all_fixtures() {
        let res = fixtures::run_fixture(&fx)
            .unwrap_or_else(|e| panic!("{}: {}", path.display(), e));
        if !res.passed {
            failures.push(format!(
                "\n{}  ({})\n    {}",
                res.slug,
                fx.teaches,
                res.failures.join("\n    ")
            ));
        }
    }
    assert!(
        failures.is_empty(),
        "{} characterization fixture(s) diverged from the behaviour they froze:{}",
        failures.len(),
        failures.join("")
    );
}

/// The ground-truth rule, enforced where it cannot be argued with: a
/// fixture grades on what the wallet was actually paid. A derived label can
/// lie (docs/LESSONS.md#L36) and this suite is what other measurements are
/// checked against.
#[test]
fn every_fixture_is_wallet_graded() {
    for (path, fx) in all_fixtures() {
        assert_eq!(
            fx.outcome.source,
            "wallet",
            "{} is {}-graded",
            path.display(),
            fx.outcome.source
        );
    }
}

/// Self-containment, field by field: a full-mode fixture that shipped a
/// short kline slice would silently reconstruct a different rho rather than
/// fail, which is the one failure mode an offline harness cannot survive.
#[test]
fn every_fixture_carries_everything_its_replay_reads() {
    const KLINE_LOOKBACK_S: i64 = 2700;
    for (path, fx) in all_fixtures() {
        let name = path.display();
        assert!(!fx.teaches.starts_with("TODO"), "{}: teaches is still a TODO", name);
        assert!(!fx.provenance.is_null(), "{}: no provenance", name);
        assert!(fx.expect.is_some(), "{}: unblessed", name);
        assert_eq!(fx.window_utc.len(), 2, "{}: window_utc", name);

        if fx.mode != "full" {
            continue;
        }
        let start = fx.params["start"].as_f64().expect("params.start") as i64;
        let end = fx.params["end"].as_f64().expect("params.end") as i64;

        // Which slice has to be whole follows the arm's feed, exactly as it
        // does in the loader and live: a stream-fed window never read a
        // kline, and a Binance window has no rtds slice to be short of.
        if fx.params["feed"].as_str() == Some("rtds") {
            assert!(fx.klines.is_empty(), "{}: a stream-fed window read no klines", name);
            assert!(!fx.rtds.is_empty(), "{}: full mode on rtds needs its stream slice", name);
            let t = |r: &serde_json::Value| r["t_recv"].as_f64().unwrap_or(0.0);
            let first = fx.rtds.iter().map(t).fold(f64::INFINITY, f64::min);
            let last = fx.rtds.iter().map(t).fold(f64::NEG_INFINITY, f64::max);
            // The settlement reference prints AT `start`, and the last
            // in-window mark AT the close — both have to be inside.
            assert!(
                first <= start as f64,
                "{}: rtds slice starts {:.0}s after the window's reference print",
                name,
                first - start as f64
            );
            assert!(
                last >= end as f64,
                "{}: rtds slice ends {:.0}s before the close",
                name,
                end as f64 - last
            );
            continue;
        }

        let have: std::collections::HashSet<i64> = fx.klines.iter().map(|k| k.t).collect();
        let (lo, hi) = (
            (start - KLINE_LOOKBACK_S).div_euclid(60) * 60,
            end.div_euclid(60) * 60,
        );
        let missing = (lo..hi).step_by(60).filter(|m| !have.contains(m)).count();
        assert_eq!(missing, 0, "{}: kline slice is short by {} minute(s)", name, missing);
    }
}

/// A fixture's expectations are only meaningful against the tape generation
/// present in ITS slice — the tape grew structured fields mid-corpus, so a
/// gate-heavy fixture cut before they shipped pins less than it looks like
/// it does. Recording that is what stops the difference from being invisible.
#[test]
fn every_fixture_records_its_tape_schema_generation() {
    for (path, fx) in all_fixtures() {
        assert!(
            fx.provenance.get("tape_schema").is_some(),
            "{}: no tape_schema in provenance",
            path.display()
        );
    }
}
