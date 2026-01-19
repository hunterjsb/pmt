"""CLI script for redeeming resolved Polymarket positions (gasless)."""

import argparse
import os
import sys
import time

from dotenv import load_dotenv
from rich.console import Console
from rich.table import Table

load_dotenv()

console = Console()


def get_gasless_client():
    """Create a gasless web3 client with Builder credentials."""
    from polymarket_apis import PolymarketGaslessWeb3Client
    from polymarket_apis.types.clob_types import ApiCreds

    private_key = os.environ.get("PM_PRIVATE_KEY")
    sig_type = int(os.environ.get("PM_SIGNATURE_TYPE", "1"))
    api_key = os.environ.get("PM_API_KEY")
    secret = os.environ.get("PM_SECRET")
    passphrase = os.environ.get("PM_PASSPHRASE")

    if not all([private_key, api_key, secret, passphrase]):
        return None

    return PolymarketGaslessWeb3Client(
        private_key=private_key,
        signature_type=sig_type,
        builder_creds=ApiCreds(key=api_key, secret=secret, passphrase=passphrase),
    )


def get_clob_client():
    """Create authenticated CLOB client for position queries."""
    from polymarket.clob import create_authenticated_clob
    return create_authenticated_clob()


def get_resolved_positions(clob) -> list[dict]:
    """Find all positions on resolved markets."""
    positions = clob.positions()

    resolved = []
    for i, p in enumerate(positions):
        condition_id = p["market"]
        try:
            if clob.is_condition_resolved(condition_id):
                numerators = clob.get_payout_numerators(condition_id)
                outcome_idx = 0 if p["outcome"].lower() == "yes" else 1
                won = numerators[outcome_idx] > 0

                resolved.append({
                    **p,
                    "condition_id": condition_id,
                    "won": won,
                    "payout": p["shares"] if won else 0.0,
                })
        except Exception:
            continue

        if (i + 1) % 10 == 0:
            time.sleep(0.5)

    return resolved


def display_positions(positions: list[dict]) -> None:
    """Display resolved positions in a table."""
    if not positions:
        console.print("[yellow]No resolved positions found.[/yellow]")
        return

    table = Table(title="Resolved Positions", show_lines=True)
    table.add_column("Market", style="cyan", max_width=40)
    table.add_column("Outcome", style="yellow", justify="center")
    table.add_column("Shares", style="magenta", justify="right")
    table.add_column("Status", justify="center")
    table.add_column("Payout", style="green bold", justify="right")

    for pos in positions:
        table.add_row(
            pos.get("market", "")[:40],
            pos["outcome"],
            f"{pos['shares']:.2f}",
            "[green]WON[/green]" if pos["won"] else "[red]LOST[/red]",
            f"${pos['payout']:.2f}" if pos["won"] else "$0.00",
        )

    console.print(table)

    total_won = sum(1 for p in positions if p["won"])
    total_lost = len(positions) - total_won
    total_payout = sum(p["payout"] for p in positions)

    console.print(f"\n[dim]{total_won} winning, {total_lost} losing | Payout: ${total_payout:.2f}[/dim]")


def redeem_position(gasless, pos: dict, dry_run: bool = False) -> bool:
    """Redeem a single position. Returns True on success."""
    condition_id = pos["condition_id"]
    shares = pos["shares"]
    amounts = [shares, 0] if pos["outcome"].lower() == "yes" else [0, shares]

    if dry_run:
        return True

    for attempt in range(3):
        try:
            gasless.redeem_position(condition_id=condition_id, amounts=amounts, neg_risk=False)
            return True
        except Exception as e:
            if "rate limit" in str(e).lower() or "too many" in str(e).lower():
                wait = 15 * (attempt + 1)
                console.print(f"[yellow]rate limited, waiting {wait}s...[/yellow]", end=" ")
                time.sleep(wait)
            else:
                console.print(f"[red]{e}[/red]")
                return False

    return False


def redeem_all(gasless, positions: list[dict], dry_run: bool = False) -> None:
    """Redeem all resolved positions."""
    if not positions:
        return

    mode = "[DRY RUN] " if dry_run else ""
    console.print(f"\n[bold]{mode}Redeeming {len(positions)} positions...[/bold]")

    success = 0
    for i, pos in enumerate(positions, 1):
        console.print(f"  [{i}/{len(positions)}] {pos['outcome']} {pos['shares']:.2f}... ", end="")

        if redeem_position(gasless, pos, dry_run=dry_run):
            console.print("[green]done[/green]" if not dry_run else "[blue]skip[/blue]")
            success += 1
        else:
            console.print("[red]failed[/red]")

        if not dry_run and i < len(positions):
            time.sleep(5)

    console.print(f"\n[bold]Completed: {success}/{len(positions)}[/bold]")


def main() -> None:
    parser = argparse.ArgumentParser(description="Redeem resolved Polymarket positions")
    parser.add_argument("--all", action="store_true", help="Redeem all without prompting")
    parser.add_argument("--dry-run", action="store_true", help="Show what would happen")
    args = parser.parse_args()

    console.print("[bold cyan]Position Redemption Tool[/bold cyan]")

    clob = get_clob_client()
    if clob is None:
        console.print("[red]Missing PM_PRIVATE_KEY or PM_FUNDER_ADDRESS[/red]")
        sys.exit(1)

    gasless = get_gasless_client()
    if gasless is None:
        console.print("[red]Missing PM_API_KEY, PM_SECRET, or PM_PASSPHRASE[/red]")
        sys.exit(1)

    resolved = get_resolved_positions(clob)
    display_positions(resolved)

    if not resolved:
        return

    if args.all or args.dry_run:
        redeem_all(gasless, resolved, dry_run=args.dry_run)
    else:
        console.print("\n[bold]Options:[/bold]")
        console.print("  [1] Redeem all")
        console.print("  [2] Dry run")
        console.print("  [3] Exit")

        try:
            choice = console.input("\n[bold]Select:[/bold] ")
            if choice == "1":
                redeem_all(gasless, resolved)
            elif choice == "2":
                redeem_all(gasless, resolved, dry_run=True)
        except (KeyboardInterrupt, EOFError):
            pass


if __name__ == "__main__":
    main()
