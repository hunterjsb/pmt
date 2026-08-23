"""stats_render — the `pmt crypto stats` report, rendered from fixture dicts.

Every function under test is pure, so these run with no wallet, no engine and
no clock: build the same shapes score_activity/effectiveness.summary produce,
render to a StringIO console, and assert on what the operator would see.
"""

from __future__ import annotations

import io

import pytest
from rich.console import Console

import stats_render as sr


def _render(renderable, width: int = 120) -> str:
    con = Console(file=io.StringIO(), width=width, no_color=True, highlight=False)
    con.print(renderable)
    return con.file.getvalue()


# ---------- fixtures ----------

def _sb(**kw) -> dict:
    sb = {"wins": 147, "losses": 13, "net": -436.76, "rolls": 335, "estimated": 0,
          "riding_n": 4, "riding_usd": 317.06,
          "series": {"btc 5m": {"w": 42, "l": 2, "open": 1, "pnl": -135.77,
                                 "usd": 4799.0, "est": 0},
                      "eth 5m": {"w": 33, "l": 0, "open": 0, "pnl": 132.36,
                                 "usd": 2694.0, "est": 2}},
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
           "span_h": 5169.6, "bankroll": 1966.2}
    eff.update(kw)
    return eff


def _arms(**kw) -> dict:
    arms = {"btc-updown-5m-1787452500": {
                "roll": True, "filled_usdc": 0.0,
                "eval": {"state": "armed", "p_up": 0.9601, "committed": -1e-15}},
            "bnb-updown-5m-1787452500": {
                "roll": False, "filled_usdc": 12.5,
                "eval": {"state": "gated", "margin_bp": -4.9, "guard_bp": 6.0,
                          "reason": "basis guard: projected margin -4.9bp inside 6.0bp"}}}
    arms.update(kw)
    return arms


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

def test_gap_line_is_red_and_shouts_when_the_book_is_under_break_even():
    line = sr._gap_line(_eff(win_rate=0.919, breakeven_win_rate=0.925))
    assert "GAP -0.6pp" in line
    assert "SHORT" in line and "red" in line
    assert "green" not in line


def test_gap_line_is_green_when_the_bar_is_cleared():
    line = sr._gap_line(_eff(win_rate=0.960, breakeven_win_rate=0.925))
    assert "GAP +3.5pp" in line
    assert "clear of break-even" in line and "green" in line
    assert "red" not in line


def test_gap_line_says_so_rather_than_guessing_without_a_break_even():
    assert "not enough" in sr._gap_line(_eff(breakeven_win_rate=None))


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
    assert "~estimated" not in _render(sr.header_panel(_sb(), _eff(), None, {}, 0))
    out = _render(sr.header_panel(_sb(estimated=3), _eff(), None, {}, 0))
    assert "3 ~estimated" in out


def test_header_reports_committed_and_riding_exposure():
    out = _render(sr.header_panel(_sb(), _eff(), None, {"arms": _arms()}, 0))
    assert "$12.50" in out and "un-decided" in out  # gated arm never banked-decided
    assert "riding 4 windows $317.06" in out


def test_header_drops_the_effectiveness_line_on_an_empty_book():
    out = _render(sr.header_panel(_sb(wins=0, losses=0), _eff(n=0), None, {}, 0))
    assert "RoRC" not in out


# ---------- series ----------

def test_series_table_shows_record_flags_pnl_and_a_win_rate_bar():
    out = _render(sr.series_table(_sb()["series"], breakeven=0.925))
    assert "42-2" in out and "1 open" in out
    assert "~2" in out  # eth's two ~estimated grades
    assert "-135.77" in out and "+132.36" in out
    assert "$4,799" in out
    assert "95%" in out and "100%" in out
    assert "█" in out


def test_series_bar_is_colored_against_the_break_even_bar():
    losing = {"x": {"w": 8, "l": 2, "open": 0, "pnl": -50.0, "usd": 100.0, "est": 0}}
    winning = {"x": {"w": 10, "l": 0, "open": 0, "pnl": 50.0, "usd": 100.0, "est": 0}}
    con = Console(file=io.StringIO(), width=120)
    assert sr._rate_style(0.8, 0.925) == "red"
    assert sr._rate_style(1.0, 0.925) == "green"
    # and both still render without a break-even to measure against
    for series in (losing, winning):
        con.print(sr.series_table(series, breakeven=None))
    assert "█" in con.file.getvalue()


def test_series_table_rows_never_wrap():
    out = _render(sr.series_table(_sb()["series"], 0.925), width=80)
    body = [ln for ln in out.splitlines() if ln.strip()]
    assert len(body) == 3  # header + two series, no continuation lines


# ---------- effectiveness ----------

