"""Tests for the Python to Rust transpiler."""

import ast

import pytest

from pmstrat.transpile import (
    transpile,
    RustCodeGen,
    MatchUnwrap,
    generate_mod_rs,
    generate_tests,
    regenerate_mod_rs,
    scan_strategy_file,
)
from pmstrat.dsl import strategy
from pmstrat import Buy, Hold


@strategy(name="test_strategy", tokens=["abc123"])
def simple_strategy(ctx):
    """A simple test strategy."""
    signals = []
    book = ctx.book("abc123")
    if book is None:
        return signals
    return signals


def test_transpile_basic():
    """Test basic transpilation produces valid Rust structure."""
    result = transpile(simple_strategy)

    assert result.strategy_name == "test_strategy"
    assert result.struct_name == "TestStrategy"
    assert result.tokens == ["abc123"]
    assert "pub struct TestStrategy" in result.rust_code
    assert "impl Strategy for TestStrategy" in result.rust_code


def test_transpile_option_unwrap():
    """Test that Option patterns are converted to match expressions."""
    result = transpile(simple_strategy)

    # Should generate match expression for book = ctx.book()
    assert "let book = match ctx.order_books.get" in result.rust_code
    assert "Some(v) => v" in result.rust_code
    assert "None => return" in result.rust_code


@strategy(name="mutable_test", tokens=["xyz"])
def mutable_strategy(ctx):
    """Strategy that mutates a list."""
    signals = []
    signals.append(None)  # This should make signals mutable
    return signals


def test_transpile_mutability():
    """Test that variables used with .append() get 'mut' keyword."""
    result = transpile(mutable_strategy)

    # Should have 'let mut signals' since we call .append()
    assert "let mut signals" in result.rust_code


@strategy(name="nested_option", tokens=["tok"])
def nested_option_strategy(ctx):
    """Strategy with nested Option access (book.best_bid)."""
    signals = []
    book = ctx.book("tok")
    if book is None:
        return signals
    if book.best_bid is None:
        return signals
    bid = book.best_bid
    return signals


def test_transpile_nested_option():
    """Test that nested Option accesses (book.best_bid) are properly unwrapped."""
    result = transpile(nested_option_strategy)

    # Should have match for book
    assert "let book = match ctx.order_books.get" in result.rust_code

    # Should have match for bid (from book.best_bid)
    assert "let bid = match book.best_bid" in result.rust_code


def test_transpile_on_fill_on_shutdown():
    """Test that on_fill and on_shutdown stubs are generated."""
    result = transpile(simple_strategy)

    assert "fn on_fill(&mut self, _fill: &Fill)" in result.rust_code
    assert "fn on_shutdown(&mut self)" in result.rust_code


def test_transpile_full_strategy():
    """A whole strategy body — market scan, threshold, Buy — end to end.

    Used to run against the shipped `sure_bets` module; that and every other
    built-in DSL strategy was deleted in the 2026-08 engine cleanup, so the
    same constructs are asserted against an inline strategy instead.
    """
    from decimal import Decimal

    @strategy(
        name="certainty_taker",
        tokens=[],
        params={"MIN_CERTAINTY": Decimal("0.95"), "SIZE": Decimal("10")},
    )
    def certainty_taker(ctx):
        signals = []
        for token_id, market in ctx.markets.items():
            book = ctx.book(token_id)
            if book is None:
                continue
            if book.best_ask is None:
                continue
            ask = book.best_ask
            if ask >= MIN_CERTAINTY:  # noqa: F821 — transpiler const
                signals.append(Buy(token_id, ask, SIZE, "low"))  # noqa: F821
        return signals

    result = transpile(certainty_taker)

    # Basic structure
    assert result.strategy_name == "certainty_taker"
    assert result.struct_name == "CertaintyTaker"
    assert "pub struct CertaintyTaker" in result.rust_code
    assert "impl Strategy for CertaintyTaker" in result.rust_code

    # The constructs the deleted strategy exercised
    assert "ctx.markets.iter()" in result.rust_code  # markets iteration
    assert "Signal::Buy" in result.rust_code  # Buy signals
    assert "Urgency::" in result.rust_code  # Urgency enum
    assert "const MIN_CERTAINTY: Decimal = dec!(0.95);" in result.rust_code

    # Verify the code compiles (syntax check via string patterns)
    assert "fn on_tick(&mut self, ctx: &StrategyContext)" in result.rust_code
    assert "Vec<Signal>" in result.rust_code


