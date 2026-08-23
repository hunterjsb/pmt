"""Shared loading/labelling for the resolution-farmer study.

Joins the gamma metadata corpus to the CLOB price history and turns each market into a set of
candidate ENTRIES: "at endDate minus Δ the favorite traded at p; did it win?".
"""

from __future__ import annotations

import datetime as dt
import json
import os
from pathlib import Path

OUT = Path(os.path.expanduser("~/.pmt/resfarm"))

DELTAS_H = (6.0, 4.0, 3.0, 2.0, 1.0)
# how stale the nearest sample may be before we refuse to call it "the price at T-Δ"
MAX_STALE_S = 20 * 60

# prices-history returns the BOOK MIDPOINT (verified against live bestBid/bestAsk), so these
# are mid buckets; the ask is mid + half-spread and half-spread is a measured input, not a guess.
PRICE_BUCKETS = ((0.90, 0.93), (0.93, 0.95), (0.95, 0.97), (0.97, 0.985),
                 (0.985, 0.995), (0.995, 0.999), (0.999, 1.001))

# first match wins -- ordered most-specific to least
CATEGORY_RULES = (
    ("crypto-updown", ("up-or-down",)),
    ("weather", ("weather", "daily-temperature", "highest-temperature")),
    ("esports", ("esports", "counter-strike-2", "league-of-legends", "dota", "valorant")),
    ("soccer", ("soccer", "fifa-world-cup", "ucl", "mls", "epl", "europa-conference-league")),
    ("tennis", ("tennis",)),
    ("baseball", ("mlb", "baseball")),
    ("basketball", ("nba", "basketball", "wnba")),
    ("football", ("nfl", "football", "cfb")),
    ("hockey", ("nhl", "hockey")),
    ("mma", ("mma", "ufc", "boxing")),
    ("motorsport", ("f1", "nascar", "motorsport")),
    ("sports-other", ("sports", "games")),
    ("crypto-other", ("crypto", "crypto-prices", "bitcoin", "ethereum")),
    ("politics", ("politics", "elections", "us-politics", "trump")),
    ("geopolitics", ("geopolitics", "world", "middle-east", "russia-ukraine")),
    ("economics", ("economics", "fed", "inflation", "cpi")),
    ("finance", ("finance", "stocks", "sp500")),
    ("mentions", ("mentions", "mention-markets")),
    ("culture", ("pop-culture", "culture", "entertainment", "movies", "music", "awards")),
    ("tech", ("tech", "ai", "openai")),
)


def iso(s):
    """gamma emits three timestamp shapes: ISO-Z, bare ISO, and '2026-08-08 20:33:13+00'."""
    if not s:
        return None
    s = str(s).strip()
    try:
        if s.endswith("Z"):
            return dt.datetime.fromisoformat(s[:-1] + "+00:00")
        s2 = s.replace(" ", "T")
        if s2.endswith("+00"):
            s2 += ":00"
        d = dt.datetime.fromisoformat(s2)
        return d if d.tzinfo else d.replace(tzinfo=dt.timezone.utc)
    except Exception:
        return None


def category(tags) -> str:
    ts = set(tags or [])
    for name, keys in CATEGORY_RULES:
        if ts & set(keys):
            return name
    return "other"


def bucket(p: float) -> str | None:
    for lo, hi in PRICE_BUCKETS:
        if lo <= p < hi:
            return f"{lo:.3f}-{min(hi,1.0):.3f}"
    return None


def winner_side(outcome_prices) -> str | None:
    """'YES'/'NO'/'SPLIT' from the resolved outcomePrices pair, or None if unresolved."""
    try:
        pr = json.loads(outcome_prices) if isinstance(outcome_prices, str) else outcome_prices
        y = float(pr[0])
    except Exception:
        return None
    if y >= 0.99:
        return "YES"
    if y <= 0.01:
        return "NO"
    if 0.4 < y < 0.6:
        return "SPLIT"
    return None


