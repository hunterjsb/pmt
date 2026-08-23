//! Acceptance criterion 1 of issue #5, as a test: the characterization
//! suite passes on a machine that has never run the engine.
//!
//! Its own binary with a single test, because it clobbers `HOME` for the
//! whole process — anything else running beside it would see the fake home
//! too. Replay's `~/.pmt` paths (`default_eval_tape_path`,
//! `default_book_tape_path`, `kline_cache_path`) are all derived from
//! `$HOME`, so pointing it at an empty directory means any accidental
//! dependency on the corpus fails loudly here instead of on a fresh
//! checkout six months from now.

use pmengine::replay::fixtures;
use std::path::PathBuf;

#[test]
fn the_suite_passes_with_no_pmt_directory() {
    let fake_home = std::env::temp_dir()
        .join(format!("pmengine-no-home-{}", std::process::id()));
    let _ = std::fs::remove_dir_all(&fake_home);
    std::fs::create_dir_all(&fake_home).unwrap();
    std::env::set_var("HOME", &fake_home);
    assert!(!fake_home.join(".pmt").exists(), "the fake home must be empty");

    let dir = PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("fixtures");
    for path in fixtures::fixture_paths(&dir).expect("fixtures directory") {
        let fx = fixtures::load_fixture(&path).unwrap_or_else(|e| panic!("{}", e));
        let res = fixtures::run_fixture(&fx)
            .unwrap_or_else(|e| panic!("{}: {}", path.display(), e));
        assert!(
            res.passed,
            "{} failed with no ~/.pmt:\n  {}",
            res.slug,
            res.failures.join("\n  ")
        );
    }
    // Nothing may have been created under the fake home — a fixture run
    // that touched the kline cache would leave one behind.
    assert!(!fake_home.join(".pmt").exists(), "a fixture run wrote under $HOME");
    let _ = std::fs::remove_dir_all(&fake_home);
}