def test_effectiveness_table_pairs_every_number_with_what_it_means():
    out = _render(sr.effectiveness_table(_eff()))
    for label in ("$-weighted win rate", "break-even win rate", "profit factor",
                  "return on notional", "RoRC", "bankroll growth", "utilization"):
        assert label in out
    assert "92.5%" in out and "0.79" in out and "-24.69%/h" in out
    assert "$18,504" in out and "avg hold 5.7m" in out
    assert "over 215.4d" in out  # a 5169h span reported in days, not hours


def test_effectiveness_table_reports_a_short_span_in_hours():
    assert "over 6.2h" in _render(sr.effectiveness_table(_eff(span_h=6.2)))


def test_effectiveness_table_dashes_every_undefined_metric():
    empty = {"n": 0, "notional": 0.0, "pnl": 0.0, "win_rate": None,
             "mww_rate": None, "breakeven_win_rate": None, "profit_factor": None,
             "gross_win": 0.0, "gross_loss": 0.0, "return_on_notional": None,
             "rorc": None, "bgr": None, "utilization": None, "span_h": 0.0,
             "bankroll": None}
    out = _render(sr.effectiveness_table(empty))
    assert out.count("—") >= 5


# ---------- calibration ----------

def test_calibration_table_grades_each_bucket_against_its_own_stated_fair():
    out = _render(sr.calibration_table({0.90: [17, 13], 0.95: [837, 744]}))
    assert "0.90" in out and "13/17" in out and "76%" in out
    assert "744/837" in out and "89%" in out
    # 76% realized on a 0.90 stated fair is over-confidence, and says so in red
    assert sr._rate_style(13 / 17, 0.90) == "red"
    assert sr._rate_style(1.0, 0.95) == "green"


# ---------- live arms ----------

def test_arms_table_compacts_a_basis_guard_reason_instead_of_wrapping_it():
    out = _render(sr.arms_table(_arms()), width=100)
    assert "margin -4.9 vs 6.0bp" in out
    # the raw sentence is what used to blow the column and wrap the row
    assert "projected margin" not in out
    body = [ln for ln in out.splitlines() if ln.strip()]
    assert len(body) == 3  # header + two arms
    assert all(len(ln) <= 100 for ln in out.splitlines())


def test_arms_table_falls_back_to_the_legacy_reason_sentence():
    arms = {"btc-updown-5m-1787452500": {
        "eval": {"state": "gated",
                  "reason": "basis guard: projected margin +12.5bp inside 6.0bp"}}}
    assert "margin +12.5 vs 6.0bp" in _render(sr.arms_table(arms))


def test_arms_table_normalizes_float_dust_in_committed():
    out = _render(sr.arms_table(_arms()))
    assert "$0.00" in out and "$-0.00" not in out


def test_arms_table_renders_state_roll_and_fair():
    out = _render(sr.arms_table(_arms()))
    assert "armed" in out and "gated" in out
    assert "0.9601" in out
    assert "⟳" in out and "·" in out  # rolling arm and a one-shot one
    assert "btc 5m" in out  # slug rendered through updown_slugs.display


def test_arms_table_tolerates_a_missing_or_half_built_eval():
    # An engine restart mid-flight leaves last_eval None or partial.
    out = _render(sr.arms_table({"btc-updown-5m-1787452500": {"eval": None},
                                  "eth-updown-5m-1787452500": {}}))
    assert "?" in out and "—" in out


# ---------- the whole report ----------

def test_render_stats_lays_out_every_section():
    out = _render(sr.render_stats(_sb(), _eff(), {"total": 1649.14},
                                  {"arms": _arms(), "pending_rolls": ["btc 5m"]},
                                  1787452500))
    assert "updown fleet" in out and "windows since 08-23 02:35Z" in out
    for header in ("by series", "effectiveness", "calibration", "live arms"):
        assert header in out
    assert "pending rolls: btc 5m" in out
    # the header panel comes first, the sections after it, in reading order
    order = [out.index(h) for h in ("updown fleet", "by series", "effectiveness",
                                    "calibration", "live arms")]
    assert order == sorted(order)


def test_render_stats_drops_sections_that_have_no_data():
    out = _render(sr.render_stats({"wins": 0, "losses": 0, "net": 0.0, "rolls": 0},
                                  _eff(n=0), None, None, 0))
    for header in ("by series", "effectiveness", "calibration", "live arms"):
        assert header not in out
    assert "0W-0L" in out


def test_render_stats_survives_an_engine_that_never_answered():
    # status={} is what crypto_stats passes when engine.post() sys.exit()s.
    out = _render(sr.render_stats(_sb(), _eff(), {"total": 1649.14}, {}, 0))
    assert "live arms" not in out
    assert "committed $0.00" in out
    assert "147W-13L" in out
