"""One-shot pricing for Polymarket's recurring crypto up/down markets.

Two resolution families hide behind near-identical titles (the split that
burned us twice — always parsed from the description, never assumed):
  - twap:  "updown" series — Chainlink 60s-TWAP of the whole range vs the
           TWAP at range start. Binance 1m klines only proxy the stream;
           projected margins inside ~3bp are Chainlink-basis coin flips.
  - close_open: hourly "<h>PM ET" series — Binance 1h candle close >= open.
           Binance IS the resolution source, so no basis risk.
"""

from __future__ import annotations

import json
import math
import re
import time

import requests

from . import hosts
from .fit import BINANCE_DATA, fetch_klines, realized_sigma, _norm_cdf
from .scanner import fetch_event

REQUEST_TIMEOUT = 10

# Chainlink-TWAP vs Binance-proxy disagreement band (bp on the final margin).
BASIS_NOISE_BP = 3.0

_PAIR_RE = re.compile(r"([A-Z0-9]{2,10})/(?:USDT?|USD)")
_TITLE_SYMBOLS = {"bitcoin": "BTCUSDT", "btc": "BTCUSDT", "ethereum": "ETHUSDT",
                  "eth": "ETHUSDT", "solana": "SOLUSDT", "sol": "SOLUSDT",
                  "xrp": "XRPUSDT", "doge": "DOGEUSDT", "dogecoin": "DOGEUSDT"}


def slug_of(ref: str) -> str:
    """Accept a polymarket.com event URL or a bare slug."""
    if "/event/" in ref:
        ref = ref.split("/event/", 1)[1]
    return ref.split("?")[0].strip("/")


def spot_price(symbol: str = "BTCUSDT") -> float:
    r = requests.get(f"{BINANCE_DATA}/api/v3/ticker/price",
                     params={"symbol": symbol}, timeout=REQUEST_TIMEOUT)
    r.raise_for_status()
    return float(r.json()["price"])


def fetch_book(token: str) -> dict:
    """Public CLOB book, normalized: best_bid/best_ask floats or None."""
    r = requests.get(f"{hosts.CLOB}/book", params={"token_id": token},
                     timeout=REQUEST_TIMEOUT, headers=hosts.UA)
    r.raise_for_status()
    b = r.json()
    bids = [(float(x["price"]), float(x["size"])) for x in b.get("bids") or []]
    asks = [(float(x["price"]), float(x["size"])) for x in b.get("asks") or []]
    return {
        "best_bid": max((p for p, _ in bids), default=None),
        "best_ask": min((p for p, _ in asks), default=None),
        "bid_depth": sum(s for _, s in bids),
        "ask_depth": sum(s for _, s in asks),
    }


def _iso_epoch(s: str) -> float:
    from datetime import datetime
    return datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()


def parse_semantics(event: dict) -> dict:
    """Resolution semantics for an up/down event, read from the description."""
    mkts = [m for m in (event.get("markets") or []) if not m.get("archived")]
    if not mkts:
        raise ValueError("event has no markets")
    m = mkts[0]
    desc = m.get("description") or ""

    if "time-weighted average price" in desc.lower():
        kind = "twap"
    elif "close price" in desc.lower() and "open price" in desc.lower():
        kind = "close_open"
    else:
        raise ValueError("unrecognized resolution semantics — read the description manually")

    pair = _PAIR_RE.search(desc)
    symbol = pair.group(1).upper() + "USDT" if pair else None
    if not symbol:
        title = (event.get("title") or "").lower()
        symbol = next((s for k, s in _TITLE_SYMBOLS.items() if k in title), None)
    if not symbol:
        raise ValueError("could not determine resolution pair")

    outcomes = json.loads(m["outcomes"])
    tokens = json.loads(m["clobTokenIds"])
    by_outcome = {o.lower(): t for o, t in zip(outcomes, tokens)}
    fee_rate = float((m.get("feeSchedule") or {}).get("rate") or 0.0)

    return {
        "slug": event.get("slug") or m.get("slug"),
        "title": m.get("question") or event.get("title"),
        "kind": kind,
        "symbol": symbol,
        "start": _iso_epoch(m["eventStartTime"]),
        "end": _iso_epoch(m["endDate"]),
        "token_up": by_outcome.get("up"),
        "token_down": by_outcome.get("down"),
        "fee_rate": fee_rate,
        "closed": bool(m.get("closed")),
    }


def taker_fee(price: float, fee_rate: float) -> float:
    """crypto_fees_v2, exponent 1: per-share fee on the cheaper side of the pair."""
    return fee_rate * min(price, 1.0 - price)


