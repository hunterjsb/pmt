"""Unit tests for PolymarketAPI.sweep sizing, book_levels, and the sell settlement retry.

No network: instances are built bare (__new__) and book/placement methods stubbed.
"""

import json
import os

import pytest

from polymarket.api import PolymarketAPI


@pytest.fixture
def api():
    """Bare API instance — no auth client; stub methods per-test."""
    return PolymarketAPI.__new__(PolymarketAPI)


# Levels are (price, size) floats, best-first, as book_levels returns them.
BOOK = {
    "bids": [(0.48, 100.0), (0.47, 200.0), (0.45, 50.0)],
    "asks": [(0.50, 100.0), (0.52, 200.0), (0.55, 400.0)],
    "mid": 0.49,
    "spread": 0.02,
}


def _no_place(*a, **kw):
    raise AssertionError("sweep must not place an order here")


def test_sweep_buy_normal(api):
    api.book_levels = lambda token, depth=None: BOOK
    api.place = _no_place
    out = api.sweep("buy", token="1", to_price=0.52, dry_run=True)
    assert out["plan"]["levels"] == [(0.50, 100.0), (0.52, 200.0)]
    assert out["plan"]["size"] == pytest.approx(300.0)
    assert out["plan"]["est_cost"] == pytest.approx(0.50 * 100 + 0.52 * 200)
    assert out["placed"] is None


def test_sweep_buy_max_cost_trims_worst_level(api):
    api.book_levels = lambda token, depth=None: BOOK
    api.place = _no_place
    out = api.sweep("buy", token="1", to_price=0.55, max_cost=100.0, dry_run=True)
    levels = out["plan"]["levels"]
    # First level whole ($50), second partial-taken with the remaining $50.
    assert levels[0] == (0.50, 100.0)
    assert levels[1][0] == 0.52
    assert levels[1][1] == pytest.approx(50.0 / 0.52)
    assert len(levels) == 2
    assert out["plan"]["est_cost"] == pytest.approx(100.0)
    assert out["plan"]["size"] == pytest.approx(100.0 + 50.0 / 0.52)
    # Committed notional is place_size * to_price, so the budget also caps size.
    assert out["plan"]["place_size"] == int(100.0 / 0.55)


def test_sweep_max_cost_caps_committed_notional(api):
    # Cheap ask drags est_cost down, but the GTC rests at to_price — every
    # share can fill there if the cheap level vanishes. Budget must cap that.
    deep = {"bids": [], "asks": [(0.10, 50.0), (0.90, 500.0)], "mid": None, "spread": None}
    api.book_levels = lambda token, depth=None: deep
    api.get_tick_size = lambda token: "0.01"
    calls = []

    def fake_place(side, *, token, price, size, tick_size=None):
        calls.append((side, token, price, size, tick_size))
        return {"orderID": "abc", "status": "live"}

    api.place = fake_place
    out = api.sweep("buy", token="1", to_price=0.90, max_cost=50.0, dry_run=False)
    assert out["plan"]["size"] == pytest.approx(100.0)
    assert out["plan"]["est_cost"] == pytest.approx(50.0)
    assert calls == [("buy", "1", 0.90, 55, "0.01")]
    assert 55 * 0.90 <= 50.0


def test_sweep_budget_under_min_never_places(api):
    api.book_levels = lambda token, depth=None: BOOK
    api.place = _no_place
    # Plan is 300 sh, but $2 at 0.52 caps place_size to 3 — under the exchange min.
    out = api.sweep("buy", token="1", to_price=0.52, max_cost=2.0, dry_run=False)
    assert out["plan"]["place_size"] == 3
    assert out["placed"] is None


def test_sweep_empty_band_never_places(api):
    api.book_levels = lambda token, depth=None: BOOK
    api.place = _no_place
    out = api.sweep("buy", token="1", to_price=0.49, dry_run=False)
    assert out["plan"]["levels"] == []
    assert out["plan"]["size"] == 0.0
    assert out["placed"] is None


