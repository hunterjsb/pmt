"""Backtest tools for strategy evaluation."""

from __future__ import annotations

from typing import TYPE_CHECKING

from ..serialize import serialize

if TYPE_CHECKING:
    from fastmcp import FastMCP


def register(mcp: "FastMCP") -> None:
    """Register backtest tools with the MCP server."""

    @mcp.tool(
        name="run_backtest",
        description="""Run a backtest with synthetic market data.

Simulates a market that drifts toward resolution (price → 1.00) with
configurable volatility and duration. Uses the sure_bets strategy by default.

Returns backtest results including:
- Total P&L (realized + unrealized)
- Estimated liquidity rewards
- Win rate
- Number of trades

Useful for evaluating strategy performance before live trading.""",
    )
    def run_backtest(
        strategy_name: str = "sure_bets",
        num_ticks: int = 100,
        initial_price: float = 0.96,
        volatility: float = 0.001,
        hours_to_expiry: float = 2.0,
        initial_balance: float = 1000.0,
        slippage_bps: float = 10.0,
    ) -> dict:
        """Run a backtest with synthetic data.

        Args:
            strategy_name: Strategy to test (default: "sure_bets")
            num_ticks: Number of simulated ticks (default: 100)
            initial_price: Starting price (default: 0.96 = 96%)
            volatility: Price volatility per tick (default: 0.001)
            hours_to_expiry: Simulated hours until market expires (default: 2.0)
            initial_balance: Starting USDC balance (default: 1000)
            slippage_bps: Slippage in basis points (default: 10 = 0.1%)

        Returns:
            BacktestResult dict with P&L, trades, win rate, etc.
        """
        from decimal import Decimal

        from pmstrat.backtest import Backtester, generate_synthetic_ticks

        # Import the strategy
        if strategy_name == "sure_bets":
            from pmstrat.strategies.sure_bets import on_tick as strategy_fn
        else:
            return {
                "error": f"Unknown strategy: {strategy_name}",
                "available_strategies": ["sure_bets"],
            }

        # Create backtester
        backtester = Backtester(
            strategy_fn=strategy_fn,
            initial_balance=Decimal(str(initial_balance)),
            slippage_bps=Decimal(str(slippage_bps)),
        )

        # Generate synthetic ticks
        ticks = generate_synthetic_ticks(
            num_ticks=num_ticks,
            initial_price=Decimal(str(initial_price)),
            volatility=Decimal(str(volatility)),
            hours_to_expiry=hours_to_expiry,
        )

        # Run backtest
        result = backtester.run(ticks)

        return serialize(result)

    @mcp.tool(
        name="list_strategies",
        description="""List available strategies for backtesting.

Returns names and descriptions of strategies that can be used with run_backtest.""",
    )
    def list_strategies() -> list[dict]:
        """List available strategies.

        Returns:
            List of strategy info dicts with name and description
        """
        return [
            {
                "name": "sure_bets",
                "description": (
                    "Low-risk betting on high-certainty expiring markets. "
                    "Buys outcomes priced at 95%+ that expire within 2 hours."
                ),
            },
        ]
