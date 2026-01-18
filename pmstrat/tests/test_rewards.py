"""Tests for rewards simulator."""

from decimal import Decimal
from datetime import datetime

import pytest

from pmstrat.rewards import (
    RewardsSimulator,
    MarketRewardConfig,
    Order,
)


def test_score_order_at_midpoint():
    """Order exactly at midpoint gets maximum score."""
    sim = RewardsSimulator()
    config = MarketRewardConfig(
        token_id="test",
        daily_pool_usdc=Decimal("100"),
        max_spread=Decimal("0.04"),
        min_size=Decimal("20"),
    )

    order = Order(
        token_id="test",
        side="BID",
        price=Decimal("0.50"),  # At mid
        size=Decimal("100"),
        timestamp=datetime.now(),
    )

    score = sim.score_order(order, mid_price=Decimal("0.50"), config=config)

    assert score.qualified
    assert score.distance_from_mid == Decimal(0)
    assert score.score > Decimal(0)


def test_score_order_at_max_spread():
    """Order at max spread gets minimal score."""
    sim = RewardsSimulator()
    config = MarketRewardConfig(
        token_id="test",
        daily_pool_usdc=Decimal("100"),
        max_spread=Decimal("0.04"),
        min_size=Decimal("20"),
    )

    order = Order(
        token_id="test",
        side="BID",
        price=Decimal("0.46"),  # 4 cents from mid
        size=Decimal("100"),
        timestamp=datetime.now(),
    )

    score = sim.score_order(order, mid_price=Decimal("0.50"), config=config)

    assert score.qualified
    assert score.distance_from_mid == Decimal("0.04")
    assert score.score == Decimal(0)  # At max spread, score is 0


def test_score_order_beyond_max_spread():
    """Order beyond max spread is disqualified."""
    sim = RewardsSimulator()
    config = MarketRewardConfig(
        token_id="test",
        daily_pool_usdc=Decimal("100"),
        max_spread=Decimal("0.04"),
        min_size=Decimal("20"),
    )

    order = Order(
        token_id="test",
        side="BID",
        price=Decimal("0.45"),  # 5 cents from mid
        size=Decimal("100"),
        timestamp=datetime.now(),
    )

    score = sim.score_order(order, mid_price=Decimal("0.50"), config=config)

    assert not score.qualified
    assert "Spread" in score.reason


def test_score_order_below_min_size():
    """Order below min size is disqualified."""
    sim = RewardsSimulator()
    config = MarketRewardConfig(
        token_id="test",
        daily_pool_usdc=Decimal("100"),
        max_spread=Decimal("0.04"),
        min_size=Decimal("20"),
    )

    order = Order(
        token_id="test",
        side="BID",
        price=Decimal("0.50"),
        size=Decimal("10"),  # Below min
        timestamp=datetime.now(),
    )

    score = sim.score_order(order, mid_price=Decimal("0.50"), config=config)

    assert not score.qualified
    assert "Size" in score.reason


def test_two_sided_bonus():
    """Two-sided quoting gets 3x bonus."""
    sim = RewardsSimulator()

    bid_order = Order(
        token_id="test",
        side="BID",
        price=Decimal("0.49"),
        size=Decimal("100"),
        timestamp=datetime.now(),
    )

    ask_order = Order(
        token_id="test",
        side="ASK",
        price=Decimal("0.51"),
        size=Decimal("100"),
        timestamp=datetime.now(),
    )

    # Single-sided
    single_result = sim.calculate_epoch_rewards(
        your_orders=[bid_order],
        mid_price=Decimal("0.50"),
        token_id="test",
        total_market_score=Decimal("100"),
    )

    # Two-sided
    two_sided_result = sim.calculate_epoch_rewards(
        your_orders=[bid_order, ask_order],
        mid_price=Decimal("0.50"),
        token_id="test",
        total_market_score=Decimal("100"),
    )

    # Two-sided should have higher score
    assert two_sided_result.your_score > single_result.your_score


def test_annual_yield_calculation():
    """Test APY calculation."""
    sim = RewardsSimulator()

    # $1/day on $1000 = 36.5% APY
    apy = sim.estimate_annual_yield(
        daily_reward=Decimal("1"),
        capital_deployed=Decimal("1000"),
    )

    expected = Decimal("0.365")  # 36.5%
    assert abs(apy - expected) < Decimal("0.001")
