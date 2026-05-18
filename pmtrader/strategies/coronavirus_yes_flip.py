"""Coronavirus pandemic YES flip — sweep 6.2-6.4¢ asks, sell at 8.5¢."""

import argparse
import json
import sys

from dotenv import load_dotenv

load_dotenv()

from _flip import submit_buy, submit_sell_with_settlement_retry
from polymarket.markets import CORONAVIRUS_PANDEMIC
from polymarket.clob_v2 import create_authenticated_clob_v2

BUY_PRICE = 0.064
BUY_SIZE = 60
SELL_PRICE = 0.085


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--send", action="store_true")
    args = parser.parse_args()

    print(f"BUY {BUY_SIZE} @ ≤${BUY_PRICE}  →  SELL filled @ ${SELL_PRICE}  ({'LIVE' if args.send else 'DRY-RUN'})")
    if not args.send:
        return 0

    client = create_authenticated_clob_v2()

    buy_resp = submit_buy(client, CORONAVIRUS_PANDEMIC, BUY_PRICE, BUY_SIZE)
    print(json.dumps(buy_resp, indent=2, default=str))

    filled = int(float(buy_resp.get("takingAmount") or 0))
    if filled <= 0:
        print("Buy did not fill; skipping sell.")
        return 1

    sell_resp = submit_sell_with_settlement_retry(client, CORONAVIRUS_PANDEMIC, SELL_PRICE, filled)
    print(json.dumps(sell_resp, indent=2, default=str))

    spent = float(buy_resp.get("makingAmount") or 0)
    print(f"Spent ${spent:.2f} → sell for ${filled*SELL_PRICE:.2f} = +${filled*SELL_PRICE - spent:.2f}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
