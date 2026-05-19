"""Maker BUY of pandemic NO at a chosen price.

Use for accumulation + rewards while resting. Order must be ≥$200 notional and
within 5.5¢ of mid to qualify for the pandemic market's liquidity rewards
($1000/day pot).
"""

import argparse
import json
import sys

from dotenv import load_dotenv

load_dotenv()

from polymarket.clob_v2 import create_authenticated_clob_v2
from polymarket.markets import HANTAVIRUS_PANDEMIC
from py_clob_client_v2 import OrderArgs, OrderType, PartialCreateOrderOptions, Side


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--price", type=float, required=True, help="Bid price (at or below best bid)")
    parser.add_argument("--size", type=int, required=True, help="Shares to bid for")
    parser.add_argument("--send", action="store_true")
    args = parser.parse_args()

    notional = args.size * args.price
    print(f"BID {args.size} NO @ ${args.price}  notional ${notional:.4f}  rewards={notional >= 200}  ({'LIVE' if args.send else 'DRY-RUN'})")
    if not args.send:
        return 0

    client = create_authenticated_clob_v2()
    resp = client.create_and_post_order(
        order_args=OrderArgs(
            token_id=HANTAVIRUS_PANDEMIC.no_token,
            price=args.price,
            side=Side.BUY,
            size=args.size,
        ),
        options=PartialCreateOrderOptions(tick_size=HANTAVIRUS_PANDEMIC.tick_size),
        order_type=OrderType.GTC,
    )
    print(json.dumps(resp, indent=2, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