def test_transpile_slug_field():
    """Test that slug field access is correctly transpiled."""
    @strategy(name="slug_test", tokens=["abc"])
    def slug_strategy(ctx):
        signals = []
        for token_id, market in ctx.markets.items():
            slug = market.slug
            signals.append(Hold())
        return signals

    result = transpile(slug_strategy)

    # slug should be accessed with .clone() as it's a String
    assert "market.slug.clone()" in result.rust_code


def test_transpile_usdc_balance():
    """Test that usdc_balance field access is correctly transpiled."""
    @strategy(name="balance_test", tokens=["abc"])
    def balance_strategy(ctx):
        signals = []
        balance = ctx.usdc_balance
        return signals

    result = transpile(balance_strategy)

    # usdc_balance should map to ctx.usdc_balance
    assert "ctx.usdc_balance" in result.rust_code


def test_transpile_params():
    """Test that strategy params are transpiled to Rust constants."""
    from decimal import Decimal

    @strategy(
        name="params_test",
        tokens=[],
        params={
            "MIN_VALUE": Decimal("0.95"),
            "MAX_HOURS": 48.0,
            "KEYWORDS": ["foo", "bar", "baz"],
        }
    )
    def params_strategy(ctx):
        signals = []
        return signals

    result = transpile(params_strategy)

    # Check that constants are generated
    assert "const MIN_VALUE: Decimal = dec!(0.95);" in result.rust_code
    assert "const MAX_HOURS: f64 = 48.0;" in result.rust_code
    assert 'const KEYWORDS: &[&str] = &["foo", "bar", "baz"];' in result.rust_code


def test_transpile_string_lower():
    """Test that str.lower() is transpiled to to_lowercase()."""
    @strategy(name="lower_test", tokens=[])
    def lower_strategy(ctx):
        signals = []
        for token_id, market in ctx.markets.items():
            q_lower = market.question.lower()
        return signals

    result = transpile(lower_strategy)

    # lower() should become to_lowercase()
    assert ".to_lowercase()" in result.rust_code


def test_transpile_in_operator():
    """Test that 'x in y' is transpiled to y.contains(x)."""
    @strategy(name="in_test", tokens=[])
    def in_strategy(ctx):
        signals = []
        for token_id, market in ctx.markets.items():
            q_lower = market.question.lower()
            if "keyword" in q_lower:
                continue
        return signals

    result = transpile(in_strategy)

    # 'in' should become .contains() - no & needed for string contains
    assert '.contains("keyword".to_string())' in result.rust_code


def test_transpile_liquidity():
    """Test that liquidity field access is correctly transpiled."""
    @strategy(name="liquidity_test", tokens=[])
    def liquidity_strategy(ctx):
        signals = []
        for token_id, market in ctx.markets.items():
            if market.liquidity is not None:
                continue
        return signals

    result = transpile(liquidity_strategy)

    # liquidity should be accessible
    assert "market.liquidity" in result.rust_code


# ---------------------------------------------------------------------------
# mod.rs registry generation
# ---------------------------------------------------------------------------

# A minimal strategy file: pub struct + impl Strategy.
_STRATEGY_RS = """//! Auto-generated from Python strategy: {name}

pub struct {struct} {{
    id: String,
    tokens: Vec<String>,
}}

impl Strategy for {struct} {{
    fn id(&self) -> &str {{ &self.id }}
}}
"""

# A helper module: types only, no Strategy impl.
_HELPER_RS = """//! Pure pricing helpers for a sibling strategy.

pub(crate) struct FeedState {
    pub last: i64,
}

pub(crate) fn eval_model(_s: &FeedState) -> i64 { 0 }
"""


def _write_strategies_dir(tmp_path):
    """Build a strategies dir shaped like pmengine's: two plain strategies,
    one strategy that asks for pub(crate), and one helper module."""
    d = tmp_path / "strategies"
    d.mkdir()
    (d / "alert_test.rs").write_text(
        _STRATEGY_RS.format(name="alert_test", struct="AlertTest")
    )
    (d / "updown.rs").write_text(
        "//! Multi-arm crypto trigger.\n"
        "// The replay harness drives this module directly.\n"
        "// pmstrat: pub(crate)\n\n"
        + _STRATEGY_RS.format(name="updown", struct="Updown")
    )
    (d / "updown_model.rs").write_text(_HELPER_RS)
    return d


def test_generate_mod_rs_declares_helper_modules(tmp_path):
    """A .rs file with no `impl Strategy` is a helper: it still gets a
    `pub(crate) mod` line, but no re-export and no registry entry."""
    d = _write_strategies_dir(tmp_path)

    content = generate_mod_rs(d)

    assert "pub(crate) mod updown_model;" in content
    assert "pub use updown_model" not in content
    assert 'm.insert("updown_model"' not in content


