"""Test strategy that places and immediately cancels an order."""

from decimal import Decimal
from pmstrat import strategy, Context, Signal, Buy, Cancel, Hold, Urgency


# A real token ID from Polymarket - "Will BTC be above $100k on Jan 31?"
# This is a liquid market that should have active trading
TOKEN_ID = "21742633143463906290569050155826241533067272736897614950488156847949938836455"


_order_placed = False

@strategy(
    name="order_test",
    tokens=[TOKEN_ID],
    tick_interval_ms=5000,
    transpilable=False,  # Uses global state - Python-only test strategy
)
def on_tick(ctx: Context) -> list[Signal]:
    """Place a small order once, then hold."""
    global _order_placed

    if _order_placed:
        return [Hold()]

    token_id = "21742633143463906290569050155826241533067272736897614950488156847949938836455"
    _order_placed = True

    return [
        Buy(
            token_id=token_id,
            price=Decimal("0.01"),
            size=Decimal("5"),
            urgency=Urgency.LOW,
        )
    ]
