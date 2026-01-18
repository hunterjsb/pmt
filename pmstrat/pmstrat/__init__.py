"""pmstrat - Strategy DSL and backtesting for Polymarket."""

from .signal import Signal, Buy, Sell, Cancel, Hold, Urgency
from .context import Context, OrderBookSnapshot, Position, MarketInfo
from .dsl import strategy
from .rewards import RewardsSimulator, MarketRewardConfig
from .transpile import transpile, transpile_to_file, TranspileResult

__all__ = [
    "Signal",
    "Buy",
    "Sell",
    "Cancel",
    "Hold",
    "Urgency",
    "Context",
    "OrderBookSnapshot",
    "Position",
    "MarketInfo",
    "strategy",
    "RewardsSimulator",
    "MarketRewardConfig",
    "transpile",
    "transpile_to_file",
    "TranspileResult",
]
