"""Polymarket API surface.

- `PolymarketAPI` — authenticated v2 client (orders, positions, activity)
- `Gamma`         — read-only Gamma client (events, markets)
- `get_order_book_depth` — full-depth book fetch (no auth)
- `hosts`         — single source of truth for API hosts + proxy override
- `pnl`           — activity-ledger replay (realized + unrealized PnL)
"""

from . import hosts
from .api import FlipResult, PolymarketAPI, lookup_market_name
from .clob import get_order_book_depth, sampling_markets
from .gamma import Gamma
from .models import Market, OrderBook, OrderBookLevel, Token

__all__ = [
    "FlipResult",
    "Gamma",
    "Market",
    "OrderBook",
    "OrderBookLevel",
    "PolymarketAPI",
    "Token",
    "get_order_book_depth",
    "hosts",
    "lookup_market_name",
    "sampling_markets",
]
