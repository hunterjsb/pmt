"""Scanner for high-certainty outcomes on markets expiring soon."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

from polymarket import Market, Token, clob, gamma


@dataclass
class ExpiringOpportunity:
    """Represents a high-certainty outcome on a market expiring soon."""

    market: Market
    token: Token
    price_pct: float
    end_date: str
    hours_until_expiry: float
    slug: str


def parse_end_date(end_date_str: str | None) -> datetime | None:
    """Parse end date string to datetime object.

    Args:
        end_date_str: ISO 8601 date string (e.g., "2024-12-30T23:59:59Z")

    Returns:
        datetime object or None if parsing fails
    """
    if not end_date_str:
        return None

    try:
        # Handle ISO 8601 format with Z suffix
        if end_date_str.endswith("Z"):
            end_date_str = end_date_str[:-1] + "+00:00"

        return datetime.fromisoformat(end_date_str)
    except (ValueError, AttributeError):
        return None


def hours_until(end_date: datetime) -> float:
    """Calculate hours until end date from now.

    Args:
        end_date: Target datetime

    Returns:
        Hours remaining (can be negative if expired)
    """
    now = datetime.now(timezone.utc)
    delta = end_date - now
    return delta.total_seconds() / 3600


def find_expiring_opportunities(
    min_price_pct: float = 98.0,
    max_hours: float = 2.0,
) -> list[ExpiringOpportunity]:
    """Find high-certainty outcomes on markets expiring soon.

    Strategy: Look for outcomes priced at 98%+ that expire in 2 hours or less.
    These are "almost certain" outcomes that will resolve soon, offering quick,
    low-risk returns.

    Args:
        min_price_pct: Minimum outcome price percentage (default 98%)
        max_hours: Maximum hours until expiry (default 2 hours)

    Returns:
        List of expiring opportunities
    """
    opportunities = []

    # Get active markets from CLOB (increased limit)
    markets = clob.sampling_markets(limit=500)

    # Get market metadata from Gamma to check end dates (increased limit)
    gamma_markets = gamma.markets(limit=500, closed=False)

    # Build a lookup map: question -> market_data
    # Also try matching by slug and other fields for better coverage
    gamma_lookup = {}
    gamma_by_slug = {}
    for gm in gamma_markets:
        question = gm.get("question", "")
        slug = gm.get("slug", "")
        if question:
            gamma_lookup[question] = gm
        if slug:
            gamma_by_slug[slug] = gm

    for market in markets:
        # Try to find matching market data with end date
        market_data = gamma_lookup.get(market.question)

        # If no exact match, try fuzzy matching on the question
        if not market_data:
            # Try to find by partial match
            for q, data in gamma_lookup.items():
                if (
                    market.question.lower() in q.lower()
                    or q.lower() in market.question.lower()
                ):
                    market_data = data
                    break

        if not market_data:
            continue

        end_date_str = market_data.get("endDate")
        end_date = parse_end_date(end_date_str)

        if not end_date:
            continue

        hours_left = hours_until(end_date)

        # Skip if not expiring soon enough or already expired
        if hours_left < 0 or hours_left > max_hours:
            continue

        # Check each token for high certainty outcomes
        for token in market.tokens:
            if token.price is None:
                continue

            price_pct = token.price * 100

            if price_pct >= min_price_pct:
                opportunities.append(
                    ExpiringOpportunity(
                        market=market,
                        token=token,
                        price_pct=price_pct,
                        end_date=end_date_str or "Unknown",
                        hours_until_expiry=hours_left,
                        slug=market_data.get("slug", ""),
                    )
                )

    # Sort by hours until expiry (soonest first)
    opportunities.sort(key=lambda x: x.hours_until_expiry)

    return opportunities


def calculate_max_return(price_pct: float, hours_left: float) -> dict:
    """Calculate maximum potential return for an expiring opportunity.

    Args:
        price_pct: Current price percentage (e.g., 98.5)
        hours_left: Hours until market expires

    Returns:
        Dictionary with return_pct, hourly_rate, and break_even info
    """
    # Maximum gain if outcome resolves to 100%
    max_return_pct = (100 - price_pct) / price_pct * 100

    # Hourly rate of return
    hourly_rate = max_return_pct / hours_left if hours_left > 0 else 0

    # How much the price can drop before we lose money
    break_even_drop = 100 - price_pct

    return {
        "max_return_pct": max_return_pct,
        "hourly_rate_pct": hourly_rate,
        "break_even_drop_pct": break_even_drop,
    }


if __name__ == "__main__":
    print("🕐 Scanning for high-certainty expiring markets...")
    print("   Looking for: 98%+ certainty, expiring within 2 hours\n")

    opportunities = find_expiring_opportunities(min_price_pct=98.0, max_hours=2.0)

    if not opportunities:
        print("No opportunities found matching criteria.")
    else:
        print(f"Found {len(opportunities)} opportunities:\n")

        for opp in opportunities:
            returns = calculate_max_return(opp.price_pct, opp.hours_until_expiry)

            print(f"📊 {opp.market.question[:60]}...")
            print(f"   Outcome: {opp.token.outcome} @ {opp.price_pct:.2f}%")
            print(f"   Expires in: {opp.hours_until_expiry:.1f} hours")
            print(f"   Max return: {returns['max_return_pct']:.2f}%")
            print(f"   Hourly rate: {returns['hourly_rate_pct']:.2f}%/hr")
            print()
