"""Simple backtest runner for strategies."""

from dataclasses import dataclass, field
from decimal import Decimal
from datetime import datetime, timedelta
from typing import Callable, Iterator
import json

from .signal import Signal, Buy, Sell, Hold
from .context import Context, OrderBookSnapshot, Position, MarketInfo
from .rewards import RewardsSimulator, Order, EpochReward


@dataclass
class Fill:
    """A simulated fill."""
    token_id: str
    side: str  # "BUY" or "SELL"
    price: Decimal
    size: Decimal
    timestamp: datetime
    slippage: Decimal = Decimal(0)


@dataclass
class BacktestResult:
    """Results from a backtest run."""
    start_time: datetime
    end_time: datetime
    num_ticks: int
    num_trades: int
    total_pnl: Decimal
    realized_pnl: Decimal
    unrealized_pnl: Decimal
    estimated_rewards: Decimal
    total_return: Decimal  # P&L + rewards
    win_rate: float
    fills: list[Fill] = field(default_factory=list)
    positions: dict[str, Position] = field(default_factory=dict)

    def summary(self) -> str:
        """Human-readable summary."""
        return f"""
Backtest Results
================
Period: {self.start_time.date()} to {self.end_time.date()}
Ticks: {self.num_ticks}
Trades: {self.num_trades}

P&L:
  Realized: ${self.realized_pnl:.2f}
  Unrealized: ${self.unrealized_pnl:.2f}
  Total P&L: ${self.total_pnl:.2f}

Rewards:
  Estimated: ${self.estimated_rewards:.2f}

Total Return: ${self.total_return:.2f}
Win Rate: {self.win_rate:.1%}
"""


@dataclass
class Tick:
    """A single tick of market data."""
    timestamp: datetime
    token_id: str
    best_bid: Decimal | None
    best_ask: Decimal | None
    bid_size: Decimal
    ask_size: Decimal
    # Optional market metadata
    question: str = ""
    outcome: str = ""
    end_date: datetime | None = None


