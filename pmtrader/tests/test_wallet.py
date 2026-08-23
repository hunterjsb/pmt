"""Tests for the shared wallet-activity client (address resolution + pagination).

Network is monkeypatched — no live data-api calls.
"""

from __future__ import annotations

import pytest

from polymarket import wallet


class _FakeResp:
    def __init__(self, rows):
        self._rows = rows

    def json(self):
        return self._rows


def test_funder_address_raises_when_unset(monkeypatch):
    monkeypatch.delenv("PM_FUNDER_ADDRESS", raising=False)
    with pytest.raises(ValueError, match="PM_FUNDER_ADDRESS not set"):
        wallet.funder_address()


def test_funder_address_returns_env_value(monkeypatch):
    monkeypatch.setenv("PM_FUNDER_ADDRESS", "0xabc")
    assert wallet.funder_address() == "0xabc"


def test_fetch_activity_page_passes_pagination_params(monkeypatch):
    seen = {}

    def fake_get(url, params, headers, timeout):
        seen["url"] = url
        seen["params"] = params
        return _FakeResp([{"timestamp": 1}])

    monkeypatch.setattr(wallet.requests, "get", fake_get)
    rows = wallet.fetch_activity_page("0xabc", 500)
    assert rows == [{"timestamp": 1}]
    assert seen["url"] == wallet.ACTIVITY_URL
    assert seen["params"] == {"user": "0xabc", "limit": 500, "offset": 500}


def test_fetch_activity_page_none_json_becomes_empty_list(monkeypatch):
    monkeypatch.setattr(wallet.requests, "get", lambda *a, **k: _FakeResp(None))
    assert wallet.fetch_activity_page("0xabc", 0) == []


def test_fetch_wallet_activity_paginates_until_short_page(monkeypatch):
    # Two full pages of PAGE_SIZE, then a short final page -> 3 calls total.
    # Timestamps stay well above floor=0 throughout, so only page length ends it.
    page1 = [{"timestamp": 3_000_000 - i} for i in range(wallet.PAGE_SIZE)]
    page2 = [{"timestamp": 2_000_000 - i} for i in range(wallet.PAGE_SIZE)]
    page3 = [{"timestamp": 1_000_000}]
    pages = [page1, page2, page3]
    calls = []

    def fake_page(addr, offset, *, limit=wallet.PAGE_SIZE):
        calls.append(offset)
        return pages[len(calls) - 1]

    monkeypatch.setattr(wallet, "fetch_activity_page", fake_page)
    rows = wallet.fetch_wallet_activity("0xabc", floor=0.0)
    assert calls == [0, wallet.PAGE_STEP, 2 * wallet.PAGE_STEP]
    assert len(rows) == 2 * wallet.PAGE_SIZE + 1


def test_fetch_wallet_activity_stops_early_when_oldest_row_predates_floor(monkeypatch):
    # A full page whose oldest (last) row is already below floor stops pagination
    # immediately, even though the page itself was full-sized.
    page = [{"timestamp": 1000 - i} for i in range(wallet.PAGE_SIZE)]
    calls = []

    def fake_page(addr, offset, *, limit=wallet.PAGE_SIZE):
        calls.append(offset)
        return page

    monkeypatch.setattr(wallet, "fetch_activity_page", fake_page)
    floor = page[-1]["timestamp"] + 1  # oldest row on the first page is already stale
    rows = wallet.fetch_wallet_activity("0xabc", floor=floor)
    assert calls == [0]
    assert rows == page


def test_fetch_wallet_activity_floor_zero_walks_full_history(monkeypatch):
    # floor=0 must never trip the "oldest row < floor" early-stop — only an
    # empty/short final page ends pagination (the all-time ledger case).
    page1 = [{"timestamp": 500 - i} for i in range(wallet.PAGE_SIZE)]
    page2 = [{"timestamp": 5, "transactionHash": "0xfinal"}]  # short final page
    pages = [page1, page2]
    calls = []

    def fake_page(addr, offset, *, limit=wallet.PAGE_SIZE):
        calls.append(offset)
        return pages[len(calls) - 1]

    monkeypatch.setattr(wallet, "fetch_activity_page", fake_page)
    rows = wallet.fetch_wallet_activity("0xabc", floor=0.0)
    assert calls == [0, wallet.PAGE_STEP]
    assert len(rows) == wallet.PAGE_SIZE + 1


def _row(ts: int, tx: str = "") -> dict:
    """One activity row with a distinct row_key."""
    return {"timestamp": ts, "transactionHash": tx or f"0x{ts:x}", "type": "TRADE",
            "side": "BUY", "size": 1, "usdcSize": 1.0, "slug": "btc-updown-5m-1"}


def _pager(pages: list[list[dict]], calls: list[int]):
    """fetch_activity_page stub that serves `pages` in order and records offsets."""
    def fake_page(addr, offset, *, limit=wallet.PAGE_SIZE):
        calls.append(offset)
        i = len(calls) - 1
        return pages[i] if i < len(pages) else []
    return fake_page


# ---------- ActivityLedger: the incremental (O(new) not O(history)) refresh ----------

def test_ledger_first_refresh_walks_full_history(monkeypatch):
    # Nothing is known yet, so the early stop can't fire: pages until a short one.
    pages = [[_row(3_000_000 - i) for i in range(wallet.PAGE_SIZE)],
             [_row(2_000_000 - i) for i in range(wallet.PAGE_SIZE)],
             [_row(1_000_000)]]
    calls: list[int] = []
    monkeypatch.setattr(wallet, "fetch_activity_page", _pager(pages, calls))

    led = wallet.ActivityLedger()
    new = led.refresh("0xabc")
    assert calls == [0, wallet.PAGE_STEP, 2 * wallet.PAGE_STEP]
    assert led.last_pages == 3
    assert new == len(led.rows) == 2 * wallet.PAGE_SIZE + 1
    assert led.primed