def test_generate_mod_rs_honors_pub_crate_marker(tmp_path):
    """`// pmstrat: pub(crate)` in a strategy file widens its mod line."""
    d = _write_strategies_dir(tmp_path)

    content = generate_mod_rs(d)

    assert "pub(crate) mod updown;" in content
    # Unmarked strategies stay private.
    assert "\nmod alert_test;" in content
    assert "pub(crate) mod alert_test;" not in content
    # Strategies are still re-exported and registered.
    assert "pub use updown::Updown;" in content
    assert 'm.insert("updown", StrategyInfo' in content


def test_generate_mod_rs_round_trips(tmp_path):
    """Regenerating over an existing mod.rs reproduces it byte for byte —
    no hand-maintained lines to lose, so a regen can't silently drop them."""
    d = _write_strategies_dir(tmp_path)

    regenerate_mod_rs(d)
    first = (d / "mod.rs").read_text()
    regenerate_mod_rs(d)
    second = (d / "mod.rs").read_text()

    assert first == second
    # And a mod.rs deleted outright comes back identical.
    (d / "mod.rs").unlink()
    regenerate_mod_rs(d)
    assert (d / "mod.rs").read_text() == first


def test_helper_with_pub_struct_is_not_registered(tmp_path):
    """A helper that grows a plain `pub struct` must not become a bogus
    strategy — registration requires an `impl Strategy for` that struct."""
    d = tmp_path / "strategies"
    d.mkdir()
    (d / "market_maker.rs").write_text(
        _STRATEGY_RS.format(name="market_maker", struct="MarketMaker")
    )
    (d / "updown_oracle.rs").write_text(
        "//! Chainlink poller.\n\npub struct OracleState {\n    pub round: u64,\n}\n"
    )

    assert scan_strategy_file(d / "updown_oracle.rs") is None

    content = generate_mod_rs(d)
    assert "pub(crate) mod updown_oracle;" in content
    assert "OracleState" not in content
    assert 'm.insert("updown_oracle"' not in content


# ---------------------------------------------------------------------------
# private/ submodule mount (pm-trade/pmt-strategies) — the generator's half of
# the public/private split: one committed mod.rs that compiles both ways.
# ---------------------------------------------------------------------------

def _write_split_strategies_dir(tmp_path):
    """A strategies dir shaped like the post-split pmengine tree: the example
    strategy public, updown + one helper mounted under private/."""
    d = tmp_path / "strategies"
    d.mkdir()
    (d / "example.rs").write_text(
        _STRATEGY_RS.format(name="example", struct="Example")
    )
    p = d / "private"
    p.mkdir()
    (p / "updown.rs").write_text(
        "//! Multi-arm crypto trigger.\n"
        "// The replay harness drives this module directly.\n"
        "// pmstrat: pub(crate)\n\n"
        + _STRATEGY_RS.format(name="updown", struct="Updown")
    )
    (p / "updown_model.rs").write_text(_HELPER_RS)
    # Non-.rs submodule files the glob must ignore.
    (p / "README.md").write_text("mount docs\n")
    (p / "fixtures").mkdir()
    (p / "fixtures" / "btc.json").write_text("{}")
    return d


def test_private_files_get_gated_path_decls(tmp_path):
    """Every private item — mod decl, pub use, registry insert — is wrapped
    in #[cfg(private_strategies)], with a #[path] decl keeping the module at
    crate::strategies::<name>."""
    d = _write_split_strategies_dir(tmp_path)

    content = generate_mod_rs(d)

    assert (
        '#[cfg(private_strategies)]\n#[path = "private/updown.rs"]\npub(crate) mod updown;'
        in content
    )
    assert (
        '#[cfg(private_strategies)]\n#[path = "private/updown_model.rs"]\npub(crate) mod updown_model;'
        in content
    )
    assert "#[cfg(private_strategies)]\npub use updown::Updown;" in content
    assert '#[cfg(private_strategies)]\n    m.insert("updown", StrategyInfo' in content
    # The public strategy stays ungated.
    assert "\nmod example;" in content
    assert "#[cfg(private_strategies)]\nmod example;" not in content
    assert "pub use example::Example;" in content
    assert 'm.insert("example", StrategyInfo' in content
    # example's unconditional insert keeps `let mut m` used in public builds.
    assert "#[allow(unused_mut)]" not in content
    # README / fixtures in the mount never leak into the module tree.
    assert "mod fixtures" not in content
    assert "README" not in content


