"""Built-in strategies."""

from .sure_bets import on_tick as sure_bets
from .market_maker import on_tick as market_maker
from .dynamic_market_maker import on_tick as dynamic_market_maker

__all__ = ["sure_bets", "market_maker", "dynamic_market_maker"]
