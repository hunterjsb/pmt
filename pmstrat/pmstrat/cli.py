"""CLI for pmstrat - run strategies and backtests."""

import sys
from decimal import Decimal

from rich.console import Console
from rich.table import Table

console = Console()


def main():
    """Main CLI entry point."""
    if len(sys.argv) < 2:
        print_usage()
        return

    command = sys.argv[1]

    if command == "backtest":
        run_backtest(sys.argv[2:])
    elif command == "scan":
        run_scan(sys.argv[2:])
    elif command == "simulate":
        run_simulate(sys.argv[2:])
    else:
        console.print(f"[red]Unknown command: {command}[/red]")
        print_usage()


def print_usage():
    console.print("""
[bold]pmstrat[/bold] - Strategy DSL and backtesting for Polymarket

[bold]Commands:[/bold]
  backtest <strategy.py> [--data FILE]  Run backtest on strategy
  scan                                   Scan for sure_bets opportunities (live)
  simulate [--ticks N]                   Run strategy on synthetic data

[bold]Examples:[/bold]
  pmstrat backtest strategies/sure_bets.py
  pmstrat scan
  pmstrat simulate --ticks 1000
""")


def run_backtest(args: list[str]):
    """Run a backtest."""
    from .backtest import Backtester, generate_synthetic_ticks, load_ticks_from_jsonl

    # Parse args
    strategy_path = None
    data_path = None
    num_ticks = 500

    i = 0
    while i < len(args):
        if args[i] == "--data" and i + 1 < len(args):
            data_path = args[i + 1]
            i += 2
        elif args[i] == "--ticks" and i + 1 < len(args):
            num_ticks = int(args[i + 1])
            i += 2
        elif not args[i].startswith("--"):
            strategy_path = args[i]
            i += 1
        else:
            i += 1

    # Load strategy
    if strategy_path:
        console.print(f"[dim]Loading strategy from {strategy_path}...[/dim]")
        # Import strategy module
        import importlib.util
        spec = importlib.util.spec_from_file_location("strategy", strategy_path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)

        # Find the strategy function
        strategy_fn = None
        for name in dir(module):
            obj = getattr(module, name)
            if hasattr(obj, "_strategy_meta"):
                strategy_fn = obj
                break

        if strategy_fn is None:
            console.print("[red]No @strategy decorated function found![/red]")
            return
    else:
        # Default to sure_bets
        console.print("[dim]Using default sure_bets strategy...[/dim]")
        from .strategies.sure_bets import on_tick
        strategy_fn = on_tick

    # Load or generate data
    if data_path:
        console.print(f"[dim]Loading data from {data_path}...[/dim]")
        ticks = load_ticks_from_jsonl(data_path)
    else:
        console.print(f"[dim]Generating {num_ticks} synthetic ticks...[/dim]")
        ticks = generate_synthetic_ticks(num_ticks=num_ticks)

    # Run backtest
    console.print("[bold]Running backtest...[/bold]")
    backtester = Backtester(
        strategy_fn=strategy_fn,
        initial_balance=Decimal("1000"),
    )
    result = backtester.run(ticks)

    # Print results
    console.print(result.summary())

    # Show fills table
    if result.fills:
        table = Table(title="Fills")
        table.add_column("Time", style="dim")
        table.add_column("Token", style="cyan")
        table.add_column("Side")
        table.add_column("Price", justify="right")
        table.add_column("Size", justify="right")

        for fill in result.fills[:20]:  # Show first 20
            table.add_row(
                fill.timestamp.strftime("%H:%M:%S"),
                fill.token_id[:16] + "...",
                "[green]BUY[/green]" if fill.side == "BUY" else "[red]SELL[/red]",
                f"{fill.price:.3f}",
                f"{fill.size:.0f}",
            )

        console.print(table)


def run_scan(args: list[str]):
    """Scan for live opportunities using Gamma API directly."""
    import json
    from datetime import datetime, timezone

    import httpx

    console.print("[bold]Scanning for sure_bets opportunities...[/bold]\n")

    # Parse args
    min_price = 95.0
    max_hours = 2.0

    i = 0
    while i < len(args):
        if args[i] == "--min-price" and i + 1 < len(args):
            min_price = float(args[i + 1])
            i += 2
        elif args[i] == "--max-hours" and i + 1 < len(args):
            max_hours = float(args[i + 1])
            i += 2
        else:
            i += 1

    try:
        # Fetch markets from Gamma API
        response = httpx.get(
            "https://gamma-api.polymarket.com/markets",
            params={"closed": "false", "limit": 500},
            timeout=30,
        )
        response.raise_for_status()
        markets = response.json()

        now = datetime.now(timezone.utc)
        opportunities = []

        for m in markets:
            end_date = m.get("endDate")
            if not end_date:
                continue

            # Parse end date
            try:
                if end_date.endswith("Z"):
                    end_date = end_date[:-1] + "+00:00"
                end_dt = datetime.fromisoformat(end_date)
                hours_left = (end_dt - now).total_seconds() / 3600
            except (ValueError, AttributeError):
                continue

            if hours_left < 0 or hours_left > max_hours:
                continue

            # Check outcome prices (these come as JSON strings from API)
            outcomes_raw = m.get("outcomes", "[]")
            prices_raw = m.get("outcomePrices", "[]")

            try:
                outcomes = json.loads(outcomes_raw) if isinstance(outcomes_raw, str) else outcomes_raw
                prices = json.loads(prices_raw) if isinstance(prices_raw, str) else prices_raw
            except json.JSONDecodeError:
                continue

            for i, price_str in enumerate(prices or []):
                try:
                    price_pct = float(price_str) * 100
                except (ValueError, TypeError):
                    continue

                if price_pct >= min_price:
                    exp_return = (100.0 - price_pct) / price_pct * 100
                    opportunities.append({
                        "question": m.get("question", "Unknown"),
                        "outcome": outcomes[i] if i < len(outcomes) else "Unknown",
                        "price_pct": price_pct,
                        "hours_left": hours_left,
                        "expected_return": exp_return,
                    })

        # Sort by expected return
        opportunities.sort(key=lambda x: x["expected_return"], reverse=True)

        if not opportunities:
            console.print(f"[yellow]No opportunities found (>={min_price:.0f}% expiring within {max_hours:.0f}h)[/yellow]")
            return

        table = Table(title=f"Found {len(opportunities)} Opportunities")
        table.add_column("Market", style="cyan", max_width=50)
        table.add_column("Outcome", style="yellow")
        table.add_column("Price", justify="right")
        table.add_column("Expiry", justify="right")
        table.add_column("Return", justify="right", style="green")

        for opp in opportunities[:15]:
            table.add_row(
                opp["question"][:50],
                opp["outcome"],
                f"{opp['price_pct']:.1f}%",
                f"{opp['hours_left']:.1f}h",
                f"{opp['expected_return']:.2f}%",
            )

        console.print(table)

    except httpx.HTTPError as e:
        console.print(f"[red]HTTP error: {e}[/red]")
    except Exception as e:
        console.print(f"[red]Error: {e}[/red]")


def run_simulate(args: list[str]):
    """Run strategy on synthetic data."""
    num_ticks = 500

    # Parse args
    i = 0
    while i < len(args):
        if args[i] == "--ticks" and i + 1 < len(args):
            num_ticks = int(args[i + 1])
            i += 2
        else:
            i += 1

    run_backtest(["--ticks", str(num_ticks)])


if __name__ == "__main__":
    main()
