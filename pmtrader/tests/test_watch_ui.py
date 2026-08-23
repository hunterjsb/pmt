"""Pure-seam tests for the crypto watch dashboard's render layer: margin-regex
parsing, evidence/countdown color thresholds, the tape log's fixed-width
alignment, the risk header's committed/undecided math, the merged windows
table (lifecycle glyphs, live/riding/decided merge, retention), the controls
modal, the arms table's column geometry, and the terminal-mode helpers.

Every dashboard render function must tolerate a missing/partial eval (an
engine restart mid-watch leaves last_eval None or half-built) — several tests
below exist specifically to pin that behavior down.

The fetch/grade side of the same dashboard is tested in
test_cli_crypto_watch.py and test_cli_crypto_stats.py.
"""

from __future__ import annotations

import json
import time
from collections import deque

import click
import pytest

import watch_ui as cc

# ---------- margin-regex / gated-reason compaction ----------

def test_gated_reason_compact_parses_basis_guard():
    reason = ("basis guard: projected margin -4.9bp inside 6.0bp noise band "
              "[banked -3.2bp cushion 9.3bp]")
    assert cc._gated_reason_compact(reason) == "margin -4.9 vs 6.0bp"


def test_gated_reason_compact_positive_margin():
    reason = ("basis guard: projected margin +12.3bp inside 6.0bp noise band "
              "[banked 1bp cushion 2bp]")
    assert cc._gated_reason_compact(reason) == "margin +12.3 vs 6.0bp"


def test_gated_reason_compact_falls_back_to_raw_when_regex_misses():
    reason = "window 42% elapsed, firing opens at 50%"
    assert cc._gated_reason_compact(reason) == reason


def test_gated_reason_compact_handles_missing_reason():
    assert cc._gated_reason_compact(None) == "gated"
    assert cc._gated_reason_compact("") == "gated"


def test_gated_reason_compact_prefers_structured_fields_over_the_regex():
    # A reworded reason the regex cannot touch — the fields still render it.
    e = {"reason": "guard says no", "margin_bp": -4.9, "guard_bp": 6.0}
    assert cc._gated_reason_compact(e["reason"], e) == "margin -4.9 vs 6.0bp"


def test_gated_reason_compact_falls_back_to_regex_on_a_pre_structured_eval():
    # Engine built before the fields shipped: reason only, nulls (or no keys).
    reason = ("basis guard: projected margin -4.9bp inside 6.0bp noise band "
              "[banked -3.2bp cushion 9.3bp]")
    e = {"reason": reason, "margin_bp": None, "guard_bp": None}
    assert cc._gated_reason_compact(reason, e) == "margin -4.9 vs 6.0bp"
    assert cc._gated_reason_compact(reason, {"reason": reason}) == "margin -4.9 vs 6.0bp"


def test_gated_reason_compact_structured_survives_a_non_basis_gate():
    # feed stale: nulls everywhere, reason still shown verbatim.
    e = {"reason": "feed stale", "margin_bp": None, "guard_bp": None}
    assert cc._gated_reason_compact("feed stale", e) == "feed stale"


# ---------- evidence column color thresholds ----------

def test_evidence_style_banked_decided_is_always_green():
    assert cc._evidence_style(0.1, 9.3, True) == "green"


def test_evidence_style_ratio_thresholds():
    assert cc._evidence_style(9.3, 9.3, False) == "green"   # |banked| == cushion
    assert cc._evidence_style(3.0, 9.3, False) == "yellow"  # 0.3*9.3=2.79 <= 3.0 < 9.3
    assert cc._evidence_style(1.0, 9.3, False) == "dim"     # below theta-cleared band
    assert cc._evidence_style(1.0, 0.0, False) == "dim"     # zero cushion: no div-by-zero


def test_evidence_markup_missing_fields_render_dash():
    assert cc._evidence_markup({}) == "[dim]—[/dim]"
    assert cc._evidence_markup({"banked_bp": 1.0}) == "[dim]—[/dim]"  # cushion missing


# ---------- countdown column thresholds ----------

def test_countdown_style_thresholds():
    assert cc._countdown_style(30) == "bold red"   # < 60s
    assert cc._countdown_style(250) == "white"     # < 5min
    assert cc._countdown_style(400) == "dim"       # > 5min


def test_countdown_markup_unparseable_slug_is_dash():
    assert cc._countdown_markup("not-a-slug", 1000.0) == "[dim]—[/dim]"


def test_countdown_markup_past_end_shows_zero():
    slug = "btc-updown-5m-1000"  # end = 1300
    assert cc._countdown_markup(slug, 2000.0) == "[dim]0:00[/dim]"


# ---------- mode text ----------

def test_mode_text_prefers_quiesce_window_state():
    assert cc._mode_text({"state": "flip"}) == "flip"
    assert cc._mode_text({"state": "quiesce"}) == "quiesce"


def test_mode_text_falls_back_to_armed_mode_field():
    assert cc._mode_text({"state": "armed", "mode": "safe"}) == "safe"
    assert cc._mode_text({"state": "armed", "mode": "spec"}) == "spec"


def test_mode_text_missing_is_dash():
    assert cc._mode_text({}) == "—"


# ---------- tape log fixed-width alignment ----------

def test_tape_head_is_fixed_width_regardless_of_slug_length():
    short = cc._tape_head({"t": 1700000100, "slug": "btc-updown-5m-1700000000"})
    long = cc._tape_head({"t": 1700000100, "slug": "doge-updown-60m-1700000000"})
    assert len(short) == len(long) == 24  # "HH:MM:SS  " (10) + slug padded to 14


def _agg_col() -> int:
    """Column the collapse counter occupies on EVERY tape line: after the
    fixed head and the fixed tag. Derived, never hard-coded, so widening
    either moves the expectation with it."""
    return len(cc._tape_head({"t": 0, "slug": ""})) + 1 + cc._TAPE_TAG_WIDTH


def test_tape_render_columns_align_across_event_types():
    slug = "doge-updown-60m-1700000000"  # exercises the widest display() form
    base_t = 1700000100
    head = cc._tape_head({"t": base_t, "slug": slug})
    fire = {"t": base_t, "slug": slug, "ev": "fire", "side": "up", "ask": 0.97,
            "fair": 0.99, "net": 0.01, "size": 10, "committed": 5.0, "rho": 0.1,
            "mode": "safe"}
    eval_ = {"t": base_t, "slug": slug, "ev": "eval", "p_up": 0.98, "rho": 0.2,
              "committed": 5.0, "banked_decided": False, "sides": []}
    gated = {"t": base_t, "slug": slug, "ev": "gated",
              "reason": "window 42% elapsed, firing opens at 50%"}
    roll = {"t": base_t, "slug": slug, "ev": "roll", "size": 25}
    closed = {"t": base_t, "slug": slug, "ev": "cleanup"}
    exit_ = {"t": base_t, "slug": slug, "ev": "exit", "side": "up", "size": 10,
             "bid": 0.95, "fair": 0.99}

    rendered = {name: click.unstyle(cc._tape_render(json.dumps(r)))
                for name, r in (("fire", fire), ("eval", eval_),
                                 ("gated", gated), ("roll", roll),
                                 ("cleanup", closed), ("exit", exit_))}
    for name, line in rendered.items():
        assert line[:len(head)] == head, name
        assert line[len(head)] == " ", name
        # the field after the fixed-width event tag starts at the same offset
        # for every event type — this is the actual alignment claim.
        assert line[len(head) + 1 + cc._TAPE_TAG_WIDTH] == " ", name
        # ...and the aggregation column is present (blank) on every one of
        # them, so a collapsed line never shifts the body of an uncollapsed one.
        assert line[_agg_col():_agg_col() + cc._TAPE_AGG_WIDTH].strip() == "", name


# ---------- risk header: committed / undecided / resting ----------

def test_risk_exposure_sums_filled_and_flags_undecided():
    arms = {
        "a": {"filled_usdc": 20.0, "eval": {"banked_decided": True}},
        "b": {"filled_usdc": 15.0, "eval": {"banked_decided": False}},
        "c": {"filled_usdc": 5.0, "eval": None},  # missing eval -> undecided
        "d": "not-a-dict",  # defensive: corrupt status entry, must not raise
    }
    committed, undecided, resting = cc._risk_exposure(arms)
    assert committed == 40.0
    assert undecided == 20.0  # b (15) + c (5)
    assert resting == 0.0


def test_risk_exposure_sums_resting_from_either_engine_shape():
    # arm-level `resting_usdc` is the current engine; `eval.resting` is the
    # pre-status-field fallback and is only emitted when non-zero.
    arms = {
        "a": {"filled_usdc": 10.0, "resting_usdc": 45.0, "eval": {}},
        "b": {"filled_usdc": 10.0, "eval": {"resting": 30.0}},
        "c": {"filled_usdc": 10.0, "eval": {}},  # taker-only arm
    }
    assert cc._risk_exposure(arms)[2] == 75.0


def test_risk_exposure_empty_or_none():
    assert cc._risk_exposure({}) == (0.0, 0.0, 0.0)
    assert cc._risk_exposure(None) == (0.0, 0.0, 0.0)


