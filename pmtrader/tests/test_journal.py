"""Pure-seam tests for the trade journal: notable detection per class, the
high-water-mark / seen-key idempotency contract, state survival across runs,
and the markdown the file actually accumulates.

Every fixture below is inline — graded windows shaped like
cli_crypto.score_activity's `eff_windows`, raw tape lines shaped like the
engine's own, arms shaped like arms-state.json. No network, no ~/.pmt, no
engine: the whole module is pure detection over already-read inputs, which is
the point of keeping the I/O in cli_crypto.
"""

from __future__ import annotations

import json
import time

import pytest

from polymarket import journal as j

# A window mid-morning local time, so a day boundary never lands inside a test.
T0 = time.mktime((2026, 8, 23, 10, 0, 0, 0, 0, -1))
DAY = 86400.0


def _win(slug: str, pnl: float, end_ts: float, *, notional: float = 100.0,
         won: bool | None = None, est: bool = False) -> dict:
    return {"slug": slug, "won": pnl > 0 if won is None else won, "pnl": pnl,
            "est": est, "end_ts": end_ts, "notional": notional,
            "entry_ts": end_ts - 200, "exit_ts": end_ts + 30}


def _eval_line(slug: str, t: float, side: str, *, brake: str | None = None,
               ask: float = 0.9, fair: float = 0.99, net: float = 0.08) -> str:
    s: dict = {"side": side, "ask": ask, "fair": fair, "net": net}
    if brake:
        s["brake"] = brake
    return json.dumps({"ev": "eval", "slug": slug, "t": t, "sides": [s]}) + "\n"


def _fire(slug: str, t: float, side: str, ask: float, size: float) -> dict:
    return {"t": t, "slug": slug, "side": side, "ask": ask, "size": size}


# ---------- day extremes ----------

def test_day_extremes_names_the_biggest_win_and_loss_of_each_day():
    windows = [
        _win("btc-updown-5m-1", 12.0, T0),
        _win("eth-updown-5m-2", 40.0, T0 + 60),      # the day's best
        _win("sol-updown-5m-3", -8.0, T0 + 120),
        _win("xrp-updown-5m-4", -90.0, T0 + 180),    # the day's worst
        _win("bnb-updown-5m-5", 500.0, T0 - DAY),    # a different day entirely
    ]
    lines = [e["line"] for e in j.day_extremes(windows, {})]
    assert any("eth 5m best of the day +$40.00" in ln for ln in lines)
    assert any("xrp 5m worst of the day -$90.00" in ln for ln in lines)
    assert any("bnb 5m best of the day +$500.00" in ln for ln in lines)
    assert not any("btc 5m" in ln or "sol 5m" in ln for ln in lines)


def test_day_extremes_stay_silent_until_the_figure_is_actually_beaten():
    windows = [_win("btc-updown-5m-1", 40.0, T0)]
    state = {"day_best": {j._day(T0): 40.0}}
    assert j.day_extremes(windows, state) == []

    windows.append(_win("eth-updown-5m-2", 41.0, T0 + 60))
    (ev,) = j.day_extremes(windows, state)
    assert "eth 5m best of the day +$41.00" in ev["line"]
    assert ev["state"] == {"day_best": {j._day(T0): 41.0}}


def test_a_full_forfeit_says_the_whole_stake_went():
    (ev,) = j.day_extremes([_win("btc-updown-5m-1", -300.0, T0, notional=300.0)], {})
    assert "the whole $300 went" in ev["line"]


def test_a_partial_loss_reports_what_was_committed_instead():
    (ev,) = j.day_extremes([_win("btc-updown-5m-1", -30.0, T0, notional=300.0)], {})
    assert "$300 committed" in ev["line"]


def test_sub_dollar_windows_are_not_anybodys_headline():
    assert j.day_extremes([_win("btc-updown-5m-1", 0.10, T0)], {}) == []


def test_an_imputed_pnl_is_marked_estimated():
    (ev,) = j.day_extremes([_win("btc-updown-5m-1", 40.0, T0, est=True)], {})
    assert ev["line"].endswith("(est)")


