"""Show Polymarket REWARD and YIELD income for your funder address.

Both endpoints are public (no Cognito/proxy needed). REWARD = maker liquidity
rewards. YIELD = interest on cash held in Polymarket.

Usage:
    uv run python rewards.py              # last 30 days, both types
    uv run python rewards.py --all        # everything
    uv run python rewards.py --type reward
"""

from __future__ import annotations

import argparse
import os
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone

import requests
from dotenv import load_dotenv
from rich.console import Console
from rich.table import Table

API = "https://data-api.polymarket.com/activity"
console = Console()


def fetch(funder: str, kind: str, limit: int = 500) -> list[dict]:
    r = requests.get(API, params={"user": funder, "type": kind, "limit": limit}, timeout=10)
    r.raise_for_status()
    return r.json() or []


def filter_since(events: list[dict], since_ts: float | None) -> list[dict]:
    if since_ts is None:
        return events
    return [e for e in events if e["timestamp"] >= since_ts]


def show_rewards(events: list[dict]) -> None:
    total = sum(e["usdcSize"] for e in events)
    table = Table(title=f"REWARD events  ({len(events)} events, ${total:.4f} total)")
    table.add_column("Date (UTC)")
    table.add_column("Amount", justify="right")
    table.add_column("TX")
    for e in events:
        ts = datetime.fromtimestamp(e["timestamp"], tz=timezone.utc)
        table.add_row(ts.strftime("%Y-%m-%d %H:%M"), f"${e['usdcSize']:.4f}", e["transactionHash"][:14])
    console.print(table)


def show_yield(events: list[dict]) -> None:
    by_month: dict[str, float] = defaultdict(float)
    for e in events:
        ts = datetime.fromtimestamp(e["timestamp"], tz=timezone.utc)
        by_month[ts.strftime("%Y-%m")] += e["usdcSize"]
    total = sum(by_month.values())
    table = Table(title=f"YIELD by month  ({len(events)} events, ${total:.4f} total)")
    table.add_column("Month")
    table.add_column("USDC", justify="right")
    for m in sorted(by_month):
        table.add_row(m, f"${by_month[m]:.4f}")
    console.print(table)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--days", type=int, default=30, help="Lookback window (default: 30)")
    parser.add_argument("--all", action="store_true", help="No date filter")
    parser.add_argument(
        "--type", choices=("reward", "yield", "all"), default="all", help="Income type to show"
    )
    parser.add_argument("--funder", help="Override PM_FUNDER_ADDRESS")
    args = parser.parse_args()

    load_dotenv()
    funder = args.funder or os.environ.get("PM_FUNDER_ADDRESS")
    if not funder:
        print("ERROR: set PM_FUNDER_ADDRESS or pass --funder", file=sys.stderr)
        return 1

    since = None if args.all else (datetime.now(tz=timezone.utc) - timedelta(days=args.days)).timestamp()

    if args.type in ("reward", "all"):
        show_rewards(filter_since(fetch(funder, "REWARD"), since))
    if args.type in ("yield", "all"):
        show_yield(filter_since(fetch(funder, "YIELD"), since))

    return 0


if __name__ == "__main__":
    sys.exit(main())
