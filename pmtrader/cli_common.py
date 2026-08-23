"""Shared singletons + render helpers for cli.py and cli_crypto.py (and any
future command-group split) — one Console instance, one lazy PolymarketAPI
loader, one P&L color rule, so a second Rich console, a second auth path, or a
second green/red convention never quietly diverges from the first.
"""

from __future__ import annotations

from rich.console import Console

console = Console()


def _api():
    """Lazy-load PolymarketAPI so commands that don't need auth (search, market,
    book) can run without a configured proxy."""
    from polymarket import PolymarketAPI

    return PolymarketAPI()


def _pnl_color(pnl: float) -> str:
    """Rich style name for a signed money figure. Flat/zero reads as a win —
    losing nothing is not a loss."""
    return "green" if pnl >= 0 else "red"
