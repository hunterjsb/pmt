"""Resolution-farmer study, stage 12: interrogate the one pocket that survives the holdout.

`crypto-other` at Δ=4h clears its breakeven with a positive 95% lower bound over the corpus.
Before calling that an edge, this asks what the markets actually ARE, what risk the 100% win
rate is being paid for, and whether the sample period could plausibly have shown a loss.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path

OUT = Path(os.path.expanduser("~/.pmt/resfarm"))


def main():
    rows = json.load((OUT / "entries.json").open())
    sel = [r for r in rows if r["cat"] == "crypto-other" and r["delta_h"] == 4.0
           and r["tradable"] and r["p_fav"] >= 0.90 and (r["won"] is not None or r["split"])]
    print(f"crypto-other, Δ=4h, buyable, mid>=0.90: n={len(sel)}, "
          f"losses={sum(1 for r in sel if not r['won'])}")

    print("\n=== what are these markets? ===")
    shapes = Counter()
    for r in sel:
        q = r["question"] or ""
        q2 = re.sub(r"[\d,\.]+", "#", q)
        shapes[q2[:70]] += 1
    for s, n in shapes.most_common(10):
        print(f"  {n:>4}  {s}")

    print("\n=== event clustering (how many legs of the same ladder per event) ===")
    ev = Counter(r["event_slug"] for r in sel)
    print(f"  {len(ev)} distinct events for {len(sel)} entries "
          f"({len(sel)/max(len(ev),1):.1f} legs per event)")
    for e, n in ev.most_common(6):
        print(f"    {n:>3} legs  {e}")

    print("\n=== side taken: is this selling crash insurance? ===")
    print("  ", Counter(r["fav"] for r in sel).most_common())
    print("  a 'NO' favourite on 'will BTC be ABOVE $X' is a bet the price stays DOWN;")
    print("  a 'YES' favourite is a bet it stays UP. Both are short realised volatility.")

    print("\n=== per-day exposure: how many legs would be open at once ===")
    per_day = Counter(dt.datetime.fromtimestamp(r["end_ts"], dt.timezone.utc).date().isoformat()
                      for r in sel)
    days = sorted(per_day)
    counts = [per_day[d] for d in days]
    print(f"  {len(days)} active days, mean {sum(counts)/len(days):.1f}/day, max {max(counts)}/day")
    print(f"  worst-case single-day notional at $75/clip: ${max(counts)*75:,.0f}")

    print("\n=== the distance the price had to travel ===")
    print("  mid at T-4h -> implied 'probability of being wrong', by bucket:")
    b = defaultdict(int)
    for r in sel:
        b[round(1 - r["p_fav"], 3)] += 1
    for k in sorted(b)[:12]:
        print(f"    implied miss prob {k:.3f}  n={b[k]}")

    print("\n=== hold time ===")
    h = sorted(r["hold_h"] for r in sel if r["hold_h"] is not None)
    if h:
        print(f"  median {h[len(h)//2]:.1f}h  p90 {h[int(len(h)*0.9)]:.1f}h  max {max(h):.1f}h")

    print("\n=== what the 'edge' annualises to, if you never lose (the tell) ===")
    n = len(sel)
    k = sum(1 for r in sel if not r["won"] and not r["split"])
    entry = sum(min(r["p_fav"] + r["tick"] / 2, 1 - r["tick"]) for r in sel) / n
    fee = sum(r["fee_rate"] for r in sel) / n * entry * (1 - entry)
    cost = entry + fee
    roi_perfect = (1.0 - cost) / cost
    hold = sorted(r["hold_h"] for r in sel if r["hold_h"] is not None)
    med_h = hold[len(hold) // 2] if hold else 4.0
    turns = 24.0 / med_h
    print(f"  mean cost basis {cost:.4f}; a win pays {1-cost:.4f} -> {roi_perfect*100:.2f}%/trade")
    print(f"  median hold {med_h:.1f}h -> {turns:.1f} turns/day -> {roi_perfect*turns*100:.1f}%/DAY")
    print(f"  compounded, that is {(1+roi_perfect)**(turns*365)-1:.3g}x per year")
    print("  No market leaves that on the table. The counterparty is not confused -- it is being")
    print("  paid for a tail. The only question is whether your sample contains one.")

    print("\n=== what it actually returned, once the sample was long enough ===")
    realized = ((n - k) * (1.0 - cost) - k * cost) / (n * cost)
    print(f"  losses: {k}/{n} ({k/n:.2%})  vs breakeven loss rate {1-cost:.4f} ({1/(1-cost):.0f}:1)")
    print(f"  REALIZED ROI per trade: {realized*100:+.2f}%")
    if k:
        print(f"  each loss costs {cost/(1-cost):.0f} wins; {k} losses cost {k*cost/(1-cost):.0f} "
              f"winning trades out of {n-k} available")

    print("\n=== the tail it is short: correlated breach ===")
    per_hour = Counter(dt.datetime.fromtimestamp(r["end_ts"], dt.timezone.utc)
                       .strftime("%Y-%m-%d %H:00") for r in sel)
    worst_hour = per_hour.most_common(1)[0]
    clip = 75.0
    daily_profit = (sum(counts) / len(days)) * clip * roi_perfect
    print(f"  busiest single resolution hour: {worst_hour[1]} legs, all settling off ONE price print")
    print(f"  at ${clip:g}/clip, if half of them breach together: "
          f"${worst_hour[1]//2 * clip * cost:,.0f} lost")
    print(f"  best-case daily profit at that clip (zero losses): ${daily_profit:,.2f}")
    print(f"  -> one correlated bad hour erases "
          f"{(worst_hour[1]//2 * clip * cost)/max(daily_profit,1e-9):.0f} DAYS of best-case profit")
    print("  and it is the SAME underlying, the SAME direction, and the SAME 4h horizon as the")
    print("  crypto up/down fleet -- so it stacks that book's tail rather than diversifying it.")

    print("\n=== how correlated are these with each other? ===")
    print("  all legs of one ladder resolve off the SAME underlying price at the SAME instant.")
    print("  legs sharing a resolution day:")
    same_day = Counter(dt.datetime.fromtimestamp(r["end_ts"], dt.timezone.utc)
                       .strftime("%Y-%m-%d %H:00") for r in sel)
    print(f"  {len(same_day)} distinct resolution hours; busiest hour holds "
          f"{same_day.most_common(1)[0][1]} legs")
    tot = sum(1 for r in sel)
    big = sum(n for _, n in same_day.most_common(5))
    print(f"  top 5 resolution hours hold {big}/{tot} ({big/tot:.0%}) of all positions")


if __name__ == "__main__":
    main()