def test_exposure_rows_color_thresholds():
    sb = {"riding_n": 0, "riding_usd": 0.0}

    def undecided(amount):
        status = {"arms": {"a": {"filled_usdc": amount, "eval": {"banked_decided": False}}}}
        return " ".join(c for row in cc.exposure_rows(status, sb) for c in row)

    assert "[red]" in undecided(600.0)
    assert "[yellow]" in undecided(400.0)
    line = undecided(200.0)
    assert "[red]" not in line and "[yellow]" not in line


def test_exposure_rows_never_repeat_the_top_panel_capital():
    # The dupe the exposure line was born with: capital belongs to the header
    # panel's own money row, and two money lines back to back read as one line
    # printed twice.
    rows = cc.exposure_rows({"arms": {"a": {"filled_usdc": 1.0, "eval": {}}}},
                             {"riding_n": 0, "riding_usd": 0.0})
    assert len(rows) == 1
    label, v1, v2, v3 = rows[0]
    assert label == "exposure"
    assert v1.startswith("committed ")
    assert "capital" not in " ".join(rows[0])


def test_exposure_rows_show_resting_only_when_a_bid_is_on_the_book():
    sb = {"riding_n": 0, "riding_usd": 0.0}
    resting = cc.exposure_rows(
        {"arms": {"a": {"filled_usdc": 10.0, "resting_usdc": 45.0, "eval": {}}}}, sb)
    assert len(resting) == 2 and resting[1][0] == "resting"
    assert "◇resting $45.00" in resting[1][1]
    # A taker-only fleet must not carry a "$0.00" field every tick.
    flat = cc.exposure_rows(
        {"arms": {"a": {"filled_usdc": 10.0, "resting_usdc": 0.0, "eval": {}}}}, sb)
    assert len(flat) == 1
    assert "resting" not in " ".join(flat[0])


# ---------- RTDS settlement-stream health ----------

def test_rtds_rich_is_silent_until_something_arms_on_the_stream():
    # A Binance-only fleet must not carry a line about a socket it never
    # opened — the risk header is one line and every character is spent.
    assert cc._rtds_rich(None) == ""
    assert cc._rtds_rich({}) == ""
    assert cc._rtds_rich({"started": False, "events": 0}) == ""


def test_rtds_rich_reads_green_connected_and_red_dark():
    live = cc._rtds_rich({"started": True, "connected": True, "events": 900,
                           "events_per_s": 8.0, "last_event_age_s": 0.4,
                           "consumers": 2, "reconnects": 0})
    assert "[green]rtds[/green]" in live
    assert "8.0/s" in live and "2 arms" in live

    # One socket feeds every stream-fed arm, so its death is a fleet event.
    dark = cc._rtds_rich({"started": True, "connected": False, "events": 900,
                           "events_per_s": 0.0, "last_event_age_s": 47.0,
                           "consumers": 2, "reconnects": 3,
                           "err": "read: connection reset"})
    assert "[red]rtds DOWN[/red]" in dark
    assert "age 47s" in dark and "3 reconnects" in dark
    assert "connection reset" in dark


def test_rtds_rich_tolerates_a_stream_that_has_never_printed():
    # started, connected, zero events: `last_event_age_s` is null and must
    # not format as a confident "age 0s".
    line = cc._rtds_rich({"started": True, "connected": True, "events": 0,
                           "events_per_s": 0.0, "last_event_age_s": None,
                           "consumers": 1})
    assert "no events yet" in line


_LIVE_RTDS = {"started": True, "connected": True, "events": 10, "events_per_s": 8.0,
              "last_event_age_s": 0.5, "consumers": 1}


def test_has_rtds_arm_reads_the_arm_not_the_socket():
    assert cc._has_rtds_arm({"a": {"feed": "rtds"}})
    assert not cc._has_rtds_arm({"a": {"feed": "binance"}})
    assert not cc._has_rtds_arm({"a": {}})          # pre-feed-field engine
    assert not cc._has_rtds_arm({"a": "not-a-dict"})  # corrupt entry, no raise
    assert not cc._has_rtds_arm({})
    assert not cc._has_rtds_arm(None)


def test_rtds_line_is_silent_once_no_arm_reads_the_stream():
    # The socket is opened lazily by the first rtds arm and OUTLIVES it. A
    # health line for a stream nothing is trading on is a line that never goes
    # away, and a line that never goes away stops being read.
    assert cc._rtds_line({"arms": {"a": {"feed": "binance"}}, "rtds": _LIVE_RTDS}) == ""
    assert cc._rtds_line({"arms": {}, "rtds": _LIVE_RTDS}) == ""
    assert cc._rtds_line(None) == ""
    assert "rtds" in cc._rtds_line({"arms": {"a": {"feed": "rtds"}}, "rtds": _LIVE_RTDS})


def test_rtds_line_still_reports_a_dark_stream_an_arm_depends_on():
    # feed=="rtds" is the arm's config, not the socket's state — a DOWN stream
    # is exactly when the line matters most.
    dark = cc._rtds_line({"arms": {"a": {"feed": "rtds"}},
                          "rtds": {"started": True, "connected": False, "events": 5,
                                   "events_per_s": 0.0, "last_event_age_s": 60.0,
                                   "consumers": 0}})
    assert "[red]rtds DOWN[/red]" in dark


def test_header_carries_the_stream_state_when_an_arm_reads_it():
    status = {"arms": {"a": {"filled_usdc": 1.0, "feed": "rtds", "eval": {}}},
              "rtds": _LIVE_RTDS}
    row = cc.feed_row(status)
    assert row is not None and row[0] == "feed" and "rtds" in row[1]
    # ...and stays out of the way when the fleet is binance-only.
    binance = {"arms": {"a": {"filled_usdc": 1.0, "feed": "binance", "eval": {}}},
               "rtds": _LIVE_RTDS}
    assert cc.feed_row(binance) is None


def test_the_arm_cell_marks_which_feed_an_arm_reads():
    # The arms table's flag column folded into the arm cell; the markers are
    # what the fold may not lose.
    arms = {
        "xrp-updown-5m-9999999999": {"filled_usdc": 1.0, "roll": True,
                                      "feed": "rtds", "eval": None},
        "btc-updown-5m-9999999999": {"filled_usdc": 1.0, "roll": True,
                                      "feed": "binance", "eval": None},
    }
    by_slug = {r["slug"]: cc._arm_cell(r) for r in cc.live_rows(arms)}
    assert "≈" in by_slug["xrp-updown-5m-9999999999"]
    assert "≈" not in by_slug["btc-updown-5m-9999999999"]


def test_the_arm_cell_fits_every_marker_at_once():
    """⟳ + ≈ + ◇ on one arm is a real state (a rolling, stream-fed, maker
    arm), and the widest label the fleet has is "doge 15m" — together they are
    what the `arm` column is sized for."""
    from rich.console import Console

    arms = {"doge-updown-15m-1700000000": {"filled_usdc": 1.0, "roll": True,
                                            "feed": "rtds", "maker_bid": True,
                                            "eval": None}}
    row = cc.live_rows(arms)[0]
    assert cc._arm_flags({"roll": True, "feed": "rtds", "maker_bid": True}) == "⟳≈◇"
    c = Console(record=True, width=200)
    c.print(cc.build_windows_table({}, 1700000100.0, arms=arms))
    line = next(ln for ln in c.export_text().splitlines() if "doge" in ln)
    cell = [x.strip() for x in line.strip("│").split("│")][1]
    assert cell == "doge 15m ⟳≈◇", cell
    assert "…" not in cell, cell
    assert row["flags"] == "⟳≈◇"


def test_a_non_rolling_arm_keeps_the_roll_slot_occupied():
    # "·" holds the place so the feed/maker markers never shift between rows.
    assert cc._arm_flags({}) == "·"
    assert cc._arm_flags({"feed": "rtds"}) == "·≈"


def test_the_money_cell_fits_a_resting_bid_beside_a_four_figure_fill():
    # The resting $ shares the committed cell, same as the arms table's did.
    from rich.console import Console

    arms = {"btc-updown-5m-9999999999": {
        "filled_usdc": 1204.50, "roll": True, "resting_usdc": 450.0,
        "eval": {"state": "armed", "committed": 1204.50, "resting": 450.0}}}
    c = Console(record=True, width=200)
    c.print(cc.build_windows_table({}, 1000.0, arms=arms))
    row = next(ln for ln in c.export_text().splitlines() if "btc" in ln)
    assert "$1,204.50 ◇$450" in row, row


def test_an_unreachable_engine_still_announces_itself_after_the_fold():
    """The arms table's red placeholder was the only thing that said the engine
    had stopped answering. With no arms table it moved to the header — the
    windows table would otherwise keep painting an hour-old decided tail while
    the exposure row read a confident "committed $0.00"."""
    assert cc.engine_row({}) is not None
    assert cc.engine_row(None) is not None
    assert cc.engine_row({"arms": {}}) is not None
    row = cc.engine_row({"arms": {}})
    assert row[0] == "engine" and "unreachable or no arms" in row[1]
    assert "[red]" in row[1]
    # ...and it gets out of the way the moment something is armed.
    assert cc.engine_row({"arms": {"btc-updown-5m-1": {}}}) is None


# ---------- the merged windows table ----------
#
# The panel that exists because a real BNB fill (2026-08-23 14:38, window
# 14:35-14:40) was invisible on the dashboard for nearly four minutes: the arm
# had rolled, the fire had scrolled off the tape, and the only per-trade view
# carried decided windows only.
#
# It absorbed the recent-windows chip strip on 2026-08-23 (watch_ui's design
# note). The "nothing lost in the merge" tests below are the pin on that: the
# strip's at-a-glance ✓/✗ sequence and its ◆ riding chips must still be
# readable off the merged table, in the same order.

