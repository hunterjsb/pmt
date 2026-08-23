"""Tests for the Python to Rust transpiler."""

import ast
from pmstrat.transpile import (
    transpile,
    RustCodeGen,
    MatchUnwrap,
    generate_mod_rs,
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
