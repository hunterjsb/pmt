"""Sure Bets Strategy - Low-risk betting on high-certainty expiring markets.

Strategy:
    - Find markets priced at 95%+ that are expiring within 48 hours
    - Buy the high-certainty outcome
    - Wait for resolution, collect 1-5% profit

Risk profile:
    - Very low: Only bet on near-certain outcomes
    - Main risk: Market doesn't resolve as expected (rare at 95%+)
    - Expected win rate: 95%+
"""

from decimal import Decimal

# from datetime import datetime, timezone
from pmstrat import Buy, Context, Hold, Signal, Urgency, strategy

# Strategy parameters are defined in the decorator and transpiled to Rust constants
# These module-level constants are used for Python-side testing/simulation
MIN_CERTAINTY = Decimal("0.95")
MAX_CERTAINTY = Decimal("0.99")
MAX_HOURS_TO_EXPIRY = 48.0
MIN_LIQUIDITY = 500.0
MAX_POSITION_SIZE = Decimal("100")
MIN_ORDER_SIZE = Decimal("10")
MAX_SINGLE_ORDER = Decimal("50")
MIN_EXPECTED_RETURN = Decimal("0.01")

# Keywords for excluded markets (esports, sports, etc.)
EXCLUDE_KEYWORDS = [
    # Esports
    "dota",
    "counter-strike",
    "valorant",
    "league of legends",
    "overwatch",
    "csgo",
    "cs2",
    "lol",
    "pubg",
    "fortnite",
    "rocket league",
    "starcraft",
    "kill handicap",
    "map handicap",
    "game handicap",
    "games total",
    "bo3",
    "bo5",
    "esports",
    "e-sports",
    # Soccer/Football - generic patterns that catch most matches
    " vs ",
    " vs. ",
    " fc",
    " afc",
    " cf",
    "united fc",
    "city fc",
    "o/u 2.5",
    "o/u 3.5",
    "o/u 4.5",
    "o/u 1.5",
    "o/u 0.5",
    "over/under",
    "over 0.5",
    "over 1.5",
    "over 2.5",
    "over 3.5",
    "over 4.5",
    "under 0.5",
    "under 1.5",
    "under 2.5",
    "under 3.5",
    "under 4.5",
    # Leagues and competitions
    "premier league",
    "epl",
    "champions league",
    "la liga",
    "bundesliga",
    "serie a",
    "ligue 1",
    "eredivisie",
    "championship",
    "league one",
    "league two",
    "copa america",
    "euros",
    "euro 2024",
    "euro 2025",
    "world cup",
    # US Sports
    "nfl",
    "nba",
    "mlb",
    "nhl",
    "mls",
    "ufc",
    "wwe",
    "ncaa",
    "super bowl",
    "stanley cup",
    "world series",
    # Other sports
    "fifa",
    "olympics",
    "tennis",
    "golf",
    "boxing",
    "mma",
    "f1",
    "nascar",
    "cricket",
    "rugby",
    "atp",
    "wta",
    "pga",
]


