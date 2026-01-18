"""Tests for the Python to Rust transpiler."""

import ast
from pmstrat.transpile import transpile, RustCodeGen, MatchUnwrap
from pmstrat.dsl import strategy


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
