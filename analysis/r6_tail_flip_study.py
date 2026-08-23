#!/usr/bin/env python3
"""R6 tail-flip study (ROADMAP.md Phase 2, R6).

2026-08-23 CORRECTION — READ analysis/fourh_fit.md BEFORE USING THIS SCRIPT'S
CONCLUSIONS. Its "winner" is the average of the window's minute marks vs the
reference, i.e. what pmengine's eval_model believes settles the market. That is
not what settles the market: analysis/settlement_rule_check.py shows over 3,193
resolved windows that resolution follows the Chainlink 60s TWAP at the range END
vs the same stream at range start (98.98% agreement at 4h vs 86.02% for the
range average; 144-4 head-to-head where the two disagree). So the "~0% flip rate
at safety >= 1.0" below is a true statement about a quantity that decides
nothing. The flip rates against real resolutions are 1.8% (5m), 7.5% (15m) and
9.6% (4h) at theta=1.0. R6's conclusions need re-deriving once eval_model is
re-specified.

The updown strategy's "banked_decided" verdict (pmengine/src/strategies/
updown.rs::eval_model) trusts a Gaussian cushion:

    banked_margin_bp = (banked_avg/ref - 1)*1e4 * (banked_s/window)
    cushion_bp       = guard_bp + sig_bp * sqrt((rem_s/60)/3) * (rem_s/window)
    safety           = |banked_margin_bp| / cushion_bp

and unlocks the full clip budget once safety > 1 on the banked side (roughly
— see budget_unlocked/side_safety in updown.rs). The implicit claim is
Gaussian: safety=k means the reversal needed to flip the window is a ~k-sigma
event on the *unlocked* remainder, i.e. P(flip) ~= 1-norm_cdf(k). But crypto
minute bars aren't Gaussian (jump risk), and a sol 15m window that looked
"decided" flipped for -$142 the night this was commissioned.

This script replays the model's own math (not a new model) tick-by-tick over
weeks of real Binance 1m klines, and measures the TRUE conditional flip rate
as a function of the safety multiple — per symbol, per duration, per
time-remaining bucket. It is pure measurement: it does not touch pmengine,
does not arm/disarm anything, and does not change any live parameter. It only
reads klines-1m-{SYMBOL}.jsonl from the corpus and prints a report.

Formulas below are transcribed 1:1 from pmengine/src/strategies/updown.rs
(eval_model, trailing_sigma_bp, vol_floor_bp, norm_cdf) as of 2026-08-23,
with two backtest-only simplifications the live code doesn't need:
  - "spot" (the live tick price) becomes m(current_minute) = (o+c)/2 of the
    in-progress minute, since 1m klines are our only resolution.
  - the live arm-time sigma_bp_per_min fallback (an operator param) isn't
    available here; vol_floor_bp's cold-start branch is approximated by the
    12m fast window, which is what it degrades toward anyway.
  - "first crossing" of a safety/k/J/p threshold in a window is the first
    time-STEP whose current value lands in that bucket (or clears that
    threshold), tracked with a per-window visited-set, keyed by bucket index
    only (not by which side is currently banked). If a window's banked side
    flips sign in the first few high-noise steps (small banked_s), a bucket's
    "first crossing" reflects whichever side was banked at that instant, not
    a fixed side. This mirrors the live gate's per-tick side_safety check
    closely enough for this study; it is not a perfect reconstruction of a
    hypothetical "first clip ever" event across side flips.

Corpus cache: ~/.pmt/corpus/klines-1m-{SYMBOL}.jsonl, rows
{"t": minute_epoch_secs, "o": ..., "c": ...} — append-only, deduped by t.
Never rewritten; this script only appends what's missing for the requested
lookback window.

Run: cd pmtrader && uv run python ../analysis/r6_tail_flip_study.py
     cd pmtrader && uv run python ../analysis/r6_tail_flip_study.py --no-fetch   (use cache as-is)
     cd pmtrader && uv run python ../analysis/r6_tail_flip_study.py --days 7     (shorter lookback)
"""
from __future__ import annotations

