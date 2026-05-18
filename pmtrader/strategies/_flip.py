"""Shared helper for two-leg flip strategies: taker BUY → maker SELL.

The buy matches off-chain immediately, but the on-chain ERC-1155 balance can
take a beat to settle. Posting the sell straight after sometimes fails with
"not enough balance / allowance" — so we retry the sell with exponential
backoff instead of using a fixed sleep.
"""

from __future__ import annotations

import time

from py_clob_client_v2 import (
    OrderArgs,
    OrderType,
    PartialCreateOrderOptions,
    Side,
)
from py_clob_client_v2.exceptions import PolyApiException

from polymarket.markets import Market


def submit_buy(client, market: Market, price: float, size: int) -> dict:
    return client.create_and_post_order(
        order_args=OrderArgs(token_id=market.yes_token, price=price, side=Side.BUY, size=size),
        options=PartialCreateOrderOptions(tick_size=market.tick_size),
        order_type=OrderType.GTC,
    )


def submit_sell_with_settlement_retry(
    client,
    market: Market,
    price: float,
    size: int,
    *,
    token: str | None = None,
    max_attempts: int = 6,
) -> dict:
    """Post a sell, retrying on settlement-lag balance errors.

    Polymarket reports `not enough balance / allowance` when the prior buy's
    on-chain settlement hasn't landed yet. Exponential backoff: 0.5, 1, 2, 4, 8s.
    """
    token = token or market.yes_token
    args = OrderArgs(token_id=token, price=price, side=Side.SELL, size=size)
    opts = PartialCreateOrderOptions(tick_size=market.tick_size)

    for attempt in range(max_attempts):
        try:
            return client.create_and_post_order(args, opts, order_type=OrderType.GTC)
        except PolyApiException as e:
            msg = str(e).lower()
            if "not enough balance" in msg or "allowance" in msg:
                if attempt + 1 == max_attempts:
                    raise
                time.sleep(0.5 * (2**attempt))
                continue
            raise

    raise RuntimeError("unreachable")
