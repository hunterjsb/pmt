"""Backfill the Polymarket print tape for every window in the book corpus.

ROADMAP Phase 0 lists "Polymarket prints" as forward-only, and the engine's
own live recorder (updown.rs record_book -> up_tn/up_tbuy/...) has been
writing ~zeros. Both turn out to be wrong: data-api /trades serves the full
public print history per market, by slug, long after the window resolves.
This harvests it into a corpus file the study scripts (and R8/VPIN) can read.

Writes ~/.pmt/corpus/prints.jsonl — append-only, deduped by transactionHash
+ asset + timestamp + size, so re-running only adds what's missing.

Run: uv run python analysis/firsthalf_harvest_prints.py [max_windows]
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).parent))

from firsthalf_lib import load_book_windows, parse_slug  # noqa: E402

TRADES_URL = "https://data-api.polymarket.com/trades"
UA = {"User-Agent": "pmt-research/1.0"}
OUT = Path.home() / ".pmt" / "corpus" / "prints.jsonl"
PAGE = 500


GAMMA_URL = "https://gamma-api.polymarket.com/events"


def condition_id(slug: str) -> str | None:
    """conditionId for a window slug, via gamma.

    Needed because data-api /trades SILENTLY IGNORES a `slug` filter — it
    answers with the global cross-market feed and a 200, so a slug-keyed
    harvest looks like it worked and is entirely wrong. Only `market=<cid>`
    actually filters; every row is checked against it below.
    """
    r = requests.get(GAMMA_URL, params={"slug": slug}, headers=UA, timeout=30)
    if r.status_code != 200:
        return None
    evs = r.json()
    if not evs or not evs[0].get("markets"):
        return None
    return evs[0]["markets"][0].get("conditionId")


def fetch_window(slug: str, cid: str, pause: float = 0.3) -> list[dict]:
    """Every public print for one window, paginated to exhaustion."""
    out: list[dict] = []
    seen: set[tuple] = set()
    offset = 0
    while True:
        r = requests.get(
            TRADES_URL,
            params={"market": cid, "limit": PAGE, "offset": offset},
            headers=UA,
            timeout=40,
        )
        if r.status_code != 200:
            print(f"  ! {slug} offset={offset} HTTP {r.status_code}", file=sys.stderr)
            break
        page = r.json()
        if not page:
            break
        fresh = 0
        for t in page:
            if t.get("conditionId") != cid:  # paranoia: the API ignores filters it dislikes
                continue
            key = (t.get("transactionHash"), t.get("asset"), t.get("timestamp"), t.get("size"))
            if key in seen:
                continue
            seen.add(key)
            out.append(t)
            fresh += 1
        offset += PAGE
        if len(page) < PAGE or fresh == 0 or offset > 20000:
            break
        time.sleep(pause)
    return out


def slim(t: dict, slug: str) -> dict:
    """Only the fields any study needs — the profile blob is 80% of the payload."""
    return {
        "slug": slug,
        "t": t.get("timestamp"),
        "asset": t.get("asset"),
        "outcome": (t.get("outcome") or "").lower(),
        "side": (t.get("side") or "").lower(),
        "size": t.get("size"),
        "price": t.get("price"),
        "wallet": t.get("proxyWallet"),
        "tx": t.get("transactionHash"),
    }


def main() -> None:
    limit = int(sys.argv[1]) if len(sys.argv) > 1 else 10**9
    wins = load_book_windows()
    slugs = sorted(wins, key=lambda s: parse_slug(s)["start"])

    done: set[str] = set()
    if OUT.exists():
        with OUT.open() as f:
            for line in f:
                try:
                    done.add(json.loads(line)["slug"])
                except (ValueError, KeyError):
                    continue
    todo = [s for s in slugs if s not in done][:limit]
    print(f"{len(slugs)} windows in book corpus, {len(done)} already harvested, fetching {len(todo)}")

    OUT.parent.mkdir(parents=True, exist_ok=True)
    total = 0
    with OUT.open("a") as f:
        for i, slug in enumerate(todo, 1):
            try:
                cid = condition_id(slug)
                if not cid:
                    print(f"  ! {slug}: no conditionId", file=sys.stderr)
                    continue
                trades = fetch_window(slug, cid)
            except requests.RequestException as e:
                print(f"  ! {slug}: {e}", file=sys.stderr)
                continue
            for t in trades:
                f.write(json.dumps(slim(t, slug), separators=(",", ":")) + "\n")
            f.flush()
            total += len(trades)
            print(f"[{i}/{len(todo)}] {slug:<32} {len(trades):>5} prints")
            time.sleep(0.35)
    print(f"harvested {total} prints -> {OUT}")


if __name__ == "__main__":
    main()