@strategy(
    name="sure_bets",
    tokens=[],  # Dynamic - we scan for opportunities
    tick_interval_ms=60000,  # Check every minute
    params={
        "MIN_CERTAINTY": Decimal("0.95"),
        "MAX_CERTAINTY": Decimal("0.99"),
        "MAX_HOURS_TO_EXPIRY": 48.0,
        "MIN_LIQUIDITY": 500.0,
        "MAX_POSITION_SIZE": Decimal("100"),
        "MIN_ORDER_SIZE": Decimal("10"),
        "MAX_SINGLE_ORDER": Decimal("50"),
        "MIN_EXPECTED_RETURN": Decimal("0.01"),
        "EXCLUDE_KEYWORDS": EXCLUDE_KEYWORDS,
    },
)
def on_tick(ctx: Context) -> list[Signal]:
    """Scan for high-certainty expiring markets and generate buy signals."""
    signals: list[Signal] = []

    for token_id, market in ctx.markets.items():
        # Filter by excluded keywords (strategy does its own filtering)
        q_lower = market.question.lower()
        excluded = False
        for keyword in EXCLUDE_KEYWORDS:
            if keyword in q_lower:
                excluded = True
        if excluded:
            continue

        # Filter by liquidity (if available) - explicit Option handling
        liquidity = market.liquidity
        if liquidity is not None:
            if liquidity < MIN_LIQUIDITY:
                continue

        # Skip if no end date
        if market.end_date is None:
            continue

        # Check if expiring soon - explicit Option unwrap
        hours_left = market.hours_until_expiry
        if hours_left is None:
            continue
        if hours_left < 0.0:
            continue
        if hours_left > MAX_HOURS_TO_EXPIRY:
            continue

        # Get order book - explicit Option unwrap
        book = ctx.book(token_id)
        if book is None:
            continue

        # Get best ask price - explicit Option unwrap
        ask_price = book.best_ask
        if ask_price is None:
            continue

        # Check if high certainty (price >= MIN_CERTAINTY and <= MAX_CERTAINTY)
        if ask_price < MIN_CERTAINTY:
            continue
        if ask_price > MAX_CERTAINTY:
            continue

        # Calculate expected return
        # If we buy at ask and it resolves to 1.00, our profit is (1.00 - ask) / ask
        expected_return = (Decimal("1.00") - ask_price) / ask_price
        if expected_return < MIN_EXPECTED_RETURN:
            continue

        # Check if we already have a position - explicit Option handling
        position = ctx.position(token_id)
        current_size = Decimal(0)
        if position is not None:
            current_size = position.size

        # Don't exceed max position
        if current_size >= MAX_POSITION_SIZE:
            continue

        # Calculate how much more we can buy
        remaining = MAX_POSITION_SIZE - current_size

        # Get available ask size
        ask_size = book.ask_size

        # Calculate order size (min of remaining, ask_size, MAX_SINGLE_ORDER)
        # Explicit comparisons instead of min() for transpiler
        size = remaining
        if ask_size < size:
            size = ask_size
        if MAX_SINGLE_ORDER < size:
            size = MAX_SINGLE_ORDER

        if size < MIN_ORDER_SIZE:
            continue

        # Generate buy signal
        signals.append(
            Buy(
                token_id=token_id,
                price=ask_price,
                size=size,
                urgency=Urgency.MEDIUM,
            )
        )

    return signals if signals else [Hold()]


def is_excluded(question_lower: str) -> bool:
    """Check if question contains excluded keywords (helper for non-transpiled use)."""
    for keyword in EXCLUDE_KEYWORDS:
        if keyword in question_lower:
            return True
    return False


def scan_opportunities(ctx: Context) -> list[dict]:
    """Utility function to list current opportunities without trading.

    Returns list of dicts with opportunity details.
    """
    opportunities = []

    for token_id, market in ctx.markets.items():
        if market.end_date is None:
            continue

        hours_left = market.hours_until_expiry
        if hours_left is None:
            continue
        if hours_left < 0 or hours_left > MAX_HOURS_TO_EXPIRY:
            continue

        book = ctx.book(token_id)
        if book is None:
            continue
        if book.best_ask is None:
            continue

        ask_price = book.best_ask
        if ask_price < MIN_CERTAINTY:
            continue
        if ask_price > MAX_CERTAINTY:
            continue

        expected_return = (Decimal("1.00") - ask_price) / ask_price
        hourly_return = expected_return / Decimal(str(max(hours_left, 0.1)))

        opportunities.append(
            {
                "token_id": token_id,
                "question": market.question,
                "outcome": market.outcome,
                "ask_price": float(ask_price),
                "ask_size": float(book.ask_size),
                "hours_left": hours_left,
                "expected_return_pct": float(expected_return * 100),
                "hourly_return_pct": float(hourly_return * 100),
            }
        )

    # Sort by hourly return (best first)
    opportunities.sort(key=lambda x: x["hourly_return_pct"], reverse=True)
    return opportunities
