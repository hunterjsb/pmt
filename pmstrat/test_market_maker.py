#!/usr/bin/env python3
"""Test the market maker strategy with mock market data."""

from decimal import Decimal
from datetime import datetime, timezone

from pmstrat.context import Context, OrderBookSnapshot, Position
from pmstrat.strategies.market_maker import on_tick, TOKEN_ID


def test_scenario(name: str, best_bid: Decimal, best_ask: Decimal, position_size: Decimal = Decimal("0")):
    """Run the strategy with given market conditions and print results."""
    print(f"\n{'='*60}")
    print(f"Scenario: {name}")
    print(f"  Best Bid: {best_bid}")
    print(f"  Best Ask: {best_ask}")
    print(f"  Mid: {(best_bid + best_ask) / 2}")
    print(f"  Spread: {best_ask - best_bid}")
    print(f"  Position: {position_size}")
    print("-" * 60)

    # Create order book
    book = OrderBookSnapshot(
        token_id=TOKEN_ID,
        best_bid=best_bid,
        best_ask=best_ask,
        bid_size=Decimal("100"),
        ask_size=Decimal("100"),
    )

    # Create position if non-zero
    positions = {}
    if position_size != Decimal("0"):
        positions[TOKEN_ID] = Position(
            token_id=TOKEN_ID,
            size=position_size,
            avg_entry_price=Decimal("0.50"),
        )

    # Create context
    ctx = Context(
        timestamp=datetime.now(timezone.utc),
        books={TOKEN_ID: book},
        positions=positions,
    )

    # Run strategy
    signals = on_tick(ctx)

    print("Signals generated:")
    for signal in signals:
        print(f"  {signal}")

    return signals


def main():
    print("Market Maker Strategy Test")
    print("=" * 60)
    print(f"Token ID: {TOKEN_ID}")

    # Scenario 1: Normal market, flat position
    test_scenario(
        "Normal market, flat position",
        best_bid=Decimal("0.45"),
        best_ask=Decimal("0.55"),
        position_size=Decimal("0"),
    )

    # Scenario 2: Normal market, long position (should skew quotes down)
    test_scenario(
        "Normal market, long 50 shares",
        best_bid=Decimal("0.45"),
        best_ask=Decimal("0.55"),
        position_size=Decimal("50"),
    )

    # Scenario 3: Normal market, short position (should skew quotes up)
    test_scenario(
        "Normal market, short 50 shares",
        best_bid=Decimal("0.45"),
        best_ask=Decimal("0.55"),
        position_size=Decimal("-50"),
    )

    # Scenario 4: At max long position (should only quote ask)
    test_scenario(
        "At max long position (100)",
        best_bid=Decimal("0.45"),
        best_ask=Decimal("0.55"),
        position_size=Decimal("100"),
    )

    # Scenario 5: At max short position (should only quote bid)
    test_scenario(
        "At max short position (-100)",
        best_bid=Decimal("0.45"),
        best_ask=Decimal("0.55"),
        position_size=Decimal("-100"),
    )

    # Scenario 6: Tight spread (should hold - not enough edge)
    test_scenario(
        "Tight spread (no edge)",
        best_bid=Decimal("0.495"),
        best_ask=Decimal("0.505"),
        position_size=Decimal("0"),
    )

    # Scenario 7: Wide spread market
    test_scenario(
        "Wide spread market",
        best_bid=Decimal("0.30"),
        best_ask=Decimal("0.70"),
        position_size=Decimal("0"),
    )

    # Scenario 8: Near price boundaries
    test_scenario(
        "Near lower boundary",
        best_bid=Decimal("0.02"),
        best_ask=Decimal("0.08"),
        position_size=Decimal("0"),
    )


if __name__ == "__main__":
    main()
