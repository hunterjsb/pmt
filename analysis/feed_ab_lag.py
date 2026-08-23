#!/usr/bin/env python3
"""RTDS receive lag: what the staleness gate actually measures, and how much
of the feed A/B moves if the stream-fed tolerance were 3s instead of 5s.

  uv run --project pmtrader python analysis/feed_ab_lag.py --work <dir>

Two clocks, and the engine only watches one of them.

  `route_sample` stamps `spot_ts = now`, the LOCAL RECEIPT time of the
  sample (updown_rtds.rs). `eval_model` then gates on
  `now - spot_ts > MAX_SPOT_AGE_S` (5.0s). So the staleness gate measures
  age-since-receipt and is blind to the relay lag that came before it: a
  print that is already ~1.7s old by its own observation clock registers as
  0s old the instant it lands.

  The true age of the price a decision is taken on is
  `(now - t_recv) + (t_recv - ts)` — receipt age plus relay lag. The relay
  half has its own bound, `MAX_SAMPLE_LAG_S = 10.0`: a spot sample older
  than that by its own clock is DROPPED, which freezes `spot_ts` and lets
  the 5s gate bind a moment later. So the design ceiling on true mark age
  is 5 + 10 = 15s, and the observed ceiling is 5 + (max observed lag).

This script measures the observed side of that, at the timestamps replay
actually fired on (`sim.first_fire_t`), not at sampled ticks.

Measures only. Changes nothing.
"""

import argparse
import bisect
import json
import math
import pathlib
from collections import defaultdict

SYMS = ["btc", "eth", "sol", "bnb", "xrp"]
RTDS_SYMBOL = {s: f"{s}/usd" for s in SYMS}
TOPIC_SPOT = "crypto_prices_chainlink"
VARIANTS = ["rtds_tw30", "rtds_liveguard", "rtds_streamguard", "rtds_floorguard"]
MAX_SPOT_AGE_S = 5.0
MAX_SAMPLE_LAG_S = 10.0


def pct(xs, q):
    if not xs:
        return float("nan")
    xs = sorted(xs)
    return xs[min(len(xs) - 1, max(0, int(math.ceil(q * len(xs))) - 1))]


