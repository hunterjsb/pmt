"""Pure-math tests for the capital-effectiveness metrics.

The fixtures below are deliberately shaped like the real updown book —
many small wins (+2-8% of stake) against rare total losses (-100%) — because
the whole reason this module exists is that a metric can look excellent on
that shape while the account bleeds. Several tests pin exactly that: a 90%+
win rate sitting next to a sub-1.00 profit factor and a negative growth rate.
"""

from __future__ import annotations

import math

import pytest

import cli_crypto as cc
from polymarket import effectiveness as eff


def _w(notional, pnl, won, entry_ts=1000.0, exit_ts=0.0, end_ts=1900.0, slug="btc-updown-15m-1000"):
    """One graded window record, positional-friendly for terse fixtures."""
    return {"slug": slug, "notional": notional, "pnl": pnl, "won": won,
            "entry_ts": entry_ts, "exit_ts": exit_ts, "end_ts": end_ts}


# ---------- entry timing ----------

def test_weighted_ts_is_the_average_dollars_entry():
    # $100 in at t=0 and $300 in at t=100 -> the average dollar entered at t=75
    assert eff.weighted_ts(0 * 100 + 100 * 300, 400) == 75.0


def test_weighted_ts_zero_when_nothing_was_bought():
    assert eff.weighted_ts(0.0, 0.0) == 0.0


# ---------- hold time ----------

def test_hold_seconds_runs_entry_to_redeem():
    w = _w(100, 5, True, entry_ts=1000, exit_ts=2000, end_ts=1900)
    assert eff.hold_seconds(w) == 1000.0


def test_hold_seconds_floors_exit_at_window_end():
    # Capital cannot come back before settlement; an unredeemed loss (exit 0)
    # is tied up until the window ends and then is simply gone.
    w = _w(100, -100, False, entry_ts=1000, exit_ts=0.0, end_ts=1900)
    assert eff.hold_seconds(w) == 900.0


def test_hold_seconds_zero_without_buys():
    assert eff.hold_seconds(_w(0, 3, True, entry_ts=0.0)) == 0.0


def test_hold_seconds_never_zero_for_a_real_position():
    w = _w(100, 5, True, entry_ts=1900, exit_ts=1900, end_ts=1900)
    assert eff.hold_seconds(w) == eff.MIN_HOLD_S


# ---------- dollar-hours ----------

def test_dollar_hours_makes_size_and_duration_commensurable():
    # $10 held 1h and $265 held 0.25h
    ws = [_w(10, 0.5, True, entry_ts=3600, exit_ts=7200, end_ts=7200),
          _w(265, -265, False, entry_ts=3600, exit_ts=0, end_ts=4500)]
    assert eff.dollar_hours(ws) == pytest.approx(10 * 1.0 + 265 * 0.25)


def test_dollar_hours_skips_zero_notional_windows():
    assert eff.dollar_hours([_w(0, 4, True, entry_ts=3600, end_ts=7200)]) == 0.0


# ---------- the win-rate corrections ----------

def test_money_weighted_win_rate_collapses_a_flattering_count():
    # 11 x $10 wins, 1 x $265 loss: 92% of the tally marks, 29% of the money.
    ws = [_w(10, 0.5, True) for _ in range(11)] + [_w(265, -265, False)]
    assert eff.win_rate(ws) == pytest.approx(11 / 12)
    assert eff.money_weighted_win_rate(ws) == pytest.approx(110 / 375)


def test_money_weighted_win_rate_none_without_exposure():
    assert eff.money_weighted_win_rate([]) is None
    assert eff.money_weighted_win_rate([_w(0, 0, True)]) is None


def test_win_rate_none_on_empty():
    assert eff.win_rate([]) is None


def test_profit_factor_below_one_on_the_real_payoff_shape():
    # The structural trap: wins pay +5% of stake, a loss pays -100%.
    ws = [_w(100, 5.0, True) for _ in range(11)] + [_w(100, -100.0, False)]
    assert eff.win_rate(ws) == pytest.approx(11 / 12)      # 92% "win rate"
    assert eff.profit_factor(ws) == pytest.approx(55 / 100)  # and it loses money
    assert eff.gross_win_loss(ws) == (pytest.approx(55.0), pytest.approx(100.0))


def test_profit_factor_undefined_without_losses():
    # Undefined, not infinite — a dashboard must print a dash, not a number.
    assert eff.profit_factor([_w(100, 5, True)]) is None


