"""Anthropic Hanta Straddle — both legs as sanity-sized hedges.

Thesis: we don't think a hantavirus pandemic happens in 2026, so the main bet
is NO pandemic at 92.85¢ (placed separately in hantavirus_no.py). This script
buys a tiny YES vaccine position as the offset hedge — if a pandemic does
happen, a vaccine becomes likely and pays out, softening the blow.

Both legs intentionally tiny ($5-ish): this is opinion expression, not
rewards farming.

Routes through pmproxy Lambda (eu-west-1) for the geoblock bypass.
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

# Hantavirus vaccine 2026 — YES token
YES_TOKEN = "33574848766046164159312361389126746625941229104553637902902710371273925289603"

PRICE = 0.09
SIZE = 12  # min 5 shares for maker orders, but marketable (taker) orders need ≥$1 notional
TICK_SIZE = "0.01"  # vaccine market uses 1¢ tick; pandemic market uses 0.1¢


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
    print("Anthropic Hanta Straddle — vaccine YES hedge")
    print("=" * 60)
    print(f"  Token:  {YES_TOKEN}")
    print(f"  BUY:    {SIZE} YES @ ${PRICE}  (cost ${SIZE*PRICE:.4f})")
    print(f"  Mode:   {'LIVE — will submit' if args.send else 'DRY-RUN'}")
    print("=" * 60)

    if not args.send:
        print("\nDry-run complete. Re-run with --send to submit.")
        return 0

    print("\nSubmitting...")
    resp = client.create_and_post_order(
        order_args=OrderArgs(token_id=YES_TOKEN, price=PRICE, side=Side.BUY, size=SIZE),
        options=PartialCreateOrderOptions(tick_size=TICK_SIZE),
        order_type=OrderType.GTC,
    )
    print(json.dumps(resp, indent=2, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
