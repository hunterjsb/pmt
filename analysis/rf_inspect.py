"""Resolution-farmer study, stage 4: pull everything needed to hand-classify one failure.

For a market that was >=93c three hours before its end and still lost, this fetches the
resolution rules, the event comment thread (scored with the same keyword bank `pmt scan`
uses), the actual trade tape around the close, and the price path -- enough to say
"genuine upset" vs "technicality" without guessing.
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

import requests

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "pmtrader"))

import rf_lib as L  # noqa: E402

UA = {"User-Agent": "pmtrader/1.0"}
GAMMA = "https://gamma-api.polymarket.com"
DATA = "https://data-api.polymarket.com"
OUT = Path(os.path.expanduser("~/.pmt/resfarm"))

FLAG_KEYWORDS = (
    "resolut", "resolve", "uma", "oracle", "scam", "rig", "rule",
    "technical", "announc", "clarif", "dispute", "ambig", "criteri",
)


def get(url, params, tries=4):
    for i in range(tries):
        try:
            r = requests.get(url, params=params, headers=UA, timeout=30)
            if r.status_code == 200:
                return r.json()
        except Exception:
            pass
        time.sleep(0.8 * (i + 1))
    return None


def inspect(mid: str) -> dict:
    # `closed` defaults to false on /markets, so a resolved market is invisible without this
    m = (get(f"{GAMMA}/markets", {"id": mid, "closed": "true", "include_tag": "true"}) or [None])[0]
    if not m:
        return {"id": mid, "error": "market not found"}
    ev = (m.get("events") or [{}])[0]
    out = {
        "id": mid,
        "question": m.get("question"),
        "slug": m.get("slug"),
        "url": f"https://polymarket.com/event/{ev.get('slug')}" if ev.get("slug") else None,
        "event_title": ev.get("title"),
        "tags": [t.get("slug") for t in (m.get("tags") or [])],
        "endDate": m.get("endDate"),
        "closedTime": m.get("closedTime"),
        "umaEndDate": m.get("umaEndDate"),
        "umaResolutionStatuses": m.get("umaResolutionStatuses"),
        "outcomes": m.get("outcomes"),
        "outcomePrices": m.get("outcomePrices"),
        "volume": m.get("volume"),
        "lastTradePrice": m.get("lastTradePrice"),
        "negRisk": m.get("negRisk"),
        "resolutionSource": m.get("resolutionSource"),
        "description": (m.get("description") or "")[:2500],
        "conditionId": m.get("conditionId"),
    }
    time.sleep(0.3)
    cs = get(f"{GAMMA}/comments", {"parent_entity_type": "Event", "parent_entity_id": ev.get("id"),
                                   "limit": 100, "order": "createdAt", "ascending": "false"}) or []
    flagged = [c for c in cs if any(k in (c.get("body") or "").lower() for k in FLAG_KEYWORDS)]
    out["comments_total"] = len(cs)
    out["comments_flagged"] = len(flagged)
    out["comments_ratio"] = len(flagged) / len(cs) if cs else 0.0
    out["comment_samples"] = [
        {"t": c.get("createdAt"), "body": (c.get("body") or "")[:300]} for c in flagged[:12]
    ]
    time.sleep(0.3)
    trades = get(f"{DATA}/trades", {"market": m.get("conditionId"), "limit": 500}) or []
    out["trade_count_sampled"] = len(trades)
    if trades:
        sizes = sorted(float(t.get("size") or 0) * float(t.get("price") or 0) for t in trades)
        out["trade_notional_median"] = sizes[len(sizes) // 2]
        out["trade_notional_p90"] = sizes[int(len(sizes) * 0.9)]
        out["trade_notional_sum"] = sum(sizes)
    return out


def main():
    args = sys.argv[1:]
    out_name = "failure_dossiers.json"
    if args and args[0] == "--sample":
        # a random draw across the whole corpus, to check the by-volume top-30 was representative
        import random
        n = int(args[1]) if len(args) > 1 else 15
        losers = json.load((OUT / "losers_d3.json").open())
        random.seed(11)
        ids = [r["id"] for r in random.sample(losers, min(n, len(losers)))]
        out_name = "failure_dossiers_random.json"
    elif args:
        ids = args
    else:
        losers = json.load((OUT / "losers_d3.json").open())
        ids = [r["id"] for r in losers[:30]]
    res = []
    for i, mid in enumerate(ids):
        r = inspect(mid)
        res.append(r)
        print(f"[{i+1}/{len(ids)}] {r.get('question','?')[:80]}", flush=True)
        time.sleep(0.4)
    with (OUT / out_name).open("w") as f:
        json.dump(res, f, indent=1)
    print(f"wrote {OUT/out_name}")


if __name__ == "__main__":
    main()
