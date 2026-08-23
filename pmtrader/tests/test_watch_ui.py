"""Pure-seam tests for the crypto watch dashboard's render layer: margin-regex
parsing, evidence/countdown color thresholds, the tape log's fixed-width
alignment, the risk header's committed/undecided math, the recent-windows
strip, the arms table's column geometry, and the terminal-mode helpers.

Every dashboard render function must tolerate a missing/partial eval (an
engine restart mid-watch leaves last_eval None or half-built) — several tests
below exist specifically to pin that behavior down.

The fetch/grade side of the same dashboard is tested in test_cli_crypto.py.
"""

from __future__ import annotations

import json
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

    rendered = {name: click.unstyle(cc._tape_render(json.dumps(r)))
                for name, r in (("fire", fire), ("eval", eval_),
                                 ("gated", gated), ("roll", roll))}
    for name, line in rendered.items():
        assert line[:len(head)] == head, name
        assert line[len(head)] == " ", name
        # the field after the fixed-width event tag starts at the same offset
        # for every event type — this is the actual alignment claim.
        assert line[len(head) + 1 + cc._TAPE_TAG_WIDTH] == " ", name


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


def test_build_risk_header_color_thresholds():
    sb = {"riding_n": 0, "riding_usd": 0.0}

    def undecided(amount):
        status = {"arms": {"a": {"filled_usdc": amount, "eval": {"banked_decided": False}}}}
        return cc.build_risk_header(status, sb)

    assert "[red]" in undecided(600.0)
    assert "[yellow]" in undecided(400.0)
    line = undecided(200.0)
    assert "[red]" not in line and "[yellow]" not in line


def test_build_risk_header_never_repeats_the_top_panel_capital():
    # The dupe this line was born with: capital belongs to the header panel
    # directly above it, and two money lines back to back read as one line
    # printed twice.
    line = cc.build_risk_header(
        {"arms": {"a": {"filled_usdc": 1.0, "eval": {}}}},
        {"riding_n": 0, "riding_usd": 0.0})
    assert "capital" not in line
    assert line.startswith("committed ")


def test_build_risk_header_shows_resting_only_when_a_bid_is_on_the_book():
    sb = {"riding_n": 0, "riding_usd": 0.0}
    resting = cc.build_risk_header(
        {"arms": {"a": {"filled_usdc": 10.0, "resting_usdc": 45.0, "eval": {}}}}, sb)
    assert "◇resting $45.00" in resting
    # A taker-only fleet must not carry a "$0.00" field every tick.
    flat = cc.build_risk_header(
        {"arms": {"a": {"filled_usdc": 10.0, "resting_usdc": 0.0, "eval": {}}}}, sb)
    assert "resting" not in flat


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


def test_risk_header_carries_the_stream_state_when_an_arm_reads_it():
    sb = {"riding_n": 0, "riding_usd": 0.0}
    status = {"arms": {"a": {"filled_usdc": 1.0, "feed": "rtds", "eval": {}}},
              "rtds": _LIVE_RTDS}
    assert "rtds" in cc.build_risk_header(status, sb)
    # ...and stays out of the way when the fleet is binance-only.
    binance = {"arms": {"a": {"filled_usdc": 1.0, "feed": "binance", "eval": {}}},
               "rtds": _LIVE_RTDS}
    assert "rtds" not in cc.build_risk_header(binance, sb)


def test_arms_table_marks_which_feed_an_arm_reads():
    from rich.console import Console

    arms = {
        "xrp-updown-5m-9999999999": {"filled_usdc": 1.0, "roll": True,
                                      "feed": "rtds", "eval": None},
        "btc-updown-5m-9999999999": {"filled_usdc": 1.0, "roll": True,
                                      "feed": "binance", "eval": None},
    }
    c = Console(record=True, width=160)
    c.print(cc.build_arms_table(arms, now=1000.0))
    out = c.export_text().splitlines()
    xrp = next(ln for ln in out if "xrp" in ln)
    btc = next(ln for ln in out if "btc" in ln)
    assert "≈" in xrp, xrp
    assert "≈" not in btc, btc


