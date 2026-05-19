"""Portfolio view: performance, exposure, and thesis-level correlation.

Performance: per-position P&L from data-api (already pre-computed).
Exposure: total capital deployed in filled positions + locked in resting orders.
Correlation: groups positions by shared theme keywords so you can see net
            exposure to a thesis (e.g. "pandemic-yes" across hantavirus +
            coronavirus markets, even though they're separate events).

Usage:
    uv run python portfolio.py
    uv run python portfolio.py --orders         # include open orders
    uv run python portfolio.py --themes pandemic,btc,ai
"""

from __future__ import annotations

import argparse
import os
import re
import sys
from collections import defaultdict

import requests
from dotenv import load_dotenv
from rich.console import Console
from rich.table import Table

DATA_API = "https://data-api.polymarket.com"
console = Console()


def _known_token_labels() -> dict[str, str]:
    """Build token_id → "Market name (YES|NO)" map from polymarket.markets."""
    from polymarket import markets as M

    labels: dict[str, str] = {}
    for attr in dir(M):
        obj = getattr(M, attr)
        if not hasattr(obj, "yes_token"):
            continue
        labels[obj.yes_token] = f"{obj.name[:30]} YES"
        labels[obj.no_token] = f"{obj.name[:30]} NO"
    return labels

# Default theme buckets — title-keyword regexes. Edit/extend as your book grows.
DEFAULT_THEMES = {
    "hantavirus":  r"\bhantavirus\b",
    "pandemic":    r"\bpandemic\b",
    "vaccine":     r"\bvaccine\b",
    "coronavirus": r"\bcoronavirus\b|\bcovid\b",
    "btc":         r"\bbitcoin\b|\bBTC\b",
    "eth":         r"\bethereum\b|\bETH\b|\bether\b",
    "ai":          r"\bAGI\b|\bOpenAI\b|\bAnthropic\b|\bChatGPT\b",
    "bank-fail":   r"\bfail by\b",
    "trump":       r"\bTrump\b|\bDJT\b",
}


def pnl_color(pnl: float) -> str:
    return "green" if pnl >= 0 else "red"


def fetch_positions(funder: str) -> list[dict]:
    r = requests.get(f"{DATA_API}/positions", params={"user": funder, "limit": 200}, timeout=10)
    r.raise_for_status()
    return r.json() or []


def fetch_open_orders():
    # L2-authed; reuse our v2 client.
    from polymarket.clob_v2 import create_authenticated_clob_v2

    client = create_authenticated_clob_v2()
    raw = client.get_open_orders()
    return raw.data if hasattr(raw, "data") else (raw or [])


def show_positions(positions: list[dict]) -> tuple[float, float, float]:
    table = Table(title="Positions", show_lines=False)
    table.add_column("Market", max_width=42)
    table.add_column("Side", justify="center")
    table.add_column("Size", justify="right")
    table.add_column("Avg", justify="right")
    table.add_column("Cur", justify="right")
    table.add_column("Cost", justify="right")
    table.add_column("Value", justify="right")
    table.add_column("PnL", justify="right")

    total_cost = total_value = total_pnl = 0.0
    for p in sorted(positions, key=lambda x: x.get("cashPnl", 0)):
        cost = p["size"] * p["avgPrice"]
        pnl = p.get("cashPnl", p["currentValue"] - cost)
        c = pnl_color(pnl)
        total_cost += cost
        total_value += p["currentValue"]
        total_pnl += pnl
        table.add_row(
            p["title"][:42],
            p["outcome"],
            f"{p['size']:.0f}",
            f"${p['avgPrice']:.4f}",
            f"${p['curPrice']:.4f}",
            f"${cost:.2f}",
            f"${p['currentValue']:.2f}",
            f"[{c}]${pnl:+.2f}[/{c}]",
        )
    table.add_section()
    table.add_row(
        "[bold]TOTAL[/bold]", "", "", "", "",
        f"[bold]${total_cost:.2f}[/bold]",
        f"[bold]${total_value:.2f}[/bold]",
        f"[bold {pnl_color(total_pnl)}]${total_pnl:+.2f}[/]",
    )
    console.print(table)
    return total_cost, total_value, total_pnl


