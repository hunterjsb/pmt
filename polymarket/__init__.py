"""Polymarket API client.

Thin wrappers around:
- Gamma API: Market metadata (events, markets, tags, search)
- CLOB API: Trading data (order books, prices, markets)
- Authenticated CLOB: orders/trades + on-chain balances/positions
"""

from .clob import (
    AuthenticatedClob,
    Clob,
    create_authenticated_clob,
    get_order_book_depth,
)
from .gamma import Gamma
from .models import Event, Market, OrderBook, OrderBookLevel, Token

__all__ = [
    # Models
    "Token",
    "Market",
    "OrderBook",
    "OrderBookLevel",
    "Event",
    # Clients
    "Clob",
    "Gamma",
    "AuthenticatedClob",
    "create_authenticated_clob",
    # Utilities
    "get_order_book_depth",
]

# Singleton instances for convenience
clob = Clob()
gamma = Gamma()
