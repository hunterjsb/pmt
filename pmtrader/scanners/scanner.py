"""Market scanner for finding order book volume cliff opportunities."""

from __future__ import annotations

import time
from dataclasses import dataclass

from rich.live import Live
from rich.table import Table

from polymarket import Market, OrderBook, Token, clob


@dataclass
class VolumeCliffOpportunity:
    """Represents a volume cliff arbitrage opportunity."""

    market: Market
    token: Token
    price_pct: float
    buy_levels: list[tuple[float, float, float]]  # (price, size, dollar_value)
    cliff_level: tuple[float, float, float]  # (price, size, dollar_value) - thick level
    dollar_value_jump: float  # Jump in dollar value from thin to thick
    potential_resale_price: float  # Where we can place our ask


def analyze_order_book(
    market: Market,
    token: Token,
    order_book: OrderBook,
    min_volume_jump: float = 2000.0,
    min_price_gap_cents: float = 2.0,
) -> VolumeCliffOpportunity | None:
    """Analyze order book for volume cliff opportunities.

    Strategy: Find thin ask levels followed by a large jump in dollar volume (price × size).
    Example: $60 @ 93¢, $50 @ 94¢, then $3,000 @ 96¢ (cliff!)
    We buy at 93¢ and 94¢, then resell at 95¢ (just below the cliff).

    Args:
        market: The market
        token: The token/outcome
        order_book: Order book for the token
        min_volume_jump: Minimum dollar value jump to consider a "cliff"
        min_price_gap_cents: Minimum price gap (in cents) to make it worth the trade

    Returns:
        VolumeCliffOpportunity if found, None otherwise
    """
    if not order_book.asks or len(order_book.asks) < 2:
        return None

    # Calculate dollar value (price × volume) for each ask level
    asks_with_value = []
    for level in order_book.asks[:10]:
        dollar_value = level.price * level.size
        asks_with_value.append((level.price, level.size, dollar_value))

    # Look for volume cliffs: significant jump in dollar value between levels
    for i in range(len(asks_with_value) - 1):
        current_price, current_size, current_value = asks_with_value[i]
        next_price, next_size, next_value = asks_with_value[i + 1]

        # Check if there's a significant volume jump
        volume_jump = next_value - current_value

        if volume_jump < min_volume_jump:
            continue

        # Check if there's a meaningful price gap (in cents)
        # In prediction markets, asks often descend, so use absolute value
        price_gap_cents = abs(next_price - current_price) * 100

        if price_gap_cents < min_price_gap_cents:
            continue

        # Found a cliff! Collect all thin levels up to this point
        buy_levels = asks_with_value[: i + 1]

        # Calculate where we can place our ask
        # For descending asks (normal in prediction markets), we resell between the levels
        # For ascending asks, we resell just below the cliff
        if next_price > current_price:
            # Ascending: resell just below the thick level
            resale_price = next_price - 0.01
        else:
            # Descending: resell between thin and thick (average them)
            resale_price = (current_price + next_price) / 2

        # Calculate weighted average buy price
        total_buy_volume = sum(size for _, size, _ in buy_levels)
        if total_buy_volume == 0:
            continue

        avg_buy_price = (
            sum(price * size for price, size, _ in buy_levels) / total_buy_volume
        )

        # Make sure resale price is higher than buy price
        if resale_price <= avg_buy_price:
            continue

        return VolumeCliffOpportunity(
            market=market,
            token=token,
            price_pct=token.price * 100 if token.price else 0,
            buy_levels=buy_levels,
            cliff_level=(next_price, next_size, next_value),
            dollar_value_jump=volume_jump,
            potential_resale_price=resale_price,
        )

    return None


def find_volume_cliff_opportunities(
    markets: list[Market],
    min_pct: float = 85.0,
    max_pct: float = 99.0,
    **kwargs,
) -> list[VolumeCliffOpportunity]:
    """Find volume cliff opportunities in high-probability outcomes."""
    opportunities = []

    for market in markets:
        for token in market.tokens:
            if token.price is None:
                continue

            price_pct = token.price * 100

            # Focus on high-probability outcomes (the side we'd bet on)
            if not (min_pct <= price_pct <= max_pct):
                continue

            try:
                # Fetch order book for this token
                order_book = clob.order_book(token.token_id, token.outcome)

                # Analyze for volume cliff opportunity
                opp = analyze_order_book(market, token, order_book, **kwargs)
                if opp:
                    opportunities.append(opp)

            except Exception:
                # Skip tokens with issues fetching order book
                continue

    return opportunities


