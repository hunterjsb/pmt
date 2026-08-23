"""Resolution-farmer study, stage 2: price history in the final hours before each market's
scheduled end.

One CLOB /prices-history call per market over [endDate-8h, endDate+2h] at 5-minute fidelity.
The YES token is enough -- NO is 1-YES on a binary CTF pair.

Anchoring on endDate (not closedTime) is deliberate: a live scanner keys on "endDate is within
N hours", which involves no knowledge of when UMA actually got around to resolving. closedTime
is kept separately as the hold-time / redemption-lag measurement.

Gentle by construction: a global token bucket caps the whole pool at RATE requests/second.
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
CLOB = "https://clob.polymarket.com"
OUT = Path(os.path.expanduser("~/.pmt/resfarm"))

RATE = 10.0          # requests/second across the whole pool
WORKERS = 6
PRE_H = 8            # hours of history before endDate
POST_H = 2
FIDELITY = 5         # minutes

_lock = threading.Lock()
_next_slot = [time.monotonic()]


def throttle():
    with _lock:
        now = time.monotonic()
        slot = max(now, _next_slot[0])
        _next_slot[0] = slot + 1.0 / RATE
    d = slot - time.monotonic()
    if d > 0:
        time.sleep(d)


def iso(s: str | None):
    if not s:
        return None
    s = s.strip()
    try:
        if s.endswith("Z"):
            return dt.datetime.fromisoformat(s[:-1] + "+00:00")
        if "+" in s[10:] or s.endswith("+00"):
            # gamma's closedTime is "2026-08-08 20:33:13+00" -- not ISO
            s2 = s.replace(" ", "T")
            if s2.endswith("+00"):
                s2 += ":00"
            return dt.datetime.fromisoformat(s2)
        return dt.datetime.fromisoformat(s).replace(tzinfo=dt.timezone.utc)
    except Exception:
        return None


def fetch_one(m: dict) -> dict | None:
    end = iso(m.get("endDate"))
    if end is None:
        return None
    try:
        toks = json.loads(m.get("clobTokenIds") or "[]")
    except Exception:
        return None
    if not toks:
        return None
    ts_end = int(end.timestamp()) + POST_H * 3600
    ts_start = int(end.timestamp()) - PRE_H * 3600
    for attempt in range(4):
        throttle()
        try:
            r = requests.get(
                f"{CLOB}/prices-history",
                params={"market": toks[0], "startTs": ts_start, "endTs": ts_end, "fidelity": FIDELITY},
                headers=UA, timeout=30,
            )
            if r.status_code == 200:
                h = r.json().get("history") or []
                return {
                    "id": m["id"],
                    "t": [p["t"] for p in h],
                    "p": [p["p"] for p in h],
                }
            if r.status_code in (400, 404):
                return {"id": m["id"], "t": [], "p": [], "err": r.status_code}
        except Exception:
            pass
        time.sleep(1.0 * (attempt + 1))
    return {"id": m["id"], "t": [], "p": [], "err": "retry"}


def main():
    vol_min = float(sys.argv[1]) if len(sys.argv) > 1 else 10000.0
    markets = []
    with (OUT / "markets.jsonl").open() as f:
        for line in f:
            try:
                m = json.loads(line)
            except Exception:
                continue  # tolerate a torn last line while stage 1 is still appending
            if float(m.get("volume") or 0) < vol_min:
                continue
            # the crypto up/down series is the fleet's existing book, not this study's universe
            if "up-or-down" in (m.get("tags") or []):
                continue
            markets.append(m)
    path = OUT / "prices.jsonl"
    done = set()
    if path.exists():
        with path.open() as f:
            for line in f:
                try:
                    done.add(json.loads(line)["id"])
                except Exception:
                    pass
    todo = [m for m in markets if m["id"] not in done]
    print(f"{len(markets)} markets >= ${vol_min:,.0f}; {len(done)} cached; {len(todo)} to fetch", flush=True)

    n = 0
    t0 = time.time()
    with path.open("a") as f, ThreadPoolExecutor(max_workers=WORKERS) as pool:
        for res in pool.map(fetch_one, todo):
            n += 1
            if res:
                f.write(json.dumps(res) + "\n")
            if n % 500 == 0:
                f.flush()
                el = time.time() - t0
                print(f"{n}/{len(todo)}  {el/60:.1f}min  eta {(len(todo)-n)*el/max(n,1)/60:.0f}min", flush=True)


if __name__ == "__main__":
    main()