# 160 by default: the folded table's natural width is ~135, and Rich squeezes
# columns (and therefore glyphs) on a console narrower than that.
def _trades_text(sb, now=1787510500.0, width=160, limit=None, odds=None,
                 arms=None):
    from rich.console import Console

    c = Console(record=True, width=width)
    c.print(cc.build_windows_table(sb, now, arms=arms, limit=limit, odds=odds))
    return c.export_text()


def _glyph_column(out: str) -> str:
    """The lifecycle column read straight down — what the chip strip used to
    be, and the thing the merge is not allowed to lose."""
    return "".join(ln.strip("│").split("│")[0].strip()
                   for ln in out.splitlines() if ln.startswith("│"))


def _decided(slug, pnl, end_ts, **kw):
    row = {"slug": slug, "won": pnl >= 0, "pnl": pnl, "est": False, "end_ts": end_ts,
           "notional": 10.0, "shares": 10.0, "entry_px": 0.96, "side": "up",
           "entry_ts": end_ts - 200, "exit_ts": end_ts + 20}
    row.update(kw)
    return row


def test_every_decided_window_in_the_scoreboard_renders_a_trades_row():
    """The guarantee the chip strip could not make: hand the table a
    scoreboard and every window in it is on screen. A trade that graded is a
    trade the operator can see."""
    sb = {"windows": [_decided(f"btc-updown-5m-{1787500000 + i * 300}", i - 3.0,
                                1787500300 + i * 300) for i in range(6)],
          "riding_windows": []}
    out = _trades_text(sb)
    assert out.count("btc 5m") == 6


# ---------- nothing lost in the strip -> table merge ----------

def test_the_merged_table_carries_the_decided_sequence_the_strip_showed():
    """The strip's whole job was an at-a-glance ✓/✗ run, newest first. Read the
    merged table's glyph column straight down and it is the same run, in the
    same order — which is the only reason deleting the strip is not a loss."""
    pnls = [4.0, -44.0, 12.0, -3.0, 1.0]
    sb = {"windows": [_decided(f"btc-updown-5m-{1787500000 + i * 300}", p,
                                1787500300 + i * 300)
                      for i, p in enumerate(pnls)],
          "riding_windows": []}
    # newest window-end first, so the sequence reverses the construction order
    assert _glyph_column(_trades_text(sb)) == "✓✗✓✗✓"


def test_the_merged_table_carries_the_riding_chips_identity():
    """`◆ bnb5 $19` was the strip's riding chip: glyph, arm, notional. All
    three survive as columns, and the ◆ still leads the newest row."""
    sb = {"windows": [_decided("eth-updown-5m-1787509500", 4.0, 1787509800)],
          "riding_windows": [_riding("bnb-updown-5m-1787510100", 1787510400)]}
    out = _trades_text(sb)
    assert _glyph_column(out) == "◆✓"
    row = next(ln for ln in out.splitlines() if "bnb 5m" in ln)
    assert "◆" in row and "$19.44" in row and "riding" in row


def test_an_estimated_verdict_is_dim_in_the_glyph_column_like_its_chip_was():
    # Same lower-confidence convention the ~estimated chip carried: the glyph
    # keeps its shape and loses its win/loss color.
    est = {"slug": "sol-updown-5m-1", "won": True, "pnl": 3.0, "est": True}
    assert cc._stage_cell(est) == "[dim]✓[/]"
    assert cc._stage_cell(dict(est, est=False)) == "[green]✓[/]"


def test_the_glyph_column_names_every_stage_of_a_windows_life():
    stages = {"armed": "○", "gated": "⊘", "riding": "◆", "won": "✓", "lost": "✗"}
    for stage, glyph in stages.items():
        assert glyph in cc._STAGE_LEGEND, stage
    assert cc._stage_cell({"state": "armed"}) == "[green]○[/]"
    assert cc._stage_cell({"state": "gated"}) == "[yellow]⊘[/]"
    assert cc._stage_cell({"held": True}) == "[cyan]◆[/]"
    assert cc._stage_cell({"won": False}) == "[red]✗[/]"
    # A state this build doesn't know is still "not firing", never a fake gate.
    assert cc._stage_cell({"state": "quiesce"}) == "[dim]⊘[/]"


def test_a_gated_arm_holding_a_position_still_reads_as_riding():
    """The gate blocks the NEXT fire, not the position already on the books —
    money outranks posture in the glyph column."""
    w = {"slug": "btc-updown-5m-1", "held": True, "live": True, "state": "gated"}
    assert cc._stage(w) == "riding"
    # ...and a graded window is decided whatever its arm is still doing.
    assert cc._stage(dict(w, won=True)) == "won"


def test_a_filled_window_renders_before_its_redeem_posts():
    """THE regression. A win waits on Polymarket's redeem row and a loss posts
    no row at all until the 300s gamma grace expires; for that whole stretch
    the window is undecided, and it used to exist on this dashboard only as
    the header's "riding N windows $W" count."""
    sb = {"windows": [], "riding_windows": [
        {"slug": "bnb-updown-5m-1787510100", "won": None, "pnl": None, "est": False,
         "end_ts": 1787510400, "notional": 19.44, "shares": 20.0,
         "entry_px": 0.972, "side": "up"}]}
    out = _trades_text(sb)
    assert "bnb 5m" in out
    assert "19.44" in out and "up" in out
    assert "riding" in out          # no verdict yet — never a fake $0.00 P&L
    assert "+0.00" not in out


def test_windows_title_states_the_retention_cap():
    sb = {"windows": [_decided(f"btc-updown-5m-{1787500000 + i}", 1.0,
                                1787500300 + i) for i in range(12)],
          "riding_windows": [_riding(f"bnb-updown-5m-{1787510100 + i}",
                                      1787510400 + i) for i in range(2)]}
    assert cc.windows_title(sb) == "windows · 0 live · 2 riding · last 12 decided"
    assert cc.windows_title({}) == "windows · 0 live · 0 riding · last 0 decided"
    assert cc.windows_title(None) == "windows · 0 live · 0 riding · last 0 decided"
    # A short panel says how much of the held set it is actually painting.
    assert cc.windows_title(sb, shown=6) == (
        "windows · 6 of 14 · 0 live · 2 riding · last 12 decided")
    assert cc.windows_title(sb, shown=14) == (
        "windows · 0 live · 2 riding · last 12 decided")
    # A live arm with nothing filled adds a row, and the title counts it.
    arms = {"xrp-updown-5m-1787600000": {"eval": {"state": "gated"}}}
    assert cc.windows_title(sb, arms) == (
        "windows · 1 live · 2 riding · last 12 decided")


def _riding(slug, end_ts, notional=19.44):
    return {"slug": slug, "won": None, "pnl": None, "end_ts": end_ts,
            "notional": notional, "entry_px": 0.97, "side": "up",
            "shares": 20.0, "entry_ts": end_ts - 180, "exit_ts": 0.0}


def test_a_fresh_fill_tops_the_panel_and_a_short_panel_keeps_it():
    sb = {"windows": [_decided(f"btc-updown-5m-{1787500000 + i * 300}", 1.0,
                                1787500300 + i * 300) for i in range(6)],
          "riding_windows": [_riding("bnb-updown-5m-1787510100", 1787510400)]}
    rows = cc.window_rows(sb, limit=2)
    assert rows[0]["slug"] == "bnb-updown-5m-1787510100"
    assert len(rows) == 2
    assert len(cc.window_rows(sb)) == 7  # no limit: everything


def test_a_window_stuck_riding_since_yesterday_does_not_squat_on_the_panel():
    """Four windows had been undecided for 13-25h on 2026-08-23 ($317 of
    them). Riding-always-first would have handed them 4 of the panel's 6 rows
    in perpetuity; the risk header's "riding N windows $W" is where a stuck
    total belongs, not this panel, which answers "where is the fleet now"."""
    sb = {"windows": [_decided(f"btc-updown-5m-{1787509500 + i * 300}", 1.0,
                                1787509800 + i * 300) for i in range(4)],
          "riding_windows": [_riding(f"eth-updown-5m-{1787420000 + i}", 1787420300 + i)
                              for i in range(4)]}
    rows = cc.window_rows(sb, limit=4)
    assert all(r["won"] is not None for r in rows), "stale riding crowded out today"
    # ...and they are still all there, in age order, when nothing is capping.
    assert len(cc.window_rows(sb)) == 8


# ---------- the live head: windows the wallet cannot see yet ----------

_LIVE_ARMS = {
    "btc-updown-5m-1787510700": {  # ends 1787511000, still open
        "filled_usdc": 120.0, "roll": True,
        "eval": {"state": "armed", "committed": 120.0}},
    "xrp-updown-5m-1787510700": {"eval": {"state": "gated", "committed": 0.0}},
}


def test_a_live_arm_holds_a_row_before_anything_has_filled():
    """The head of the lifecycle. An armed window has no wallet row until it
    fires, so the scoreboard cannot see it at all — without the engine's arms
    the table could only ever start at "already spent money"."""
    rows = cc.window_rows({"windows": [], "riding_windows": []}, _LIVE_ARMS)
    assert {r["slug"] for r in rows} == set(_LIVE_ARMS)
    assert all(r["live"] and r["won"] is None for r in rows)
    out = _trades_text({}, arms=_LIVE_ARMS, now=1787510800.0)
    assert "armed" in out and "gated" in out
    # the window has not ended, so `t` counts DOWN to its close (the arms
    # table's T-) rather than up from it
    assert "3:20" in out, out


