"""Simple spread watcher strategy - buys when spread is wide."""

from ..dsl import strategy
from ..signal import Buy, Hold, Urgency
from decimal import Decimal


@strategy(
    name="spread_watcher",
    # Vermont Governor 2026 - Phil Scott (YES)
    tokens=["41583919731714354912849507182398941127545694257513505398713274521520484370640"],
)
def on_tick(ctx):
    """Buy if spread is > 50% and we can get a good price."""
    token = "41583919731714354912849507182398941127545694257513505398713274521520484370640"
    signals = []

    book = ctx.book(token)
    if book is None:
        return signals

    # Only proceed if we have both bid and ask
    if book.best_bid is None:
        return signals
    if book.best_ask is None:
        return signals

    bid = book.best_bid
    ask = book.best_ask
    spread = ask - bid

    # If spread is wide (> 0.50), place a bid in the middle
    if spread > Decimal("0.50"):
        mid = (bid + ask) / Decimal("2")
        signals.append(Buy(
            token_id=token,
            price=mid,
            size=Decimal("1"),
            urgency=Urgency.LOW,
        ))

    return signals