class Backtester:
    """Run strategies against historical or simulated data."""

    def __init__(
        self,
        strategy_fn: Callable[[Context], list[Signal]],
        initial_balance: Decimal = Decimal("1000"),
        slippage_bps: Decimal = Decimal("10"),  # 0.1% default slippage
    ):
        """Initialize backtester.

        Args:
            strategy_fn: Strategy function decorated with @strategy
            initial_balance: Starting USDC balance
            slippage_bps: Slippage in basis points (10 = 0.1%)
        """
        self.strategy_fn = strategy_fn
        self.initial_balance = initial_balance
        self.slippage_pct = slippage_bps / Decimal("10000")

        # State
        self.balance = initial_balance
        self.positions: dict[str, Position] = {}
        self.fills: list[Fill] = []
        self.rewards_sim = RewardsSimulator()

        # Track orders for reward simulation
        self.resting_orders: list[Order] = []

    def run(self, ticks: Iterator[Tick]) -> BacktestResult:
        """Run backtest over tick data.

        Args:
            ticks: Iterator of Tick objects (historical or simulated)

        Returns:
            BacktestResult with P&L and statistics
        """
        num_ticks = 0
        start_time = None
        end_time = None

        books: dict[str, OrderBookSnapshot] = {}
        markets: dict[str, MarketInfo] = {}

        for tick in ticks:
            num_ticks += 1
            if start_time is None:
                start_time = tick.timestamp
            end_time = tick.timestamp

            # Update order book
            books[tick.token_id] = OrderBookSnapshot(
                token_id=tick.token_id,
                best_bid=tick.best_bid,
                best_ask=tick.best_ask,
                bid_size=tick.bid_size,
                ask_size=tick.ask_size,
            )

            # Update market info
            if tick.question or tick.end_date:
                markets[tick.token_id] = MarketInfo(
                    token_id=tick.token_id,
                    question=tick.question,
                    outcome=tick.outcome,
                    end_date=tick.end_date,
                )

            # Build context
            ctx = Context(
                timestamp=tick.timestamp,
                books=books,
                positions=self.positions.copy(),
                markets=markets,
                total_realized_pnl=self._realized_pnl(),
                total_unrealized_pnl=self._unrealized_pnl(books),
                usdc_balance=self.balance,
            )

            # Run strategy
            signals = self.strategy_fn(ctx)

            # Execute signals
            for signal in signals:
                self._execute_signal(signal, books, tick.timestamp)

            # Check for resolved markets (price hits 1.00 or 0.00)
            self._check_resolutions(books, tick.timestamp)

        # Calculate final stats
        realized = self._realized_pnl()
        unrealized = self._unrealized_pnl(books)
        total_pnl = realized + unrealized

        # Estimate rewards (simplified: assume average positioning)
        estimated_rewards = self._estimate_rewards(books, end_time - start_time if start_time and end_time else timedelta(days=1))

        # Win rate
        winning_fills = sum(1 for f in self.fills if self._is_winning_fill(f))
        win_rate = winning_fills / len(self.fills) if self.fills else 0.0

        return BacktestResult(
            start_time=start_time or datetime.now(),
            end_time=end_time or datetime.now(),
            num_ticks=num_ticks,
            num_trades=len(self.fills),
            total_pnl=total_pnl,
            realized_pnl=realized,
            unrealized_pnl=unrealized,
            estimated_rewards=estimated_rewards,
            total_return=total_pnl + estimated_rewards,
            win_rate=win_rate,
            fills=self.fills.copy(),
            positions=self.positions.copy(),
        )

    def _execute_signal(
        self,
        signal: Signal,
        books: dict[str, OrderBookSnapshot],
        timestamp: datetime,
    ) -> Fill | None:
        """Execute a signal and return fill if successful."""
        if isinstance(signal, Hold):
            return None

        if isinstance(signal, Buy):
            book = books.get(signal.token_id)
            if book is None or book.best_ask is None:
                return None

            # Apply slippage
            fill_price = book.best_ask * (Decimal("1") + self.slippage_pct)
            fill_size = min(signal.size, book.ask_size)

            # Check balance
            cost = fill_price * fill_size
            if cost > self.balance:
                fill_size = self.balance / fill_price
                cost = fill_price * fill_size

            if fill_size < Decimal("1"):
                return None

            # Execute
            self.balance -= cost
            self._update_position(signal.token_id, fill_size, fill_price)

            fill = Fill(
                token_id=signal.token_id,
                side="BUY",
                price=fill_price,
                size=fill_size,
                timestamp=timestamp,
                slippage=fill_price - book.best_ask,
            )
            self.fills.append(fill)
            return fill

        if isinstance(signal, Sell):
            book = books.get(signal.token_id)
            if book is None or book.best_bid is None:
                return None

            position = self.positions.get(signal.token_id)
            if position is None or position.size <= 0:
                return None

            # Apply slippage
            fill_price = book.best_bid * (Decimal("1") - self.slippage_pct)
            fill_size = min(signal.size, position.size, book.bid_size)

            if fill_size < Decimal("1"):
                return None

            # Execute
            proceeds = fill_price * fill_size
            self.balance += proceeds
            self._update_position(signal.token_id, -fill_size, fill_price)

            fill = Fill(
                token_id=signal.token_id,
                side="SELL",
                price=fill_price,
                size=fill_size,
                timestamp=timestamp,
                slippage=book.best_bid - fill_price,
            )
            self.fills.append(fill)
            return fill

        return None

    def _update_position(self, token_id: str, size_delta: Decimal, price: Decimal):
        """Update position after a fill."""
        if token_id not in self.positions:
            self.positions[token_id] = Position(token_id=token_id)

        pos = self.positions[token_id]
        old_size = pos.size
        new_size = old_size + size_delta

        if size_delta > 0:
            # Buying - update average entry
            if old_size <= 0:
                pos.avg_entry_price = price
            else:
                old_value = pos.avg_entry_price * old_size
                new_value = price * size_delta
                pos.avg_entry_price = (old_value + new_value) / new_size
        elif size_delta < 0:
            # Selling - realize P&L
            realized = (-size_delta) * (price - pos.avg_entry_price)
            pos.realized_pnl += realized

        pos.size = new_size

    def _check_resolutions(self, books: dict[str, OrderBookSnapshot], timestamp: datetime):
        """Check if any positions have resolved (price = 1.00 or 0.00)."""
        for token_id, pos in list(self.positions.items()):
            if pos.size <= 0:
                continue

            book = books.get(token_id)
            if book is None:
                continue

            mid = book.mid_price
            if mid is None:
                continue

            # Check for resolution
            if mid >= Decimal("0.99"):
                # Resolved YES - we win if we hold the token
                self.balance += pos.size * Decimal("1.00")
                pos.realized_pnl += pos.size * (Decimal("1.00") - pos.avg_entry_price)
                pos.size = Decimal(0)
            elif mid <= Decimal("0.01"):
                # Resolved NO - we lose
                pos.realized_pnl += pos.size * (Decimal("0.00") - pos.avg_entry_price)
                pos.size = Decimal(0)

    def _realized_pnl(self) -> Decimal:
        """Calculate total realized P&L."""
        return sum((p.realized_pnl for p in self.positions.values()), Decimal(0))

    def _unrealized_pnl(self, books: dict[str, OrderBookSnapshot]) -> Decimal:
        """Calculate total unrealized P&L."""
        total = Decimal(0)
        for token_id, pos in self.positions.items():
            if pos.size <= 0:
                continue
            book = books.get(token_id)
            if book is None or book.mid_price is None:
                continue
            total += pos.size * (book.mid_price - pos.avg_entry_price)
        return total

    def _estimate_rewards(
        self,
        books: dict[str, OrderBookSnapshot],
        duration: timedelta,
    ) -> Decimal:
        """Estimate liquidity rewards earned.

        This is a rough estimate - real rewards depend on competition.
        """
        # For sure_bets strategy, we're not providing liquidity (we're taking)
        # So rewards would come from any resting orders we place
        # For simplicity, estimate based on position value
        total_position_value = sum(
            (pos.size * pos.avg_entry_price for pos in self.positions.values()),
            Decimal(0),
        )

        # Holding rewards: 4% APY
        days = Decimal(str(duration.total_seconds() / 86400))
        holding_rewards = total_position_value * Decimal("0.04") * days / Decimal("365")

        return holding_rewards

    def _is_winning_fill(self, fill: Fill) -> bool:
        """Determine if a fill was profitable (simplified)."""
        # For sure_bets, a winning buy is one where we bought at < 1.00
        # and the market resolved to 1.00
        if fill.side == "BUY":
            return fill.price < Decimal("1.00")
        return True  # Sells lock in profit


