"""pmstrat - Strategy DSL and backtesting for Polymarket."""

from .signal import (
    Signal, Buy, Sell, Cancel, Hold, Shutdown, Urgency,
    Subscribe, Unsubscribe, Alert,
)
from .context import Context, OrderBookSnapshot, Position, MarketInfo, TradeRecord
from .dsl import strategy
from .rewards import RewardsSimulator, MarketRewardConfig
from .transpile import (
    transpile,
    transpile_to_file,
    TranspileResult,
    TranspileError,
    ValidationError,
    validate_strategy,
    regenerate_mod_rs,
    find_pmengine_strategies_dir,
)

__all__ = [
    "Signal",
    "Buy",
    "Sell",
    "Cancel",
    "Hold",
    "Shutdown",
    "Urgency",
    "Subscribe",
    "Unsubscribe",
    "Alert",
    "Context",
    "OrderBookSnapshot",
    "Position",
    "MarketInfo",
    "TradeRecord",
    "strategy",
    "RewardsSimulator",
    "MarketRewardConfig",
    "transpile",
    "transpile_to_file",
    "TranspileResult",
    "TranspileError",
    "ValidationError",
    "validate_strategy",
    "regenerate_mod_rs",
    "find_pmengine_strategies_dir",
]
