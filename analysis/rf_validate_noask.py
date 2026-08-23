"""Resolution-farmer study: validate the "empty ask ladder" heuristic against live books.

The whole study leans on one inference: a recorded MID above 1 - 1.5*tick means the ask side is
a synthetic 1.000 standing in for an empty ladder, so the position is unbuyable. That inference
is made from historical mids, where no book was recorded. This checks it against live CLOB books
in both directions -- markets gamma says have bestAsk == 1.0, and markets it says do not.
"""

from __future__ import annotations

import json
import sys
import time
from collections import Counter

import requests

UA = {"User-Agent": "pmtrader/1.0"}
GAMMA = "https://gamma-api.polymarket.com/markets/keyset"
CLOB = "https://clob.polymarket.com/book"


def get(url, params, tries=5):
    for i in range(tries):
        try:
            r = requests.get(url, params=params, headers=UA, timeout=45)
            if r.status_code == 200:
                return r.json()
        except Exception:
            pass
        time.sleep(1.0 * (i + 1))
    return None


def main():
    want = int(sys.argv[1]) if len(sys.argv) > 1 else 25
    rows, cur, g = [], None, 0
    while len(rows) < 1500 and g < 20:
        p = {"closed": "false", "active": "true", "limit": 100, "volume_num_min": 5000}
        if cur:
            p["after_cursor"] = cur
        d = get(GAMMA, p)
        if not d:
            break
        ms = d.get("markets") or []
        rows.extend(ms)
        cur = d.get("next_cursor")
        g += 1
        if not ms or not cur:
            break
        time.sleep(0.2)
    print(f"scanned {len(rows)} live markets")

    empty_pred, real_pred = [], []
    for m in rows:
        bb, ba = m.get("bestBid"), m.get("bestAsk")
        if bb is None or ba is None:
            continue
        tick = float(m.get("orderPriceMinTickSize") or 0.01)
        try:
            toks = json.loads(m["clobTokenIds"])
        except Exception:
            continue
        for idx, (bid, ask) in enumerate(((float(bb), float(ba)), (1 - float(ba), 1 - float(bb)))):
            mid = (bid + ask) / 2
            if mid < 0.90:
                continue
            # the study's rule, applied to the mid alone -- no book knowledge
            (empty_pred if mid > 1 - 1.5 * tick + 1e-9 else real_pred).append((m, toks[idx], mid))

    print(f"predicted UNBUYABLE (mid > 1-1.5t): {len(empty_pred)}")
    print(f"predicted BUYABLE   (mid <= 1-1.5t): {len(real_pred)}")

    res = Counter()
    for label, pool in (("predicted-UNBUYABLE", empty_pred), ("predicted-BUYABLE", real_pred)):
        checked = 0
        for m, tid, mid in pool:
            if checked >= want:
                break
            b = get(CLOB, {"token_id": tid})
            time.sleep(0.3)
            if b is None:
                continue
            asks = b.get("asks") or []
            checked += 1
            res[(label, "book has asks" if asks else "book EMPTY")] += 1
        print(f"  {label}: checked {checked}")

    print("\n=== heuristic vs live book ===")
    for k, v in sorted(res.items()):
        print(f"  {k[0]:<22} -> {k[1]:<16} {v}")
    tp = res[("predicted-UNBUYABLE", "book EMPTY")]
    fp = res[("predicted-UNBUYABLE", "book has asks")]
    tn = res[("predicted-BUYABLE", "book has asks")]
    fn = res[("predicted-BUYABLE", "book EMPTY")]
    if tp + fp:
        print(f"\n  unbuyable prediction precision: {tp}/{tp+fp} ({tp/(tp+fp):.1%})")
    if tn + fn:
        print(f"  buyable prediction precision  : {tn}/{tn+fn} ({tn/(tn+fn):.1%})")


if __name__ == "__main__":
    main()
