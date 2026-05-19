"""CLOB (Central Limit Order Book) API client."""

from __future__ import annotations

import os
import time

import requests
from py_clob_client.client import ClobClient
from py_clob_client.clob_types import (
    MarketOrderArgs,
    OpenOrderParams,
    OrderArgs,
    OrderType,
)

from .models import Market, OrderBook, OrderBookLevel, Token

# Optional cognito support (requires boto3)
try:
    from .cognito import CognitoAuth, create_cognito_auth
except ImportError:
    CognitoAuth = None  # type: ignore
    create_cognito_auth = lambda: None  # type: ignore


def _get_proxy_headers(cognito_auth: CognitoAuth | None = None) -> dict[str, str]:
    """Get headers for proxy requests, including auth if available."""
    if cognito_auth is None:
        return {}
    return cognito_auth.get_auth_header()


def get_order_book_depth(
    token_id: str,
    host: str = "https://clob.polymarket.com",
    cognito_auth: CognitoAuth | None = None,
) -> OrderBook:
    """Get full order book depth with all price levels via direct API call.

    The py_clob_client library only returns aggregated levels. This function
    fetches the complete order book ladder directly from the API.

    Args:
        token_id: The token ID to get order book for
        host: CLOB API host URL

    Returns:
        OrderBook with full depth of bids and asks

    Example:
        >>> book = get_order_book_depth("123456789...")
        >>> print(f"Best ask: {book.asks[0].price:.3f} (size: {book.asks[0].size})")
        >>> print(f"Next ask: {book.asks[1].price:.3f} (size: {book.asks[1].size})")
    """
    url = f"{host}/book"
    params = {"token_id": token_id}
    headers = _get_proxy_headers(cognito_auth)

    response = requests.get(url, params=params, headers=headers, timeout=10)
    response.raise_for_status()

    data = response.json()

    # Parse bids and asks, sorted for display
    # Bids: highest price first (best bid at top)
    # Asks: lowest price first (best ask at top)
    bids = sorted(
        [
            OrderBookLevel(float(b["price"]), float(b["size"]))
            for b in data.get("bids", [])
        ],
        key=lambda x: x.price,
        reverse=True,
    )
    asks = sorted(
        [
            OrderBookLevel(float(a["price"]), float(a["size"]))
            for a in data.get("asks", [])
        ],
        key=lambda x: x.price,
    )

    return OrderBook(name="Token", bids=bids, asks=asks)


CLOB_HOST = "https://clob.polymarket.com"
CHAIN_ID = 137  # Polygon

# Public Polygon RPC and contract addresses (not sensitive)
POLYGON_RPC = "https://polygon-rpc.com"
USDC_CONTRACT = "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174"
CTF_CONTRACT = (
    "0x4D97DCd97eC945f40cF65F87097ACe5EA0476045"  # Conditional Tokens (ERC-1155)
)
GAMMA_HOST = "https://gamma-api.polymarket.com"


def get_proxy_url() -> str:
    """Get proxy URL from environment (read at runtime)."""
    return os.environ.get("PMPROXY_URL", "")


def get_clob_host(proxy: bool = False) -> str:
    """Get the CLOB host URL, optionally routing through proxy."""
    proxy_url = get_proxy_url()
    if proxy and proxy_url:
        return f"{proxy_url.rstrip('/')}/clob"
    return CLOB_HOST


def get_gamma_host(proxy: bool = False) -> str:
    """Get the Gamma host URL, optionally routing through proxy."""
    proxy_url = get_proxy_url()
    if proxy and proxy_url:
        return f"{proxy_url.rstrip('/')}/gamma"
    return GAMMA_HOST


def get_chain_host(proxy: bool = False) -> str:
    """Get the Chain/RPC host URL, optionally routing through proxy."""
    proxy_url = get_proxy_url()
    if proxy and proxy_url:
        return f"{proxy_url.rstrip('/')}/chain"
    return POLYGON_RPC


class Clob:
    """Read-only client for the Polymarket CLOB (Central Limit Order Book) API."""

    def __init__(
        self,
        host: str | None = None,
        *,
        proxy: bool = False,
        cognito_auth: CognitoAuth | None = None,
    ) -> None:
        self.host = host or get_clob_host(proxy)
        self._client = ClobClient(self.host)
        self._cognito_auth = cognito_auth
        self._is_proxy = proxy or bool(get_proxy_url())

    def ok(self):
        return self._client.get_ok()

    def server_time(self):
        return self._client.get_server_time()

    def _get_headers(self) -> dict[str, str]:
        """Get headers for requests, including auth if using proxy."""
        if self._is_proxy and self._cognito_auth:
            return self._cognito_auth.get_auth_header()
        return {}

    def market(self, condition_id: str) -> dict:
        """Get market info by condition_id."""
        response = requests.get(
            f"{self.host}/markets/{condition_id}",
            headers=self._get_headers(),
            timeout=10,
        )
        response.raise_for_status()
        return response.json()

    def sampling_markets(self, limit: int = 100) -> list[Market]:
        response = requests.get(
            f"{self.host}/sampling-markets",
            headers=self._get_headers(),
            timeout=10,
        )
        response.raise_for_status()
        data = response.json().get("data", [])[:limit]

        markets = []
        for m in data:
            tokens = [
                Token(
                    outcome=t.get("outcome", "?"),
                    price=t.get("price"),
                    token_id=t.get("token_id", ""),
                )
                for t in m.get("tokens", [])
            ]
            markets.append(Market(question=m.get("question", "Unknown"), tokens=tokens))

        return markets

    def order_book(self, token_id: str, name: str = "Token") -> OrderBook:
        """Get order book for a token.

        Note: py_clob_client aggregates order book levels. For full depth,
        use get_order_book_depth() function instead.
        """
        book = self._client.get_order_book(token_id)
        bids = [
            OrderBookLevel(float(b.price), float(b.size)) for b in (book.bids or [])
        ]
        asks = [
            OrderBookLevel(float(a.price), float(a.size)) for a in (book.asks or [])
        ]
        return OrderBook(name=name, bids=bids, asks=asks)

    def midpoint(self, token_id: str):
        """Returns {'mid': '0.123'}."""
        return self._client.get_midpoint(token_id)

    def price(self, token_id: str, side: str = "BUY"):
        """Returns {'price': '0.123'}."""
        return self._client.get_price(token_id, side=side)

    def spread(self, token_id: str):
        """Returns (best_bid_dict, best_ask_dict)."""
        return self.price(token_id, "SELL"), self.price(token_id, "BUY")
