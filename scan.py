"""CLI script for running market scanners."""

import argparse

from rich.console import Console
from rich.table import Table

console = Console()


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Scan Polymarket for trading opportunities"
    )

    # Add strategy selection as a subcommand
    subparsers = parser.add_subparsers(dest="strategy", help="Strategy to use")

    # Volume cliff scanner (original)
    cliff_parser = subparsers.add_parser(
        "cliff", help="Scan for volume cliff opportunities (ask ladder gaps)"
    )
    cliff_parser.add_argument(
        "--min",
        type=float,
        default=85.0,
        help="Minimum outcome price percentage (default: 85.0)",
    )
    cliff_parser.add_argument(
        "--max",
        type=float,
        default=99.0,
        help="Maximum outcome price percentage (default: 99.0)",
    )
    cliff_parser.add_argument(
        "--volume-jump",
        type=float,
        default=2000.0,
        help="Minimum dollar value jump to consider a cliff (default: 2000.0)",
    )
    cliff_parser.add_argument(
        "--price-gap",
        type=float,
        default=2.0,
        help="Minimum price gap in cents (default: 2.0)",
    )
    cliff_parser.add_argument(
        "--interval",
        type=int,
        default=30,
        help="Seconds between scans (default: 30)",
    )
    cliff_parser.add_argument(
        "--once",
        action="store_true",
        help="Run once and exit (don't loop)",
    )
    cliff_parser.add_argument(
        "--limit",
        type=int,
        help="Maximum number of scans (default: infinite)",
    )

    # Expiring markets scanner (new)
    expiring_parser = subparsers.add_parser(
        "expiring", help="Scan for high-certainty outcomes expiring soon"
    )
    expiring_parser.add_argument(
        "--min-price",
        type=float,
        default=98.0,
        help="Minimum outcome price percentage (default: 98.0)",
    )
    expiring_parser.add_argument(
        "--max-hours",
        type=float,
        default=2.0,
        help="Maximum hours until expiry (default: 2.0)",
    )
    expiring_parser.add_argument(
        "--interval",
        type=int,
        default=60,
        help="Seconds between scans (default: 60)",
    )
    expiring_parser.add_argument(
        "--once",
        action="store_true",
        help="Run once and exit (don't loop)",
    )
    expiring_parser.add_argument(
        "--verbose",
        "-v",
        action="store_true",
        help="Show verbose output (markets scanned, debug info)",
    )

    args = parser.parse_args()

    # Show help if no strategy selected
    if not args.strategy:
        parser.print_help()
        return

    # Run volume cliff scanner
    if args.strategy == "cliff":
        from strategies.scanner import (
            create_opportunities_table,
            scan_continuous,
            scan_once,
        )

        console.print("[bold cyan]🔍 Polymarket Volume Cliff Scanner[/bold cyan]")
        console.print(f"   Outcome Range: {args.min}% - {args.max}%")
        console.print(f"   Min Volume Jump: ${args.volume_jump:,.0f}")
        console.print(f"   Min Price Gap: {args.price_gap}¢")

        if args.once:
            console.print("   Mode: Single scan\n")
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

    # Run expiring markets scanner
    elif args.strategy == "expiring":
        from strategies.expiring import (
            calculate_max_return,
            find_expiring_opportunities,
        )

        console.print("[bold cyan]🕐 Polymarket Expiring Markets Scanner[/bold cyan]")
        console.print(f"   Min Price: {args.min_price}%")
        console.print(f"   Max Hours Until Expiry: {args.max_hours}")

        if args.verbose:
            console.print("   [dim]Verbose mode enabled[/dim]")

        if args.once:
            console.print("   Mode: Single scan\n")

            if args.verbose:
                console.print("\n[dim]Fetching markets...[/dim]")

            opportunities = find_expiring_opportunities(
                min_price_pct=args.min_price,
                max_hours=args.max_hours,
            )

            if args.verbose:
                from polymarket import clob, gamma

                markets = clob.sampling_markets(limit=500)
                gamma_markets = gamma.markets(limit=500, closed=False)
                console.print(f"[dim]Scanned {len(markets)} CLOB markets[/dim]")
                console.print(f"[dim]Scanned {len(gamma_markets)} Gamma markets[/dim]")

            if not opportunities:
                console.print(
                    "[yellow]No opportunities found matching criteria.[/yellow]"
                )
            else:
                # Create table
                table = Table(
                    title=f"🕐 Expiring Markets ({args.min_price}%+ certainty, <{args.max_hours}h)",
                    show_lines=True,
                )

                table.add_column("Market", style="cyan", max_width=35)
                table.add_column("Outcome", style="yellow", justify="center")
                table.add_column("Price", style="magenta", justify="right")
                table.add_column("Expires", style="red", justify="right")
                table.add_column("Max Return", style="green bold", justify="right")
                table.add_column("Rate/hr", style="blue", justify="right")

                for opp in opportunities:
                    returns = calculate_max_return(
                        opp.price_pct, opp.hours_until_expiry
                    )

                    question = opp.market.question
                    if len(question) > 35:
                        question = question[:32] + "..."

                    table.add_row(
                        question,
                        opp.token.outcome,
                        f"{opp.price_pct:.2f}%",
                        f"{opp.hours_until_expiry:.1f}h",
                        f"{returns['max_return_pct']:.2f}%",
                        f"{returns['hourly_rate_pct']:.2f}%",
                    )

                console.print(table)
                console.print(f"\n[dim]Found {len(opportunities)} opportunities[/dim]")
        else:
            import time

            from rich.live import Live

            console.print(f"   Interval: {args.interval}s")
            console.print("   [dim]Press Ctrl+C to stop[/dim]\n")

            iteration = 0
            with Live(refresh_per_second=1) as live:
                while True:
                    try:
                        opportunities = find_expiring_opportunities(
                            min_price_pct=args.min_price,
                            max_hours=args.max_hours,
                        )

                        # Create table
                        table = Table(
                            title=f"🕐 Expiring Markets ({args.min_price}%+ certainty, <{args.max_hours}h)",
                            show_lines=True,
                        )

                        table.add_column("Market", style="cyan", max_width=35)
                        table.add_column("Outcome", style="yellow", justify="center")
                        table.add_column("Price", style="magenta", justify="right")
                        table.add_column("Expires", style="red", justify="right")
                        table.add_column(
                            "Max Return", style="green bold", justify="right"
                        )
                        table.add_column("Rate/hr", style="blue", justify="right")

                        for opp in opportunities:
                            returns = calculate_max_return(
                                opp.price_pct, opp.hours_until_expiry
                            )

                            question = opp.market.question
                            if len(question) > 35:
                                question = question[:32] + "..."

                            table.add_row(
                                question,
                                opp.token.outcome,
                                f"{opp.price_pct:.2f}%",
                                f"{opp.hours_until_expiry:.1f}h",
                                f"{returns['max_return_pct']:.2f}%",
                                f"{returns['hourly_rate_pct']:.2f}%",
                            )

                        table.caption = (
                            f"[dim]Scan #{iteration + 1} | "
                            f"Found {len(opportunities)} opportunities | "
                            f"Next scan in {args.interval}s[/dim]"
                        )

                        live.update(table)

                        iteration += 1
                        time.sleep(args.interval)

                    except KeyboardInterrupt:
                        break
                    except Exception as e:
                        console.print(f"\n[red]Error during scan: {e}[/red]")
                        time.sleep(args.interval)


if __name__ == "__main__":
    main()
