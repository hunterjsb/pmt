"""Pure tests for the outcomes priority/staleness logic. No network — everything
here is inline fixtures (activity rows, synthetic Chainlink rounds, tape lines).
"""

import pytest

from polymarket.outcomes import (
    build_outcomes,
    chainlink_outcome,
    ck_settlement_width_s,
    extract_updown_slugs,
    load_outcomes,
    merge_outcomes,
    parse_updown_slug,
    wallet_outcomes,
    window_universe,
    write_outcomes,
)


# ---------- slug parsing / window universe ----------

def test_parse_updown_slug():
    w = parse_updown_slug("btc-updown-15m-1787449500")
    assert w == {"symbol": "btc", "dur_s": 900, "start": 1787449500, "end": 1787450400}


def test_parse_updown_slug_rejects_non_updown():
    assert parse_updown_slug("btc-1pm-et-1787449500") is None
    assert parse_updown_slug("not-a-slug") is None


def test_extract_updown_slugs_tolerant_of_junk():
    lines = [
        '{"ev": "book", "slug": "btc-updown-5m-100"}',
        "",
        "not json",
        '{"ev": "fire", "slug": "eth-updown-15m-200"}',
        '{"ev": "cleanup"}',  # no slug
        '{"ev": "book", "slug": "btc-updown-5m-100"}',  # dup
    ]
    assert extract_updown_slugs(lines) == {"btc-updown-5m-100", "eth-updown-15m-200"}


def test_window_universe_filters_open_and_before_since():
    now = 10_000.0
    slugs = {
        "btc-updown-5m-9000",   # end=9300, closed, >= since -> in
        "btc-updown-5m-9985",   # end=10285 > now-30 -> still open, excluded
        "btc-updown-5m-500",    # start < since -> excluded
        "junk-slug",
    }
    out = window_universe(slugs, since=1000, now=now)
    assert [w["slug"] for w in out] == ["btc-updown-5m-9000"]


def test_window_universe_sorted_by_start():
    slugs = {"btc-updown-5m-2000", "btc-updown-5m-1000"}
    out = window_universe(slugs, since=0, now=100_000)
    assert [w["start"] for w in out] == [1000, 2000]


def test_ck_settlement_width():
    assert ck_settlement_width_s(300) == 30    # 5m window
    assert ck_settlement_width_s(900) == 60    # 15m window
    assert ck_settlement_width_s(1800) == 60   # anything wider than 5m


# ---------- (a) wallet truth ----------

def test_wallet_outcomes_paying_redeem_names_winner():
    rows = [
        {"type": "REDEEM", "slug": "btc-updown-5m-100", "outcome": "Up", "usdcSize": 51.0},
    ]
    assert wallet_outcomes(rows) == {"btc-updown-5m-100": "up"}


def test_wallet_outcomes_zero_redeem_flips_to_other_side():
    # held "Down", it paid $0 -> the wallet lost, "up" actually won
    rows = [
        {"type": "REDEEM", "slug": "btc-updown-15m-200", "outcome": "Down", "usdcSize": 0},
    ]
    assert wallet_outcomes(rows) == {"btc-updown-15m-200": "up"}


def test_wallet_outcomes_ignores_non_redeem_and_non_updown():
    rows = [
        {"type": "TRADE", "slug": "btc-updown-5m-100", "side": "BUY", "usdcSize": 10.0},
        {"type": "REDEEM", "slug": "some-other-market", "outcome": "Yes", "usdcSize": 5.0},
    ]
    assert wallet_outcomes(rows) == {}


def test_wallet_outcomes_prefers_paying_row_when_both_sides_redeemed():
    # edge case: redeemed both outcome tokens on the same slug (held both)
    rows = [
        {"type": "REDEEM", "slug": "eth-updown-5m-300", "outcome": "Down", "usdcSize": 0},
        {"type": "REDEEM", "slug": "eth-updown-5m-300", "outcome": "Up", "usdcSize": 40.0},
    ]
    assert wallet_outcomes(rows) == {"eth-updown-5m-300": "up"}


# ---------- (b) Chainlink corpus inference + staleness guard ----------

def _rounds(prices_by_t: dict[int, float]) -> list[dict]:
    return [{"round_id": i, "price": p, "updated_at": t}
            for i, (t, p) in enumerate(sorted(prices_by_t.items()))]


def test_chainlink_outcome_up_when_settlement_above_reference():
    # 5m window [1000, 1300), width 30s: reference [970,1000), settlement [1270,1300).
    # last round sits exactly at window end so the "corpus caught up" guard clears.
    rounds = _rounds({960: 100.0, 1250: 110.0, 1300: 999.0})
    window = {"slug": "s", "symbol": "btc", "dur_s": 300, "start": 1000, "end": 1300}
    winner, reason = chainlink_outcome(window, rounds)
    assert winner == "up" and reason is None


def test_chainlink_outcome_down_when_settlement_below_reference():
    rounds = _rounds({960: 100.0, 1250: 90.0, 1300: 999.0})
    window = {"slug": "s", "symbol": "btc", "dur_s": 300, "start": 1000, "end": 1300}
    winner, reason = chainlink_outcome(window, rounds)
    assert winner == "down" and reason is None


def test_chainlink_outcome_no_corpus_data():
    window = {"slug": "s", "symbol": "btc", "dur_s": 300, "start": 1000, "end": 1300}
    winner, reason = chainlink_outcome(window, [])
    assert winner is None and reason == "no corpus data"


