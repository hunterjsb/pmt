"""Gamma API client for market metadata."""

from __future__ import annotations

import requests

from .models import Event

GAMMA_HOST = "https://gamma-api.polymarket.com"


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
    ) -> list[Event]:
        params = {
            "order": order,
            "ascending": str(ascending).lower(),
            "closed": str(closed).lower(),
            "limit": limit,
        }
        response = requests.get(f"{self.host}/events", params=params, timeout=10)
        response.raise_for_status()
        data = response.json()

        events = []
        for e in data:
            liquidity = e.get("liquidity")
            volume = e.get("volume")
            events.append(
                Event(
                    title=e.get("title", "Unknown"),
                    slug=e.get("slug", "N/A"),
                    end_date=e.get("endDate"),
                    liquidity=float(liquidity) if liquidity else None,
                    volume=float(volume) if volume else None,
                )
            )

        return events

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