def test_breakeven_win_rate_names_the_bar_the_headline_must_clear():
    # Wins pay $5, losses pay $100 -> you need 100/105 = 95.2% to stay flat.
    ws = [_w(100, 5.0, True) for _ in range(11)] + [_w(100, -100.0, False)]
    be = eff.breakeven_win_rate(ws)
    assert be == pytest.approx(100 / 105)
    assert eff.win_rate(ws) < be          # 91.7% actual vs 95.2% required
    assert eff.profit_factor(ws) < 1.0    # and the two agree, as they must


def test_breakeven_win_rate_agrees_with_profit_factor_at_the_boundary():
    # 20 wins of $5 against 1 loss of $100: exactly break-even.
    ws = [_w(100, 5.0, True) for _ in range(20)] + [_w(100, -100.0, False)]
    assert eff.win_rate(ws) == pytest.approx(eff.breakeven_win_rate(ws))
    assert eff.profit_factor(ws) == pytest.approx(1.0)


def test_breakeven_win_rate_undefined_until_both_sides_exist():
    assert eff.breakeven_win_rate([_w(100, 5.0, True)]) is None
    assert eff.breakeven_win_rate([_w(100, -100.0, False)]) is None


def test_return_on_notional_is_cents_per_dollar_risked():
    ws = [_w(100, 5.0, True), _w(100, -100.0, False)]
    assert eff.return_on_notional(ws) == pytest.approx(-95 / 200)


def test_return_on_notional_none_without_exposure():
    assert eff.return_on_notional([]) is None


# ---------- RoRC: trade quality ----------

def test_rorc_is_pnl_per_dollar_hour_at_risk():
    ws = [_w(100, 5.0, True, entry_ts=3600, exit_ts=7200, end_ts=7200)]  # 100 $-hours
    r = eff.return_on_risk_capital(ws)
    assert r["dollar_hours"] == pytest.approx(100.0)
    assert r["per_hour"] == pytest.approx(0.05)
    assert r["per_day"] == pytest.approx(0.05 * 24)
    assert r["annualized"] == pytest.approx(0.05 * eff.HOURS_PER_YEAR)
    assert r["avg_hold_h"] == pytest.approx(1.0)


def test_rorc_halves_when_the_same_edge_takes_twice_as_long():
    fast = [_w(100, 5.0, True, entry_ts=3600, exit_ts=5400, end_ts=5400)]
    slow = [_w(100, 5.0, True, entry_ts=3600, exit_ts=7200, end_ts=7200)]
    assert (eff.return_on_risk_capital(slow)["per_hour"]
            == pytest.approx(eff.return_on_risk_capital(fast)["per_hour"] / 2))


def test_rorc_none_without_any_exposure():
    assert eff.return_on_risk_capital([]) is None
    assert eff.return_on_risk_capital([_w(0, 1, True, entry_ts=3600)]) is None


# ---------- calendar span, utilization ----------

def test_calendar_span_runs_first_entry_to_now():
    ws = [_w(100, 5, True, entry_ts=1000, exit_ts=2000, end_ts=1900)]
    assert eff.calendar_span_s(ws, now=11000) == 10000.0


def test_calendar_span_falls_back_to_last_release_without_a_clock():
    ws = [_w(100, 5, True, entry_ts=1000, exit_ts=2000, end_ts=1900),
          _w(100, -100, False, entry_ts=1500, exit_ts=0, end_ts=2400)]
    assert eff.calendar_span_s(ws) == 1400.0  # 1000 -> 2400


def test_calendar_span_zero_without_entries():
    assert eff.calendar_span_s([]) == 0.0


def test_utilization_is_the_duty_cycle_of_the_money():
    # $100 at risk for 1h out of a $1000 bankroll over a 10h span -> 1%
    ws = [_w(100, 5, True, entry_ts=3600, exit_ts=7200, end_ts=7200)]
    assert eff.utilization(ws, 1000.0, 36000.0) == pytest.approx(0.01)


def test_utilization_none_without_a_bankroll_or_span():
    ws = [_w(100, 5, True, entry_ts=3600, exit_ts=7200, end_ts=7200)]
    assert eff.utilization(ws, None, 3600.0) is None
    assert eff.utilization(ws, 0.0, 3600.0) is None
    assert eff.utilization(ws, 1000.0, 0.0) is None


# ---------- BGR: capital effectiveness ----------

