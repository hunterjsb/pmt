"""Market discovery — the `pmt find` backend.

Every ad-hoc market hunt (sports bets, new updown series, live events) was
an inline python snippet with hand-built gamma URLs and a copied UA header.
This is that snippet, once, with the lessons baked in: public-search for
free text, direct slug probes as fallback, and token-id resolution in the
same call so a found market is immediately tradeable.
"""

from __future__ import annotations

import json

import requests

from . import hosts


def _get(url: str, params: dict | None = None) -> dict | list:
    r = requests.get(url, params=params, headers=hosts.UA, timeout=8)
    r.raise_for_status()
    return r.json()


def normalize_market(m: dict) -> dict:
    """One market row: slug, outcomes zipped with gamma prices + token ids."""
    try:
        outcomes = json.loads(m.get("outcomes") or "[]")
        prices = json.loads(m.get("outcomePrices") or "[]") if isinstance(
            m.get("outcomePrices"), str) else (m.get("outcomePrices") or [])
        tokens = json.loads(m.get("clobTokenIds") or "[]")
    except (json.JSONDecodeError, TypeError):
        outcomes, prices, tokens = [], [], []
    sides = []
    for i, o in enumerate(outcomes):
        sides.append({
            "outcome": o,
            "price": float(prices[i]) if i < len(prices) else None,
            "token": tokens[i] if i < len(tokens) else None,
        })
    return {
        "slug": m.get("slug") or "",
        "question": m.get("question") or "",
        "closed": bool(m.get("closed")),
        "active": bool(m.get("active")),
        "liquidity": float(m.get("liquidity") or 0.0),
        "sides": sides,
    }


def find_markets(query: str, limit: int = 8, include_closed: bool = False) -> list[dict]:
    """Free-text search -> events with normalized markets, open ones first.

    Falls back to a direct market-slug probe when the query looks like a
    slug (has dashes, no spaces) — public-search misses exact slugs
    sometimes, direct lookup never does.
    """
    events: list[dict] = []
    try:
        d = _get(f"{hosts.GAMMA}/public-search",
                 {"q": query, "limit_per_type": limit})
        for ev in (d.get("events") or [])[:limit]:
            markets = [normalize_market(m) for m in (ev.get("markets") or [])]
            if not include_closed:
                markets = [m for m in markets if not m["closed"]]
            if markets:
                events.append({
                    "event": ev.get("slug") or "",
                    "start": ev.get("startDate") or "",
                    "markets": markets,
                })
        # Live-now beats relevance: a game happening today should outrank
        # a December futures market no matter what the search engine says.
        import datetime as _dt
        now = _dt.datetime.now(_dt.timezone.utc)

        def _staleness(ev: dict) -> float:
            try:
                s = _dt.datetime.fromisoformat(ev["start"].replace("Z", "+00:00"))
                return abs((now - s).total_seconds())
            except (ValueError, AttributeError):
                return float("inf")

        events.sort(key=_staleness)
    except requests.RequestException:
        pass

    if not events and "-" in query and " " not in query:
        try:
            d = _get(f"{hosts.GAMMA}/markets", {"slug": query})
            if d:
                m = normalize_market(d[0])
                if include_closed or not m["closed"]:
                    events.append({"event": query, "markets": [m]})
        except requests.RequestException:
            pass
    return events


def market_tokens(slug: str) -> dict[str, str]:
    """{outcome: token_id} for one market slug — the tradeable handles."""
    d = _get(f"{hosts.GAMMA}/markets", {"slug": slug})
    if not d:
        return {}
    m = normalize_market(d[0])
    return {s["outcome"]: s["token"] for s in m["sides"] if s["token"]}
