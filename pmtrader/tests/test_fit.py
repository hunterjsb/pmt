"""Unit tests for polymarket.fit pure functions. No network."""

import pytest

from polymarket.fit import (
    parse_bucket,
    parse_symbol,
    realized_sigma,
    touch_prob_analytic,
    touch_prob_empirical,
)

TOUCH_DESC = (
    'This market will resolve to "Yes" if any Binance 1 minute candle for BTC/USDT '
    "has a final Low price equal to or lower than the price specified in the title."
)


def test_parse_bucket_down_arrow_with_commas():
    b = parse_bucket({"groupItemTitle": "↓ 70,000", "description": TOUCH_DESC})
    assert b == {"direction": "down", "barrier": 70000.0}


def test_parse_bucket_up_arrow():
    desc = TOUCH_DESC.replace("Low", "High").replace("lower", "higher")
    b = parse_bucket({"groupItemTitle": "↑ 90,000", "description": desc})
    assert b == {"direction": "up", "barrier": 90000.0}


def test_parse_bucket_direction_from_description_when_no_arrow():
    b = parse_bucket({"groupItemTitle": "70,000 or below", "description": TOUCH_DESC})
    assert b["direction"] == "down"


def test_parse_bucket_rejects_terminal_markets():
    # no candle wording -> terminal semantics, out of scope for a touch model
    assert parse_bucket({"groupItemTitle": "↓ 70,000", "description": "Resolves to the closing price."}) is None


def test_parse_bucket_rejects_directionless_or_numberless():
    assert parse_bucket({"groupItemTitle": "Something else", "description": ""}) is None
    assert parse_bucket({"groupItemTitle": "↓ soon", "description": TOUCH_DESC}) is None


def test_parse_symbol():
    assert parse_symbol("the Binance BTC/USDT trading pair") == "BTCUSDT"
    assert parse_symbol("no pair here") is None
    assert parse_symbol(None) is None


def test_realized_sigma_flat_and_short():
    assert realized_sigma([100.0] * 50, 30) == 0.0
    assert realized_sigma([100.0, 101.0], 30) == 0.0  # too few candles


def test_touch_prob_analytic_through_barrier_is_certain():
    assert touch_prob_analytic(69000, 70000, "down", 0.02, 9.5) == 1.0
    assert touch_prob_analytic(91000, 90000, "up", 0.02, 9.5) == 1.0


def test_touch_prob_analytic_degenerate_inputs():
    assert touch_prob_analytic(77000, 70000, "down", 0.0, 9.5) == 0.0
    assert touch_prob_analytic(77000, 70000, "down", 0.02, 0.0) == 0.0


def test_touch_prob_analytic_monotone_in_distance():
    near = touch_prob_analytic(77000, 75000, "down", 0.02, 9.5)
    far = touch_prob_analytic(77000, 65000, "down", 0.02, 9.5)
    assert 0 < far < near < 1


def test_touch_prob_empirical_counts_hits():
    # 30 flat closes; lows dip 10% below on even candles -> every window w/ an even candle hits
    closes = [100.0] * 30
    lows = [90.0 if i % 2 == 0 else 100.0 for i in range(30)]
    p = touch_prob_empirical(closes, lows, 0.95, "down", 2)
    assert p == 1.0
    assert touch_prob_empirical(closes, [100.0] * 30, 0.95, "down", 2) == 0.0


def test_touch_prob_empirical_up_direction():
    closes = [100.0] * 40
    highs = [100.0] * 40
    highs[25] = 120.0
    p = touch_prob_empirical(closes, highs, 1.1, "up", 3)
    assert p == pytest.approx(3 / 37)


def test_touch_prob_empirical_too_few_windows_is_none():
    assert touch_prob_empirical([100.0] * 10, [100.0] * 10, 0.9, "down", 5) is None
