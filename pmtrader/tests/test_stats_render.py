"""stats_render — the `pmt crypto stats` report, rendered from fixture dicts.

Every function under test is pure, so these run with no wallet, no engine and
no clock: build the same shapes score_activity/effectiveness.summary produce,
render to a StringIO console, and assert on what the operator would see.
"""

from __future__ import annotations

import io

import pytest
from rich.console import Console
from rich.text import Text

import stats_render as sr


def _render(renderable, width: int = 120) -> str:
    con = Console(file=io.StringIO(), width=width, no_color=True, highlight=False)
    con.print(renderable)
    return con.file.getvalue()


def _plain(lines: list[str], width: int = 120) -> str:
    """Markup lines as the operator sees them — the block builders return
    Rich markup, and asserting on the tags instead of the text would pass on
    a report nobody could read."""
    return _render(Text.from_markup("\n".join(lines)), width=width)


# ---------- fixtures ----------

def _sb(**kw) -> dict:
    sb = {"wins": 147, "losses": 13, "net": -436.76, "rolls": 335, "estimated": 0,
          "riding_n": 4, "riding_usd": 317.06,
          "series": {"btc 5m": {"w": 42, "l": 2, "open": 1, "pnl": -135.77,
                                 "usd": 4799.0, "est": 0, "med": 5.77},
                      "eth 5m": {"w": 33, "l": 0, "open": 0, "pnl": 132.36,
                                 "usd": 2694.0, "est": 2, "med": 2.26}},
          "cal": {0.90: [17, 13], 0.95: [837, 744]}}
    sb.update(kw)
    return sb


def _eff(**kw) -> dict:
    eff = {"n": 160, "notional": 18504.0, "pnl": -436.76,
           "win_rate": 0.919, "mww_rate": 0.89, "breakeven_win_rate": 0.925,
           "profit_factor": 0.79, "gross_win": 1610.0, "gross_loss": 2046.0,
           "return_on_notional": -0.0236,
           "rorc": {"per_hour": -0.2469, "avg_hold_h": 0.095},
           "bgr": {"per_day_pct": -0.14}, "utilization": 0.0002,
           "span_h": 5169.6, "bankroll": 1966.2,
           "streak": {"current": 7, "longest": 41}}
    eff.update(kw)
    return eff


def _arms(**kw) -> dict:
    arms = {"btc-updown-5m-1787452500": {
                "roll": True, "filled_usdc": 0.0, "feed": "binance",
                "eval": {"state": "armed", "p_up": 0.9601, "committed": -1e-15}},
            "bnb-updown-5m-1787452500": {
                "roll": False, "filled_usdc": 12.5, "feed": "rtds",
                "maker_bid": True,
                "eval": {"state": "gated", "margin_bp": -4.9, "guard_bp": 6.0,
                          "reason": "basis guard: projected margin -4.9bp inside 6.0bp"}}}
    arms.update(kw)
    return arms


def _flags(**kw) -> dict:
    flags = {"btc 5m": {"feed": "binance", "maker_bid": False},
             "eth 5m": {"feed": "rtds", "maker_bid": True}}
    flags.update(kw)
    return flags


def _maker(**kw) -> dict:
    m = {"candidates": 775, "candidate_windows": 56, "rested": 31,
         "rested_windows": 5, "placed": 26, "fills": 1, "fill_usd": 49.0,
         "fill_windows": 1, "wins": 1, "losses": 0, "pnl": 1.95}
    m.update(kw)
    return m


def _chase(**kw) -> dict:
    c = {"acks": 182, "suppressed": 2, "suppressed_share": 0.0109,
         "ack_p50": 281.71, "ack_p90": 397.22, "sign_p50": 0.17,
         "chase_n": 16, "chased": 15, "buffer_med_c": 1.29, "buffer_max_c": 4.0}
    c.update(kw)
    return c


_EMPTY_MAKER = {"candidates": 0, "candidate_windows": 0, "rested": 0,
                "rested_windows": 0, "placed": 0, "fills": 0, "fill_usd": 0.0,
                "fill_windows": 0, "wins": 0, "losses": 0, "pnl": None}
_EMPTY_CHASE = {"acks": 0, "suppressed": 0, "suppressed_share": None,
                "ack_p50": None, "ack_p90": None, "sign_p50": None,
                "chase_n": 0, "chased": 0, "buffer_med_c": None,
                "buffer_max_c": None}


