"""Polymarket data-api wallet-activity client — shared by every updown
consumer that needs the trading wallet's trade/redeem history: the fleet
scoreboard, `pmt crypto activity`/`window`, and outcomes/shadow's
wallet-first resolver. One copy of the pagination and address resolution,
not the three that used to exist.
"""

from __future__ import annotations

import os
import json
import time
from pathlib import Path

import requests

from . import hosts

ACTIVITY_URL = f"{hosts.DATA}/activity"
PAGE_SIZE = 500
# Deliberately < PAGE_SIZE: the overlap re-reads the seam that offset
# pagination over a LIVE feed keeps shifting, absorbing up to 100 fresh
# inserts per fetch; row_key dedupe collapses the rest. docs/LESSONS.md#L25.
PAGE_STEP = 400
# Incremental refreshes self-heal with a full re-walk this often — see
# ActivityLedger.refresh on why (mutable data-api rows drift the ledger).
RESYNC_S = 300.0


def redeem_identity(a: dict) -> tuple:
    """Stable identity for a REDEEM: everything EXCEPT the amounts.

    Redeem rows MUTATE after first indexing — the payout fields finalize in
    stages — and an identity that includes the amounts makes the finalized
    version look like a NEW row beside its draft. Measured live 2026-08-23:
    every one of the first 30 ledger-drift purges was a REDEEM ($1,227.83
    double-counted), reading as an optimistic watch P&L between resyncs.
    One redeem per (tx, market, outcome) is an on-chain invariant, so this
    key is safe; $0 dust rows collapse harmlessly as before.
    """
    return (
        a.get("transactionHash") or "",
        "REDEEM",
        a.get("asset") or a.get("slug") or "",
        a.get("outcome") or "",
    )


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
        self._redeems: dict = {}  # redeem_identity -> the row currently held
        self.primed = False  # False until the first (full-history) walk lands
        self.last_pages = 0  # pages fetched by the most recent refresh
        self.last_drift = 0  # stale rows the most recent resync had to purge
        self._resynced_at = 0.0

    def refresh(self, addr: str) -> int:
        """Fetch new rows into the ledger; returns how many were new.

        The first call walks the full history; later calls stop at the first
        fully-known page, which in steady state is page 2. Every RESYNC_S the
        incremental path is REPLACED by a fresh full walk: data-api rows are
        not immutable — a row that mutates after we hold it (a partial-fill
        aggregate growing, a reindex) leaves the incremental ledger holding
        BOTH versions, and the accumulated drift showed up live as a wrong
        watch P&L that a restart "fixed" (2026-08-23). The full history is a
        handful of pages, so the periodic walk costs almost nothing and
        bounds any such drift at RESYNC_S old.
        """
        if not self.primed or time.time() - self._resynced_at >= RESYNC_S:
            return self._resync(addr)
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
                if a.get("type") == "REDEEM":
                    # A mutated redeem must REPLACE its draft, never sit
                    # beside it — see redeem_identity.
                    rid = redeem_identity(a)
                    prev = self._redeems.get(rid)
                    if prev is not None:
                        self._seen.discard(row_key(prev))
                        self.rows.remove(prev)
                    self._redeems[rid] = a
                self._seen.add(k)
                self.rows.append(a)
                page_new += 1
            new += page_new
            if len(page) < PAGE_SIZE:
                break  # end of history
            if page_new == 0:
                break  # a whole page we already hold — caught up with the head
            offset += PAGE_STEP
        self.last_pages = pages
        return new

    def _resync(self, addr: str) -> int:
        """Fresh full walk, atomically replacing the ledger's state.

        Rows the old state held that the feed no longer contains (stale
        versions of mutated rows, deletions) are purged — and logged to
        ~/.pmt/engine/ledger-drift.jsonl so the next drift event names its
        culprit rows instead of being argued from symptoms.
        """
        rows: list[dict] = []
        seen: set = set()
        offset = 0
        pages = 0
        while True:
            page = fetch_activity_page(addr, offset)
            pages += 1
            for a in page:
                k = row_key(a)
                if k not in seen:
                    seen.add(k)
                    rows.append(a)
            if len(page) < PAGE_SIZE:
                break
            offset += PAGE_STEP
        stale = [a for a in self.rows if row_key(a) not in seen] if self.primed else []
        new = len(seen - self._seen)
        if stale:
            _log_ledger_drift(stale)
        self.rows, self._seen = rows, seen
        self._redeems = {redeem_identity(a): a for a in rows if a.get("type") == "REDEEM"}
        self.primed = True
        self.last_pages = pages
        self.last_drift = len(stale)
        self._resynced_at = time.time()
        return new


def _log_ledger_drift(stale_rows: list[dict]) -> None:
    """Append purged-stale rows to the drift log; best-effort, never raises."""
    try:
        path = Path.home() / ".pmt" / "engine" / "ledger-drift.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a") as fh:
            for a in stale_rows:
                fh.write(json.dumps({"purged_at": time.time(), "row": a}) + "\n")
    except OSError:
        pass
