"""CLI script for running the volume cliff scanner."""

import argparse

from rich.console import Console

from strategies.scanner import scan_continuous, scan_once

console = Console()


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Scan Polymarket for volume cliff opportunities (ask ladder gaps)"
    )
    parser.add_argument(
        "--min",
        type=float,
        default=85.0,
        help="Minimum outcome price percentage (default: 85.0)",
    )
    parser.add_argument(
        "--max",
        type=float,
        default=99.0,
        help="Maximum outcome price percentage (default: 99.0)",
    )
    parser.add_argument(
        "--volume-jump",
        type=float,
        default=2000.0,
        help="Minimum dollar value jump to consider a cliff (default: 2000.0)",
    )
    parser.add_argument(
        "--price-gap",
        type=float,
        default=2.0,
        help="Minimum price gap in cents (default: 2.0)",
    )
    parser.add_argument(
        "--interval",
        type=int,
        default=30,
        help="Seconds between scans (default: 30)",
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="Run once and exit (don't loop)",
    )
    parser.add_argument(
        "--limit",
        type=int,
        help="Maximum number of scans (default: infinite)",
    )

    args = parser.parse_args()

    console.print("[bold cyan]🔍 Polymarket Volume Cliff Scanner[/bold cyan]")
    console.print(f"   Outcome Range: {args.min}% - {args.max}%")
    console.print(f"   Min Volume Jump: ${args.volume_jump:,.0f}")
    console.print(f"   Min Price Gap: {args.price_gap}¢")

    if args.once:
        console.print("   Mode: Single scan\n")
        from strategies.scanner import create_opportunities_table

        opportunities = scan_once(
            min_pct=args.min,
            max_pct=args.max,
            min_volume_jump=args.volume_jump,
            min_price_gap_cents=args.price_gap,
        )
        table = create_opportunities_table(opportunities)
        console.print(table)
        console.print(f"\n[dim]Found {len(opportunities)} opportunities[/dim]")
    else:
        console.print(f"   Interval: {args.interval}s")
        if args.limit:
            console.print(f"   Max scans: {args.limit}")
        console.print("   [dim]Press Ctrl+C to stop[/dim]\n")

        scan_continuous(
            min_pct=args.min,
            max_pct=args.max,
            min_volume_jump=args.volume_jump,
            min_price_gap_cents=args.price_gap,
            interval=args.interval,
            max_iterations=args.limit,
        )


if __name__ == "__main__":
    main()