def load_spot(rtds_dir):
    """sym -> (t_recv[], ts[]) for the chainlink spot topic, receive-ordered.

    Spot is the only topic that moves `spot_ts`, so it is the only one the
    staleness gate can see.
    """
    inv = {v: k for k, v in RTDS_SYMBOL.items()}
    rows = defaultdict(list)
    for f in sorted(pathlib.Path(rtds_dir).glob("rtds-*.jsonl")):
        with open(f) as fh:
            for line in fh:
                if TOPIC_SPOT not in line:
                    continue
                if not any(s in line for s in RTDS_SYMBOL.values()):
                    continue
                d = json.loads(line)
                sym = inv.get(d.get("symbol"))
                if sym is None or d.get("topic") != TOPIC_SPOT:
                    continue
                rows[sym].append((d["t_recv"], d["ts"] / 1000.0))
    out = {}
    for s, rs in rows.items():
        rs.sort()
        # A sample whose own clock is already past MAX_SAMPLE_LAG_S is
        # dropped by route_sample and never moves spot_ts, so it must not
        # count as a fresh mark here either.
        rs = [r for r in rs if r[0] - r[1] <= MAX_SAMPLE_LAG_S]
        out[s] = ([r[0] for r in rs], [r[1] for r in rs])
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--work", required=True)
    ap.add_argument("--rtds-dir", required=True)
    ap.add_argument("--book-tape", default=None)
    args = ap.parse_args()
    work = pathlib.Path(args.work)

    spot = load_spot(args.rtds_dir)

    print("## Relay lag on the spot topic (the only topic that moves `spot_ts`)\n")
    print("| sym | n | lag p50 s | p90 | p99 | max | dropped as over-lagged |")
    print("|---|---|---|---|---|---|---|")
    for s in SYMS:
        tr, ts = spot[s]
        lag = [a - b for a, b in zip(tr, ts)]
        print(f"| {s} | {len(lag)} | {pct(lag,0.5):.3f} | {pct(lag,0.9):.3f} | "
              f"{pct(lag,0.99):.3f} | {max(lag):.3f} | "
              f"{'0 (none over %.0fs)' % MAX_SAMPLE_LAG_S} |")
    lag99 = {s: pct([a - b for a, b in zip(*spot[s])], 0.99) for s in SYMS}

    print("\n## Mark age at every replayed fire\n")
    print("`receipt age` is what MAX_SPOT_AGE_S compares. `true age` adds the "
          "relay lag — the actual age of the price the clip was taken on.\n")
    print("| variant | fires timed | receipt age p50/p99/max | true age p50/p99/max |"
          " receipt >3s | receipt >5s | true > lag p99 |")
    print("|---|---|---|---|---|---|---|")
    per_variant = {}
    for v in VARIANTS:
        rec_ages, true_ages, gated3, gated5, over99 = [], [], [], [], 0
        for sym in SYMS:
            path = work / f"out-{sym}-{v}.jsonl"
            if not path.exists():
                continue
            tr, ts = spot[sym]
            for line in open(path):
                r = json.loads(line)
                if "aggregate" in r["slug"]:
                    continue
                t = r["sim"].get("first_fire_t")
                if t is None:
                    continue
                j = bisect.bisect_right(tr, t) - 1
                if j < 0:
                    continue
                ra = t - tr[j]
                ta = t - ts[j]
                rec_ages.append(ra)
                true_ages.append(ta)
                if ra > 3.0:
                    gated3.append((r["slug"], r["sim"].get("pnl") or 0.0, ra))
                if ra > MAX_SPOT_AGE_S:
                    gated5.append((r["slug"], r["sim"].get("pnl") or 0.0, ra))
                if ta > lag99[sym]:
                    over99 += 1
        per_variant[v] = (rec_ages, true_ages, gated3, gated5)
        n = len(rec_ages)
        print(f"| {v} | {n} | {pct(rec_ages,0.5):.2f} / {pct(rec_ages,0.99):.2f} / "
              f"{max(rec_ages):.2f} | {pct(true_ages,0.5):.2f} / "
              f"{pct(true_ages,0.99):.2f} / {max(true_ages):.2f} | "
              f"{len(gated3)} | {len(gated5)} | {over99}/{n} |")

    print("\n## Sensitivity of the A/B to MAX_SPOT_AGE_S ∈ {3, 5}\n")
    print("Every window whose first replayed fire sat on a mark older than 3s — "
          "i.e. the windows a 3s tolerance would newly gate at the moment they "
          "opened.\n")
    for v in VARIANTS:
        _, _, g3, g5 = per_variant[v]
        tot3 = sum(p for _, p, _ in g3)
        if not g3:
            print(f"- **{v}**: no fire opened on a mark older than 3s. "
                  f"A 3s tolerance changes nothing.")
            continue
        print(f"- **{v}**: {len(g3)} window(s), {tot3:+.2f} of net P&L — "
              + ", ".join(f"`{s}` ({p:+.0f}, age {a:.1f}s)" for s, p, a in g3))
        if g5:
            print(f"  - already past the deployed 5s: {len(g5)} window(s)")

    # `first_fire_t` is the only fire timestamp the report carries, so the
    # table above times ONE clip per window. This closes that gap from the
    # other side: the receipt-age of every armed tick in the comparable set.
    # If the (3, 5] band is empty across all armed time, then no clip —
    # first or later — could have sat in it either.
    if args.book_tape:
        print("\n### The same question over ALL armed ticks, not just first fires\n")
        meta = json.load(open(work / "meta.json"))
        comparable = {s: set(v) for s, v in meta["comparable"].items()}
        ages = defaultdict(list)
        with open(args.book_tape) as fh:
            for line in fh:
                if "-5m-" not in line:
                    continue
                try:
                    d = json.loads(line)
                except Exception:
                    continue
                slug = d.get("slug") or ""
                sym = slug.split("-")[0]
                if sym not in SYMS or slug not in comparable.get(sym, ()):
                    continue
                t = d.get("t")
                start = float(slug.rsplit("-", 1)[1])
                if t is None or not (start <= t <= start + 300):
                    continue
                tr, _ts = spot[sym]
                j = bisect.bisect_right(tr, t) - 1
                if j >= 0:
                    ages[sym].append(t - tr[j])
        print("| sym | armed ticks | receipt age p50/p90/p99/max | in (3,5] | >5s (already gated) |")
        print("|---|---|---|---|---|")
        for s in SYMS:
            a = ages[s]
            if not a:
                continue
            band = sum(1 for x in a if 3.0 < x <= MAX_SPOT_AGE_S)
            over = sum(1 for x in a if x > MAX_SPOT_AGE_S)
            print(f"| {s} | {len(a)} | {pct(a,0.5):.2f} / {pct(a,0.9):.2f} / "
                  f"{pct(a,0.99):.2f} / {max(a):.1f} | {band} ({band/len(a)*100:.2f}%) | "
                  f"{over} ({over/len(a)*100:.2f}%) |")


if __name__ == "__main__":
    main()