# ---------- latch saves ----------

def test_latch_save_prices_a_refused_side_that_went_on_to_lose():
    slug = "btc-updown-5m-1787490000"
    lines = [_eval_line(slug, T0 + i, "down", brake="latched", ask=0.5)
             for i in range(3)]
    fires = [_fire(slug, T0, "up", 0.9, 100.0)]  # clip = 100*0.9 = $90
    (ev,) = j.latch_saves(lines, {slug: "up"}, fires, {})
    assert "btc 5m latch held down" in ev["line"]
    assert "$90 not spent" in ev["line"]


def test_latch_save_reports_what_the_window_finally_closed_at():
    slug = "btc-updown-5m-1787490000"
    lines = [_eval_line(slug, T0, "down", brake="latched", ask=0.5)]
    fires = [_fire(slug, T0, "up", 0.9, 100.0)]
    graded = {slug: _win(slug, 15.5, T0 + 300)}
    (ev,) = j.latch_saves(lines, {slug: "up"}, fires, graded)
    assert "window closed +$15.50" in ev["line"]


def test_a_latch_that_refused_the_eventual_winner_is_not_a_save():
    slug = "btc-updown-5m-1787490000"
    lines = [_eval_line(slug, T0, "up", brake="latched", ask=0.5)]
    fires = [_fire(slug, T0, "up", 0.9, 100.0)]
    assert j.latch_saves(lines, {slug: "up"}, fires, {}) == []


def test_a_sub_clip_latch_refusal_is_noise_not_a_story():
    slug = "btc-updown-5m-1787490000"
    lines = [_eval_line(slug, T0, "down", brake="latched", ask=0.5)]
    fires = [_fire(slug, T0, "up", 0.9, 10.0)]  # clip = $9, under one default clip
    assert j.latch_saves(lines, {slug: "up"}, fires, {}) == []


def test_an_unresolved_window_is_never_guessed_into_a_save():
    slug = "btc-updown-5m-1787490000"
    lines = [_eval_line(slug, T0, "down", brake="latched", ask=0.5)]
    fires = [_fire(slug, T0, "up", 0.9, 100.0)]
    assert j.latch_saves(lines, {}, fires, {}) == []


def test_other_brakes_are_not_latch_saves():
    slug = "btc-updown-5m-1787490000"
    lines = [_eval_line(slug, T0, "down", brake="safety", ask=0.5)]
    fires = [_fire(slug, T0, "up", 0.9, 100.0)]
    assert j.latch_saves(lines, {slug: "up"}, fires, {}) == []


# ---------- firsts ----------

def test_first_window_of_a_new_symbol_is_journaled_once():
    windows = [_win("doge-updown-5m-1", 4.0, T0, notional=40.0),
               _win("doge-updown-5m-2", 5.0, T0 + 600, notional=50.0)]
    evs = j.firsts(windows, [], {}, [], {}, T0)
    assert [e["key"] for e in evs] == ["first:symbol:doge"]
    assert "first doge window filled — $40 in, won" in evs[0]["line"]
    assert evs[0]["state"] == {"symbols": {"doge": "doge-updown-5m-1"}}


def test_a_symbol_already_in_state_is_not_a_first_again():
    windows = [_win("doge-updown-5m-1", 4.0, T0)]
    assert j.firsts(windows, [], {}, [], {"symbols": {"doge": "x"}}, T0) == []


def test_first_rtds_arm_comes_off_the_arm_store():
    arms = [{"slug": "btc-updown-5m-100", "feed": "binance", "start": 100.0},
            {"slug": "xrp-updown-5m-200", "feed": "rtds", "start": 200.0}]
    (ev,) = j.firsts([], [], {}, arms, {}, T0)
    assert ev["key"] == "first:rtds" and ev["t"] == 200.0
    assert "xrp 5m first window off the rtds feed" in ev["line"]


