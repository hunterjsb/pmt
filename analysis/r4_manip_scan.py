"""
R4 — near-settlement manipulation-signature scan.

Literature (Dai/Jia/Yu 2026) on 5m BTC prediction markets finds attacks that push spot
against the currently-winning ("banked") side in the closing seconds, then let it revert —
exploiting a last-print settlement snapshot. Our own settlement moved to a Chainlink TWAP
window (30s on 5m, 60s on 15m/4h) ~2026-08-07, which should blunt a pure last-tick attack,
so this scan looks for the residual shape in OUR book tape rather than assuming the paper's
mechanism transfers unchanged (ROADMAP R4).

Per window (a Polymarket up/down slug), over the final 90s at ~1s cadence we flag:
  (a) reversal      — spot excursion against the banked side that mostly reverts before close
  (b) book_spike     — a book-price (up_mid) jump not backed by a matching spot move
  (c) oneside_burst  — a run of same-direction Polymarket print flow (up_tbuy/tsell, dn_*)
plus max adverse excursion (MAE) in the final 90s and whether the outcome flipped relative
to the banked side 60s before close.

Output is idempotent per slug (dedupe-by-slug, not append-only): rerun nightly, each slug's
row is recomputed fresh so outcomes that resolve after the fact (outcomes.jsonl grows) get
picked up on the next pass without hand-pruning.
"""
import json
import math
import re
import time
from collections import defaultdict
from pathlib import Path
from statistics import pstdev

BOOK_TAPE = Path.home() / ".pmt" / "engine" / "book-tape.jsonl"
OUTCOMES = Path.home() / ".pmt" / "corpus" / "outcomes.jsonl"
OUT_PATH = Path.home() / ".pmt" / "corpus" / "r4-flags.jsonl"

SCAN_VERSION = "r4v1"

# ROADMAP 2026-08-23 measured per-symbol vol (bp/min); unlisted symbols fall back to the
# XRP figure as a conservative (widest) default rather than pretending BTC-like calm.
SIGMA_BP_PER_MIN = {"btc": 4.0, "eth": 6.5, "sol": 11.0, "xrp": 21.0, "doge": 17.0}
DEFAULT_SIGMA_BP_PER_MIN = 21.0

FINAL_WINDOW_S = 90.0     # the near-settlement scan region the task specifies
FLIP_LOOKBACK_S = 60.0    # "outcome flipped vs its 60s-prior state"
TAPE_COMPLETE_TOL_S = 10.0  # last recorded tick must be within this of window close

# (a) reversal signature
SPIKE_SIGMA_MULT = 2.5     # peak adverse move must clear this many sigma over its own elapsed dt
REVERSION_FRAC = 0.6       # must give back >=60% of the peak excursion by window close
MIN_REVERT_LEAD_S = 5.0    # peak needs >=5s of window left to count as "reverses in seconds"

# (b) book-spike signature
BOOK_JUMP_ABS = 0.04       # tick-to-tick up_mid move, in contract-price units [0,1]
BOOK_JUMP_SPOT_SIGMA_MULT = 0.5  # while the trailing spot move stays under this many sigma
BOOK_SPIKE_TRAIL_S = 10.0  # trailing lookback for "did spot actually support this" (smooths
                            # the single-tick spot staleness seen in the raw feed — spot ticks
                            # arrive slower than book samples, so back-to-back deltas are often
                            # a false zero, not a real "no move")
BOOK_SPIKE_BAND = (0.05, 0.95)  # only score jumps starting from a still-contestable mid price;
                                 # near expiry a decided contract's mid pins near 0/1 and any
                                 # residual bid/ask flutter there (e.g. the ask briefly vanishing)
                                 # is liquidity noise, not a push — excluded so it can't dominate

# (c) one-sided print burst (R8's "consecutive-same-side-flow counter", cheap v1)
BURST_RUN_MIN = 4          # consecutive same-sign print samples
BURST_VOL_MIN = 10.0       # total buy+sell volume across the run, filters dust prints

SLUG_RE = re.compile(r"^([a-z0-9]+)-updown-(\d+)(m|h)-(\d+)$")


