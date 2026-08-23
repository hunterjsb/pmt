#!/usr/bin/env python3
"""Robustness pass over the carve-out A/B runs.

The A/B table reports one number per variant over one night. That number is
worth exactly as much as its concentration allows: `analysis/correlation_
study.md` already showed this corpus punishing policies whose whole point
estimate was one incident window wearing the policy's name, and the k-curve
here is NOT monotone (k1.10 and k1.15 lose money, k1.25 wins big), which is
the classic signature of a cliff rather than an effect.

So for every variant this asks four questions the headline can't:

  concentration  how much of the delta is the single best window, the top 3,
                 the top 5
  trimmed        the delta with those windows removed
  sign test      of the windows the variant moved, how many moved the right
                 way — a p-value that does not care about magnitude
  bootstrap      2.5/97.5 percentiles of the delta, resampling moved windows

A variant whose delta survives the trim and whose sign test clears 0.05 is a
policy. One that doesn't is a story about a couple of trades.
"""
import argparse
import json
import math
import os
import random

WORK = "/var/home/hunter/Desktop/code/pmt-carveout-work"


def load(path):
    return {r["slug"]: r for r in map(json.loads, open(path))
            if "aggregate" not in r["slug"]}


def deltas(base, run):
    out = []
    for slug, r in run.items():
        d = (r["sim"]["pnl"] or 0.0) - (base[slug]["sim"]["pnl"] or 0.0)
        if abs(d) > 1e-9:
            out.append((d, slug))
    return sorted(out)


def sign_test(vals):
    """Two-sided binomial p that positives and negatives are 50/50."""
    pos = sum(1 for v in vals if v > 0)
    n = sum(1 for v in vals if v != 0)
    if n == 0:
        return 1.0, 0, 0
    k = max(pos, n - pos)
    tail = sum(math.comb(n, i) for i in range(k, n + 1)) / 2 ** n
    return min(1.0, 2 * tail), pos, n - pos


def bootstrap(vals, iters=20000, seed=7):
    if not vals:
        return 0.0, 0.0
    rng = random.Random(seed)
    n = len(vals)
    sums = sorted(sum(rng.choice(vals) for _ in range(n)) for _ in range(iters))
    return sums[int(0.025 * iters)], sums[int(0.975 * iters)]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dir", default=os.path.join(WORK, "ab"))
    ap.add_argument("--variants", default=None)
    a = ap.parse_args()

    base = load(os.path.join(a.dir, "run-base.jsonl"))
    names = (a.variants.split(",") if a.variants else
             sorted(f[4:-6] for f in os.listdir(a.dir)
                    if f.startswith("run-") and f.endswith(".jsonl")
                    and f != "run-base.jsonl"))

    hdr = (f"{'variant':<18} {'delta':>9} {'moved':>6} {'-top1':>9} {'-top3':>9} "
           f"{'-top5':>9} {'W':>4} {'L':>4} {'sign p':>7} {'boot 2.5%':>10} {'97.5%':>9}")
    print(hdr + "\n" + "-" * len(hdr))
    for name in names:
        path = os.path.join(a.dir, f"run-{name}.jsonl")
        if not os.path.exists(path):
            continue
        ds = deltas(base, load(path))
        vals = [d for d, _ in ds]
        total = sum(vals)
        top = sorted(vals, reverse=True)
        trim = lambda n: total - sum(top[:n])
        p, pos, neg = sign_test(vals)
        lo, hi = bootstrap(vals)
        print(f"{name:<18} {total:>+9.2f} {len(vals):>6} {trim(1):>+9.2f} "
              f"{trim(3):>+9.2f} {trim(5):>+9.2f} {pos:>4} {neg:>4} "
              f"{p:>7.3f} {lo:>+10.2f} {hi:>+9.2f}")

    print("\ntop movers per variant (delta, window):")
    for name in names:
        path = os.path.join(a.dir, f"run-{name}.jsonl")
        if not os.path.exists(path):
            continue
        ds = deltas(base, load(path))
        best = sorted(ds, reverse=True)[:4]
        worst = ds[:3]
        print(f"\n  {name}")
        for d, s in best:
            print(f"    +{d:>8.2f}  {s}")
        for d, s in worst:
            print(f"    {d:>+9.2f}  {s}")


if __name__ == "__main__":
    main()
