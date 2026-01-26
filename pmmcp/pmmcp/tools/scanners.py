"""Scanner tools for finding trading opportunities."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import TYPE_CHECKING

from ..serialize import serialize

if TYPE_CHECKING:
    from fastmcp import FastMCP


def _parse_end_date(end_date_str: str | None) -> datetime | None:
    """Parse ISO 8601 date string to datetime."""
    if not end_date_str:
        return None
    try:
        if end_date_str.endswith("Z"):
            end_date_str = end_date_str[:-1] + "+00:00"
        return datetime.fromisoformat(end_date_str)
    except (ValueError, AttributeError):
        return None


def register(mcp: "FastMCP") -> None:
    """Register scanner tools with the MCP server."""

    @mcp.tool(
        name="market_overview",
        description="""Get a compact overview of available markets with LIVE prices.

Uses CLOB sampling_markets for accurate live prices (not stale Gamma data).
Scans markets and checks order books for actual liquidity.

Output includes:
- Total markets/outcomes scanned
- Price distribution (how many at each certainty level)
- High-certainty opportunities WITH asks available (ready to trade)

Use this to quickly see what's tradeable right now.""",
    )
    def market_overview(
        sample_size: int = 100,
        min_certainty: float = 85.0,
        max_opportunities: int = 15,
    ) -> dict:
        """Get compact market overview with live prices.

        Args:
            sample_size: Number of markets to sample from CLOB (default 100)
            min_certainty: Minimum price % to consider high-certainty (default 85)
            max_opportunities: Max opportunities to return (default 15)

        Returns:
            Summary dict with stats and tradeable opportunities
        """
        from polymarket import clob

        # Sample markets from CLOB (has live prices)
        markets = clob.sampling_markets(limit=sample_size)

        # Stats
        by_price = {"50-80%": 0, "80-90%": 0, "90-95%": 0, "95-98%": 0, "98%+": 0}
        total_outcomes = 0
        high_certainty = []

        for m in markets:
            for t in m.tokens:
                total_outcomes += 1
                if not t.price:
                    continue

                pct = float(t.price) * 100

                # Categorize by price (use higher of Yes/No price)
                effective_pct = pct if pct >= 50 else 100 - pct
                if effective_pct >= 98:
                    by_price["98%+"] += 1
                elif effective_pct >= 95:
                    by_price["95-98%"] += 1
                elif effective_pct >= 90:
                    by_price["90-95%"] += 1
                elif effective_pct >= 80:
                    by_price["80-90%"] += 1
                else:
                    by_price["50-80%"] += 1

                # Track high-certainty candidates
                # Only include tokens where pct >= min_certainty (the likely winner to BUY)
                # Skip tokens < 50% (that's the losing side - we'd need to buy the other token)
                if pct >= min_certainty:
                    high_certainty.append({
                        "pct": pct,
                        "token_id": t.token_id,
                        "q": (m.question or "")[:50],
                        "outcome": t.outcome,
                    })

        # Sort by certainty (highest first)
        high_certainty.sort(key=lambda x: -x["pct"])

        # Check order books for liquidity on top candidates
        opportunities = []
        for item in high_certainty:
            if len(opportunities) >= max_opportunities:
                break
            try:
                book = clob.order_book(item["token_id"])
                asks = book.asks or []
                if asks:
                    best_ask = min(float(a.price) for a in asks)
                    total_ask_size = sum(float(a.size) for a in asks)
                    # Potential gain = payout (1.00) - cost (best_ask)
                    potential_gain_pct = (1 - best_ask) * 100
                    opportunities.append({
                        "q": item["q"],
                        "outcome": item["outcome"],
                        "certainty": round(item["pct"], 1),
                        "best_ask": round(best_ask, 3),
                        "ask_depth": round(total_ask_size, 0),
                        "potential_gain_pct": round(potential_gain_pct, 1),
                        "token_id": item["token_id"],
                    })
            except Exception:
                continue

        return {
            "markets_sampled": len(markets),
            "total_outcomes": total_outcomes,
            "by_price": by_price,
            "high_certainty_found": len(high_certainty),
            "with_liquidity": len(opportunities),
            "opportunities": opportunities,
        }

    @mcp.tool(
        name="scan_expiring",
        description="""Find high-certainty outcomes on markets expiring soon.

Looks for outcomes priced at min_price_pct%+ where the market's endDate has
passed or is passing soon. Markets resolve shortly after their endDate.

Strategy: Buy high-probability outcomes (98%+) that are about to resolve,
capturing the remaining 1-2% as the price moves to 100%.

Returns list of opportunities with question, outcome, price, time until expiry.""",
    )
    def scan_expiring(
        min_price_pct: float = 98.0,
        max_hours: float = 2.0,
        max_events: int = 500,
        limit: int = 10,
        only_open: bool = True,
    ) -> list[dict]:
        """Find expiring market opportunities.

        Args:
            min_price_pct: Minimum outcome price percentage (default 98%)
            max_hours: Maximum hours after endDate to consider (default 2)
            max_events: Maximum events to scan (default 500)
            limit: Maximum results to return (default 10)
            only_open: Only return markets that haven't passed endDate yet (default True)

        Returns:
            List of ExpiringOpportunity dicts
        """
        # Import here to avoid circular imports and allow lazy loading
        from strategies.expiring import find_expiring_opportunities

        opportunities = find_expiring_opportunities(
            min_price_pct=min_price_pct,
            max_hours=max_hours,
            max_events=max_events,
        )

        # Filter to only open markets if requested
        if only_open:
            opportunities = [o for o in opportunities if o.hours_until_expiry > 0]

        # Limit results
        opportunities = opportunities[:limit]

        return serialize(opportunities)

    @mcp.tool(
        name="scan_volume_cliffs",
        description="""Find order book volume cliff opportunities.

Looks for thin ask levels followed by a large jump in dollar volume.
Example: $60 @ 93¢, $50 @ 94¢, then $3,000 @ 96¢ (cliff!)
Strategy: Buy at thin levels (93¢, 94¢), resell just below the cliff (95¢).

Returns opportunities with buy levels, cliff level, and potential resale price.""",
    )
    def scan_volume_cliffs(
        min_pct: float = 85.0,
        max_pct: float = 99.0,
        min_volume_jump: float = 2000.0,
        min_price_gap_cents: float = 2.0,
    ) -> list[dict]:
        """Find volume cliff opportunities.

        Args:
            min_pct: Minimum outcome price percentage (default 85%)
            max_pct: Maximum outcome price percentage (default 99%)
            min_volume_jump: Minimum dollar value jump for cliff (default $2000)
            min_price_gap_cents: Minimum price gap in cents (default 2¢)

        Returns:
            List of VolumeCliffOpportunity dicts
        """
        from strategies.scanner import scan_once

        opportunities = scan_once(
            min_pct=min_pct,
            max_pct=max_pct,
            min_volume_jump=min_volume_jump,
            min_price_gap_cents=min_price_gap_cents,
        )
        return serialize(opportunities)
