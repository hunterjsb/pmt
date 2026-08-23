//! The green-because-skipped guard — the single most dangerous failure mode
//! of the private-strategies split. A checkout without the pmt-strategies
//! submodule still compiles, the characterization suites gate down to zero
//! tests, and clippy stays clean: the money-path regression net quietly
//! stops existing. This test is deliberately UNGATED so it runs in both
//! flavors; the documented private gate exports PMENGINE_EXPECT_PRIVATE=1
//! and a silently-public build then fails HERE, loudly, by name.
//! Public CI leaves the variable unset and this test trivially passes.

#[test]
fn flavor_matches_expectation() {
    if std::env::var("PMENGINE_EXPECT_PRIVATE").as_deref() == Ok("1") {
        assert_eq!(
            env!("PMENGINE_PRIVATE_STRATEGIES"),
            "1",
            "expected the PRIVATE flavor but the submodule is not initialized — \
             run: git submodule update --init --checkout pmengine/src/strategies/private"
        );
    }
}