def parse_slug(slug):
    m = SLUG_RE.match(slug)
    if not m:
        return None
    sym, num, unit, epoch = m.groups()
    mult = 60 if unit == "m" else 3600
    duration_s = int(num) * mult
    return {
        "symbol": sym,
        "duration_s": duration_s,
        "duration": f"{num}{unit}",
        "window_start": int(epoch),
        "window_end": int(epoch) + duration_s,
    }


def load_outcomes():
    out = {}
    if not OUTCOMES.exists():
        return out
    with open(OUTCOMES) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            d = json.loads(line)
            slug = d.get("slug")
            if not slug:
                continue
            # wallet grading is the only scoreboard (ROADMAP) — prefer it over chainlink-sourced
            prev = out.get(slug)
            if prev is None or (prev.get("source") != "wallet" and d.get("source") == "wallet"):
                out[slug] = d
    return out


def load_book_tape():
    windows = defaultdict(list)
    if not BOOK_TAPE.exists():
        return windows
    with open(BOOK_TAPE) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                d = json.loads(line)
            except json.JSONDecodeError:
                continue
            if d.get("ev") != "book" or "slug" not in d or "t" not in d:
                continue
            windows[d["slug"]].append(d)
    for recs in windows.values():
        recs.sort(key=lambda r: r["t"])
    return windows


def side_of(spot, ref):
    if spot is None or ref is None or ref == 0:
        return None
    if spot > ref:
        return "up"
    if spot < ref:
        return "down"
    return None  # exact tie, no lean


def nearest_rec(recs, target_t, tol_s):
    """Record closest to target_t among those with a valid spot, within tol_s."""
    best, best_dt = None, None
    for r in recs:
        spot = r.get("spot")
        if not spot:
            continue
        dt = abs(r["t"] - target_t)
        if best_dt is None or dt < best_dt:
            best, best_dt = r, dt
    if best is not None and best_dt is not None and best_dt <= tol_s:
        return best
    return None


def bp(a, b_):
    if not b_:
        return None
    return (a - b_) / b_ * 1e4


def detect_reversal(final_recs, sigma_bp_per_min, ref_price):
    """(a) spot excursion against the banked side (as of the start of final90) that
    reverts before window close. Returns (flag, detail, mae_bp, mae_lead_s)."""
    valid = [r for r in final_recs if r.get("spot")]
    if len(valid) < 3:
        return False, None, None, None

    t0_rec = valid[0]
    side0 = side_of(t0_rec["spot"], ref_price)
    if side0 is None:
        return False, None, None, None
    sign = 1.0 if side0 == "down" else -1.0  # adverse = move toward the other side

    close_t = valid[-1]["t"]
    series = []  # (t, adverse_bp)
    for r in valid:
        d_bp = bp(r["spot"], t0_rec["spot"])
        if d_bp is None:
            continue
        series.append((r["t"], sign * d_bp))

    if not series:
        return False, None, None, None

    mae_t, mae_bp = max(series, key=lambda p: p[1])
    mae_bp = max(mae_bp, 0.0)
    mae_lead_s = close_t - mae_t

    # spike test: peak must clear SPIKE_SIGMA_MULT sigma scaled to its own elapsed dt from t0
    elapsed = max(mae_t - t0_rec["t"], 1.0)
    sigma_at_elapsed = sigma_bp_per_min * math.sqrt(elapsed / 60.0)
    is_spike = mae_bp >= SPIKE_SIGMA_MULT * sigma_at_elapsed

    close_adverse = series[-1][1]
    reverted_bp = mae_bp - close_adverse
    reverted_enough = mae_bp > 0 and (reverted_bp / mae_bp) >= REVERSION_FRAC
    had_time = mae_lead_s >= MIN_REVERT_LEAD_S

    flag = bool(is_spike and reverted_enough and had_time)
    detail = {
        "banked_side_at_t0": side0,
        "peak_adverse_bp": round(mae_bp, 2),
        "peak_lead_s": round(mae_lead_s, 2),
        "sigma_at_peak_bp": round(sigma_at_elapsed, 2),
        "reverted_bp": round(reverted_bp, 2),
        "reverted_frac": round(reverted_bp / mae_bp, 3) if mae_bp > 0 else None,
        "is_spike": is_spike,
        "reverted_enough": reverted_enough,
        "had_time_to_revert": had_time,
    }
    return flag, detail, round(mae_bp, 2), round(mae_lead_s, 2)


