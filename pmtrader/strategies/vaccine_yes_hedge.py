"""Tiny YES vaccine buy as hedge to the main NO pandemic position."""

import argparse
import json
import sys

from dotenv import load_dotenv

load_dotenv()

from polymarket.markets import HANTAVIRUS_VACCINE
from polymarket.clob_v2 import create_authenticated_clob_v2
from py_clob_client_v2 import OrderArgs, OrderType, PartialCreateOrderOptions, Side

PRICE = 0.09
SIZE = 12  # ≥$1 notional clears the marketable-order floor


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--send", action="store_true")
    args = parser.parse_args()

    print(f"BUY {SIZE} YES @ ${PRICE}  cost ${SIZE*PRICE:.2f}  ({'LIVE' if args.send else 'DRY-RUN'})")
    if not args.send:
        return 0

    client = create_authenticated_clob_v2()
    resp = client.create_and_post_order(
        order_args=OrderArgs(
            token_id=HANTAVIRUS_VACCINE.yes_token, price=PRICE, side=Side.BUY, size=SIZE
        ),
        options=PartialCreateOrderOptions(tick_size=HANTAVIRUS_VACCINE.tick_size),
        order_type=OrderType.GTC,
    )
    print(json.dumps(resp, indent=2, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
