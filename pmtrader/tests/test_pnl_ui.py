"""Unit tests for the UI-matching P/L series helpers. No network — series stubbed."""

import pytest
import requests

from polymarket.api import PolymarketAPI
from polymarket.pnl import UI_WINDOWS, ui_window_value


def test_window_delta_is_last_minus_first():
    series = [{"t": 1, "p": 10.0}, {"t": 2, "p": 12.5}, {"t": 3, "p": 9.0}]
    assert ui_window_value(series) == pytest.approx(-1.0)


def test_all_time_is_latest_value():
    # The series is cumulative from account start, so "all" is the last point,
    # not last-minus-first (the first point is day one's close, not zero).
    series = [{"t": 1, "p": 2.47}, {"t": 2, "p": 607.33}]
    assert ui_window_value(series, all_time=True) == pytest.approx(607.33)


def test_empty_series_is_none():
    assert ui_window_value([]) is None
    assert ui_window_value([], all_time=True) is None


def test_get_ui_pnl_maps_windows_and_degrades():
    api = PolymarketAPI.__new__(PolymarketAPI)
    calls = []

    def fake_series(interval, fidelity):
        calls.append((interval, fidelity))
        if interval == "1w":
            raise requests.ConnectionError("cold cache")
        return [{"t": 1, "p": 1.0}, {"t": 2, "p": 3.0}]

    api.get_pnl_series = fake_series
    out = api.get_ui_pnl()

    assert calls == list(UI_WINDOWS.values())
    assert out["7d"] is None  # failed fetch degrades to None, never raises
    assert out["1d"] == pytest.approx(2.0)
    assert out["30d"] == pytest.approx(2.0)
    assert out["all"] == pytest.approx(3.0)