def test_ledger_steady_state_refresh_stops_on_first_known_page(monkeypatch):
    # Quiet feed: page 0 comes back entirely known -> one page, zero new rows.
    page0 = [_row(3_000_000 - i) for i in range(wallet.PAGE_SIZE)]
    page1 = [_row(2_000_000 - i) for i in range(wallet.PAGE_SIZE)]
    tail = [_row(1_000_000)]
    calls: list[int] = []
    monkeypatch.setattr(wallet, "fetch_activity_page",
                        _pager([page0, page1, tail, page0], calls))

    led = wallet.ActivityLedger()
    led.refresh("0xabc")
    before = len(led.rows)
    calls.clear()
    monkeypatch.setattr(wallet, "fetch_activity_page", _pager([page0], calls))

    assert led.refresh("0xabc") == 0
    assert calls == [0]           # one page, not the whole history
    assert led.last_pages == 1
    assert len(led.rows) == before  # nothing double-counted


def test_ledger_refresh_merges_new_head_rows_without_double_counting(monkeypatch):
    # Live feed: 3 new rows arrived, shifting everything down by 3. Page 0 is
    # partly new so the walk continues; page 1 is wholly known and ends it.
    hist = [_row(3_000_000 - i) for i in range(wallet.PAGE_SIZE + 200)]
    calls: list[int] = []
    monkeypatch.setattr(wallet, "fetch_activity_page",
                        _pager([hist[:wallet.PAGE_SIZE], hist[wallet.PAGE_STEP:]], calls))
    led = wallet.ActivityLedger()
    led.refresh("0xabc")
    before = len(led.rows)

    fresh = [_row(3_000_100 + i) for i in range(3)]
    shifted = fresh + hist
    calls.clear()
    monkeypatch.setattr(wallet, "fetch_activity_page",
                        _pager([shifted[:wallet.PAGE_SIZE],
                                shifted[wallet.PAGE_STEP:wallet.PAGE_STEP + wallet.PAGE_SIZE]],
                               calls))

    assert led.refresh("0xabc") == 3
    assert calls == [0, wallet.PAGE_STEP]  # stopped at the first fully-known page
    assert len(led.rows) == before + 3
    keys = [wallet.row_key(a) for a in led.rows]
    assert len(keys) == len(set(keys))  # no duplicates anywhere in the ledger


def test_ledger_refresh_does_not_stop_on_a_partly_known_seam_page(monkeypatch):
    # THE seam case (see PAGE_STEP): a page can open with rows we already hold
    # and still carry new ones below them. One known row must never stop the
    # walk — only a FULL page of known rows does.
    known = [_row(3_000_000 - i) for i in range(wallet.PAGE_SIZE)]
    calls: list[int] = []
    monkeypatch.setattr(wallet, "fetch_activity_page", _pager([known, []], calls))
    led = wallet.ActivityLedger()
    led.refresh("0xabc")

    # A refresh where page 0 = [known seam rows..., brand-new older rows...]:
    # every page is "mostly known" but never fully, so the walk must continue.
    older = [_row(1_000_000 - i) for i in range(wallet.PAGE_SIZE)]
    seam_page = known[-100:] + older[:wallet.PAGE_SIZE - 100]
    tail = older[wallet.PAGE_SIZE - 100:]
    calls.clear()
    monkeypatch.setattr(wallet, "fetch_activity_page", _pager([seam_page, tail], calls))

    new = led.refresh("0xabc")
    assert calls == [0, wallet.PAGE_STEP]  # did NOT stop on the known seam rows
    assert new == wallet.PAGE_SIZE         # every older row landed exactly once
    keys = [wallet.row_key(a) for a in led.rows]
    assert len(keys) == len(set(keys))


def test_ledger_empty_history_refreshes_are_cheap(monkeypatch):
    # An empty wallet must not re-walk forever just because it holds no rows.
    calls: list[int] = []
    monkeypatch.setattr(wallet, "fetch_activity_page", _pager([[], []], calls))
    led = wallet.ActivityLedger()
    assert led.refresh("0xabc") == 0
    assert led.refresh("0xabc") == 0
    assert calls == [0, 0]
    assert led.rows == []


def test_fetch_wallet_activity_dedupes_seam_rows_from_live_inserts(monkeypatch):
    # Offset pagination over a live feed: inserts shift rows down, so the
    # seam rows of page N reappear at the top of page N+1. The overlap +
    # row_key dedupe must collapse them instead of double-counting.
    dup = {"timestamp": 2_000_100, "transactionHash": "0xdup", "type": "TRADE",
           "side": "BUY", "size": 5, "usdcSize": 4.5}
    page1 = [{"timestamp": 3_000_000 - i} for i in range(wallet.PAGE_SIZE - 1)] + [dup]
    page2 = [dup] + [{"timestamp": 1_000_000}]
    pages = [page1, page2]
    calls = []

    def fake_page(addr, offset, *, limit=wallet.PAGE_SIZE):
        calls.append(offset)
        return pages[len(calls) - 1]

    monkeypatch.setattr(wallet, "fetch_activity_page", fake_page)
    rows = wallet.fetch_wallet_activity("0xabc", floor=0.0)
    assert sum(1 for a in rows if a.get("transactionHash") == "0xdup") == 1
    assert len(rows) == wallet.PAGE_SIZE + 1
