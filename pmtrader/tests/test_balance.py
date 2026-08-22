"""Unit tests for PolymarketAPI.get_usdc_balance. No network — client stubbed."""

import pytest

from polymarket.api import PolymarketAPI, locked_buy_cash


class _StubClient:
    """Records the balance-allowance params and returns a canned COLLATERAL balance."""

    def __init__(self, balance_micro: str):
        self.balance_micro = balance_micro
        self.params = None

    def get_balance_allowance(self, params=None):
        self.params = params
        return {"balance": self.balance_micro, "allowance": "0"}


@pytest.fixture
def api():
    return PolymarketAPI.__new__(PolymarketAPI)


def test_usdc_balance_available_and_locked(api):
    api.client = _StubClient("123450000")  # $123.45 in 1e6 units
    api.get_orders = lambda: [
        # remaining 60 sh @ 0.50 → $30 locked
        {"side": "BUY", "original_size": "100", "size_matched": "40", "price": "0.50"},
        # sells lock shares, not cash
        {"side": "SELL", "original_size": "200", "size_matched": "0", "price": "0.60"},
        # remaining 10 sh @ 0.25 → $2.50 locked
        {"side": "BUY", "original_size": "10", "size_matched": "0", "price": "0.25"},
    ]
    out = api.get_usdc_balance()
    # CLOB balance is gross — available must already net out the bid locks
    assert out["total"] == pytest.approx(123.45)
    assert out["locked"] == pytest.approx(32.5)
    assert out["available"] == pytest.approx(90.95)

    from py_clob_client_v2 import AssetType

    assert api.client.params.asset_type == AssetType.COLLATERAL


def test_usdc_balance_no_orders(api):
    api.client = _StubClient("0")
    api.get_orders = lambda: []
    assert api.get_usdc_balance() == {"total": 0.0, "locked": 0.0, "available": 0.0}


def test_locked_buy_cash_counts_unfilled_remainder_only():
    partial = {"side": "BUY", "original_size": "100", "size_matched": "40", "price": "0.50"}
    assert locked_buy_cash(partial) == pytest.approx(30.0)
    assert locked_buy_cash({"side": "SELL", "original_size": "200", "price": "0.60"}) == 0.0
    assert locked_buy_cash({"side": "BUY"}) == 0.0


def test_usdc_balance_tolerates_missing_fields(api):
    api.client = _StubClient("1000000")
    api.get_orders = lambda: [{"side": "BUY"}]  # no size/price fields → contributes $0
    out = api.get_usdc_balance()
    assert out["total"] == pytest.approx(1.0)
    assert out["locked"] == 0.0
    assert out["available"] == pytest.approx(1.0)
