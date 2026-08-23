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
