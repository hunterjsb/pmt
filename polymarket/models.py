"""Domain models for Polymarket data."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(repr=False)
class Token:
    """Represents a market outcome token."""

    outcome: str
    price: float | None
    token_id: str

    def __str__(self) -> str:
        price_str = f"{self.price:.1%}" if self.price is not None else "N/A"
        token_preview = (
            self.token_id[:30] + "..." if len(self.token_id) > 30 else self.token_id
        )
        return f"    [{self.outcome}] {price_str} | {token_preview}"

    def __repr__(self) -> str:
        return self.__str__()


@dataclass(repr=False)
class Market:
    """Represents a prediction market."""

    question: str
    tokens: list[Token]

    def __str__(self) -> str:
        question_preview = self.question[:60]
        if len(self.question) > 60:
            question_preview += "..."

        lines = [f"\n  • {question_preview}"]
        lines.extend(str(token) for token in self.tokens)
        return "\n".join(lines)

    def __repr__(self) -> str:
        return self.__str__()


@dataclass(repr=False)
class OrderBookLevel:
    """Represents a price level in the order book."""

    price: float
    size: float

    def __str__(self) -> str:
        return f"{self.price:.1%} x {self.size:,.0f} shares"

    def __repr__(self) -> str:
        return self.__str__()


@dataclass(repr=False)
class OrderBook:
    """Represents an order book for a token."""

    name: str
    bids: list[OrderBookLevel]
    asks: list[OrderBookLevel]

    def __str__(self) -> str:
        lines = [f"\n  📈 {self.name} Order Book:"]
        lines.append(f"     Depth: {len(self.bids)} bids, {len(self.asks)} asks")

        if self.bids:
            lines.append(f"     Top Bid: {self.bids[0]}")
        if self.asks:
            lines.append(f"     Top Ask: {self.asks[0]}")

        return "\n".join(lines)

    def __repr__(self) -> str:
        return self.__str__()


@dataclass(repr=False)
class Event:
    """Represents a Polymarket event."""

    title: str
    slug: str
    end_date: str | None
    liquidity: float | None
    volume: float | None

    def __str__(self) -> str:
        lines = [
            "\n" + "=" * 60,
            f"📊 {self.title}",
            f"   Slug: {self.slug}",
            f"   End Date: {self.end_date or 'N/A'}",
        ]

        if self.liquidity:
            lines.append(f"   Liquidity: ${self.liquidity:,.0f}")
        if self.volume:
            lines.append(f"   Volume: ${self.volume:,.0f}")

        return "\n".join(lines)

    def __repr__(self) -> str:
        return self.__str__()
