"""Pandemic NO — maker bid for accumulation + rewards.

Posts a 216-share BUY limit at $0.9285 on the Hantavirus pandemic 2026 NO token.
- $0.9285 = between current best bid ($0.928) and best ask ($0.929), so it becomes
  the new best bid and goes to the front of the queue.
- 216 shares × $0.9285 = $200.56 notional, just above the $200 min for liquidity
  rewards on the pandemic market ($1000/day pot, max spread 5.5¢ from mid).
- Either it fills (we get 216 more NO shares cheap, matching our thesis) or it
  sits and earns rewards while we wait.

Routes through pmproxy Lambda for the geoblock bypass.
"""

import argparse
import json
import os
import sys

from dotenv import load_dotenv

load_dotenv()

# --- Monkey-patch v2 HTTP layer to inject Cognito Bearer on every call. ---
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

# Hantavirus pandemic 2026 — NO token
NO_TOKEN = "95212449865986159112377413335252801281670333750637442556685159781445406848396"

PRICE = 0.9285  # 0.1¢ inside current best bid (0.928); becomes new best bid
SIZE = 216  # 216 × 0.9285 = $200.56, just clears the $200 rewards-eligibility floor
TICK_SIZE = "0.001"  # pandemic market: 0.1¢ tick


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
    print("Pandemic NO — maker bid for accumulation + rewards")
    print("=" * 60)
    print(f"  Token:  {NO_TOKEN}")
    print(f"  BID:    {SIZE} NO @ ${PRICE}  (notional ${SIZE*PRICE:.4f})")
    print(f"  Rewards-eligible: {SIZE*PRICE >= 200}")
    print(f"  Mode:   {'LIVE — will submit' if args.send else 'DRY-RUN'}")
    print("=" * 60)

    if not args.send:
        print("\nDry-run complete. Re-run with --send to submit.")
        return 0

    print("\nSubmitting maker bid...")
    resp = client.create_and_post_order(
        order_args=OrderArgs(token_id=NO_TOKEN, price=PRICE, side=Side.BUY, size=SIZE),
        options=PartialCreateOrderOptions(tick_size=TICK_SIZE),
        order_type=OrderType.GTC,
    )
    print(json.dumps(resp, indent=2, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