def test_bankroll_growth_compounds_rather_than_averaging():
    # +1%, +1%, -3% on a $1000 book: the arithmetic mean says -0.33%/window,
    # but the multiplicative truth is worse — that gap IS the metric.
    ws = [_w(100, 10.0, True), _w(100, 10.0, True), _w(100, -30.0, False)]
    g = eff.bankroll_growth(ws, 1000.0, 86400.0)
    expected = math.log(1.01) * 2 + math.log(0.97)
    assert g["log_total"] == pytest.approx(expected)
    assert g["per_day"] == pytest.approx(expected)          # span is exactly a day
    assert g["per_day_pct"] == pytest.approx((math.exp(expected) - 1) * 100)
    assert g["per_day_pct"] < -1.0                          # worse than the naive sum
    assert g["span_h"] == pytest.approx(24.0)


def test_bankroll_growth_punishes_a_deep_loss_harder_than_the_mean_does():
    # Same total P&L (-$10), different shape: one -$10 hit vs ten -$1 hits.
    spread = [_w(100, -1.0, False) for _ in range(10)]
    concentrated = [_w(100, -10.0, False)]
    B, span = 100.0, 86400.0
    assert (eff.bankroll_growth(concentrated, B, span)["log_total"]
            < eff.bankroll_growth(spread, B, span)["log_total"])


def test_bankroll_growth_survives_a_total_wipeout():
    # 1 + r <= 0 is undefined; clamped rather than raised, because this
    # number gets printed on a live dashboard.
    g = eff.bankroll_growth([_w(500, -500.0, False)], 400.0, 86400.0)
    assert g["log_total"] < 0
    assert math.isfinite(g["log_total"])
    assert g["annual_pct"] == pytest.approx(-100.0)


def test_bankroll_growth_refuses_to_annualize_a_sub_day_sample():
    # A 3h sample compounded to a year is a 1e60 number. Withheld, not printed.
    g = eff.bankroll_growth([_w(100, 50.0, True)], 1000.0, 3 * 3600.0)
    assert g["annual_pct"] is None
    assert g["per_day_pct"] is not None      # same-day extrapolation still shown


def test_bankroll_growth_annual_pct_does_not_overflow_on_a_long_hot_span():
    ws = [_w(100, 900.0, True) for _ in range(50)]
    g = eff.bankroll_growth(ws, 1000.0, 2 * 86400.0)
    assert math.isfinite(g["annual_pct"])


def test_bankroll_growth_none_without_a_bankroll():
    assert eff.bankroll_growth([_w(100, 5, True)], None, 86400.0) is None
    assert eff.bankroll_growth([_w(100, 5, True)], 1000.0, 0.0) is None


def test_bgr_approximates_rorc_times_utilization():
    # The bridge identity, to first order (ln(1+x) ~= x at these sizes).
    ws = [_w(100, 2.0, True, entry_ts=3600, exit_ts=7200, end_ts=7200),
          _w(100, 3.0, True, entry_ts=10800, exit_ts=14400, end_ts=14400)]
    s = eff.summary(ws, bankroll=5000.0, now=36000.0)
    lhs = s["bgr"]["per_day"] / 24.0                       # growth per calendar hour
    rhs = s["rorc"]["per_hour"] * s["utilization"]
    assert lhs == pytest.approx(rhs, rel=0.01)


# ---------- summary + header ----------

def test_summary_is_none_safe_on_an_empty_book():
    s = eff.summary([])
    assert s["n"] == 0 and s["pnl"] == 0.0
    for k in ("win_rate", "mww_rate", "breakeven_win_rate", "profit_factor",
              "return_on_notional", "rorc", "bgr", "utilization"):
        assert s[k] is None


def test_summary_reports_the_whole_story_on_the_real_shape():
    # The live shape that motivated the module, in one window set
    # (docs/LESSONS.md#L27): 11 wins of +$0.50 on $10 clips, one -$265 window.
    ws = [_w(10, 0.5, True, entry_ts=3600, exit_ts=4500, end_ts=4500) for _ in range(11)]
    ws.append(_w(265, -265.0, False, entry_ts=3600, exit_ts=0, end_ts=4500))
    s = eff.summary(ws, bankroll=1000.0, now=90000.0)
    assert s["n"] == 12
    assert s["win_rate"] == pytest.approx(11 / 12)     # 92%: the flattering read
    assert s["mww_rate"] < 0.30                        # 29% of the money won
    assert s["profit_factor"] < 0.03                   # $5.50 won vs $265 lost
    assert s["pnl"] == pytest.approx(-259.5)
    assert s["return_on_notional"] < 0
    assert s["rorc"]["per_hour"] < 0
    assert s["bgr"]["per_day_pct"] < 0
    assert 0 < s["utilization"] < 1
    assert s["bankroll"] == 1000.0


