"""Resolution-farmer study, stage 3: realized base rates, hazards, and economics.

Everything here is per-Δ so a market never counts five times -- within one Δ slice each market
contributes exactly one observation, which is what makes the binomial CIs mean anything.
"""

from __future__ import annotations

import json
import math
import os
import random
import sys
from collections import Counter, defaultdict
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import rf_lib as L  # noqa: E402

OUT = Path(os.path.expanduser("~/.pmt/resfarm"))


def wilson(k: int, n: int, z: float = 1.96):
    if n == 0:
        return (0.0, 0.0)
    p = k / n
    d = 1 + z * z / n
    c = p + z * z / (2 * n)
    s = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n))
    return ((c - s) / d, (c + s) / d)


def trade_econ(p_fav: float, fee_rate: float, slip: float, won, split: bool, tick: float = 0.001):
    """Per-share P&L and capital.

    p_fav is the recorded MID; the taker pays the ask, modelled as mid + slip and clamped to
    the last quotable tick. Fee is Polymarket's live taker formula, C * rate * p * (1-p),
    charged at match time in USDC on top of the notional.
    """
    entry = min(p_fav + slip, 1.0 - tick)
    fee = fee_rate * entry * (1 - entry)
    cap = entry + fee
    payoff = 0.5 if split else (1.0 if won else 0.0)
    return payoff - cap, cap


