"""Survivorship check for the resolution-farmer study.

A `closed=true` corpus silently drops every market that blew past its endDate and is STILL
unresolved -- i.e. exactly the disputed/stuck tail the strategy is most exposed to. This
counts them against the resolved population for the same endDate window, and characterises
the UMA status of what is still hanging.
"""

from __future__ import annotations

import json
import os
import sys
import time
from collections import Counter
from pathlib import Path

import requests

UA = {"User-Agent": "pmtrader/1.0"}
G = "https://gamma-api.polymarket.com/markets/keyset"
OUT = Path(os.path.expanduser("~/.pmt/resfarm"))


def page_all(params: dict, cap: int = 400):
    rows, cur, g = [], None, 0
    while True:
        p = dict(params)
        p["limit"] = 100
        if cur:
            p["after_cursor"] = cur
        for attempt in range(5):
            try:
                r = requests.get(G, params=p, headers=UA, timeout=45)
                if r.status_code == 200:
                    break
            except Exception:
                pass
            time.sleep(1.0 * (attempt + 1))
        else:
            break
        d = r.json()
        ms = d.get("markets") or []
        rows.extend(ms)
        cur = d.get("next_cursor")
        g += 1
        if not ms or not cur or len(ms) < 100 or g > cap:
            break
        time.sleep(0.15)
    return rows


def main():
    lo, hi = sys.argv[1], sys.argv[2]
    vmin = sys.argv[3] if len(sys.argv) > 3 else "10000"
    base = {"end_date_min": f"{lo}T00:00:00Z", "end_date_max": f"{hi}T23:59:59Z",
            "volume_num_min": vmin, "include_tag": "true"}

    closed = page_all(dict(base, closed="true"))
    openm = page_all(dict(base, closed="false"))
    print(f"endDate in [{lo}, {hi}], volume >= ${float(vmin):,.0f}")
    print(f"  closed/resolved : {len(closed)}")
    print(f"  still open      : {len(openm)}")
    frac = len(openm) / max(len(closed) + len(openm), 1)
    print(f"  unresolved share: {frac:.2%}")
    print()
    print("  still-open uma status:", Counter(m.get("umaResolutionStatus") for m in openm).most_common())
    print("  still-open accepting orders:", Counter(m.get("acceptingOrders") for m in openm).most_common())
    tags = Counter()
    for m in openm:
        for t in (m.get("tags") or []):
            tags[t.get("slug") if isinstance(t, dict) else t] += 1
    print("  still-open top tags:", tags.most_common(12))
    print()
    print("  sample of still-open markets:")
    for m in sorted(openm, key=lambda x: -float(x.get("volume") or 0))[:20]:
        print(f"    ${float(m.get('volume') or 0):>10,.0f}  end={m.get('endDate')}  "
              f"uma={m.get('umaResolutionStatus')}  {(m.get('question') or '')[:70]}")

    with (OUT / "unresolved.json").open("w") as f:
        json.dump({"closed": len(closed), "open": len(openm),
                   "open_rows": [{k: m.get(k) for k in
                                  ("id", "question", "slug", "endDate", "volume",
                                   "umaResolutionStatus", "umaResolutionStatuses", "acceptingOrders")}
                                 for m in openm]}, f)


if __name__ == "__main__":
    main()
