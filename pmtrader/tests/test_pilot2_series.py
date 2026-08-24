"""The series partition, and the refusal that is the pilot's whole licence.

Two participants quoting one market under one beneficial owner is wash-trade
shaped no matter what either intended. The desktop engine owns the majors, the
EU engine owns bnb-updown-5m, and this pilot is a THIRD participant. A config
that points live capital at any of those must stop the process.
"""

from __future__ import annotations

import pytest

from pilot2 import series


def test_default_live_series_are_the_approved_three():
    assert series.DEFAULT_LIVE_SERIES == ("doge-updown-5m", "hype-updown-5m", "bnb-updown-15m")
    assert series.live_series("") == list(series.DEFAULT_LIVE_SERIES)


def test_shadow_series_are_the_majors_both_tiers():
    assert series.shadow_series() == ["btc-updown-5m", "eth-updown-5m",
                                      "sol-updown-5m", "xrp-updown-5m",
                                      "btc-updown-15m", "eth-updown-15m",
                                      "sol-updown-15m", "xrp-updown-15m"]


@pytest.mark.parametrize("bad", [
    "btc-updown-5m", "eth-updown-5m", "sol-updown-5m", "xrp-updown-5m",
    "btc-updown-15m", "eth-updown-1h", "xrp-updown-4h",   # the desktop rolls every duration
    "bnb-updown-5m",                                       # the EU engine's one series
])
def test_live_refuses_every_series_an_engine_owns(bad):
    with pytest.raises(series.SeriesRefused) as e:
        series.live_series(bad)
    assert bad in str(e.value)
    assert "wash-trade" in str(e.value)


def test_live_refuses_the_whole_list_when_one_entry_is_stolen():
    """A single bad entry is fatal — the pilot does not quietly trade the
    remainder and leave the operator thinking the config was accepted."""
    with pytest.raises(series.SeriesRefused):
        series.live_series("doge-updown-5m,btc-updown-5m,hype-updown-5m")


def test_bnb_15m_is_free_even_though_bnb_5m_is_owned():
    """The EU box's allowlist is exactly `bnb-updown-5m`. A prefix match on
    'bnb-updown' would wrongly refuse the 15m series this pilot was given."""
    assert series.owner_of("bnb-updown-5m") == "bnb-updown-5m"
    assert series.owner_of("bnb-updown-15m") is None
    assert series.live_series("bnb-updown-15m") == ["bnb-updown-15m"]


def test_unparseable_series_is_refused_rather_than_watched():
    with pytest.raises(series.SeriesRefused):
        series.live_series("not-a-series")


def test_series_env_is_read_when_no_argument_is_given(monkeypatch):
    monkeypatch.setenv(series.SERIES_ENV, "doge-updown-5m")
    assert series.live_series() == ["doge-updown-5m"]
    monkeypatch.setenv(series.SERIES_ENV, "btc-updown-5m")
    with pytest.raises(series.SeriesRefused):
        series.live_series()


def test_series_list_is_normalised_and_deduplicated():
    assert series.parse_series(" DOGE-updown-5m , doge-updown-5m ,, hype-updown-5m ") == \
        ["doge-updown-5m", "hype-updown-5m"]


def test_current_slug_sits_on_the_windows_own_grid():
    """Window starts are multiples of the duration, which is what makes the
    live slug computable and gamma a confirmation rather than a scan."""
    assert series.current_slug("doge-updown-5m", 1787400123.0) == "doge-updown-5m-1787400000"
    assert series.current_slug("bnb-updown-15m", 1787400901.0) == "bnb-updown-15m-1787400900"
    assert series.window_bounds("doge-updown-5m-1787400000") == (1787400000.0, 1787400300.0)


def test_symbol_maps_to_the_streams_naming():
    assert series.symbol_of("doge-updown-5m") == "doge/usd"
    assert series.symbol_of("bnb-updown-15m") == "bnb/usd"
    assert series.symbol_of("nonsense") is None