def _gates(**kw) -> dict:
    """A shadow.build_report() reply, trimmed to what the table reads."""
    r = {"categories": {
             "basis_guard": {"episodes": 923, "priced": 758, "hit_rate": 0.62,
                              "missed_wins": 9734.0, "avoided_losses": 6944.0,
                              "net": 2789.0},
             "safety": {"episodes": 645, "priced": 561, "hit_rate": 0.46,
                         "missed_wins": 5051.0, "avoided_losses": 8011.0,
                         "net": -2959.0},
             "distrust": {"episodes": 0, "priced": 0, "hit_rate": None,
                           "missed_wins": 0.0, "avoided_losses": 0.0, "net": 0.0}},
         "totals": {"episodes": 1568, "missed_wins": 14785.0,
                     "avoided_losses": 14955.0, "net": -170.0},
         "coverage": {"windows": 351, "unpriced_episodes": 32,
                       "skipped_unresolved": 245}}
    r.update(kw)
    return r


# ---------- scalars ----------

def test_zeroed_kills_negative_float_dust():
    # An engine's committed figure comes back as -1e-15 often enough that
    # "$-0.00" was a standing eyesore on the arms table.
    assert sr._zeroed(-1e-15) == 0.0
    assert sr._zeroed(-0.004) == 0.0
    assert sr._zeroed(-0.02) == -0.02


def test_money_colors_by_sign_and_flat_reads_as_a_win():
    assert "green" in sr._money(12.0) and "+12.00" in sr._money(12.0)
    assert "red" in sr._money(-12.0)
    assert "green" in sr._money(0.0)
    assert "—" in sr._money(None)


def test_undefined_rates_render_a_dash_not_a_zero():
    # "no losses yet" and "zero" mean opposite things on a scoreboard.
    assert "—" in sr._pct(None)
    assert "—" in sr._signed_pct(None)
    assert sr._pct(0.0) == "0%"


def test_bar_spans_its_full_scale_and_clamps():
    assert sr._bar(1.0).count("█") == sr._BAR_W
    assert sr._bar(2.0).count("█") == sr._BAR_W  # clamped, not overdrawn
    assert sr._bar(0.0).count("░") == sr._BAR_W
    assert sr._bar(-1.0).count("░") == sr._BAR_W
    assert "·" in sr._bar(None)


def test_bar_has_sub_cell_resolution_in_the_band_that_matters():
    # 84% and 89% must not draw the same bar: every win rate on this report
    # lives in the 80-100% band, where whole blocks quantize them together.
    assert sr._bar(0.84) != sr._bar(0.89)


@pytest.mark.parametrize("rate,bar,expected", [
    (0.96, 0.925, "green"),   # clear
    (0.90, 0.925, "yellow"),  # near miss
    (0.60, 0.925, "red"),     # bleeding
    (0.90, None, "cyan"),     # nothing to measure against
    (None, 0.925, "cyan"),
])
def test_rate_style_grades_against_the_bar_it_must_clear(rate, bar, expected):
    assert sr._rate_style(rate, bar) == expected


def test_floor_label():
    assert sr.floor_label(0) == "all time"
    assert sr.floor_label(1787452500) == "windows since 08-23 02:35Z"


# ---------- header ----------

def test_gap_row_is_red_and_shouts_when_the_book_is_under_break_even():
    row = " ".join(sr._gap_cells(_eff(win_rate=0.919, breakeven_win_rate=0.925)))
    assert "GAP -0.6pp" in row
    assert "SHORT" in row and "red" in row
    assert "green" not in row


def test_gap_row_is_green_when_the_bar_is_cleared():
    row = " ".join(sr._gap_cells(_eff(win_rate=0.960, breakeven_win_rate=0.925)))
    assert "GAP +3.5pp" in row
    assert "clear of break-even" in row and "green" in row
    assert "red" not in row


def test_gap_row_says_so_rather_than_guessing_without_a_break_even():
    cells = sr._gap_cells(_eff(breakeven_win_rate=None))
    assert "not enough" in " ".join(cells)
    # ...and leaves the value cells EMPTY rather than printing a 0.0% bar: the
    # bar is unknown, which is not the same claim as zero.
    assert cells[1] == "" and cells[2] == ""


def test_header_carries_record_pnl_capital_and_the_floor():
    out = _render(sr.header_panel(_sb(), _eff(), {"total": 1649.14}, {}, 0))
    assert "147W-13L" in out and "(91.9%)" in out
    assert "-436.76" in out
    assert "$1,649.14" in out
    assert "335 rolls" in out
    assert "all time" in out


def test_header_win_rate_keeps_a_decimal_so_it_is_comparable_to_break_even():
    # Rounded to "92%" the headline reads as level with a 92.5% bar it is
    # actually under — the exact misreading this report exists to prevent.
    out = _render(sr.header_panel(_sb(), _eff(), None, {}, 0))
    assert "(91.9%)" in out and "(92%)" not in out


