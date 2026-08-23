"""Resolution-farmer study, stage 9: can ANY filter set rescue this?

Sweeps the filter grid for a subset whose realized win rate beats its own breakeven with
n >= 200, then re-checks the winner out of sample on a date holdout. A grid this wide will
always produce something that looks profitable in-sample -- the holdout is the whole point,
and a candidate that does not survive it is noise, not an edge.
"""

from __future__ import annotations

import datetime as dt
import itertools
import json
import math
import os
import sys
from pathlib import Path

OUT = Path(os.path.expanduser("~/.pmt/resfarm"))


def wilson_lo(k, n, z=1.96):
    if n == 0:
        return 0.0
    p = k / n
    d = 1 + z * z / n
    c = p + z * z / (2 * n)
    s = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n))
    return (c - s) / d


def econ(r, slip):
    entry = min(r["p_fav"] + slip, 1.0 - r["tick"])
    fee = r["fee_rate"] * entry * (1 - entry)
    cap = entry + fee
    payoff = 0.5 if r["split"] else (1.0 if r["won"] else 0.0)
    return payoff - cap, cap


def score(sel, slip):
    if not sel:
        return None
    pnl = cap = 0.0
    w = 0
    for r in sel:
        a, c = econ(r, slip)
        pnl += a
        cap += c
        w += 1 if r["won"] else 0
    n = len(sel)
    roi = pnl / cap
    # bootstrap-free lower bound on ROI via the win-rate lower bound at the mean entry price
    mean_entry = cap / n
    roi_lo = (wilson_lo(w, n) - mean_entry) / mean_entry
    return {"n": n, "wr": w / n, "roi": roi, "roi_lo": roi_lo, "mean_entry": mean_entry,
            "pnl_per_1k_notional": roi * 1000}


def main():
    rows = json.load((OUT / "entries.json").open())
    slip = float(os.environ.get("RF_SLIP", "0.002"))
    base = [r for r in rows if r["cat"] != "crypto-updown" and r["tradable"]
            and (r["won"] is not None or r["split"])]
    days = sorted({dt.datetime.fromtimestamp(r["end_ts"], dt.timezone.utc).date().isoformat()
                   for r in base})
    cut = days[len(days) // 2]
    print(f"{len(base)} buyable entries over {len(days)} days; holdout cut at {cut}\n")

    def is_train(r):
        return dt.datetime.fromtimestamp(r["end_ts"], dt.timezone.utc).date().isoformat() < cut

    train = [r for r in base if is_train(r)]
    test = [r for r in base if not is_train(r)]
    print(f"train n={len(train)}  test n={len(test)}\n")

    cats = sorted({r["cat"] for r in base})
    grid = {
        "delta_h": [6.0, 4.0, 3.0, 2.0, 1.0],
        "px_lo": [0.90, 0.93, 0.95, 0.97, 0.985, 0.995],
        "px_hi": [0.95, 0.97, 0.985, 0.995, 0.9986],
        "min_vol": [10_000, 25_000, 50_000, 100_000, 250_000],
        "cat": [None] + cats,
        "min_distinct": [0, 3, 11],
    }
    keys = list(grid)
    results = []
    for combo in itertools.product(*(grid[k] for k in keys)):
        f = dict(zip(keys, combo))
        if f["px_hi"] <= f["px_lo"]:
            continue
        sel = [r for r in train
               if r["delta_h"] == f["delta_h"]
               and f["px_lo"] <= r["p_fav"] < f["px_hi"]
               and r["vol"] >= f["min_vol"]
               and (f["cat"] is None or r["cat"] == f["cat"])
               and r["distinct_late"] >= f["min_distinct"]]
        s = score(sel, slip)
        if s and s["n"] >= 200:
            results.append((f, s))

    print(f"filter combinations with n>=200 in train: {len(results)}")
    results.sort(key=lambda x: -x[1]["roi"])
    print("\n=== top 12 IN-SAMPLE by ROI/trade ===")
    print(f"{'Δ':>4} {'px band':>16} {'vol>=':>9} {'cat':<14} {'dl':>3} | "
          f"{'n':>5} {'win%':>7} {'ROI':>8} {'ROI lo95':>9}")
    for f, s in results[:12]:
        print(f"{f['delta_h']:>4.0f} [{f['px_lo']:.3f},{f['px_hi']:.4f}) {f['min_vol']:>9,} "
              f"{str(f['cat']):<14} {f['min_distinct']:>3} | {s['n']:>5} {s['wr']*100:>6.2f}% "
              f"{s['roi']*100:>7.2f}% {s['roi_lo']*100:>8.2f}%")

    print("\n=== the same 12, re-scored OUT OF SAMPLE ===")
    print(f"{'Δ':>4} {'px band':>16} {'vol>=':>9} {'cat':<14} | {'n':>5} {'win%':>7} {'ROI':>8}")
    for f, s in results[:12]:
        sel = [r for r in test
               if r["delta_h"] == f["delta_h"]
               and f["px_lo"] <= r["p_fav"] < f["px_hi"]
               and r["vol"] >= f["min_vol"]
               and (f["cat"] is None or r["cat"] == f["cat"])
               and r["distinct_late"] >= f["min_distinct"]]
        t = score(sel, slip)
        if t:
            print(f"{f['delta_h']:>4.0f} [{f['px_lo']:.3f},{f['px_hi']:.4f}) {f['min_vol']:>9,} "
                  f"{str(f['cat']):<14} | {t['n']:>5} {t['wr']*100:>6.2f}% {t['roi']*100:>7.2f}%")
        else:
            print(f"{f['delta_h']:>4.0f} [{f['px_lo']:.3f},{f['px_hi']:.4f}) {f['min_vol']:>9,} "
                  f"{str(f['cat']):<14} |  (empty out of sample)")

    pos = [x for x in results if x[1]["roi"] > 0]
    poslo = [x for x in results if x[1]["roi_lo"] > 0]
    print(f"\nin-sample positive-ROI filter sets: {len(pos)}/{len(results)}")
    print(f"in-sample sets whose 95% LOWER bound is positive: {len(poslo)}/{len(results)}")

    print("\n=== whole-corpus (no split) reference cells with n>=200 ===")
    ref = []
    for combo in itertools.product(*(grid[k] for k in keys)):
        f = dict(zip(keys, combo))
        if f["px_hi"] <= f["px_lo"]:
            continue
        sel = [r for r in base
               if r["delta_h"] == f["delta_h"]
               and f["px_lo"] <= r["p_fav"] < f["px_hi"]
               and r["vol"] >= f["min_vol"]
               and (f["cat"] is None or r["cat"] == f["cat"])
               and r["distinct_late"] >= f["min_distinct"]]
        s = score(sel, slip)
        if s and s["n"] >= 200:
            ref.append((f, s))
    ref.sort(key=lambda x: -x[1]["roi"])
    for f, s in ref[:10]:
        print(f"{f['delta_h']:>4.0f} [{f['px_lo']:.3f},{f['px_hi']:.4f}) {f['min_vol']:>9,} "
              f"{str(f['cat']):<14} dl>={f['min_distinct']:<3} | n={s['n']:>5} "
              f"wr={s['wr']*100:>6.2f}% ROI={s['roi']*100:>7.3f}% lo95={s['roi_lo']*100:>7.2f}%")
    print(f"\nwhole-corpus cells with n>=200: {len(ref)}; positive ROI: "
          f"{sum(1 for x in ref if x[1]['roi']>0)}; positive 95% lower bound: "
          f"{sum(1 for x in ref if x[1]['roi_lo']>0)}")


if __name__ == "__main__":
    main()
