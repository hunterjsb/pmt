"""Pure tests for the outcomes priority/staleness logic. No network — everything
here is inline fixtures (activity rows, synthetic Chainlink rounds, tape lines).
"""

import pytest

from polymarket.outcomes import (
    SOURCE_RANK,
    book_outcome,
    build_outcomes,
    chainlink_outcome,
    ck_settlement_width_s,
    exited_flat,
    extract_updown_slugs,
    gamma_resolution,
    grade_window,
    is_terminal_source,
    load_outcomes,
    merge_outcomes,
    parse_updown_slug,
    source_rank,
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
    # one width everywhere: the measured record killed the 30s-at-5m
    # convention (book-graded 283/284 at 60s vs 277/284; settle_width.md)
    assert ck_settlement_width_s(300) == 60    # 5m window
    assert ck_settlement_width_s(900) == 60    # 15m window
    assert ck_settlement_width_s(1800) == 60   # anything wider


# ---------- (a) wallet truth ----------

def test_wallet_outcomes_paying_redeem_names_winner():
    rows = [
        {"type": "REDEEM", "slug": "btc-updown-5m-100", "outcome": "Up", "usdcSize": 51.0},
    ]
    assert wallet_outcomes(rows) == {"btc-updown-5m-100": "up"}


def test_wallet_outcomes_zero_redeem_flips_to_other_side():
    # held "Down" (row carries our real share size), paid $0 -> "up" won.
    # The flip REQUIRES a size (docs/LESSONS.md#L22).
    rows = [
        {"type": "REDEEM", "slug": "btc-updown-15m-200", "outcome": "Down",
         "usdcSize": 0, "size": 120.0},
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
    # 5m window [1000, 1300), width 60s: reference [940,1000), settlement [1240,1300).
    # last round sits exactly at window end so the "corpus caught up" guard clears.
    rounds = _rounds({900: 100.0, 1250: 110.0, 1300: 999.0})
    window = {"slug": "s", "symbol": "btc", "dur_s": 300, "start": 1000, "end": 1300}
    winner, reason = chainlink_outcome(window, rounds)
    assert winner == "up" and reason is None


def test_chainlink_outcome_down_when_settlement_below_reference():
    rounds = _rounds({900: 100.0, 1250: 90.0, 1300: 999.0})
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
    # span_start for this window = 1000 - 60 = 940; a round at exactly 940-600=340
    # is fine, and the reference TWAP rides that maximal 600s carry too. The
    # settlement round sits at 1240 so its whole [1240,1300) span is fresh.
    rounds = _rounds({340: 100.0, 1240: 105.0, 1300: 110.0})
    window = {"slug": "s", "symbol": "btc", "dur_s": 300, "start": 1000, "end": 1300}
    winner, reason = chainlink_outcome(window, rounds)
    assert winner == "up" and reason is None


# ---------- (c) gamma resolution + live grading priority ----------

def test_gamma_resolution_resolved_up():
    markets = [{"outcomes": '["Up", "Down"]', "outcomePrices": '["1", "0"]', "closed": True}]
    assert gamma_resolution(markets) == {"resolved": True, "winner": "up"}


def test_gamma_resolution_resolved_down():
    markets = [{"outcomes": '["Up", "Down"]', "outcomePrices": '["0", "1"]', "closed": True}]
    assert gamma_resolution(markets) == {"resolved": True, "winner": "down"}


def test_gamma_resolution_still_trading_is_not_resolved():
    # closed=False and prices mid-range -- market hasn't settled yet
    markets = [{"outcomes": '["Up", "Down"]', "outcomePrices": '["0.62", "0.38"]', "closed": False}]
    assert gamma_resolution(markets) == {"resolved": False, "winner": None}


def test_gamma_resolution_empty_response_is_unknown_slug():
    assert gamma_resolution([]) == {"resolved": False, "winner": None}


def test_gamma_resolution_tolerant_of_malformed_json():
    markets = [{"outcomes": "not json", "outcomePrices": '["1", "0"]'}]
    assert gamma_resolution(markets) == {"resolved": False, "winner": None}


def test_grade_window_paying_redeem_wins_even_with_dust_redeem_seen():
    # The summed PAYING amount decides the grade, not the presence of a $0
    # row: redeem_seen is True here (the dust row) but redeemed_usd is the
    # paying total, and that has to win the priority check. docs/LESSONS.md#L23.
    won, estimated = grade_window(52.30, True, "up", None, now=2000, end=1000)
    assert won is True and estimated is False


def test_grade_window_zero_redeem_confirms_loss_without_gamma():
    # an actual $0 redemption is ground truth from the wallet -- no need
    # to ask gamma at all.
    won, estimated = grade_window(0.0, True, "up", None, now=2000, end=1000)
    assert won is False and estimated is False


def test_grade_window_no_redeem_within_grace_is_riding():
    won, estimated = grade_window(0.0, False, "up", None, now=1250, end=1000)  # 250s < 300s
    assert won is None and estimated is False


def test_grade_window_past_grace_no_redeem_no_longer_assumes_loss():
    # With no gamma reachable this still degrades to the old assume-LOSS
    # heuristic, but flags it `estimated` (docs/LESSONS.md#L24).
    won, estimated = grade_window(0.0, False, "up", None, now=2000, end=1000)
    assert won is False and estimated is True


def test_grade_window_gamma_confirms_slow_win_past_grace():
    gamma = {"resolved": True, "winner": "up"}
    won, estimated = grade_window(0.0, False, "up", gamma, now=2000, end=1000)
    assert won is True and estimated is False


def test_grade_window_gamma_confirms_loss_past_grace():
    gamma = {"resolved": True, "winner": "down"}
    won, estimated = grade_window(0.0, False, "up", gamma, now=2000, end=1000)
    assert won is False and estimated is False


def test_grade_window_gamma_not_yet_resolved_is_riding():
    gamma = {"resolved": False, "winner": None}
    won, estimated = grade_window(0.0, False, "up", gamma, now=2000, end=1000)
    assert won is None and estimated is False


def test_grade_window_gamma_resolved_but_fired_side_unknown_is_riding():
    # resolved, but we can't tell which side we held -- never guess
    gamma = {"resolved": True, "winner": "up"}
    won, estimated = grade_window(0.0, False, None, gamma, now=2000, end=1000)
    assert won is None and estimated is False


# ---------- priority merge ----------

def test_build_outcomes_wallet_takes_priority_over_chainlink():
    windows = [{"slug": "btc-updown-5m-1000", "symbol": "btc", "dur_s": 300, "start": 1000, "end": 1300}]
    wallet = {"btc-updown-5m-1000": "down"}
    # chainlink rounds would say "up" if consulted -- wallet must win instead
    rounds_by_symbol = {"btc": _rounds({900: 100.0, 1250: 110.0, 1300: 999.0})}
    rows, dropped = build_outcomes(windows, wallet, rounds_by_symbol)
    assert rows == [{"slug": "btc-updown-5m-1000", "winner": "down", "source": "wallet"}]
    assert dropped == []


def test_build_outcomes_falls_back_to_chainlink_when_not_traded():
    windows = [{"slug": "btc-updown-5m-1000", "symbol": "btc", "dur_s": 300, "start": 1000, "end": 1300}]
    rounds_by_symbol = {"btc": _rounds({900: 100.0, 1250: 110.0, 1300: 999.0})}
    rows, dropped = build_outcomes(windows, {}, rounds_by_symbol)
    assert rows == [{"slug": "btc-updown-5m-1000", "winner": "up", "source": "chainlink"}]
    assert dropped == []


def test_build_outcomes_drops_untraded_windows_with_stale_corpus():
    windows = [{"slug": "btc-updown-5m-1000", "symbol": "btc", "dur_s": 300, "start": 1000, "end": 1300}]
    rows, dropped = build_outcomes(windows, {}, {"btc": []})
    assert rows == []
    assert dropped == [{"slug": "btc-updown-5m-1000",
                        "reason": "no corpus data; no terminal book samples"}]


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


# ---------- source ranking: wallet > resolution > chainlink > book ----------

def test_source_rank_orders_the_four_sources():
    assert (source_rank("wallet") > source_rank("resolution")
            > source_rank("chainlink") > source_rank("book"))
    # An unknown source can never outrank a real one, however it got written.
    assert source_rank("vibes") < source_rank("book")
    assert source_rank(None) < source_rank("book")


def test_only_the_exchange_authored_sources_may_grade_a_win_or_a_loss():
    # The wallet's payment and the market's settlement are the exchange's.
    # Chainlink and the terminal book are OUR read, and a model that grades
    # itself grades its own losses as wins.
    assert is_terminal_source("wallet") and is_terminal_source("resolution")
    assert not is_terminal_source("chainlink") and not is_terminal_source("book")
    assert not is_terminal_source(None)
    assert set(SOURCE_RANK) == {"wallet", "resolution", "chainlink", "book"}


@pytest.mark.parametrize("weaker", ["resolution", "chainlink", "book"])
def test_merge_outcomes_never_downgrades_wallet(weaker):
    existing = {"s1": {"slug": "s1", "winner": "up", "source": "wallet"}}
    merged, _added, upgraded = merge_outcomes(
        existing, [{"slug": "s1", "winner": "down", "source": weaker}])
    assert merged["s1"]["source"] == "wallet" and merged["s1"]["winner"] == "up"
    assert upgraded == 0


@pytest.mark.parametrize("weaker", ["chainlink", "book"])
def test_merge_outcomes_resolution_upgrades_our_own_reads(weaker):
    existing = {"s1": {"slug": "s1", "winner": "up", "source": weaker}}
    merged, _added, upgraded = merge_outcomes(
        existing, [{"slug": "s1", "winner": "down", "source": "resolution"}])
    assert merged["s1"] == {"slug": "s1", "winner": "down", "source": "resolution"}
    assert upgraded == 1


def test_merge_outcomes_wallet_still_upgrades_resolution():
    existing = {"s1": {"slug": "s1", "winner": "up", "source": "resolution"}}
    merged, _added, upgraded = merge_outcomes(
        existing, [{"slug": "s1", "winner": "down", "source": "wallet"}])
    assert merged["s1"]["source"] == "wallet" and upgraded == 1


@pytest.mark.parametrize("source", ["wallet", "resolution", "chainlink", "book"])
def test_merge_outcomes_same_source_never_rewrites(source):
    # First write wins, so the order of the walk cannot change the corpus.
    existing = {"s1": {"slug": "s1", "winner": "up", "source": source}}
    merged, added, upgraded = merge_outcomes(
        existing, [{"slug": "s1", "winner": "down", "source": source}])
    assert merged["s1"]["winner"] == "up" and (added, upgraded) == (0, 0)


def test_build_outcomes_resolution_beats_chainlink_but_loses_to_wallet():
    w = {"slug": "btc-updown-5m-1000", "symbol": "btc", "dur_s": 300,
         "start": 1000, "end": 1300}
    # chainlink would read "up" here; the market settled "down".
    rounds = {"btc": _rounds({960: 100.0, 1250: 110.0, 1300: 999.0})}
    res = {"btc-updown-5m-1000": "down"}
    rows, dropped = build_outcomes([w], {}, rounds, None, res)
    assert rows == [{"slug": "btc-updown-5m-1000", "winner": "down", "source": "resolution"}]
    assert dropped == []
    # ...and the wallet still outranks it.
    rows, _ = build_outcomes([w], {"btc-updown-5m-1000": "up"}, rounds, None, res)
    assert rows == [{"slug": "btc-updown-5m-1000", "winner": "up", "source": "wallet"}]


def test_build_outcomes_without_resolutions_is_unchanged():
    w = {"slug": "btc-updown-5m-1000", "symbol": "btc", "dur_s": 300,
         "start": 1000, "end": 1300}
    rounds = {"btc": _rounds({960: 100.0, 1250: 110.0, 1300: 999.0})}
    rows, _ = build_outcomes([w], {}, rounds, None, {})
    assert rows == [{"slug": "btc-updown-5m-1000", "winner": "up", "source": "chainlink"}]


# ---------- a window sold flat can never produce a redeem row ----------

def test_exited_flat_tolerates_data_api_share_dust():
    assert exited_flat(100.0, 99.998625)      # a real 100-share exit
    assert exited_flat(100.0, 100.0)


def test_exited_flat_is_false_while_anything_is_still_held():
    assert not exited_flat(100.0, 70.0)
    assert not exited_flat(100.0, 0.0)
    assert not exited_flat(0.0, 0.0)          # never traded, not "flat"


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


def test_zero_size_dust_redeem_never_flips_blind():
    # Only a SIZED row identifies our held loser; without one, wallet_outcomes
    # must stay silent for the slug (docs/LESSONS.md#L22).
    from polymarket.outcomes import wallet_outcomes
    dust_only = [{"type": "REDEEM", "slug": "btc-updown-15m-1", "usdcSize": 0,
                  "size": 0, "outcome": "Up"}]
    assert wallet_outcomes(dust_only) == {}
    sized = [{"type": "REDEEM", "slug": "btc-updown-15m-1", "usdcSize": 0,
              "size": 265.0, "outcome": "Down"}]
    assert wallet_outcomes(sized) == {"btc-updown-15m-1": "up"}


# ---------- terminal-book source ----------

def _book(t, up_bid=None, up_ask=None, dn_bid=None, dn_ask=None):
    return {"t": t, "up_bid": up_bid, "up_ask": up_ask, "dn_bid": dn_bid, "dn_ask": dn_ask}


def test_book_outcome_grades_pinned_terminal_book():
    w = {"slug": "btc-updown-5m-1000", "symbol": "btc", "dur_s": 300, "start": 1000, "end": 1300}
    recs = [_book(1290, up_bid=0.97, dn_ask=0.03), _book(1295, up_bid=0.98, dn_ask=0.02)]
    assert book_outcome(w, recs) == ("up", None)


def test_book_outcome_refuses_mid_window_tape():
    # tape died at t=1200 with a decisive-looking book — a forecast, not a settlement
    w = {"slug": "btc-updown-5m-1000", "symbol": "btc", "dur_s": 300, "start": 1000, "end": 1300}
    recs = [_book(1150, up_bid=0.97, dn_ask=0.02), _book(1200, up_bid=0.98, dn_ask=0.02)]
    winner, reason = book_outcome(w, recs)
    assert winner is None and reason == "no terminal book samples"


def test_book_outcome_needs_two_agreeing_samples():
    w = {"slug": "btc-updown-5m-1000", "symbol": "btc", "dur_s": 300, "start": 1000, "end": 1300}
    winner, reason = book_outcome(w, [_book(1295, up_bid=0.97, dn_ask=0.02)])
    assert winner is None and reason == "terminal book ambiguous"


def test_book_outcome_refuses_unpinned_or_contested_book():
    w = {"slug": "btc-updown-5m-1000", "symbol": "btc", "dur_s": 300, "start": 1000, "end": 1300}
    # not pinned hard enough
    recs = [_book(1290, up_bid=0.90, dn_ask=0.11), _book(1295, up_bid=0.91, dn_ask=0.10)]
    assert book_outcome(w, recs)[0] is None
    # pinned both ways across samples (flip at the wire) -> refuse
    recs = [_book(1290, up_bid=0.97, dn_ask=0.02), _book(1294, up_bid=0.97, dn_ask=0.02),
            _book(1299, dn_bid=0.96, up_ask=0.03)]
    assert book_outcome(w, recs)[0] is None


def test_build_outcomes_book_is_strictly_last():
    w = {"slug": "btc-updown-5m-1000", "symbol": "btc", "dur_s": 300, "start": 1000, "end": 1300}
    book = {"btc-updown-5m-1000": [_book(1290, dn_bid=0.97, up_ask=0.02),
                                    _book(1295, dn_bid=0.98, up_ask=0.02)]}
    # chainlink says up and is fresh -> book (down) must NOT be consulted
    rounds = {"btc": _rounds({900: 100.0, 1250: 110.0, 1300: 999.0})}
    rows, _ = build_outcomes([w], {}, rounds, book)
    assert rows == [{"slug": w["slug"], "winner": "up", "source": "chainlink"}]
    # chainlink stale -> book grades
    rows, dropped = build_outcomes([w], {}, {"btc": []}, book)
    assert rows == [{"slug": w["slug"], "winner": "down", "source": "book"}]
    assert dropped == []


def test_merge_outcomes_wallet_and_chainlink_upgrade_book():
    existing = {"s1": {"slug": "s1", "winner": "up", "source": "book"}}
    merged, _, upgraded = merge_outcomes(existing, [{"slug": "s1", "winner": "down", "source": "wallet"}])
    assert merged["s1"]["source"] == "wallet" and upgraded == 1
    existing = {"s1": {"slug": "s1", "winner": "up", "source": "book"}}
    merged, _, upgraded = merge_outcomes(existing, [{"slug": "s1", "winner": "down", "source": "chainlink"}])
    assert merged["s1"]["source"] == "chainlink" and upgraded == 1
    # book never overwrites anything
    existing = {"s1": {"slug": "s1", "winner": "up", "source": "chainlink"}}
    merged, _, upgraded = merge_outcomes(existing, [{"slug": "s1", "winner": "down", "source": "book"}])
    assert merged["s1"]["source"] == "chainlink" and upgraded == 0


def test_chainlink_outcome_refuses_margin_inside_noise_floor():
    # ~2bp move: real settlements this close live inside flat-hold interpolation
    # error (measured 2026-08-23: sub-1bp labels worse than a coin flip vs wallet)
    w = {"slug": "btc-updown-5m-1000", "symbol": "btc", "dur_s": 300, "start": 1000, "end": 1300}
    rounds = _rounds({900: 100.0, 1240: 100.02, 1300: 100.02})
    winner, reason = chainlink_outcome(w, rounds)
    assert winner is None and "noise floor" in reason
