"""Hantavirus Pandemic Two-Sided Maker.

Pins a $200+ BID/ASK pair on Hantavirus pandemic NO inside Polymarket's
rewards eligibility window (max_spread 5.5¢ around the midpoint). Quotes
~0.9¢ off the mid on each side. Re-quotes when one side fills or the mid
drifts. Inventory skew is intentionally tiny so we stay symmetric — the
existing NO position (~1,295 shares) is the fundamental thesis, not P&L
from market making.

Reward eligibility for high-mid markets (mid > 0.90) uses the strict
`min(Q_bid, Q_ask)` formula — both sides must stay live to score. The
engine's REST book poller keeps mid current; this strategy re-quotes on
each tick so the structure heals within `tick_interval_ms`.
"""

from decimal import Decimal

from ..dsl import strategy
from ..signal import Buy, Sell, Cancel, Hold, Signal, Urgency


# Hantavirus pandemic in 2026? — NO token
TOKEN_ID = "95212449865986159112377413335252801281670333750637442556685159781445406848396"

# Quote ±0.9¢ around mid → 1.8¢ total spread, well under 5.5¢ rewards cap.
HALF_SPREAD = Decimal("0.009")
# Per-side quote size. 400 × $0.94 ≈ $376 notional, comfortably above the
# $200 rewards minimum. Smaller than the full 1295 NO we hold so the engine
# can run a real BID inside ~$480 free cash without a balance error.
ORDER_SIZE = Decimal("400")
# Hard cap on net long NO position from this strategy (we already hold ~1,295
# NO outside the strategy). Setting wide so the strategy can extend the
# position by ~600 before it stops bidding.
MAX_POSITION = Decimal("2000")
# Don't quote if computed spread collapses below this. Tiny insurance.
MIN_EDGE = Decimal("0.003")


@strategy(
    name="hanta_maker",
    tokens=[TOKEN_ID],
    tick_interval_ms=10000,  # Re-quote every 10s
    params={
        "TOKEN_ID": TOKEN_ID,
        "HALF_SPREAD": HALF_SPREAD,
        "ORDER_SIZE": ORDER_SIZE,
        "MAX_POSITION": MAX_POSITION,
        "MIN_EDGE": MIN_EDGE,
    },
)
def on_tick(ctx) -> list[Signal]:
    """Quote a symmetric BID/ASK pair around the current mid."""
    signals: list[Signal] = []
    token_id = TOKEN_ID

    book = ctx.book(token_id)
    if book is None:
        return [Hold()]
    if book.best_bid is None:
        return [Hold()]
    if book.best_ask is None:
        return [Hold()]

    bid = book.best_bid
    ask = book.best_ask
    mid = (bid + ask) / Decimal("2")

    my_bid = mid - HALF_SPREAD
    my_ask = mid + HALF_SPREAD

    if my_ask - my_bid < MIN_EDGE * Decimal("2"):
        return [Hold()]

    if my_bid < Decimal("0.01"):
        my_bid = Decimal("0.01")
    if my_ask > Decimal("0.99"):
        my_ask = Decimal("0.99")

    # Refresh both sides every tick. Cancel-then-replace keeps us at the
    # current mid; if one side already filled, this re-establishes the pair.
    signals.append(Cancel(token_id=token_id))

    position = ctx.position(token_id)
    position_size = Decimal("0")
    if position is not None:
        position_size = position.size

    # Only bid if we're not already at the long cap.
    if position_size < MAX_POSITION:
        signals.append(
            Buy(
                token_id=token_id,
                size=ORDER_SIZE,
                price=my_bid,
                urgency=Urgency.MEDIUM,
            )
        )

    # Always offer the ask — we're long these shares outside the strategy
    # anyway and want to keep that side of the pair live for rewards.
    signals.append(
        Sell(
            token_id=token_id,
            size=ORDER_SIZE,
            price=my_ask,
            urgency=Urgency.MEDIUM,
        )
    )

    return signals