def summarize(rows, key_fn, slip=0.01, min_n=1):
    agg = defaultdict(lambda: {"n": 0, "w": 0, "split": 0, "pnl": 0.0, "cap": 0.0,
                               "px": 0.0, "hold": [], "vol": []})
    for r in rows:
        if r["won"] is None and not r["split"]:
            continue
        k = key_fn(r)
        a = agg[k]
        a["n"] += 1
        a["w"] += 1 if r["won"] else 0
        a["split"] += 1 if r["split"] else 0
        a["px"] += r["p_fav"]
        pnl, cap = trade_econ(r["p_fav"], r["fee_rate"], slip, r["won"], r["split"], r["tick"])
        a["pnl"] += pnl
        a["cap"] += cap
        if r["hold_h"] is not None:
            a["hold"].append(r["hold_h"])
        a["vol"].append(r["vol"])
    out = []
    for k, a in agg.items():
        if a["n"] < min_n:
            continue
        lo, hi = wilson(a["w"], a["n"])
        mean_px = a["px"] / a["n"]
        roi = a["pnl"] / a["cap"] if a["cap"] else 0.0
        hold = sorted(a["hold"])
        out.append({
            "key": k, "n": a["n"], "wins": a["w"], "splits": a["split"],
            "wr": a["w"] / a["n"], "lo": lo, "hi": hi,
            "mean_px": mean_px, "roi": roi,
            "med_hold": hold[len(hold) // 2] if hold else None,
            "p90_hold": hold[int(len(hold) * 0.9)] if hold else None,
            "med_vol": sorted(a["vol"])[len(a["vol"]) // 2] if a["vol"] else 0,
        })
    return sorted(out, key=lambda r: -r["n"])


def table(title, rows, cols=("key", "n", "wr", "lo", "hi", "mean_px", "roi", "med_hold")):
    print(f"\n### {title}")
    hdr = f"{'key':<26} {'n':>6} {'win%':>7} {'wilson95':>15} {'mean px':>8} {'ROI/tr':>8} {'be':>7} {'edge':>7} {'medhold':>8}"
    print(hdr)
    print("-" * len(hdr))
    for r in rows:
        be = r["mean_px"] * 1.0
        edge = r["wr"] - r["mean_px"]
        print(f"{str(r['key']):<26} {r['n']:>6} {r['wr']*100:>6.2f}% "
              f"[{r['lo']*100:>5.1f},{r['hi']*100:>5.1f}] {r['mean_px']:>8.4f} "
              f"{r['roi']*100:>7.2f}% {be:>7.4f} {edge*100:>+6.2f}% "
              f"{(r['med_hold'] if r['med_hold'] is not None else float('nan')):>8.1f}")


def slip_table(rows, title):
    """The whole verdict lives in this table: gross edge vs the half-spread you actually pay."""
    print(f"\n### {title} -- ROI per trade vs assumed half-spread")
    print(f"{'bucket':<14} {'n':>6} {'win%':>7} {'mean mid':>9} " +
          " ".join(f"{'slip '+f'{s:.4f}':>12}" for s in (0.0, 0.0005, 0.002, 0.005, 0.01)))
    by = defaultdict(list)
    for r in rows:
        if r["won"] is None and not r["split"]:
            continue
        by[r["bucket"]].append(r)
    for b in sorted(by):
        sel = by[b]
        n = len(sel)
        w = sum(1 for r in sel if r["won"])
        mid = sum(r["p_fav"] for r in sel) / n
        cells = []
        for s in (0.0, 0.0005, 0.002, 0.005, 0.01):
            pnl = cap = 0.0
            for r in sel:
                a, c = trade_econ(r["p_fav"], r["fee_rate"], s, r["won"], r["split"], r["tick"])
                pnl += a
                cap += c
            cells.append(f"{pnl/cap*100:>11.2f}%")
        print(f"{b:<14} {n:>6} {w/n*100:>6.2f}% {mid:>9.4f} " + " ".join(cells))


def main():
    slip = float(os.environ.get("RF_SLIP", "0.002"))
    markets = L.load_markets()
    prices = L.load_prices()
    rows = L.build_entries(markets, prices)
    print(f"markets={len(markets)}  with-history={len(prices)}  entries={len(rows)}  slip={slip}")

    cats = Counter(r["cat"] for r in rows)
    print("categories:", cats.most_common())

    # The no-ask population is reported before it is dropped, because "how many sure things
    # are there" and "how many can you actually buy" differ by a factor of ~3.
    for d in (6.0, 3.0, 1.0):
        sl = [r for r in rows if r["delta_h"] == d and r["cat"] != "crypto-updown"]
        nt = sum(1 for r in sl if not r["tradable"])
        print(f"Δ={d:g}h: {len(sl)} favourites >=0.90; {nt} ({nt/max(len(sl),1):.1%}) have an "
              f"EMPTY ask ladder (mid > 1-1.5*tick) and cannot be bought at any price")

    # --- adverse selection: is the offer there when the outcome is actually settled? --------
    print("\n### ADVERSE SELECTION -- Δ=3h split by whether a real offer stood behind the mid")
    print(f"{'population':<34} {'n':>6} {'realized win%':>14} {'mean mid':>10} {'edge':>8}")
    allsl = [r for r in rows if r["delta_h"] == 3.0 and r["cat"] != "crypto-updown"
             and (r["won"] is not None or r["split"])]
    for lab, pred in (("mid>=0.90  BUYABLE (real ask)", lambda r: r["tradable"]),
                      ("mid>=0.90  UNBUYABLE (no ask)", lambda r: not r["tradable"]),
                      ("mid>=0.99  BUYABLE (real ask)", lambda r: r["tradable"] and r["p_fav"] >= 0.99),
                      ("mid>=0.99  UNBUYABLE (no ask)", lambda r: not r["tradable"] and r["p_fav"] >= 0.99)):
        s = [r for r in allsl if pred(r)]
        if not s:
            continue
        wr = sum(1 for r in s if r["won"]) / len(s)
        mp = sum(r["p_fav"] for r in s) / len(s)
        print(f"{lab:<34} {len(s):>6} {wr*100:>13.2f}% {mp:>10.4f} {(wr-mp)*100:>+7.2f}%")
    print("\n  buyable share by category (Δ=3h, mid>=0.90) -- the more settled the outcome, the")
    print("  less of it is for sale. esports (endDate = start+4-6h, so T-3h is mid/post-match)")
    print("  is the most settled category and the least buyable:")
    bycat = defaultdict(lambda: [0, 0])
    for r in allsl:
        bycat[r["cat"]][0] += 1
        bycat[r["cat"]][1] += 1 if r["tradable"] else 0
    for c, (n, b) in sorted(bycat.items(), key=lambda kv: -kv[1][0]):
        if n < 40:
            continue
        print(f"    {c:<16} {b:>5}/{n:<6} buyable ({b/n:>5.1%})")
    print()
    print("  a wide/one-sided book biases the MID upward, so the buyable rows' negative edge is")
    print("  if anything understated -- the ask you would really pay is above the mid shown.")
    print("  same split, restricted to deep books where the mid is trustworthy:")
    for vmin in (25_000, 100_000, 250_000):
        s = [r for r in allsl if r["tradable"] and r["vol"] >= vmin]
        if len(s) < 30:
            continue
        wr = sum(1 for r in s if r["won"]) / len(s)
        mp = sum(r["p_fav"] for r in s) / len(s)
        print(f"    BUYABLE, vol>=${vmin:>9,}         {len(s):>6} {wr*100:>13.2f}% {mp:>10.4f} "
              f"{(wr-mp)*100:>+7.2f}%")

    # --- headline: Δ = 3h, non-crypto-updown, all categories, actually buyable -------------
    d3 = [r for r in rows if r["delta_h"] == 3.0 and r["cat"] != "crypto-updown" and r["tradable"]]
    print(f"\nΔ=3h BUYABLE entries: {len(d3)}  distinct markets: {len(set(r['id'] for r in d3))}")
    print(f"SPLIT (UMA 50/50) outcomes at Δ=3h: {sum(1 for r in d3 if r['split'])}")

    table("ALL CATEGORIES by price bucket (Δ=3h)", summarize(d3, lambda r: r["bucket"], slip))
    slip_table(d3, "ALL CATEGORIES (Δ=3h)")
    for c in ("weather", "soccer", "esports", "crypto-other", "politics", "tennis", "baseball"):
        sel = [r for r in d3 if r["cat"] == c]
        if len(sel) >= 60:
            slip_table(sel, f"{c} (Δ=3h)")

    for d in (6.0, 4.0, 2.0, 1.0):
        dd = [r for r in rows if r["delta_h"] == d and r["cat"] != "crypto-updown" and r["tradable"]]
        table(f"by price bucket (Δ={d:.0f}h)", summarize(dd, lambda r: r["bucket"], slip))

    table("by category (Δ=3h, px>=0.93)",
          summarize([r for r in d3 if r["p_fav"] >= 0.93], lambda r: r["cat"], slip, min_n=30))

    for b in ("0.930-0.950", "0.950-0.970", "0.970-0.985", "0.985-0.995", "0.995-0.999"):
        table(f"category x bucket {b} (Δ=3h)",
              summarize([r for r in d3 if r["bucket"] == b], lambda r: r["cat"], slip, min_n=30))

    # --- hazard axes ----------------------------------------------------------------------
    hi = [r for r in d3 if r["p_fav"] >= 0.93]
    table("volume decile (Δ=3h, px>=0.93)",
          summarize(hi, lambda r: f"vol {int(math.log10(max(r['vol'],1)))}e", slip, min_n=30))
    # NOTE: with fidelity=5 the sampling grid, not the market, sets staleness -- every row lands
    # in 2-10m. This axis measures the API, not liquidity; distinct_late below is the real proxy.
    table("staleness of the T-3h quote (px>=0.93) -- ARTIFACT OF THE 5-MIN GRID, not a signal",
          summarize(hi, lambda r: ("fresh<2m" if r["stale_s"] < 120 else
                                   "2-10m" if r["stale_s"] < 600 else "10-20m"), slip, min_n=30))
    table("late-window price activity (distinct prices in final 6h, px>=0.93)",
          summarize(hi, lambda r: ("flat<=2" if r["distinct_late"] <= 2 else
                                   "3-10" if r["distinct_late"] <= 10 else
                                   "11-40" if r["distinct_late"] <= 40 else ">40"), slip, min_n=30))
    table("negRisk (px>=0.93)", summarize(hi, lambda r: f"negRisk={r['neg_risk']}", slip, min_n=30))
    table("auto-resolved (px>=0.93)", summarize(hi, lambda r: f"auto={r['auto']}", slip, min_n=30))
    table("uma rounds (px>=0.93)",
          summarize(hi, lambda r: f"uma={len(r['uma_statuses'] or [])}", slip, min_n=20))

    # --- coverage of the `pmt scan` comment signal -----------------------------------------
    cc = [(int(markets[r["id"]].get("event_comment_count") or 0), r["won"]) for r in hi]
    zero = sum(1 for c, _ in cc if c == 0)
    lf = [c for c, w in cc if w == 0]
    print("\n### `pmt scan` comment-complaint signal: coverage on this universe")
    print(f"  qualifying entries with ZERO event comments : {zero}/{len(cc)} ({zero/max(len(cc),1):.1%})")
    print(f"  FAILURES with ZERO event comments           : {sum(1 for c in lf if c==0)}/{len(lf)} "
          f"({sum(1 for c in lf if c==0)/max(len(lf),1):.1%})")
    print("  -> the complaint-ratio signal is undefined on essentially the whole population")

    # --- the loser file -------------------------------------------------------------------
    losers = [r for r in hi if r["won"] == 0 or r["split"]]
    print(f"\nfailures at Δ=3h px>=0.93: {len(losers)} of {len(hi)} ({len(losers)/max(len(hi),1):.2%})")
    with (OUT / "losers_d3.json").open("w") as f:
        json.dump(sorted(losers, key=lambda r: -r["vol"]), f, indent=1)
    print(f"wrote {OUT/'losers_d3.json'}")

    # last-trade reversal proxy: did the market itself discover the problem before close?
    rev = Counter()
    for r in hi:
        lt = r["last_trade"]
        moved_against = (lt < 0.5) if r["fav"] == "YES" else (lt > 0.5)
        rev[(bool(r["won"]), moved_against)] += 1
    nw = rev[(True, True)] + rev[(True, False)]
    nl = rev[(False, True)] + rev[(False, False)]
    print(f"\nDID THE MARKET DISCOVER IT FIRST? (last trade on the other side of 0.50 from entry)")
    print(f"  of {nl} FAILURES: {rev[(False, True)]} ({rev[(False,True)]/max(nl,1):.1%}) had already flipped")
    print(f"  of {nw} WINNERS : {rev[(True, True)]} ({rev[(True,True)]/max(nw,1):.1%}) had flipped")
    print("  -> losses are ordinary re-pricing on news, not surprises at settlement")

    # atomic: every later stage reads this file, and a half-written one is invisible corruption
    tmp = OUT / "entries.json.tmp"
    with tmp.open("w") as f:
        json.dump(rows, f)
    tmp.replace(OUT / "entries.json")
    print(f"wrote {OUT/'entries.json'} ({len(rows)} rows)")


if __name__ == "__main__":
    main()
