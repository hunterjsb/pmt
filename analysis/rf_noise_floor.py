"""Resolution-farmer study, stage 11: is the best pocket's edge even measurable?

The 0.995-0.999 band is the only place the raw numbers come out positive. This asks the
question that decides whether that means anything: at a 99.8% win rate, how many observations
does it take to distinguish a +10bp edge from zero -- and how many days of profit does one
failure cost?

The answer is the study's real conclusion. You cannot validate an edge this small before the
Poisson noise in the failure count swamps it.
"""

from __future__ import annotations

import json
import math
import os
import sys
from pathlib import Path

OUT = Path(os.path.expanduser("~/.pmt/resfarm"))

# two-sided 95% Poisson upper bounds on the count, for small k (chi-square quantiles / 2)
POIS_HI = {0: 3.69, 1: 5.57, 2: 7.22, 3: 8.77, 4: 10.24, 5: 11.67,
           6: 13.06, 7: 14.42, 8: 15.76, 9: 17.08, 10: 18.39}


def main():
    rows = json.load((OUT / "entries.json").open())
    d3 = [r for r in rows if r["delta_h"] == 3.0 and r["cat"] != "crypto-updown"
          and r["tradable"] and (r["won"] is not None or r["split"])]

    print(f"{'band':<14} {'n':>6} {'losses':>7} {'mean ask*':>10} {'breakeven':>10} "
          f"{'realized':>10} {'95% upper':>11} {'wins per':>9} {'n needed':>10}")
    print(f"{'':<14} {'':>6} {'':>7} {'(mid+½tick)':>10} {'loss rate':>10} {'loss rate':>10} "
          f"{'loss rate':>11} {'1 loss':>9} {'for 2σ':>10}")
    print("-" * 104)

    for lo, hi in ((0.930, 0.950), (0.950, 0.970), (0.970, 0.985),
                   (0.985, 0.995), (0.995, 0.9986)):
        sel = [r for r in d3 if lo <= r["p_fav"] < hi]
        if len(sel) < 20:
            continue
        n = len(sel)
        k = sum(1 for r in sel if not r["won"] and not r["split"])
        # the ask is at least half a tick above a mid; use that as the optimistic entry
        entry = sum(min(r["p_fav"] + r["tick"] / 2, 1 - r["tick"]) for r in sel) / n
        fee = sum(r["fee_rate"] for r in sel) / n * entry * (1 - entry)
        cost = entry + fee
        be_loss = 1.0 - cost                     # loss rate at which EV = 0
        real_loss = k / n
        hi_loss = POIS_HI.get(k, k + 2 * math.sqrt(max(k, 1))) / n
        wins_per_loss = cost / max(1.0 - cost, 1e-9)
        # n such that the standard error on the loss rate is half the edge margin
        margin = be_loss - real_loss
        need = (4 * real_loss * (1 - real_loss) / margin ** 2) if margin > 0 else float("inf")
        print(f"[{lo:.3f},{hi:.4f}) {n:>6} {k:>7} {entry:>10.4f} {be_loss:>10.5f} "
              f"{real_loss:>10.5f} {hi_loss:>11.5f} {wins_per_loss:>9.0f} "
              f"{(f'{need:,.0f}' if need != float('inf') else 'never'):>10}")

    print()
    print("* mean ask = mid + half a tick: the cheapest execution physically possible on a")
    print("  1-tick-wide book. Anything wider makes every row worse.")
    print("  'breakeven loss rate' = 1 - (ask + fee). 'wins per 1 loss' = how many winning")
    print("  trades one failure erases. 'n needed for 2σ' = observations required before the")
    print("  realized loss rate is two standard errors clear of breakeven.")

    band = [r for r in d3 if 0.995 <= r["p_fav"] < 0.9986]
    if band:
        import datetime as dt
        days = len({dt.datetime.fromtimestamp(r["end_ts"], dt.timezone.utc).date().isoformat()
                    for r in d3})
        per_day = len(band) / days
        n = len(band)
        k = sum(1 for r in band if not r["won"] and not r["split"])
        entry = sum(min(r["p_fav"] + r["tick"] / 2, 1 - r["tick"]) for r in band) / n
        fee = sum(r["fee_rate"] for r in band) / n * entry * (1 - entry)
        margin = (1 - entry - fee) - k / n
        need = 4 * (k / n) * (1 - k / n) / margin ** 2 if margin > 0 else float("inf")
        print(f"\nbest band supply: {per_day:.1f} qualifying markets/day over {days} days")
        if need != float("inf"):
            print(f"  at that rate, {need:,.0f} observations = "
                  f"{need/max(per_day,1e-9)/365:.1f} YEARS of trading to establish the edge")


if __name__ == "__main__":
    main()
