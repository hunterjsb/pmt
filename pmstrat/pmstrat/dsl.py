"""Strategy DSL decorators and helpers."""

from dataclasses import dataclass, field
from typing import Callable, List, Any
from functools import wraps

from .signal import Signal
from .context import Context


@dataclass
class MarketFilter:
    """Engine-side market scanner filter for a strategy.

    Mirror of the Rust `pmengine::gamma::MarketFilter`. Set on a strategy
    via `@strategy(market_filter=MarketFilter(...))` — the engine then
    refreshes that strategy's watched-token set every PMENGINE_SCAN_INTERVAL_S
    by querying gamma and matching against the filter, dispatching
    Subscribe/Unsubscribe under the hood.

    Defaults match the Rust defaults: $20k liquidity, mid in [0.05, 0.95],
    any category, recurring series excluded.
    """
    min_liquidity: float = 20_000.0
    max_hours_to_expiry: float | None = None
    min_mid: str = "0.05"   # Decimal as string for clean transpile
    max_mid: str = "0.95"
    categories: List[str] = field(default_factory=list)
    exclude_recurring: bool = True
    max_subscriptions: int = 30


@dataclass
class StrategyMeta:
    """Metadata attached to a strategy function."""
    name: str
    tokens: List[str]
    tick_interval_ms: int
    on_tick: Callable[[Context], List[Signal]]
    on_fill: Callable[[Context, Any], None] | None = None
    params: dict[str, Any] = field(default_factory=dict)
    transpilable: bool = True
    market_filter: MarketFilter | None = None


def strategy(
    name: str,
    tokens: List[str] | None = None,
    tick_interval_ms: int = 1000,
    params: dict[str, Any] | None = None,
    transpilable: bool = True,
    market_filter: MarketFilter | None = None,
):
    """Decorator to define a strategy.

    Usage:
        @strategy(name="my_strat", tokens=["0x123..."], params={"MIN_SIZE": Decimal("10")})
        def on_tick(ctx: Context) -> list[Signal]:
            ...

    Args:
        name: Unique identifier for the strategy
        tokens: List of token IDs to subscribe to (can be empty for dynamic strategies)
        tick_interval_ms: How often to call on_tick (in milliseconds)
        params: Dictionary of strategy parameters (transpiled to Rust constants)
        transpilable: If False, this strategy won't be transpiled (for Python-only test strategies)
    """
    def decorator(func: Callable[[Context], List[Signal]]):
        @wraps(func)
        def wrapper(ctx: Context) -> List[Signal]:
            return func(ctx)

        # Attach metadata for introspection
        wrapper._strategy_meta = StrategyMeta(
            name=name,
            tokens=tokens or [],
            tick_interval_ms=tick_interval_ms,
            on_tick=func,
            params=params or {},
            transpilable=transpilable,
            market_filter=market_filter,
        )

        return wrapper
    return decorator


def get_strategy_meta(func: Callable) -> StrategyMeta | None:
    """Extract strategy metadata from a decorated function."""
    return getattr(func, "_strategy_meta", None)