def test_header_shows_a_question_mark_not_a_zero_for_an_unreachable_balance():
    out = _render(sr.header_panel(_sb(), _eff(), None, {}, 0))
    assert "capital ?" in out
    assert "$0.00 " not in out.split("break-even")[0]


def test_header_notes_estimated_windows_only_when_there_are_some():
    assert "estimated" not in _render(sr.header_panel(_sb(), _eff(), None, {}, 0))
    out = _render(sr.header_panel(_sb(estimated=3), _eff(), None, {}, 0))
    assert "estimated" in out and "3 windows" in out
    assert "gamma unreachable" in out


def test_header_reports_committed_and_riding_exposure():
    out = _render(sr.header_panel(_sb(), _eff(), None, {"arms": _arms()}, 0))
    assert "$12.50" in out and "un-decided" in out  # gated arm never banked-decided
    assert "riding 4 windows $317.06" in out
    assert "resting" not in out  # taker-only arms carry no "$0.00" field


def test_header_reports_a_resting_maker_bid_only_when_one_is_on_the_book():
    arms = _arms()
    arms["btc-updown-5m-1787452500"]["resting_usdc"] = 45.0
    out = _render(sr.header_panel(_sb(), _eff(), None, {"arms": arms}, 0))
    assert "resting" in out and "$45.00" in out


def test_header_never_repeats_the_effectiveness_table():
    # The header used to carry a $W/PF/$ret/RoRC/BGR/util one-liner — the same
    # six numbers the table two sections down explains, and the line that
    # wrapped at 100 cols. One home per number.
    out = _render(sr.header_panel(_sb(), _eff(), {"total": 1649.14}, {}, 0))
    for label in ("RoRC", "BGR", "PF", "util"):
        assert label not in out


def test_header_carries_the_streak_beside_the_record():
    out = _render(sr.header_panel(_sb(), _eff(), None, {}, 0))
    assert "streak 7" in out and "best 41" in out


def test_header_streak_of_zero_still_prints_because_a_loss_just_landed():
    # A hidden zero would read as "no streak data"; it means the last window
    # lost, which is the thing the operator most wants to see.
    out = _render(sr.header_panel(_sb(), _eff(streak={"current": 0, "longest": 41}),
                                  None, {}, 0))
    assert "streak 0" in out and "best 41" in out


def test_header_omits_the_streak_entirely_before_anything_is_graded():
    out = _render(sr.header_panel(_sb(), _eff(streak={"current": 0, "longest": None}),
                                  None, {}, 0))
    assert "streak" not in out


def test_header_reports_the_fleet_cap_and_how_close_it_came():
    out = _render(sr.header_panel(_sb(), _eff(), None, {}, 0,
                                  {"cap": 350.0, "ticks": 6459,
                                   "peak_undecided": 350.0, "blocked_usd": 100.88}))
    assert "fleet cap" in out and "$350" in out
    assert "peak un-decided $350" in out and "6,459 ticks" in out
    assert "refused $101" in out


def test_header_omits_the_fleet_line_when_no_cap_is_set():
    # An uncapped fleet has infinite headroom — there is no ration to report.
    out = _render(sr.header_panel(_sb(), _eff(), None, {}, 0,
                                  {"cap": 0.0, "ticks": 0, "peak_undecided": None,
                                   "blocked_usd": 0.0}))
    assert "fleet cap" not in out


def test_header_says_so_when_a_cap_is_set_but_the_tape_never_saw_it():
    out = _render(sr.header_panel(_sb(), _eff(), None, {}, 0,
                                  {"cap": 350.0, "ticks": 0, "peak_undecided": None,
                                   "blocked_usd": 0.0}))
    assert "fleet cap" in out and "$350" in out and "no capped ticks" in out


# ---------- header: the label/value grid ----------

def _grid_lines(out: str) -> list[str]:
    """The panel's content rows with border and panel padding stripped, so
    column 0 is the grid's own column 0."""
    lines = []
    for ln in out.splitlines():
        ln = ln.rstrip()
        if ln.startswith("│") and ln.endswith("│"):
            lines.append(ln[1:-1][1:])  # drop both borders, then the left pad
    return lines


def _value_col(line: str) -> int | None:
    """Where this row's first value field starts, or None for a row that has
    only a label."""
    tail = line[sr._HDR_LABEL_W:]
    return None if not tail.strip() else sr._HDR_LABEL_W + (len(tail) - len(tail.lstrip()))


