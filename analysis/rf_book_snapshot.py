"""Resolution-farmer study, stage 5: what the ask side actually looks like at 93-99c.

Books are not backfillable, so the historical study has to ASSUME an execution cost. This
measures it instead: one live snapshot over every non-updown market whose endDate is inside
the next N hours, giving the real spread and the real VWAP to fill $50 / $100 / $500 on the
favourite. It doubles as the operational supply count -- how many qualifying markets exist
in a window at all.

Snapshot, not a study: run it a few times across the day/week before trusting the numbers.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import requests

UA = {"User-Agent": "pmtrader/1.0"}
GAMMA = "https://gamma-api.polymarket.com"
CLOB = "https://clob.polymarket.com"
OUT = Path(os.path.expanduser("~/.pmt/resfarm"))
OUT.mkdir(parents=True, exist_ok=True)

RATE = 8.0
_lock = threading.Lock()
_slot = [time.monotonic()]


def throttle():
    with _lock:
        now = time.monotonic()
        s = max(now, _slot[0])
        _slot[0] = s + 1.0 / RATE
    d = s - time.monotonic()
    if d > 0:
        time.sleep(d)


def page(params, cap=200):
    rows, cur, g = [], None, 0
    while True:
        p = dict(params)
        p["limit"] = 100
        if cur:
            p["after_cursor"] = cur
        throttle()
        try:
            r = requests.get(f"{GAMMA}/markets/keyset", params=p, headers=UA, timeout=45)
            if r.status_code != 200:
                break
            d = r.json()
        except Exception:
            break
        ms = d.get("markets") or []
        rows.extend(ms)
        cur = d.get("next_cursor")
        g += 1
        if not ms or not cur or len(ms) < 100 or g > cap:
            break
    return rows


def vwap_fill(levels, notional):
    """Walk the ask ladder for `notional` dollars; returns (vwap, filled, levels_used)."""
    spent = shares = 0.0
    used = 0
    for lv in levels:
        px = float(lv["price"])
        sz = float(lv["size"])
        avail = px * sz
        take = min(avail, notional - spent)
        if take <= 0:
            break
        shares += take / px
        spent += take
        used += 1
        if spent >= notional - 1e-9:
            break
    if shares == 0:
        return None, 0.0, 0
    return spent / shares, spent, used


def snap(m):
    try:
        toks = json.loads(m["clobTokenIds"])
    except Exception:
        return None
    ba, bb = m.get("bestAsk"), m.get("bestBid")
    if ba is None or bb is None:
        return None
    mid = (float(ba) + float(bb)) / 2
    fav_idx = 0 if mid >= 0.5 else 1
    throttle()
    try:
        r = requests.get(f"{CLOB}/book", params={"token_id": toks[fav_idx]}, headers=UA, timeout=25)
        if r.status_code != 200:
            return None
        b = r.json()
    except Exception:
        return None
    # CLOB returns asks worst-first; sort ascending so we cross the cheapest first
    asks = sorted((b.get("asks") or []), key=lambda x: float(x["price"]))
    bids = sorted((b.get("bids") or []), key=lambda x: -float(x["price"]))
    if not asks:
        return None
    best_ask = float(asks[0]["price"])
    best_bid = float(bids[0]["price"]) if bids else 0.0
    out = {
        "id": m["id"], "question": (m.get("question") or "")[:110],
        "endDate": m.get("endDate"), "volume": float(m.get("volume") or 0),
        "tags": [t.get("slug") for t in (m.get("tags") or []) if isinstance(t, dict)],
        "fee_rate": float(((m.get("feeSchedule") or {})).get("rate") or 0.0) if m.get("feesEnabled") else 0.0,
        "tick": float(m.get("orderPriceMinTickSize") or 0.01),
        "fav_side": "YES" if fav_idx == 0 else "NO",
        "mid_fav": mid if fav_idx == 0 else 1 - mid,
        "best_ask": best_ask, "best_bid": best_bid, "spread": best_ask - best_bid,
        "ask_top_notional": best_ask * float(asks[0]["size"]),
        "ask_total_notional": sum(float(a["price"]) * float(a["size"]) for a in asks),
    }
    for n in (50, 100, 500, 2000):
        v, filled, used = vwap_fill(asks, n)
        out[f"vwap{n}"] = v
        out[f"filled{n}"] = filled
        out[f"levels{n}"] = used
    return out


def main():
    hours = float(sys.argv[1]) if len(sys.argv) > 1 else 8.0
    vol_min = sys.argv[2] if len(sys.argv) > 2 else "10000"
    now = dt.datetime.now(dt.timezone.utc)
    lo = now.strftime("%Y-%m-%dT%H:%M:%SZ")
    hi = (now + dt.timedelta(hours=hours)).strftime("%Y-%m-%dT%H:%M:%SZ")
    ms = page({"closed": "false", "active": "true", "archived": "false",
               "end_date_min": lo, "end_date_max": hi,
               "volume_num_min": vol_min, "include_tag": "true"})
    ms = [m for m in ms if "up-or-down" not in [t.get("slug") for t in (m.get("tags") or []) if isinstance(t, dict)]]
    print(f"{len(ms)} live non-updown markets with endDate in the next {hours:g}h, vol >= ${float(vol_min):,.0f}")

    cand = []
    for m in ms:
        ba, bb = m.get("bestAsk"), m.get("bestBid")
        if ba is None or bb is None:
            continue
        mid = (float(ba) + float(bb)) / 2
        if max(mid, 1 - mid) >= 0.90:
            cand.append(m)
    print(f"{len(cand)} of them have a favourite at mid >= 0.90")

    res = []
    with ThreadPoolExecutor(max_workers=5) as pool:
        for r in pool.map(snap, cand):
            if r:
                res.append(r)
    stamp = now.strftime("%Y%m%dT%H%M%SZ")
    p = OUT / f"books_{stamp}.json"
    with p.open("w") as f:
        json.dump({"snapshot_utc": lo, "horizon_h": hours, "universe": len(ms),
                   "candidates": len(cand), "books": res}, f, indent=1)
    print(f"wrote {p} ({len(res)} books)")

    for band in ((0.90, 0.93), (0.93, 0.95), (0.95, 0.97), (0.97, 0.99), (0.99, 1.0)):
        sel = [r for r in res if band[0] <= r["best_ask"] < band[1]]
        if not sel:
            continue
        sp = sorted(r["spread"] for r in sel)
        s100 = sorted((r["vwap100"] - r["best_ask"]) for r in sel if r["vwap100"])
        fill100 = sum(1 for r in sel if r["filled100"] >= 99.9)
        fill500 = sum(1 for r in sel if r["filled500"] >= 499.9)
        print(f"ask {band[0]:.2f}-{band[1]:.2f}: n={len(sel):>3}  "
              f"med spread={sp[len(sp)//2]:.4f}  "
              f"med slip-to-$100={(s100[len(s100)//2] if s100 else float('nan')):.4f}  "
              f"$100 fillable {fill100}/{len(sel)}  $500 fillable {fill500}/{len(sel)}")


if __name__ == "__main__":
    main()
