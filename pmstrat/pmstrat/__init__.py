"""pmstrat - Strategy DSL and backtesting for Polymarket."""

from .signal import Signal, Buy, Sell, Cancel, Hold, Urgency
from .context import Context, OrderBookSnapshot, Position, MarketInfo
from .dsl import strategy
from .rewards import RewardsSimulator, MarketRewardConfig

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
]
