"""Shared singletons + render helpers for cli.py and every command-group split
off it (the cli_crypto_* modules, cli_sports, watch_ui) — one Console instance,
one lazy PolymarketAPI loader, one P&L color rule, one --since convention, one
deprecation banner, so a second Rich console, a second auth path, or a second
green/red convention never quietly diverges from the first.
"""

from __future__ import annotations

from rich.console import Console

console = Console()

DEPRECATED = "[deprecated — candidate for removal, speak up if you use this]"


def _deprecated(reason: str):
    """Stamp the removal-candidate banner (plus why) onto a command's --help.

    These commands still work — nothing here is broken. They're flagged as
    one-offs whose moment looks past, so the operator sees the flag in
    `--help` and can veto before anything actually gets deleted.
    """
    def deco(f):
        f.__doc__ = f"{DEPRECATED}\n\n{reason}\n\n{f.__doc__ or ''}"
        return f

    return deco


def _api():
    """Lazy-load PolymarketAPI so commands that don't need auth (search, market,
    book) can run without a configured proxy."""
    from polymarket import PolymarketAPI

    return PolymarketAPI()


def _pnl_color(pnl: float) -> str:
    """Rich style name for a signed money figure. Flat/zero reads as a win —
    losing nothing is not a loss."""
    return "green" if pnl >= 0 else "red"


def _parse_since(v: float | None) -> float:
    """HOURS_AGO_OR_EPOCH: small values are hours-ago, big ones (a real Unix
    timestamp is always > 1e6 in hours-ago terms) are a raw epoch already."""
    import time as _t

    if v is None:
        return 0.0
    if v > 1_000_000:
        return v
    return _t.time() - v * 3600
