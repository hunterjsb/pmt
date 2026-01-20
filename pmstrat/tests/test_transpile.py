"""Tests for the Python to Rust transpiler."""

import ast
from pmstrat.transpile import transpile, RustCodeGen, MatchUnwrap
from pmstrat.dsl import strategy
from pmstrat import Hold


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


def test_transpile_sure_bets():
    """Test transpiling the actual sure_bets strategy.

    This is an integration test that verifies:
    1. The sure_bets strategy can be transpiled without errors
    2. Key constructs are correctly converted
    """
    from pmstrat.strategies.sure_bets import on_tick

    result = transpile(on_tick)

    # Basic structure
    assert result.strategy_name == "sure_bets"
    assert result.struct_name == "SureBets"
    assert "pub struct SureBets" in result.rust_code
    assert "impl Strategy for SureBets" in result.rust_code

    # Key patterns from sure_bets are transpiled
    assert "ctx.markets.iter()" in result.rust_code  # markets iteration
    assert "Signal::Buy" in result.rust_code  # Buy signals
    assert "Urgency::" in result.rust_code  # Urgency enum

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
