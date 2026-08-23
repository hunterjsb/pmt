"""Polymarket data-api wallet-activity client — shared by every updown
consumer that needs the trading wallet's trade/redeem history: the fleet
scoreboard, `pmt crypto activity`/`window`, and outcomes/shadow's
wallet-first resolver.

Before this module, three near-identical copies of this fetch existed
(scoreboard's inline loop, activity's page helper, outcomes/shadow's
paginated fetch) with the same pagination and address-resolution logic
maintained three times.
"""

from __future__ import annotations

import os

import requests

from . import hosts

ACTIVITY_URL = f"{hosts.DATA}/activity"
PAGE_SIZE = 500
# Pages advance by less than their size: offset pagination over a LIVE feed
# shifts rows down whenever the fleet trades mid-walk, duplicating (or,
# on deletions, skipping) boundary rows. The overlap re-reads the seam —
# up to 100 fresh inserts per page-fetch are absorbed — and row_key
# dedupe collapses whatever duplicates remain. Without this the all-time
# scoreboard drifted run-to-run while nothing had actually settled.
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


class ActivityLedger:
    """An accumulating wallet-activity ledger with an INCREMENTAL refresh.

    fetch_wallet_activity() re-walks the whole history on every call, which
    is fine for a one-shot report and ruinous for the `watch` dashboard: an
    all-time scoreboard every 10s meant N sequential HTTP pages every 10s,
    growing forever as the wallet trades. This keeps the rows in memory and
    re-reads only the new head of the feed.

    Refresh walks NEWEST-first and stops at the first page whose rows are
    ALL already known. One known row is NOT enough to stop: offset
    pagination over a live feed shifts rows across the page seam (see
    PAGE_STEP above), so a page can legitimately open with rows we already
    hold and still carry new ones below them. Requiring a FULL page of
    known rows makes the stop exact — the row_key set is the same identity
    the seam-dedupe uses, so "known" means known, not "looks similar".
    """

    def __init__(self) -> None:
        self.rows: list[dict] = []
        self._seen: set = set()
        self.primed = False  # False until the first (full-history) walk lands
        self.last_pages = 0  # pages fetched by the most recent refresh

    def refresh(self, addr: str) -> int:
        """Fetch new rows into the ledger; returns how many were new.

        The first call walks the full history (no early stop — nothing is
        known yet, so every page is "all new" anyway); later calls stop at
        the first fully-known page, which in steady state is page 2.
        """
        offset = 0
        new = 0
        pages = 0
        while True:
            page = fetch_activity_page(addr, offset)
            pages += 1
            page_new = 0
            for a in page:
                k = row_key(a)
                if k in self._seen:
                    continue
                self._seen.add(k)
                self.rows.append(a)
                page_new += 1
            new += page_new
            if len(page) < PAGE_SIZE:
                break  # end of history
            if self.primed and page_new == 0:
                break  # a whole page we already hold — caught up with the head
            offset += PAGE_STEP
        self.primed = True
        self.last_pages = pages
        return new