def _spot_at(spot_series, t):
    """Last known spot at or before t (ffill); spot_series is sorted [(t, spot), ...]."""
    val = None
    for st, sv in spot_series:
        if st > t:
            break
        val = sv
    return val


def detect_book_spike(final_recs, sigma_bp_per_min, lookback_recs):
    """(b) up_mid jump not backed by a matching trailing spot move, while the contract is
    still contestable (excludes decay noise once a window is already effectively decided).
    lookback_recs must cover back to at least final_recs[0].t - BOOK_SPIKE_TRAIL_S so the
    trailing reference near the start of the final-90s stretch isn't starved of history."""
    spot_series = [(r["t"], r["spot"]) for r in lookback_recs if r.get("spot")]
    pts = []
    for r in final_recs:
        ub, ua = r.get("up_bid"), r.get("up_ask")
        if ub is None or ua is None:
            continue
        pts.append((r["t"], (ub + ua) / 2.0))
    if len(pts) < 3 or len(spot_series) < 3:
        return False, None

    worst = None  # (book_jump_abs, spot_sigma_ratio, t, d_spot_bp)
    book_series, spot_delta_series = [], []
    sigma_trail = sigma_bp_per_min * math.sqrt(BOOK_SPIKE_TRAIL_S / 60.0)
    lo, hi = BOOK_SPIKE_BAND
    for (t1, m1), (t2, m2) in zip(pts, pts[1:]):
        d_book = m2 - m1
        book_series.append(d_book)
        spot_now = _spot_at(spot_series, t2)
        spot_then = _spot_at(spot_series, t2 - BOOK_SPIKE_TRAIL_S)
        d_spot_bp = bp(spot_now, spot_then) if spot_now and spot_then else None
        spot_delta_series.append(d_spot_bp or 0.0)
        if not (lo <= m1 <= hi):
            continue  # already effectively decided — flutter here isn't a "push"
        if d_spot_bp is None:
            continue  # no trailing spot reference — can't claim "unsupported", so don't guess
        spot_ratio = abs(d_spot_bp) / sigma_trail if sigma_trail else 0.0
        if abs(d_book) >= BOOK_JUMP_ABS and spot_ratio < BOOK_JUMP_SPOT_SIGMA_MULT:
            cand = (abs(d_book), spot_ratio, t2, d_spot_bp)
            if worst is None or cand[0] > worst[0]:
                worst = cand

    corr = None
    if len(book_series) >= 5 and pstdev(book_series) > 0 and pstdev(spot_delta_series) > 0:
        n = len(book_series)
        mb = sum(book_series) / n
        ms = sum(spot_delta_series) / n
        cov = sum((b - mb) * (s - ms) for b, s in zip(book_series, spot_delta_series)) / n
        corr = cov / (pstdev(book_series) * pstdev(spot_delta_series))

    if worst is None:
        return False, {"max_uncorrelated_jump": None, "corr_book_vs_spot": round(corr, 3) if corr is not None else None}

    jump, spot_ratio, t2, d_spot_bp = worst
    detail = {
        "max_book_jump_abs": round(jump, 3),
        "trailing_spot_bp": round(d_spot_bp, 2) if d_spot_bp is not None else None,
        "spot_sigma_ratio": round(spot_ratio, 3),
        "trail_s": BOOK_SPIKE_TRAIL_S,
        "corr_book_vs_spot": round(corr, 3) if corr is not None else None,
    }
    return True, detail


def detect_print_burst(final_recs):
    """(c) run of same-direction Polymarket print flow (R8 cheap v1)."""
    signed = []  # (t, sign, volume) using (up_tbuy-up_tsell) - (dn_tbuy-dn_tsell)
    has_any_flow_field = False
    for r in final_recs:
        if "up_tbuy" not in r:
            continue
        has_any_flow_field = True
        ub, us = r.get("up_tbuy") or 0.0, r.get("up_tsell") or 0.0
        db, ds = r.get("dn_tbuy") or 0.0, r.get("dn_tsell") or 0.0
        vol = ub + us + db + ds
        if vol <= 0:
            continue
        pressure = (ub - us) - (db - ds)
        if pressure == 0:
            continue
        signed.append((r["t"], 1 if pressure > 0 else -1, vol))

    if not has_any_flow_field:
        return False, None, False  # (flag, detail, has_print_flow)

    best_run, best_vol, best_side = 0, 0.0, None
    run, run_vol, run_side = 0, 0.0, None
    for _, s, v in signed:
        if s == run_side:
            run += 1
            run_vol += v
        else:
            run, run_vol, run_side = 1, v, s
        if run > best_run or (run == best_run and run_vol > best_vol):
            best_run, best_vol, best_side = run, run_vol, run_side

    flag = bool(best_run >= BURST_RUN_MIN and best_vol >= BURST_VOL_MIN)
    detail = {
        "longest_run": best_run,
        "run_volume": round(best_vol, 2),
        "run_side": "up" if best_side == 1 else ("down" if best_side == -1 else None),
        "n_print_samples": len(signed),
    }
    return flag, detail, True