def create_opportunities_table(opportunities: list[VolumeCliffOpportunity]) -> Table:
    """Create a rich table displaying volume cliff opportunities."""
    table = Table(
        title="🎯 Volume Cliff Opportunities (Ask Ladder Gaps)", show_lines=True
    )

    table.add_column("Market", style="cyan", max_width=30)
    table.add_column("Outcome", style="yellow", justify="center", max_width=8)
    table.add_column("Buy Levels", style="magenta", max_width=25)
    table.add_column("Cliff", style="red bold", max_width=20)
    table.add_column("Resale @", style="green bold", justify="right")
    table.add_column("$ Jump", style="blue", justify="right")

    for opp in opportunities:
        # Truncate long questions
        question = opp.market.question
        if len(question) > 30:
            question = question[:27] + "..."

        # Format buy levels (show last 3 for space)
        if len(opp.buy_levels) > 3:
            buy_str = f"...{len(opp.buy_levels) - 3} more\n"
            buy_str += "\n".join(
                f"${val:,.0f} @ {price:.1%}" for price, size, val in opp.buy_levels[-3:]
            )
        else:
            buy_str = "\n".join(
                f"${val:,.0f} @ {price:.1%}" for price, size, val in opp.buy_levels
            )

        # Format cliff level
        cliff_price, cliff_size, cliff_val = opp.cliff_level
        cliff_str = f"${cliff_val:,.0f}\n@ {cliff_price:.1%}"

        # Resale price
        resale_str = f"{opp.potential_resale_price:.1%}"

        # Dollar jump
        jump_str = f"+${opp.dollar_value_jump:,.0f}"

        table.add_row(
            question,
            opp.token.outcome,
            buy_str,
            cliff_str,
            resale_str,
            jump_str,
        )

    return table


def scan_once(
    min_pct: float = 85.0,
    max_pct: float = 99.0,
    min_volume_jump: float = 2000.0,
    min_price_gap_cents: float = 2.0,
) -> list[VolumeCliffOpportunity]:
    """Perform a single scan of markets for volume cliff opportunities."""
    markets = clob.sampling_markets(limit=100)
    opportunities = find_volume_cliff_opportunities(
        markets,
        min_pct=min_pct,
        max_pct=max_pct,
        min_volume_jump=min_volume_jump,
        min_price_gap_cents=min_price_gap_cents,
    )
    return opportunities


def scan_continuous(
    min_pct: float = 85.0,
    max_pct: float = 99.0,
    min_volume_jump: float = 2000.0,
    min_price_gap_cents: float = 2.0,
    interval: int = 30,
    max_iterations: int | None = None,
) -> None:
    """Continuously scan for volume cliff opportunities.

    Args:
        min_pct: Minimum outcome price percentage
        max_pct: Maximum outcome price percentage
        min_volume_jump: Minimum dollar value jump to consider a cliff
        min_price_gap_cents: Minimum price gap in cents
        interval: Seconds between scans
        max_iterations: Maximum number of scans (None = infinite)
    """
    iteration = 0
    seen_opportunities = set()  # Track (market_question, outcome) pairs

    with Live(refresh_per_second=1) as live:
        while max_iterations is None or iteration < max_iterations:
            try:
                opportunities = scan_once(
                    min_pct=min_pct,
                    max_pct=max_pct,
                    min_volume_jump=min_volume_jump,
                    min_price_gap_cents=min_price_gap_cents,
                )

                # Filter out opportunities we've already seen
                new_opportunities = []
                for opp in opportunities:
                    key = (opp.market.question, opp.token.outcome)
                    if key not in seen_opportunities:
                        seen_opportunities.add(key)
                        new_opportunities.append(opp)

                table = create_opportunities_table(new_opportunities)

                # Add scan info to table
                table.caption = (
                    f"[dim]Scan #{iteration + 1} | "
                    f"Found {len(new_opportunities)} NEW opportunities "
                    f"({len(opportunities)} total this scan) | "
                    f"Next scan in {interval}s[/dim]"
                )

                live.update(table)

                iteration += 1
                time.sleep(interval)

            except KeyboardInterrupt:
                break
            except Exception as e:
                print(f"\n[red]Error during scan: {e}[/red]")
                time.sleep(interval)


if __name__ == "__main__":
    # Run scanner when module is executed directly
    print("🔍 Starting volume cliff scanner...")
    print("   Looking for large jumps in dollar volume (price × size)")
    print("   Outcome range: 85-99%")
    print("   Press Ctrl+C to stop\n")

    scan_continuous(
        min_pct=85.0, max_pct=99.0, min_volume_jump=2000.0, min_price_gap_cents=2.0
    )
