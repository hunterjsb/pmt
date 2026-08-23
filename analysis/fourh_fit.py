#!/usr/bin/env python3
"""4h up/down tier fit — TAIL-HARVEST arming study (ROADMAP.md Phase 3.2 + R9).

RESULT (2026-08-23, see analysis/fourh_fit.md): the tail-harvest premise does
NOT hold, because eval_model's twap branch prices the wrong quantity. These
markets settle on the Chainlink 60s TWAP at the range END vs the same stream at
the range start, not on the average over the range — verified on 3,193 resolved
windows by analysis/settlement_rule_check.py. There is therefore no "banked"
mass, nothing is banked-decided until the final minute, and every safety/cushion
number this script computes describes a quantity that decides nothing.

The script is kept, and extended, because it is the measurement that found it:
every table below is now reported against BOTH definitions — "win%" is the
range-average rule the model believes, "REAL win%" is the rule that actually
settles (window_winner_terminal). `terminal_state()` is the correctly specified
model, for whoever re-specifies eval_model.

Phase 3.2's tail-snipe extension: arm the `*-updown-4h-*` series and let the
R9 theta gate (first clip needs side-signed |banked|/cushion >= theta) do the
work — a 4h arm only ever commits when the window is genuinely banked-decided.
This script measures whether that trade exists, using the model's OWN math
(pmengine/src/strategies/updown_model.rs::eval_model, transcribed 1:1 the same
way analysis/r6_tail_flip_study.py does it) replayed over >=21d of real
Binance 1m klines.

It is pure measurement. It never arms, disarms, or talks to the engine; it
reads ~/.pmt/corpus/klines-1m-*.jsonl (+ chainlink-*.jsonl for the basis leg)
and prints a report.

What it answers:
  Q1 EVIDENCE TIMELINE  — when does safety first cross theta in a 4h window,
      how often does a window ever get there, and what does that imply for
      fires/day/arm at theta 0.3 / 0.5 / 1.0.
  Q2 FLIP SAFETY        — conditional flip rate by safety bucket x rem bucket
      at 4h (r6's method, 4h-scaled rem buckets), a direct test of the
      sigma*sqrt(T/3) residual scaling at multi-hour horizons, and the final
      |margin| distribution (4h vs 15m vs 5m) that decides how binding the
      basis guard actually is.
  Q4 FLEET INTERACTION  — how often a decided 4h window's side agrees with the
      concurrently-decided 15m/5m window of the same symbol (rho ~ 1 exposure).

Model transcription notes (same simplifications as r6_tail_flip_study.py):
  - "spot" is m(current_minute) = (o+c)/2 of the in-progress minute.
  - the arm-time sigma_bp_per_min fallback is approximated by the 12m fast
    window (what vol_floor_bp degrades toward).
  - banked lag is a FLAT now-30s for every duration, exactly as the live code
    does it today (R9's sub-A/B proposes making it duration-aware; this study
    measures the code as deployed).
  - settlement = the 60s-TWAP regime (post 2026-08-07): the window's winner is
    the mean of its per-minute marks vs the range-start reference mark.

Run: cd pmtrader && uv run python ../analysis/fourh_fit.py
     cd pmtrader && uv run python ../analysis/fourh_fit.py --no-fetch
     cd pmtrader && uv run python ../analysis/fourh_fit.py --symbols BTCUSDT --days 21
"""
from __future__ import annotations

import argparse
import bisect
import json
import math
import statistics
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import r6_tail_flip_study as r6  # noqa: E402  (needs sys.path patch above)

CORPUS_DIR = Path.home() / ".pmt" / "corpus"

SYMBOLS = ["BTCUSDT", "ETHUSDT", "SOLUSDT", "XRPUSDT", "DOGEUSDT", "BNBUSDT"]

# Basis guards. btc/eth/sol are the live per-arm values (ROADMAP R1 verdicts).
GUARD_BP = {"BTCUSDT": 6.0, "ETHUSDT": 8.0, "SOLUSDT": 10.0,
            "XRPUSDT": 17.0, "DOGEUSDT": 14.0, "BNBUSDT": 8.0}
# Guards not backed by a live-arm decision: xrp/doge/bnb are set to the R1
# settlement-shaped p95 (rounded up) purely so this study can ask "what WOULD
# a 4h arm look like there". bnb's oracle corpus is only 50h old (measured
# 2026-08-23 in the parallel R1 pass).
GUARD_PROVISIONAL = {"XRPUSDT", "DOGEUSDT", "BNBUSDT"}

# R1 aligned, settlement-shaped p95 |basis| (60s-TWAP variant = the 15m/4h
# settlement shape), bp. Used to ask "how binding is settlement error at this
# duration's typical margin".
# (2026-08-23 re-measurement, 50h: btc 7.63 eth 7.68 sol 8.73 xrp 16.79
# doge 13.55 bnb 7.43 — bnb is BTC-class, the tightest alt basis measured.)
BASIS_P95_BP = {"BTCUSDT": 7.6, "ETHUSDT": 7.7, "SOLUSDT": 8.7,
                "XRPUSDT": 16.8, "DOGEUSDT": 13.6, "BNBUSDT": 7.4}

# Chainlink corpus symbol keys (short names) for the measured-basis section.
CK_SHORT = {"BTCUSDT": "btc", "ETHUSDT": "eth", "SOLUSDT": "sol",
            "XRPUSDT": "xrp", "DOGEUSDT": "doge", "BNBUSDT": "bnb"}

DURATIONS = [("5m", 300), ("15m", 900), ("4h", 14400)]
WINDOWS_PER_DAY = {"5m": 288, "15m": 96, "4h": 6}
THETAS = [0.3, 0.5, 0.75, 1.0, 1.5]
# Tail-harvest variants: only allow the first clip inside the last N seconds
# of the window (the `--min-elapsed` knob, expressed as absolute time left so
# it reads the same way the late-budget unlock does).
LATE_CAPS = [3600, 1800]
MANIP_PUSH_BP = 25.0       # updown.rs d_manip_push()
STEP_S = 15
FLEET_STEP_S = 60          # coarser grid for the cross-duration join