def _sigma_1m(symbol: str, now: float, lookback_min: int = 120) -> float:
    start_ms = int((now - (lookback_min + 2) * 60) // 60 * 60 * 1000)
    kl = fetch_klines(symbol, "1m", start_ms=start_ms)
    closes = [float(k[4]) for k in kl]
    return realized_sigma(closes, min(lookback_min, len(closes) - 1))


def _model_close_open(sem: dict, now: float, sig1m: float, spot: float) -> dict:
    h = fetch_klines(sem["symbol"], "1h", start_ms=int(sem["start"] * 1000))
    if now < sem["start"] or not h:
        # Window hasn't opened: the open will be ~spot, so fair is ~50/50.
        return {"pending": True, "p_up": 0.5, "p_up_lowvol": 0.5, "p_up_highvol": 0.5,
                "margin_bp": 0.0, "open": None, "t_min": (sem["end"] - now) / 60}
    open_px = float(h[0][1])
    t_min = max((sem["end"] - now) / 60, 0.0)
    if t_min <= 0:
        return {"open": open_px, "p_up": 1.0 if spot >= open_px else 0.0, "expired": True}

    def p_up(mult: float) -> float:
        return _norm_cdf(math.log(spot / open_px) / (sig1m * mult * math.sqrt(t_min)))

    return {"open": open_px, "margin_bp": (spot / open_px - 1) * 1e4, "t_min": t_min,
            "p_up": p_up(1.0), "p_up_lowvol": p_up(0.5), "p_up_highvol": p_up(1.5)}


def _model_twap(sem: dict, now: float, sig1m: float, spot: float) -> dict:
    start, end = sem["start"], sem["end"]
    kl = fetch_klines(sem["symbol"], "1m", start_ms=int((start - 120) * 1000))
    per_min = {k[0] / 1000: (float(k[1]) + float(k[4])) / 2 for k in kl}

    # Range-start reference: Chainlink's 60s TWAP at t0 ~ the prior minute's avg.
    ref_px = per_min.get(start - 60)
    if now < start or ref_px is None:
        return {"pending": True, "p_up": 0.5, "p_up_lowvol": 0.5, "p_up_highvol": 0.5,
                "margin_bp": 0.0, "ref": None, "banked": None}

    banked_vals = [v for t, v in per_min.items() if start <= t < min(now - 30, end)]
    banked_s = len(banked_vals) * 60
    banked = sum(banked_vals) / len(banked_vals) if banked_vals else spot
    rem = max(end - now, 0.0)
    window = banked_s + rem

    if window <= 0 or rem <= 0:
        final = banked
        return {"ref": ref_px, "banked": banked, "margin_bp": (final / ref_px - 1) * 1e4,
                "p_up": 1.0 if final >= ref_px else 0.0, "expired": True}

    proj = (banked * banked_s + spot * rem) / window
    # Price the remaining window's average must stay below for DOWN to win.
    breakeven = (ref_px * window - banked * banked_s) / rem

    def p_up(mult: float) -> float:
        # Remaining-window average of a random walk: sigma * sqrt(T/3).
        sig_avg = sig1m * mult * math.sqrt(max(rem / 60, 0.02) / 3)
        return 1.0 - _norm_cdf(math.log(breakeven / spot) / sig_avg)

    margin_bp = (proj / ref_px - 1) * 1e4
    return {"ref": ref_px, "banked": banked, "banked_s": banked_s, "proj": proj,
            "margin_bp": margin_bp, "breakeven": breakeven, "rem_s": rem,
            "p_up": p_up(1.0), "p_up_lowvol": p_up(0.5), "p_up_highvol": p_up(1.5),
            "basis_coinflip": abs(margin_bp) < BASIS_NOISE_BP}


def eval_updown(ref: str) -> dict:
    """Everything needed to decide (or arm the engine on) one up/down market."""
    slug = slug_of(ref)
    event = fetch_event(slug)
    if not event:
        raise ValueError(f"no event found for slug '{slug}'")
    sem = parse_semantics(event)
    now = time.time()
    spot = spot_price(sem["symbol"])
    sig1m = _sigma_1m(sem["symbol"], now)

    model = (_model_twap if sem["kind"] == "twap" else _model_close_open)(sem, now, sig1m, spot)
    p_up = model["p_up"]

    books, edges = {}, {}
    for side, token, p_side in (("up", sem["token_up"], p_up),
                                ("down", sem["token_down"], 1.0 - p_up)):
        book = fetch_book(token) if token else {}
        books[side] = book
        ask = book.get("best_ask")
        edges[side] = {
            "fair": p_side,
            "ask": ask,
            "taker_cost": (ask + taker_fee(ask, sem["fee_rate"])) if ask is not None else None,
            "net_edge": (p_side - ask - taker_fee(ask, sem["fee_rate"])) if ask is not None else None,
            "bid": book.get("best_bid"),
        }

    best_side = max(edges, key=lambda s: edges[s]["net_edge"] if edges[s]["net_edge"] is not None else -1)
    best = edges[best_side]["net_edge"]
    if model.get("pending"):
        verdict = f"PENDING — window opens in {sem['start'] - now:.0f}s; fair ~50/50 until the open prints"
    elif model.get("expired"):
        verdict = "EXPIRED — window over, do not rest orders"
    elif model.get("basis_coinflip"):
        verdict = f"COIN FLIP — projected margin {model['margin_bp']:+.1f}bp is inside Chainlink basis noise; no model edge"
    elif best is not None and best >= 0.02:
        verdict = f"TAKE {best_side.upper()} @ {edges[best_side]['ask']:.2f} (net edge {best * 100:+.1f}¢)"
    elif best is not None and best >= 0.0:
        verdict = f"MARGINAL {best_side.upper()} (net edge {best * 100:+.1f}¢) — maker or pass"
    else:
        verdict = "PASS — book at or above fair on both sides"

    return {"slug": slug, **{k: sem[k] for k in ("title", "kind", "symbol", "fee_rate")},
            "start": sem["start"], "end": sem["end"], "now": now,
            "rem_s": max(sem["end"] - now, 0.0), "spot": spot,
            "sigma_bp_per_min": sig1m * 1e4, "model": model,
            "edges": edges, "books": books, "verdict": verdict,
            "tokens": {"up": sem["token_up"], "down": sem["token_down"]}}