def test_pub_crate_marker_honored_across_private_boundary(tmp_path):
    """The content-based `// pmstrat: pub(crate)` scan survives the move into
    private/ — updown keeps crate-wide visibility."""
    d = _write_split_strategies_dir(tmp_path)

    content = generate_mod_rs(d)

    assert "pub(crate) mod updown;" in content
    # And the helper is pub(crate) as always.
    assert "pub(crate) mod updown_model;" in content


def test_public_private_stem_collision_is_a_hard_error(tmp_path):
    """The same module stem on both sides of the boundary is ambiguous —
    refuse loudly, naming both paths, never shadow silently."""
    d = _write_split_strategies_dir(tmp_path)
    (d / "updown.rs").write_text(
        _STRATEGY_RS.format(name="updown", struct="Updown")
    )

    with pytest.raises(RuntimeError) as exc:
        generate_mod_rs(d)
    msg = str(exc.value)
    assert "updown" in msg
    assert str(d / "updown.rs") in msg
    assert str(d / "private" / "updown.rs") in msg


def test_declared_but_empty_private_dir_refuses(tmp_path):
    """.gitmodules declares the mount but the submodule is not initialized:
    regenerating would silently drop every private strategy — hard error."""
    repo = tmp_path / "repo"
    d = repo / "strategies"
    d.mkdir(parents=True)
    (d / "example.rs").write_text(
        _STRATEGY_RS.format(name="example", struct="Example")
    )
    # An uninitialized submodule leaves an EMPTY directory behind.
    (d / "private").mkdir()
    (repo / ".gitmodules").write_text(
        '[submodule "strategies/private"]\n'
        "\tpath = strategies/private\n"
        "\turl = https://github.com/pm-trade/pmt-strategies.git\n"
        "\tupdate = none\n"
    )

    with pytest.raises(RuntimeError) as exc:
        regenerate_mod_rs(d)
    assert "submodule not initialized" in str(exc.value)
    assert "--public" in str(exc.value)
    # The guard must refuse BEFORE writing anything.
    assert not (d / "mod.rs").exists()


def test_public_flag_emits_zero_private_decls(tmp_path):
    """--public knowingly emits the public form: no private decls, no cfg
    gates, and the uninitialized-submodule guard is bypassed."""
    repo = tmp_path / "repo"
    d = repo / "strategies"
    d.mkdir(parents=True)
    (d / "example.rs").write_text(
        _STRATEGY_RS.format(name="example", struct="Example")
    )
    (d / "private").mkdir()
    (repo / ".gitmodules").write_text(
        '[submodule "strategies/private"]\n'
        "\tpath = strategies/private\n"
        "\turl = https://github.com/pm-trade/pmt-strategies.git\n"
    )

    regenerate_mod_rs(d, public=True)
    content = (d / "mod.rs").read_text()
    # The doc header still documents the private/ convention; what must be
    # absent is any actual gate or path decl.
    assert "#[cfg(private_strategies)]" not in content
    assert '#[path = "private/' not in content
    assert 'm.insert("example"' in content

    # Even with the submodule INITED, --public skips private/ entirely.
    split = _write_split_strategies_dir(tmp_path)
    content = generate_mod_rs(split, public=True)
    assert "updown" not in content
    assert "#[cfg(private_strategies)]" not in content


def test_generate_mod_rs_round_trips_with_private_files(tmp_path):
    """Regeneration over the split tree is byte-stable — the committed
    mod.rs never churns."""
    d = _write_split_strategies_dir(tmp_path)

    regenerate_mod_rs(d)
    first = (d / "mod.rs").read_text()
    regenerate_mod_rs(d)
    second = (d / "mod.rs").read_text()

    assert first == second
    (d / "mod.rs").unlink()
    regenerate_mod_rs(d)
    assert (d / "mod.rs").read_text() == first


def test_all_private_registry_carries_allow_unused_mut(tmp_path):
    """With zero unconditional inserts a public build's `let mut m` is never
    mutated — the generator must emit #[allow(unused_mut)] or public clippy
    fails. Only reachable if every strategy went private."""
    d = tmp_path / "strategies"
    d.mkdir()
    p = d / "private"
    p.mkdir()
    (p / "updown.rs").write_text(
        _STRATEGY_RS.format(name="updown", struct="Updown")
    )

    content = generate_mod_rs(d)
    assert "#[allow(unused_mut)]\npub fn registry()" in content


def test_generated_tests_for_private_strategy_are_cfg_gated():
    """A private strategy's generated test file opens with
    #![cfg(private_strategies)] so a public checkout skips it cleanly."""
    gated = generate_tests(simple_strategy, private=True)
    assert gated.startswith("#![cfg(private_strategies)]\n")
    ungated = generate_tests(simple_strategy)
    assert "private_strategies" not in ungated
