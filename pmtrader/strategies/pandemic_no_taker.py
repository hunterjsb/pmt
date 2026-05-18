"""Taker BUY of pandemic NO — instant accumulation at the ask.

No rewards (taker doesn't add liquidity). Use this when the maker bid has been
sitting unfilled and you want to actually acquire inventory now.
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
    parser.add_argument("--price", type=float, required=True, help="Max price to pay (best-ask or higher)")
    parser.add_argument("--size", type=int, required=True, help="Shares to buy")
    parser.add_argument("--send", action="store_true")
    args = parser.parse_args()

    print(f"BUY {args.size} NO @ ≤${args.price}  cost ${args.size*args.price:.4f}  ({'LIVE' if args.send else 'DRY-RUN'})")
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
