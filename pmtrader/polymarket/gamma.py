"""Gamma API client for market discovery."""

from __future__ import annotations

import requests

from . import hosts

REQUEST_TIMEOUT = 30


class Gamma:
    """Read-only client for the Polymarket Gamma API."""

    def __init__(self, *, via_proxy: bool = False) -> None:
        self.host = hosts.gamma_host(via_proxy)

    def events(
        self,
        *,
        limit: int = 10,
        offset: int = 0,
        closed: bool = False,
        order: str = "id",
        ascending: bool = False,
        end_date_min: str | None = None,
        end_date_max: str | None = None,
    ) -> list[dict]:
        """Events with embedded markets (clobTokenIds, outcomes, prices).

        Used by the expiring-markets scanner and the consistency tests.
        """
        params: dict = {
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

        r = requests.get(f"{self.host}/events", params=params, headers=hosts.UA, timeout=REQUEST_TIMEOUT)
        r.raise_for_status()
        return r.json()

    def markets(self, *, limit: int = 10, closed: bool = False) -> list[dict]:
        params = {"closed": str(closed).lower(), "limit": limit}
        r = requests.get(f"{self.host}/markets", params=params, headers=hosts.UA, timeout=REQUEST_TIMEOUT)
        r.raise_for_status()
        return r.json()