def _full_header(**kw):
    """Every row the identity box can emit, at once."""
    status = {"arms": _arms(),
              "rtds": {"started": True, "connected": True, "events_per_s": 3.0,
                        "last_event_age_s": 1.0, "consumers": 1, "reconnects": 7}}
    status["arms"]["btc-updown-5m-1787452500"]["resting_usdc"] = 45.0
    args = {"sb": _sb(estimated=3), "eff": _eff(), "bal": {"total": 1649.14},
            "status": status, "floor": 0,
            "fleet": {"cap": 350.0, "ticks": 6459, "peak_undecided": 350.0,
                       "blocked_usd": 100.88},
            "era_now": {"name": "stream", "sb": {"wins": 73, "losses": 0,
                                                  "net": 336.91}}}
    args.update(kw)
    return sr.header_panel(args["sb"], args["eff"], args["bal"], args["status"],
                           args["floor"], args["fleet"], era_now=args["era_now"],
                           scope_label=args.get("scope_label"))


def test_header_is_a_grid_with_one_value_column_for_every_row():
    # The whole point of the grid: record, era, P&L, break-even, exposure,
    # resting, fleet cap, feed and the estimated note all start their value
    # field at the SAME column, so the numbers read down as a column.
    lines = _grid_lines(_render(_full_header(), width=100))
    cols = {_value_col(ln) for ln in lines}
    cols.discard(None)
    assert len(cols) == 1, f"ragged value column: {sorted(cols)}"


def test_header_grid_labels_every_row_it_prints():
    lines = _grid_lines(_render(_full_header(), width=100))
    labels = [ln[:sr._HDR_LABEL_W].strip() for ln in lines]
    assert labels == ["record", "era stream", "P&L", "break-even", "exposure",
                      "resting", "fleet cap", "feed", "estimated"]


def test_header_grid_puts_the_era_record_under_the_all_time_record():
    # Same column shape on both rows is what lets the operator compare them by
    # eye — that comparison is the reason the era row exists.
    lines = _grid_lines(_render(_full_header(), width=100))
    record, era = lines[0], lines[1]
    assert record.index("147W-13L") == era.index("73W-0L")


def test_header_grid_never_wraps_at_a_hundred_columns():
    out = _render(_full_header(), width=100)
    assert all(len(ln) <= 100 for ln in out.splitlines())
    # A wrap would show up as extra content rows, not just a long line.
    assert len(_grid_lines(out)) == 9


def test_header_grid_holds_its_shape_when_every_number_grows_a_digit():
    # The regression: "streak 101 (best 101)" on a dot-joined identity line
    # pushed it past 100 columns and wrapped mid-token.
    out = _render(_full_header(
        sb=_sb(wins=1147, losses=113, net=-12436.76, rolls=12345, estimated=3),
        eff=_eff(streak={"current": 101, "longest": 101}),
        bal={"total": 123456.78}), width=100)
    assert all(len(ln) <= 100 for ln in out.splitlines())
    assert len(_grid_lines(out)) == 9
    assert "streak" in out and "101" in out


def test_header_grid_drops_rows_with_nothing_to_say():
    # No cap, no resting bid, no stream, nothing estimated: those rows are
    # absent, not padded with zeros.
    out = _render(sr.header_panel(_sb(), _eff(), None, {}, 0), width=100)
    labels = [ln[:sr._HDR_LABEL_W].strip() for ln in _grid_lines(out)]
    assert labels == ["record", "P&L", "break-even", "exposure"]


def test_header_grid_keeps_its_colour_semantics():
    # Tabulating the box must not flatten it: the loss-red P&L, the green
    # streak and the break-even verdict all still carry their style.
    con = Console(file=io.StringIO(), width=100, force_terminal=True,
                  color_system="truecolor", highlight=False)
    con.print(_full_header())
    out = con.file.getvalue()
    assert "\x1b[" in out


def test_header_grid_alignment_survives_a_scoped_era_view():
    lines = _grid_lines(_render(_full_header(scope_label="era theta · 05:00Z→10:39Z"),
                                width=100))
    cols = {_value_col(ln) for ln in lines}
    cols.discard(None)
    assert len(cols) == 1




# ---------- by symbol ----------

def test_symbol_table_shows_record_flags_net_median_and_a_win_rate_bar():
    out = _render(sr.symbol_table(_sb()["series"], _flags(), breakeven=0.925))
    assert "42-2" in out and "1 open" in out
    assert "~2" in out  # eth's two ~estimated grades
    assert "-135.77" in out and "+132.36" in out
    assert "$4,799" in out
    assert "95%" in out and "100%" in out
    assert "█" in out


def test_symbol_table_carries_the_median_window_the_totals_hide():
    # btc's -$135.77 total is one tail on 44 windows whose typical one paid
    # +$5.77 — sum and median disagree, and that IS the sizing question.
    out = _render(sr.symbol_table(_sb()["series"], _flags(), 0.925))
    assert "+5.77" in out and "+2.26" in out


