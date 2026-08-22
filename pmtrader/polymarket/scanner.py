"""Pre-trade due-diligence scanner for Polymarket events.

Four signals feed the verdict: sub-market thinness, resolution-risk
comment chatter, smart-money side asymmetry (lifetime-pnl-weighted),
and how identically-shaped sibling sub-markets in the same event
already resolved. Scoring/parsing functions are pure + network-free;
fetching is isolated below so tests can exercise the former offline.
"""

from __future__ import annotations

import json
import math
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests

from . import hosts

REQUEST_TIMEOUT = 10
THIN_VOLUME_USD = 25_000
EXTREME_PRICE = 0.03  # inside [EXTREME_PRICE, 1-EXTREME_PRICE] a market is still "live", not settled sentiment
FLAG_KEYWORDS = (
    "resolut", "resolve", "uma", "oracle", "scam", "rig", "rule",
    "technical", "announc", "clarif", "dispute", "ambig", "criteri",
)


def parse_event_ref(ref: str) -> str:
    """polymarket.com/event/<slug>[?query][/...] URL or bare slug -> slug."""
    s = ref.strip()
    if "polymarket.com/event/" in s:
        s = s.split("/event/", 1)[1]
    return s.split("/", 1)[0].split("?", 1)[0].split("#", 1)[0]


def sharp_score(pnl: float, volume: float, markets: int) -> float:
    """Lifetime pnl weighted toward volume/breadth; high-volume losers get penalized, not
    rewarded — they're churners, not sharps."""
    score = (
        math.log10(1 + max(pnl, 0))
        + 0.25 * math.log10(1 + max(volume, 0))
        + 0.25 * math.log10(1 + max(markets, 0))
    )
    if pnl < 0:
        score -= 0.5 * math.log10(1 + abs(pnl))
    return score


def side_sharpness(holders: list[dict]) -> float:
    """Share-weighted mean sharp_score across one side's top holders."""
    total = sum(h.get("amount", 0) for h in holders)
    if not holders or total <= 0:
        return 0.0
    return sum(
        h.get("amount", 0) * sharp_score(h.get("pnl", 0), h.get("volume", 0), h.get("markets", 0))
        for h in holders
    ) / total


def flag_comments(comments: list[dict]) -> dict:
    """Substring-match resolution-risk keywords against comment bodies."""
    flagged = [c for c in comments if any(k in (c.get("body") or "").lower() for k in FLAG_KEYWORDS)]
    total = len(comments)
    return {"total": total, "flagged": flagged, "ratio": (len(flagged) / total) if total else 0.0}


def _parse_prices(raw) -> list:
    """outcomePrices is normally a JSON-encoded string; tolerate already-parsed lists, empty
    strings, and malformed JSON (gamma has emitted all three) by skipping rather than raising."""
    if isinstance(raw, str):
        if not raw:
            return []
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return []
    return raw or []


def sibling_precedent(event: dict) -> list:
    """How closed sub-markets in this event already resolved (yes-price 1/0 -> YES/NO)."""
    out = []
    for m in event.get("markets") or []:
        if not m.get("closed"):
            continue
        prices = _parse_prices(m.get("outcomePrices"))
        if not prices:
            continue
        outcome = "YES" if float(prices[0]) >= 0.5 else "NO"
        out.append({
            "title": m.get("groupItemTitle") or m.get("question"),
            "volume": float(m.get("volume") or 0),
            "outcome": outcome,
        })
    return out


# ---------- network ----------

def fetch_event(slug: str) -> dict | None:
    r = requests.get(f"{hosts.GAMMA}/events", params={"slug": slug}, headers=hosts.UA, timeout=REQUEST_TIMEOUT)
    r.raise_for_status()
    events = r.json() or []
    return events[0] if events else None


def fetch_comments(event_id, limit: int = 100) -> list[dict]:
    r = requests.get(
        f"{hosts.GAMMA}/comments",
        params={
            "parent_entity_type": "Event", "parent_entity_id": event_id,
            "limit": limit, "order": "createdAt", "ascending": "false",
        },
        headers=hosts.UA, timeout=REQUEST_TIMEOUT,
    )
    r.raise_for_status()
    return r.json() or []


def fetch_holders(condition_id: str, limit: int = 10) -> list[dict]:
    r = requests.get(
        f"{hosts.DATA}/holders", params={"market": condition_id, "limit": limit},
        headers=hosts.UA, timeout=REQUEST_TIMEOUT,
    )
    r.raise_for_status()
    return r.json() or []


def fetch_wallet_stats(wallet: str) -> dict:
    """Lifetime pnl/volume/markets-traded for one wallet. Any leg failing -> 0 for that leg, never raises."""
    pnl = volume = 0.0
    markets = 0
    try:
        r = requests.get(f"{hosts.LB_API}/profit", params={"window": "all", "limit": 1, "address": wallet},
                          headers=hosts.UA, timeout=REQUEST_TIMEOUT)
        r.raise_for_status()
        data = r.json() or []
        pnl = float(data[0]["amount"]) if data else 0.0
    except Exception:
        pass
    try:
        r = requests.get(f"{hosts.LB_API}/volume", params={"window": "all", "limit": 1, "address": wallet},
                          headers=hosts.UA, timeout=REQUEST_TIMEOUT)
        r.raise_for_status()
        data = r.json() or []
        volume = float(data[0]["amount"]) if data else 0.0
    except Exception:
        pass
    try:
        r = requests.get(f"{hosts.DATA}/traded", params={"user": wallet}, headers=hosts.UA, timeout=REQUEST_TIMEOUT)
        r.raise_for_status()
        markets = int((r.json() or {}).get("traded") or 0)
    except Exception:
        pass
    return {"pnl": pnl, "volume": volume, "markets": markets}