import argparse
import bisect
import json
import math
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "pmtrader"))
from polymarket.fit import fetch_klines  # noqa: E402  (needs sys.path patch above)

# ---------------------------------------------------------------------------
# constants — mirrors pmengine/src/strategies/updown.rs unless noted
# ---------------------------------------------------------------------------

CORPUS_DIR = Path.home() / ".pmt" / "corpus"
SYMBOLS = ["BTCUSDT", "ETHUSDT", "SOLUSDT"]

# Per-arm basis guards as specified for this study (ROADMAP R6 spec) — NOT
# necessarily identical to whatever chainlink.GUARD_BP holds live right now;
# this is the input the study was asked to test. BNBUSDT is a fit candidate,
# not live: 8bp is its R1 settlement-p95 proposal (`--symbols BNBUSDT`).
GUARD_BP = {"BTCUSDT": 6.0, "ETHUSDT": 8.0, "SOLUSDT": 10.0, "BNBUSDT": 8.0}

# 4h added 2026-08-23 for the tail-harvest fit (analysis/fourh_fit.md). Its rem
# buckets below only reach 600s, so a 4h window spends ~96% of its life in the
# informational "600s+" column and the FIT pass/fail criterion is effectively
# a statement about the final 10 minutes only — read analysis/fourh_fit.py's
# 4h-scaled rem buckets for the rest of that window.
DURATIONS = [("5m", 300), ("15m", 900), ("4h", 14400)]

VOL_FAST_WINDOW = 12          # updown.rs VOL_FAST_WINDOW
SIGMA_SLOW_WINDOW = 45        # updown.rs SIGMA_SLOW_WINDOW
SIGMA_SLOW_MIN = 30           # updown.rs SIGMA_SLOW_MIN
STEP_S = 15                   # walk granularity
BANKED_LAG_S = 30.0           # updown.rs: banked cut is (now - 30s)
TARGET_DAYS = 21
SLEEP_S = 0.2                 # be polite to data-api.binance.vision
RETRIES = 3

REM_BUCKETS = [("0-60s", 0, 60), ("60-120s", 60, 120), ("120-300s", 120, 300), ("300-600s", 300, 600)]
REM_OVER_LABEL = "600s+ (info)"
# The two <1.0 rows are NOT part of the R6 spec (which starts the grid at
# 1.0) — added so a reader can see the flip curve's shape approaching the
# model's own "decided" line, instead of only seeing "0% at >=1.0" in
# isolation and wondering whether that's a real result or a broken pipeline.
SAFETY_BUCKETS = [
    ("0.5-0.75 (info, pre-decided)", 0.5, 0.75), ("0.75-1.0 (info, pre-decided)", 0.75, 1.0),
    ("1.0-1.25", 1.0, 1.25), ("1.25-1.5", 1.25, 1.5), ("1.5-2", 1.5, 2.0),
    ("2-3", 2.0, 3.0), ("3-5", 3.0, 5.0), ("5+", 5.0, float("inf")),
]
K_GRID = [round(1.0 + 0.25 * i, 2) for i in range(9)]   # 1.00 .. 3.00 step .25
J_LIST = [0.0, 10.0, 20.0, 30.0, 50.0]                   # additive jump term, bp
P_THRESH = [0.95, 0.97, 0.98, 0.99]

FIT_FLIP_MAX_PCT = 3.0
FIT_MIN_N = 30


# ---------------------------------------------------------------------------
# corpus cache: append/dedupe by minute epoch, never rewritten
# ---------------------------------------------------------------------------

def corpus_path(symbol: str) -> Path:
    return CORPUS_DIR / f"klines-1m-{symbol}.jsonl"


def load_cache(symbol: str) -> dict[int, tuple[float, float]]:
    path = corpus_path(symbol)
    out: dict[int, tuple[float, float]] = {}
    if not path.exists():
        return out
    with open(path) as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                r = json.loads(line)
                out[int(r["t"])] = (float(r["o"]), float(r["c"]))
            except (ValueError, KeyError, TypeError):
                continue
    return out