def test_symbol_table_dashes_a_median_that_does_not_exist_yet():
    series = {"x": {"w": 0, "l": 0, "open": 2, "pnl": 0.0, "usd": 50.0,
                     "est": 0, "med": None}}
    out = _render(sr.symbol_table(series, {}, 0.925))
    assert "—" in out


def test_symbol_table_names_the_feed_each_series_is_armed_on():
    out = _render(sr.symbol_table(_sb()["series"], _flags(), 0.925))
    assert "binance" in out          # btc 5m, taker on the proxy feed
    assert "≈rtds" in out       # eth 5m, off the settlement stream
    assert "◇" in out           # ...and resting maker bids


def test_symbol_table_dashes_the_feed_for_a_series_with_no_live_arm():
    # The series traded, but nothing armed is claiming these params now —
    # which is not the same as "binance".
    out = _render(sr.symbol_table(_sb()["series"], {}, 0.925))
    assert "binance" not in out and "rtds" not in out
    assert "—" in out


def test_symbol_bar_is_colored_against_the_break_even_bar():
    losing = {"x": {"w": 8, "l": 2, "open": 0, "pnl": -50.0, "usd": 100.0,
                     "est": 0, "med": -1.0}}
    winning = {"x": {"w": 10, "l": 0, "open": 0, "pnl": 50.0, "usd": 100.0,
                      "est": 0, "med": 5.0}}
    con = Console(file=io.StringIO(), width=120)
    assert sr._rate_style(0.8, 0.925) == "red"
    assert sr._rate_style(1.0, 0.925) == "green"
    # and both still render without a break-even to measure against
    for series in (losing, winning):
        con.print(sr.symbol_table(series, {}, breakeven=None))
    assert "█" in con.file.getvalue()


def test_symbol_table_rows_never_wrap_at_a_hundred_columns():
    out = _render(sr.symbol_table(_sb()["series"], _flags(), 0.925), width=100)
    body = [ln for ln in out.splitlines() if ln.strip()]
    assert len(body) == 3  # header + two series, no continuation lines
    assert all(len(ln) <= 100 for ln in out.splitlines())


# ---------- effectiveness ----------

def test_effectiveness_table_pairs_every_number_with_what_it_means():
    out = _render(sr.effectiveness_table(_eff()))
    for label in ("$-weighted win rate", "break-even win rate", "profit factor",
                  "return on notional", "RoRC", "bankroll growth", "utilization"):
        assert label in out
    assert "92.5%" in out and "0.79" in out and "-24.69%/h" in out
    assert "$18,504" in out and "hold 5.7m" in out
    assert "215.4d" in out  # a 5169h span reported in days, not hours


def test_effectiveness_table_reports_a_short_span_in_hours():
    assert "6.2h" in _render(sr.effectiveness_table(_eff(span_h=6.2)))


def test_effectiveness_table_never_wraps_at_a_hundred_columns():
    # The prose column is written to the width it has; a folded explanation
    # tore the alignment of every row under it.
    out = _render(sr.effectiveness_table(_eff()), width=100)
    body = [ln for ln in out.splitlines() if ln.strip()]
    assert len(body) == 8  # header + seven metrics, one line each
    assert all(len(ln) <= 100 for ln in out.splitlines())


def test_effectiveness_table_dashes_every_undefined_metric():
    empty = {"n": 0, "notional": 0.0, "pnl": 0.0, "win_rate": None,
             "mww_rate": None, "breakeven_win_rate": None, "profit_factor": None,
             "gross_win": 0.0, "gross_loss": 0.0, "return_on_notional": None,
             "rorc": None, "bgr": None, "utilization": None, "span_h": 0.0,
             "bankroll": None}
    out = _render(sr.effectiveness_table(empty))
    assert out.count("—") >= 5


# ---------- the resting-bid experiment ----------

def test_resting_block_separates_the_shadow_class_from_real_bids():
    lines = _plain(sr.resting_lines(_maker()))
    assert "candidates 775" in lines and "56 windows" in lines
    assert "knob off" in lines
    assert "rested 31" in lines and "placed 26" in lines


def test_resting_block_labels_its_fill_attribution_as_experiment_grade():
    lines = _plain(sr.resting_lines(_maker()))
    assert "fills 1" in lines and "1W-0L" in lines and "+1.95" in lines
    assert "experiment-grade" in lines
    assert "not proven" in lines


def test_resting_block_gets_the_window_count_grammar_right():
    one = _plain(sr.resting_lines(_maker(fill_windows=1)))
    many = _plain(sr.resting_lines(_maker(fill_windows=3)))
    assert "in 1 window " in one
    assert "in 3 windows" in many


