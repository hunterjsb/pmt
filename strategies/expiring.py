"""Scanner for high-certainty outcomes on markets expiring soon."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from polymarket import clob, gamma


@dataclass
class ExpiringOpportunity:
    """Represents a high-certainty outcome on a market expiring soon."""

    question: str
    outcome: str
    token_id: str
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
    max_events: int = 2000,
) -> list[ExpiringOpportunity]:
    """Find high-certainty outcomes on markets expiring soon.

    Strategy: Look for outcomes priced at min_price_pct%+ that expire in max_hours or less.
    These are "almost certain" outcomes that will resolve soon, offering quick,
    low-risk returns.

    This implementation uses the Gamma /events endpoint which includes full market
    details with clobTokenIds, allowing us to scan ALL markets (not just sampling).

    Args:
        min_price_pct: Minimum outcome price percentage (default 98%)
        max_hours: Maximum hours until expiry (default 2 hours)
        max_events: Maximum number of events to fetch (default 2000)

    Returns:
        List of expiring opportunities
    """
    opportunities = []
    now = datetime.now(timezone.utc)

    # Fetch events with pagination
    events_fetched = 0
    offset = 0
    batch_size = 100

    print(f"Fetching events (max {max_events})...")

    # Calculate max end date (now + max_hours)
    max_end_date = now + timedelta(hours=max_hours)
    end_date_max_str = max_end_date.isoformat().replace("+00:00", "Z")

    while events_fetched < max_events:
        try:
            # Fetch batch of events with markets, filtered by end date
            events = gamma.events(
                limit=batch_size,
                offset=offset,
                closed=False,
                order="endDate",
                ascending=True,  # Get soonest expiring first
                end_date_max=end_date_max_str,
            )

            if not events:
                break  # No more events

            print(f"Processing events {offset} to {offset + len(events)}...")

            for event in events:
                events_fetched += 1

                # Check if event has markets
                markets = event.get("markets")
                if not markets:
                    continue

                # Get event end date
                event_end_date_str = event.get("endDate")
                event_end_date = parse_end_date(event_end_date_str)

                for market in markets:
                    # Get market details
                    question = market.get("question", "Unknown")
                    end_date_str = market.get("endDate") or event_end_date_str
                    clob_token_ids = market.get("clobTokenIds")
                    outcomes = market.get("outcomes")
                    outcome_prices = market.get("outcomePrices")
                    active = market.get("active", False)
                    closed = market.get("closed", False)
                    slug = market.get("slug", "")

                    # Skip if not active or already closed
                    if not active or closed:
                        continue

                    # Parse end date
                    end_date = parse_end_date(end_date_str)
                    if not end_date:
                        # Try event end date as fallback
                        end_date = event_end_date

                    if not end_date:
                        continue

                    hours_left = hours_until(end_date)

                    # Skip if expired or not expiring soon enough
                    if hours_left < 0 or hours_left > max_hours:
                        continue

                    # Parse outcomes and token IDs
                    if not clob_token_ids or not outcomes:
                        continue

                    try:
                        # Outcomes, prices, and token IDs are JSON-encoded strings
                        outcome_list = json.loads(outcomes)
                        token_id_list = (
                            json.loads(clob_token_ids)
                            if isinstance(clob_token_ids, str)
                            else clob_token_ids
                        )

                        # Parse outcome prices from market data only
                        if not outcome_prices:
                            continue  # Skip markets without price data

                        price_list = [float(p) for p in json.loads(outcome_prices)]

                        # Check each outcome for high certainty
                        for idx, (outcome, token_id, price) in enumerate(
                            zip(outcome_list, token_id_list, price_list)
                        ):
                            price_pct = price * 100

                            if price_pct >= min_price_pct:
                                opportunities.append(
                                    ExpiringOpportunity(
                                        question=question,
                                        outcome=outcome.strip(),
                                        token_id=token_id,
                                        price_pct=price_pct,
                                        end_date=end_date_str or "Unknown",
                                        hours_until_expiry=hours_left,
                                        slug=slug,
                                    )
                                )

                    except (ValueError, IndexError, AttributeError) as e:
                        # Skip markets with parsing issues
                        continue

            offset += batch_size

        except Exception as e:
            print(f"Warning: Error fetching events at offset {offset}: {e}")
            break

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

            print(f"📊 {opp.question[:60]}...")
            print(f"   Outcome: {opp.outcome} @ {opp.price_pct:.2f}%")
            print(f"   Expires in: {opp.hours_until_expiry:.1f} hours")
            print(f"   Max return: {returns['max_return_pct']:.2f}%")
            print(f"   Hourly rate: {returns['hourly_rate_pct']:.2f}%/hr")
            print(f"   Token ID: {opp.token_id}")
            print()