def test_first_post_only_bid_comes_off_the_order_tape():
    orders = [{"stage": "ack", "t": T0, "price": "0.98", "post_only": True,
               "order_id": "0xabc"},
              {"stage": "ack", "t": T0 - 10, "price": "0.90"}]  # a taker ack
    (ev,) = j.firsts([], orders, {}, [], {}, T0)
    assert ev["key"] == "first:maker-rest" and ev["t"] == T0
    assert "first post-only bid rested at 0.98" in ev["line"]


def test_first_maker_fill_is_stamped_at_run_time_and_says_the_join_is_weak():
    (ev,) = j.firsts([], [], {"fills": 1, "fill_usd": 49.0}, [], {}, T0)
    assert ev["key"] == "first:maker-fill" and ev["t"] == T0
    assert "circumstantial" in ev["line"]


def test_no_maker_fill_no_line():
    assert j.firsts([], [], {"fills": 0, "fill_usd": 0.0}, [], {}, T0) == []


# ---------- streak milestones ----------

def test_milestones_are_25_50_then_every_50():
    assert [n for n in range(1, 210) if j.is_milestone(n)] == [25, 50, 100, 150, 200]


def test_streak_milestone_fires_on_the_window_that_reached_it():
    windows = [_win(f"btc-updown-5m-{i}", 1.0, T0 + i) for i in range(25)]
    (ev,) = j.streak_milestones(windows)
    assert ev["line"] == "25 in a row — btc 5m kept it alive"
    assert ev["t"] == T0 + 24


def test_a_loss_resets_the_run_so_the_milestone_never_lands():
    windows = [_win(f"btc-updown-5m-{i}", 1.0, T0 + i) for i in range(24)]
    windows.append(_win("btc-updown-5m-99", -5.0, T0 + 24))
    windows += [_win(f"eth-updown-5m-{i}", 1.0, T0 + 100 + i) for i in range(10)]
    assert j.streak_milestones(windows) == []


# ---------- scale changes ----------

def _arm(slug: str, size: float, clip: float) -> dict:
    return {"slug": slug, "size_usdc": size, "clip_usdc": clip}


def test_first_sight_of_a_series_seeds_silently():
    arms = [_arm("btc-updown-5m-1", 1000.0, 150.0)]
    assert j.scale_changes(arms, {}, T0) == []


def test_a_size_and_clip_move_reads_as_one_line():
    arms = [_arm("btc-updown-5m-1", 1000.0, 150.0)]
    state = {"scale": {"btc 5m": {"size": 400.0, "clip": 50.0}}}
    (ev,) = j.scale_changes(arms, state, T0)
    assert ev["line"] == "btc 5m sized up $400→$1,000, clip $50→$150"


def test_a_size_cut_says_sized_down():
    arms = [_arm("btc-updown-5m-1", 100.0, 50.0)]
    state = {"scale": {"btc 5m": {"size": 400.0, "clip": 50.0}}}
    (ev,) = j.scale_changes(arms, state, T0)
    assert "sized down $400→$100" in ev["line"] and "clip" not in ev["line"]


def test_a_roll_to_the_next_window_is_not_a_scale_change():
    state = {"scale": {"btc 5m": {"size": 400.0, "clip": 50.0}}}
    assert j.scale_changes([_arm("btc-updown-5m-999", 400.0, 50.0)], state, T0) == []


def test_note_scale_converges_even_when_the_line_was_already_written():
    # The change fires once; its key is then `seen`, so `select` filters the
    # repeat. Only note_scale's unconditional baseline stops the state from
    # sitting on the old figure forever.
    state = {"scale": {"btc 5m": {"size": 400.0, "clip": 50.0}}}
    arms = [_arm("btc-updown-5m-1", 1000.0, 150.0)]
    j.note_scale(state, arms)
    assert j.scale_changes(arms, state, T0) == []


# ---------- selection, high-water mark, state ----------

