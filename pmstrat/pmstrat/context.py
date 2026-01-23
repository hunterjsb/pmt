"""Strategy context - market state passed to strategies on each tick."""

from dataclasses import dataclass, field
from decimal import Decimal
from datetime import datetime
from typing import Optional


@dataclass
class OrderBookSnapshot:
    """Top-of-book snapshot for a token."""
    token_id: str
    best_bid: Optional[Decimal] = None
    best_ask: Optional[Decimal] = None
    bid_size: Decimal = Decimal(0)
    ask_size: Decimal = Decimal(0)

    @property
    def mid_price(self) -> Optional[Decimal]:
        if self.best_bid is not None and self.best_ask is not None:
            return (self.best_bid + self.best_ask) / 2
        return None

    @property
    def spread(self) -> Optional[Decimal]:
        if self.best_bid is not None and self.best_ask is not None:
            return self.best_ask - self.best_bid
        return None


@dataclass
class Position:
    """Position in a token."""
    token_id: str
    size: Decimal = Decimal(0)
    avg_entry_price: Decimal = Decimal(0)
    unrealized_pnl: Decimal = Decimal(0)
    realized_pnl: Decimal = Decimal(0)
    last_price: Optional[Decimal] = None  # Matches Rust Position


@dataclass
class MarketInfo:
    """Market metadata for a token."""
    token_id: str
    question: str = ""
    outcome: str = ""
    slug: str = ""
    end_date: Optional[datetime] = None
    liquidity: Optional[float] = None

    @property
    def hours_until_expiry(self) -> Optional[float]:
        if self.end_date is None:
            return None
        delta = self.end_date - datetime.now(self.end_date.tzinfo)
        return delta.total_seconds() / 3600


@dataclass
class Context:
    """Read-only context passed to strategies on each tick."""
    timestamp: datetime
    books: dict[str, OrderBookSnapshot] = field(default_factory=dict)
    positions: dict[str, Position] = field(default_factory=dict)
    markets: dict[str, MarketInfo] = field(default_factory=dict)
    total_realized_pnl: Decimal = Decimal(0)
    total_unrealized_pnl: Decimal = Decimal(0)
    usdc_balance: Decimal = Decimal(0)

    def book(self, token_id: str) -> Optional[OrderBookSnapshot]:
        """Get order book for a token."""
        return self.books.get(token_id)

    def position(self, token_id: str) -> Optional[Position]:
        """Get position for a token."""
        return self.positions.get(token_id)

    def market(self, token_id: str) -> Optional[MarketInfo]:
        """Get market info for a token."""
        return self.markets.get(token_id)

    def mid(self, token_id: str) -> Optional[Decimal]:
        """Get mid price for a token."""
        book = self.book(token_id)
        return book.mid_price if book else None

    @property
    def total_pnl(self) -> Decimal:
        """Total P&L (realized + unrealized)."""
        return self.total_realized_pnl + self.total_unrealized_pnl


# Alias for Rust naming compatibility
StrategyContext = Context