def show_orders(orders: list) -> float:
    if not orders:
        console.print("[dim]No open orders.[/dim]")
        return 0.0
    token_labels = _known_token_labels()
    table = Table(title="Open Orders")
    table.add_column("Side", justify="center")
    table.add_column("Size", justify="right")
    table.add_column("Price", justify="right")
    table.add_column("Notional", justify="right")
    table.add_column("Market", max_width=34)
    table.add_column("ID", max_width=14)
    locked_cash = 0.0
    for o in orders:
        d = o if isinstance(o, dict) else getattr(o, "__dict__", {})
        side = d.get("side", "?")
        size = float(d.get("original_size", 0))
        price = float(d.get("price", 0))
        notional = size * price
        if side == "BUY":
            locked_cash += notional
        asset = str(d.get("asset_id", ""))
        market_label = token_labels.get(asset, "…" + asset[-10:])
        table.add_row(
            side,
            f"{size:.0f}",
            f"${price:.4f}",
            f"${notional:.2f}",
            market_label,
            str(d.get("id", ""))[:12],
        )
    table.add_section()
    table.add_row("", "", "[bold]Locked (BUY)[/bold]", f"[bold]${locked_cash:.2f}[/bold]", "", "")
    console.print(table)
    return locked_cash


def show_exposure(positions: list[dict]) -> None:
    by_side = defaultdict(lambda: {"cost": 0.0, "value": 0.0})
    for p in positions:
        cost = p["size"] * p["avgPrice"]
        by_side[p["outcome"]]["cost"] += cost
        by_side[p["outcome"]]["value"] += p["currentValue"]

    table = Table(title="Exposure by Outcome Side")
    table.add_column("Side")
    table.add_column("Cost", justify="right")
    table.add_column("Value", justify="right")
    table.add_column("PnL", justify="right")
    for side, x in sorted(by_side.items()):
        pnl = x["value"] - x["cost"]
        table.add_row(side, f"${x['cost']:.2f}", f"${x['value']:.2f}",
                      f"[{pnl_color(pnl)}]${pnl:+.2f}[/]")
    console.print(table)


def show_themes(positions: list[dict], themes: dict[str, str]) -> None:
    """Group positions by theme keyword regex. A position can match multiple themes."""
    table = Table(title="Exposure by Theme (positions can match multiple)")
    table.add_column("Theme")
    table.add_column("Positions", justify="right")
    table.add_column("YES cost", justify="right")
    table.add_column("NO cost", justify="right")
    table.add_column("Net value", justify="right")
    table.add_column("PnL", justify="right")

    for name, pattern in themes.items():
        rx = re.compile(pattern, re.IGNORECASE)
        matching = [p for p in positions if rx.search(p["title"])]
        if not matching:
            continue
        yes_cost = sum(p["size"] * p["avgPrice"] for p in matching if p["outcome"] == "Yes")
        no_cost = sum(p["size"] * p["avgPrice"] for p in matching if p["outcome"] == "No")
        value = sum(p["currentValue"] for p in matching)
        pnl = value - (yes_cost + no_cost)
        table.add_row(
            name, str(len(matching)),
            f"${yes_cost:.2f}", f"${no_cost:.2f}",
            f"${value:.2f}", f"[{pnl_color(pnl)}]${pnl:+.2f}[/]",
        )
    console.print(table)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--orders", action="store_true", help="Also show open orders + locked capital")
    parser.add_argument(
        "--themes", help="Comma-separated theme names from DEFAULT_THEMES (default: all matching)"
    )
    parser.add_argument("--funder", help="Override PM_FUNDER_ADDRESS")
    args = parser.parse_args()

    load_dotenv()
    funder = args.funder or os.environ.get("PM_FUNDER_ADDRESS")
    if not funder:
        print("ERROR: set PM_FUNDER_ADDRESS or pass --funder", file=sys.stderr)
        return 1

    positions = fetch_positions(funder)
    if not positions:
        console.print("[dim]No positions.[/dim]")
        return 0

    total_cost, total_value, total_pnl = show_positions(positions)
    console.print()
    show_exposure(positions)
    console.print()

    selected_themes = DEFAULT_THEMES
    if args.themes:
        wanted = {t.strip() for t in args.themes.split(",")}
        selected_themes = {k: v for k, v in DEFAULT_THEMES.items() if k in wanted}
    show_themes(positions, selected_themes)

    if args.orders:
        console.print()
        locked = show_orders(fetch_open_orders())
        console.print(
            f"\n[bold]Summary[/bold]:  positions value ${total_value:.2f}  "
            f"+ locked in BUY orders ${locked:.2f}  "
            f"= ${total_value + locked:.2f} committed"
        )

    return 0


if __name__ == "__main__":
    sys.exit(main())
