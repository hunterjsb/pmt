"""One-off: place a single 5-share NO order on Hantavirus pandemic 2026.

Uses py_clob_client_v2 (Polymarket's current API). Routes through pmproxy
Lambda (eu-west-1) to bypass the US geoblock.
"""

import argparse
import json
import os
import sys

from dotenv import load_dotenv

load_dotenv()

# --- Monkey-patch v2 HTTP layer to inject our Cognito Bearer on every call. ---
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

TOKEN_ID = "95212449865986159112377413335252801281670333750637442556685159781445406848396"
PRICE = 0.929
SIZE = 5
SIDE = Side.BUY
TICK_SIZE = "0.001"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--send", action="store_true", help="Actually submit (default: dry-run)")
    args = parser.parse_args()

    proxy_url = os.environ["PMPROXY_URL"].rstrip("/") + "/clob"
    private_key = os.environ["PM_PRIVATE_KEY"]
    funder = os.environ["PM_FUNDER_ADDRESS"]
    sig_type = int(os.environ.get("PM_SIGNATURE_TYPE", "1"))

    # L1: derive API creds
    boot = ClobClient(host=proxy_url, chain_id=137, key=private_key,
                      signature_type=sig_type, funder=funder)
    creds = boot.create_or_derive_api_key()

    # L2: fully authenticated
    client = ClobClient(host=proxy_url, chain_id=137, key=private_key,
                        signature_type=sig_type, funder=funder, creds=creds)

    print("=" * 60)
    print("Hantavirus pandemic 2026 — single NO sanity order via pmproxy")
    print("=" * 60)
    print(f"  Proxy:  {proxy_url}")
    print(f"  Token:  {TOKEN_ID}")
    print(f"  Side:   BUY  Price: ${PRICE}  Size: {SIZE}  Cost: ${PRICE*SIZE:.4f}")
    print(f"  Mode:   {'LIVE — will submit' if args.send else 'DRY-RUN'}")
    print("=" * 60)

    if not args.send:
        print("\nDry-run complete (would call create_and_post_order). Re-run with --send.")
        return 0

    print("\nSubmitting through pmproxy → Polymarket CLOB v2...")
    resp = client.create_and_post_order(
        order_args=OrderArgs(token_id=TOKEN_ID, price=PRICE, side=SIDE, size=SIZE),
        options=PartialCreateOrderOptions(tick_size=TICK_SIZE),
        order_type=OrderType.GTC,
    )
    print("\nResponse:")
    print(json.dumps(resp, indent=2, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
