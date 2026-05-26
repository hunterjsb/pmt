"""Domain models for Polymarket data.

Plain dataclasses with no display logic — rendering belongs in the consumer.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class Token:
    outcome: str
    price: float | None
    token_id: str


@dataclass
class Market:
    question: str
    tokens: list[Token]


@dataclass
class OrderBookLevel:
    price: float
    size: float


@dataclass
class OrderBook:
    name: str
    bids: list[OrderBookLevel]
    asks: list[OrderBookLevel]
