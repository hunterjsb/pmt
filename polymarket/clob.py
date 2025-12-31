"""CLOB (Central Limit Order Book) API client."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed

import requests
from py_clob_client.client import ClobClient
from py_clob_client.clob_types import MarketOrderArgs, OpenOrderParams, OrderArgs, OrderType

from .models import Market, OrderBook, OrderBookLevel, Token


def get_order_book_depth(
    token_id: str, host: str = "https://clob.polymarket.com"
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

    response = requests.get(url, params=params, timeout=10)
    response.raise_for_status()

    data = response.json()

    # Parse bids and asks
    bids = [
        OrderBookLevel(float(b["price"]), float(b["size"]))
        for b in data.get("bids", [])
    ]
    asks = [
        OrderBookLevel(float(a["price"]), float(a["size"]))
        for a in data.get("asks", [])
    ]

    return OrderBook(name="Token", bids=bids, asks=asks)


CLOB_HOST = "https://clob.polymarket.com"
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

    def sampling_markets(self, limit: int = 100) -> list[Market]:
        response = requests.get(f"{self.host}/sampling-markets", timeout=10)
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
