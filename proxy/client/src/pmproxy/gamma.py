"""Gamma API methods."""

from dataclasses import dataclass
from typing import Any, Optional

from .base import BaseClient


@dataclass
class GammaToken:
    token_id: str
    outcome: str
    price: float


@dataclass
class GammaMarket:
    id: str
    question: str
    condition_id: str
    slug: str
    volume: float
    liquidity: float
    active: bool
    closed: bool
    tokens: list[GammaToken]


@dataclass
class Event:
    id: str
    slug: str
    title: str
    description: str
    end_date: str
    liquidity: float
    volume: float
    closed: bool
    markets: list[GammaMarket]


@dataclass
class Tag:
    id: str
    label: str
    slug: str


class GammaMixin:
    """Gamma API methods mixin."""

    def events(
        self,
        *,
        limit: Optional[int] = None,
        offset: Optional[int] = None,
        closed: Optional[bool] = None,
        order: Optional[str] = None,
        ascending: Optional[bool] = None,
        end_date_min: Optional[str] = None,
        end_date_max: Optional[str] = None,
        proxy: Optional[bool] = None,
    ) -> tuple[list[Event], list[dict[str, Any]]]:
        """
        List events with pagination and filtering.

        Args:
            limit: Max number of events to return
            offset: Number of events to skip
            closed: Filter by closed status
            order: Field to order by
            ascending: Sort direction
            end_date_min: Minimum end date (ISO format)
            end_date_max: Maximum end date (ISO format)
            proxy: Override instance proxy setting

        Returns:
            Tuple of (parsed events, raw JSON list)
        """
        self: BaseClient
        params: dict[str, Any] = {}
        if limit is not None:
            params["limit"] = limit
        if offset is not None:
            params["offset"] = offset
        if closed is not None:
            params["closed"] = str(closed).lower()
        if order is not None:
            params["order"] = order
        if ascending is not None:
            params["ascending"] = str(ascending).lower()
        if end_date_min is not None:
            params["end_date_min"] = end_date_min
        if end_date_max is not None:
            params["end_date_max"] = end_date_max

        resp = self._get("gamma", "events", params=params, proxy=proxy)
        data = resp.json()
        # Gamma returns a list directly
        events_list = data if isinstance(data, list) else data.get("data", [])
        events = [_parse_event(e) for e in events_list]
        return events, events_list

    def event_by_slug(
        self, slug: str, *, proxy: Optional[bool] = None
    ) -> tuple[Optional[Event], dict[str, Any]]:
        """
        Get event by slug.

        Args:
            slug: Event slug
            proxy: Override instance proxy setting

        Returns:
            Tuple of (parsed event or None, raw JSON)
        """
        self: BaseClient
        resp = self._get("gamma", f"events/{slug}", proxy=proxy)
        data = resp.json()
        event = _parse_event(data) if data else None
        return event, data

    def markets(
        self,
        *,
        limit: Optional[int] = None,
        offset: Optional[int] = None,
        proxy: Optional[bool] = None,
    ) -> tuple[list[GammaMarket], list[dict[str, Any]]]:
        """
        List markets.

        Args:
            limit: Max number of markets to return
            offset: Number of markets to skip
            proxy: Override instance proxy setting

        Returns:
            Tuple of (parsed markets, raw JSON list)
        """
        self: BaseClient
        params: dict[str, Any] = {}
        if limit is not None:
            params["limit"] = limit
        if offset is not None:
            params["offset"] = offset

        resp = self._get("gamma", "markets", params=params, proxy=proxy)
        data = resp.json()
        markets_list = data if isinstance(data, list) else data.get("data", [])
        markets = [_parse_gamma_market(m) for m in markets_list]
        return markets, markets_list

    def market_by_slug(
        self, slug: str, *, proxy: Optional[bool] = None
    ) -> tuple[Optional[GammaMarket], dict[str, Any]]:
        """
        Get market by slug.

        Args:
            slug: Market slug
            proxy: Override instance proxy setting

        Returns:
            Tuple of (parsed market or None, raw JSON)
        """
        self: BaseClient
        resp = self._get("gamma", f"markets/{slug}", proxy=proxy)
        data = resp.json()
        market = _parse_gamma_market(data) if data else None
        return market, data

    def tags(self, *, proxy: Optional[bool] = None) -> list[Tag]:
        """
        Get all tags.

        Args:
            proxy: Override instance proxy setting
        """
        self: BaseClient
        resp = self._get("gamma", "tags", proxy=proxy)
        data = resp.json()
        tags_list = data if isinstance(data, list) else data.get("data", [])
        return [Tag(id=t.get("id", ""), label=t.get("label", ""), slug=t.get("slug", "")) for t in tags_list]

    def events_by_tag(
        self, tag_slug: str, *, proxy: Optional[bool] = None
    ) -> tuple[list[Event], list[dict[str, Any]]]:
        """
        Get events by tag.

        Args:
            tag_slug: Tag slug to filter by
            proxy: Override instance proxy setting

        Returns:
            Tuple of (parsed events, raw JSON list)
        """
        self: BaseClient
        resp = self._get("gamma", "events", params={"tag_slug": tag_slug}, proxy=proxy)
        data = resp.json()
        events_list = data if isinstance(data, list) else data.get("data", [])
        events = [_parse_event(e) for e in events_list]
        return events, events_list

    def search(
        self, query: str, *, proxy: Optional[bool] = None
    ) -> tuple[list[GammaMarket], list[dict[str, Any]]]:
        """
        Search markets.

        Args:
            query: Search query
            proxy: Override instance proxy setting

        Returns:
            Tuple of (parsed markets, raw JSON list)
        """
        self: BaseClient
        resp = self._get("gamma", "markets", params={"_q": query}, proxy=proxy)
        data = resp.json()
        markets_list = data if isinstance(data, list) else data.get("data", [])
        markets = [_parse_gamma_market(m) for m in markets_list]
        return markets, markets_list


def _parse_gamma_token(t: dict) -> GammaToken:
    """Parse a Gamma token from JSON."""
    return GammaToken(
        token_id=t.get("token_id", ""),
        outcome=t.get("outcome", ""),
        price=float(t.get("price", 0)),
    )


def _parse_gamma_market(m: dict) -> GammaMarket:
    """Parse a Gamma market from JSON."""
    tokens = []
    for t in m.get("tokens", []):
        tokens.append(_parse_gamma_token(t))

    return GammaMarket(
        id=m.get("id", ""),
        question=m.get("question", ""),
        condition_id=m.get("conditionId", m.get("condition_id", "")),
        slug=m.get("slug", ""),
        volume=float(m.get("volume", 0)),
        liquidity=float(m.get("liquidity", 0)),
        active=m.get("active", False),
        closed=m.get("closed", False),
        tokens=tokens,
    )


def _parse_event(e: dict) -> Event:
    """Parse an event from JSON."""
    markets = []
    for m in e.get("markets", []):
        markets.append(_parse_gamma_market(m))

    return Event(
        id=e.get("id", ""),
        slug=e.get("slug", ""),
        title=e.get("title", ""),
        description=e.get("description", ""),
        end_date=e.get("endDate", e.get("end_date", "")),
        liquidity=float(e.get("liquidity", 0)),
        volume=float(e.get("volume", 0)),
        closed=e.get("closed", False),
        markets=markets,
    )
