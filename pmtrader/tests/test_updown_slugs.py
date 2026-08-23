"""Tests for the single updown-slug parser shared by the cli_crypto_* modules, outcomes, and shadow."""

from __future__ import annotations

from polymarket import updown_slugs


def test_parse_updown_slug_extracts_fields():
    w = updown_slugs.parse_updown_slug("btc-updown-15m-1787449500")
    assert w == {"symbol": "btc", "dur_s": 900, "start": 1787449500, "end": 1787450400}


def test_parse_updown_slug_rejects_non_updown():
    assert updown_slugs.parse_updown_slug("btc-1pm-et-1787449500") is None
    assert updown_slugs.parse_updown_slug("not-a-slug") is None


def test_parse_returns_tuple_with_series_key():
    out = updown_slugs.parse("eth-updown-5m-1787449500")
    assert out == ("eth", 300, 1787449500, 1787449800, "eth 5m")


def test_parse_returns_none_for_non_updown():
    assert updown_slugs.parse("not-a-slug") is None


def test_series_key_formats_minutes():
    assert updown_slugs.series_key("btc", 900) == "btc 15m"
    assert updown_slugs.series_key("sol", 300) == "sol 5m"


def test_window_start_reads_trailing_token():
    assert updown_slugs.window_start("btc-updown-15m-1787449500") == 1787449500.0


def test_window_start_zero_on_non_numeric_trailing_token():
    assert updown_slugs.window_start("no-trailing-number-here-") == 0.0
    assert updown_slugs.window_start("nodash") == 0.0


def test_is_updown_is_a_cheap_substring_check():
    assert updown_slugs.is_updown("btc-updown-15m-1787449500")
    assert not updown_slugs.is_updown("btc-1pm-et-1787449500")


def test_display_formats_symbol_duration_and_local_time():
    out = updown_slugs.display("btc-updown-5m-1787442000")
    assert out.startswith("btc 5m ")
    assert len(out.split(" ")[-1]) == 5  # HH:MM


def test_display_falls_back_to_raw_slug_when_unparseable():
    assert updown_slugs.display("garbage") == "garbage"


def test_hour_duration_slugs_are_updown_windows_too():
    # six settled 4h WINS (+$164.35) were invisible while the regex demanded
    # an 'm' token — the ledger of record may never drop a traded format
    w = updown_slugs.parse_updown_slug("btc-updown-4h-1787414400")
    assert w == {"symbol": "btc", "dur_s": 14400, "start": 1787414400, "end": 1787428800}
    assert updown_slugs.series_key(w["symbol"], w["dur_s"]) == "btc 4h"
    assert updown_slugs.parse_updown_slug("eth-updown-1h-1787414400")["dur_s"] == 3600