def test_header_line_carries_every_headline_number():
    ws = [_w(10, 0.5, True, entry_ts=3600, exit_ts=4500, end_ts=4500) for _ in range(11)]
    ws.append(_w(265, -265.0, False, entry_ts=3600, exit_ts=0, end_ts=4500))
    line = eff.header_line(eff.summary(ws, bankroll=1000.0, now=90000.0))
    for token in ("$W ", "W 92%", "need ", "PF ", "$ret ", "RoRC ", "growth ", "util "):
        assert token in line
    assert "$W 29%" in line       # money-weighted win rate beside the count's 92%
    # "need" is a fraction-of-trades bar, so it must sit on the COUNT rate
    assert "W 92% (need" in line


def test_header_line_dashes_undefined_metrics_instead_of_zeroing_them():
    line = eff.header_line(eff.summary([]))
    assert "PF —" in line and "$W —" in line
    assert "growth" not in line and "util" not in line  # no bankroll -> omitted


# ---------- wiring: cli_crypto -> effectiveness ----------
#
# These live here rather than in test_cli_crypto.py so the whole metric —
# accumulation, exposure timing, and the bankroll denominator — is pinned in
# one file.

def _no_tape_no_gamma(monkeypatch):
    monkeypatch.setattr(cc.tape, "iter_records", lambda *a, **k: iter([]))
    monkeypatch.setattr(cc, "_gamma_resolution_cached", lambda slug: None)


def test_score_activity_records_notional_weighted_entry_and_exit(monkeypatch):
    _no_tape_no_gamma(monkeypatch)
    start = 1_787_000_000
    slug = f"btc-updown-15m-{start}"
    rows = [
        {"type": "TRADE", "side": "BUY", "usdcSize": 100.0, "size": 105.0,
         "slug": slug, "timestamp": start + 100},
        {"type": "TRADE", "side": "BUY", "usdcSize": 300.0, "size": 315.0,
         "slug": slug, "timestamp": start + 200},
        {"type": "REDEEM", "usdcSize": 420.0, "outcome": "up",
         "slug": slug, "timestamp": start + 1000},
    ]
    (w,) = cc.score_activity(rows, 0.0)["eff_windows"]
    assert w["notional"] == pytest.approx(400.0)
    assert w["entry_ts"] == pytest.approx(start + 175)   # the average dollar's entry
    assert w["exit_ts"] == start + 1000
    assert eff.hold_seconds(w) == pytest.approx(825.0)


def test_score_activity_unredeemed_loss_holds_capital_to_window_end(monkeypatch):
    _no_tape_no_gamma(monkeypatch)
    start = 1_787_000_000
    slug = f"eth-updown-5m-{start}"
    rows = [
        {"type": "TRADE", "side": "BUY", "usdcSize": 50.0, "size": 53.0,
         "slug": slug, "timestamp": start + 60},
        {"type": "REDEEM", "usdcSize": 0.0, "size": 53.0, "outcome": "up",
         "slug": slug, "timestamp": start + 400},
    ]
    (w,) = cc.score_activity(rows, 0.0)["eff_windows"]
    assert w["won"] is False and w["exit_ts"] == start + 400
    assert eff.hold_seconds(w) == pytest.approx(340.0)


def test_effectiveness_summary_counts_riding_capital_in_the_bankroll():
    # Free USDC alone understates the book while windows are still in flight.
    sb = {"eff_windows": [_w(100, -5.0, False, entry_ts=3600, exit_ts=7200, end_ts=7200)],
          "riding_usd": 400.0}
    s = cc.effectiveness_summary(sb, {"total": 600.0})
    assert s["bankroll"] == pytest.approx(1000.0)


def test_effectiveness_summary_without_a_balance_leaves_bankroll_metrics_undefined():
    sb = {"eff_windows": [_w(100, -5.0, False, entry_ts=3600, exit_ts=7200, end_ts=7200)],
          "riding_usd": 0.0}
    s = cc.effectiveness_summary(sb, None)
    assert s["bankroll"] is None and s["bgr"] is None and s["utilization"] is None
    assert s["rorc"] is not None  # trade quality needs no bankroll


def test_eff_table_renders_dashes_on_an_empty_book():
    import io

    from rich.console import Console
    con = Console(file=io.StringIO(), width=200)
    con.print(cc._eff_table(eff.summary([])))
    assert "—" in con.file.getvalue()
