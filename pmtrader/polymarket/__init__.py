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
    get_chain_host,
    get_clob_host,
    get_gamma_host,
    get_order_book_depth,
    get_proxy_url,
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
    # Proxy helpers
    "get_proxy_url",
    "get_clob_host",
    "get_gamma_host",
    "get_chain_host",
]

# Singleton instances for convenience
clob = Clob()
gamma = Gamma()
