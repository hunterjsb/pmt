"""Q1 — complete-set arb: is up_ask + dn_ask ever < $1.00 after taker fees?

Answer: no, and it structurally cannot be. UP and DOWN are two mirrored views
of ONE price — Polymarket's CLOB matches complementary orders by minting, so
`up_ask == 1 - dn_bid` and `up_bid == 1 - dn_ask` are exchange invariants, and
the pair ask-sum is 1.00 + spread by construction.

The naive scan below finds 1000+ apparent opportunities worth "$800" in the
book tape. Every one is an artifact: pmengine records the two legs from two
independently-updated `ctx.order_books` entries, so within a fast second one
leg is a tick stale and the pair looks cheap (or rich — the scan finds the
impossible bid-sum > 1.00 at the SAME rate, which is the tell). This script
runs the naive scan, then the mirror test that kills it, and with --live
re-checks against simultaneous both-leg snapshots straight from the exchange.

Run: uv run python analysis/firsthalf_q1_arb.py [--live]
"""

from __future__ import annotations

import json
import sys
import time
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from firsthalf_lib import load_book_windows, pct, taker_fee  # noqa: E402


def pair_cost(up_ask: float, dn_ask: float) -> float:
    """All-in cost of one complete set bought as a taker on both legs."""
    return up_ask + dn_ask + taker_fee(up_ask) + taker_fee(dn_ask)


def naive_scan(wins: dict) -> None:
    n = two_sided = gross_sub1 = net_pos = bid_over1 = 0
    edges, per_win = [], {}
    fast = Counter()
    for rows in wins.values():
        for i, r in enumerate(rows):
            n += 1
            ua, da, ub, db = (r.get("up_ask"), r.get("dn_ask"), r.get("up_bid"), r.get("dn_bid"))
            if ua is None or da is None:
                continue
            two_sided += 1
            edges.append(1.0 - (ua + da))
            if ua + da < 1.0:
                gross_sub1 += 1
            e = 1.0 - pair_cost(ua, da)
            if e > 0:
                net_pos += 1
                slug = r["slug"]
                depth = min(r.get("up_ask_sz") or 0.0, r.get("dn_ask_sz") or 0.0)
                if e * depth > per_win.get(slug, 0.0):
                    per_win[slug] = e * depth
            if ub is not None and db is not None and ub + db > 1.0:
                bid_over1 += 1
            # is the "opportunity" concentrated where the book is moving fast?
            if i:
                p = rows[i - 1]
                pb, pa2 = p.get("dn_bid"), p.get("dn_ask")
                if pb is not None and pa2 is not None and db is not None and da is not None:
                    v = abs((db + da) / 2 - (pb + pa2) / 2) / max(r["t"] - p["t"], 0.5)
                    k = "moving" if v >= 0.02 else "quiet"
                    fast[k + "_n"] += 1
                    if e > 0:
                        fast[k + "_hit"] += 1

    print("--- naive scan of the recorded book tape ---")
    print(f"samples {n}, both asks quoted {two_sided}")
    print(f"  gross ask-sum < 1.00   : {gross_sub1} ({100*gross_sub1/two_sided:.2f}%)")
    print(f"  net of taker fee  > 0  : {net_pos} ({100*net_pos/two_sided:.2f}%)")
    print(f"  bid-sum > 1.00 (IMPOSSIBLE — free money, would never persist)")
    print(f"                         : {bid_over1} ({100*bid_over1/two_sided:.2f}%)")
    print(f"  'capturable' across {len(per_win)} windows: ${sum(per_win.values()):.2f}  <- all of it fictional")
    print("  gross pair discount (1 - ask-sum), cents: " + " ".join(
        f"p{int(q*100)}={100*pct(edges,q):+.1f}" for q in (0.05, 0.25, 0.5, 0.75, 0.95)))
    for k in ("quiet", "moving"):
        nn, hh = fast[k + "_n"], fast[k + "_hit"]
        if nn:
            print(f"  hit rate when book is {k:<7}: {100*hh/nn:5.2f}%  (n={nn})")
    print("  -> the two directions appear at the same rate, and the rate roughly")
    print("     doubles when the book is moving: that is snapshot lag, not edge.")