def test_a_fire_fills_the_row_that_is_already_there_instead_of_adding_one():
    """One row per window is the whole point of keying on the slug: the
    operator watches a row move through its life, not a second row appear
    under the first."""
    slug = "btc-updown-5m-1787510700"
    sb = {"windows": [], "riding_windows": [_riding(slug, 1787511000)]}
    rows = cc.window_rows(sb, _LIVE_ARMS)
    assert [r["slug"] for r in rows].count(slug) == 1
    merged = next(r for r in rows if r["slug"] == slug)
    assert merged["live"] and cc._stage(merged) == "riding"


def test_the_wallet_wins_over_the_engine_on_a_window_both_know():
    """Wallet is ground truth. The engine only ever contributes posture — its
    committed figure may not touch a window the scoreboard already priced."""
    slug = "btc-updown-5m-1787510700"
    sb = {"windows": [], "riding_windows": [_riding(slug, 1787511000, notional=7.5)]}
    merged = next(r for r in cc.window_rows(sb, _LIVE_ARMS) if r["slug"] == slug)
    assert merged["notional"] == 7.5, "the engine's $120 overwrote the wallet"
    assert merged["entry_px"] == 0.97 and merged["side"] == "up"


def test_the_engines_committed_is_dim_until_the_wallet_confirms_it():
    # It is a real figure and belongs on screen the moment the engine reports
    # it — but it is not ground truth, and must not read like the wallet's.
    from rich.console import Console

    slug = "btc-updown-5m-1787510700"

    def row_ansi(sb):
        c = Console(record=True, width=100)
        c.print(cc.build_windows_table(sb, 1787510800.0, arms=_LIVE_ARMS))
        return next(ln for ln in c.export_text(styles=True).splitlines()
                    if "btc 5m" in ln)

    assert "$120.00" in row_ansi({})
    engine_only = row_ansi({}).split("$120.00")[0]
    assert engine_only.endswith("\x1b[2m"), "the engine's own figure is not dim"
    # ...and the wallet's own figure for the same window is not dimmed.
    confirmed = row_ansi({"windows": [], "riding_windows": [_riding(slug, 1787511000)]})
    assert "$19.44" in confirmed
    assert not confirmed.split("$19.44")[0].endswith("\x1b[2m")


def test_window_rows_never_annotates_the_scoreboard_it_was_handed():
    """The scoreboard object is shared with the risk header's riding totals;
    marking it up in place would quietly change what another panel reads."""
    riding = _riding("btc-updown-5m-1787510700", 1787511000)
    decided = _decided("eth-updown-5m-1787500000", 1.0, 1787500300)
    sb = {"windows": [decided], "riding_windows": [riding]}
    before = (dict(riding), dict(decided))
    cc.window_rows(sb, _LIVE_ARMS)
    assert (riding, decided) == before
    assert sb["riding_windows"] == [riding] and sb["windows"] == [decided]


def test_a_non_window_arm_never_becomes_a_row():
    # The arms table shows whatever the engine named; this table's unit is a
    # WINDOW, and a row it cannot place in time would sort to the bottom
    # forever.
    assert cc.window_rows({}, {"not-a-real-slug": {"eval": {"state": "armed"}}}) == []
    assert cc.live_rows(None) == [] and cc.live_rows({}) == []


def test_the_table_reads_top_to_bottom_as_the_lifecycle():
    """One sort key — window end, descending — and a window's stage is a
    function of its age, so time order IS lifecycle order: live head, riding,
    decided tail."""
    sb = {"windows": [_decided("eth-updown-5m-1787500000", 1.0, 1787500300),
                      _decided("sol-updown-5m-1787499700", -2.0, 1787500000)],
          "riding_windows": [_riding("bnb-updown-5m-1787510100", 1787510400)]}
    assert _glyph_column(_trades_text(sb, arms=_LIVE_ARMS,
                                      now=1787510800.0)) == "○⊘◆✓✗"


def test_age_label_reads_as_time_since_not_a_clock():
    assert cc._age_label(-30) == "live"      # window still open
    assert cc._age_label(0) == "0s"
    assert cc._age_label(45) == "45s"
    assert cc._age_label(150) == "2m"
    assert cc._age_label(3600 * 2 + 240) == "2h04"


def test_trades_table_tolerates_a_half_built_row():
    # An engine/data-api seam can leave side or entry unknown; the panel must
    # still paint, the same rule the arms table lives by.
    sb = {"windows": [{"slug": "xrp-updown-5m-1787500000", "won": False,
                       "pnl": -23.19, "end_ts": 1787500300}],
          "riding_windows": [{"slug": "not-a-slug"}]}
    out = _trades_text(sb)
    assert "xrp 5m" in out and "-23.19" in out
    assert "riding" in out          # the unparseable row still paints a row


def test_trades_table_empty_scoreboard_says_so():
    # The placeholder must FIT its cell — an ellipsized "no window…" is exactly
    # the ambiguity this panel exists to remove.
    assert "no windows" in _trades_text({"windows": [], "riding_windows": []})
    assert "no windows" in _trades_text(None)
    assert "…" not in _trades_text(None)


def test_the_merged_table_fits_a_narrow_terminal_without_losing_a_column():
    """Fixed natural widths, not expand: the whole table is ~70 columns, so it
    paints the same on an 80-column terminal as on a 200-column one."""
    sb = {"windows": [_decided("btc-updown-5m-1787500000", -12436.76, 1787500300)],
          "riding_windows": [_riding("bnb-updown-5m-1787510100", 1787510400)]}
    for width in (100, 140, 200):
        out = _trades_text(sb, width=width, arms=_LIVE_ARMS)
        rows = [ln for ln in out.splitlines() if ln.startswith("│")]
        assert all(len(ln) <= width for ln in rows), width
        # A narrow console squeezes columns, never drops one: the stage
        # sequence and the row count survive at every width.
        assert len(rows) == 4, (width, rows)
    # ...and at its natural width nothing is ellipsised, five-figure P&L
    # included.
    wide = _trades_text(sb, width=200, arms=_LIVE_ARMS)
    assert _glyph_column(wide) == "○⊘◆✗"
    assert "…" not in wide and "-12,436.76" in wide


def test_the_column_geometry_is_identical_across_every_stage():
    """A double-wide glyph in the stage column would shove every field after it
    one place right on that row only — the exact jitter the fixed widths exist
    to prevent."""
    sb = {"windows": [_decided("btc-updown-5m-1787500000", 12.0, 1787500300),
                      _decided("sol-updown-5m-1787499700", -2.0, 1787500000,
                               est=True)],
          "riding_windows": [_riding("bnb-updown-5m-1787510100", 1787510400)]}
    out = _trades_text(sb, arms=_LIVE_ARMS)
    rows = [ln for ln in out.splitlines() if ln.startswith("│")]
    assert len({len(ln) for ln in rows}) == 1, rows
    assert len({tuple(len(c) for c in ln.strip("│").split("│")) for ln in rows}) == 1


def test_a_riding_row_says_what_we_paid_and_what_it_is_worth_now():
    """The operator's ask, verbatim: "show more about the position, like what
    we got it for and current odds". One row: age, arm, side, entry, now,
    size, verdict."""
    sb = {"windows": [], "riding_windows": [
        {"slug": "bnb-updown-5m-1787510100", "won": None, "pnl": None, "est": False,
         "end_ts": 1787510400, "notional": 19.44, "shares": 20.0,
         "entry_px": 0.972, "side": "up", "entry_ts": 1787510200,
         "exit_ts": 0.0}]}
    out = _trades_text(sb, odds={("bnb-updown-5m-1787510100", "up"): 0.99})
    row = next(ln for ln in out.splitlines() if "bnb 5m" in ln)
    cells = [c.strip() for c in row.strip("│").split("│")]
    assert cells[:4] == ["◆", "bnb 5m", "1m", "held 5m00s"]
    assert cells[4].startswith("20sh in ")   # wall clock is the reader's own tz
    assert cells[5:] == ["up 0.97→0.99", "$19.44", "riding"]


def test_the_now_column_is_blank_when_the_marks_feed_has_nothing():
    # No fetch yet, a failed one, or a window the wallet no longer holds: the
    # column says "—" and every other column is unaffected.
    sb = {"windows": [], "riding_windows": [_riding("bnb-updown-5m-1787510100",
                                                     1787510400)]}
    for odds in (None, {}, {("eth-updown-5m-1", "up"): 0.5}):
        out = _trades_text(sb, odds=odds)
        row = next(ln for ln in out.splitlines() if "bnb 5m" in ln)
        cells = [c.strip() for c in row.strip("│").split("│")]
        assert cells[5] == "up 0.97→—", (odds, cells)


def test_the_now_column_colors_by_which_side_is_winning():
    # A binary settles to 0 or 1: above the coin flip the side we hold is
    # ahead, below it we are behind, and that is the whole read.
    ahead = {"slug": "bnb-updown-5m-1", "side": "up"}
    assert cc._odds_cell(ahead, {("bnb-updown-5m-1", "up"): 0.99}) == "[green]0.99[/green]"
    assert cc._odds_cell(ahead, {("bnb-updown-5m-1", "up"): 0.04}) == "[red]0.04[/red]"
    assert cc._odds_cell(ahead, {}) == "[dim]—[/dim]"


