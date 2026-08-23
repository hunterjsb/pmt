"""Resolution-farmer study, stage 10: does an exit rule rescue the payoff shape?

Every failure in the hand-sampled set had the market trade through to the other side before
resolution -- the losses are ordinary re-pricing on news, not surprise settlements. That
invites the obvious question: cut at a stop instead of riding to zero?

This tests it on the recorded mid path, and deliberately reports the OPTIMISTIC version --
exit at the recorded mid, minus a fixed haircut. Real exit liquidity in a collapsing book is
worse than a mid, and the price gaps through a soccer goal rather than walking to the stop,
so treat every number here as an upper bound on what a stop could earn.
"""

from __future__ import annotations

import json
import os
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import rf_lib as L  # noqa: E402

OUT = Path(os.path.expanduser("~/.pmt/resfarm"))


def main():
    slip = float(os.environ.get("RF_SLIP", "0.002"))
    exit_haircut = float(os.environ.get("RF_EXIT_HAIRCUT", "0.01"))
    markets = L.load_markets()
    prices = L.load_prices()
    rows = L.build_entries(markets, prices)

    sel = [r for r in rows if r["delta_h"] == 3.0 and r["cat"] != "crypto-updown"
           and r["tradable"] and r["p_fav"] >= 0.93
           and (r["won"] is not None or r["split"])]
    print(f"n={len(sel)}  slip={slip}  exit haircut={exit_haircut}\n")

    # path of the favourite's mid from entry to the end of the recorded window
    paths = {}
    for r in sel:
        ts, ps = prices[r["id"]]
        t0 = r["end_ts"] - int(r["delta_h"] * 3600)
        fav_path = [(t, p if r["fav"] == "YES" else 1 - p) for t, p in zip(ts, ps) if t >= t0]
        paths[r["id"]] = fav_path

    flip = sum(1 for r in sel if paths[r["id"]] and min(p for _, p in paths[r["id"]]) < 0.5)
    print(f"markets whose favourite mid traded below 0.50 after entry: {flip} "
          f"({flip/len(sel):.1%})")
    fl_loss = sum(1 for r in sel if r["won"] == 0 and paths[r["id"]]
                  and min(p for _, p in paths[r["id"]]) < 0.5)
    n_loss = sum(1 for r in sel if r["won"] == 0)
    fl_win = sum(1 for r in sel if r["won"] == 1 and paths[r["id"]]
                 and min(p for _, p in paths[r["id"]]) < 0.5)
    n_win = sum(1 for r in sel if r["won"] == 1)
    print(f"  of the {n_loss} eventual LOSSES : {fl_loss} ({fl_loss/max(n_loss,1):.1%}) crossed 0.50 in-window")
    print(f"  of the {n_win} eventual WINS   : {fl_win} ({fl_win/max(n_win,1):.1%}) crossed 0.50 in-window")
    print("  (the recorded window ends at endDate+2h, so a move after that is invisible here)\n")

    print(f"{'stop':>6} {'n':>6} {'stopped':>8} {'stop-outs that':>15} {'ROI/trade':>11} {'ROI no-stop':>12}")
    print(f"{'':>6} {'':>6} {'':>8} {'would have won':>15}")
    for stop in (None, 0.90, 0.85, 0.80, 0.70, 0.50, 0.30):
        pnl = cap = 0.0
        n_stop = n_stop_won = 0
        for r in sel:
            entry = min(r["p_fav"] + slip, 1.0 - r["tick"])
            fee = r["fee_rate"] * entry * (1 - entry)
            c = entry + fee
            payoff = None
            if stop is not None:
                for _, p in paths[r["id"]]:
                    if p <= stop:
                        payoff = max(p - exit_haircut, 0.0)
                        n_stop += 1
                        if r["won"]:
                            n_stop_won += 1
                        break
            if payoff is None:
                payoff = 0.5 if r["split"] else (1.0 if r["won"] else 0.0)
            pnl += payoff - c
            cap += c
        base = "" if stop is None else ""
        print(f"{str(stop):>6} {len(sel):>6} {n_stop:>8} {n_stop_won:>15} "
              f"{pnl/cap*100:>10.2f}% {base:>12}")

    print("\nper-category, best stop vs no stop (ROI/trade):")
    bycat = defaultdict(list)
    for r in sel:
        bycat[r["cat"]].append(r)
    for c, rs in sorted(bycat.items(), key=lambda kv: -len(kv[1])):
        if len(rs) < 60:
            continue
        line = [f"  {c:<14} n={len(rs):>4}"]
        for stop in (None, 0.85, 0.70, 0.50):
            pnl = cap = 0.0
            for r in rs:
                entry = min(r["p_fav"] + slip, 1.0 - r["tick"])
                fee = r["fee_rate"] * entry * (1 - entry)
                cc = entry + fee
                payoff = None
                if stop is not None:
                    for _, p in paths[r["id"]]:
                        if p <= stop:
                            payoff = max(p - exit_haircut, 0.0)
                            break
                if payoff is None:
                    payoff = 0.5 if r["split"] else (1.0 if r["won"] else 0.0)
                pnl += payoff - cc
                cap += cc
            line.append(f"{'none' if stop is None else stop}={pnl/cap*100:>7.2f}%")
        print("  ".join(line))


if __name__ == "__main__":
    main()
