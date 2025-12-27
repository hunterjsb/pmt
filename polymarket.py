"""Polymarket API client module.

Thin wrappers around:
- Gamma API: Market metadata (events, markets, tags, search)
- CLOB API: Trading data (order books, prices, markets)
"""

import requests
from py_clob_client.client import ClobClient

CLOB_HOST = "https://clob.polymarket.com"
GAMMA_HOST = "https://gamma-api.polymarket.com"


class Clob:
    """Client for the Polymarket CLOB (Central Limit Order Book) API."""

    def __init__(self, host: str = CLOB_HOST) -> None:
        self.host = host
        self._client = ClobClient(host)

    def ok(self):
        """Check server status."""
        return self._client.get_ok()

    def server_time(self):
        """Get server timestamp."""
        return self._client.get_server_time()

    def sampling_markets(self, limit: int = 100) -> list[dict]:
        """Get active markets with order books."""
        response = requests.get(f"{self.host}/sampling-markets", timeout=10)
        response.raise_for_status()
        return response.json().get("data", [])[:limit]

    def order_book(self, token_id: str):
        """Get full order book for a token."""
        return self._client.get_order_book(token_id)

    def midpoint(self, token_id: str):
        """Get midpoint price for a token. Returns {'mid': '0.123'}."""
        return self._client.get_midpoint(token_id)

    def price(self, token_id: str, side: str = "BUY"):
        """Get best price for a token. Side is 'BUY' or 'SELL'. Returns {'price': '0.123'}."""
        return self._client.get_price(token_id, side=side)

    def spread(self, token_id: str):
        """Get bid/ask spread as (best_bid, best_ask)."""
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
        """Fetch events (which contain markets)."""
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
        """Fetch a specific event by its URL slug."""
        response = requests.get(f"{self.host}/events/slug/{slug}", timeout=10)
        response.raise_for_status()
        return response.json()

    def markets(self, limit: int = 10, closed: bool = False) -> list[dict]:
        """Fetch markets directly."""
        params = {
            "closed": str(closed).lower(),
            "limit": limit,
        }
        response = requests.get(f"{self.host}/markets", params=params, timeout=10)
        response.raise_for_status()
        return response.json()

    def market_by_slug(self, slug: str) -> dict:
        """Fetch a specific market by its slug."""
        response = requests.get(f"{self.host}/markets/slug/{slug}", timeout=10)
        response.raise_for_status()
        return response.json()

    def tags(self) -> list[dict]:
        """Fetch available tags for filtering markets."""
        response = requests.get(f"{self.host}/tags", timeout=10)
        response.raise_for_status()
        return response.json()

    def events_by_tag(
        self, tag_id: int, limit: int = 10, closed: bool = False
    ) -> list[dict]:
        """Fetch events filtered by tag."""
        params = {
            "tag_id": tag_id,
            "closed": str(closed).lower(),
            "limit": limit,
        }
        response = requests.get(f"{self.host}/events", params=params, timeout=10)
        response.raise_for_status()
        return response.json()

    def search(self, query: str, limit: int = 10) -> list[dict]:
        """Search for markets by keyword."""
        params = {
            "query": query,
            "limit": limit,
        }
        response = requests.get(f"{self.host}/search", params=params, timeout=10)
        response.raise_for_status()
        return response.json()


clob = Clob()
gamma = Gamma()
