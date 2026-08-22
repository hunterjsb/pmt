"""Distribution-fit for exchange price-bucket events.

Prices every touch bucket (↓/↑ barrier) in an event two ways — analytic
(driftless reflection: 2Φ(-z)) and empirical (how often same-length
historical windows actually crossed that far) — then compares against the
market's YES price to surface buckets that don't fit the distribution.
Rules quirks that burned us are first-class: only price action since
market creation counts, so already-touched buckets are checked against
exchange candles and reported as settled rather than modeled.
"""

from __future__ import annotations

import json
import math
import re
from datetime import datetime, timezone

import requests

from . import hosts
from .scanner import fetch_event

REQUEST_TIMEOUT = 15
BINANCE_DATA = "https://data-api.binance.vision"  # public mirror; binance.com geoblocks US
EDGE_FLAG = 0.05

_PAIR_RE = re.compile(r"([A-Z0-9]{2,10})/([A-Z0-9]{3,6})")
_NUM_RE = re.compile(r"(\d[\d,]*\.?\d*)")


def parse_bucket(market: dict) -> dict | None:
    """Touch bucket from a sub-market: direction + barrier, or None if not that shape.

    Titles look like "↓ 70,000" / "↑ 90,000"; the description's Low/lower vs
    High/higher wording is the fallback when the arrow is absent.
    """
    title = market.get("groupItemTitle") or market.get("question") or ""
    desc = (market.get("description") or "").lower()

    direction = None
    if "↓" in title:
        direction = "down"
    elif "↑" in title:
        direction = "up"
    elif "low price equal to or lower" in desc:
        direction = "down"
    elif "high price equal to or higher" in desc:
        direction = "up"
    if direction is None:
        return None
    # touch semantics only — terminal markets need a different model entirely
    if desc and "candle" not in desc:
        return None

    m = _NUM_RE.search(title)
    if not m:
        return None
    return {"direction": direction, "barrier": float(m.group(1).replace(",", ""))}


def parse_symbol(description: str) -> str | None:
    """Resolution pair from the description ("BTC/USDT" -> "BTCUSDT")."""
    m = _PAIR_RE.search(description or "")
    return (m.group(1) + m.group(2)) if m else None


def realized_sigma(closes: list[float], lookback: int) -> float:
    """Sample stdev of log close-to-close returns over the last `lookback` candles."""
    cs = closes[-(lookback + 1):]
    if len(cs) < 3:
        return 0.0
    rets = [math.log(cs[i + 1] / cs[i]) for i in range(len(cs) - 1)]
    mu = sum(rets) / len(rets)
    return math.sqrt(sum((r - mu) ** 2 for r in rets) / (len(rets) - 1))


def _norm_cdf(x: float) -> float:
    return 0.5 * math.erfc(-x / math.sqrt(2))


def touch_prob_analytic(spot: float, barrier: float, direction: str, sigma: float, horizon: float) -> float:
    """P(any touch) under a driftless walk: 2Φ(-z), the reflection-principle bound.

    sigma is per-candle, horizon in the same candle units.
    """
    if spot <= 0 or barrier <= 0 or horizon <= 0:
        return 0.0
    if (direction == "down" and spot <= barrier) or (direction == "up" and spot >= barrier):
        return 1.0
    if sigma <= 0:
        return 0.0
    z = abs(math.log(barrier / spot)) / (sigma * math.sqrt(horizon))
    return min(1.0, 2.0 * _norm_cdf(-z))


def touch_prob_empirical(closes: list[float], extremes: list[float], ratio: float,
                         direction: str, window: int) -> float | None:
    """Fraction of rolling windows whose extreme crossed start_close * ratio.

    `extremes` are per-candle lows (down) or highs (up); candle-period extremes
    are exact minima/maxima, so touch detection loses nothing to granularity.
    """
    n = len(closes) - window
    if n < 20:  # too few windows to mean anything
        return None
    hits = 0
    for i in range(n):
        target = closes[i] * ratio
        ext = extremes[i + 1:i + 1 + window]
        if direction == "down":
            hits += min(ext) <= target
        else:
            hits += max(ext) >= target
    return hits / n


# ---------- network ----------

def fetch_klines(symbol: str, interval: str, start_ms: int | None = None, limit: int = 1000) -> list:
    params = {"symbol": symbol, "interval": interval, "limit": limit}
    if start_ms is not None:
        params["startTime"] = start_ms
    r = requests.get(f"{BINANCE_DATA}/api/v3/klines", params=params,
                     headers=hosts.UA, timeout=REQUEST_TIMEOUT)
    r.raise_for_status()
    return r.json() or []


