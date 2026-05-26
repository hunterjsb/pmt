"""Single source of truth for Polymarket API hosts + the pmproxy override.

`PMPROXY_URL`, if set, routes every public Polymarket call through the proxy
Lambda (which does the Cognito-authed multi-tenant fan-out). When unset,
calls go direct to the public hosts.
"""

from __future__ import annotations

import os

CLOB = "https://clob.polymarket.com"
GAMMA = "https://gamma-api.polymarket.com"
DATA = "https://data-api.polymarket.com"
POLYGON_RPC = "https://polygon-rpc.com"

UA = {"User-Agent": "pmtrader/1.0"}


def proxy_url() -> str:
    """Empty string when no proxy configured."""
    return os.environ.get("PMPROXY_URL", "").rstrip("/")


def clob_host(via_proxy: bool = False) -> str:
    p = proxy_url()
    return f"{p}/clob" if via_proxy and p else CLOB


def gamma_host(via_proxy: bool = False) -> str:
    p = proxy_url()
    return f"{p}/gamma" if via_proxy and p else GAMMA
