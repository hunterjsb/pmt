"""CLOB API methods."""

from dataclasses import dataclass
from typing import Any, Optional

from .base import BaseClient


@dataclass
class OrderBookLevel:
    price: float
    size: float


@dataclass
class OrderBook:
    bids: list[OrderBookLevel]
    asks: list[OrderBookLevel]
    asset_id: str
    hash: str
    timestamp: str
    market: str


@dataclass
class Token:
    token_id: str
    outcome: str
    price: float
    winner: bool


@dataclass
class Market:
    condition_id: str
    question_id: str
    question: str
    market_slug: str
    description: str
    end_date_iso: str
    active: bool
    closed: bool
    tokens: list[Token]
    minimum_order_size: float
    minimum_tick_size: float


class ClobMixin:
    """CLOB API methods mixin."""

    def ok(self, *, proxy: Optional[bool] = None) -> bool:
        """
        Health check.

        Args:
            proxy: Override instance proxy setting
        """
        self: BaseClient
        resp = self._get("clob", "", proxy=proxy)
        result = resp.json()
        # API returns "OK" string
        return result == "OK" or (isinstance(result, dict) and result.get("ok", False))

    def sampling_markets(
        self, *, proxy: Optional[bool] = None
    ) -> tuple[list[Market], dict[str, Any]]:
        """
        Get sampling markets with order books.

        Returns:
            Tuple of (parsed markets, raw JSON response)
        """
        self: BaseClient
        resp = self._get("clob", "sampling-markets", proxy=proxy)
        data = resp.json()
        markets = [_parse_market(m) for m in data.get("data", [])]
        return markets, data

    def order_book(
        self, token_id: str, *, proxy: Optional[bool] = None
    ) -> OrderBook:
        """
        Get full order book for a token.

        Args:
            token_id: The token ID to get order book for
            proxy: Override instance proxy setting
        """
        self: BaseClient
        resp = self._get("clob", "book", params={"token_id": token_id}, proxy=proxy)
        data = resp.json()
        return OrderBook(
            bids=[OrderBookLevel(price=float(b["price"]), size=float(b["size"])) for b in data.get("bids", [])],
            asks=[OrderBookLevel(price=float(a["price"]), size=float(a["size"])) for a in data.get("asks", [])],
            asset_id=data.get("asset_id", ""),
            hash=data.get("hash", ""),
            timestamp=data.get("timestamp", ""),
            market=data.get("market", ""),
        )

    def midpoint(self, token_id: str, *, proxy: Optional[bool] = None) -> float:
        """
        Get midpoint price for a token.

        Args:
            token_id: The token ID
            proxy: Override instance proxy setting
        """
        self: BaseClient
        resp = self._get("clob", "midpoint", params={"token_id": token_id}, proxy=proxy)
        return float(resp.json().get("mid", 0))

    def price(
        self, token_id: str, side: str, *, proxy: Optional[bool] = None
    ) -> float:
        """
        Get best bid or ask price.

        Args:
            token_id: The token ID
            side: "BUY" or "SELL"
            proxy: Override instance proxy setting
        """
        self: BaseClient
        resp = self._get(
            "clob", "price",
            params={"token_id": token_id, "side": side.upper()},
            proxy=proxy,
        )
        return float(resp.json().get("price", 0))

    def spread(
        self, token_id: str, *, proxy: Optional[bool] = None
    ) -> tuple[float, float, float]:
        """
        Get bid, ask, and spread.

        Args:
            token_id: The token ID
            proxy: Override instance proxy setting

        Returns:
            Tuple of (bid, ask, spread)
        """
        self: BaseClient
        resp = self._get("clob", "spread", params={"token_id": token_id}, proxy=proxy)
        data = resp.json()
        return (
            float(data.get("bid", 0)),
            float(data.get("ask", 0)),
            float(data.get("spread", 0)),
        )


def _parse_token(t: dict) -> Token:
    """Parse a token from JSON."""
    return Token(
        token_id=t.get("token_id", ""),
        outcome=t.get("outcome", ""),
        price=float(t.get("price", 0)),
        winner=t.get("winner", False),
    )


def _parse_market(m: dict) -> Market:
    """Parse a market from JSON."""
    return Market(
        condition_id=m.get("condition_id", ""),
        question_id=m.get("question_id", ""),
        question=m.get("question", ""),
        market_slug=m.get("market_slug", ""),
        description=m.get("description", ""),
        end_date_iso=m.get("end_date_iso", ""),
        active=m.get("active", False),
        closed=m.get("closed", False),
        tokens=[_parse_token(t) for t in m.get("tokens", [])],
        minimum_order_size=float(m.get("minimum_order_size", 0)),
        minimum_tick_size=float(m.get("minimum_tick_size", 0)),
    )