def analyze_window(slug, recs, outcomes, now):
    meta = parse_slug(slug)
    if meta is None:
        return None
    window_end = meta["window_end"]
    if window_end > now:
        return None  # still live — do not score an in-progress window

    valid_recs = [r for r in recs if r.get("spot")]
    ref_price = valid_recs[0]["spot"] if valid_recs else None
    last_t = recs[-1]["t"] if recs else None
    tape_complete = bool(last_t is not None and (window_end - last_t) <= TAPE_COMPLETE_TOL_S)

    sym = meta["symbol"]
    sigma = SIGMA_BP_PER_MIN.get(sym, DEFAULT_SIGMA_BP_PER_MIN)

    final_recs = [r for r in recs if r["t"] >= window_end - FINAL_WINDOW_S and r["t"] <= window_end + 5]

    outc = outcomes.get(slug)
    outcome_winner = outc.get("winner") if outc else None
    outcome_source = outc.get("source") if outc else None

    banked_close = banked_t60 = None
    flip = None
    if ref_price is not None:
        close_rec = nearest_rec(recs, window_end, tol_s=15.0)
        t60_rec = nearest_rec(recs, window_end - FLIP_LOOKBACK_S, tol_s=15.0)
        if close_rec is not None:
            banked_close = side_of(close_rec["spot"], ref_price)
        if t60_rec is not None:
            banked_t60 = side_of(t60_rec["spot"], ref_price)
        if outcome_winner is not None and banked_t60 is not None:
            flip = outcome_winner != banked_t60

    if not tape_complete or ref_price is None or len(final_recs) < 3:
        # not enough final-90s coverage to score signatures, but still record what we know
        # (visible in the corpus so incomplete recording nights don't silently vanish)
        return {
            "slug": slug,
            "symbol": sym,
            "duration": meta["duration"],
            "duration_s": meta["duration_s"],
            "window_start": meta["window_start"],
            "window_end": window_end,
            "computed_at": round(now, 0),
            "n_book_records": len(recs),
            "n_final90_records": len(final_recs),
            "tape_complete": tape_complete,
            "has_print_flow": None,
            "outcome": outcome_winner,
            "outcome_source": outcome_source,
            "banked_side_close": banked_close,
            "banked_side_t60prior": banked_t60,
            "flip_vs_60s_prior": flip,
            "max_adverse_excursion_bp": None,
            "mae_peak_lead_s": None,
            "flag_a_reversal": None,
            "flag_a_detail": None,
            "flag_b_book_spike": None,
            "flag_b_detail": None,
            "flag_c_oneside_burst": None,
            "flag_c_detail": None,
            "any_flag": None,
            "n_flags": None,
            "scan_version": SCAN_VERSION,
            "insufficient_data": True,
        }

    flag_a, detail_a, mae_bp, mae_lead_s = detect_reversal(final_recs, sigma, ref_price)
    lookback_recs = [r for r in recs if r["t"] >= window_end - FINAL_WINDOW_S - BOOK_SPIKE_TRAIL_S]
    flag_b, detail_b = detect_book_spike(final_recs, sigma, lookback_recs)
    flag_c, detail_c, has_print_flow = detect_print_burst(final_recs)

    flags = [bool(flag_a), bool(flag_b), bool(flag_c)]
    return {
        "slug": slug,
        "symbol": sym,
        "duration": meta["duration"],
        "duration_s": meta["duration_s"],
        "window_start": meta["window_start"],
        "window_end": window_end,
        "computed_at": round(now, 0),
        "n_book_records": len(recs),
        "n_final90_records": len(final_recs),
        "tape_complete": tape_complete,
        "has_print_flow": has_print_flow,
        "outcome": outcome_winner,
        "outcome_source": outcome_source,
        "banked_side_close": banked_close,
        "banked_side_t60prior": banked_t60,
        "flip_vs_60s_prior": flip,
        "max_adverse_excursion_bp": mae_bp,
        "mae_peak_lead_s": mae_lead_s,
        "flag_a_reversal": flag_a,
        "flag_a_detail": detail_a,
        "flag_b_book_spike": flag_b,
        "flag_b_detail": detail_b,
        "flag_c_oneside_burst": flag_c,
        "flag_c_detail": detail_c,
        "any_flag": any(flags),
        "n_flags": sum(flags),
        "scan_version": SCAN_VERSION,
        "insufficient_data": False,
    }


