"""Gamma API client for market metadata."""

from __future__ import annotations

import os

import requests

from .models import Event

GAMMA_HOST = "https://gamma-api.polymarket.com"


def get_proxy_url() -> str:
    """Get proxy URL from environment (read at runtime)."""
    return os.environ.get("PMPROXY_URL", "")


def get_gamma_host(proxy: bool = False) -> str:
    """Get the Gamma host URL, optionally routing through proxy."""
    proxy_url = get_proxy_url()
    if proxy and proxy_url:
        return f"{proxy_url.rstrip('/')}/gamma"
    return GAMMA_HOST


class Gamma:
    """Client for the Polymarket Gamma API (market metadata)."""

    def __init__(self, host: str | None = None, *, proxy: bool = False) -> None:
        self.host = host or get_gamma_host(proxy)

    def events(
        self,
        limit: int = 10,
        offset: int = 0,
        closed: bool = False,
        order: str = "id",
        ascending: bool = False,
        end_date_min: str | None = None,
        end_date_max: str | None = None,
    ) -> list[dict]:
        """Fetch events with their markets.

        Returns raw dict data including nested markets with clobTokenIds.

        Args:
            limit: Maximum number of events to return
            offset: Pagination offset
            closed: Include closed events
            order: Field to order by
            ascending: Sort order
            end_date_min: ISO 8601 datetime string for minimum end date
            end_date_max: ISO 8601 datetime string for maximum end date
        """
        params = {
            "order": order,
            "ascending": str(ascending).lower(),
            "closed": str(closed).lower(),
            "limit": limit,
            "offset": offset,
        }
        if end_date_min:
            params["end_date_min"] = end_date_min
        if end_date_max:
            params["end_date_max"] = end_date_max

        response = requests.get(f"{self.host}/events", params=params, timeout=10)
        response.raise_for_status()
        return response.json()

    def event_by_slug(self, slug: str) -> Event:
        response = requests.get(f"{self.host}/events/slug/{slug}", timeout=10)
        response.raise_for_status()
        e = response.json()

        liquidity = e.get("liquidity")
        volume = e.get("volume")
        return Event(
            title=e.get("title", "Unknown"),
            slug=e.get("slug", "N/A"),
            end_date=e.get("endDate"),
            liquidity=float(liquidity) if liquidity else None,
            volume=float(volume) if volume else None,
        )

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

    def series(
        self,
        limit: int = 100,
        offset: int = 0,
        closed: bool = False,
        active: bool = True,
    ) -> list[dict]:
        """Fetch series markets.

        Series contain recurring markets like sports games, crypto prices, etc.
        Each series can contain multiple events and markets.

        Args:
            limit: Maximum number of series to return
            offset: Offset for pagination
            closed: Include closed series
            active: Include only active series

        Returns:
            List of series data with nested events and markets
        """
        params: dict[str, str | int] = {
            "limit": limit,
            "offset": offset,
        }
        if not closed:
            params["closed"] = "false"
        if active:
            params["active"] = "true"

        response = requests.get(f"{self.host}/series", params=params, timeout=10)
        response.raise_for_status()
        return response.json()
