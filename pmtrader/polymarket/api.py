"""High-level authenticated Polymarket trading API.

Wraps py_clob_client_v2 with friendlier methods and hides the Cognito Bearer
monkey-patch + L1/L2 boilerplate.

Read endpoints (positions, value, activity, rewards config) hit the public
data-api or CLOB directly so they don't need auth.

Usage:
    from polymarket import PolymarketAPI

    api = PolymarketAPI()
    api.place_buy(token=..., price=0.93, size=200)
    result = api.flip(token=..., buy_price=0.09, sell_price=0.10, size=850)
    orders = api.get_orders()
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass
from pathlib import Path

import requests

DATA_API = "https://data-api.polymarket.com"
CLOB_API = "https://clob.polymarket.com"
GAMMA_API = "https://gamma-api.polymarket.com"
UA = {"User-Agent": "pmtrader/1.0"}
_MARKET_CACHE = Path.home() / ".cache" / "pmt" / "markets.json"


def lookup_market_name(condition_id: str) -> str | None:
    """Resolve condition_id → market question, with persistent disk cache.

    Returns None on miss + network failure. Cache file is human-readable JSON,
    safe to delete to force re-resolution.
    """
    _MARKET_CACHE.parent.mkdir(parents=True, exist_ok=True)
    try:
        cache: dict = json.loads(_MARKET_CACHE.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        cache = {}
    if condition_id in cache:
        return cache[condition_id]
    try:
        r = requests.get(f"{CLOB_API}/markets/{condition_id}", headers=UA, timeout=5)
        r.raise_for_status()
        name = r.json().get("question")
        if not name:
            return None
        cache[condition_id] = name
        _MARKET_CACHE.write_text(json.dumps(cache, indent=2))
        return name
    except Exception:
        return None


@dataclass
class FlipResult:
    buy_id: str
    sell_id: str
    buy_filled: int
    cost: float
    sell_price: float
    sell_status: str

    @property
    def potential_profit(self) -> float:
        return self.buy_filled * self.sell_price - self.cost


class PolymarketAPI:
    """Authenticated Polymarket trading client."""

    def __init__(self) -> None:
        from .clob_v2 import create_authenticated_clob_v2

        self.client = create_authenticated_clob_v2()

    # --- order placement ---

    def place_buy(
        self,
        *,
        token: str,
        price: float,
        size: int,
        tick_size: str | None = None,
    ) -> dict:
        return self._place(token=token, price=price, size=size, side="BUY", tick_size=tick_size)

    def place_sell(
        self,
        *,
        token: str,
        price: float,
        size: int,
        tick_size: str | None = None,
    ) -> dict:
        return self._place(token=token, price=price, size=size, side="SELL", tick_size=tick_size)

    def _place(self, *, token, price, size, side, tick_size=None) -> dict:
        from py_clob_client_v2 import OrderArgs, OrderType, PartialCreateOrderOptions, Side

        s = Side.BUY if side == "BUY" else Side.SELL
        ts = tick_size or self.get_tick_size(token)
        return self.client.create_and_post_order(
            order_args=OrderArgs(token_id=token, price=price, side=s, size=size),
            options=PartialCreateOrderOptions(tick_size=ts),
            order_type=OrderType.GTC,
        )

    def flip(
        self,
        *,
        token: str,
        buy_price: float,
        sell_price: float,
        size: int,
        tick_size: str | None = None,
        max_settlement_attempts: int = 6,
    ) -> FlipResult:
        """Buy at buy_price, then sell at sell_price.

        The sell side retries on the "not enough balance / allowance" error
        Polymarket returns when the buy hasn't settled on-chain yet.
        """
        from py_clob_client_v2.exceptions import PolyApiException

        ts = tick_size or self.get_tick_size(token)

        buy_resp = self.place_buy(token=token, price=buy_price, size=size, tick_size=ts)
        filled = int(float(buy_resp.get("takingAmount") or 0))
        if filled <= 0:
            raise RuntimeError(f"buy did not fill: {buy_resp}")
        cost = float(buy_resp.get("makingAmount") or 0)

        sell_resp: dict | None = None
        for attempt in range(max_settlement_attempts):
            try:
                sell_resp = self.place_sell(
                    token=token, price=sell_price, size=filled, tick_size=ts
                )
                break
            except PolyApiException as e:
                msg = str(e).lower()
                if attempt + 1 < max_settlement_attempts and (
                    "balance" in msg or "allowance" in msg
                ):
                    time.sleep(0.5 * (2 ** attempt))
                    continue
                raise

        return FlipResult(
            buy_id=buy_resp.get("orderID", ""),
            sell_id=(sell_resp or {}).get("orderID", ""),
            buy_filled=filled,
            cost=cost,
            sell_price=sell_price,
            sell_status=(sell_resp or {}).get("status", ""),
        )

    def cancel(self, order_id: str) -> dict:
        return self.client.cancel_orders([order_id])

    # --- L2-authed reads ---

    def get_orders(self) -> list[dict]:
        raw = self.client.get_open_orders()
        data = raw.data if hasattr(raw, "data") else (raw or [])
        return [d if isinstance(d, dict) else getattr(d, "__dict__", {}) for d in data]

    def get_tick_size(self, token: str) -> str:
        return str(self.client.get_tick_size(token))

    def get_book(self, token: str) -> dict:
        return self.client.get_order_book(token)

    # --- public reads (no auth) ---

    @property
    def funder(self) -> str:
        addr = os.environ.get("PM_FUNDER_ADDRESS")
        if not addr:
            raise RuntimeError("PM_FUNDER_ADDRESS not set")
        return addr

    def get_positions(self) -> list[dict]:
        r = requests.get(
            f"{DATA_API}/positions",
            params={"user": self.funder, "limit": 200},
            headers=UA,
            timeout=10,
        )
        r.raise_for_status()
        return r.json() or []

    def get_portfolio_value(self) -> float:
        r = requests.get(
            f"{DATA_API}/value", params={"user": self.funder}, headers=UA, timeout=10
        )
        r.raise_for_status()
        data = r.json()
        return data[0].get("value", 0) if data else 0

    def get_activity(self, *, kind: str | None = None, limit: int = 100) -> list[dict]:
        params: dict = {"user": self.funder, "limit": limit}
        if kind:
            params["type"] = kind
        r = requests.get(f"{DATA_API}/activity", params=params, headers=UA, timeout=10)
        r.raise_for_status()
        return r.json() or []

    def get_full_activity(self, *, kind: str | None = None, page: int = 500) -> list[dict]:
        """Paginate `/activity` via offset until empty. Returns newest-first."""
        out: list[dict] = []
        offset = 0
        while True:
            params: dict = {"user": self.funder, "limit": page, "offset": offset}
            if kind:
                params["type"] = kind
            r = requests.get(f"{DATA_API}/activity", params=params, headers=UA, timeout=15)
            r.raise_for_status()
            batch = r.json() or []
            if not batch:
                break
            out.extend(batch)
            if len(batch) < page:
                break
            offset += page
        return out

    def get_rewards_config(self, condition_id: str) -> dict:
        r = requests.get(
            f"{CLOB_API}/rewards/markets/{condition_id}", headers=UA, timeout=10
        )
        r.raise_for_status()
        data = r.json().get("data", [])
        return data[0] if data else {}

    # --- market lookups ---

    def get_market(self, slug_or_cid: str) -> dict:
        """Look up a market or event by gamma slug or CLOB condition_id."""
        if slug_or_cid.startswith("0x"):
            r = requests.get(
                f"{CLOB_API}/markets/{slug_or_cid}", headers=UA, timeout=10
            )
            r.raise_for_status()
            return r.json()
        r = requests.get(
            f"{GAMMA_API}/events", params={"slug": slug_or_cid}, headers=UA, timeout=10
        )
        r.raise_for_status()
        events = r.json() or []
        return events[0] if events else {}

    def search_markets(self, query: str, *, limit: int = 20) -> list[dict]:
        """Free-text search across events. Returns event records with embedded markets."""
        r = requests.get(
            f"{GAMMA_API}/public-search",
            params={"q": query, "limit_per_type": limit},
            headers=UA,
            timeout=10,
        )
        r.raise_for_status()
        return (r.json() or {}).get("events", [])