def test_position_odds_needs_the_side_to_match_not_just_the_window():
    # Holding `up` at 0.99 and `down` at 0.01 are the same window and opposite
    # facts — a slug-only lookup would print the wrong one half the time.
    w = {"slug": "bnb-updown-5m-1", "side": "down"}
    odds = {("bnb-updown-5m-1", "up"): 0.99, ("bnb-updown-5m-1", "down"): 0.01}
    assert cc.position_odds(odds, w) == 0.01
    assert cc.position_odds(odds, {"slug": "bnb-updown-5m-1"}) is None  # side unknown
    assert cc.position_odds(None, w) is None


def test_trades_table_marks_an_estimated_pnl_and_never_shows_negative_zero():
    sb = {"windows": [_decided("sol-updown-5m-1787500000", 3.0, 1787500300, est=True),
                      _decided("eth-updown-5m-1787500000", -0.001, 1787500300)],
          "riding_windows": []}
    out = _trades_text(sb)
    assert "~+3.00" in out
    assert "-0.00" not in out and "+0.00" in out  # _zero() snaps the residual


# ---------- arms table: missing/partial eval tolerance (4d) ----------

def test_live_rows_tolerate_none_and_partial_evals():
    """The arms table's hard rule, inherited: an engine restart mid-watch
    leaves last_eval None or half-built, and the row must keep painting."""
    from rich.console import Console

    arms = {
        "btc-updown-5m-9999999999": {"filled_usdc": 10.0, "roll": True, "eval": None},
        "eth-updown-15m-8888888888": {"filled_usdc": 5.0, "roll": False,
                                       "eval": {"state": "armed"}},  # no p_up/rho/sides
        "sol-updown-5m-9999999999": {},        # no filled_usdc/roll/eval at all
        "not-a-real-slug": {"eval": {"state": "armed"}},   # not a window
    }
    c = Console(record=True, width=200)
    c.print(cc.build_windows_table({}, 1000.0, arms=arms))   # must not raise
    out = c.export_text()
    assert "—" in out           # missing fields degrade, never a traceback
    assert "$10.00" in out
    assert "not-a-real" not in out  # this table's unit is a window


def test_the_folded_table_keeps_the_arms_tables_column_geometry_rule():
    """Fixed widths so nothing jitters as a gate reason or a stage changes —
    now across live AND settled rows, which is the whole risk of repurposing a
    cell by stage."""
    from rich.console import Console

    long_reason = ("basis guard: projected margin -4.9bp inside 6.0bp noise band "
                   "[banked -3.2bp cushion 9.3bp] and then some extra unexpected text")
    arms = {
        "btc-updown-5m-1700000300": {"filled_usdc": 1.0, "roll": True,
                                      "eval": {"state": "gated", "reason": long_reason}},
        "eth-updown-15m-1700000900": {"filled_usdc": 2.0, "feed": "rtds",
                                       "maker_bid": True, "resting_usdc": 45.0,
                                       "eval": {
            "state": "armed", "p_up": 0.8732, "mode": "safe", "rho": 0.4,
            "banked_bp": 12.3, "cushion_bp": 9.3, "banked_decided": True,
            "committed": 2.0, "sides": [{"side": "up", "safety": 0.9}]}},
        "sol-updown-5m-1700000300": {"eval": None},
    }
    sb = {"windows": [_decided("xrp-updown-5m-1699990000", -12436.76, 1699990300)],
          "riding_windows": [_riding("bnb-updown-5m-1699999000", 1699999300)]}
    c = Console(record=True, width=200)
    c.print(cc.build_windows_table(sb, 1700000000.0, arms=arms))
    lines = [ln for ln in c.export_text().splitlines() if ln.startswith("│")]
    bounds = {tuple(i for i, ch in enumerate(ln) if ch == "│") for ln in lines}
    assert len(lines) == 5 and len(bounds) == 1, lines


# ---------- tape run-collapsing ----------
#
# The collapser's ONE safety property: it may hide repetition, never a
# transition. Most of what follows is that property stated once per material
# field — if a trigger below stops breaking its run, a real state change went
# invisible on the dashboard.

_BTC = "btc-updown-5m-1700000000"
_ETH = "eth-updown-5m-1700000000"
_T0 = 1700000000


def _collapse(*records, lines=None):
    """Feed records (dicts) through a fresh collapser; return the rendered
    deque, unstyled."""
    c = cc.TapeCollapser()
    lines = deque(maxlen=200) if lines is None else lines
    for r in records:
        c.add(json.dumps(r), lines)
    return [click.unstyle(ln) for ln in lines]


def _basis(i=0, slug=_BTC, margin=1.0, guard=6.0, **over):
    r = {"t": _T0 + i, "slug": slug, "ev": "gated",
         "reason": "basis guard: projected margin +1.0bp inside 6.0bp noise band",
         "margin_bp": margin, "guard_bp": guard}
    r.update(over)
    return r


def _eval(i=0, slug=_BTC, **over):
    r = {"t": _T0 + i, "slug": slug, "ev": "eval", "p_up": 0.9800, "rho": 0.20,
         "committed": 5.00, "banked_decided": False, "state": "armed", "mode": "safe",
         "sides": [{"side": "up", "ask": 0.97, "net": 0.0100, "safety": 0.40},
                    {"side": "down", "ask": 0.05, "net": -0.0200, "safety": -0.40}]}
    r.update(over)
    return r


def _gate(i=0, slug=_BTC, reason="theta 0.12 below band 0.30", **over):
    r = {"t": _T0 + i, "slug": slug, "ev": "gated", "reason": reason}
    r.update(over)
    return r


def _fire(i=0, slug=_BTC, **over):
    r = {"t": _T0 + i, "slug": slug, "ev": "fire", "side": "up", "ask": 0.97,
         "fair": 0.99, "net": 0.01, "size": 10, "committed": 5.0, "rho": 0.1,
         "mode": "safe"}
    r.update(over)
    return r


# --- basis guard: the shape that already shipped, unchanged ---

def test_basis_guard_run_collapses_every_arm_onto_one_line():
    out = _collapse(_basis(0, _BTC, margin=1.0), _basis(1, _ETH, margin=-4.9),
                    _basis(2, _BTC, margin=2.0))
    assert len(out) == 1
    assert out[0][_agg_col():].startswith("×3")
    # freshest margin per symbol, sorted, on one line — the pre-abstraction shape
    assert "basis bp/guard: btc +2.0/6 · eth -4.9/6" in out[0]


def test_basis_guard_run_ends_on_any_other_event():
    out = _collapse(_basis(0), _basis(1), _eval(2), _basis(3))
    assert len(out) == 3
    assert "×2" in out[0]
    assert "eval" in out[1]
    assert "×" not in out[2]  # a fresh run of one counts nothing


def test_basis_guard_falls_back_to_the_regex_on_a_pre_structured_record():
    out = _collapse(_basis(0, margin_bp=None, guard_bp=None,
                            reason=("basis guard: projected margin -4.9bp inside "
                                    "6.0bp noise band")))
    assert "btc -4.9/6" in out[0]


# --- eval runs ---

def test_eval_run_collapses_with_a_count_and_its_span():
    out = _collapse(*[_eval(i) for i in range(4)])
    assert len(out) == 1
    assert "×4" in out[0]
    span = f"⟨{cc._hms(_T0)}→{cc._hms(_T0 + 3)}⟩"
    assert span in out[0], out[0]
    assert "p↑0.9800" in out[0]  # freshest values, rendered as a normal eval line


def test_a_lone_eval_renders_byte_identically_to_an_uncollapsed_one():
    # No ×1 and no span: a run of one is just a record, and the collapser must
    # not change how the quiet case looks.
    rec = _eval(0)
    lines = deque()
    cc.TapeCollapser().add(json.dumps(rec), lines)
    assert lines[0] == cc._tape_render(json.dumps(rec))


_MATERIAL = [
    ("p_up", {"p_up": 0.9950}),                       # > 0.01
    ("committed", {"committed": 5.50}),               # > $0.01
    ("banked_decided", {"banked_decided": True}),
    ("mode", {"mode": "spec"}),
    ("quiesce_state", {"state": "quiesce"}),
    ("maker_candidate", {"maker_candidate": True}),
    ("maker_rest", {"maker_rest": 0.955}),
    # best side flips to down on a net move far inside the 0.5¢ tolerance:
    # WHICH side is best is discrete, not a number that drifted.
    ("best_side", {"sides": [{"side": "up", "ask": 0.97, "net": 0.0100, "safety": 0.40},
                              {"side": "down", "ask": 0.05, "net": 0.0101, "safety": -0.40}]}),
    ("best_net", {"sides": [{"side": "up", "ask": 0.97, "net": 0.0180, "safety": 0.40},
                             {"side": "down", "ask": 0.05, "net": -0.0200, "safety": -0.40}]}),
    ("brake_set", {"sides": [{"side": "up", "ask": 0.97, "net": 0.0100, "safety": 0.40,
                               "brake": "safety"},
                              {"side": "down", "ask": 0.05, "net": -0.0200, "safety": -0.40}]}),
    ("safety_badge", {"sides": [{"side": "up", "ask": 0.97, "net": 0.0100, "safety": 0.10},
                                 {"side": "down", "ask": 0.05, "net": -0.0200,
                                  "safety": -0.40}]}),
    ("side_went_dark", {"sides": [{"side": "up", "ask": 0.97, "net": 0.0100, "safety": 0.40},
                                   {"side": "down", "ask": 0.05, "net": -0.0200,
                                    "safety": None}]}),
]


