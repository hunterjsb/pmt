"""Capital-effectiveness metrics — what the plain W-L count refuses to say.

The count win rate flatters structurally: bounded up (+2-8% of stake, buying
at 0.92-0.98 to collect $1), unbounded down (-100%), and blind to TIME — see
docs/LESSONS.md#L27.

So this module answers the question in two denominators, because "how
effective are we being with our money" is two questions wearing one coat:

  TRADE QUALITY   — denominator is capital actually at risk × hours it was
                    at risk. Idle capital and idle time are invisible; this
                    grades the trades, not the deployment.
                        -> return_on_risk_capital() ("RoRC")
  CAPITAL EFFECT. — denominator is the whole bankroll × calendar time. Idle
                    money and dark hours dilute it, on purpose; this grades
                    the operation.
                        -> bankroll_growth() ("BGR")

and utilization() is the bridge between them (BGR ~= RoRC x utilization to
first order, since ln(1+x) ~= x at these magnitudes).

Metric pedigree, deliberately borrowed rather than invented:
  - money_weighted_win_rate: the dollar-weighted analogue of a hit rate —
    same family as money-/dollar-weighted return, which weights outcomes by
    the capital actually exposed to them instead of counting episodes.
  - return_on_risk_capital: return per dollar-hour of exposure, the carry
    desk's "P&L per dollar-day of risk" / return on capital employed. It is
    time-weighted where a plain ROI is not.
  - bankroll_growth: E[ln(1 + r)] — the Kelly-native measure. Maximizing it
    IS maximizing long-run geometric growth, and it punishes -100% tails
    the way compounding actually does, which a mean return does not.
  - profit_factor: gross wins / gross losses, break-even at 1.00. The
    sharpest single antidote to a flattering win rate.

Everything here is pure (no network, no clock unless you hand it one) so
the math is unit-testable with inline fixtures; `pmt crypto stats` does the
I/O and calls in.

Window records
--------------
Each input `window` is a mapping with:
    notional  float  dollars put at risk (gross BUY usdc for the window)
    pnl       float  realized dollars for the window (redeem + sell - buy)
    won       bool   graded outcome
    entry_ts  float  notional-weighted BUY timestamp (0 if no buys)
    exit_ts   float  redeem timestamp, 0 if none posted
    end_ts    float  window settlement epoch
Extra keys are ignored. Windows still riding (ungraded) must NOT be passed:
their P&L isn't known, so including them would understate exposure-time or
invent a result.
"""

from __future__ import annotations

import math
from collections.abc import Sequence

HOURS_PER_YEAR = 8760.0
DAYS_PER_YEAR = 365.25
# A hold of zero would divide by zero; a same-second buy-and-redeem is not
# real anyway (settlement is at window end at the earliest).
MIN_HOLD_S = 1.0
# math.exp overflows past ~709. Growth rates this extreme are already
# "unbounded", so clamping the exponent costs no information.
_EXP_CAP = 700.0
# A window that lost more than the entire bankroll makes ln(1+r) undefined.
# Clamp rather than raise: the number is meant to be printed on a dashboard.
_LOG_FLOOR = 1e-9


def weighted_ts(ts_usd_sum: float, usd_sum: float) -> float:
    """Notional-weighted entry time from streaming accumulators: the caller
    adds `timestamp * usdcSize` and `usdcSize` per BUY row, this divides.

    Why weighted and not first-fill: a window is built from clips fired over
    minutes. Grading the whole notional from the first clip's timestamp
    over-counts exposure-time on the dollars that arrived late — the average
    dollar's entry is the honest start of its own hold.

    0.0 when nothing was bought (there is no entry time for no money).
    """
    return (ts_usd_sum / usd_sum) if usd_sum > 0 else 0.0