def load_ticks_from_jsonl(filepath: str) -> Iterator[Tick]:
    """Load tick data from JSONL file.

    Expected format per line:
    {
        "timestamp": "2026-01-15T10:00:00Z",
        "token_id": "0x123...",
        "best_bid": 0.95,
        "best_ask": 0.96,
        "bid_size": 1000,
        "ask_size": 500,
        "question": "Will X happen?",
        "outcome": "Yes",
        "end_date": "2026-01-15T12:00:00Z"
    }
    """
    with open(filepath, "r") as f:
        for line in f:
            data = json.loads(line)
            yield Tick(
                timestamp=datetime.fromisoformat(data["timestamp"].replace("Z", "+00:00")),
                token_id=data["token_id"],
                best_bid=Decimal(str(data["best_bid"])) if data.get("best_bid") else None,
                best_ask=Decimal(str(data["best_ask"])) if data.get("best_ask") else None,
                bid_size=Decimal(str(data.get("bid_size", 0))),
                ask_size=Decimal(str(data.get("ask_size", 0))),
                question=data.get("question", ""),
                outcome=data.get("outcome", ""),
                end_date=datetime.fromisoformat(data["end_date"].replace("Z", "+00:00")) if data.get("end_date") else None,
            )


def generate_synthetic_ticks(
    num_ticks: int = 100,
    initial_price: Decimal = Decimal("0.96"),
    volatility: Decimal = Decimal("0.001"),
    hours_to_expiry: float = 2.0,
) -> Iterator[Tick]:
    """Generate synthetic tick data for testing.

    Simulates a market that starts at initial_price and drifts toward 1.00
    as expiry approaches (simulating a "sure bet" resolving).
    """
    import random

    now = datetime.now()
    end_date = now + timedelta(hours=hours_to_expiry)
    price = float(initial_price)

    for i in range(num_ticks):
        # Time progresses
        timestamp = now + timedelta(minutes=i)

        # Price drifts toward 1.00 with some noise
        drift = (1.0 - price) * 0.01  # Drift toward 1.00
        noise = random.gauss(0, float(volatility))
        price = min(0.999, max(0.90, price + drift + noise))

        # Spread
        spread = 0.01
        bid = Decimal(str(round(price - spread / 2, 3)))
        ask = Decimal(str(round(price + spread / 2, 3)))

        yield Tick(
            timestamp=timestamp,
            token_id="test_token_001",
            best_bid=bid,
            best_ask=ask,
            bid_size=Decimal(str(random.randint(100, 1000))),
            ask_size=Decimal(str(random.randint(100, 1000))),
            question="Test market: Will this resolve YES?",
            outcome="Yes",
            end_date=end_date,
        )