@pytest.mark.parametrize("name,change", _MATERIAL, ids=[n for n, _ in _MATERIAL])
def test_every_material_change_ends_an_eval_run_and_renders_fresh(name, change):
    out = _collapse(_eval(0), _eval(1), _eval(2, **change))
    assert len(out) == 2, f"{name} was collapsed away: {out}"
    assert "×2" in out[0]
    assert "×" not in out[1], out[1]  # the fresh line is a run of one


def test_sub_tolerance_wobble_does_not_end_an_eval_run():
    # Every number moved, none of them far enough to repaint differently.
    out = _collapse(
        _eval(0),
        _eval(1, p_up=0.9850, committed=5.005,
              sides=[{"side": "up", "ask": 0.97, "net": 0.0140, "safety": 0.40},
                     {"side": "down", "ask": 0.05, "net": -0.0200, "safety": -0.40}]))
    assert len(out) == 1 and "×2" in out[0]


def test_a_slow_drift_still_breaks_the_run_because_tolerance_is_anchored():
    # 0.008 a tick is under tolerance every tick; chained comparison would let
    # p_up walk anywhere. Anchored to the run's first record, it breaks.
    out = _collapse(_eval(0, p_up=0.9800), _eval(1, p_up=0.9880), _eval(2, p_up=0.9960))
    assert len(out) == 2, out
    assert "×2" in out[0] and "0.9960" in out[1]


def test_interleaved_arms_collapse_independently_instead_of_thrashing():
    recs = []
    for i in range(6):
        recs += [_eval(i, _BTC), _eval(i, _ETH)]
    out = _collapse(*recs)
    assert len(out) == 2, out  # one live line per arm, not twelve
    assert all("×6" in ln for ln in out)
    assert "btc" in out[0] and "eth" in out[1]


def test_one_arms_material_change_leaves_the_other_arms_run_alone():
    recs = [_eval(0, _BTC), _eval(0, _ETH), _eval(1, _BTC), _eval(1, _ETH),
            _eval(2, _BTC, p_up=0.5), _eval(2, _ETH)]
    out = _collapse(*recs)
    assert len(out) == 3, out
    assert "×2" in out[0] and "btc" in out[0]      # btc's run, closed
    assert "×3" in out[1] and "eth" in out[1]      # eth's run, still running
    assert "btc" in out[2] and "×" not in out[2]   # btc's fresh line


# --- non-basis gate runs ---

def test_theta_gate_run_collapses_per_arm():
    out = _collapse(*[_gate(i) for i in range(5)])
    assert len(out) == 1
    assert "×5" in out[0] and "theta 0.12 below band 0.30" in out[0]


def test_a_creeping_counter_inside_the_reason_does_not_end_a_gate_run():
    # String equality would end this run on every single tick and collapse
    # nothing — the elapsed gate's whole reason is a counter.
    out = _collapse(*[_gate(i, reason=f"window {40 + i}% elapsed, firing opens at 50%")
                      for i in range(6)])
    assert len(out) == 1 and "×6" in out[0]
    assert "window 45% elapsed" in out[0]  # freshest


def test_margin_drift_beyond_epsilon_ends_a_gate_run():
    out = _collapse(_gate(0, margin_bp=1.0, guard_bp=6.0),
                    _gate(1, margin_bp=1.4, guard_bp=6.0),   # within 0.5bp
                    _gate(2, margin_bp=2.0, guard_bp=6.0))   # +1.0 off the anchor
    assert len(out) == 2, out
    assert "×2" in out[0] and "×" not in out[1]


def test_a_stale_feed_ends_a_gate_run_as_the_spot_age_climbs():
    out = _collapse(_gate(0, reason="feed stale", spot_age_s=0.4),
                    _gate(1, reason="feed stale", spot_age_s=1.2),
                    _gate(2, reason="feed stale", spot_age_s=5.1))
    assert len(out) == 2, out


def test_a_gate_and_an_eval_on_one_arm_never_share_a_run():
    # An arm is gated or armed, never both — the transition must be visible.
    out = _collapse(_gate(0), _gate(1), _eval(2), _eval(3), _gate(4))
    assert len(out) == 3, out
    assert "×2" in out[0] and "×2" in out[1] and "×" not in out[2]


def test_a_gate_run_is_per_arm_not_fleet_wide():
    out = _collapse(_gate(0, _BTC), _gate(0, _ETH), _gate(1, _BTC), _gate(1, _ETH))
    assert len(out) == 2 and all("×2" in ln for ln in out)


# --- the aggregation marker's ONE column ---
#
# The operator's complaint, verbatim: "i cant tell which ones are aggregated bc
# i dont know where to look for the x, its in the middle for gated but im not
# sure where it is for eval". The count now has exactly one home on every line
# type, and these tests are what keeps it there.

def test_the_collapse_counter_sits_in_the_same_column_for_every_line_type():
    runs = {
        "basis": [_basis(i) for i in range(3)],
        "eval": [_eval(i) for i in range(3)],
        "gate": [_gate(i) for i in range(3)],
    }
    cols = {}
    for name, recs in runs.items():
        out = _collapse(*recs)
        assert len(out) == 1, (name, out)
        cols[name] = out[0].index("×")
    assert len(set(cols.values())) == 1, f"ragged ×N column: {cols}"
    assert set(cols.values()) == {_agg_col()}


def test_the_counter_column_is_blank_but_reserved_on_an_uncollapsed_line():
    # The other half of one column: a lone record must not shift its body left
    # into the space a collapsed line uses, or the two stop lining up.
    for rec in (_eval(0), _gate(0), _fire(0),
                {"t": _T0, "slug": _BTC, "ev": "cleanup"}):
        line = click.unstyle(cc._tape_render(json.dumps(rec)))
        assert line[_agg_col():_agg_col() + cc._TAPE_AGG_WIDTH] == " " * cc._TAPE_AGG_WIDTH
        assert "×" not in line


def test_the_span_still_trails_the_line_it_belongs_to():
    # The count moved to its column; the span (variable-position by nature)
    # stays where it was, after the body — read second, never instead.
    out = _collapse(*[_eval(i) for i in range(3)])
    assert out[0].rstrip().endswith(f"⟨{cc._hms(_T0)}→{cc._hms(_T0 + 2)}⟩")


def test_the_fleet_wide_basis_line_carries_no_span_because_it_cannot_afford_one():
    # Five arms' margins is already the widest line the tape emits; the
    # ⟨from→to⟩ tail pushed it past the panel and cost a second row.
    out = _collapse(*[_basis(i, slug=s) for i in range(3)
                      for s in (_BTC, _ETH, "sol-updown-5m-1700000000")])
    assert len(out) == 1
    assert "⟨" not in out[0]
    assert out[0][_agg_col():].startswith("×9")


# --- roll + window-close consolidation ---

def _roll(i=0, slug=_BTC, size=25):
    return {"t": _T0 + i, "slug": slug, "ev": "roll", "size": size}


def _cleanup(i=0, slug=_BTC):
    return {"t": _T0 + i, "slug": slug, "ev": "cleanup"}


_BTC_NEXT = "btc-updown-5m-1700000300"  # the window _BTC rolls into


def test_a_window_close_and_its_roll_render_as_one_line():
    """They are emitted together, at the same instant, and neither reads
    without the other: the close names the window that ended, the roll names
    the one that was armed."""
    out = _collapse(_cleanup(0, _BTC), _roll(0, _BTC_NEXT, size=1000))
    assert len(out) == 1, out
    line = out[0]
    assert "ROLL" in line
    assert "closed" in line and "next window armed ($1000)" in line
    # both windows are still named: the one that shut and the one now armed
    assert cc._tape_slug(_BTC_NEXT) in line
    assert time.strftime("%H:%M", time.localtime(1700000000)) in line
    # and it keeps the fixed geometry every other tape line has
    assert line[_agg_col():_agg_col() + cc._TAPE_AGG_WIDTH].strip() == ""


def test_the_pair_merges_per_arm_when_the_whole_fleet_rolls_at_once():
    # Five arms roll on the same tick: five lines, not ten.
    eth_next = "eth-updown-5m-1700000300"
    out = _collapse(_cleanup(0, _BTC), _cleanup(0, _ETH),
                    _roll(0, _BTC_NEXT), _roll(0, eth_next))
    assert len(out) == 2, out
    assert all("closed" in ln and "next window armed" in ln for ln in out)


def test_a_close_with_no_roll_still_says_the_window_closed():
    # A --no-roll arm: half the pair is the whole event, and merging must not
    # invent a roll that never happened.
    out = _collapse(_cleanup(0, _BTC))
    assert len(out) == 1
    assert "window closed" in out[0] and "armed" not in out[0]


def test_a_roll_with_no_close_still_says_the_next_window_is_armed():
    out = _collapse(_roll(0, _BTC_NEXT))
    assert len(out) == 1
    assert "next window armed ($25)" in out[0] and "closed" not in out[0]