def hold_seconds(w: dict) -> float:
    """Seconds this window's capital was tied up.

    Entry is the notional-weighted buy time (money committed in clips, so
    the average dollar's start, not the first clip's). Exit is the redeem
    row's timestamp, floored at window end — capital cannot come back
    before settlement, and an unredeemed loss never comes back at all, so
    `end_ts` is the right release point for it.

    Returns 0.0 for a window with no buys (nothing was at risk).
    """
    entry = float(w.get("entry_ts") or 0.0)
    if entry <= 0:
        return 0.0
    exit_ts = max(float(w.get("exit_ts") or 0.0), float(w.get("end_ts") or 0.0))
    return max(MIN_HOLD_S, exit_ts - entry)


def dollar_hours(windows: Sequence[dict]) -> float:
    """Σ(dollars at risk × hours at risk) — the denominator of RoRC.

    This is the unit that makes a $10 five-minute fill and a $265
    fifteen-minute fill commensurable.
    """
    return sum(float(w.get("notional") or 0.0) * hold_seconds(w) / 3600.0
               for w in windows if float(w.get("notional") or 0.0) > 0)


def money_weighted_win_rate(windows: Sequence[dict]) -> float | None:
    """Fraction of DOLLARS at risk that landed on winning windows (0..1).

    The literal risk-weighted win rate: same shape as the count win rate,
    but a $265 bet counts 26x a $10 bet. None when nothing was at risk.

    Note what it still does NOT know: the payoff asymmetry. A 90% money-
    weighted win rate is comfortably losing when wins pay 5% and losses pay
    -100% — that is what profit_factor() is for.
    """
    total = sum(float(w.get("notional") or 0.0) for w in windows)
    if total <= 0:
        return None
    won = sum(float(w.get("notional") or 0.0) for w in windows if w.get("won"))
    return won / total


def win_rate(windows: Sequence[dict]) -> float | None:
    """Plain count win rate (0..1) — kept here only so the flattering number
    and its corrections are computed from the identical window set."""
    if not windows:
        return None
    return sum(1 for w in windows if w.get("won")) / len(windows)


def by_settlement(windows: Sequence[dict]) -> list[dict]:
    """Windows in settlement order — THE order, so every consumer counting a
    run counts the same one.

    Sorted by `end_ts` and then by `slug`, and the slug is load-bearing rather
    than cosmetic. Whole series settle on the same :00/:05 boundary, so the
    key is massively tied: 170 tied groups covering 460 of 590 windows live on
    2026-08-24. Python's sort is stable, so a tie fell through to the caller's
    order — which for the scoreboard is `win_by_slug` insertion order, i.e.
    the order data-api's pages happened to return the rows in. wallet.py's own
    docstring is that those pages seam-shift and mutate between walks.

    The number that came out was therefore not reproducible: shuffling only
    within ties, `streak(...)["longest"]` flipped 107 <-> 108 on roughly half
    of 400 draws, and `pmt crypto stats` printed "best 107" as a coin flip.
    The journal is worse off still — it stamps `streak:{n}:{slug}` into a
    permanent file, so a re-ordered walk names a different window for the same
    crossing and writes the milestone a second time.

    Within one settlement second the true order is genuinely unknown, so no
    tiebreak is more correct than another; what matters is that one is FIXED,
    and the slug is the only stable identity a window has.
    """
    return sorted(windows, key=lambda w: (float(w.get("end_ts") or 0.0),
                                           str(w.get("slug") or "")))


def streak(windows: Sequence[dict]) -> dict:
    """{"current", "longest"} consecutive wins, ordered by settlement.

    A payoff shape this lopsided (+2-8% up, -100% down) makes the run
    length between losses the operator's real pulse: the book is only ever
    as good as the streak the next loss interrupts. `current` counts back
    from the newest settled window, so a loss most recently makes it 0 —
    which is the honest answer, not a hidden number.

    Ordered by `by_settlement` rather than trusting caller order: the
    scoreboard builds its window list from a dict, so arrival order is
    insertion order, not time — and see that function for why the tiebreak
    inside a settlement second decides the printed number.
    """
    ordered = by_settlement(windows)
    longest = run = 0
    for w in ordered:
        run = run + 1 if w.get("won") else 0
        longest = max(longest, run)
    return {"current": run, "longest": longest}


