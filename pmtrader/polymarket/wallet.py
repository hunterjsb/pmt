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
    offset = 0
    while True:
        page = fetch_activity_page(addr, offset)
        rows.extend(page)
        if len(page) < PAGE_SIZE or (page and page[-1]["timestamp"] < floor):
            break
        offset += PAGE_SIZE
    return rows