def test_flag_column_fits_every_marker_at_once():
    # ⟳ + ≈ + ◇ on one arm is a real state (a rolling, stream-fed, maker
    # arm). The column was sized for "roll" alone and silently ellipsised the
    # third marker when maker step 0 added it.
    from rich.console import Console

    arms = {"xrp-updown-5m-1700000000": {"filled_usdc": 1.0, "roll": True,
                                          "feed": "rtds", "maker_bid": True,
                                          "eval": None}}
    c = Console(record=True, width=160)
    c.print(cc.build_arms_table(arms, now=1700000100.0))
    out = c.export_text().splitlines()
    row = next(ln for ln in out if "xrp" in ln)
    # last cell, i.e. between the final two column separators
    flags = row.rsplit("│", 3)[-2]
    assert flags.strip() == "⟳≈◇", row
    assert "…" not in flags, row
    # ...and the header still names what the column now holds.
    header = next(ln for ln in out if "T-" in ln and "flags" in ln)
    assert "arm" in header and "roll" not in header


def test_committed_column_fits_a_resting_bid_beside_a_four_figure_fill():
    # The resting $ was added into the committed cell without widening it.
    from rich.console import Console

    arms = {"btc-updown-5m-9999999999": {
        "filled_usdc": 1204.50, "roll": True, "resting_usdc": 450.0,
        "eval": {"state": "armed", "committed": 1204.50, "resting": 450.0}}}
    c = Console(record=True, width=160)
    c.print(cc.build_arms_table(arms, now=1000.0))
    row = next(ln for ln in c.export_text().splitlines() if "btc" in ln)
    assert "$1,204.50 ◇$450" in row, row


def test_controls_panel_legend_names_every_arms_table_marker():
    # The overlay is the only place the glyphs are spelled out.
    from rich.console import Console
    import io

    con = Console(file=io.StringIO(), width=160)
    con.print(cc._controls_panel())
    out = con.file.getvalue()
    for glyph in ("⟳", "≈", "◇"):
        assert glyph in out, glyph
    # ONE content line: the strip slot is 3 rows and a second would be clipped.
    body = [ln for ln in out.splitlines() if ln.startswith("│")]
    assert len(body) == 1, body


# ---------- recent-windows strip ----------

def test_window_chip_formats_win_and_loss():
    win = cc._window_chip({"slug": "btc-updown-5m-1700000000", "won": True,
                            "pnl": 12.0, "est": False})
    loss = cc._window_chip({"slug": "eth-updown-15m-1700000000", "won": False,
                             "pnl": -44.0, "est": False})
    assert win == "[green]✓ btc5 +12[/green]"
    assert loss == "[red]✗ eth15 -44[/red]"


def test_window_chip_estimated_is_dim_not_win_loss_colored():
    chip = cc._window_chip({"slug": "sol-updown-5m-1700000000", "won": True,
                             "pnl": 3.0, "est": True})
    assert chip.startswith("[dim]") and "[green]" not in chip


def test_build_windows_strip_empty():
    assert cc.build_windows_strip([]) == "[dim]no resolved windows yet[/dim]"
    assert cc.build_windows_strip(None) == "[dim]no resolved windows yet[/dim]"


# ---------- arms table: missing/partial eval tolerance (4d) ----------

def test_build_arms_table_no_arms_shows_placeholder():
    from rich.console import Console

    t = cc.build_arms_table({}, now=1000.0)
    c = Console(record=True, width=140)
    c.print(t)
    assert "engine unreachable or no arms" in c.export_text()


