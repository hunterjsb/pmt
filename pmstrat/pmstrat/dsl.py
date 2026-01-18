"""Strategy DSL decorators and helpers."""

from dataclasses import dataclass, field
from typing import Callable, List, Any
from functools import wraps

from .signal import Signal
from .context import Context


@dataclass
class StrategyMeta:
    """Metadata attached to a strategy function."""
    name: str
    tokens: List[str]
    tick_interval_ms: int
    on_tick: Callable[[Context], List[Signal]]
    on_fill: Callable[[Context, Any], None] | None = None


def strategy(
    name: str,
    tokens: List[str] | None = None,
    tick_interval_ms: int = 1000,
):
    """Decorator to define a strategy.

    Usage:
        @strategy(name="my_strat", tokens=["0x123..."])
        def on_tick(ctx: Context) -> list[Signal]:
            ...

    Args:
        name: Unique identifier for the strategy
        tokens: List of token IDs to subscribe to (can be empty for dynamic strategies)
        tick_interval_ms: How often to call on_tick (in milliseconds)
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
        )

        return wrapper
    return decorator


def get_strategy_meta(func: Callable) -> StrategyMeta | None:
    """Extract strategy metadata from a decorated function."""
    return getattr(func, "_strategy_meta", None)
