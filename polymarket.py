"""Polymarket API client module.

Thin wrappers around:
- Gamma API: Market metadata (events, markets, tags, search)
- CLOB API: Trading data (order books, prices, markets)
- Authenticated CLOB: orders/trades + on-chain balances/positions

Design notes:
- Keep wrappers thin; allow errors to raise.
- Authenticated client derives API creds from the private key (avoids stale env creds).
- Positions are ERC-1155 balances queried via Polygon JSON-RPC against the funder address.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed

import requests
from py_clob_client.client import ClobClient
from py_clob_client.clob_types import OpenOrderParams

CLOB_HOST = "https://clob.polymarket.com"
GAMMA_HOST = "https://gamma-api.polymarket.com"
CHAIN_ID = 137  # Polygon

# Public Polygon RPC and contract addresses (not sensitive)
POLYGON_RPC = "https://polygon-rpc.com"
USDC_CONTRACT = "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174"
CTF_CONTRACT = (
    "0x4D97DCd97eC945f40cF65F87097ACe5EA0476045"  # Conditional Tokens (ERC-1155)
)


class Clob:
    """Read-only client for the Polymarket CLOB (Central Limit Order Book) API."""

    def __init__(self, host: str = CLOB_HOST) -> None:
        self.host = host
        self._client = ClobClient(host)

    def ok(self):
        return self._client.get_ok()

    def server_time(self):
        return self._client.get_server_time()

    def sampling_markets(self, limit: int = 100) -> list[dict]:
        response = requests.get(f"{self.host}/sampling-markets", timeout=10)
        response.raise_for_status()
        return response.json().get("data", [])[:limit]

    def order_book(self, token_id: str):
        return self._client.get_order_book(token_id)

    def midpoint(self, token_id: str):
        """Returns {'mid': '0.123'}."""
        return self._client.get_midpoint(token_id)

    def price(self, token_id: str, side: str = "BUY"):
        """Returns {'price': '0.123'}."""
        return self._client.get_price(token_id, side=side)

    def spread(self, token_id: str):
        """Returns (best_bid_dict, best_ask_dict)."""
        return self.price(token_id, "SELL"), self.price(token_id, "BUY")


class Gamma:
    """Client for the Polymarket Gamma API (market metadata)."""

    def __init__(self, host: str = GAMMA_HOST) -> None:
        self.host = host

    def events(
        self,
        limit: int = 10,
        closed: bool = False,
        order: str = "id",
        ascending: bool = False,
    ) -> list[dict]:
        params = {
            "order": order,
            "ascending": str(ascending).lower(),
            "closed": str(closed).lower(),
            "limit": limit,
        }
        response = requests.get(f"{self.host}/events", params=params, timeout=10)
        response.raise_for_status()
        return response.json()

    def event_by_slug(self, slug: str) -> dict:
        response = requests.get(f"{self.host}/events/slug/{slug}", timeout=10)
        response.raise_for_status()
        return response.json()

    def markets(self, limit: int = 10, closed: bool = False) -> list[dict]:
        params = {"closed": str(closed).lower(), "limit": limit}
        response = requests.get(f"{self.host}/markets", params=params, timeout=10)
        response.raise_for_status()
        return response.json()

    def market_by_slug(self, slug: str) -> dict:
        response = requests.get(f"{self.host}/markets/slug/{slug}", timeout=10)
        response.raise_for_status()
        return response.json()

    def tags(self) -> list[dict]:
        response = requests.get(f"{self.host}/tags", timeout=10)
        response.raise_for_status()
        return response.json()

    def events_by_tag(
        self, tag_id: int, limit: int = 10, closed: bool = False
    ) -> list[dict]:
        params = {"tag_id": tag_id, "closed": str(closed).lower(), "limit": limit}
        response = requests.get(f"{self.host}/events", params=params, timeout=10)
        response.raise_for_status()
        return response.json()

    def search(self, query: str, limit: int = 10) -> list[dict]:
        params = {"query": query, "limit": limit}
        response = requests.get(f"{self.host}/search", params=params, timeout=10)
        response.raise_for_status()
        return response.json()


class AuthenticatedClob:
    """Authenticated CLOB client for orders/trades + on-chain balances/positions.

    Notes:
    - Uses funder address for on-chain balances (USDC + ERC-1155 outcome tokens).
    - Derives CLOB API creds from the private key.
    """

    def __init__(
        self,
        private_key: str,
        funder_address: str,
        signature_type: int = 0,
        host: str = CLOB_HOST,
        chain_id: int = CHAIN_ID,
        polygon_rpc: str = POLYGON_RPC,
    ) -> None:
        self.host = host
        self._funder = funder_address
        self._rpc = polygon_rpc

        self._client = ClobClient(
            host,
            key=private_key,
            chain_id=chain_id,
            signature_type=signature_type,
            funder=funder_address,
        )
        self._client.set_api_creds(self._client.create_or_derive_api_creds())

    # -----------------------------
    # CLOB: orders & trades
    # -----------------------------

    def trades(self):
        return self._client.get_trades()

    def open_orders(self, market: str = "", asset_id: str = ""):
        params = OpenOrderParams(market=market, asset_id=asset_id)
        return self._client.get_orders(params)

    def order(self, order_id: str):
        return self._client.get_order(order_id)

    def cancel(self, order_id: str):
        return self._client.cancel(order_id)

    def cancel_all(self):
        return self._client.cancel_all()

    # -----------------------------
    # Convenience read-only
    # -----------------------------

    def ok(self):
        return self._client.get_ok()

    def order_book(self, token_id: str):
        return self._client.get_order_book(token_id)

    def midpoint(self, token_id: str):
        return self._client.get_midpoint(token_id)

    def price(self, token_id: str, side: str = "BUY"):
        return self._client.get_price(token_id, side=side)

    def spread(self, token_id: str):
        return self.price(token_id, "SELL"), self.price(token_id, "BUY")

    # -----------------------------
    # On-chain balances (funder)
    # -----------------------------

    def usdc_balance(self) -> float:
        """USDC balance for funder address via JSON-RPC eth_call."""
        address_padded = self._funder[2:].lower().zfill(64)
        data = "0x70a08231" + address_padded  # balanceOf(address)

        payload = {
            "jsonrpc": "2.0",
            "method": "eth_call",
            "params": [{"to": USDC_CONTRACT, "data": data}, "latest"],
            "id": 1,
        }

        response = requests.post(self._rpc, json=payload, timeout=10)
        response.raise_for_status()
        result = response.json()

        balance_wei = int(result["result"], 16)
        return balance_wei / 1e6  # USDC has 6 decimals

    def token_balance(self, token_id: str) -> float:
        """ERC-1155 balanceOf(funder, token_id) for Conditional Tokens."""
        address_padded = self._funder[2:].lower().zfill(64)
        token_padded = hex(int(token_id))[2:].zfill(64)
        data = "0x00fdd58e" + address_padded + token_padded  # balanceOf(address,id)

        payload = {
            "jsonrpc": "2.0",
            "method": "eth_call",
            "params": [{"to": CTF_CONTRACT, "data": data}, "latest"],
            "id": 1,
        }

        response = requests.post(self._rpc, json=payload, timeout=10)
        response.raise_for_status()
        result = response.json()

        balance = int(result["result"], 16)
        return balance / 1e6  # Polymarket outcome tokens use 6 decimals

    def positions(self) -> list[dict]:
        """Current positions by on-chain balances for tokens seen in trade history.

        Returns list of:
          { token_id, outcome, market, shares }
        """
        trades = self.trades()

        token_meta: dict[str, dict] = {}
        for t in trades:
            token_id = t["asset_id"]
            if token_id not in token_meta:
                token_meta[token_id] = {"outcome": t["outcome"], "market": t["market"]}

        def fetch(token_id: str) -> tuple[str, float]:
            return token_id, self.token_balance(token_id)

        positions: list[dict] = []
        with ThreadPoolExecutor(max_workers=10) as ex:
            futures = {ex.submit(fetch, tid): tid for tid in token_meta}
            for fut in as_completed(futures):
                token_id, bal = fut.result()
                if bal > 0.01:
                    meta = token_meta[token_id]
                    positions.append(
                        {
                            "token_id": token_id,
                            "outcome": meta["outcome"],
                            "market": meta["market"],
                            "shares": bal,
                        }
                    )

        return positions


def create_authenticated_clob() -> AuthenticatedClob | None:
    """Create an authenticated client from environment variables.

    Requires:
    - PM_PRIVATE_KEY
    - PM_FUNDER_ADDRESS
    Optional:
    - PM_SIGNATURE_TYPE (default 0)
    """
    from env import PM_FUNDER_ADDRESS, PM_PRIVATE_KEY, PM_SIGNATURE_TYPE

    if not PM_PRIVATE_KEY or not PM_FUNDER_ADDRESS:
        return None

    return AuthenticatedClob(
        private_key=PM_PRIVATE_KEY,
        funder_address=PM_FUNDER_ADDRESS,
        signature_type=PM_SIGNATURE_TYPE,
    )


clob = Clob()
gamma = Gamma()
