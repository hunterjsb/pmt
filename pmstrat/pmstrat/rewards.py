"""Polymarket liquidity rewards simulator.

Based on the formula from Polymarket docs:
    S(v, s) = ((v - s) / v)² × b

Where:
    v = max_spread (maximum distance from midpoint to qualify)
    s = your spread (distance from midpoint)
    b = market multiplier

Two-sided quoting gets ~3x boost.
Rewards are distributed pro-rata from daily pool.
"""

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Optional
from datetime import datetime, timedelta


@dataclass
class MarketRewardConfig:
    """Per-market reward configuration."""
    token_id: str
    daily_pool_usdc: Decimal  # Total USDC distributed daily for this market
    max_spread: Decimal = Decimal("0.04")  # ±4¢ default
    min_size: Decimal = Decimal("20")  # Minimum shares to qualify
    multiplier: Decimal = Decimal("1.0")  # Market-specific multiplier


@dataclass
class Order:
    """A resting limit order for reward calculation."""
    token_id: str
    side: str  # "BID" or "ASK"
    price: Decimal
    size: Decimal
    timestamp: datetime = field(default_factory=datetime.now)


@dataclass
class RewardScore:
    """Score for a single order."""
    order: Order
    score: Decimal
    distance_from_mid: Decimal
    qualified: bool
    reason: str = ""


@dataclass
class EpochReward:
    """Reward calculation for an epoch (typically 1 day)."""
    token_id: str
    epoch_start: datetime
    epoch_end: datetime
    total_score: Decimal
    your_score: Decimal
    your_share: Decimal  # Fraction of pool
    reward_usdc: Decimal
    orders_scored: list[RewardScore] = field(default_factory=list)


class RewardsSimulator:
    """Simulates Polymarket liquidity rewards."""

    def __init__(self, market_configs: dict[str, MarketRewardConfig] | None = None):
        """Initialize with market configurations.

        Args:
            market_configs: Dict of token_id -> MarketRewardConfig
        """
        self.market_configs = market_configs or {}
        self.default_config = MarketRewardConfig(
            token_id="default",
            daily_pool_usdc=Decimal("50"),  # Conservative default
            max_spread=Decimal("0.04"),
            min_size=Decimal("20"),
        )

    def get_config(self, token_id: str) -> MarketRewardConfig:
        """Get config for a market, falling back to default."""
        return self.market_configs.get(token_id, self.default_config)

    def score_order(
        self,
        order: Order,
        mid_price: Decimal,
        config: MarketRewardConfig,
    ) -> RewardScore:
        """Calculate reward score for a single order.

        Formula: S = ((max_spread - distance) / max_spread)² × multiplier
        """
        # Calculate distance from midpoint
        if order.side == "BID":
            distance = mid_price - order.price
        else:  # ASK
            distance = order.price - mid_price

        # Check if order qualifies
        if order.size < config.min_size:
            return RewardScore(
                order=order,
                score=Decimal(0),
                distance_from_mid=distance,
                qualified=False,
                reason=f"Size {order.size} < min {config.min_size}",
            )

        if distance > config.max_spread:
            return RewardScore(
                order=order,
                score=Decimal(0),
                distance_from_mid=distance,
                qualified=False,
                reason=f"Spread {distance} > max {config.max_spread}",
            )

        if distance < 0:
            # Order crosses the spread (would be a taker)
            return RewardScore(
                order=order,
                score=Decimal(0),
                distance_from_mid=distance,
                qualified=False,
                reason="Order crosses spread",
            )

        # Calculate score: ((v - s) / v)² × b × size
        v = config.max_spread
        s = distance
        base_score = ((v - s) / v) ** 2 * config.multiplier

        # Weight by size (normalized)
        size_weight = order.size / Decimal("100")  # Normalize to 100 shares
        score = base_score * size_weight

        return RewardScore(
            order=order,
            score=score,
            distance_from_mid=distance,
            qualified=True,
        )

    def calculate_epoch_rewards(
        self,
        your_orders: list[Order],
        mid_price: Decimal,
        token_id: str,
        total_market_score: Decimal | None = None,
        epoch_duration: timedelta = timedelta(days=1),
    ) -> EpochReward:
        """Calculate rewards for an epoch.

        Args:
            your_orders: Your resting orders during the epoch
            mid_price: Average mid price during epoch
            token_id: Market token ID
            total_market_score: Total score from all participants (if known)
            epoch_duration: Duration of epoch (default 1 day)

        Returns:
            EpochReward with your share of the pool
        """
        config = self.get_config(token_id)
        now = datetime.now()

        # Score each order
        scores = [self.score_order(order, mid_price, config) for order in your_orders]
        qualified_scores = [s for s in scores if s.qualified]

        # Calculate two-sided bonus
        has_bid = any(s.order.side == "BID" for s in qualified_scores)
        has_ask = any(s.order.side == "ASK" for s in qualified_scores)
        two_sided = has_bid and has_ask

        # Sum your score
        your_score = sum((s.score for s in qualified_scores), Decimal(0))

        # Apply two-sided bonus (3x)
        if two_sided:
            your_score *= Decimal("3")
        elif qualified_scores and mid_price > Decimal("0.10") and mid_price < Decimal("0.90"):
            # Single-sided in mid-range gets reduced score
            your_score /= Decimal("3")

        # Estimate total market score if not provided
        if total_market_score is None:
            # Assume you're ~5% of the market (conservative)
            total_market_score = your_score * Decimal("20") if your_score > 0 else Decimal("1")

        # Calculate your share
        your_share = your_score / total_market_score if total_market_score > 0 else Decimal(0)

        # Calculate reward
        reward_usdc = config.daily_pool_usdc * your_share

        return EpochReward(
            token_id=token_id,
            epoch_start=now - epoch_duration,
            epoch_end=now,
            total_score=total_market_score,
            your_score=your_score,
            your_share=your_share,
            reward_usdc=reward_usdc,
            orders_scored=scores,
        )

    def estimate_daily_rewards(
        self,
        orders_by_token: dict[str, list[Order]],
        mid_prices: dict[str, Decimal],
    ) -> dict[str, EpochReward]:
        """Estimate daily rewards across multiple markets.

        Args:
            orders_by_token: Dict of token_id -> list of your orders
            mid_prices: Dict of token_id -> mid price

        Returns:
            Dict of token_id -> EpochReward
        """
        results = {}
        for token_id, orders in orders_by_token.items():
            mid = mid_prices.get(token_id, Decimal("0.50"))
            results[token_id] = self.calculate_epoch_rewards(
                your_orders=orders,
                mid_price=mid,
                token_id=token_id,
            )
        return results

    def estimate_annual_yield(
        self,
        daily_reward: Decimal,
        capital_deployed: Decimal,
    ) -> Decimal:
        """Calculate annualized yield from daily rewards.

        Args:
            daily_reward: Daily USDC reward
            capital_deployed: Capital locked in orders

        Returns:
            APY as a decimal (e.g., 0.12 for 12%)
        """
        if capital_deployed <= 0:
            return Decimal(0)
        daily_yield = daily_reward / capital_deployed
        return daily_yield * Decimal("365")
