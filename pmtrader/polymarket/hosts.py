"""Single source of truth for Polymarket API hosts + the pmproxy override.

`PMPROXY_URL`, if set, routes calls through the proxy Lambda; requests to
it are SigV4-signed (see sigv4.py — IAM is the proxy's sole auth layer).
When unset, calls go direct to the public hosts.

Only `gamma_host()` consults the override today; the other constants below
are used raw, so a proxy is not in fact in front of every call.
"""

from __future__ import annotations

import os

CLOB = "https://clob.polymarket.com"
GAMMA = "https://gamma-api.polymarket.com"
DATA = "https://data-api.polymarket.com"
USER_PNL = "https://user-pnl-api.polymarket.com"
POLYGON_RPC = "https://polygon-rpc.com"
LB_API = "https://lb-api.polymarket.com"  # leaderboard: lifetime pnl/volume by wallet

UA = {"User-Agent": "pmtrader/1.0"}


def proxy_url() -> str:
    """Empty string when no proxy configured."""
    return os.environ.get("PMPROXY_URL", "").rstrip("/")


def gamma_host(via_proxy: bool = False) -> str:
    p = proxy_url()
    return f"{p}/gamma" if via_proxy and p else GAMMA
