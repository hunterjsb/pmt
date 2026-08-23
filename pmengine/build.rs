//! Sets `cfg(private_strategies)` when the pm-trade/pmt-strategies submodule
//! is mounted at src/strategies/private. The probe is a FILE, never the
//! directory: an uninitialized submodule leaves an empty dir behind, and that
//! must read as ABSENT or every public clone breaks.

fn main() {
    // Keeps rustc/clippy's unexpected_cfgs lint clean on both flavors.
    println!("cargo::rustc-check-cfg=cfg(private_strategies)");
    // Dir mtime flips on submodule init/deinit; the file covers edits.
    println!("cargo:rerun-if-changed=src/strategies/private");
    println!("cargo:rerun-if-changed=src/strategies/private/updown.rs");
    let private = std::path::Path::new("src/strategies/private/updown.rs").exists();
    // tests/flavor.rs reads this env to fail loudly when the documented
    // private gate (PMENGINE_EXPECT_PRIVATE=1) runs against a public build.
    println!(
        "cargo:rustc-env=PMENGINE_PRIVATE_STRATEGIES={}",
        if private { "1" } else { "0" }
    );
    if private {
        println!("cargo:rustc-cfg=private_strategies");
    } else {
        println!("cargo:warning=private strategies absent — building public engine (example only)");
    }
}