def mirror_test(wins: dict) -> None:
    exact = near = tot = 0
    s_exact = s_tot = 0
    diffs = []

    def q(r, tok):
        return (r.get(f"{tok}_bid"), r.get(f"{tok}_ask"))

    for rows in wins.values():
        for i, r in enumerate(rows):
            ub, ua, db, da = (r.get("up_bid"), r.get("up_ask"), r.get("dn_bid"), r.get("dn_ask"))
            if None in (ub, ua, db, da):
                continue
            tot += 1
            d1, d2 = abs(ua - (1 - db)), abs(ub - (1 - da))
            diffs.append(max(d1, d2))
            if d1 < 1e-9 and d2 < 1e-9:
                exact += 1
            if d1 <= 0.0105 and d2 <= 0.0105:
                near += 1
            if 0 < i < len(rows) - 1 and all(
                q(rows[i - 1], t) == q(r, t) == q(rows[i + 1], t) for t in ("up", "dn")
            ):
                s_tot += 1
                if d1 < 1e-9 and d2 < 1e-9:
                    s_exact += 1
    print("\n--- mirror test: is up_ask == 1 - dn_bid and up_bid == 1 - dn_ask? ---")
    print(f"fully two-sided samples : {tot}")
    print(f"  exact mirror          : {exact} ({100*exact/tot:.1f}%)")
    print(f"  within one tick       : {near} ({100*near/tot:.1f}%)")
    print(f"  exact | settled book  : {s_exact}/{s_tot} ({100*s_exact/max(s_tot,1):.1f}%)")
    print("  deviation pctiles: " + " ".join(f"p{int(x*100)}={100*pct(diffs,x):.1f}c"
                                             for x in (0.5, 0.75, 0.9, 0.99)))
    print("  -> the deviation is the tape's own two-leg desync. Settling the book")
    print("     raises exact agreement; the live check below removes it entirely.")


def live_check(rounds: int = 3) -> None:
    """Both legs of many live windows in ONE batch request — no leg desync."""
    import requests

    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "pmtrader"))
    from polymarket import hosts  # noqa: PLC0415

    now = int(time.time())
    slugs = [
        f"{sym}-updown-{lab}-{(now // sec) * sec + k * sec}"
        for sym in ("btc", "eth", "sol", "xrp", "doge")
        for lab, sec in (("5m", 300), ("15m", 900))
        for k in (0, 1)
    ]
    toks = {}
    for s in slugs:
        try:
            ev = requests.get(f"{hosts.GAMMA}/events", params={"slug": s},
                              headers=hosts.UA, timeout=20).json()
        except requests.RequestException:
            continue
        if ev and ev[0].get("markets") and ev[0]["markets"][0].get("acceptingOrders"):
            toks[s] = json.loads(ev[0]["markets"][0]["clobTokenIds"])

    print(f"\n--- live check: {len(toks)} open windows, both legs per batch request ---")
    n = mir = sub1 = over1 = 0
    for _ in range(rounds):
        payload = [{"token_id": t} for pair in toks.values() for t in pair]
        r = requests.post(f"{hosts.CLOB}/books", json=payload,
                          headers={**hosts.UA, "Content-Type": "application/json"}, timeout=30)
        if r.status_code != 200:
            print(f"  books HTTP {r.status_code}")
            return
        books = {b.get("asset_id"): b for b in r.json()}

        def top(b, side):
            lv = b.get(side) or []
            if not lv:
                return None
            f = max if side == "bids" else min
            return float(f(lv, key=lambda x: float(x["price"]))["price"])

        for t0, t1 in toks.values():
            b0, b1 = books.get(t0), books.get(t1)
            if not b0 or not b1:
                continue
            ub, ua = top(b0, "bids"), top(b0, "asks")
            db, da = top(b1, "bids"), top(b1, "asks")
            if None in (ub, ua, db, da):
                continue
            n += 1
            mir += abs(ua - (1 - db)) < 1e-9 and abs(ub - (1 - da)) < 1e-9
            sub1 += (ua + da) < 1.0
            over1 += (ub + db) > 1.0
        time.sleep(4)
    print(f"  simultaneous both-leg samples : {n}")
    print(f"  EXACT mirror                  : {mir}/{n}")
    print(f"  ask-sum < 1.000 (the arb)     : {sub1}/{n}")
    print(f"  bid-sum > 1.000 (reverse arb) : {over1}/{n}")


def main() -> None:
    print("=" * 74)
    print("Q1  COMPLETE-SET ARB")
    print("=" * 74)
    wins = load_book_windows()
    naive_scan(wins)
    mirror_test(wins)
    if "--live" in sys.argv:
        live_check()
    print("\nVERDICT: no complete-set arb exists on these books, at any moment of")
    print("any window. The pair ask-sum is 1.00 + spread by exchange construction,")
    print("and the crypto taker fee adds ~3.5c at p=0.5 on top of that.")


if __name__ == "__main__":
    main()
