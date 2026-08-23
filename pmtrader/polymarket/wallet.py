"""Polymarket data-api wallet-activity client — shared by every updown
consumer that needs the trading wallet's trade/redeem history: the fleet
scoreboard, `pmt crypto activity`/`window`, and outcomes/shadow's
wallet-first resolver. One copy of the pagination and address resolution,
not the three that used to exist.
"""

from __future__ import annotations

import os

import requests

from . import hosts

ACTIVITY_URL = f"{hosts.DATA}/activity"
PAGE_SIZE = 500
# Deliberately < PAGE_SIZE: the overlap re-reads the seam that offset
# pagination over a LIVE feed keeps shifting, absorbing up to 100 fresh
# inserts per fetch; row_key dedupe collapses the rest. docs/LESSONS.md#L25.
PAGE_STEP = 400


def row_key(a: dict) -> tuple:
    """Stable identity for one activity row (no server-side id exists).
    Genuinely identical $0-redeem dust rows collapse too — zero-value, so
    the aggregate is unaffected."""
    return (
        a.get("transactionHash") or "",
        a.get("type") or "",
        a.get("asset") or a.get("slug") or "",
        a.get("side") or "",
        a.get("outcome") or "",
        a.get("size") or 0,
        a.get("usdcSize") or 0,
        a.get("timestamp") or 0,
    )


def funder_address() -> str:
    """PM_FUNDER_ADDRESS from the environment; raises if unset.

    An unset addr used to let pagination fall through silently and report
    a clean "0W-0L" — indistinguishable from a genuinely empty trading
    history. Every wallet-activity consumer must fail loud instead.
    """
    addr = os.environ.get("PM_FUNDER_ADDRESS", "")
    if not addr:
        raise ValueError("PM_FUNDER_ADDRESS not set")
    return addr


def fetch_activity_page(addr: str, offset: int, *, limit: int = PAGE_SIZE) -> list[dict]:
    """One page of /activity rows, newest first."""
    return requests.get(
        ACTIVITY_URL,
        params={"user": addr, "limit": limit, "offset": offset},
        headers=hosts.UA, timeout=8,
    ).json() or []


# ---------------------------------------------------------------------------
# READ THIS BEFORE YOU CACHE THE WALLET FEED. Someone has already tried.
#
# The full re-walk below looks wasteful and IS the cheap option. An
# accumulating in-memory ledger with an incremental head-refresh lived here
# until 2026-08-23; it cost five patches and was deleted. What it ran into,
# in the order it found them:
#
#   * data-api rows MUTATE IN PLACE. A REDEEM indexes with draft amounts and
#     the payout finalizes in stages; a partial-fill aggregate grows; a
#     reindex rewrites a row outright. Any identity that includes the amounts
#     makes the finalized row look NEW, so a cache ends up holding the draft
#     AND the final. Measured live: the first 30 drift purges were every one
#     of them a REDEEM, $1,227.83 double-counted, showing up as an optimistic
#     `watch` P&L that a restart mysteriously "fixed".
#   * PAGES SEAM-SHIFT. This is offset pagination over a LIVE feed: inserts
#     at the head push rows down between requests, so page N's tail reappears
#     at the top of page N+1. A page can therefore open with rows you already
#     hold and still carry new ones below them — "stop at the first known
#     row" is wrong, and even "stop at the first fully-known page" only
#     bounds the damage.
#   * MUTATION HAPPENS MID-WALK. A redeem can finalize between two of your
#     own page fetches, putting its draft on one page and its final on the
#     next one's seam overlap. There is no walk short enough to be atomic.
#
# The invariant that survives all three: this feed is authoritative and
# mutable, so the only correct read is a fresh one. The history is a handful
# of pages and every caller runs it off a worker thread or a one-shot report.
# If pagination cost ever genuinely hurts, the fix is a FLOOR at strategy
# genesis — a smaller honest walk, never a second code path holding old rows.
# ---------------------------------------------------------------------------


def fetch_wallet_activity(addr: str, floor: float = 0.0) -> list[dict]:
    """Every activity row back to `floor` (paginate until a page runs short
    of PAGE_SIZE or its oldest row predates floor).

    floor=0 walks the full history — the early-stop condition below only
    ever fires on a real (positive) timestamp, so it never short-circuits.
    """
    rows: list[dict] = []
    seen: set = set()
    offset = 0
    while True:
        page = fetch_activity_page(addr, offset)
        for a in page:
            k = row_key(a)
            if k not in seen:
                seen.add(k)
                rows.append(a)
        if len(page) < PAGE_SIZE or (page and page[-1]["timestamp"] < floor):
            break
        offset += PAGE_STEP
    return rows