def test_chainlink_outcome_dropped_when_last_round_predates_window_end():
    # last known round is well before the window even opens -> classic stale step-extension
    rounds = _rounds({500: 100.0, 600: 101.0})
    window = {"slug": "s", "symbol": "btc", "dur_s": 300, "start": 1000, "end": 1300}
    winner, reason = chainlink_outcome(window, rounds)
    assert winner is None
    assert "predates window end" in reason


def test_chainlink_outcome_dropped_when_no_round_within_10min_of_query_span():
    # a round exists before span_start, but > 600s earlier -> no valid carry-in.
    # last round pinned at window end so this isn't caught by the other guard instead.
    rounds = _rounds({0: 100.0, 1300: 110.0})
    window = {"slug": "s", "symbol": "btc", "dur_s": 300, "start": 1000, "end": 1300}
    winner, reason = chainlink_outcome(window, rounds)
    assert winner is None
    assert "10min" in reason


def test_chainlink_outcome_boundary_exactly_10min_is_still_ok():
    # span_start for this window = 1000 - 30 = 970; a round at exactly 970-600=370 is fine
    rounds = _rounds({370: 100.0, 1260: 105.0, 1300: 110.0})
    window = {"slug": "s", "symbol": "btc", "dur_s": 300, "start": 1000, "end": 1300}
    winner, reason = chainlink_outcome(window, rounds)
    assert winner == "up" and reason is None


# ---------- priority merge ----------

def test_build_outcomes_wallet_takes_priority_over_chainlink():
    windows = [{"slug": "btc-updown-5m-1000", "symbol": "btc", "dur_s": 300, "start": 1000, "end": 1300}]
    wallet = {"btc-updown-5m-1000": "down"}
    # chainlink rounds would say "up" if consulted -- wallet must win instead
    rounds_by_symbol = {"btc": _rounds({960: 100.0, 1250: 110.0, 1300: 999.0})}
    rows, dropped = build_outcomes(windows, wallet, rounds_by_symbol)
    assert rows == [{"slug": "btc-updown-5m-1000", "winner": "down", "source": "wallet"}]
    assert dropped == []


def test_build_outcomes_falls_back_to_chainlink_when_not_traded():
    windows = [{"slug": "btc-updown-5m-1000", "symbol": "btc", "dur_s": 300, "start": 1000, "end": 1300}]
    rounds_by_symbol = {"btc": _rounds({960: 100.0, 1250: 110.0, 1300: 999.0})}
    rows, dropped = build_outcomes(windows, {}, rounds_by_symbol)
    assert rows == [{"slug": "btc-updown-5m-1000", "winner": "up", "source": "chainlink"}]
    assert dropped == []


def test_build_outcomes_drops_untraded_windows_with_stale_corpus():
    windows = [{"slug": "btc-updown-5m-1000", "symbol": "btc", "dur_s": 300, "start": 1000, "end": 1300}]
    rows, dropped = build_outcomes(windows, {}, {"btc": []})
    assert rows == []
    assert dropped == [{"slug": "btc-updown-5m-1000", "reason": "no corpus data"}]


# ---------- outcomes corpus: append + dedupe, wallet upgrades chainlink ----------

def test_merge_outcomes_adds_new_rows():
    merged, added, upgraded = merge_outcomes({}, [{"slug": "s1", "winner": "up", "source": "wallet"}])
    assert merged == {"s1": {"slug": "s1", "winner": "up", "source": "wallet"}}
    assert added == 1 and upgraded == 0


def test_merge_outcomes_wallet_upgrades_chainlink():
    existing = {"s1": {"slug": "s1", "winner": "down", "source": "chainlink"}}
    new = [{"slug": "s1", "winner": "up", "source": "wallet"}]
    merged, added, upgraded = merge_outcomes(existing, new)
    assert merged["s1"] == {"slug": "s1", "winner": "up", "source": "wallet"}
    assert added == 0 and upgraded == 1


def test_merge_outcomes_never_downgrades_wallet_to_chainlink():
    existing = {"s1": {"slug": "s1", "winner": "up", "source": "wallet"}}
    new = [{"slug": "s1", "winner": "down", "source": "chainlink"}]
    merged, added, upgraded = merge_outcomes(existing, new)
    # wallet row is untouched even though the new chainlink read disagrees
    assert merged["s1"] == {"slug": "s1", "winner": "up", "source": "wallet"}
    assert added == 0 and upgraded == 0


def test_write_and_load_outcomes_roundtrip(tmp_path):
    path = tmp_path / "outcomes.jsonl"
    merged = {
        "btc-updown-5m-2000": {"slug": "btc-updown-5m-2000", "winner": "up", "source": "chainlink"},
        "btc-updown-5m-1000": {"slug": "btc-updown-5m-1000", "winner": "down", "source": "wallet"},
    }
    write_outcomes(merged, path)
    loaded = load_outcomes(path)
    assert loaded == merged
    # chronological by window start despite dict insertion order
    lines = path.read_text().splitlines()
    assert '"btc-updown-5m-1000"' in lines[0]
    assert '"btc-updown-5m-2000"' in lines[1]


def test_load_outcomes_missing_file_is_empty(tmp_path):
    assert load_outcomes(tmp_path / "nope.jsonl") == {}
