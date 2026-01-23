"""Dynamic Market Maker - Auto-discovers and quotes multiple markets.

Strategy:
    - Scans all available markets via ctx.markets
    - Filters by liquidity, price range, spread, and expiry
    - Makes markets on multiple qualifying tokens simultaneously
    - Applies per-token inventory skew to manage risk

Filter criteria:
    - MIN_LIQUIDITY: $10,000 - Sufficient depth
    - MIN_PRICE: 0.20 - Avoid resolved-NO markets
    - MAX_PRICE: 0.80 - Avoid resolved-YES markets
    - MIN_SPREAD_PCT: 2% - Need edge to profit
    - MAX_SPREAD_PCT: 15% - Too wide = too risky
    - MIN_HOURS_TO_EXPIRY: 24h - Time to mean-revert
    - MAX_TOKENS: 5 - Limit complexity
"""

from decimal import Decimal

from ..dsl import strategy
from ..signal import Buy, Sell, Cancel, Hold, Signal, Urgency


# Strategy parameters
MIN_LIQUIDITY = 10000.0           # Minimum market liquidity ($)
MIN_PRICE = Decimal("0.20")       # Avoid near-resolved-NO
MAX_PRICE = Decimal("0.80")       # Avoid near-resolved-YES
MIN_SPREAD_PCT = Decimal("0.02")  # Minimum spread to quote (2%)
MAX_SPREAD_PCT = Decimal("0.15")  # Maximum spread to quote (15%)
MIN_HOURS_TO_EXPIRY = 24.0        # At least 24h until expiry
MAX_TOKENS = 5                    # Max tokens to quote simultaneously
MAX_POSITION = Decimal("75")      # Max position per token
ORDER_SIZE = Decimal("10")        # Quote size each side
SPREAD_BPS = Decimal("150")       # Our spread width (1.5% total)
SKEW_FACTOR = Decimal("0.001")    # Price skew per unit of inventory
MIN_EDGE = Decimal("0.005")       # Minimum edge to quote


@strategy(
    name="dynamic_market_maker",
    tokens=[],  # Dynamic discovery - scan all markets
    tick_interval_ms=10000,  # Check every 10 seconds
    params={
        "MIN_LIQUIDITY": MIN_LIQUIDITY,
        "MIN_PRICE": MIN_PRICE,
        "MAX_PRICE": MAX_PRICE,
        "MIN_SPREAD_PCT": MIN_SPREAD_PCT,
        "MAX_SPREAD_PCT": MAX_SPREAD_PCT,
        "MIN_HOURS_TO_EXPIRY": MIN_HOURS_TO_EXPIRY,
        "MAX_TOKENS": MAX_TOKENS,
        "MAX_POSITION": MAX_POSITION,
        "ORDER_SIZE": ORDER_SIZE,
        "SPREAD_BPS": SPREAD_BPS,
        "SKEW_FACTOR": SKEW_FACTOR,
        "MIN_EDGE": MIN_EDGE,
    },
)
def on_tick(ctx) -> list[Signal]:
    """Scan markets, filter, and generate market making quotes."""
    signals: list[Signal] = []
    tokens_quoted = 0

    for token_id, market in ctx.markets.items():
        # Stop if we've quoted enough tokens
        if tokens_quoted >= MAX_TOKENS:
            break

        # Filter by liquidity - explicit None check
        liquidity = market.liquidity
        if liquidity is None:
            continue
        if liquidity < MIN_LIQUIDITY:
            continue

        # Filter by expiry - need time to mean-revert
        hours_left = market.hours_until_expiry
        if hours_left is None:
            continue
        if hours_left < MIN_HOURS_TO_EXPIRY:
            continue

        # Get order book - explicit None check
        book = ctx.book(token_id)
        if book is None:
            continue

        # Need both sides to calculate mid and spread
        if book.best_bid is None:
            continue
        if book.best_ask is None:
            continue

        bid = book.best_bid
        ask = book.best_ask

        # Filter by price range - avoid near-resolved markets
        mid = (bid + ask) / Decimal("2")
        if mid < MIN_PRICE:
            continue
        if mid > MAX_PRICE:
            continue

        # Filter by spread - need edge but not too wide
        market_spread = ask - bid
        spread_pct = market_spread / mid
        if spread_pct < MIN_SPREAD_PCT:
            continue
        if spread_pct > MAX_SPREAD_PCT:
            continue

        # Get current position - explicit None handling
        position = ctx.position(token_id)
        position_size = Decimal("0")
        if position is not None:
            position_size = position.size

        # Calculate our quote spread (half on each side)
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
            continue

        # Clamp prices to valid range [0.01, 0.99]
        if my_bid < Decimal("0.01"):
            my_bid = Decimal("0.01")
        if my_ask > Decimal("0.99"):
            my_ask = Decimal("0.99")

        # Determine what to quote based on position
        can_buy = position_size < MAX_POSITION
        neg_max_position = Decimal("0") - MAX_POSITION
        can_sell = position_size > neg_max_position

        # Calculate sizes - don't exceed position limits
        buy_size = ORDER_SIZE
        remaining_buy = MAX_POSITION - position_size
        if remaining_buy < buy_size:
            buy_size = remaining_buy

        sell_size = ORDER_SIZE
        remaining_sell = MAX_POSITION + position_size  # How much we can sell/short
        if remaining_sell < sell_size:
            sell_size = remaining_sell

        # Cancel existing orders first
        signals.append(Cancel(token_id=token_id))

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

        tokens_quoted = tokens_quoted + 1

    return signals if signals else [Hold()]
