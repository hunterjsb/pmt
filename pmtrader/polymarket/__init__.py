"""Polymarket API client.

- `Clob` — read-only CLOB wrapper (sampling_markets, order_book, midpoint, prices)
- `Gamma` — Gamma API wrapper (events, search, market metadata)
- `PolymarketAPI` — authenticated v2 client for placing orders, querying positions, etc.
- Singleton convenience instances: `clob`, `gamma`
"""

from .api import FlipResult, PolymarketAPI
from .clob import (
    Clob,
    get_chain_host,
    get_clob_host,
    get_gamma_host,
    get_order_book_depth,
    get_proxy_url,
)
from .gamma import Gamma
from .models import Event, Market, OrderBook, OrderBookLevel, Token

__all__ = [
    "Token",
    "Market",
    "OrderBook",
    "OrderBookLevel",
    "Event",
    "Clob",
    "Gamma",
    "PolymarketAPI",
    "FlipResult",
    "get_order_book_depth",
    "get_proxy_url",
    "get_clob_host",
    "get_gamma_host",
    "get_chain_host",
]

# Singleton instances for convenience
clob = Clob()
gamma = Gamma()