def gross_win_loss(windows: Sequence[dict]) -> tuple[float, float]:
    """(gross winning dollars, gross losing dollars as a positive number)."""
    gw = sum(float(w["pnl"]) for w in windows if float(w.get("pnl") or 0.0) > 0)
    gl = -sum(float(w["pnl"]) for w in windows if float(w.get("pnl") or 0.0) < 0)
    return gw, gl


def profit_factor(windows: Sequence[dict]) -> float | None:
    """Gross wins / gross losses. 1.00 is break-even; below 1.00 the book
    loses money no matter how high the win rate reads. None when there are
    no losses yet (undefined, not infinite — don't print a fake number)."""
    gw, gl = gross_win_loss(windows)
    if gl <= 0:
        return None
    return gw / gl


def breakeven_win_rate(windows: Sequence[dict]) -> float | None:
    """The win rate THIS payoff structure needs just to break even:
    avg_loss / (avg_win + avg_loss).

    The classic required-win-rate identity from R-multiple analysis, and the
    one number that makes a 92% headline finally mean something — it says
    what 92% has to beat. With wins paying a few percent of stake and losses
    paying -100%, the bar sits in the nineties, which is exactly why a win
    rate that "looks incredible" can still be a losing book.

    None until there is at least one win AND one loss to size the payoff.
    """
    gw, gl = gross_win_loss(windows)
    n_win = sum(1 for w in windows if float(w.get("pnl") or 0.0) > 0)
    n_loss = sum(1 for w in windows if float(w.get("pnl") or 0.0) < 0)
    if n_win == 0 or n_loss == 0:
        return None
    avg_win, avg_loss = gw / n_win, gl / n_loss
    if avg_win + avg_loss <= 0:
        return None
    return avg_loss / (avg_win + avg_loss)


def return_on_notional(windows: Sequence[dict]) -> float | None:
    """Total P&L / total dollars put at risk — cents earned per dollar
    traded, ignoring time. The time-free ancestor of RoRC; useful because
    it is the one number that survives any argument about hold times."""
    total = sum(float(w.get("notional") or 0.0) for w in windows)
    if total <= 0:
        return None
    return sum(float(w.get("pnl") or 0.0) for w in windows) / total


def return_on_risk_capital(windows: Sequence[dict]) -> dict | None:
    """TRADE QUALITY: return per dollar-hour of capital actually at risk.

    P&L / Σ(notional × hours). Answers "while our money was working, how
    hard was it working" — idle capital and idle time are deliberately
    outside the denominator.

    Returns {"per_hour", "per_day", "annualized", "dollar_hours",
    "avg_hold_h"} as fractions (0.01 = 1%), or None with no exposure.

    `annualized` is the SIMPLE extrapolation (×8760): the hypothetical of
    keeping one dollar continuously at risk for a year. It is offered for
    scale only — compounding an intermittently-deployed rate out to a year
    manufactures precision the deployment schedule doesn't have. The
    annualized number that is honest about calendar time is BGR, below.
    """
    dh = dollar_hours(windows)
    if dh <= 0:
        return None
    pnl = sum(float(w.get("pnl") or 0.0) for w in windows)
    notional = sum(float(w.get("notional") or 0.0) for w in windows)
    per_hour = pnl / dh
    return {
        "per_hour": per_hour,
        "per_day": per_hour * 24.0,
        "annualized": per_hour * HOURS_PER_YEAR,
        "dollar_hours": dh,
        "avg_hold_h": (dh / notional) if notional > 0 else None,
    }


def calendar_span_s(windows: Sequence[dict], now: float | None = None) -> float:
    """Wall-clock seconds the operation has been running: first dollar
    committed to `now` (or to the last capital release if `now` is absent).

    Ending at `now` is the point — a fleet that has been dark for two days
    should see its capital effectiveness decay, because the money genuinely
    was not working.
    """
    entries = [float(w.get("entry_ts") or 0.0) for w in windows]
    entries = [t for t in entries if t > 0]
    if not entries:
        return 0.0
    start = min(entries)
    last = max(max(float(w.get("exit_ts") or 0.0), float(w.get("end_ts") or 0.0))
               for w in windows)
    end = now if now is not None else last
    return max(0.0, end - start)