# rem buckets scaled for a 4h window (r6's stop at 600s, which is 4% of a 4h
# window and would hide the entire harvest region).
REM_BUCKETS_4H = [("0-60s", 0, 60), ("1-5m", 60, 300), ("5-15m", 300, 900),
                  ("15-30m", 900, 1800), ("30-60m", 1800, 3600),
                  ("1-2h", 3600, 7200), ("2h+", 7200, float("inf"))]
SAFETY_BUCKETS = r6.SAFETY_BUCKETS


def norm_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def pct(vals: list[float], q: float) -> float:
    if not vals:
        return float("nan")
    s = sorted(vals)
    i = min(int(q * (len(s) - 1) + 0.5), len(s) - 1)
    return s[i]


# ---------------------------------------------------------------------------
# the model, tick by tick — a transcription of eval_model()
# ---------------------------------------------------------------------------

def tick_state(series: dict, guard_bp: float, start: int, end: int, t_now: float) -> dict | None:
    """One eval_model() read at t_now. None when the tick is unevaluable
    (before the first banked minute / after the window). `gate_ok` is False
    exactly where eval_model returns its basis-guard Err."""
    idx = series["t_to_idx"]
    marks = series["marks"]
    current_minute = int(t_now // 60) * 60
    j = idx.get(current_minute)
    if j is None:
        return None
    spot = marks[j]
    ref_px = marks[idx[start - 60]]

    rem = end - t_now
    if rem <= 0:
        return None
    cutoff = min(t_now - r6.BANKED_LAG_S, end)
    b_cnt, banked_avg = r6.range_avg(series, start, cutoff)
    if b_cnt == 0 or banked_avg is None:
        banked_avg, banked_s = spot, 0.0
    else:
        banked_s = b_cnt * 60.0
    window = banked_s + rem
    if window <= 0:
        return None

    proj = (banked_avg * banked_s + spot * rem) / window
    margin_bp = (proj / ref_px - 1.0) * 1e4
    banked_margin_bp = (banked_avg / ref_px - 1.0) * 1e4 * (banked_s / window)
    sig_bp = r6.sigma_bp_at(series, j)
    rem_term = math.sqrt(max(rem / 60.0, 0.02) / 3.0)
    cushion_bp = guard_bp + sig_bp * rem_term * (rem / window)

    breakeven = (ref_px * window - banked_avg * banked_s) / rem
    sig_avg = (sig_bp / 1e4) * rem_term
    if sig_avg > 1e-12 and breakeven > 0 and spot > 0:
        p_up = 1.0 - norm_cdf(math.log(breakeven / spot) / sig_avg)
    else:
        p_up = 1.0 if breakeven < spot else (0.0 if breakeven > spot else 0.5)

    side = "up" if p_up >= 0.5 else "down"
    signed = banked_margin_bp if side == "up" else -banked_margin_bp
    safety = signed / max(cushion_bp, 1e-9)      # side-signed, exactly side_safety()
    return {
        "t": t_now, "rem": rem, "spot": spot, "ref": ref_px, "sig_bp": sig_bp,
        "margin_bp": margin_bp, "banked_margin_bp": banked_margin_bp,
        "cushion_bp": cushion_bp, "p_up": p_up, "side": side, "safety": safety,
        "banked_s": banked_s, "window": window,
        "gate_ok": abs(margin_bp) >= guard_bp,
        # flip_proof: survives a MANIP_PUSH_BP adversarial shove sustained for
        # the whole remaining window. It is what lets a clip live through
        # quiesce down to FLIP_BUY_CUTOFF_S (updown.rs).
        "flip_proof": (safety >= 1.0
                       and abs(banked_margin_bp) > guard_bp + MANIP_PUSH_BP * (rem / window)),
    }


def terminal_state(series: dict, guard_bp: float, start: int, end: int,
                   t_now: float) -> dict | None:
    """The model these markets ACTUALLY settle against: a digital on the
    terminal 60s TWAP vs the range-start 60s TWAP. No banked mass, no Asian
    cushion — only where the price ends up. This is the same shape as
    eval_model's `close_open` branch (z = ln(spot/ref)/(sig*sqrt(t))), with the
    reference being the pre-start minute's mark rather than a candle open.

    The settlement quantity is a 60s average, so the effective horizon is
    rem-30s (the last half-minute is already inside the averaging window);
    that correction is small except in the final minute."""
    idx = series["t_to_idx"]
    marks = series["marks"]
    j = idx.get(int(t_now // 60) * 60)
    if j is None:
        return None
    spot = marks[j]
    ref_px = marks[idx[start - 60]]
    rem = max(end - t_now - 30.0, 5.0)
    sig = r6.sigma_bp_at(series, j) / 1e4
    z = math.log(spot / ref_px) / max(sig * math.sqrt(rem / 60.0), 1e-12)
    p_up = norm_cdf(z)
    return {"p_up": p_up, "side": "up" if p_up >= 0.5 else "down",
            "conf": max(p_up, 1.0 - p_up), "margin_bp": (spot / ref_px - 1.0) * 1e4,
            "z": z, "sig_bp": sig * 1e4, "rem": end - t_now,
            "gate_ok": abs((spot / ref_px - 1.0) * 1e4) >= guard_bp}


def window_winner_terminal(series: dict, start: int, end: int) -> tuple[str, float]:
    """The winner under the rule the MARKET actually settles on: the Chainlink
    60s-TWAP at range END vs the 60s-TWAP at range START — i.e. the final
    minute's mark vs the pre-start minute's mark. Validated 144/144 against
    gamma resolutions on 4h and 222/232 on 15m (the residual is basis noise on
    thin margins); pmtrader's own grader (polymarket/outcomes.py
    ::chainlink_outcome) already uses this rule. eval_model's twap branch does
    NOT — it averages the whole range, which is a different quantity."""
    idx = series["t_to_idx"]
    ref_px = series["marks"][idx[start - 60]]
    last = series["marks"][idx[end - 60]]
    return ("up" if last >= ref_px else "down"), (last / ref_px - 1.0) * 1e4


def window_winner(series: dict, start: int, end: int) -> tuple[str, float, float]:
    """(winner, final margin bp, ref) on the model's own settlement math."""
    idx = series["t_to_idx"]
    ref_px = series["marks"][idx[start - 60]]
    _cnt, avg_full = r6.range_avg(series, start, end)
    if avg_full is None:
        return "down", 0.0, ref_px
    return ("up" if avg_full >= ref_px else "down"), (avg_full / ref_px - 1.0) * 1e4, ref_px


# ---------------------------------------------------------------------------
# Q1 — evidence timeline + fire frequency
# ---------------------------------------------------------------------------

def timeline_for(series: dict, symbol: str, duration: int, dur_label: str) -> dict:
    guard = GUARD_BP[symbol]
    out = {
        "n": 0, "final_margin_bp": [],
        # per theta: first-crossing elapsed (safety only), first FIRE elapsed
        # (safety + basis guard + side agreement), outcomes
        "safety_first": {th: [] for th in THETAS},
        "fire_first": {th: [] for th in THETAS},
        "fire_win": {th: [0, 0] for th in THETAS},          # [n, wins] vs range-average rule
        "fire_win_term": {th: [0, 0] for th in THETAS},     # [n, wins] vs the REAL settlement rule
        "fire_pre_final30": {th: 0 for th in THETAS},
        "fire_in_final60": {th: 0 for th in THETAS},
        "fire_side_stable": {th: [0, 0] for th in THETAS},  # [n, side still same at end-60s]
        "ever_decided": 0, "ever_decided_pre_final30": 0,
        "safety_at_final30": [], "safety_at_final10": [],
        "margin_at_final30": [], "safety_at_final60": [], "margin_at_final60": [],
        # late-entry variants: {cap: {theta: [n_fired, wins, [rem_at_entry]]}}
        # [n_fired, wins, [rem_at_entry], n_flip_proof_at_entry]
        "late": {cap: {th: [0, 0, [], 0, 0] for th in THETAS} for cap in LATE_CAPS},
    }
    for start, end in r6.iter_window_starts(series, duration):
        if not r6.window_ready(series, start, end):
            continue
        out["n"] += 1
        winner, fmargin, _ref = window_winner(series, start, end)
        winner_t, _tm = window_winner_terminal(series, start, end)
        out["final_margin_bp"].append(abs(fmargin))

        seen_safety = set()
        seen_fire = {}
        seen_late: set = set()
        decided = False
        decided_pre30 = False
        t = start + 60
        last_side = None
        while t <= end - STEP_S:
            st = tick_state(series, guard, start, end, t)
            if st is None:
                t += STEP_S
                continue
            elapsed = t - start
            rem = end - t
            for th in THETAS:
                if th not in seen_safety and st["safety"] >= th:
                    seen_safety.add(th)
                    out["safety_first"][th].append(elapsed)
                if th not in seen_fire and st["safety"] >= th and st["gate_ok"]:
                    seen_fire[th] = (elapsed, st["side"])
                    out["fire_first"][th].append(elapsed)
                    cell = out["fire_win"][th]
                    cell[0] += 1
                    if st["side"] == winner:
                        cell[1] += 1
                    cell_t = out["fire_win_term"][th]
                    cell_t[0] += 1
                    if st["side"] == winner_t:
                        cell_t[1] += 1
                    if rem > 1800:
                        out["fire_pre_final30"][th] += 1
                    if rem <= 3600:
                        out["fire_in_final60"][th] += 1
            for cap in LATE_CAPS:
                for th in THETAS:
                    key = (cap, th)
                    if key in seen_late or rem > cap:
                        continue
                    if st["safety"] >= th and st["gate_ok"]:
                        seen_late.add(key)
                        cell = out["late"][cap][th]
                        cell[0] += 1
                        if st["side"] == winner:
                            cell[1] += 1
                        cell[2].append(rem)
                        if st["flip_proof"]:
                            cell[3] += 1
                        if st["side"] == winner_t:
                            cell[4] += 1
            if st["safety"] >= 1.0:
                decided = True
                if rem > 1800:
                    decided_pre30 = True
            if abs(rem - 3600) < STEP_S / 2:
                out["safety_at_final60"].append(st["safety"])
                out["margin_at_final60"].append(abs(st["margin_bp"]))
            if abs(rem - 1800) < STEP_S / 2:
                out["safety_at_final30"].append(st["safety"])
                out["margin_at_final30"].append(abs(st["margin_bp"]))
            if abs(rem - 600) < STEP_S / 2:
                out["safety_at_final10"].append(st["safety"])
            last_side = st["side"]
            t += STEP_S
        for th, (_el, side) in seen_fire.items():
            cell = out["fire_side_stable"][th]
            cell[0] += 1
            if last_side == side:
                cell[1] += 1
        out["ever_decided"] += 1 if decided else 0
        out["ever_decided_pre_final30"] += 1 if decided_pre30 else 0
    out["dur_label"] = dur_label
    out["duration"] = duration
    return out


def print_timeline(symbol: str, res: dict) -> None:
    n = res["n"]
    dur = res["dur_label"]
    wpd = WINDOWS_PER_DAY[dur]
    guard = GUARD_BP[symbol]
    flag = "  [guard PROVISIONAL — not a live arm]" if symbol in GUARD_PROVISIONAL else ""
    print(f"\n--- {symbol} / {dur}  (guard={guard:.0f}bp{flag}, n={n} windows) ---")
    print(f"  {'theta':<6s} {'P(ever safety>=t)':>18s} {'P(FIRE)':>9s} {'fires/day/arm':>14s} "
          f"{'first-fire elapsed min p10/p50/p90':>36s} {'as % of window':>16s} "
          f"{'win%':>7s} {'REAL win%':>10s} {'pre-final30m':>13s} {'side-stable':>12s}")
    for th in THETAS:
        sf = res["safety_first"][th]
        ff = res["fire_first"][th]
        p_safety = len(sf) / n if n else 0.0
        p_fire = len(ff) / n if n else 0.0
        fw_n, fw_w = res["fire_win"][th]
        win = f"{100.0 * fw_w / fw_n:5.1f}%" if fw_n else "   n/a"
        ft_n, ft_w = res["fire_win_term"][th]
        win_t = f"{100.0 * ft_w / ft_n:8.1f}%" if ft_n else "     n/a"
        st_n, st_ok = res["fire_side_stable"][th]
        stable = f"{100.0 * st_ok / st_n:5.1f}%" if st_n else "   n/a"
        if ff:
            p10, p50, p90 = pct(ff, .10) / 60, pct(ff, .50) / 60, pct(ff, .90) / 60
            tx = f"{p10:8.1f} /{p50:8.1f} /{p90:8.1f}"
            fx = f"{100 * pct(ff, .50) / res['duration']:14.0f}%"
        else:
            tx, fx = " " * 27, " " * 15
        pre30 = f"{100.0 * res['fire_pre_final30'][th] / n:11.1f}%" if n else "n/a"
        print(f"  {th:<6.2f} {100 * p_safety:17.1f}% {100 * p_fire:8.1f}% "
              f"{wpd * p_fire:14.2f} {tx:>36s} {fx:>16s} {win:>7s} {win_t:>10s} {pre30:>13s} "
              f"{stable:>12s}")
    ed = 100.0 * res["ever_decided"] / n if n else 0.0
    ed30 = 100.0 * res["ever_decided_pre_final30"] / n if n else 0.0
    s30 = res["safety_at_final30"]
    s10 = res["safety_at_final10"]
    m30 = res["margin_at_final30"]
    print(f"  banked-decided (safety>=1.0) ever: {ed:.1f}%   before the final 30min: {ed30:.1f}%")
    if s30:
        print(f"  safety at T-30min: p10={pct(s30, .1):+.2f} p50={pct(s30, .5):+.2f} "
              f"p90={pct(s30, .9):+.2f}   |proj margin| at T-30min: p50={pct(m30, .5):.1f}bp "
              f"(guard {guard:.0f}bp)")
    if s10:
        print(f"  safety at T-10min: p10={pct(s10, .1):+.2f} p50={pct(s10, .5):+.2f} "
              f"p90={pct(s10, .9):+.2f}")
    s60 = res["safety_at_final60"]
    m60 = res["margin_at_final60"]
    if s60:
        print(f"  safety at T-60min: p10={pct(s60, .1):+.2f} p50={pct(s60, .5):+.2f} "
              f"p90={pct(s60, .9):+.2f}   |proj margin| at T-60min: p50={pct(m60, .5):.1f}bp")
    if any(res["late"][cap][th][0] for cap in LATE_CAPS for th in THETAS):
        print(f"  TAIL-HARVEST variant — first clip only inside the final N minutes "
              f"(the min_elapsed knob):")
        print(f"    {'window':<8s} {'theta':<6s} {'P(FIRE)':>9s} {'fires/day/arm':>14s} "
              f"{'win%':>7s} {'entry rem min p50':>18s} {'flip-proof at entry':>20s} "
              f"{'REAL win%':>10s}")
        for cap in LATE_CAPS:
            for th in THETAS:
                nfire, wins, rems, fp, wins_t = res["late"][cap][th]
                if n == 0:
                    continue
                w = f"{100.0 * wins / nfire:6.1f}%" if nfire else "   n/a"
                wt = f"{100.0 * wins_t / nfire:9.1f}%" if nfire else "      n/a"
                rm = f"{pct(rems, .5) / 60:17.1f}" if rems else "              n/a"
                fpx = f"{100.0 * fp / nfire:19.1f}%" if nfire else "                n/a"
                print(f"    {'T-' + str(cap // 60) + 'min':<8s} {th:<6.2f} "
                      f"{100.0 * nfire / n:8.1f}% {wpd * nfire / n:14.2f} {w:>7s} {rm:>18s} "
                      f"{fpx:>20s} {wt:>10s}")


# ---------------------------------------------------------------------------
# Q2 — flip table (4h rem buckets) + Gaussian residual scaling + margins
# ---------------------------------------------------------------------------

def rem_bucket_4h(rem: float) -> int:
    for i, (_l, lo, hi) in enumerate(REM_BUCKETS_4H):
        if lo <= rem < hi:
            return i
    return len(REM_BUCKETS_4H) - 1


def flip_and_scale(series: dict, symbol: str, duration: int) -> dict:
    """r6's first-crossing flip table with 4h-scaled rem buckets, plus the
    residual-scaling z sample: z = ln(A_rem/spot) / (sig*sqrt(rem_min/3)),
    where A_rem is the REALIZED average mark over the model's remaining
    window. std(z) ~ 1 means the sqrt(T/3) Asian-average cushion is the right
    size at this horizon; std(z) > 1 means it is too small."""
    guard = GUARD_BP[symbol]
    agg = {"bucket": {}, "z": {i: [] for i in range(len(REM_BUCKETS_4H))},
           "theta_flip": {th: {} for th in THETAS}, "n": 0}
    for start, end in r6.iter_window_starts(series, duration):
        if not r6.window_ready(series, start, end):
            continue
        agg["n"] += 1
        winner, _fm, _ref = window_winner(series, start, end)
        visited: set = set()
        visited_theta: set = set()
        t = start + 60
        while t <= end - STEP_S:
            st = tick_state(series, guard, start, end, t)
            if st is None:
                t += STEP_S
                continue
            rb = rem_bucket_4h(st["rem"])
            banked_side = ("up" if st["banked_margin_bp"] > 0
                           else ("down" if st["banked_margin_bp"] < 0 else None))
            if banked_side is not None:
                sb = r6.safety_bucket_idx(abs(st["banked_margin_bp"]) / max(st["cushion_bp"], 1e-9))
                if sb is not None and sb not in visited:
                    visited.add(sb)
                    cell = agg["bucket"].setdefault((sb, rb), [0, 0])
                    cell[0] += 1
                    if banked_side != winner:
                        cell[1] += 1
            for th in THETAS:
                if th not in visited_theta and st["safety"] >= th and st["gate_ok"]:
                    visited_theta.add(th)
                    cell = agg["theta_flip"][th].setdefault(rb, [0, 0])
                    cell[0] += 1
                    if st["side"] != winner:
                        cell[1] += 1
            # residual scaling sample. sig floor of 0.5bp/min: a dead-flat
            # trailing window makes the denominator ~0 and the ratio explodes
            # (the live code is protected there by the arm-time param floor).
            lo = max(t - r6.BANKED_LAG_S, start)
            cnt, a_rem = r6.range_avg(series, lo, end)
            if cnt >= 1 and a_rem and st["spot"] > 0 and st["sig_bp"] >= 0.5 and st["rem"] >= 60:
                denom = (st["sig_bp"] / 1e4) * math.sqrt(max(st["rem"] / 60.0, 0.02) / 3.0)
                if denom > 1e-12:
                    agg["z"][rb].append(math.log(a_rem / st["spot"]) / denom)
            t += STEP_S
    return agg


def print_flip_table(symbol: str, dur_label: str, agg: dict) -> None:
    print(f"\n--- {symbol} / {dur_label}  (guard={GUARD_BP[symbol]:.0f}bp, n={agg['n']} windows) ---")
    hdr = f"{'safety':<30s} {'gauss':>7s}"
    for lbl, _lo, _hi in REM_BUCKETS_4H:
        hdr += f"  {lbl:>17s}"
    print(hdr)
    for i, (lbl, lo, _hi) in enumerate(SAFETY_BUCKETS):
        row = f"{lbl:<30s} {100 * (1 - norm_cdf(lo)):6.2f}%"
        for rb in range(len(REM_BUCKETS_4H)):
            n, flips = agg["bucket"].get((i, rb), [0, 0])
            cell = "n=0" if n == 0 else f"n={n:<4d} {100.0 * flips / n:4.1f}%"
            row += f"  {cell:>17s}"
        print(row)


def print_theta_flip(symbol: str, dur_label: str, agg: dict) -> None:
    print(f"  entry-gate flip rate (first FIRE per window: safety>=theta AND basis guard passed):")
    for th in THETAS:
        cells = agg["theta_flip"][th]
        tot = sum(c[0] for c in cells.values())
        fl = sum(c[1] for c in cells.values())
        by = "  ".join(
            f"{REM_BUCKETS_4H[rb][0]}:n={cells[rb][0]},{100.0 * cells[rb][1] / cells[rb][0]:.1f}%"
            for rb in sorted(cells) if cells[rb][0] > 0)
        rate = f"{100.0 * fl / tot:5.2f}%" if tot else "  n/a"
        print(f"    theta={th:<4.2f}  n={tot:<5d} flip={rate}   [by rem at entry] {by}")


def print_scaling(symbol: str, dur_label: str, agg: dict) -> None:
    print(f"  residual scaling check — z = ln(A_rem/spot)/(sig*sqrt(rem/3)), the model's own "
          f"Asian-average residual. Scale>1 = the sqrt(T/3) cushion is too SMALL at that horizon.")
    print(f"    {'rem bucket':<12s} {'n':>8s} {'robustSD':>9s} {'trimSD':>8s} {'p1':>7s} "
          f"{'p99':>7s} {'|z|>3':>7s} {'|z|>5':>7s}")
    for rb, (lbl, _lo, _hi) in enumerate(REM_BUCKETS_4H):
        zs = agg["z"][rb]
        if len(zs) < 30:
            continue
        # robust scale from the central quantiles (immune to the sigma-estimate
        # blowups that make a raw stdev meaningless here)
        rsd = (pct(zs, .84134) - pct(zs, .15866)) / 2.0
        trimmed = [z for z in zs if abs(z) <= 10]
        tsd = statistics.pstdev(trimmed) if len(trimmed) > 2 else float("nan")
        big3 = 100.0 * sum(1 for z in zs if abs(z) > 3) / len(zs)
        big5 = 100.0 * sum(1 for z in zs if abs(z) > 5) / len(zs)
        print(f"    {lbl:<12s} {len(zs):>8d} {rsd:>9.2f} {tsd:>8.2f} {pct(zs, .01):>7.2f} "
              f"{pct(zs, .99):>7.2f} {big3:>6.2f}% {big5:>6.2f}%")


# ---------------------------------------------------------------------------
# Q2b — final margin distribution per symbol x duration (basis bindingness)
# ---------------------------------------------------------------------------

def margin_stats(series: dict, symbol: str, duration: int) -> dict:
    vals = []
    for start, end in r6.iter_window_starts(series, duration):
        if not r6.window_ready(series, start, end):
            continue
        _w, fm, _r = window_winner(series, start, end)
        vals.append(abs(fm))
    guard = GUARD_BP[symbol]
    p95 = BASIS_P95_BP.get(symbol)
    n = len(vals)
    return {
        "n": n, "p10": pct(vals, .10), "p25": pct(vals, .25), "p50": pct(vals, .50),
        "p75": pct(vals, .75), "p90": pct(vals, .90),
        "lt_guard": 100.0 * sum(1 for v in vals if v < guard) / n if n else float("nan"),
        "lt_p95": (100.0 * sum(1 for v in vals if p95 and v < p95) / n) if (n and p95) else None,
        "lt_30": 100.0 * sum(1 for v in vals if v < 30.0) / n if n else float("nan"),
        "ge_30": 100.0 * sum(1 for v in vals if v >= 30.0) / n if n else float("nan"),
    }


# ---------------------------------------------------------------------------
# Q2c — MEASURED Chainlink-vs-Binance settlement error, 4h vs 15m
# ---------------------------------------------------------------------------

def load_chainlink(short: str) -> list[tuple[int, float]]:
    p = CORPUS_DIR / f"chainlink-{short}.jsonl"
    if not p.exists():
        return []
    rows = []
    for line in p.open():
        line = line.strip()
        if not line:
            continue
        try:
            r = json.loads(line)
            rows.append((int(r["updated_at"]), float(r["price"])))
        except (ValueError, KeyError, TypeError):
            continue
    rows.sort()
    return rows


def ck_minute_twap(rounds: list[tuple[int, float]], m_start: int) -> float | None:
    """Time-weighted Chainlink average over [m_start, m_start+60) — the same
    step-interpolated TWAP analysis/r1_aligned_basis.py uses. None if the
    minute isn't fully covered by the round history."""
    if not rounds:
        return None
    ts = [r[0] for r in rounds]
    i = bisect.bisect_right(ts, m_start) - 1
    if i < 0 or ts[-1] < m_start + 60:
        return None
    total, t = 0.0, m_start
    while t < m_start + 60:
        nxt = ts[i + 1] if i + 1 < len(ts) else m_start + 60
        seg_end = min(nxt, m_start + 60)
        total += rounds[i][1] * (seg_end - t)
        t = seg_end
        i += 1
        if i >= len(rounds):
            break
    return total / 60.0 if t >= m_start + 60 else None


def basis_margin_error(series: dict, symbol: str, duration: int) -> dict:
    """For every window fully covered by BOTH corpora: the settlement margin
    computed on Chainlink TWAPs minus the same margin on Binance marks. This
    is the error that actually decides a banked-decided window — and it is the
    quantity the basis guard exists to cover."""
    rounds = load_chainlink(CK_SHORT[symbol])
    if not rounds:
        return {"n": 0}
    ck_lo, ck_hi = rounds[0][0], rounds[-1][0]
    errs, bn_margins = [], []
    for start, end in r6.iter_window_starts(series, duration):
        if not r6.window_ready(series, start, end):
            continue
        if start - 60 < ck_lo or end > ck_hi:
            continue
        ref_ck = ck_minute_twap(rounds, start - 60)
        if ref_ck is None:
            continue
        vals, ok = [], True
        m = start
        while m < end:
            v = ck_minute_twap(rounds, m)
            if v is None:
                ok = False
                break
            vals.append(v)
            m += 60
        if not ok or not vals:
            continue
        ck_margin = (sum(vals) / len(vals) / ref_ck - 1.0) * 1e4
        _w, bn_margin, _r = window_winner(series, start, end)
        errs.append(ck_margin - bn_margin)
        bn_margins.append(bn_margin)
    if not errs:
        return {"n": 0}
    a = [abs(e) for e in errs]
    disagree = sum(1 for e, b in zip(errs, bn_margins) if (b + e >= 0) != (b >= 0))
    gate = [(e, b) for e, b in zip(errs, bn_margins) if abs(b) >= GUARD_BP[symbol]]
    gate_flip = sum(1 for e, b in gate if (b + e >= 0) != (b >= 0))
    return {"n": len(errs), "mean": statistics.fmean(errs), "p50": pct(a, .50),
            "p90": pct(a, .90), "p95": pct(a, .95), "max": max(a),
            "flip_windows": disagree, "gate_n": len(gate), "gate_flip": gate_flip,
            "errs": errs}


def basis_flip_convolution(series: dict, symbol: str, duration: int,
                           errs: list[float]) -> dict:
    """P(the oracle flips a window we would have entered), estimated over the
    FULL kline corpus instead of the ~50h oracle overlap: every gate-passing
    window's Binance margin is perturbed by every measured settlement error.
    Decouples the (long, stable) margin distribution from the (short, scarce)
    oracle-error sample, which is the only way to get a usable number for 4h."""
    if not errs:
        return {"n": 0}
    guard = GUARD_BP[symbol]
    margins = []
    for start, end in r6.iter_window_starts(series, duration):
        if not r6.window_ready(series, start, end):
            continue
        _w, fm, _r = window_winner(series, start, end)
        if abs(fm) >= guard:
            margins.append(fm)
    if not margins:
        return {"n": 0}
    flips = sum(1 for m in margins for e in errs if (m + e >= 0) != (m >= 0))
    total = len(margins) * len(errs)
    return {"n": len(margins), "pairs": total, "p_flip": 100.0 * flips / total}


# ---------------------------------------------------------------------------
# Q4 — fleet interaction: concurrent decided directions across durations
# ---------------------------------------------------------------------------

def fire_grid(series: dict, symbol: str, duration: int, theta: float,
              late_cap: float | None = None) -> dict[int, tuple[str, bool]]:
    """minute -> (fired side, side==winner) for the enclosing window, only for
    minutes at which the window's gate is open (safety>=theta AND guard).
    `late_cap` restricts to minutes with rem <= cap (the min_elapsed posture)."""
    guard = GUARD_BP[symbol]
    out: dict[int, tuple[str, bool]] = {}
    for start, end in r6.iter_window_starts(series, duration):
        if not r6.window_ready(series, start, end):
            continue
        winner, _fm, _r = window_winner(series, start, end)
        t = start + 60
        while t <= end - FLEET_STEP_S:
            if late_cap is not None and (end - t) > late_cap:
                t += FLEET_STEP_S
                continue
            st = tick_state(series, guard, start, end, t)
            if st is not None and st["gate_ok"] and st["safety"] >= theta:
                out[int(t)] = (st["side"], st["side"] == winner)
            t += FLEET_STEP_S
    return out


def fleet_overlap(series: dict, symbol: str, theta: float,
                  late_cap: float | None = None) -> dict:
    g4 = fire_grid(series, symbol, 14400, theta, late_cap)
    g15 = fire_grid(series, symbol, 900, theta)
    g5 = fire_grid(series, symbol, 300, theta)
    up4 = sum(1 for s, _ in g4.values() if s == "up")
    up15 = sum(1 for s, _ in g15.values() if s == "up")
    res = {"n_4h_open": len(g4), "both15": 0, "same15": 0, "both5": 0, "same5": 0,
           "joint_loss15": 0, "joint_loss5": 0, "loss4_when_both15": 0,
           "up4": up4, "up15": up15}
    for t, (s4, ok4) in g4.items():
        if t in g15:
            s15, ok15 = g15[t]
            res["both15"] += 1
            if s15 == s4:
                res["same15"] += 1
                if not ok4 and not ok15:
                    res["joint_loss15"] += 1
            if not ok4:
                res["loss4_when_both15"] += 1
        if t in g5:
            s5, ok5 = g5[t]
            res["both5"] += 1
            if s5 == s4:
                res["same5"] += 1
                if not ok4 and not ok5:
                    res["joint_loss5"] += 1
    res["n_15_open"] = len(g15)
    res["n_5_open"] = len(g5)
    res["minutes_total"] = len(series["ts"])
    return res


# ---------------------------------------------------------------------------

def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--symbols", nargs="+", default=SYMBOLS)
    ap.add_argument("--days", type=float, default=21.0, help="corpus lookback to FETCH")
    ap.add_argument("--limit-days", type=float, default=None,
                    help="analyse only the most recent N days of the cache (regime subsets)")
    ap.add_argument("--no-fetch", action="store_true")
    ap.add_argument("--theta-fleet", type=float, default=0.3)
    ap.add_argument("--skip", nargs="*", default=[],
                    help="sections to skip: timeline flip margins basis fleet")
    args = ap.parse_args()

    print(f"4h up/down tier fit — lookback {args.days:.0f}d, symbols {args.symbols}")
    print(f"model transcription: eval_model() @ updown_model.rs; banked lag flat "
          f"{r6.BANKED_LAG_S:.0f}s; step {STEP_S}s; settlement = 60s-TWAP regime\n")

    series_by: dict[str, dict] = {}
    print("COVERAGE")
    for sym in args.symbols:
        stats: dict = {}
        if not args.no_fetch:
            try:
                stats = r6.extend_corpus(sym, args.days)
            except Exception as e:  # noqa: BLE001
                print(f"{sym}: fetch error ({e}) — using cache")
        data = r6.load_cache(sym)
        if args.limit_days and data:
            cut = max(data) - args.limit_days * 86400
            data = {t: v for t, v in data.items() if t >= cut}
        series_by[sym] = r6.build_series(data)
        r6.print_coverage(sym, series_by[sym], stats)

    if "timeline" not in args.skip:
        print("\n" + "=" * 118)
        print("Q1  EVIDENCE TIMELINE + FIRE FREQUENCY")
        print("P(FIRE) = window ever reaches safety>=theta with the basis guard passed and the "
              "banked side agreeing with p_up.")
        print("win% = the side that fired matched the window's Binance-settlement winner "
              "(NOT Chainlink truth — see the basis section).")
        print("=" * 118)
        for sym in args.symbols:
            for lbl, dur in DURATIONS:
                print_timeline(sym, timeline_for(series_by[sym], sym, dur, lbl))

    if "flip" not in args.skip:
        print("\n" + "=" * 118)
        print("Q2  FLIP RATE BY SAFETY x REM (4h-scaled buckets) + RESIDUAL SCALING")
        print("n = windows whose safety FIRST entered this bucket at this rem; "
              "flip% = the banked side lost.")
        print("=" * 118)
        for sym in args.symbols:
            for lbl, dur in [("4h", 14400), ("15m", 900)]:
                agg = flip_and_scale(series_by[sym], sym, dur)
                print_flip_table(sym, lbl, agg)
                print_theta_flip(sym, lbl, agg)
                print_scaling(sym, lbl, agg)

    if "margins" not in args.skip:
        print("\n" + "=" * 118)
        print("Q2b  FINAL |MARGIN| DISTRIBUTION — how binding is the guard / the basis tail?")
        print("=" * 118)
        print(f"{'symbol':<9s} {'dur':<4s} {'n':>6s} {'p10':>7s} {'p25':>7s} {'p50':>7s} "
              f"{'p75':>7s} {'p90':>7s} {'<guard':>8s} {'<basisP95':>10s} {'>=30bp':>8s} "
              f"{'p50/guard':>10s} {'p50/p95':>8s}")
        for sym in args.symbols:
            for lbl, dur in DURATIONS:
                s = margin_stats(series_by[sym], sym, dur)
                g = GUARD_BP[sym]
                p95 = BASIS_P95_BP.get(sym)
                lt95 = f"{s['lt_p95']:9.1f}%" if s["lt_p95"] is not None else "      n/a"
                r95 = f"{s['p50'] / p95:8.1f}" if p95 else "     n/a"
                print(f"{sym:<9s} {lbl:<4s} {s['n']:>6d} {s['p10']:>7.1f} {s['p25']:>7.1f} "
                      f"{s['p50']:>7.1f} {s['p75']:>7.1f} {s['p90']:>7.1f} {s['lt_guard']:>7.1f}% "
                      f"{lt95:>10s} {s['ge_30']:>7.1f}% {s['p50'] / g:>10.1f} {r95:>8s}")

    if "basis" not in args.skip:
        print("\n" + "=" * 118)
        print("Q2c  MEASURED CHAINLINK-vs-BINANCE SETTLEMENT ERROR (corpus-limited, ~50h)")
        print("err = (margin computed on Chainlink minute-TWAPs) - (margin on Binance marks), bp. "
              "This is the quantity the basis guard covers; a 4h window averages 240 oracle "
              "minutes against a 60s reference, a 15m window averages 15.")
        print("=" * 118)
        print(f"{'symbol':<9s} {'dur':<4s} {'n':>5s} {'mean':>8s} {'p50|e|':>8s} {'p90|e|':>8s} "
              f"{'p95|e|':>8s} {'max|e|':>8s} {'flips(all)':>11s} {'flips(gate)':>12s} "
              f"{'P(flip|gate) conv':>18s}")
        for sym in args.symbols:
            err_by_dur: dict[str, list[float]] = {}
            for lbl, dur in [("15m", 900), ("4h", 14400)]:
                b = basis_margin_error(series_by[sym], sym, dur)
                if not b.get("n"):
                    print(f"{sym:<9s} {lbl:<4s} {'0':>5s}   (no overlapping oracle corpus)")
                    continue
                err_by_dur[lbl] = b["errs"]
                # thin per-duration samples borrow the 15m error sample: same
                # 60s-TWAP settlement mechanic, same reference leg.
                src = b["errs"] if b["n"] >= 30 else err_by_dur.get("15m", [])
                conv = basis_flip_convolution(series_by[sym], sym, dur, src)
                tag = "" if b["n"] >= 30 else "*"
                cv = (f"{conv['p_flip']:16.2f}%{tag}" if conv.get("n")
                      else "               n/a")
                print(f"{sym:<9s} {lbl:<4s} {b['n']:>5d} {b['mean']:>+8.2f} {b['p50']:>8.2f} "
                      f"{b['p90']:>8.2f} {b['p95']:>8.2f} {b['max']:>8.2f} "
                      f"{b['flip_windows']:>4d}/{b['n']:<6d} {b['gate_flip']:>5d}/{b['gate_n']:<6d} "
                      f"{cv:>18s}")
        print("  P(flip|gate) conv = every gate-passing window in the FULL kline corpus x every "
              "measured error; * = the duration's own error sample was <30, so the 15m sample "
              "was used.")

    if "fleet" not in args.skip:
        th = args.theta_fleet
        print("\n" + "=" * 118)
        print(f"Q4  FLEET INTERACTION — same-symbol overlap at theta={th}")
        print("Grid = 60s. '4h open' = minutes where the 4h arm's entry gate would pass. "
              "Same-side% = the concurrent shorter window's gate-open side matched the 4h side.")
        print("=" * 118)
        print(f"{'symbol':<9s} {'4h posture':<10s} {'4h open min':>12s} {'%of corpus':>11s} "
              f"{'15m also open':>14s} {'same side':>10s} {'5m also open':>13s} {'same side':>10s} "
              f"{'joint-loss 15m':>15s} {'4h up-share':>12s} {'15m up-share':>13s}")
        for sym in args.symbols:
            for posture, cap in (("all-window", None), ("final-60m", 3600.0)):
                f = fleet_overlap(series_by[sym], sym, th, cap)
                b15 = f["both15"]
                b5 = f["both5"]
                n4 = f["n_4h_open"]
                open_pct = 100.0 * n4 / max(f["minutes_total"], 1)
                s15 = f"{100.0 * f['same15'] / b15:9.1f}%" if b15 else "      n/a"
                s5 = f"{100.0 * f['same5'] / b5:9.1f}%" if b5 else "      n/a"
                j15 = f"{100.0 * f['joint_loss15'] / f['same15']:14.2f}%" if f["same15"] else "      n/a"
                p15 = f"{100.0 * b15 / n4:13.1f}%" if n4 else "     n/a"
                p5 = f"{100.0 * b5 / n4:12.1f}%" if n4 else "    n/a"
                u4 = f"{100.0 * f['up4'] / n4:11.1f}%" if n4 else "    n/a"
                u15 = (f"{100.0 * f['up15'] / f['n_15_open']:12.1f}%"
                       if f["n_15_open"] else "     n/a")
                print(f"{sym:<9s} {posture:<10s} {n4:>12d} {open_pct:>10.1f}% {p15:>14s} "
                      f"{s15:>10s} {p5:>13s} {s5:>10s} {j15:>15s} {u4:>12s} {u15:>13s}")

    print("\nCAVEATS: 'win%' grades against the RANGE-AVERAGE rule eval_model believes in; "
          "'REAL win%' grades against the terminal 60s-TWAP rule the markets actually settle on "
          "(analysis/settlement_rule_check.py, 3193 resolved windows). Where the two columns "
          "diverge, the second one is the money. Q2's flip tables, the safety/cushion percentiles "
          "and Q4's overlap are all computed in the range-average frame and describe a quantity "
          "that decides nothing — kept because they are the measurement that exposed the gap. "
          "Q2c's basis leg is 50h of oracle corpus. Fills, book depth and fees are NOT modelled: "
          "P(FIRE) is a MODEL-gate frequency, an upper bound on real fires. xrp/doge/bnb guards "
          "are R1-p95 proposals, not live arms.")


if __name__ == "__main__":
    main()
