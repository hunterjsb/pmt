"""Resolution-farmer study, stage 1: pull the resolved-market metadata corpus from gamma.

One JSONL row per closed market whose scheduled endDate falls in the window. Only the
fields the study needs are kept -- raw gamma rows are ~8KB each and we want ~70k of them.

gamma's offset pagination hard-caps at offset 2100 ("offset too large, use /markets/keyset");
a busy day has >2100 closed markets, so this uses the keyset cursor endpoint. `include_tag`
is what carries the real category labels (Politics / Sports / Crypto / ...).

Cache lives in ~/.pmt/resfarm/ -- scratchpads are tmpfs and the box powers off nightly.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import sys
import time
from pathlib import Path

import requests

UA = {"User-Agent": "pmtrader/1.0"}
GAMMA = "https://gamma-api.polymarket.com"
OUT = Path(os.path.expanduser("~/.pmt/resfarm"))
OUT.mkdir(parents=True, exist_ok=True)

PAGE = 100
SLEEP = 0.2

KEEP = (
    "id", "question", "slug", "conditionId", "clobTokenIds", "outcomes", "outcomePrices",
    "endDate", "startDate", "closedTime", "umaEndDate", "umaResolutionStatus",
    "umaResolutionStatuses", "volume", "feeType", "feeSchedule", "feesEnabled",
    "negRisk", "negRiskMarketID", "groupItemTitle", "resolvedBy", "resolutionSource",
    "orderPriceMinTickSize", "orderMinSize", "customLiveness", "umaBond",
    "automaticallyResolved", "lastTradePrice",
)


def _get(url: str, params: dict, tries: int = 6):
    """gamma intermittently 5xx's or returns a bare error object; retry with backoff."""
    for i in range(tries):
        try:
            r = requests.get(url, params=params, headers=UA, timeout=45)
            if r.status_code == 200:
                return r.json()
            if r.status_code == 422:
                return {"_fatal": r.text}
            time.sleep(1.0 * (i + 1))
        except Exception:
            time.sleep(1.0 * (i + 1))
    return None


def slim(m: dict) -> dict:
    out = {k: m.get(k) for k in KEEP}
    ev = (m.get("events") or [{}])[0]
    out["event_slug"] = ev.get("slug")
    out["event_title"] = ev.get("title")
    out["event_ticker"] = ev.get("ticker")
    out["event_id"] = ev.get("id")
    out["event_volume"] = ev.get("volume")
    out["event_comment_count"] = ev.get("commentCount")
    out["tags"] = [t.get("slug") for t in (m.get("tags") or []) if t.get("slug")]
    d = m.get("description") or ""
    out["desc_head"] = d[:600]
    return out


def fetch_day(day: str, vol_min: float) -> list[dict]:
    rows, cursor, guard = [], None, 0
    while True:
        p = {
            "closed": "true",
            "end_date_min": f"{day}T00:00:00Z",
            "end_date_max": f"{day}T23:59:59Z",
            "volume_num_min": str(vol_min),
            "limit": PAGE, "include_tag": "true",
        }
        if cursor:
            p["after_cursor"] = cursor
        d = _get(f"{GAMMA}/markets/keyset", p)
        if d is None or "_fatal" in (d if isinstance(d, dict) else {}):
            print(f"  !! {day} cursor page {guard} failed: {str(d)[:120]}", file=sys.stderr)
            break
        ms = d.get("markets") or []
        rows.extend(slim(m) for m in ms)
        cursor = d.get("next_cursor")
        guard += 1
        if not ms or not cursor or len(ms) < PAGE or guard > 120:
            break
        time.sleep(SLEEP)
    return rows


def main():
    start = dt.date.fromisoformat(sys.argv[1])
    end = dt.date.fromisoformat(sys.argv[2])
    vol_min = float(sys.argv[3]) if len(sys.argv) > 3 else 0.0
    path = OUT / "markets.jsonl"
    seen = set()
    if path.exists():
        with path.open() as f:
            for line in f:
                try:
                    seen.add(json.loads(line)["id"])
                except Exception:
                    pass
    print(f"resuming with {len(seen)} markets already cached", flush=True)
    with path.open("a") as f:
        d = start
        while d <= end:
            day = d.isoformat()
            rows = fetch_day(day, vol_min)
            new = [r for r in rows if r["id"] not in seen]
            for r in new:
                seen.add(r["id"])
                f.write(json.dumps(r) + "\n")
            f.flush()
            print(f"{day}: {len(rows)} fetched, {len(new)} new (total {len(seen)})", flush=True)
            d += dt.timedelta(days=1)
            time.sleep(SLEEP)


if __name__ == "__main__":
    main()