def utilization(windows: Sequence[dict], bankroll: float | None,
                 span_s: float) -> float | None:
    """Bridge metric: dollar-hours at risk / (bankroll × calendar hours).

    The duty cycle of the money. 1.0 means every dollar was at risk every
    hour; 0.05 means 95% of the capital-time was idle. This is what turns a
    respectable RoRC into a thin BGR.
    """
    if not bankroll or bankroll <= 0 or span_s <= 0:
        return None
    return dollar_hours(windows) / (bankroll * span_s / 3600.0)


def bankroll_growth(windows: Sequence[dict], bankroll: float | None,
                     span_s: float) -> dict | None:
    """CAPITAL EFFECTIVENESS: Kelly-native log growth of the whole bankroll
    over calendar time.

    g = Σ ln(1 + pnl_i / B), the exact quantity Kelly sizing maximizes: the
    long-run geometric growth rate. Two properties earn it the primary
    slot over a mean return: it compounds (three +1% windows and one -3%
    window is NOT flat), and it punishes deep drawdowns superlinearly,
    which is precisely the -100%-tail risk this payoff structure carries.

    B is held constant at the CURRENT bankroll rather than reconstructed
    per window — we don't keep a balance history. That biases the number
    toward the present size of the book; over a period where the bankroll
    moved a lot, read it as directional, not exact.

    Returns {"log_total", "per_day", "per_day_pct", "annual_pct", "span_h"}
    or None without a bankroll. `*_pct` are simple-equivalent percentages
    (exp(g)-1), so they are comparable to a quoted rate of return.

    `annual_pct` is None on spans under a day. Compounding a three-hour
    sample out to a year produces numbers like 1e60 — arithmetically real,
    informationally worthless, and the kind of figure that ends up quoted.
    """
    if not bankroll or bankroll <= 0 or span_s <= 0:
        return None
    g = 0.0
    for w in windows:
        r = float(w.get("pnl") or 0.0) / bankroll
        g += math.log(max(1.0 + r, _LOG_FLOOR))
    days = span_s / 86400.0
    per_day = g / days
    return {
        "log_total": g,
        "per_day": per_day,
        "per_day_pct": _pct_from_log(per_day),
        "annual_pct": _pct_from_log(per_day * DAYS_PER_YEAR) if days >= 1.0 else None,
        "span_h": span_s / 3600.0,
    }


def _pct_from_log(g: float) -> float:
    """exp(g)-1 as a percentage, overflow-clamped."""
    return (math.exp(min(g, _EXP_CAP)) - 1.0) * 100.0


def summary(windows: Sequence[dict], *, bankroll: float | None = None,
            now: float | None = None) -> dict:
    """Every metric above over one graded window set, in one pass-safe dict.

    Keys are None where the inputs can't support them (no exposure, no
    bankroll, no losses yet) — callers print a dash, never a zero, because
    "undefined" and "zero" mean opposite things on a scoreboard.
    """
    windows = list(windows)
    span_s = calendar_span_s(windows, now)
    rorc = return_on_risk_capital(windows)
    bgr = bankroll_growth(windows, bankroll, span_s)
    gw, gl = gross_win_loss(windows)
    return {
        "n": len(windows),
        "notional": sum(float(w.get("notional") or 0.0) for w in windows),
        "pnl": sum(float(w.get("pnl") or 0.0) for w in windows),
        "win_rate": win_rate(windows),
        "mww_rate": money_weighted_win_rate(windows),
        "breakeven_win_rate": breakeven_win_rate(windows),
        "profit_factor": profit_factor(windows),
        "gross_win": gw,
        "gross_loss": gl,
        "return_on_notional": return_on_notional(windows),
        "rorc": rorc,
        "bgr": bgr,
        "utilization": utilization(windows, bankroll, span_s),
        "span_h": span_s / 3600.0,
        "bankroll": bankroll,
        "streak": streak(windows),
    }
