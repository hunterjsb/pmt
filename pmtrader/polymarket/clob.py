"""CLOB read endpoints that don't need auth.

The authenticated trading client lives in `polymarket.api.PolymarketAPI`.
This module exists so scanners and the depth-test fixture have a way to
read order books and market lists without dragging in the full SDK.
"""

from __future__ import annotations

import requests

from . import hosts
from .models import Market, OrderBook, OrderBookLevel, Token


def get_order_book_depth(token_id: str, host: str | None = None) -> OrderBook:
    """Full order book ladder for a token (bids high→low, asks low→high)."""
    base = host or hosts.CLOB
    r = requests.get(f"{base}/book", params={"token_id": token_id}, headers=hosts.UA, timeout=10)
    r.raise_for_status()
    data = r.json()
    bids = sorted(
        (OrderBookLevel(float(b["price"]), float(b["size"])) for b in data.get("bids") or []),
        key=lambda x: x.price, reverse=True,
    )
    asks = sorted(
        (OrderBookLevel(float(a["price"]), float(a["size"])) for a in data.get("asks") or []),
        key=lambda x: x.price,
    )
    return OrderBook(name="Token", bids=bids, asks=asks)


def sampling_markets(limit: int = 100, host: str | None = None) -> list[Market]:
    """The CLOB's `/sampling-markets` selection. Used by the cliff scanner."""
    base = host or hosts.CLOB
    r = requests.get(f"{base}/sampling-markets", headers=hosts.UA, timeout=10)
    r.raise_for_status()
    out = []
    for m in (r.json().get("data") or [])[:limit]:
        tokens = [
            Token(outcome=t.get("outcome", "?"), price=t.get("price"), token_id=t.get("token_id", ""))
            for t in m.get("tokens") or []
        ]
        out.append(Market(question=m.get("question", "Unknown"), tokens=tokens))
    return out
