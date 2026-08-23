"""Resolution-farmer study, stage 7: is there anything to BUY after the event is decided?

The strategy's whole premise is the window between "the real world has settled this" and
"UMA has posted the payout". This measures the final resting book in exactly that window,
using gamma's frozen bestBid/bestAsk on resolved markets (they stop updating at close, so
they ARE the last book).

The question is not what the price was. It is whether anyone was offering.
"""

from __future__ import annotations

import json
import os
import random
import sys
import time
from collections import Counter
from pathlib import Path

import requests

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import rf_lib as L  # noqa: E402

UA = {"User-Agent": "pmtrader/1.0"}
GAMMA = "https://gamma-api.polymarket.com"
OUT = Path(os.path.expanduser("~/.pmt/resfarm"))
BATCH = 40


def get(params, tries=5):
    for i in range(tries):
        try:
            r = requests.get(f"{GAMMA}/markets", params=params, headers=UA, timeout=50)
            if r.status_code == 200:
                d = r.json()
                if isinstance(d, list):
                    return d
        except Exception:
            pass
        time.sleep(1.2 * (i + 1))
    return []


def main():
    n_sample = int(sys.argv[1]) if len(sys.argv) > 1 else 1500
    vol_min = float(sys.argv[2]) if len(sys.argv) > 2 else 25000.0

    pool = []
    with (OUT / "markets.jsonl").open() as f:
        for line in f:
            try:
                m = json.loads(line)
            except Exception:
                continue
            if float(m.get("volume") or 0) < vol_min:
                continue
            if "up-or-down" in (m.get("tags") or []):
                continue
            if L.winner_side(m.get("outcomePrices")) not in ("YES", "NO"):
                continue
            pool.append(m)
    random.seed(7)
    sample = random.sample(pool, min(n_sample, len(pool)))
    print(f"sampling {len(sample)} resolved non-updown markets with volume >= ${vol_min:,.0f}")

    byid = {m["id"]: m for m in sample}
    fetched = []
    ids = list(byid)
    for i in range(0, len(ids), BATCH):
        chunk = ids[i:i + BATCH]
        params = [("id", x) for x in chunk] + [("closed", "true")]
        fetched.extend(get(params))
        time.sleep(0.25)
        if (i // BATCH) % 10 == 0:
            print(f"  {i+len(chunk)}/{len(ids)}", flush=True)

    stats = Counter()
    ask_hist = Counter()
    rows = []
    for m in fetched:
        src = byid.get(m["id"])
        if not src:
            continue
        win = L.winner_side(src.get("outcomePrices"))
        bb, ba = m.get("bestBid"), m.get("bestAsk")
        if ba is None:
            stats["no bestAsk field"] += 1
            continue
        # gamma reports the YES book; the winning side's ask is 1-bestBid when NO won
        if win == "YES":
            win_ask = float(ba)
            win_bid = float(bb) if bb is not None else 0.0
        else:
            win_ask = 1.0 - float(bb) if bb is not None else 1.0
            win_bid = 1.0 - float(ba)
        tick = float(m.get("orderPriceMinTickSize") or 0.01)
        buyable = win_ask <= 1.0 - tick + 1e-9
        stats["total"] += 1
        stats["winning side buyable at close" if buyable else "winning side NO ASK at close"] += 1
        ask_hist[round(win_ask, 3)] += 1
        rows.append({"id": m["id"], "q": m.get("question"), "win": win,
                     "win_ask": win_ask, "win_bid": win_bid, "buyable": buyable,
                     "cat": L.category(src.get("tags")),
                     "vol": float(m.get("volume") or 0)})

    print("\n=== final resting book on the side that WON, at market close ===")
    for k, v in stats.most_common():
        print(f"  {k:<38} {v:>6}  ({v/max(stats['total'],1):.1%})" if k != "total"
              else f"  {k:<38} {v:>6}")
    print("\n  ask price on the winning side (top 12):")
    for k, v in ask_hist.most_common(12):
        print(f"    ask={k:<7} {v:>6}")

    print("\n  buyable share by category:")
    by = {}
    for r in rows:
        d = by.setdefault(r["cat"], [0, 0])
        d[0] += 1
        d[1] += 1 if r["buyable"] else 0
    for c, (n, b) in sorted(by.items(), key=lambda kv: -kv[1][0]):
        if n >= 20:
            print(f"    {c:<16} {b:>5}/{n:<5} buyable ({b/n:.1%})")

    with (OUT / "postevent.json").open("w") as f:
        json.dump(rows, f)
    print(f"\nwrote {OUT/'postevent.json'}")


if __name__ == "__main__":
    main()