def _enrich_holders(holders: list[dict]) -> list[dict]:
    """Attach lifetime pnl/volume/markets + sharp_score to each holder, fetched concurrently."""
    out = [dict(h) for h in holders]
    with ThreadPoolExecutor(max_workers=8) as pool:
        futs = {pool.submit(fetch_wallet_stats, h.get("proxyWallet", "")): i for i, h in enumerate(out)}
        for fut in as_completed(futs):
            i = futs[fut]
            stats = fut.result()
            out[i].update(stats)
            out[i]["score"] = sharp_score(stats["pnl"], stats["volume"], stats["markets"])
    return out


def _select_target(open_markets: list[dict], match: str | None) -> dict | None:
    """Highest-volume open sub-market with a conditionId; --match narrows first and must hit
    something real — silently falling back to the default would deep-dive the wrong market."""
    candidates = open_markets
    if match:
        m_low = match.lower()
        narrowed = [
            m for m in candidates
            if m_low in (m.get("question") or "").lower() or m_low in (m.get("groupItemTitle") or "").lower()
        ]
        if not narrowed:
            avail = ", ".join(
                m.get("groupItemTitle") or m.get("question") or "?" for m in candidates
            ) or "(no open sub-markets)"
            raise ValueError(f"--match '{match}' matched no open sub-market. Available: {avail}")
        candidates = narrowed
    candidates = [m for m in candidates if m.get("conditionId")]
    return max(candidates, key=lambda m: float(m.get("volume") or 0), default=None)


def scan_event(slug: str, match: str | None = None) -> dict:
    """Fetch + assemble everything the `pmt scan` CLI command needs."""
    slug = parse_event_ref(slug)
    event = fetch_event(slug)
    if not event:
        raise ValueError(f"No event found for slug '{slug}'")

    open_markets = [m for m in (event.get("markets") or []) if not m.get("closed") and not m.get("archived")]

    market_rows = []
    for m in open_markets:
        prices = _parse_prices(m.get("outcomePrices"))
        yes_price = float(prices[0]) if prices else None
        volume = float(m.get("volume") or 0)
        # 0 excluded: an unpriced, never-traded placeholder isn't "thin," it just hasn't started
        thin = (
            yes_price is not None and 0 < volume < THIN_VOLUME_USD
            and EXTREME_PRICE < yes_price < 1 - EXTREME_PRICE
        )
        market_rows.append({
            "title": m.get("groupItemTitle") or m.get("question") or "(unnamed)",
            "conditionId": m.get("conditionId") or None,
            "volume": volume,
            "yes_price": yes_price,
            "thin": thin,
        })

    comments = fetch_comments(event["id"]) if event.get("id") else []
    comment_flags = flag_comments(comments)
    siblings = sibling_precedent(event)

    target = _select_target(open_markets, match)
    target_row = next((r for r in market_rows if target and r["conditionId"] == target.get("conditionId")), None)

    holders_yes: list[dict] = []
    holders_no: list[dict] = []
    if target:
        raw_holders = fetch_holders(target["conditionId"])
        all_holders = [h for entry in raw_holders for h in (entry.get("holders") or [])]
        holders_yes = _enrich_holders([h for h in all_holders if h.get("outcomeIndex") == 0])
        holders_no = _enrich_holders([h for h in all_holders if h.get("outcomeIndex") == 1])

    score_yes = side_sharpness(holders_yes)
    score_no = side_sharpness(holders_no)
    verdict = _build_verdict(score_yes, score_no, target_row, comment_flags)

    return {
        "slug": slug,
        "event": {
            "title": event.get("title"),
            "volume": float(event.get("volume") or 0),
            "liquidity": float(event.get("liquidity") or 0),
            "commentCount": int(event.get("commentCount") or 0),
        },
        "markets": market_rows,
        "comments": comment_flags,
        "siblings": siblings,
        "target": {
            "title": target_row["title"] if target_row else None,
            "conditionId": target.get("conditionId") if target else None,
        },
        "holders_yes": holders_yes,
        "holders_no": holders_no,
        "score_yes": score_yes,
        "score_no": score_no,
        "verdict": verdict,
    }


def _build_verdict(score_yes: float, score_no: float, target_row: dict | None, comment_flags: dict) -> str:
    if target_row is None:
        return "no open sub-market with a tradable condition id to analyze"
    if score_yes == 0 and score_no == 0:
        return "no holder data — insufficient signal to call a side"

    if score_yes >= score_no:
        side, lead, other = "YES", score_yes, score_no
        price = target_row["yes_price"]
    else:
        side, lead, other = "NO", score_no, score_yes
        price = 1 - target_row["yes_price"] if target_row["yes_price"] is not None else None

    # elevated resolution-risk chatter is *why* this needs a scan in the first place
    if comment_flags["ratio"] > 0.2:
        tag = "the technicality side"
    elif target_row["thin"]:
        tag = "the thin side"
    elif price is not None and price >= 0.5:
        tag = "the favorite"
    else:
        tag = "the underdog"

    px = f"{price:.2f}" if price is not None else "?"
    return f"sharps are on {side} ({lead:.1f} vs {other:.1f}) @ {px} — {tag}"
