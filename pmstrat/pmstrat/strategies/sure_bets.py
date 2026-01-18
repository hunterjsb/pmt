"""Sure Bets Strategy - Low-risk betting on high-certainty expiring markets.

Strategy:
    - Find markets priced at 95%+ that are expiring within 2 hours
    - Buy the high-certainty outcome
    - Wait for resolution, collect 1-5% profit

Risk profile:
    - Very low: Only bet on near-certain outcomes
    - Main risk: Market doesn't resolve as expected (rare at 95%+)
    - Expected win rate: 95%+
"""

from decimal import Decimal
from datetime import datetime, timezone

from pmstrat import strategy, Context, Buy, Hold, Signal, Urgency


# Strategy parameters
MIN_CERTAINTY = Decimal("0.95")  # 95% minimum price
MAX_HOURS_TO_EXPIRY = Decimal("2.0")  # Only markets expiring within 2 hours
MAX_POSITION_SIZE = Decimal("100")  # Max shares per position
MIN_EXPECTED_RETURN = Decimal("0.01")  # 1% minimum expected return


@strategy(
    name="sure_bets",
    tokens=[],  # Dynamic - we scan for opportunities
    tick_interval_ms=60000,  # Check every minute
)
def on_tick(ctx: Context) -> list[Signal]:
    """Scan for high-certainty expiring markets and generate buy signals."""
    signals: list[Signal] = []

    for token_id, market in ctx.markets.items():
        # Skip if no market info
        if market.end_date is None:
            continue

        # Check if expiring soon
        hours_left = market.hours_until_expiry
        if hours_left is None or hours_left < 0 or hours_left > float(MAX_HOURS_TO_EXPIRY):
            continue

        # Get order book
        book = ctx.book(token_id)
        if book is None or book.best_ask is None:
            continue

        # Check if high certainty (price >= 95%)
        ask_price = book.best_ask
        if ask_price < MIN_CERTAINTY:
            continue

        # Calculate expected return
        # If we buy at ask and it resolves to 1.00, our profit is (1.00 - ask) / ask
        expected_return = (Decimal("1.00") - ask_price) / ask_price
        if expected_return < MIN_EXPECTED_RETURN:
            continue

        # Check if we already have a position
        position = ctx.position(token_id)
        current_size = position.size if position else Decimal(0)

        # Don't exceed max position
        if current_size >= MAX_POSITION_SIZE:
            continue

        # Calculate how much more we can buy
        remaining = MAX_POSITION_SIZE - current_size
        size = min(remaining, book.ask_size, Decimal("50"))  # Cap single order at 50

        if size < Decimal("10"):  # Minimum order size
            continue

        # Generate buy signal
        signals.append(Buy(
            token_id=token_id,
            price=ask_price,
            size=size,
            urgency=Urgency.MEDIUM,
        ))

    return signals if signals else [Hold()]


def scan_opportunities(ctx: Context) -> list[dict]:
    """Utility function to list current opportunities without trading.

    Returns list of dicts with opportunity details.
    """
    opportunities = []

    for token_id, market in ctx.markets.items():
        if market.end_date is None:
            continue

        hours_left = market.hours_until_expiry
        if hours_left is None or hours_left < 0 or hours_left > float(MAX_HOURS_TO_EXPIRY):
            continue

        book = ctx.book(token_id)
        if book is None or book.best_ask is None:
            continue

        ask_price = book.best_ask
        if ask_price < MIN_CERTAINTY:
            continue

        expected_return = (Decimal("1.00") - ask_price) / ask_price
        hourly_return = expected_return / Decimal(str(max(hours_left, 0.1)))

        opportunities.append({
            "token_id": token_id,
            "question": market.question,
            "outcome": market.outcome,
            "ask_price": float(ask_price),
            "ask_size": float(book.ask_size),
            "hours_left": hours_left,
            "expected_return_pct": float(expected_return * 100),
            "hourly_return_pct": float(hourly_return * 100),
        })

    # Sort by hourly return (best first)
    opportunities.sort(key=lambda x: x["hourly_return_pct"], reverse=True)
    return opportunities