def test_resting_block_is_empty_when_the_tape_has_nothing_to_say():
    assert sr.resting_lines(_EMPTY_MAKER) == []
    assert sr.resting_lines({}) == []


def test_resting_block_omits_the_fill_row_until_something_landed():
    lines = _plain(sr.resting_lines(_maker(fills=0, wins=0, losses=0, pnl=None)))
    assert "rested 31" in lines
    assert "fills" not in lines and "experiment-grade" not in lines


# ---------- the order path ----------

def test_chase_block_reports_the_wire_and_what_the_matcher_suppressed():
    lines = _plain(sr.chase_lines(_chase()))
    assert "182 acked" in lines
    assert "suppressed 2" in lines and "1.1%" in lines
    assert "p50 282ms" in lines and "p90 397ms" in lines


def test_chase_block_refuses_to_print_a_sub_millisecond_stage_as_zero():
    # "sign p50 0ms" implies a stage that measured zero rather than one that
    # is not a stage at all.
    lines = _plain(sr.chase_lines(_chase(sign_p50=0.17)))
    assert "0ms" not in lines and "sign <1ms" in lines
    slow = _plain(sr.chase_lines(_chase(sign_p50=42.0)))
    assert "sign p50 42ms" in slow


def test_chase_block_prices_the_pay_up_buffer_actually_spent():
    lines = _plain(sr.chase_lines(_chase()))
    assert "15 of 16 priced fires chased" in lines
    assert "median 1.29c" in lines and "max 4.00c" in lines


def test_chase_block_is_empty_when_the_tape_has_nothing_to_say():
    assert sr.chase_lines(_EMPTY_CHASE) == []
    assert sr.chase_lines({}) == []


def test_chase_block_prints_the_wire_even_before_any_fire_carried_a_limit():
    lines = _plain(sr.chase_lines(_chase(chase_n=0, chased=0,
                                             buffer_med_c=None, buffer_max_c=None)))
    assert "182 acked" in lines
    assert "pay-up" not in lines  # unknown, not zero


def test_chase_block_lines_never_wrap_at_a_hundred_columns():
    for line in sr.chase_lines(_chase()):
        assert len(_render(line, width=100).rstrip("\n")) <= 100


# ---------- gates ----------

def test_gates_table_flips_the_money_color_because_net_is_a_refusal():
    # POSITIVE net on this ledger means the gate turned down money (over-tight)
    # — the opposite sign convention to every other money cell on the report.
    from polymarket import shadow

    con = Console(file=io.StringIO(), width=100, no_color=True, highlight=False)
    con.print(sr.gates_table(_gates(), list(shadow.CATEGORY_ORDER), shadow.verdict))
    out = con.file.getvalue()
    assert "basis_guard" in out and "+2,789" in out
    assert "safety" in out and "-2,959" in out
    assert "over-tight" in out and "paying for itself" in out
    # a category with zero episodes never gets a row
    assert "distrust" not in out
    assert all(len(ln) <= 100 for ln in out.splitlines())


def test_gates_footer_carries_both_halves_and_the_coverage_gap():
    lines = _plain(sr.gates_footer(_gates()))
    assert "missed wins 14,785" in lines and "avoided losses 14,955" in lines
    assert "-170" in lines
    assert "351 windows" in lines and "32 unpriced" in lines
    assert "245 unresolved" in lines


# ---------- calibration (--full only) ----------

def test_calibration_table_grades_each_bucket_against_its_own_stated_fair():
    out = _render(sr.calibration_table({0.90: [17, 13], 0.95: [837, 744]}))
    assert "0.90" in out and "13/17" in out and "76%" in out
    assert "744/837" in out and "89%" in out
    # 76% realized on a 0.90 stated fair is over-confidence, and says so in red
    assert sr._rate_style(13 / 17, 0.90) == "red"
    assert sr._rate_style(1.0, 0.95) == "green"


# ---------- the whole report ----------

def _blocks(**kw) -> dict:
    b = {"flags": _flags(), "maker": _maker(), "chase": _chase(),
         "fleet": {"cap": 350.0, "ticks": 6459, "peak_undecided": 350.0,
                    "blocked_usd": 100.88}}
    b.update(kw)
    return b


def test_render_stats_reads_top_down_in_the_order_the_operator_asks():
    out = _render(sr.render_stats(_sb(), _eff(), {"total": 1649.14},
                                  {"arms": _arms(), "pending_rolls": ["btc 5m"]},
                                  1787452500, blocks=_blocks()))
    assert "updown fleet" in out and "windows since 08-23 02:35Z" in out
    order = [out.index(h) for h in ("updown fleet", "by symbol", "effectiveness",
                                     "resting bids", "order path")]
    assert order == sorted(order)