def extreme_since(symbol: str, start_ms: int, direction: str) -> float | None:
    """Exact min low / max high since start_ms via hourly candles (paginated)."""
    best = None
    cursor = start_ms
    while True:
        kl = fetch_klines(symbol, "1h", start_ms=cursor)
        if not kl:
            break
        vals = [float(k[3]) if direction == "down" else float(k[2]) for k in kl]
        cur = min(vals) if direction == "down" else max(vals)
        best = cur if best is None else (min(best, cur) if direction == "down" else max(best, cur))
        if len(kl) < 1000:
            break
        cursor = int(kl[-1][0]) + 3_600_000
    return best


def _parse_iso(s: str | None) -> datetime | None:
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None


def fit_event(slug: str, lookback: int = 90) -> dict:
    """Assemble the full fit report for one event's touch buckets."""
    event = fetch_event(slug)
    if not event:
        raise ValueError(f"No event found for slug '{slug}'")
    now = datetime.now(timezone.utc)

    open_markets = [m for m in (event.get("markets") or []) if not m.get("closed") and not m.get("archived")]
    symbol = next((parse_symbol(m.get("description")) for m in open_markets
                   if parse_symbol(m.get("description"))), None)
    if not symbol:
        raise ValueError("No resolution pair (e.g. BTC/USDT) found in any sub-market description")

    daily = fetch_klines(symbol, "1d")
    d_closes = [float(k[4]) for k in daily]
    d_lows = [float(k[3]) for k in daily]
    d_highs = [float(k[2]) for k in daily]
    spot = d_closes[-1]
    sigma_d = realized_sigma(d_closes, lookback)

    hourly = fetch_klines(symbol, "1h")
    h_closes = [float(k[4]) for k in hourly]
    h_lows = [float(k[3]) for k in hourly]
    h_highs = [float(k[2]) for k in hourly]
    sigma_h = realized_sigma(h_closes, min(lookback * 24, len(h_closes) - 1))

    rows = []
    for m in open_markets:
        bucket = parse_bucket(m)
        title = m.get("groupItemTitle") or m.get("question") or "(unnamed)"
        if not bucket:
            rows.append({"title": title, "status": "unsupported"})
            continue
        end = _parse_iso(m.get("endDate"))
        if not end:
            rows.append({"title": title, "status": "no end date"})
            continue
        horizon_d = (end - now).total_seconds() / 86400
        if horizon_d <= 0:
            rows.append({"title": title, "status": "expired"})
            continue

        direction, barrier = bucket["direction"], bucket["barrier"]
        prices = m.get("outcomePrices")
        if isinstance(prices, str):
            try:
                prices = json.loads(prices)
            except json.JSONDecodeError:
                prices = None
        mkt_yes = float(prices[0]) if prices else None

        # only price action since creation counts — a bucket may already be settled in fact
        created = _parse_iso(m.get("createdAt")) or _parse_iso(m.get("startDate"))
        touched = False
        if created:
            ext = extreme_since(symbol, int(created.timestamp() * 1000), direction)
            if ext is not None:
                touched = ext <= barrier if direction == "down" else ext >= barrier

        if touched:
            p_model, p_emp, sigma_req = 1.0, 1.0, 0.0
            status = "TOUCHED"
        else:
            p_model = touch_prob_analytic(spot, barrier, direction, sigma_d, horizon_d)
            # short horizons resolve on hourly candles, long ones on daily
            if horizon_d < 2:
                window = max(1, round(horizon_d * 24))
                p_emp = touch_prob_empirical(h_closes, h_lows if direction == "down" else h_highs,
                                             barrier / spot, direction, window)
            else:
                window = max(1, round(horizon_d))
                p_emp = touch_prob_empirical(d_closes, d_lows if direction == "down" else d_highs,
                                             barrier / spot, direction, window)
            sig_T = sigma_d * math.sqrt(horizon_d)
            sigma_req = (abs(math.log(barrier / spot)) / sig_T) if sig_T > 0 else 0.0
            status = "ok"

        edge = (p_emp - mkt_yes) if (p_emp is not None and mkt_yes is not None) else None
        flag = ""
        if status == "TOUCHED":
            flag = "settled — check price!" if (mkt_yes is not None and mkt_yes < 0.95) else "settled"
        elif edge is not None and abs(edge) >= EDGE_FLAG:
            flag = "YES cheap" if edge > 0 else "NO cheap"

        rows.append({
            "title": title, "status": status, "direction": direction, "barrier": barrier,
            "distance_pct": (barrier / spot - 1) * 100,
            "sigma_required": sigma_req, "p_model": p_model, "p_empirical": p_emp,
            "market_yes": mkt_yes, "edge": edge, "flag": flag, "horizon_days": horizon_d,
        })

    rows.sort(key=lambda r: r.get("barrier") or 0)
    return {
        "slug": slug,
        "event_title": event.get("title"),
        "symbol": symbol,
        "spot": spot,
        "sigma_daily": sigma_d,
        "sigma_hourly": sigma_h,
        "lookback_days": lookback,
        "buckets": rows,
    }
