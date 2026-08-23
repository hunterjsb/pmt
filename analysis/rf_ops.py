"""Resolution-farmer study, stage 8: operational shape.

Supply (how many qualifying markets exist per day), capital turnover (how long a clip is
locked), and the redemption lag -- measured as closedTime minus endDate, i.e. how long after
the scheduled end the payout actually became claimable. Slow resolution is the quiet killer
of every "small edge, high turnover" pitch, so it is measured rather than assumed.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import sys
from collections import Counter, defaultdict
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import rf_lib as L  # noqa: E402

OUT = Path(os.path.expanduser("~/.pmt/resfarm"))


def q(vals, p):
    if not vals:
        return float("nan")
    v = sorted(vals)
    return v[min(int(p * len(v)), len(v) - 1)]


def main():
    rows = json.load((OUT / "entries.json").open())
    d3 = [r for r in rows if r["delta_h"] == 3.0 and r["cat"] != "crypto-updown"]
    buy = [r for r in d3 if r["tradable"]]

    days = sorted({dt.datetime.fromtimestamp(r["end_ts"], dt.timezone.utc).date().isoformat()
                   for r in d3})
    span = len(days)
    print(f"corpus span: {span} days ({days[0]} .. {days[-1]})\n")

    print("=== SUPPLY: qualifying markets per day (Δ=3h, non-updown) ===")
    for label, sel in (("favourite mid >= 0.90 (any)", d3),
                       ("  of which BUYABLE (has an ask)", buy),
                       ("  buyable and mid in [0.93, 0.9985]",
                        [r for r in buy if r["p_fav"] >= 0.93]),
                       ("  buyable, [0.93,0.9985], vol >= $25k",
                        [r for r in buy if r["p_fav"] >= 0.93 and r["vol"] >= 25_000]),
                       ("  buyable, [0.93,0.9985], vol >= $100k",
                        [r for r in buy if r["p_fav"] >= 0.93 and r["vol"] >= 100_000])):
        per_day = Counter(dt.datetime.fromtimestamp(r["end_ts"], dt.timezone.utc).date().isoformat()
                          for r in sel)
        counts = [per_day.get(d, 0) for d in days]
        print(f"  {label:<42} total={len(sel):>6}  {len(sel)/span:>6.1f}/day  "
              f"p10={q(counts,0.1):>3}  med={q(counts,0.5):>3}  p90={q(counts,0.9):>3}")

    print("\n=== category mix of the buyable [0.93,0.9985] pool ===")
    cc = Counter(r["cat"] for r in buy if r["p_fav"] >= 0.93)
    tot = sum(cc.values())
    for c, n in cc.most_common():
        print(f"  {c:<16} {n:>5}  ({n/tot:>5.1%})  {n/span:>5.1f}/day")

    print("\n=== REDEMPTION LAG: closedTime - endDate, by category (hours) ===")
    lag = defaultdict(list)
    for r in d3:
        if r["closed_ts"]:
            lag[r["cat"]].append((r["closed_ts"] - r["end_ts"]) / 3600.0)
    allv = [v for vs in lag.values() for v in vs]
    print(f"  {'category':<16} {'n':>6} {'p10':>7} {'med':>7} {'p75':>7} {'p90':>7} {'p99':>7} {'max':>8}")
    for c, vs in sorted(lag.items(), key=lambda kv: -len(kv[1])):
        if len(vs) < 20:
            continue
        print(f"  {c:<16} {len(vs):>6} {q(vs,0.1):>7.1f} {q(vs,0.5):>7.1f} {q(vs,0.75):>7.1f} "
              f"{q(vs,0.9):>7.1f} {q(vs,0.99):>7.1f} {max(vs):>8.1f}")
    print(f"  {'ALL':<16} {len(allv):>6} {q(allv,0.1):>7.1f} {q(allv,0.5):>7.1f} {q(allv,0.75):>7.1f} "
          f"{q(allv,0.9):>7.1f} {q(allv,0.99):>7.1f} {max(allv):>8.1f}")

    print("\n=== TOTAL HOLD: entry (endDate-3h) to redeemable, hours ===")
    hold = [r["hold_h"] for r in buy if r["hold_h"] is not None and r["p_fav"] >= 0.93]
    print(f"  n={len(hold)}  p10={q(hold,0.1):.1f}  med={q(hold,0.5):.1f}  "
          f"p75={q(hold,0.75):.1f}  p90={q(hold,0.9):.1f}  p99={q(hold,0.99):.1f}  max={max(hold):.1f}")
    print(f"  implied turnover at the median: {24/q(hold,0.5):.2f} deployments/day per dollar")

    print("\n=== EVENT CLUSTERING: correlated legs a farmer would buy together ===")
    ev = Counter(r["event_slug"] for r in buy if r["p_fav"] >= 0.93)
    dist = Counter(ev.values())
    print(f"  {len(ev)} distinct events; legs per event: " +
          ", ".join(f"{k}:{v}" for k, v in sorted(dist.items())[:8]))
    print("  biggest single-event clusters:")
    for e, n in ev.most_common(8):
        print(f"    {n:>3} legs  {e}")

    print("\n=== WORST DAYS: failures per calendar day (buyable, mid>=0.93) ===")
    fails = defaultdict(list)
    for r in buy:
        if r["p_fav"] >= 0.93 and r["won"] == 0:
            d = dt.datetime.fromtimestamp(r["end_ts"], dt.timezone.utc).date().isoformat()
            fails[d].append(r)
    for d, fs in sorted(fails.items(), key=lambda kv: -len(kv[1]))[:6]:
        evs = Counter(f["event_slug"] for f in fs)
        print(f"  {d}: {len(fs)} failures across {len(evs)} events "
              f"(worst event contributed {evs.most_common(1)[0][1]})")


if __name__ == "__main__":
    main()