def test_render_stats_default_view_stays_focused():
    # calibration is superseded by analysis/r6_report.txt; a static live-arms
    # snapshot is `pmt crypto watch`'s job. Both are --full only.
    out = _render(sr.render_stats(_sb(), _eff(), {"total": 1649.14},
                                  {"arms": _arms()}, 0, blocks=_blocks()))
    assert "calibration" not in out
    assert "live arms" not in out


def test_render_stats_full_restores_everything_demoted():
    out = _render(sr.render_stats(_sb(), _eff(), {"total": 1649.14},
                                  {"arms": _arms(), "pending_rolls": ["btc 5m"]},
                                  0, blocks=_blocks(), full=True), width=140)
    assert "calibration" in out
    assert "live arms" in out and "pending rolls: btc 5m" in out
    # and the demoted blocks come AFTER everything the default view shows
    assert out.index("order path") < out.index("calibration") < out.index("live arms")


def test_render_stats_full_uses_watchs_own_arms_table_not_a_second_one():
    # A static snapshot that can drift from the live dashboard is worse than
    # no snapshot: --full renders watch_ui.build_arms_table verbatim.
    out = _render(sr.render_stats(_sb(), _eff(), None, {"arms": _arms()}, 0,
                                  blocks=_blocks(), full=True), width=140)
    for col in ("evidence", "p_up", "mode", "rho", "flags"):
        assert col in out
    assert not hasattr(sr, "arms_table")


def test_render_stats_drops_every_block_that_has_no_data():
    out = _render(sr.render_stats({"wins": 0, "losses": 0, "net": 0.0, "rolls": 0},
                                  _eff(n=0, streak={"current": 0, "longest": None}),
                                  None, None, 0,
                                  blocks={"flags": {}, "maker": _EMPTY_MAKER,
                                           "chase": _EMPTY_CHASE, "fleet": None},
                                  full=True))
    for header in ("by symbol", "effectiveness", "resting bids", "order path",
                   "calibration", "live arms", "gates"):
        assert header not in out
    assert "0W-0L" in out


def test_render_stats_survives_an_engine_that_never_answered():
    # status={} is what crypto_stats passes when engine.post() sys.exit()s.
    out = _render(sr.render_stats(_sb(), _eff(), {"total": 1649.14}, {}, 0,
                                  blocks=_blocks()))
    assert "live arms" not in out
    assert "committed $0.00" in out
    assert "147W-13L" in out


def test_render_stats_works_with_no_blocks_at_all():
    # The tape folds are optional: a caller that hasn't computed them still
    # gets the wallet half of the report.
    out = _render(sr.render_stats(_sb(), _eff(), None, {}, 0))
    assert "by symbol" in out and "effectiveness" in out
    assert "resting bids" not in out and "order path" not in out


def test_render_stats_adds_the_gates_section_only_when_asked():
    out = _render(sr.render_stats(_sb(), _eff(), None, {}, 0, blocks=_blocks(),
                                  gates=_gates()), width=100)
    assert "gates" in out and "basis_guard" in out
    assert "hindsight-priced" in out
    assert all(len(ln) <= 100 for ln in out.splitlines())


def test_render_stats_never_wraps_a_default_report_at_a_hundred_columns():
    out = _render(sr.render_stats(_sb(), _eff(), {"total": 1649.14},
                                  {"arms": _arms()}, 1787452500,
                                  blocks=_blocks()), width=100)
    assert all(len(ln) <= 100 for ln in out.splitlines())


# ---------- by era ----------

def _era(name, start, end, wins, losses, net, breakeven=None, span_h=3.0) -> dict:
    return {"name": name, "why": f"why {name}", "start": start, "end": end,
            "span_h": span_h, "breakeven": breakeven,
            "sb": {"wins": wins, "losses": losses, "net": net, "rolls": 0,
                   "series": {}, "cal": {}, "estimated": 0}}


def _eras() -> list[dict]:
    return [
        _era("pre-brake", 0.0, 1787451100.0, 49, 8, -447.87, 0.876, span_h=None),
        _era("brakes", 1787451100.0, 1787461200.0, 60, 4, -91.04, 0.946, span_h=2.8),
        _era("quiet", 1787461200.0, 1787481547.0, 0, 0, 0.0, None, span_h=5.7),
        _era("stream", 1787481547.0, float("inf"), 73, 0, 336.91, None, span_h=5.7),
    ]


def test_era_table_shows_every_era_including_ones_that_traded_nothing():
    out = _render(sr.era_table(_eras()), width=100)
    for name in ("pre-brake", "brakes", "quiet", "stream"):
        assert name in out
    assert "0-0" in out  # the empty era renders, it does not vanish


