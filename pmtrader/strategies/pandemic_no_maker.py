"""Maker bid on the pandemic NO token, sized to clear the $200 rewards floor.

Becomes the new best bid (0.1¢ inside the prior best). Either fills cheap or
sits earning liquidity rewards ($1000/day pot, 5.5¢ max spread, $200 min size).
"""

import argparse
import json
import sys

from dotenv import load_dotenv

load_dotenv()

from polymarket.markets import HANTAVIRUS_PANDEMIC
from polymarket.clob_v2 import create_authenticated_clob_v2
from py_clob_client_v2 import OrderArgs, OrderType, PartialCreateOrderOptions, Side

PRICE = 0.9285
SIZE = 216  # 216 × 0.9285 = $200.56, clears the $200 rewards-eligibility floor


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--send", action="store_true")
    args = parser.parse_args()

    notional = SIZE * PRICE
    print(f"BID {SIZE} NO @ ${PRICE}  notional ${notional:.2f}  rewards={notional >= 200}  ({'LIVE' if args.send else 'DRY-RUN'})")
    if not args.send:
        return 0

    client = create_authenticated_clob_v2()
    resp = client.create_and_post_order(
        order_args=OrderArgs(
            token_id=HANTAVIRUS_PANDEMIC.no_token, price=PRICE, side=Side.BUY, size=SIZE
        ),
        options=PartialCreateOrderOptions(tick_size=HANTAVIRUS_PANDEMIC.tick_size),
        order_type=OrderType.GTC,
    )
    print(json.dumps(resp, indent=2, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
