"""Vaccine YES flip — sweep 9¢ asks, resell at 10¢.

Buys whatever's available at 9¢ on the YES vaccine token (currently ~852 shares,
$76.68), then immediately posts a sell at 10¢. Sits behind the existing 10¢
queue (~2,490 shares) waiting for someone to chew through that depth.

Profit if filled: ~$0.01/share = ~$8.50 on ~850 shares (~11%).
Risk if it never fills: stuck with ~$77 of YES vaccine until 2026-12-31 resolution.

Doesn't qualify for rewards (under $200 notional).

Routes through pmproxy Lambda.
"""

import argparse
import json
import os
import sys
import time

from dotenv import load_dotenv

load_dotenv()

# --- Cognito Bearer monkey-patch ---
from polymarket.cognito import create_cognito_auth  # noqa: E402

_cognito = create_cognito_auth()

import py_clob_client_v2.http_helpers.helpers as _ph  # noqa: E402

_orig = _ph._overload_headers


def _patched(method, headers):
    h = _orig(method, headers)
    if _cognito:
        h.update(_cognito.get_auth_header())
    return h


_ph._overload_headers = _patched
# --- end monkey-patch ---

from py_clob_client_v2 import (  # noqa: E402
    ClobClient,
    OrderArgs,
    OrderType,
    PartialCreateOrderOptions,
    Side,
)

YES_TOKEN = "33574848766046164159312361389126746625941229104553637902902710371273925289603"
BUY_PRICE = 0.09
BUY_SIZE = 850  # rounded down from ~852 available, leaves a little buffer if anyone front-runs
SELL_PRICE = 0.10
TICK_SIZE = "0.01"  # vaccine market: 1¢ tick


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--send", action="store_true", help="Actually submit (default: dry-run)")
    args = parser.parse_args()

    proxy_url = os.environ["PMPROXY_URL"].rstrip("/") + "/clob"
    pk = os.environ["PM_PRIVATE_KEY"]
    funder = os.environ["PM_FUNDER_ADDRESS"]
    sig_type = int(os.environ.get("PM_SIGNATURE_TYPE", "1"))

    boot = ClobClient(host=proxy_url, chain_id=137, key=pk, signature_type=sig_type, funder=funder)
    creds = boot.create_or_derive_api_key()
    client = ClobClient(host=proxy_url, chain_id=137, key=pk, signature_type=sig_type,
                        funder=funder, creds=creds)

    print("=" * 60)
    print("Vaccine YES flip — sweep 9¢, sell 10¢")
    print("=" * 60)
    print(f"  BUY:    {BUY_SIZE} @ ≤${BUY_PRICE}  (cost ${BUY_SIZE*BUY_PRICE:.2f} if all 9¢)")
    print(f"  SELL:   filled-size @ ${SELL_PRICE}  (~${BUY_SIZE*SELL_PRICE:.2f} if same size)")
    print(f"  Mode:   {'LIVE' if args.send else 'DRY-RUN'}")
    print("=" * 60)

    if not args.send:
        print("\nDry-run complete. Re-run with --send.")
        return 0

    print("\n[BUY] Submitting taker buy...")
    buy_resp = client.create_and_post_order(
        order_args=OrderArgs(token_id=YES_TOKEN, price=BUY_PRICE, side=Side.BUY, size=BUY_SIZE),
        options=PartialCreateOrderOptions(tick_size=TICK_SIZE),
        order_type=OrderType.GTC,
    )
    print(json.dumps(buy_resp, indent=2, default=str))

    # Pull actual filled size from response.
    # takingAmount is shares acquired on a BUY.
    filled = float(buy_resp.get("takingAmount", 0) or 0)
    if filled <= 0:
        print("\nBuy did not fill (takingAmount=0); skipping sell. Check status above.")
        return 1

    sell_size = int(filled)  # whole shares; if filled is fractional we may leave dust
    print(f"\n[SELL] Submitting maker sell for {sell_size} shares @ ${SELL_PRICE}...")
    time.sleep(1)  # brief gap so the buy's on-chain settlement clears

    sell_resp = client.create_and_post_order(
        order_args=OrderArgs(token_id=YES_TOKEN, price=SELL_PRICE, side=Side.SELL, size=sell_size),
        options=PartialCreateOrderOptions(tick_size=TICK_SIZE),
        order_type=OrderType.GTC,
    )
    print(json.dumps(sell_resp, indent=2, default=str))

    spent = float(buy_resp.get("makingAmount", 0) or 0)
    potential_revenue = sell_size * SELL_PRICE
    print(f"\nFlip summary: spent ${spent:.2f} → sell order for ${potential_revenue:.2f}")
    print(f"Profit if sell fills: ${potential_revenue - spent:.2f}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