def test_sweep_sub_min_band_never_places(api):
    thin = {"bids": [], "asks": [(0.50, 3.0)], "mid": None, "spread": None}
    api.book_levels = lambda token, depth=None: thin
    api.place = _no_place
    out = api.sweep("buy", token="1", to_price=0.50, dry_run=False)
    assert out["plan"]["size"] == pytest.approx(3.0)
    assert out["placed"] is None


def test_sweep_sell_side(api):
    api.book_levels = lambda token, depth=None: BOOK
    api.place = _no_place
    out = api.sweep("sell", token="1", to_price=0.47, dry_run=True)
    assert out["plan"]["levels"] == [(0.48, 100.0), (0.47, 200.0)]
    assert out["plan"]["size"] == pytest.approx(300.0)
    assert out["plan"]["est_cost"] == pytest.approx(0.48 * 100 + 0.47 * 200)
    assert out["placed"] is None


def test_sweep_places_gtc_at_to_price_when_live(api):
    api.book_levels = lambda token, depth=None: BOOK
    api.get_tick_size = lambda token: "0.01"
    calls = []

    def fake_place(side, *, token, price, size, tick_size=None):
        calls.append((side, token, price, size, tick_size))
        return {"orderID": "abc", "status": "live"}

    api.place = fake_place
    out = api.sweep("buy", token="1", to_price=0.52, dry_run=False)
    assert out["placed"] == {"orderID": "abc", "status": "live"}
    assert calls == [("buy", "1", 0.52, 300, "0.01")]


def test_sweep_rejects_bad_side(api):
    with pytest.raises(ValueError):
        api.sweep("hold", token="1", to_price=0.5)


def test_book_levels_normalizes_real_clob_response(api):
    """Raw CLOB fixture: string prices, bids asc / asks desc → floats, best-first."""
    fixture_path = os.path.join(
        os.path.dirname(__file__), "fixtures", "clob_api_response.json"
    )
    if not os.path.exists(fixture_path):
        pytest.skip("Sample fixture not found")
    with open(fixture_path) as f:
        raw = json.load(f)

    api.get_book = lambda token: raw
    out = api.book_levels("1")
    assert out["bids"][0] == (0.966, 8.27)
    assert out["asks"][0] == (0.979, 428.0)
    bid_prices = [p for p, _ in out["bids"]]
    ask_prices = [p for p, _ in out["asks"]]
    assert bid_prices == sorted(bid_prices, reverse=True)
    assert ask_prices == sorted(ask_prices)
    assert out["mid"] == pytest.approx((0.966 + 0.979) / 2)
    assert out["spread"] == pytest.approx(0.979 - 0.966)

    shallow = api.book_levels("1", depth=2)
    assert len(shallow["bids"]) == 2 and len(shallow["asks"]) == 2
    assert shallow["mid"] == out["mid"]  # mid/spread from full book, not truncated


def test_book_levels_empty_book(api):
    api.get_book = lambda token: {"bids": [], "asks": None}
    out = api.book_levels("1")
    assert out == {"bids": [], "asks": [], "mid": None, "spread": None}


def test_sell_settlement_retry_backs_off_then_succeeds(api, monkeypatch):
    from py_clob_client_v2.exceptions import PolyApiException

    monkeypatch.setattr("polymarket.api.time.sleep", lambda s: None)
    calls = []

    def fake_sell(*, token, price, size, tick_size):
        calls.append((token, price, size, tick_size))
        if len(calls) < 3:
            raise PolyApiException(error_msg="not enough balance / allowance")
        return {"orderID": "ok", "status": "live"}

    api.place_sell = fake_sell
    resp = api._sell_with_settlement_retry(token="1", price=0.9, size=10, tick_size="0.01")
    assert resp["orderID"] == "ok"
    assert len(calls) == 3


def test_sell_settlement_retry_reraises_other_errors(api):
    from py_clob_client_v2.exceptions import PolyApiException

    def fake_sell(**kw):
        raise PolyApiException(error_msg="invalid order payload")

    api.place_sell = fake_sell
    with pytest.raises(PolyApiException):
        api._sell_with_settlement_retry(token="1", price=0.9, size=10, tick_size="0.01")
