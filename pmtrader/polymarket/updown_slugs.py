"""Updown slug parsing — one place for the "sym-updown-Nm-epoch" format.

Every crypto-updown consumer (outcomes grading, shadow's episode grouping,
the fleet scoreboard, tape rendering, the window post-mortem) needs the
same symbol/duration/start/end split; before this module each site kept
its own regex or ad-hoc split/rsplit, which is how subtly different copies
of the same math spread across cli.py, outcomes.py, and shadow.py.
"""

from __future__ import annotations

import re

# h durations are real traded history: requiring the m token silently
# dropped every 4h window from the ledger — six settled WINS (+$164.35)
# rode invisible until a raw wallet walk caught the gap (regression_sweep).
SLUG_RE = re.compile(r"^([a-z]+)-updown-(\d+)([mh])-(\d+)$")


def parse_updown_slug(slug: str) -> dict | None:
    """{'symbol','dur_s','start','end'} from an updown slug, or None if it doesn't match."""
    m = SLUG_RE.match(slug)
    if not m:
        return None
    sym, dur_n, dur_unit, start_s = m.groups()
    start = int(start_s)
    dur_s = int(dur_n) * (3600 if dur_unit == "h" else 60)
    return {"symbol": sym, "dur_s": dur_s, "start": start, "end": start + dur_s}


def parse(slug: str) -> tuple[str, int, int, int, str] | None:
    """(symbol, duration_s, start, end, series_key), or None if not an updown slug.

    series_key groups a series across rolls for display/aggregation, e.g.
    "btc 15m" — symbol + duration label, the key the fleet scoreboard's
    per-series table and `pmt crypto stats` bucket by.
    """
    w = parse_updown_slug(slug)
    if w is None:
        return None
    return w["symbol"], w["dur_s"], w["start"], w["end"], series_key(w["symbol"], w["dur_s"])


def series_key(symbol: str, dur_s: int) -> str:
    """'btc 15m' from ('btc', 900); hour formats keep their own unit ('btc 4h')."""
    if dur_s >= 3600 and dur_s % 3600 == 0:
        return f"{symbol} {dur_s // 3600}h"
    return f"{symbol} {dur_s // 60}m"


def window_start(slug: str) -> float:
    """Window-start epoch from an updown slug's trailing token, 0 if absent.

    Deliberately looser than parse_updown_slug: it only needs the trailing
    token, so it works on any slug whose caller already knows (by other
    means, e.g. `is_updown`) is an updown window.
    """
    try:
        return float(slug.rsplit("-", 1)[1])
    except (IndexError, ValueError):
        return 0.0


def is_updown(slug: str) -> bool:
    """Cheap membership check without a full parse."""
    return "-updown-" in slug


def display(slug: str) -> str:
    """'btc-updown-5m-1787442000' -> 'btc 5m 23:40' (window start, local time).

    Falls back to the raw slug for anything that doesn't match.
    """
    import time as _t

    w = parse_updown_slug(slug)
    if w is None:
        return slug
    start_label = _t.strftime("%H:%M", _t.localtime(w["start"]))
    return f"{w['symbol']} {w['dur_s'] // 60}m {start_label}"