def load_markets(vol_min: float = 0.0) -> dict:
    out = {}
    with (OUT / "markets.jsonl").open() as f:
        for line in f:
            try:
                m = json.loads(line)
            except Exception:
                continue
            if float(m.get("volume") or 0) >= vol_min:
                out[m["id"]] = m
    return out


def load_prices() -> dict:
    out = {}
    with (OUT / "prices.jsonl").open() as f:
        for line in f:
            try:
                r = json.loads(line)
            except Exception:
                continue
            if r.get("t"):
                out[r["id"]] = (r["t"], r["p"])
    return out


def price_at(ts, ps, target: int):
    """Last observation at or before `target`, plus its staleness in seconds.

    prices-history carries the last price forward, so a hit only means "nothing has traded
    since" -- staleness is the honest liquidity caveat and is returned, not hidden.
    """
    lo, hi = 0, len(ts) - 1
    if not ts or ts[0] > target:
        return None, None
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if ts[mid] <= target:
            lo = mid
        else:
            hi = mid - 1
    return ps[lo], target - ts[lo]


def build_entries(markets: dict, prices: dict, deltas=DELTAS_H) -> list[dict]:
    """One row per (market, Δ) where the favorite was priced in a farmable band."""
    rows = []
    for mid, (ts, ps) in prices.items():
        m = markets.get(mid)
        if not m:
            continue
        end = iso(m.get("endDate"))
        closed = iso(m.get("closedTime")) or iso(m.get("umaEndDate"))
        win = winner_side(m.get("outcomePrices"))
        if end is None or win is None:
            continue
        t_end = int(end.timestamp())
        # the market must still have been quoting at/after endDate, else it closed early and a
        # scanner keyed on endDate would never have seen it live
        last_t = ts[-1]
        cat = category(m.get("tags"))
        vol = float(m.get("volume") or 0)
        fee_rate = float(((m.get("feeSchedule") or {}) or {}).get("rate") or 0.0) if m.get("feesEnabled") else 0.0
        distinct_late = len(set(p for t, p in zip(ts, ps) if t >= t_end - 6 * 3600))
        for dh in deltas:
            tgt = t_end - int(dh * 3600)
            p, stale = price_at(ts, ps, tgt)
            if p is None or stale is None or stale > MAX_STALE_S:
                continue
            if last_t < tgt + 600:
                continue  # series died before/at the sample -- not tradable then
            fav = "YES" if p >= 0.5 else "NO"
            pf = p if fav == "YES" else 1.0 - p
            b = bucket(pf)
            if b is None:
                continue
            # An ask can never be quoted above 1-tick, so the richest MID that can have a real
            # offer behind it is (1-2t + 1-t)/2 = 1-1.5t. Anything above that is a synthetic
            # ask of 1.000 standing in for an EMPTY ask ladder -- nobody is selling, and the
            # position is unbuyable at any size. Verified live: 8/8 such books had zero asks.
            tick = float(m.get("orderPriceMinTickSize") or 0.01)
            tradable = pf <= 1.0 - 1.5 * tick + 1e-9
            rows.append({
                "id": mid, "cat": cat, "delta_h": dh, "p_fav": pf, "bucket": b,
                "tradable": tradable,
                "fav": fav, "winner": win,
                "won": 1 if win == fav else (0 if win in ("YES", "NO") else None),
                "split": win == "SPLIT",
                "vol": vol, "fee_rate": fee_rate, "tick": tick,
                "stale_s": stale, "distinct_late": distinct_late,
                "end_ts": t_end,
                "closed_ts": int(closed.timestamp()) if closed else None,
                "hold_h": ((closed - end).total_seconds() / 3600.0 + dh) if closed else None,
                "neg_risk": bool(m.get("negRisk")),
                "event_slug": m.get("event_slug"),
                "question": m.get("question"),
                "slug": m.get("slug"),
                "last_trade": float(m.get("lastTradePrice") or 0),
                "uma_statuses": m.get("umaResolutionStatuses"),
                "auto": bool(m.get("automaticallyResolved")),
            })
    return rows
