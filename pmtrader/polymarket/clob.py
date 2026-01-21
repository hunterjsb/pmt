"""CLOB (Central Limit Order Book) API client."""

from __future__ import annotations

import os
import time

import requests
from py_clob_client.client import ClobClient
from py_clob_client.clob_types import (
    MarketOrderArgs,
    OpenOrderParams,
    OrderArgs,
    OrderType,
)

from .models import Market, OrderBook, OrderBookLevel, Token

# Optional cognito support (requires boto3)
try:
    from .cognito import CognitoAuth, create_cognito_auth
except ImportError:
    CognitoAuth = None  # type: ignore
    create_cognito_auth = lambda: None  # type: ignore


def _get_proxy_headers(cognito_auth: CognitoAuth | None = None) -> dict[str, str]:
    """Get headers for proxy requests, including auth if available."""
    if cognito_auth is None:
        return {}
    return cognito_auth.get_auth_header()


def get_order_book_depth(
    token_id: str,
    host: str = "https://clob.polymarket.com",
    cognito_auth: CognitoAuth | None = None,
) -> OrderBook:
    """Get full order book depth with all price levels via direct API call.

    The py_clob_client library only returns aggregated levels. This function
    fetches the complete order book ladder directly from the API.

    Args:
        token_id: The token ID to get order book for
        host: CLOB API host URL

    Returns:
        OrderBook with full depth of bids and asks

    Example:
        >>> book = get_order_book_depth("123456789...")
        >>> print(f"Best ask: {book.asks[0].price:.3f} (size: {book.asks[0].size})")
        >>> print(f"Next ask: {book.asks[1].price:.3f} (size: {book.asks[1].size})")
    """
    url = f"{host}/book"
    params = {"token_id": token_id}
    headers = _get_proxy_headers(cognito_auth)

    response = requests.get(url, params=params, headers=headers, timeout=10)
    response.raise_for_status()

    data = response.json()

    # Parse bids and asks, sorted for display
    # Bids: highest price first (best bid at top)
    # Asks: lowest price first (best ask at top)
    bids = sorted(
        [
            OrderBookLevel(float(b["price"]), float(b["size"]))
            for b in data.get("bids", [])
        ],
        key=lambda x: x.price,
        reverse=True,
    )
    asks = sorted(
        [
            OrderBookLevel(float(a["price"]), float(a["size"]))
            for a in data.get("asks", [])
        ],
        key=lambda x: x.price,
    )

    return OrderBook(name="Token", bids=bids, asks=asks)


CLOB_HOST = "https://clob.polymarket.com"
CHAIN_ID = 137  # Polygon

# Public Polygon RPC and contract addresses (not sensitive)
POLYGON_RPC = "https://polygon-rpc.com"
USDC_CONTRACT = "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174"
CTF_CONTRACT = (
    "0x4D97DCd97eC945f40cF65F87097ACe5EA0476045"  # Conditional Tokens (ERC-1155)
)
GAMMA_HOST = "https://gamma-api.polymarket.com"


def get_proxy_url() -> str:
    """Get proxy URL from environment (read at runtime)."""
    return os.environ.get("PMPROXY_URL", "")


def get_clob_host(proxy: bool = False) -> str:
    """Get the CLOB host URL, optionally routing through proxy."""
    proxy_url = get_proxy_url()
    if proxy and proxy_url:
        return f"{proxy_url.rstrip('/')}/clob"
    return CLOB_HOST


def get_gamma_host(proxy: bool = False) -> str:
    """Get the Gamma host URL, optionally routing through proxy."""
    proxy_url = get_proxy_url()
    if proxy and proxy_url:
        return f"{proxy_url.rstrip('/')}/gamma"
    return GAMMA_HOST


def get_chain_host(proxy: bool = False) -> str:
    """Get the Chain/RPC host URL, optionally routing through proxy."""
    proxy_url = get_proxy_url()
    if proxy and proxy_url:
        return f"{proxy_url.rstrip('/')}/chain"
    return POLYGON_RPC


