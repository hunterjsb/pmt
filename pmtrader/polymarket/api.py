"""Authenticated Polymarket trading API.

`PolymarketAPI` wraps py_clob_client_v2 with friendlier methods and hides
the Cognito Bearer monkey-patch + L1/L2 derivation. Read endpoints that
don't need auth (positions, value, activity, rewards config) call the
public data-api or CLOB directly.

    from polymarket import PolymarketAPI
    api = PolymarketAPI()
    api.place(side="buy", token=..., price=0.93, size=200)
    api.get_positions()
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass
from pathlib import Path

import requests

from . import hosts

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
        r = requests.get(f"{hosts.CLOB}/markets/{condition_id}", headers=hosts.UA, timeout=5)
        r.raise_for_status()
        name = r.json().get("question")
        if not name:
            return None
        cache[condition_id] = name
        _MARKET_CACHE.write_text(json.dumps(cache, indent=2))
        return name
    except Exception:
        return None


def locked_buy_cash(order: dict) -> float:
    """Cash a resting BUY still commits: unfilled remainder x price.

    Shared by `pmt balance` and `pmt orders` so their Locked figures agree.
    """
    if str(order.get("side", "")).upper() != "BUY":
        return 0.0
    remaining = float(order.get("original_size") or 0) - float(order.get("size_matched") or 0)
    return remaining * float(order.get("price") or 0)


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
        from .env import load_project_env

        load_project_env()
        self.client = create_authenticated_clob_v2()

    # --- order placement ---

    def place(
        self,
        side: str,
        *,
        token: str,
        price: float,
        size: int,
        tick_size: str | None = None,
    ) -> dict:
        """Place a single GTC limit order. `side` is 'buy' or 'sell'."""
        from py_clob_client_v2 import OrderArgs, OrderType, PartialCreateOrderOptions, Side

        s = Side.BUY if side.lower() == "buy" else Side.SELL
        ts = tick_size or self.get_tick_size(token)
        return self.client.create_and_post_order(
            order_args=OrderArgs(token_id=token, price=price, side=s, size=size),
            options=PartialCreateOrderOptions(tick_size=ts),
            order_type=OrderType.GTC,
        )

    # Thin wrappers used by `flip` and any caller that prefers the typed form.
    def place_buy(self, *, token, price, size, tick_size=None) -> dict:
        return self.place("buy", token=token, price=price, size=size, tick_size=tick_size)

    def place_sell(self, *, token, price, size, tick_size=None) -> dict:
        return self.place("sell", token=token, price=price, size=size, tick_size=tick_size)

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
        ts = tick_size or self.get_tick_size(token)

        buy_resp = self.place_buy(token=token, price=buy_price, size=size, tick_size=ts)
        filled = int(float(buy_resp.get("takingAmount") or 0))
        if filled <= 0:
            raise RuntimeError(f"buy did not fill: {buy_resp}")
        cost = float(buy_resp.get("makingAmount") or 0)

        sell_resp = self._sell_with_settlement_retry(
            token=token,
            price=sell_price,
            size=filled,
            tick_size=ts,
            max_attempts=max_settlement_attempts,
        )

        return FlipResult(
            buy_id=buy_resp.get("orderID", ""),
            sell_id=(sell_resp or {}).get("orderID", ""),
            buy_filled=filled,
            cost=cost,
            sell_price=sell_price,
            sell_status=(sell_resp or {}).get("status", ""),
        )

    def _sell_with_settlement_retry(
        self,
        *,
        token: str,
        price: float,
        size: int,
        tick_size: str | None,
        max_attempts: int = 6,
    ) -> dict:
        """Sell with backoff on the balance/allowance error an unsettled buy causes."""
        from py_clob_client_v2.exceptions import PolyApiException

        for attempt in range(max_attempts):
            try:
                return self.place_sell(token=token, price=price, size=size, tick_size=tick_size)
            except PolyApiException as e:
                msg = str(e).lower()
                if attempt + 1 < max_attempts and ("balance" in msg or "allowance" in msg):
                    time.sleep(0.5 * (2 ** attempt))
                    continue
                raise
        raise RuntimeError("unreachable")  # loop always returns or raises

    def sweep(
        self,
        side: str,
        *,
        token: str,
        to_price: float,
        max_cost: float | None = None,
        tick_size: str | None = None,
        dry_run: bool = False,
    ) -> dict:
        """Take displayed liquidity through to_price with one GTC limit at to_price.

        buy consumes asks priced <= to_price; sell consumes bids priced >= to_price.
        Returns {"plan": {"levels", "size", "est_cost", "place_size"}, "placed": resp | None};
        never places when dry_run or place_size < 5 shares (exchange minimum).
        max_cost caps both the snapshot plan and the committed notional of the
        placed order (place_size * to_price) — displayed levels can vanish
        before the match, leaving every share to fill at to_price.
        """
        s = side.lower()
        if s not in ("buy", "sell"):
            raise ValueError(f"side must be 'buy' or 'sell', got {side!r}")

        book = self.book_levels(token)
        if s == "buy":
            band = [(p, sz) for p, sz in book["asks"] if p <= to_price]
        else:
            band = [(p, sz) for p, sz in book["bids"] if p >= to_price]

        if max_cost is not None:
            # Trim from the worst level down: partial-take the level that breaches budget.
            trimmed: list[tuple[float, float]] = []
            cost = 0.0
            for p, sz in band:
                level_cost = p * sz
                if cost + level_cost <= max_cost:
                    trimmed.append((p, sz))
                    cost += level_cost
                else:
                    part = (max_cost - cost) / p
                    if part > 0:
                        trimmed.append((p, part))
                    break
            band = trimmed

        size = sum(sz for _, sz in band)
        est_cost = sum(p * sz for p, sz in band)
        place_size = int(size)
        if max_cost is not None:
            place_size = min(place_size, int(max_cost / to_price))
        plan = {"levels": band, "size": size, "est_cost": est_cost, "place_size": place_size}

        placed = None
        if not dry_run and place_size >= 5:
            ts = tick_size or self.get_tick_size(token)
            placed = self.place(s, token=token, price=to_price, size=place_size, tick_size=ts)
        return {"plan": plan, "placed": placed}

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

    def book_levels(self, token: str, depth: int | None = None) -> dict:
        """Normalized float book: bids desc, asks asc, plus mid/spread from top of book."""
        b = self.get_book(token)

        def _lv(x) -> tuple[float, float]:
            if hasattr(x, "price"):
                return float(x.price), float(x.size)
            return float(x["price"]), float(x["size"])

        raw_bids = (b.bids if hasattr(b, "bids") else b.get("bids", [])) or []
        raw_asks = (b.asks if hasattr(b, "asks") else b.get("asks", [])) or []
        bids = sorted((_lv(x) for x in raw_bids), key=lambda x: x[0], reverse=True)
        asks = sorted((_lv(x) for x in raw_asks), key=lambda x: x[0])
        mid = spread = None
        if bids and asks:
            mid = (bids[0][0] + asks[0][0]) / 2
            spread = asks[0][0] - bids[0][0]
        if depth is not None:
            bids, asks = bids[:depth], asks[:depth]
        return {"bids": bids, "asks": asks, "mid": mid, "spread": spread}

    def get_usdc_balance(self) -> dict:
        """Cash view: spendable USDC plus cash committed to resting BUY orders."""
        from py_clob_client_v2 import AssetType, BalanceAllowanceParams

        resp = self.client.get_balance_allowance(
            BalanceAllowanceParams(asset_type=AssetType.COLLATERAL)
        )
        available = float((resp or {}).get("balance") or 0) / 1e6
        locked = sum(locked_buy_cash(o) for o in self.get_orders())
        return {"available": available, "locked": locked}

    # --- public reads (no auth) ---

    @property
    def funder(self) -> str:
        addr = os.environ.get("PM_FUNDER_ADDRESS")
        if not addr:
            raise RuntimeError("PM_FUNDER_ADDRESS not set")
        return addr

    def get_positions(self) -> list[dict]:
        r = requests.get(
            f"{hosts.DATA}/positions",
            params={"user": self.funder, "limit": 200},
            headers=hosts.UA,
            timeout=10,
        )
        r.raise_for_status()
        return r.json() or []

    def get_portfolio_value(self) -> float:
        r = requests.get(
            f"{hosts.DATA}/value", params={"user": self.funder}, headers=hosts.UA, timeout=10
        )
        r.raise_for_status()
        data = r.json()
        return data[0].get("value", 0) if data else 0

    def get_pnl_series(self, interval: str = "all", fidelity: str = "1d") -> list[dict]:
        """Cumulative P/L series behind the polymarket.com profile graph.

        Returns [{"t": unix_seconds, "p": cumulative_pnl}, ...]. Values are
        mark-to-market — realized plus open positions marked at current odds —
        which is why they diverge from the realized-only activity replay.
        """
        r = requests.get(
            f"{hosts.USER_PNL}/user-pnl",
            params={"user_address": self.funder, "interval": interval, "fidelity": fidelity},
            headers=hosts.UA,
            timeout=15,
        )
        r.raise_for_status()
        return r.json() or []

    def get_ui_pnl(self) -> dict[str, float | None]:
        """Window P/L exactly as the polymarket.com profile shows it.

        None for a window whose fetch failed — the endpoint can be slow on a
        cold cache, and a partial answer beats none.
        """
        from .pnl import UI_WINDOWS, ui_window_value

        out: dict[str, float | None] = {}
        for window, (interval, fidelity) in UI_WINDOWS.items():
            try:
                series = self.get_pnl_series(interval, fidelity)
            except requests.RequestException:
                out[window] = None
                continue
            out[window] = ui_window_value(series, all_time=window == "all")
        return out

    def get_activity(self, *, kind: str | None = None, limit: int = 100) -> list[dict]:
        params: dict = {"user": self.funder, "limit": limit}
        if kind:
            params["type"] = kind
        r = requests.get(f"{hosts.DATA}/activity", params=params, headers=hosts.UA, timeout=10)
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
            r = requests.get(f"{hosts.DATA}/activity", params=params, headers=hosts.UA, timeout=15)
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
            f"{hosts.CLOB}/rewards/markets/{condition_id}", headers=hosts.UA, timeout=10
        )
        r.raise_for_status()
        data = r.json().get("data", [])
        return data[0] if data else {}

    # --- market lookups ---

    def get_market(self, slug_or_cid: str) -> dict:
        """Look up a market or event by gamma slug or CLOB condition_id."""
        if slug_or_cid.startswith("0x"):
            r = requests.get(
                f"{hosts.CLOB}/markets/{slug_or_cid}", headers=hosts.UA, timeout=10
            )
            r.raise_for_status()
            return r.json()
        r = requests.get(
            f"{hosts.GAMMA}/events", params={"slug": slug_or_cid}, headers=hosts.UA, timeout=10
        )
        r.raise_for_status()
        events = r.json() or []
        return events[0] if events else {}

    def search_markets(self, query: str, *, limit: int = 20) -> list[dict]:
        """Free-text search across events. Returns event records with embedded markets."""
        r = requests.get(
            f"{hosts.GAMMA}/public-search",
            params={"q": query, "limit_per_type": limit},
            headers=hosts.UA,
            timeout=10,
        )
        r.raise_for_status()
        return (r.json() or {}).get("events", [])