def append_cache(symbol: str, rows: dict[int, tuple[float, float]]) -> int:
    if not rows:
        return 0
    path = corpus_path(symbol)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a") as fh:
        for t in sorted(rows):
            o, c = rows[t]
            fh.write(json.dumps({"t": t, "o": o, "c": c}) + "\n")
    return len(rows)


def _fetch_klines_retry(symbol: str, start_ms: int, limit: int = 1000) -> list:
    last_err = None
    for attempt in range(RETRIES):
        try:
            return fetch_klines(symbol, "1m", start_ms=start_ms, limit=limit)
        except Exception as e:  # noqa: BLE001 — public mirror hiccups are routine
            last_err = e
            time.sleep(0.6 * (attempt + 1))
    raise RuntimeError(f"klines fetch failed after {RETRIES} attempts: {last_err}")


def fetch_range(symbol: str, start_s: int, end_s: int) -> dict[int, tuple[float, float]]:
    """Paginate 1m klines over [start_s, end_s) from data-api.binance.vision.

    NEVER api.binance.com (geo-blocked). limit=1000/call, startTime pagination,
    politely rate-limited.
    """
    out: dict[int, tuple[float, float]] = {}
    cursor_ms = start_s * 1000
    while cursor_ms // 1000 < end_s:
        kl = _fetch_klines_retry(symbol, cursor_ms)
        time.sleep(SLEEP_S)
        if not kl:
            break
        stop = False
        for k in kl:
            t = int(k[0]) // 1000
            if t >= end_s:
                stop = True
                break
            o, c = float(k[1]), float(k[4])
            if o > 0 and c > 0:
                out[t] = (o, c)
        if stop or len(kl) < 1000:
            break
        cursor_ms = (int(kl[-1][0]) // 1000 + 60) * 1000
    return out


def extend_corpus(symbol: str, target_days: float) -> dict:
    """Top up (newest -> now) + backfill (target_start -> oldest). Only fetches gaps."""
    existing = load_cache(symbol)
    now_minute = int(time.time() // 60) * 60
    target_start = now_minute - int(target_days * 86400)
    fetched: dict[int, tuple[float, float]] = {}

    if not existing:
        fetched.update(fetch_range(symbol, target_start, now_minute))
    else:
        oldest, newest = min(existing), max(existing)
        if oldest > target_start:
            fetched.update(fetch_range(symbol, target_start, oldest))
        if newest + 60 < now_minute:
            fetched.update(fetch_range(symbol, newest + 60, now_minute))

    new_rows = {t: v for t, v in fetched.items() if t not in existing}
    appended = append_cache(symbol, new_rows)
    merged = dict(existing)
    merged.update(fetched)
    return {"appended": appended, "existing": len(existing), "total": len(merged), "data": merged}


# ---------------------------------------------------------------------------
# per-symbol series + rolling-stat scaffolding
# ---------------------------------------------------------------------------

def build_series(data: dict[int, tuple[float, float]]) -> dict:
    ts = sorted(data)
    marks = [(data[t][0] + data[t][1]) / 2.0 for t in ts]
    closes = [data[t][1] for t in ts]
    t_to_idx = {t: i for i, t in enumerate(ts)}

    prefix_marks = [0.0] * (len(ts) + 1)
    for i, m in enumerate(marks):
        prefix_marks[i + 1] = prefix_marks[i] + m

    n_lr = max(len(closes) - 1, 0)
    prefix_lr = [0.0] * (n_lr + 1)
    prefix_lr2 = [0.0] * (n_lr + 1)
    for i in range(1, len(closes)):
        r = math.log(closes[i] / closes[i - 1])
        prefix_lr[i] = prefix_lr[i - 1] + r
        prefix_lr2[i] = prefix_lr2[i - 1] + r * r

    gaps = 0
    for i in range(1, len(ts)):
        if ts[i] - ts[i - 1] != 60:
            gaps += 1

    return {
        "ts": ts, "marks": marks, "closes": closes, "t_to_idx": t_to_idx,
        "prefix_marks": prefix_marks, "prefix_lr": prefix_lr, "prefix_lr2": prefix_lr2,
        "gaps": gaps,
    }


def trailing_sigma_bp(series: dict, j: int, window: int) -> float:
    """Mirrors updown.rs trailing_sigma_bp: sample stdev of log returns, bp. j = end index, inclusive."""
    n = min(j + 1, window + 1)
    if n < 4:
        return 0.0
    start_idx = j - n + 1
    lo, hi = start_idx, j
    count = hi - lo
    if count <= 0:
        return 0.0
    prefix_lr, prefix_lr2 = series["prefix_lr"], series["prefix_lr2"]
    sum_r = prefix_lr[hi] - prefix_lr[lo]
    sum_r2 = prefix_lr2[hi] - prefix_lr2[lo]
    if count <= 1:
        return 0.0
    var = (sum_r2 - sum_r * sum_r / count) / (count - 1)
    return math.sqrt(max(var, 0.0)) * 1e4


def sigma_bp_at(series: dict, j: int) -> float:
    """Mirrors updown.rs: vol_floor_bp(slow, samples, param).max(fast). No arm-time param here —
    the cold-start (samples < SIGMA_SLOW_MIN) branch falls back to the fast window instead,
    which is what the live floor degrades toward anyway (see module docstring)."""
    fast = trailing_sigma_bp(series, j, VOL_FAST_WINDOW)
    slow = trailing_sigma_bp(series, j, SIGMA_SLOW_WINDOW)
    samples = j + 1
    floor = slow if (samples >= SIGMA_SLOW_MIN and slow > 0.0) else fast
    return max(floor, fast)


def norm_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def range_avg(series: dict, lo_t: float, hi_t: float) -> tuple[int, float | None]:
    """(count, avg mark) over minute keys in [lo_t, hi_t)."""
    ts = series["ts"]
    lo = bisect.bisect_left(ts, lo_t)
    hi = bisect.bisect_left(ts, hi_t)
    cnt = hi - lo
    if cnt <= 0:
        return 0, None
    total = series["prefix_marks"][hi] - series["prefix_marks"][lo]
    return cnt, total / cnt


def window_ready(series: dict, start: int, end: int) -> bool:
    """All minute keys in [start-60, end) present — required for a clean, gap-free replay."""
    t_to_idx = series["t_to_idx"]
    if (start - 60) not in t_to_idx:
        return False
    t = start
    while t < end:
        if t not in t_to_idx:
            return False
        t += 60
    return True


def iter_window_starts(series: dict, duration: int):
    ts = series["ts"]
    if len(ts) < 2:
        return
    t_min, t_max = ts[0], ts[-1]
    s0 = ((t_min + 60 + duration - 1) // duration) * duration
    s_last = ((t_max - duration + 60) // duration) * duration
    s = s0
    while s <= s_last:
        yield s, s + duration
        s += duration


# ---------------------------------------------------------------------------
# bucket helpers
# ---------------------------------------------------------------------------

def rem_bucket_idx(rem: float):
    for i, (_, lo, hi) in enumerate(REM_BUCKETS):
        if lo <= rem < hi:
            return i
    return "over"  # >= 600s, informational-only


def safety_bucket_idx(safety: float):
    for i, (_, lo, hi) in enumerate(SAFETY_BUCKETS):
        if lo <= safety < hi:
            return i
    return None


# ---------------------------------------------------------------------------
# one window replay — walks the model's own math at STEP_S resolution and
# records first-crossing events into the shared aggregator
# ---------------------------------------------------------------------------

def simulate_window(series: dict, guard_bp: float, start: int, end: int, agg: dict) -> None:
    idx = series["t_to_idx"]
    marks = series["marks"]
    ref_px = marks[idx[start - 60]]

    cnt, avg_full = range_avg(series, start, end)
    winner = "up" if (avg_full is not None and avg_full >= ref_px) else "down"

    visited_bucket: set = set()
    visited_k: set = set()
    visited_j: set = set()
    visited_p: set = set()

    t_now = start + 60
    last_t = end - 15
    while t_now <= last_t:
        current_minute = (t_now // 60) * 60
        j = idx[current_minute]
        spot = marks[j]

        cutoff = min(t_now - BANKED_LAG_S, end)
        b_cnt, banked_avg = range_avg(series, start, cutoff)
        rem = end - t_now
        if rem <= 0:
            break
        if b_cnt == 0 or banked_avg is None:
            banked_avg, banked_s, window_len = spot, 0.0, rem
        else:
            banked_s = b_cnt * 60.0
            window_len = banked_s + rem
        if window_len <= 0:
            t_now += STEP_S
            continue

        banked_margin_bp = (banked_avg / ref_px - 1.0) * 1e4 * (banked_s / window_len)
        sig_bp = sigma_bp_at(series, j)
        sig_frac = sig_bp / 1e4
        rem_min_term = math.sqrt(max(rem / 60.0, 0.02) / 3.0)
        cushion_bp = guard_bp + sig_bp * rem_min_term * (rem / window_len)
        safety = abs(banked_margin_bp) / max(cushion_bp, 1e-9)
        banked_side = "up" if banked_margin_bp > 0 else ("down" if banked_margin_bp < 0 else None)

        breakeven = (ref_px * window_len - banked_avg * banked_s) / rem
        sig_avg = sig_frac * rem_min_term
        if sig_avg > 1e-12 and breakeven > 0 and spot > 0:
            p_up = 1.0 - norm_cdf(math.log(breakeven / spot) / sig_avg)
        else:
            p_up = 1.0 if breakeven < spot else (0.0 if breakeven > spot else 0.5)
        claimed_p = max(p_up, 1.0 - p_up)
        claimed_side = "up" if p_up >= 0.5 else "down"

        rb = rem_bucket_idx(rem)

        if banked_side is not None:
            sb = safety_bucket_idx(safety)
            if sb is not None and sb not in visited_bucket:
                visited_bucket.add(sb)
                cell = agg["bucket"].setdefault((sb, rb), [0, 0])
                cell[0] += 1
                if banked_side != winner:
                    cell[1] += 1

            for k in K_GRID:
                if k not in visited_k and safety >= k:
                    visited_k.add(k)
                    cell = agg["k"][k].setdefault(rb, [0, 0])
                    cell[0] += 1
                    if banked_side != winner:
                        cell[1] += 1

            for J in J_LIST:
                if J not in visited_j:
                    cushion_j = cushion_bp + J * (rem / window_len)
                    safety_j = abs(banked_margin_bp) / max(cushion_j, 1e-9)
                    if safety_j >= 1.0:
                        visited_j.add(J)
                        cell = agg["j"][J].setdefault(rb, [0, 0])
                        cell[0] += 1
                        if banked_side != winner:
                            cell[1] += 1

        for p_th in P_THRESH:
            if p_th not in visited_p and claimed_p >= p_th:
                visited_p.add(p_th)
                cell = agg["p"].setdefault(p_th, [0, 0])
                cell[0] += 1
                if claimed_side == winner:
                    cell[1] += 1

        t_now += STEP_S


def new_agg() -> dict:
    return {
        "bucket": {}, "p": {},
        "k": {k: {} for k in K_GRID},
        "j": {J: {} for J in J_LIST},
        "windows_total": 0, "windows_used": 0, "windows_skipped": 0,
    }


def run_symbol_duration(series: dict, guard_bp: float, duration: int) -> dict:
    agg = new_agg()
    for start, end in iter_window_starts(series, duration):
        agg["windows_total"] += 1
        if not window_ready(series, start, end):
            agg["windows_skipped"] += 1
            continue
        simulate_window(series, guard_bp, start, end, agg)
        agg["windows_used"] += 1
    return agg


def naive_reversal_rate(series: dict, duration: int) -> tuple[int, int]:
    """Pipeline sanity check, independent of the cushion/safety math entirely:
    does the sign of (avg-so-far vs ref) at 50% elapsed match the sign of the
    full-window average vs ref? This has nothing to do with banked_decided —
    it's just "does this dataset actually contain windows that move against
    their own early trend at all". If the main flip tables come back near 0%
    at safety>=1 while this comes back near 0% too, something in the replay
    is broken; if this is double-digit and the safety-gated tables are still
    ~0%, that's the cushion doing its job on pure Binance self-consistency,
    not a broken pipeline. Returns (total, reversals).
    """
    total, reversals = 0, 0
    for start, end in iter_window_starts(series, duration):
        if not window_ready(series, start, end):
            continue
        idx = series["t_to_idx"]
        ref_px = series["marks"][idx[start - 60]]
        mid = start + duration // 2
        cnt1, avg1 = range_avg(series, start, mid)
        _cnt2, avg_full = range_avg(series, start, end)
        if cnt1 == 0 or avg1 is None or avg_full is None:
            continue
        total += 1
        if (avg1 >= ref_px) != (avg_full >= ref_px):
            reversals += 1
    return total, reversals


# ---------------------------------------------------------------------------
# report
# ---------------------------------------------------------------------------

def human(t: int) -> str:
    return datetime.fromtimestamp(t, tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def fmt_cell(n: int, flips: int) -> str:
    if n == 0:
        return "   n=0          "
    pct = 100.0 * flips / n
    return f"n={n:<5d} flip={pct:5.1f}%"


def print_coverage(symbol: str, series: dict, fetch_stats: dict) -> None:
    ts = series["ts"]
    if not ts:
        print(f"{symbol}: NO DATA")
        return
    days = (ts[-1] - ts[0]) / 86400.0
    print(f"{symbol}: {human(ts[0])} -> {human(ts[-1])}  ({days:.1f}d, {len(ts)} minutes, "
          f"{series['gaps']} gap(s) >1min, +{fetch_stats.get('appended', 0)} fetched this run)")


def print_flip_table(symbol: str, dur_label: str, guard_bp: float, agg: dict) -> None:
    print(f"\n--- {symbol} / {dur_label}  (guard={guard_bp:.1f}bp, "
          f"windows: total={agg['windows_total']} used={agg['windows_used']} "
          f"skipped(gap)={agg['windows_skipped']}) ---")
    header = f"{'safety':<10s} {'gauss 1-tail':>13s}"
    for lbl, _, _ in REM_BUCKETS:
        header += f"  {lbl:>20s}"
    header += f"  {REM_OVER_LABEL:>20s}"
    print(header)
    for i, (lbl, lo, _hi) in enumerate(SAFETY_BUCKETS):
        gauss = 1.0 - norm_cdf(lo)
        row = f"{lbl:<10s} {gauss * 100:>12.2f}%"
        for rb in range(len(REM_BUCKETS)):
            n, flips = agg["bucket"].get((i, rb), [0, 0])
            row += f"  {fmt_cell(n, flips):>20s}"
        n, flips = agg["bucket"].get((i, "over"), [0, 0])
        row += f"  {fmt_cell(n, flips):>20s}"
        print(row)


def print_p_table(symbol: str, dur_label: str, agg: dict) -> None:
    print(f"\n  p_up calibration (claimed_p = max(p_up,1-p_up), first-crossing per threshold):")
    print(f"  {'threshold':<10s} {'n':>6s} {'win%':>8s}")
    for p_th in P_THRESH:
        n, wins = agg["p"].get(p_th, [0, 0])
        win_pct = f"{100.0 * wins / n:6.1f}%" if n > 0 else "   n/a"
        print(f"  >={p_th:<8.2f} {n:>6d} {win_pct:>8s}")


def fit_grid(agg_key: dict, grid: list[float]) -> tuple[float | None, list]:
    """agg_key: {grid_value: {rem_bucket_idx (0..3): [n, flips]}}. Returns (best, rows)."""
    core = list(range(len(REM_BUCKETS)))
    best = None
    rows = []
    for v in grid:
        cells = agg_key[v]
        ok = True
        any_data = False
        detail = []
        for rb in core:
            n, flips = cells.get(rb, [0, 0])
            if n > 0:
                any_data = True
            flip_pct = (100.0 * flips / n) if n > 0 else None
            passed = (n >= FIT_MIN_N) and (flip_pct is not None) and (flip_pct < FIT_FLIP_MAX_PCT)
            ok = ok and passed
            detail.append((n, flip_pct, passed))
        rows.append((v, detail, ok, any_data))
        if ok and best is None:
            best = v
    return best, rows


def print_fit_section(symbol: str, dur_label: str, agg: dict) -> tuple[float | None, float | None]:
    print(f"\n  FIT — smallest multiplicative k with flip<{FIT_FLIP_MAX_PCT:.0f}% "
          f"and n>={FIT_MIN_N} in every rem bucket (0-60,60-120,120-300,300-600):")
    best_k, k_rows = fit_grid(agg["k"], K_GRID)
    for v, detail, ok, any_data in k_rows:
        cells = "  ".join(
            (f"n={n:<4d} {p:4.1f}%{'*' if not passed else ' '}" if n > 0 else "n=0        ")
            for n, p, passed in detail
        )
        mark = "  <= PASS" if ok else ""
        print(f"    k={v:<4.2f}  {cells}{mark}")
    if best_k is not None:
        print(f"    => k={best_k:.2f}")
    else:
        print(f"    => not achieved within k<=3.00 (thin/failing rem bucket persists — see * above)")

    print(f"\n  FIT — smallest additive jump J (bp) with flip<{FIT_FLIP_MAX_PCT:.0f}% "
          f"and n>={FIT_MIN_N} in every rem bucket, at safety'=|banked|/(cushion+J*rem/window)>=1.0:")
    best_j, j_rows = fit_grid(agg["j"], J_LIST)
    for v, detail, ok, any_data in j_rows:
        cells = "  ".join(
            (f"n={n:<4d} {p:4.1f}%{'*' if not passed else ' '}" if n > 0 else "n=0        ")
            for n, p, passed in detail
        )
        mark = "  <= PASS" if ok else ""
        print(f"    J={v:<4.0f}  {cells}{mark}")
    if best_j is not None:
        print(f"    => J={best_j:.0f}bp")
    else:
        print(f"    => not achieved within J<=50bp (thin/failing rem bucket persists — see * above)")

    return best_k, best_j


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--symbols", nargs="+", default=SYMBOLS)
    ap.add_argument("--days", type=float, default=TARGET_DAYS)
    ap.add_argument("--no-fetch", action="store_true", help="use the on-disk cache as-is, no network")
    args = ap.parse_args()

    print(f"R6 tail-flip study — target lookback {args.days:.0f}d, symbols {args.symbols}\n")

    series_by_symbol: dict[str, dict] = {}
    print("COVERAGE")
    for sym in args.symbols:
        fetch_stats: dict = {}
        if not args.no_fetch:
            try:
                fetch_stats = extend_corpus(sym, args.days)
            except Exception as e:  # noqa: BLE001 — report and continue with what's cached
                print(f"{sym}: fetch error ({e}) — falling back to on-disk cache")
        data = load_cache(sym)
        series = build_series(data)
        series_by_symbol[sym] = series
        print_coverage(sym, series, fetch_stats)

    print("\nWINDOWS")
    results: dict[tuple[str, str], dict] = {}
    for sym in args.symbols:
        series = series_by_symbol[sym]
        if not series["ts"]:
            continue
        guard_bp = GUARD_BP.get(sym, 8.0)
        for dur_label, duration in DURATIONS:
            agg = run_symbol_duration(series, guard_bp, duration)
            results[(sym, dur_label)] = agg
            nrt, nrr = naive_reversal_rate(series, duration)
            nr_pct = f"{100.0 * nrr / nrt:.1f}%" if nrt else "n/a"
            print(f"  {sym}/{dur_label}: total={agg['windows_total']} used={agg['windows_used']} "
                  f"skipped(gap)={agg['windows_skipped']}   "
                  f"[sanity: naive 50%-elapsed reversal rate = {nr_pct} of {nrt} windows — "
                  f"pipeline check, unrelated to the cushion; see naive_reversal_rate() docstring]")

    print("\n" + "=" * 100)
    print("FLIP-RATE TABLES  (n = # windows whose safety first entered this bucket at this rem; "
          "flip% = banked side lost)")
    print("Gaussian 1-tail = 1-norm_cdf(bucket lower edge) — what the model implicitly claims the "
          "flip probability is at that safety multiple, IF returns were Gaussian.")
    print("=" * 100)
    for sym in args.symbols:
        for dur_label, _duration in DURATIONS:
            agg = results.get((sym, dur_label))
            if agg is None:
                continue
            print_flip_table(sym, dur_label, GUARD_BP.get(sym, 8.0), agg)

    print("\n" + "=" * 100)
    print("P_UP CALIBRATION  (win% = claimed side matched the window's actual winner)")
    print("=" * 100)
    for sym in args.symbols:
        for dur_label, _duration in DURATIONS:
            agg = results.get((sym, dur_label))
            if agg is None:
                continue
            print(f"\n--- {sym} / {dur_label} ---")
            print_p_table(sym, dur_label, agg)

    print("\n" + "=" * 100)
    print("FIT RECOMMENDATION  (* marks a rem bucket failing flip<3% or n<30 at that grid value)")
    print("=" * 100)
    fits: dict[tuple[str, str], tuple[float | None, float | None]] = {}
    for sym in args.symbols:
        for dur_label, _duration in DURATIONS:
            agg = results.get((sym, dur_label))
            if agg is None:
                continue
            print(f"\n--- {sym} / {dur_label} ---")
            fits[(sym, dur_label)] = print_fit_section(sym, dur_label, agg)

    print("\n" + "=" * 100)
    print("SUMMARY — multiplicative k vs additive J, per symbol/duration")
    print("=" * 100)
    for (sym, dur_label), (best_k, best_j) in fits.items():
        k_txt = f"k={best_k:.2f}" if best_k is not None else "k: not achieved <=3.00"
        j_txt = f"J={best_j:.0f}bp" if best_j is not None else "J: not achieved <=50bp"
        if best_k is not None and best_j is not None:
            # Compare added cushion width at a representative rem=180s (mid of 120-300s bucket),
            # window~=duration (banked share small there is the riskiest regime for gating).
            note = ("multiplicative wins (achieved at a lower grid step)" if K_GRID.index(best_k) <= J_LIST.index(best_j)
                    else "additive wins (achieved at a lower grid step)")
        else:
            note = "at least one parameterization did not reach the target within its grid — see FIT detail above"
        print(f"  {sym:<9s} {dur_label:<4s}  {k_txt:<28s} {j_txt:<20s}  {note}")

    print("\nCaveats: buckets are first-crossing events (one per window per bucket, tracked without "
          "resetting on a mid-window banked-side flip — see module docstring). n<30 cells are not "
          "reliable; treat any FIT recommendation drawn from thin cells as provisional. The 600s+ rem "
          "column is informational only and excluded from the FIT pass/fail criterion.\n"
          "SCOPE: 'winner' here is the model's OWN Binance-kline settlement math (avg of the full "
          "window's per-minute marks vs ref), never Chainlink/Polymarket's actual resolution — by "
          "design (see module docstring). If a live loss shows banked_decided true right up to close "
          "yet this study still reports ~0% flip at that safety/rem cell, the live loss was most "
          "likely basis error (Binance vs Chainlink), not a genuine reversal of the Binance remaining "
          "path — that's R1's measurement, not this one. Cross-check any specific incident against "
          "~/.pmt/corpus/chainlink-{symbol}.jsonl before concluding the cushion itself is miscalibrated.")


if __name__ == "__main__":
    main()