def test_build_arms_table_tolerates_none_and_partial_eval():
    from rich.console import Console

    arms = {
        "btc-updown-5m-9999999999": {"filled_usdc": 10.0, "roll": True, "eval": None},
        "eth-updown-15m-8888888888": {"filled_usdc": 5.0, "roll": False,
                                       "eval": {"state": "armed"}},  # no p_up/rho/sides
        "not-a-real-slug": {},  # missing filled_usdc/roll entirely
    }
    t = cc.build_arms_table(arms, now=1000.0)  # must not raise
    c = Console(record=True, width=160)
    c.print(t)
    out = c.export_text()
    assert "—" in out  # missing fields degrade gracefully, not a traceback
    assert "$10.00" in out


def test_build_arms_table_column_geometry_is_identical_across_states():
    from rich.console import Console

    def col_boundaries(arms):
        t = cc.build_arms_table(arms, now=1700000000.0)
        c = Console(record=True, width=140)
        c.print(t)
        lines = [ln for ln in c.export_text().splitlines() if ln.startswith("│")]
        return [[i for i, ch in enumerate(ln) if ch == "│"] for ln in lines]

    short = col_boundaries({"btc-updown-5m-1700000300": {"filled_usdc": 1.0, "eval": None}})
    long_reason = ("basis guard: projected margin -4.9bp inside 6.0bp noise band "
                   "[banked -3.2bp cushion 9.3bp] and then some extra unexpected text")
    long_ = col_boundaries({
        "btc-updown-5m-1700000300": {"filled_usdc": 1.0,
                                      "eval": {"state": "gated", "reason": long_reason}},
        "eth-updown-15m-1700000900": {"filled_usdc": 2.0, "eval": {
            "state": "armed", "p_up": 0.8732, "mode": "safe", "rho": 0.4,
            "banked_bp": 12.3, "cushion_bp": 9.3, "banked_decided": True,
            "committed": 2.0, "sides": [{"side": "up", "safety": 0.9}],
        }},
    })
    assert short[0] == long_[0] == long_[1]


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
    assert "gated ×3" in out[0]
    # freshest margin per symbol, sorted, on one line — the pre-abstraction shape
    assert "basis bp/guard: btc +2.0/6 · eth -4.9/6" in out[0]


def test_basis_guard_run_ends_on_any_other_event():
    out = _collapse(_basis(0), _basis(1), _eval(2), _basis(3))
    assert len(out) == 3
    assert "gated ×2" in out[0]
    assert "eval" in out[1]
    assert "gated ×1" in out[2]  # a fresh run, counted from one


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


def _frame_text(width: int = 160) -> str:
    """Every panel one watch frame paints, rendered into one string — the only
    place a fact duplicated ACROSS builders is visible."""
    from rich.console import Console
    from rich.panel import Panel
    from rich.text import Text

    status = {"arms": _FRAME_ARMS, "rtds": dict(_LIVE_RTDS)}
    sb = {"wins": 40, "losses": 6, "net": 512.25, "rolls": 12, "estimated": 0,
          "riding_n": 2, "riding_usd": 200.0, "windows": [],
          "sliding": {"wins": 9, "losses": 2, "net": 61.5, "rolls": 3, "estimated": 0}}
    snap = {"status": status, "bal": {"total": _CAP}, "sb": sb, "sb_stale": False,
            "sb_fetched_at": 1700000000.0, "err": None}
    c = Console(record=True, width=width)
    # the exposure line lives INSIDE the header panel now — no standalone row
    c.print(cc.build_header_panel(snap, "since 08-23 04:00Z", None))
    c.print(cc.build_arms_table(_FRAME_ARMS, now=1700000000.0))
    c.print(Panel(Text.from_markup(cc.build_windows_strip(sb["windows"])),
                  title="recent windows", border_style="dim"))
    return c.export_text()


def test_composed_frame_shows_the_capital_figure_exactly_once():
    # The regression: build_risk_header led with the same "capital $X" the
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


def test_controls_panel_renders():
    from rich.console import Console
    import io
    con = Console(file=io.StringIO(), width=160)
    con.print(cc._controls_panel())
    out = con.file.getvalue()
    assert "quit" in out and "controls" in out


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

