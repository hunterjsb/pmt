"""Simple Market Maker - Quote both sides with inventory skew."""

from decimal import Decimal

from ..dsl import strategy
from ..signal import Buy, Sell, Cancel, Hold, Signal, Urgency


# Token to make markets on - pick a liquid market
# Example: "Will BTC be above $100k on Jan 31?"
TOKEN_ID = "21742633143463906290569050155826241533067272736897614950488156847949938836455"

# Strategy parameters
SPREAD_BPS = Decimal("200")      # 2% total spread
SKEW_FACTOR = Decimal("0.001")   # Price skew per unit of inventory
MAX_POSITION = Decimal("100")    # Max long or short
ORDER_SIZE = Decimal("10")       # Quote size each side
MIN_EDGE = Decimal("0.005")      # Minimum edge to quote


@strategy(
    name="market_maker",
    tokens=[TOKEN_ID],
    tick_interval_ms=5000,  # Quote every 5 seconds
    params={
        "TOKEN_ID": TOKEN_ID,
        "SPREAD_BPS": SPREAD_BPS,
        "SKEW_FACTOR": SKEW_FACTOR,
        "MAX_POSITION": MAX_POSITION,
        "ORDER_SIZE": ORDER_SIZE,
        "MIN_EDGE": MIN_EDGE,
    },
)
def on_tick(ctx) -> list[Signal]:
    """Generate market making quotes with inventory skew."""
    signals: list[Signal] = []
    token_id = TOKEN_ID

    # Get order book
    book = ctx.book(token_id)
    if book is None:
        return [Hold()]

    # Need both sides to calculate mid
    if book.best_bid is None:
        return [Hold()]
    if book.best_ask is None:
        return [Hold()]

    bid = book.best_bid
    ask = book.best_ask

    # Calculate mid price
    mid = (bid + ask) / Decimal("2")

    # Get current position
    position = ctx.position(token_id)
    position_size = Decimal("0")
    if position is not None:
        position_size = position.size

    # Calculate half spread
    half_spread_pct = SPREAD_BPS / Decimal("20000")  # BPS to decimal, then half
    half_spread = mid * half_spread_pct

    # Calculate inventory skew
    # If we're long, lower bid and raise ask (encourage sells)
    # If we're short, raise bid and lower ask (encourage buys)
    skew = position_size * SKEW_FACTOR

    # Calculate quote prices (skew moves both quotes in same direction)
    my_bid = mid - half_spread - skew
    my_ask = mid + half_spread - skew

    # Ensure we have minimum edge
    if my_ask - my_bid < MIN_EDGE * Decimal("2"):
        return [Hold()]

    # Clamp prices to valid range [0.01, 0.99]
    if my_bid < Decimal("0.01"):
        my_bid = Decimal("0.01")
    if my_ask > Decimal("0.99"):
        my_ask = Decimal("0.99")

    # Cancel existing orders first
    signals.append(Cancel(token_id=token_id))

    # Determine what to quote based on position
    can_buy = position_size < MAX_POSITION
    can_sell = position_size > -MAX_POSITION

    # Calculate sizes (don't exceed position limit)
    buy_size = ORDER_SIZE
    remaining_buy = MAX_POSITION - position_size
    if remaining_buy < buy_size:
        buy_size = remaining_buy

    sell_size = ORDER_SIZE
    remaining_sell = MAX_POSITION + position_size  # How much we can sell/short
    if remaining_sell < sell_size:
        sell_size = remaining_sell

    # Place bid if we can buy and size > 0
    if can_buy:
        if buy_size > Decimal("0"):
            signals.append(Buy(
                token_id=token_id,
                price=my_bid,
                size=buy_size,
                urgency=Urgency.LOW,  # Post-only
            ))

    # Place ask if we can sell and size > 0
    if can_sell:
        if sell_size > Decimal("0"):
            signals.append(Sell(
                token_id=token_id,
                price=my_ask,
                size=sell_size,
                urgency=Urgency.LOW,  # Post-only
            ))

    return signals