def test_era_table_prints_the_gap_against_each_eras_own_breakeven_bar():
    out = _render(sr.era_table(_eras()), width=100)
    assert "-1.6pp" in out   # 86.0% actual against an 87.6% bar
    assert "-0.8pp" in out   # 93.8% against 94.6%


def test_era_table_dashes_an_undefined_breakeven_rather_than_zeroing_it():
    # An era with no losses cannot size its payoff shape; a 0% bar would read
    # as "clears everything", which is the opposite of "not yet known".
    out = _render(sr.era_table([_era("stream", 100.0, float("inf"), 73, 0, 336.91)]),
                  width=100)
    assert "—" in out and "0.0pp" not in out


def test_era_table_labels_the_open_left_era_as_open():
    out = _render(sr.era_table(_eras()), width=100)
    assert "open" in out


def test_era_table_marks_only_the_era_the_report_is_looking_through():
    out = _render(sr.era_table(_eras(), marked="brakes"), width=100)
    assert out.count("◀") == 1


def test_era_table_fits_a_hundred_columns():
    out = _render(sr.era_table(_eras(), marked="stream"), width=100)
    assert all(len(ln) <= 100 for ln in out.splitlines())


def test_era_footnote_states_the_rules_and_counts_the_eras():
    lines = sr.era_footnote(_eras(), marked="stream")
    out = _plain(lines, width=100)
    assert "DEPLOY moments" in out and "eras.py" in out
    assert "all 4 eras listed" in out and "none may be hidden" in out
    assert "ledger of record" in out
    assert "why stream" in out            # the marked era says what it was
    assert all(len(ln) <= 100 for ln in out.splitlines())


def test_era_row_pairs_with_the_identity_row():
    label, *cells = sr.era_cells(_era("stream", 100.0, float("inf"), 73, 0, 336.91))
    out = _plain([" ".join(cells)], width=100)
    assert label == "era stream"
    assert "73W-0L" in out and "+336.91" in out
    assert "vs all-time above" in out


def test_era_row_says_scoped_when_the_whole_report_is_one_era():
    # Under --era the row above is NOT all-time, so the note must not say it is.
    cells = sr.era_cells(_era("theta", 100.0, 200.0, 44, 1, 115.59), scoped=True)
    out = _plain([" ".join(cells[1:])], width=100)
    assert "scoped" in out and "vs all-time above" not in out


def test_era_row_is_absent_when_there_is_no_era_to_name():
    assert sr.era_cells(None) is None


def test_era_span_label_marks_both_open_ends():
    assert sr.era_span_label(0.0, 1787451100.0).startswith("open→")
    assert sr.era_span_label(1787451100.0, float("inf")).endswith("→now")
    assert sr.era_span_label(1787451100.0, 1787461200.0) == "02:11Z→05:00Z"


def test_render_stats_puts_the_era_table_in_the_default_view():
    # It earns default placement: it answers the operator's standing question,
    # which the all-time line structurally cannot.
    out = _render(sr.render_stats(_sb(), _eff(), {"total": 1649.14}, {}, 0,
                                  blocks=_blocks(), era_rows=_eras(),
                                  era_now=_eras()[-1]), width=100)
    assert "by era" in out
    assert out.index("updown fleet") < out.index("by era") < out.index("by symbol")
    assert "era stream" in out          # the identity chip beside all-time
    assert all(len(ln) <= 100 for ln in out.splitlines())


def test_render_stats_era_scope_relabels_the_header_and_keeps_every_era():
    out = _render(sr.render_stats(_sb(), _eff(), None, {}, 1787461200,
                                  blocks=_blocks(), era_rows=_eras(),
                                  era_now=_eras()[1],
                                  scope_label="era brakes · 02:11Z→05:00Z"), width=100)
    assert "era brakes · 02:11Z→05:00Z" in out
    assert "windows since" not in out
    for name in ("pre-brake", "brakes", "quiet", "stream"):
        assert name in out          # scoping the view hides no era
    assert "scoped" in out and "drop --era" in out


def test_render_stats_says_why_the_era_table_is_missing_under_since():
    out = _render(sr.render_stats(_sb(), _eff(), None, {}, 1787452500,
                                  blocks=_blocks(), era_rows=None,
                                  eras_omitted=True), width=100)
    # Half an era table is worse than none — say so rather than show a short one.
    assert "by era" in out and "omitted" in out
    assert "--since floors the wallet walk" in out
    assert all(len(ln) <= 100 for ln in out.splitlines())


def test_render_stats_without_era_rows_is_the_report_as_it_was():
    out = _render(sr.render_stats(_sb(), _eff(), None, {}, 0, blocks=_blocks()),
                  width=100)
    assert "by era" not in out
    assert "by symbol" in out and "effectiveness" in out