def test_a_second_roll_never_joins_the_first_ones_line():
    # Two roll moments are two events. The pair holds one close and one roll;
    # anything more starts a fresh line.
    out = _collapse(_cleanup(0, _BTC), _roll(0, _BTC_NEXT),
                    _cleanup(300, _BTC_NEXT), _roll(300, "btc-updown-5m-1700000600"))
    assert len(out) == 2, out
    assert all("closed" in ln for ln in out)


def test_a_windows_close_still_ends_that_arms_eval_run():
    # The merge must not cost the safety property: an arm's eval run may never
    # survive across its own window boundary.
    out = _collapse(_eval(0, _BTC), _eval(1, _BTC), _cleanup(2, _BTC),
                    _roll(2, _BTC_NEXT), _eval(3, _BTC_NEXT))
    assert len(out) == 3, out
    assert "×2" in out[0]
    assert "closed" in out[1]
    assert "×" not in out[2]


# --- what must never collapse ---

@pytest.mark.parametrize("rec", [
    {"ev": "fire", "side": "up", "ask": 0.97, "fair": 0.99, "net": 0.01, "size": 10,
     "committed": 5.0, "rho": 0.1, "mode": "safe"},
    {"ev": "exit", "side": "up", "size": 10, "bid": 0.95, "fair": 0.99},
    {"ev": "roll", "size": 25},
    {"ev": "cleanup"},
    {"ev": "some-ev-this-build-has-never-heard-of"},
], ids=["fire", "exit", "roll", "cleanup", "unknown"])
def test_loud_events_never_collapse(rec):
    out = _collapse(*[dict(rec, t=_T0 + i, slug=_BTC) for i in range(3)])
    assert len(out) == 3, out


def test_a_fire_ends_every_open_run():
    out = _collapse(_eval(0, _BTC), _eval(0, _ETH), _fire(1), _eval(2, _BTC), _eval(2, _ETH))
    assert len(out) == 5, out          # 2 evals, the fire, 2 fresh evals
    assert all("×" not in ln for ln in out)


def test_a_torn_line_is_dropped_without_ending_a_run():
    # A half-written record is not a state change — it's a truncated write.
    c = cc.TapeCollapser()
    lines = deque()
    c.add(json.dumps(_eval(0)), lines)
    c.add('{"t": 170000000, "ev": "ev', lines)
    c.add("[1, 2, 3]", lines)  # valid JSON, not a record
    c.add(json.dumps(_eval(1)), lines)
    assert len(lines) == 1 and "×2" in click.unstyle(lines[0])


def test_a_malformed_record_is_dropped_rather_than_taking_the_dashboard_down():
    # sides without "net" is the shape that raises inside the best-side pick —
    # in the classifier AND in the renderer. Both are belted: the record is
    # dropped, the deque is untouched, and nothing propagates.
    c = cc.TapeCollapser()
    lines = deque()
    c.add(json.dumps(_eval(0)), lines)
    c.add(json.dumps(_eval(1, sides=[{"side": "up"}])), lines)
    c.add(json.dumps(_eval(2)), lines)
    assert len(lines) == 2  # the run, then a fresh line after the break
    assert "×" not in click.unstyle(lines[-1])


# --- deque ownership ---

def test_a_foreign_line_at_the_tail_is_never_overwritten():
    c = cc.TapeCollapser()
    lines = deque(maxlen=200)
    c.add(json.dumps(_eval(0)), lines)
    c.add(json.dumps(_eval(1)), lines)
    foreign = "a line this collapser does not own"
    lines.append(foreign)
    c.add(json.dumps(_eval(2)), lines)
    assert lines[-1] is foreign
    assert "×3" in click.unstyle(lines[-2])  # the run edited the slot it owns


def test_ownership_is_identity_not_equal_text():
    # A different record that happens to render the same text is still someone
    # else's line.
    c = cc.TapeCollapser()
    lines = deque(maxlen=200)
    c.add(json.dumps(_eval(0)), lines)
    twin = (lines[0] + " ")[:-1]  # equal text, distinct object
    assert twin == lines[0] and twin is not lines[0]
    lines.append(twin)
    c.add(json.dumps(_eval(1)), lines)
    assert lines[-1] is twin
    assert "×2" in click.unstyle(lines[0])


def test_a_run_whose_line_has_scrolled_out_of_reach_appends_instead():
    # Past the lookback the run's line is off where the operator is looking;
    # editing it there would be an invisible update.
    c = cc.TapeCollapser()
    lines = deque(maxlen=200)
    c.add(json.dumps(_eval(0)), lines)
    for i in range(cc._OWN_LOOKBACK + 1):
        lines.append(f"other {i}")
    c.add(json.dumps(_eval(1)), lines)
    assert "×2" in click.unstyle(lines[-1])
    assert lines[0].endswith("in") or "p↑" in click.unstyle(lines[0])


# ---------- composed frame: every fact renders exactly once ----------

_CAP = 4231.55
_FRAME_ARMS = {
    "btc-updown-5m-9999999999": {
        "filled_usdc": 120.0, "roll": True, "feed": "binance", "maker_bid": True,
        "resting_usdc": 45.0,
        "eval": {"state": "armed", "p_up": 0.87, "mode": "safe", "rho": 0.4,
                 "banked_bp": 12.3, "cushion_bp": 9.3, "banked_decided": True,
                 "committed": 120.0, "resting": 45.0,
                 "sides": [{"side": "up", "safety": 0.9}]}},
    "xrp-updown-5m-9999999999": {
        "filled_usdc": 80.0, "roll": True, "feed": "rtds", "maker_bid": False,
        "eval": {"state": "gated", "reason": "basis guard",
                 "margin_bp": -4.9, "guard_bp": 6.0}},
}


def _frame_text(width: int = 200) -> str:
    """Every panel one watch frame paints, rendered into one string — the only
    place a fact duplicated ACROSS builders is visible. Two panels now: the
    header and the ONE table."""
    from rich.console import Console
    from rich.panel import Panel

    status = {"arms": _FRAME_ARMS, "rtds": dict(_LIVE_RTDS)}
    sb = {"wins": 40, "losses": 6, "net": 512.25, "rolls": 12, "estimated": 0,
          "riding_n": 2, "riding_usd": 200.0, "windows": [],
          "sliding": {"wins": 9, "losses": 2, "net": 61.5, "rolls": 3, "estimated": 0}}
    snap = {"status": status, "bal": {"total": _CAP}, "sb": sb, "sb_stale": False,
            "sb_fetched_at": 1700000000.0, "err": None}
    c = Console(record=True, width=width)
    # the exposure line lives INSIDE the header panel now — no standalone row
    c.print(cc.build_header_panel(snap, "since 08-23 04:00Z", None))
    c.print(Panel(cc.build_windows_table(sb, 1700000000.0, arms=_FRAME_ARMS),
                  title=cc.windows_title(sb, _FRAME_ARMS), border_style="dim"))
    return c.export_text()


# ---------- the top box: one aligned label/value grid ----------
#
# "better alignment in the top box" — the two dot-joined prose lines this
# replaced put the same figure in a different column every tick, and on a
# narrow terminal ellipsised the digits off the ones that mattered.

_HDR_SB = {"wins": 247, "losses": 19, "net": -280.62, "rolls": 973, "estimated": 2,
           "riding_n": 7, "riding_usd": 509.23, "windows": [], "riding_windows": [],
           "sliding": {"wins": 9, "losses": 2, "net": 61.5, "rolls": 3,
                        "estimated": 1}}


def _hdr_snap(**kw):
    status = {"arms": {"btc-updown-5m-9999999999": {
        "filled_usdc": 200.0, "roll": True, "feed": "rtds", "resting_usdc": 45.0,
        "eval": {"state": "armed", "banked_decided": False, "committed": 200.0}}},
        "rtds": dict(_LIVE_RTDS)}
    snap = {"status": status, "bal": {"total": 2139.20}, "sb": dict(_HDR_SB),
            "sb_stale": False, "sb_fetched_at": 1700000000.0, "err": None, "odds": {}}
    snap.update(kw)
    return snap


def _hdr_lines(snap, render_err=None, width=120):
    """The panel's content rows with the border and panel padding stripped, so
    column 0 is the grid's own column 0."""
    from rich.console import Console

    c = Console(record=True, width=width)
    c.print(cc.build_header_panel(snap, "since 08-23 04:00Z", render_err))
    out = [ln.rstrip() for ln in c.export_text().splitlines()]
    return [ln[1:-1].rstrip()[1:] for ln in out
            if ln.startswith("│") and ln.endswith("│")]


def test_top_box_starts_every_value_field_at_the_same_column():
    lines = _hdr_lines(_hdr_snap())
    cols = set()
    for ln in lines:
        tail = ln[cc._HEAD_LABEL_W:]
        if tail.strip():
            cols.add(cc._HEAD_LABEL_W + len(tail) - len(tail.lstrip()))
    assert len(cols) == 1, f"ragged value column: {sorted(cols)}"


def test_top_box_labels_every_row_and_drops_the_ones_with_nothing_to_say():
    assert [ln[:cc._HEAD_LABEL_W].strip() for ln in _hdr_lines(_hdr_snap())] == [
        "recent", "all-time", "exposure", "resting", "feed"]
    # Binance-only, no maker bid, nothing broken: three rows, not five padded
    # with zeros.
    bare = _hdr_snap(status={"arms": {"btc-updown-5m-1": {
        "filled_usdc": 10.0, "feed": "binance", "eval": {}}}})
    assert [ln[:cc._HEAD_LABEL_W].strip() for ln in _hdr_lines(bare)] == [
        "recent", "all-time", "exposure"]


