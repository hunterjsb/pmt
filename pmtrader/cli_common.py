"""Shared singletons for cli.py and cli_crypto.py (and any future command-group
split) — one Console instance, one lazy PolymarketAPI loader, so a second
Rich console or a second auth path never quietly diverges from the first.
"""

from __future__ import annotations

from rich.console import Console

console = Console()


def _api():
    """Lazy-load PolymarketAPI so commands that don't need auth (search, market,
    book) can run without a configured proxy."""
    from polymarket import PolymarketAPI

    return PolymarketAPI()