def test_select_drops_events_behind_the_floor():
    events = [j._ev(T0 - 100, "a", "old"), j._ev(T0 + 100, "b", "new")]
    assert [e["key"] for e in j.select(events, {}, T0)] == ["b"]


def test_select_drops_keys_already_emitted():
    events = [j._ev(T0, "a", "one"), j._ev(T0 + 1, "b", "two")]
    assert [e["key"] for e in j.select(events, {"seen": ["a"]}, 0.0)] == ["b"]


def test_select_returns_oldest_first():
    events = [j._ev(T0 + 5, "b", "later"), j._ev(T0, "a", "earlier")]
    assert [e["key"] for e in j.select(events, {}, 0.0)] == ["a", "b"]


def test_commit_moves_the_hwm_only_as_far_as_what_was_written():
    state: dict = {}
    j.commit(state, [j._ev(T0, "a", "x"), j._ev(T0 + 50, "b", "y")], T0 + 999)
    assert state["hwm"] == T0 + 50
    assert state["seen"] == ["a", "b"]


def test_commit_of_nothing_leaves_the_hwm_alone():
    state = {"hwm": T0}
    j.commit(state, [], T0 + 999)
    assert state["hwm"] == T0


def test_commit_folds_each_events_own_memory_in():
    state: dict = {"symbols": {"btc": "old"}}
    j.commit(state, [j._ev(T0, "a", "x", {"symbols": {"doge": "new"}})], T0)
    assert state["symbols"] == {"btc": "old", "doge": "new"}


def test_seen_keys_are_capped_so_the_state_file_never_grows_without_bound():
    state = {"seen": [f"k{i}" for i in range(j.SEEN_CAP + 50)]}
    j.commit(state, [j._ev(T0, "fresh", "x")], T0)
    assert len(state["seen"]) == j.SEEN_CAP
    assert state["seen"][-1] == "fresh"


def test_floor_backs_off_the_hwm_by_the_late_redeem_slack():
    assert j.floor_for({"hwm": T0}, None, T0) == T0 - j.LOOKBACK_SLACK_S


def test_a_state_that_has_never_run_looks_back_one_day():
    assert j.floor_for({}, None, T0) == T0 - j.FIRST_RUN_LOOKBACK_S


def test_an_explicit_since_wins_outright():
    assert j.floor_for({"hwm": T0}, 0.0, T0) == 0.0


def test_state_survives_a_round_trip_through_disk(tmp_path):
    path = tmp_path / "state.json"
    j.save_state({"hwm": T0, "seen": ["a"], "scale": {"btc 5m": {"size": 1.0}}}, path)
    assert j.load_state(path) == {"hwm": T0, "seen": ["a"],
                                  "scale": {"btc 5m": {"size": 1.0}}}


def test_a_corrupt_state_file_is_a_fresh_start_not_a_crash(tmp_path):
    path = tmp_path / "state.json"
    path.write_text("{not json")
    assert j.load_state(path) == {}


def test_a_missing_state_file_is_a_fresh_start(tmp_path):
    assert j.load_state(tmp_path / "nope.json") == {}


# ---------- the file itself ----------

def test_append_writes_the_header_and_a_day_heading(tmp_path):
    path = tmp_path / "journal.md"
    j.append([j._ev(T0, "a", "btc 5m did a thing")], path)
    text = path.read_text()
    assert text.startswith(j.HEADER)
    assert f"## {j._day(T0)}" in text
    assert f"{j._stamp(T0)}  btc 5m did a thing" in text


def test_a_second_day_opens_its_own_heading(tmp_path):
    path = tmp_path / "journal.md"
    j.append([j._ev(T0 - DAY, "a", "yesterday")], path)
    j.append([j._ev(T0, "b", "today")], path)
    text = path.read_text()
    assert f"## {j._day(T0 - DAY)}" in text and f"## {j._day(T0)}" in text