class Clob:
    """Read-only client for the Polymarket CLOB (Central Limit Order Book) API."""

    def __init__(
        self,
        host: str | None = None,
        *,
        proxy: bool = False,
        cognito_auth: CognitoAuth | None = None,
    ) -> None:
        self.host = host or get_clob_host(proxy)
        self._client = ClobClient(self.host)
        self._cognito_auth = cognito_auth
        self._is_proxy = proxy or bool(get_proxy_url())

    def ok(self):
        return self._client.get_ok()

    def server_time(self):
        return self._client.get_server_time()

    def _get_headers(self) -> dict[str, str]:
        """Get headers for requests, including auth if using proxy."""
        if self._is_proxy and self._cognito_auth:
            return self._cognito_auth.get_auth_header()
        return {}

    def market(self, condition_id: str) -> dict:
        """Get market info by condition_id."""
        response = requests.get(
            f"{self.host}/markets/{condition_id}",
            headers=self._get_headers(),
            timeout=10,
        )
        response.raise_for_status()
        return response.json()

    def sampling_markets(self, limit: int = 100) -> list[Market]:
        response = requests.get(
            f"{self.host}/sampling-markets",
            headers=self._get_headers(),
            timeout=10,
        )
        response.raise_for_status()
        data = response.json().get("data", [])[:limit]

        markets = []
        for m in data:
            tokens = [
                Token(
                    outcome=t.get("outcome", "?"),
                    price=t.get("price"),
                    token_id=t.get("token_id", ""),
                )
                for t in m.get("tokens", [])
            ]
            markets.append(Market(question=m.get("question", "Unknown"), tokens=tokens))

        return markets

    def order_book(self, token_id: str, name: str = "Token") -> OrderBook:
        """Get order book for a token.

        Note: py_clob_client aggregates order book levels. For full depth,
        use get_order_book_depth() function instead.
        """
        book = self._client.get_order_book(token_id)
        bids = [
            OrderBookLevel(float(b.price), float(b.size)) for b in (book.bids or [])
        ]
        asks = [
            OrderBookLevel(float(a.price), float(a.size)) for a in (book.asks or [])
        ]
        return OrderBook(name=name, bids=bids, asks=asks)

    def midpoint(self, token_id: str):
        """Returns {'mid': '0.123'}."""
        return self._client.get_midpoint(token_id)

    def price(self, token_id: str, side: str = "BUY"):
        """Returns {'price': '0.123'}."""
        return self._client.get_price(token_id, side=side)

    def spread(self, token_id: str):
        """Returns (best_bid_dict, best_ask_dict)."""
        return self.price(token_id, "SELL"), self.price(token_id, "BUY")


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
        host: str | None = None,
        chain_id: int = CHAIN_ID,
        polygon_rpc: str | None = None,
        *,
        proxy: bool = False,
        cognito_auth: CognitoAuth | None = None,
    ) -> None:
        self.host = host or get_clob_host(proxy)
        self._funder = funder_address
        self._rpc = polygon_rpc or get_chain_host(proxy)
        self._cognito_auth = cognito_auth
        self._is_proxy = proxy or bool(get_proxy_url())

        self._client = ClobClient(
            self.host,
            key=private_key,
            chain_id=chain_id,
            signature_type=signature_type,
            funder=funder_address,
        )
        self._client.set_api_creds(self._client.create_or_derive_api_creds())

    def _get_headers(self) -> dict[str, str]:
        """Get headers for requests, including auth if using proxy."""
        if self._is_proxy and self._cognito_auth:
            return self._cognito_auth.get_auth_header()
        return {}

    # -----------------------------
    # CLOB: orders & trades
    # -----------------------------
    # Note: Order-related methods use py_clob_client which handles POLY_* headers
    # for Polymarket authentication. For proxy authentication (Bearer token),
    # the py_clob_client would need to be extended to support custom headers.

    def create_order(
        self,
        token_id: str,
        price: float,
        size: float,
        side: str = "BUY",
        order_type: str = "GTC",
    ):
        """Create a limit order.

        Args:
            token_id: The token ID to trade
            price: Price per share (e.g., 0.65 for 65¢)
            size: Number of shares to buy/sell
            side: "BUY" or "SELL"
            order_type: Order type - "GTC" (Good Til Cancelled), "FOK" (Fill or Kill), "GTD" (Good Til Date)

        Returns:
            Signed order object
        """
        order_args = OrderArgs(
            token_id=token_id,
            price=price,
            size=size,
            side=side,
        )
        return self._client.create_order(order_args)

    def post_order(
        self,
        token_id: str,
        price: float,
        size: float,
        side: str = "BUY",
        order_type: str = "GTC",
    ):
        """Create and post a limit order in one call.

        Args:
            token_id: The token ID to trade
            price: Price per share (e.g., 0.65 for 65¢)
            size: Number of shares to buy/sell
            side: "BUY" or "SELL"
            order_type: Order type - "GTC", "FOK", "GTD"

        Returns:
            Order response from API
        """
        order_args = OrderArgs(
            token_id=token_id,
            price=price,
            size=size,
            side=side,
        )
        return self._client.create_and_post_order(order_args)

    def market_order(
        self,
        token_id: str,
        amount: float,
        side: str = "BUY",
    ):
        """Place a market order (executes immediately at best available price).

        Args:
            token_id: The token ID to trade
            amount: Dollar amount to spend (for BUY) or shares to sell (for SELL)
            side: "BUY" or "SELL"

        Returns:
            Order response from API
        """
        order_args = MarketOrderArgs(
            token_id=token_id,
            amount=amount,
            side=side,
        )
        # Create the signed order
        signed_order = self._client.create_market_order(order_args)
        # Post it to the exchange (FOK = Fill or Kill, executes immediately or cancels)
        return self._client.post_order(signed_order, OrderType.FOK)

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

    def order_book(self, token_id: str, name: str = "Token") -> OrderBook:
        """Get order book for a token.

        Note: py_clob_client aggregates order book levels. For full depth,
        use get_order_book_depth() function instead.
        """
        book = self._client.get_order_book(token_id)
        bids = [
            OrderBookLevel(float(b.price), float(b.size)) for b in (book.bids or [])
        ]
        asks = [
            OrderBookLevel(float(a.price), float(a.size)) for a in (book.asks or [])
        ]
        return OrderBook(name=name, bids=bids, asks=asks)

    def midpoint(self, token_id: str):
        return self._client.get_midpoint(token_id)

    def price(self, token_id: str, side: str = "BUY"):
        return self._client.get_price(token_id, side=side)

    def spread(self, token_id: str):
        return self.price(token_id, "SELL"), self.price(token_id, "BUY")

    # -----------------------------
    # On-chain balances (funder)
    # -----------------------------

    def _rpc_call(self, to: str, data: str, retries: int = 3) -> str:
        """Make an eth_call and return the result hex string with retry logic."""
        payload = {
            "jsonrpc": "2.0",
            "method": "eth_call",
            "params": [{"to": to, "data": data}, "latest"],
            "id": 1,
        }

        # Include auth headers if using proxy
        headers = self._get_headers()

        last_error = None
        for attempt in range(retries):
            try:
                response = requests.post(self._rpc, json=payload, headers=headers, timeout=10)
                response.raise_for_status()
                result = response.json()

                if "error" in result:
                    error_msg = result["error"].get("message", str(result["error"]))
                    # Retry on rate limit
                    if "rate limit" in error_msg.lower() or "too many" in error_msg.lower():
                        last_error = RuntimeError(f"RPC rate limited: {error_msg}")
                        time.sleep(2 + attempt * 2)  # Backoff: 2s, 4s, 6s
                        continue
                    raise RuntimeError(f"RPC error: {error_msg}")

                if "result" not in result:
                    raise RuntimeError(f"RPC response missing 'result': {result}")

                return result["result"]

            except requests.RequestException as e:
                last_error = e
                time.sleep(2 ** attempt)
                continue

        raise last_error or RuntimeError("RPC call failed after retries")

    def usdc_balance(self) -> float:
        """USDC balance for funder address via JSON-RPC eth_call."""
        address_padded = self._funder[2:].lower().zfill(64)
        data = "0x70a08231" + address_padded  # balanceOf(address)

        hex_result = self._rpc_call(USDC_CONTRACT, data)
        balance_wei = int(hex_result, 16)
        return balance_wei / 1e6  # USDC has 6 decimals

    def token_balance(self, token_id: str) -> float:
        """ERC-1155 balanceOf(funder, token_id) for Conditional Tokens."""
        address_padded = self._funder[2:].lower().zfill(64)
        token_padded = hex(int(token_id))[2:].zfill(64)
        data = "0x00fdd58e" + address_padded + token_padded  # balanceOf(address,id)

        hex_result = self._rpc_call(CTF_CONTRACT, data)
        balance = int(hex_result, 16)
        return balance / 1e6  # Polymarket outcome tokens use 6 decimals

    def positions(self, max_tokens: int = 150) -> list[dict]:
        """Current positions by on-chain balances for tokens seen in trade history.

        Args:
            max_tokens: Max unique tokens to check (most recent trades first)

        Returns list of:
          { token_id, outcome, market, shares }
        """
        trades = self.trades()

        # Build token metadata from most recent trades first
        token_meta: dict[str, dict] = {}
        for t in trades:
            if len(token_meta) >= max_tokens:
                break
            token_id = t["asset_id"]
            if token_id not in token_meta:
                token_meta[token_id] = {"outcome": t["outcome"], "market": t["market"]}

        # Sequential with delay to avoid RPC rate limits
        positions: list[dict] = []
        for i, (token_id, meta) in enumerate(token_meta.items()):
            if i > 0:
                time.sleep(0.3)  # Rate limit throttle
            try:
                bal = self.token_balance(token_id)
                if bal > 0.01:
                    positions.append(
                        {
                            "token_id": token_id,
                            "outcome": meta["outcome"],
                            "market": meta["market"],
                            "shares": bal,
                        }
                    )
            except Exception:
                continue  # Skip tokens we can't fetch

        return positions

    # -----------------------------
    # Resolution checking
    # -----------------------------

    def _normalize_condition_id(self, condition_id: str) -> str:
        """Normalize condition ID to 64 hex chars (bytes32) without 0x prefix."""
        # Remove 0x prefix if present
        cid = condition_id[2:] if condition_id.startswith("0x") else condition_id
        # Left-pad with zeros to 64 chars (bytes32)
        return cid.lower().zfill(64)

    def is_condition_resolved(self, condition_id: str) -> bool:
        """Check if a condition has been resolved by querying payoutDenominator.

        Args:
            condition_id: The condition ID (hex string, with or without 0x prefix)

        Returns:
            True if resolved (payoutDenominator > 0), False otherwise
        """
        # payoutDenominator(bytes32) selector: 0xdd34de67
        condition_padded = self._normalize_condition_id(condition_id)
        data = "0xdd34de67" + condition_padded

        hex_result = self._rpc_call(CTF_CONTRACT, data)
        return int(hex_result, 16) > 0

    def get_payout_numerators(self, condition_id: str) -> list[int]:
        """Get payout numerators for a resolved condition.

        Args:
            condition_id: The condition ID (hex string, with or without 0x prefix)

        Returns:
            List of payout numerators [outcome0, outcome1] for binary markets.
            e.g., [1, 0] means outcome 0 (Yes) won, [0, 1] means outcome 1 (No) won.
        """
        condition_padded = self._normalize_condition_id(condition_id)

        # payoutNumerators(bytes32, uint256) selector: 0x0504c814
        # Query for index 0 and 1 (binary market)
        numerators = []
        for idx in range(2):
            idx_padded = hex(idx)[2:].zfill(64)
            data = "0x0504c814" + condition_padded + idx_padded
            hex_result = self._rpc_call(CTF_CONTRACT, data)
            numerators.append(int(hex_result, 16))

        return numerators


def create_authenticated_clob(*, proxy: bool = False) -> AuthenticatedClob | None:
    """Create an authenticated client from environment variables.

    Requires:
    - PM_PRIVATE_KEY
    - PM_FUNDER_ADDRESS
    Optional:
    - PM_SIGNATURE_TYPE (default 1)
    - PMPROXY_URL (for proxy support)
    - PMPROXY_COGNITO_CLIENT_ID, PMPROXY_USERNAME, PMPROXY_PASSWORD (for proxy auth)

    Args:
        proxy: If True, route requests through proxy (requires PMPROXY_URL env var)
    """
    from dotenv import load_dotenv

    load_dotenv()

    private_key = os.environ.get("PM_PRIVATE_KEY")
    funder_address = os.environ.get("PM_FUNDER_ADDRESS")
    signature_type = int(os.environ.get("PM_SIGNATURE_TYPE", "1"))

    if not private_key or not funder_address:
        return None

    # Create Cognito auth if using proxy and credentials are available
    cognito_auth = None
    if proxy:
        cognito_auth = create_cognito_auth()

    return AuthenticatedClob(
        private_key=private_key,
        funder_address=funder_address,
        signature_type=signature_type,
        proxy=proxy,
        cognito_auth=cognito_auth,
    )
