"""Tests for backtest runner."""

from decimal import Decimal
from datetime import datetime, timedelta

import pytest

from pmstrat import Context, Buy, Hold, OrderBookSnapshot, Position, MarketInfo
from pmstrat.backtest import Backtester, Tick, generate_synthetic_ticks


def simple_strategy(ctx: Context) -> list:
    """Simple test strategy - buy if price < 0.97."""
    signals = []
    for token_id, book in ctx.books.items():
        if book.best_ask and book.best_ask < Decimal("0.97"):
            signals.append(Buy(
                token_id=token_id,
                price=book.best_ask,
                size=Decimal("50"),
            ))
    return signals if signals else [Hold()]


def test_backtest_no_trades():
    """Backtest with no trading opportunities."""
    def hold_strategy(ctx):
        return [Hold()]

    backtester = Backtester(hold_strategy)

    # High-priced ticks (no buy opportunity)
    ticks = [
        Tick(
            timestamp=datetime.now(),
            token_id="test",
            best_bid=Decimal("0.98"),
            best_ask=Decimal("0.99"),
            bid_size=Decimal("100"),
            ask_size=Decimal("100"),
        )
        for _ in range(10)
    ]

    result = backtester.run(iter(ticks))

    assert result.num_trades == 0
    assert result.total_pnl == Decimal(0)


def test_backtest_with_trade():
    """Backtest executes a trade."""
    backtester = Backtester(simple_strategy)

    ticks = [
        Tick(
            timestamp=datetime.now() + timedelta(minutes=i),
            token_id="test",
            best_bid=Decimal("0.95"),
            best_ask=Decimal("0.96"),
            bid_size=Decimal("100"),
            ask_size=Decimal("100"),
            end_date=datetime.now() + timedelta(hours=1),
        )
        for i in range(10)
    ]

    result = backtester.run(iter(ticks))

    assert result.num_trades > 0
    assert len(result.fills) > 0
    assert result.fills[0].side == "BUY"


def test_backtest_position_resolution():
    """Position resolves when price hits 1.00."""
    backtester = Backtester(simple_strategy, initial_balance=Decimal("1000"))

    ticks = []
    now = datetime.now()

    # First tick: buy opportunity at 0.96
    ticks.append(Tick(
        timestamp=now,
        token_id="test",
        best_bid=Decimal("0.95"),
        best_ask=Decimal("0.96"),
        bid_size=Decimal("100"),
        ask_size=Decimal("100"),
        end_date=now + timedelta(hours=1),
    ))

    # Price rises to 0.99 (triggers resolution)
    for i in range(1, 10):
        ticks.append(Tick(
            timestamp=now + timedelta(minutes=i),
            token_id="test",
            best_bid=Decimal("0.99"),
            best_ask=Decimal("1.00"),
            bid_size=Decimal("100"),
            ask_size=Decimal("100"),
            end_date=now + timedelta(hours=1),
        ))

    result = backtester.run(iter(ticks))

    # Should have positive P&L from resolution
    assert result.realized_pnl > Decimal(0)


def test_synthetic_tick_generator():
    """Synthetic ticks are generated correctly."""
    ticks = list(generate_synthetic_ticks(num_ticks=100))

    assert len(ticks) == 100
    assert all(tick.best_bid is not None for tick in ticks)
    assert all(tick.best_ask is not None for tick in ticks)
    assert all(tick.best_bid < tick.best_ask for tick in ticks)


def test_slippage_applied():
    """Slippage is applied to fills."""
    backtester = Backtester(
        simple_strategy,
        slippage_bps=Decimal("50"),  # 0.5% slippage
    )

    ticks = [
        Tick(
            timestamp=datetime.now(),
            token_id="test",
            best_bid=Decimal("0.95"),
            best_ask=Decimal("0.96"),
            bid_size=Decimal("100"),
            ask_size=Decimal("100"),
        )
    ]

    result = backtester.run(iter(ticks))

    if result.fills:
        fill = result.fills[0]
        # Fill price should be higher than ask due to slippage
        assert fill.price > Decimal("0.96")
        assert fill.slippage > Decimal(0)