def load_existing_flags():
    rows = {}
    if not OUT_PATH.exists():
        return rows
    with open(OUT_PATH) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                d = json.loads(line)
            except json.JSONDecodeError:
                continue
            slug = d.get("slug")
            if slug:
                rows[slug] = d
    return rows


def write_flags(rows_by_slug):
    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    # stable order: window_start ascending, easiest to eyeball / diff across nightly runs
    ordered = sorted(rows_by_slug.values(), key=lambda d: (d.get("window_start") or 0, d.get("slug") or ""))
    with open(OUT_PATH, "w") as f:
        for row in ordered:
            f.write(json.dumps(row) + "\n")


def summarize(rows):
    scored = [r for r in rows if not r.get("insufficient_data")]
    by_dur = defaultdict(list)
    for r in scored:
        by_dur[r["duration"]].append(r)

    print(f"\n=== R4 manipulation-signature scan ({SCAN_VERSION}) ===")
    print(f"windows in book-tape: {len(rows)}  |  scored (complete final-90s tape): {len(scored)}  "
          f"|  skipped (incomplete/live): {len(rows) - len(scored)}")

    for dur in sorted(by_dur, key=lambda d: int(d[:-1])):
        ws = by_dur[dur]
        n = len(ws)
        a = sum(1 for r in ws if r["flag_a_reversal"])
        b = sum(1 for r in ws if r["flag_b_book_spike"])
        c = sum(1 for r in ws if r["flag_c_oneside_burst"])
        c_eligible = sum(1 for r in ws if r["has_print_flow"])
        any_f = sum(1 for r in ws if r["any_flag"])
        maes = [r["max_adverse_excursion_bp"] for r in ws if r["max_adverse_excursion_bp"] is not None]
        flips = [r["flip_vs_60s_prior"] for r in ws if r["flip_vs_60s_prior"] is not None]
        avg_mae = sum(maes) / len(maes) if maes else float("nan")
        flip_rate = sum(flips) / len(flips) if flips else float("nan")
        print(f"\n[{dur}] n={n}")
        print(f"  (a) reversal:      {a}/{n} ({100*a/n:.1f}%)")
        print(f"  (b) book_spike:    {b}/{n} ({100*b/n:.1f}%)")
        print(f"  (c) oneside_burst: {c}/{c_eligible} of print-instrumented windows"
              if c_eligible else "  (c) oneside_burst: 0/0 (no print-flow-instrumented windows yet)")
        print(f"  any flag:          {any_f}/{n} ({100*any_f/n:.1f}%)")
        print(f"  avg MAE final90:   {avg_mae:.2f} bp  (n={len(maes)})")
        print(f"  flip-vs-T-60s:     {100*flip_rate:.1f}% (n={len(flips)} with resolved outcome)")

    print()


def main():
    now = time.time()
    book_windows = load_book_tape()
    outcomes = load_outcomes()
    existing = load_existing_flags()

    computed = 0
    for slug, recs in book_windows.items():
        row = analyze_window(slug, recs, outcomes, now)
        if row is None:
            continue
        existing[slug] = row  # dedupe-by-slug: fresh recompute always wins
        computed += 1

    write_flags(existing)
    print(f"scanned {len(book_windows)} book-tape windows, wrote {computed} rows "
          f"({len(existing)} total in {OUT_PATH})")
    summarize(list(existing.values()))


if __name__ == "__main__":
    main()