def test_top_box_puts_the_recent_record_directly_above_the_all_time_one():
    # Same column shape on both rows is what lets the two be compared by eye,
    # which is the reason they are stacked at all.
    recent, all_time = _hdr_lines(_hdr_snap())[:2]
    assert recent.index("9W-2L") == all_time.index("247W-19L")


def test_top_box_never_truncates_a_number_however_big_it_gets():
    fat = dict(_HDR_SB, wins=1247, losses=193, net=-12436.76,
               riding_n=12, riding_usd=1234.56)
    fat["sliding"] = dict(_HDR_SB["sliding"], wins=109, losses=22, net=-1234.56)
    arms = {"btc-updown-5m-9999999999": {"filled_usdc": 12345.67, "resting_usdc": 1234.56,
                                          "feed": "rtds", "eval": {}}}
    snap = _hdr_snap(sb=fat, bal={"total": 123456.78},
                     status={"arms": arms, "rtds": dict(_LIVE_RTDS)})
    for width in (160, 120, 100):
        lines = _hdr_lines(snap, width=width)
        assert "…" not in "".join(lines), (width, lines)
        assert len(lines) == cc.header_height(snap) - 2, (width, lines)
        for fact in ("1247W-193L", "-12,436.76", "$123,456.78",
                     "committed $12,345.67", "riding 12 windows $1,234.56"):
            assert any(fact in ln for ln in lines), (width, fact)


def test_top_box_height_matches_what_it_actually_paints():
    for snap, err in ((_hdr_snap(), None),
                      (_hdr_snap(err="ConnectionError: data-api down"), None),
                      (_hdr_snap(), "TypeError: " + "x" * 90)):
        assert len(_hdr_lines(snap, err)) == cc.header_height(snap, err) - 2


def test_a_failure_note_stays_one_line_however_long_it_is():
    # It is prose the width of the panel, not a value in a 20-column money
    # field: folded into a cell it would cost five rows and say less.
    lines = _hdr_lines(_hdr_snap(), render_err="RuntimeError: " + "y" * 200)
    assert lines[-1].startswith("note")
    assert len([ln for ln in lines if ln.startswith("note")]) == 1


def test_the_scoreboard_age_and_clock_ride_the_border_not_a_row():
    from rich.console import Console

    snap = _hdr_snap(sb_fetched_at=None)
    c = Console(record=True, width=120)
    c.print(cc.build_header_panel(snap, "since 08-23 04:00Z", None))
    out = c.export_text()
    # "—", never a confident "0s ago", before the first wallet walk lands.
    assert "stats —" in out
    assert "since 08-23 04:00Z" in out
    assert not any(ln[1:].lstrip().startswith("stats")
                   for ln in out.splitlines() if ln.startswith("│"))


def test_composed_frame_shows_the_capital_figure_exactly_once():
    # The regression: the exposure line led with the same "capital $X" the
    # header panel already carried, one row above it — two dense money lines
    # back to back that read as one line printed twice.
    out = _frame_text()
    assert out.count(f"{_CAP:,.2f}") == 1, out
    assert out.count("capital") == 1, out


def test_composed_frame_puts_each_fact_in_its_own_place():
    lines = _frame_text().splitlines()
    # both facts live INSIDE the top panel now (line 1 = pulse, line 2 =
    # exposure) — the bare wedge row between panels is gone
    first_close = next(i for i, l in enumerate(lines) if "╰" in l)
    head_body = "\n".join(lines[:first_close])
    assert "9W-2L" in head_body and "capital" in head_body and "all-time" in head_body
    assert "un-decided" in head_body and "riding" in head_body
    rest = "\n".join(lines[first_close:])
    # "committed" alone also names an arms-table column; the exposure
    # phrase is the unambiguous fingerprint
    assert "un-decided" not in rest, "exposure renders exactly once, in the panel"

class _FakeStdin:
    def __init__(self, isatty=True, chars=""):
        self._isatty = isatty
        self._chars = chars

    def isatty(self):
        return self._isatty

    def read(self, n):
        ch, self._chars = self._chars[:n], self._chars[n:]
        return ch

    def fileno(self):
        return 0


def test_cbreak_stdin_noop_when_not_a_tty(monkeypatch):
    monkeypatch.setattr(cc.sys, "stdin", _FakeStdin(isatty=False))
    assert cc._cbreak_stdin() is None
    cc._restore_stdin(None)  # must not raise


def test_poll_key_none_when_not_a_tty(monkeypatch):
    monkeypatch.setattr(cc.sys, "stdin", _FakeStdin(isatty=False))
    assert cc._poll_key() is None


def test_poll_key_reads_q_when_ready(monkeypatch):
    monkeypatch.setattr(cc.sys, "stdin", _FakeStdin(isatty=True, chars="q"))
    monkeypatch.setattr(cc.select, "select", lambda r, w, x, t: ([0], [], []))
    monkeypatch.setattr(cc.os, "read", lambda fd, n: b"q")
    assert cc._poll_key() == "q"


def test_poll_key_returns_other_keys_lowercased(monkeypatch):
    monkeypatch.setattr(cc.sys, "stdin", _FakeStdin(isatty=True, chars="x"))
    monkeypatch.setattr(cc.select, "select", lambda r, w, x, t: ([0], [], []))
    monkeypatch.setattr(cc.os, "read", lambda fd, n: b"H")
    assert cc._poll_key() == "h"


def test_poll_key_none_when_nothing_ready(monkeypatch):
    monkeypatch.setattr(cc.sys, "stdin", _FakeStdin(isatty=True, chars=""))
    monkeypatch.setattr(cc.select, "select", lambda r, w, x, t: ([], [], []))
    assert cc._poll_key() is None


def test_quit_requested_swallows_select_errors(monkeypatch):
    monkeypatch.setattr(cc.sys, "stdin", _FakeStdin(isatty=True))

    def boom(*a, **k):
        raise OSError("bad fd")

    monkeypatch.setattr(cc.select, "select", boom)
    assert cc._poll_key() is None  # never raises, dashboard must survive


# ---------- the controls modal ----------

def _modal_text(width=160):
    from rich.console import Console

    c = Console(record=True, width=width)
    c.print(cc.build_help_modal(width))
    return c.export_text()


def test_the_modal_spells_out_every_glyph_on_screen():
    """The modal is the only place the glyphs are named — the strip that used
    to carry a one-line legend is gone, and a glyph nothing explains is
    decoration."""
    out = _modal_text()
    for glyph in ("⟳", "≈", "◇", "○", "⊘", "◆", "✓", "✗"):
        assert glyph in out, glyph


def test_the_modal_lists_every_key_with_an_explanation():
    out = _modal_text()
    for _press, label, desc in cc.WATCH_KEYS:
        assert label in out, label
        assert desc.split(";")[0][:24] in out, desc


def test_the_modal_says_what_since_does_now_that_it_has_the_room():
    # It changes what the header's `recent` row means, and --help is not
    # reachable without leaving the dashboard.
    out = _modal_text()
    assert "--since" in out and "recent" in out


def test_the_modal_fits_a_narrow_terminal():
    # A foreground panel wider than the screen is a wrapped mess, not a modal.
    for width in (60, 78, 200):
        for ln in _modal_text(width).splitlines():
            assert len(ln) <= min(width, cc._HELP_MODAL_W), (width, ln)


def test_poll_key_passes_timeout_through_to_select(monkeypatch):
    seen = {}

    def fake_select(r, w, x, t):
        seen["timeout"] = t
        return ([], [], [])

    monkeypatch.setattr(cc.sys, "stdin", _FakeStdin(isatty=True))
    monkeypatch.setattr(cc.select, "select", fake_select)
    assert cc._poll_key() is None
    assert seen["timeout"] == 0.0          # default stays non-blocking
    assert cc._poll_key(0.05) is None
    assert seen["timeout"] == 0.05         # the watch loop's 20Hz pacing


def test_wait_key_without_a_tty_paces_instead_of_spinning(monkeypatch):
    slept = []
    monkeypatch.setattr(cc.sys, "stdin", _FakeStdin(isatty=False))
    monkeypatch.setattr(cc.time, "sleep", lambda s: slept.append(s))
    assert cc._wait_key(0.05) is None
    assert slept == [0.05]  # no tty to select on -> the sleep is the pacing



def test_committed_never_renders_negative_zero():
    # Float drift after fills settle can leave committed at ~-1e-9; it must
    # print "$0.00", never "-$0.00", everywhere money is shown.
    import watch_ui as wu
    assert wu._zero(-1e-9) == 0.0
    assert wu._zero(-0.004) == 0.0
    assert wu._zero(-0.02) == -0.02          # a real negative survives
    from rich.console import Console
    arms = {"btc-updown-5m-1000": {"eval": {"state": "armed", "committed": -1e-9,
            "p_up": 0.5, "rho": 0.0, "sides": []}, "roll": True}}
    c = Console(record=True, width=200)
    c.print(wu.build_windows_table({}, 1000.0, arms=arms))
    assert "-$0.00" not in c.export_text()
