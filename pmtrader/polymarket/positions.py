"""Current per-position marks from the data-api `positions` endpoint.

Answers exactly one question the wallet-activity feed cannot: what is a
position we are STILL HOLDING worth right now. Activity rows say what we
paid and (eventually) what we were paid; between those two moments the
only live number is `curPrice` here.

Deliberately tiny and deliberately separate from wallet.py: this is a
best-effort DISPLAY feed on the watch dashboard's slow cadence, never an
input to grading. Grading is the wallet's, always (cli_crypto_stats'
single-source rule) — a mark is an opinion, a redeem row is a fact.

Reads PM_FUNDER_ADDRESS via wallet.funder_address(); no key, no signing,
no engine.
"""

from __future__ import annotations

import requests

from . import hosts, updown_slugs

POSITIONS_URL = f"{hosts.DATA}/positions"

# The wallet holds long-dated non-updown positions too, and the endpoint has
# no slug filter — one generous page and filter locally, rather than paginate
# a display feed.
PAGE_LIMIT = 500


def fetch_positions(addr: str, limit: int = PAGE_LIMIT,
                    timeout: float = 8.0) -> list[dict]:
    """Open positions for `addr`, newest-value first. Raises on any transport
    or HTTP failure — the caller decides what a dark feed looks like."""
    r = requests.get(POSITIONS_URL, params={"user": addr, "limit": limit},
                     headers=hosts.UA, timeout=timeout)
    r.raise_for_status()
    return r.json() or []


def current_odds(rows: list[dict] | None) -> dict[tuple[str, str], float]:
    """`{(slug, side): curPrice}` for the updown positions in `rows`.

    Keyed the way a scoreboard window is identified — slug plus the side we
    fired — so a trades row looks its own mark up directly. `outcome` comes
    back title-cased ("Up"/"Down") and is lowered to the tape's vocabulary.

    Non-updown markets are dropped: the wallet holds long-dated positions
    this dashboard has nothing to say about. A resolved-but-unredeemed
    position is KEPT — its 0.00 or 1.00 mark is exactly the "how did it
    land" the operator is waiting on.
    """
    out: dict[tuple[str, str], float] = {}
    for p in rows or []:
        if not isinstance(p, dict):
            continue
        slug = p.get("slug") or ""
        side = str(p.get("outcome") or "").lower()
        px = p.get("curPrice")
        if not slug or not side or px is None or not updown_slugs.is_updown(slug):
            continue
        try:
            out[(slug, side)] = float(px)
        except (TypeError, ValueError):
            continue
    return out