def test_a_backfill_repeats_the_heading_rather_than_joining_the_wrong_day(tmp_path):
    path = tmp_path / "journal.md"
    j.append([j._ev(T0, "a", "today")], path)
    j.append([j._ev(T0 - DAY, "b", "backfilled")], path)
    lines = path.read_text().splitlines()
    # The backfilled line must sit under its OWN heading at the bottom.
    assert lines[-2] == f"## {j._day(T0 - DAY)}"
    assert lines[-1].endswith("backfilled")


def test_append_of_nothing_does_not_create_a_file(tmp_path):
    path = tmp_path / "journal.md"
    assert j.append([], path) == 0
    assert not path.exists()


def test_running_twice_writes_no_duplicate_lines(tmp_path):
    """The end-to-end idempotency contract: detect -> select -> append ->
    commit, run again over the identical inputs, nothing new lands."""
    path, state = tmp_path / "journal.md", {}
    slug = "btc-updown-5m-1787490000"
    inputs = dict(
        windows=[_win(slug, 40.0, T0), _win("eth-updown-5m-2", -90.0, T0 + 60)],
        tape_lines=[_eval_line(slug, T0, "down", brake="latched", ask=0.5)],
        orders=[], fires=[_fire(slug, T0, "up", 0.9, 100.0)],
        winners={slug: "up"}, maker={}, arms=[], now=T0 + 500)

    for _ in range(2):
        written = j.select(j.detect(state=state, **inputs), state, 0.0)
        j.append(written, path)
        j.commit(state, written, inputs["now"])

    body = [ln for ln in path.read_text().splitlines() if ln and not ln.startswith("#")]
    assert len(body) == len(set(body)) and body


def test_a_since_backfill_over_ground_already_covered_adds_nothing(tmp_path):
    path, state = tmp_path / "journal.md", {}
    inputs = dict(windows=[_win("btc-updown-5m-1", 40.0, T0)], tape_lines=[],
                  orders=[], fires=[], winners={}, maker={}, arms=[], now=T0)
    written = j.select(j.detect(state=state, **inputs), state, T0 - 60)
    j.append(written, path)
    j.commit(state, written, T0)
    before = path.read_text()

    # --since 0: a deliberate walk back behind the high-water mark.
    again = j.select(j.detect(state=state, **inputs), state, 0.0)
    j.append(again, path)
    assert again == [] and path.read_text() == before


# ---------- --show ----------

def test_tail_returns_the_last_entries_under_their_headings(tmp_path):
    path = tmp_path / "journal.md"
    j.append([j._ev(T0 - DAY, "a", "old one")], path)
    j.append([j._ev(T0, "b", "new one"), j._ev(T0 + 1, "c", "newer one")], path)
    out = j.tail(path, n=2)
    assert out[0] == f"## {j._day(T0)}"
    assert out[-1].endswith("newer one")
    assert not any("old one" in ln for ln in out)


def test_tail_of_a_missing_journal_is_empty_not_an_error(tmp_path):
    assert j.tail(tmp_path / "nope.md", n=5) == []


def test_styled_colours_the_sign_and_dims_the_clause_after_the_dash():
    out = j.styled("10:00  btc 5m best of the day +$40.00 — $100 committed")
    assert "[green]+$40.00[/]" in out
    assert "[dim]$100 committed[/dim]" in out
    assert out.startswith("[dim]10:00[/dim]")


def test_styled_paints_a_loss_red():
    assert "[red]-$90.00[/]" in j.styled("10:00  eth 5m worst of the day -$90.00")


def test_styled_bolds_a_day_heading():
    assert j.styled("## 2026-08-23") == "[bold]## 2026-08-23[/bold]"


def test_styled_escapes_a_line_that_looks_like_markup():
    # Journal lines are data. A slug or symbol carrying brackets must not be
    # able to open a Rich tag in the operator's terminal.
    assert "\\[" in j.styled("10:00  [bold]not a tag[/bold]")


@pytest.mark.parametrize("pnl,expected", [(4.5, "+$4.50"), (-4.5, "-$4.50"),
                                          (0.0, "+$0.00")])
def test_money_always_carries_its_sign(pnl, expected):
    assert j._money(pnl) == expected
