"""CLOB book polling — REST, on a leash.

This is a pilot, not an HFT engine. `analysis/watch_load.md` measured what
happens when a hot loop treats an upstream as free: 16 sequential 30 KB fetches
inline in the engine's tick arm went dark on the control plane for whole
seconds under contention. The discipline here is the opposite of that:

  * ONE `requests.Session` for the process, so ~7 req/s of small GETs reuse one
    TLS connection instead of handshaking 7 times a second.
  * A short, hard timeout. A book we could not read in 3s is a book that is
    already stale for a 5m window.
  * Failures degrade to "no quote", never to an exception. The de-vig already
    returns nan on a one-sided book, and an unreachable book is the same
    thing: no market opinion, model stands alone, no fill possible.
  * The poll interval is a constant, not a knob that can be turned down.

`polymarket.clob.get_order_book_depth` returns the same ladder but opens a
fresh connection per call and drops the best level's SIZE into a total-depth
sum. The EV replay caps every fill at the quoted ask size, so the size AT the
top level is load-bearing — hence this fetcher rather than that one.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from polymarket import hosts

POLL_INTERVAL_S = 2.0
REQUEST_TIMEOUT_S = 3.0


@dataclass(frozen=True)
class Top:
    """Top of book. `nan` where the side is unquoted — never a substituted
    value, so `devig` can tell "no market" from "a market at zero"."""

    bid: float
    bid_size: float
    ask: float
    ask_size: float

    @property
    def quoted(self) -> bool:
        return math.isfinite(self.ask)


EMPTY = Top(float("nan"), 0.0, float("nan"), 0.0)


def parse_top(payload: object) -> Top:
    """Best bid/ask and the size AT that price, from a `/book` response.

    Aggregates every level quoted at the best price rather than taking the
    first entry: the CLOB can list one price across several maker rows, and a
    fill cap read off one of them understates what was really on offer.
    """
    if not isinstance(payload, dict):
        return EMPTY
    def _levels(key: str) -> list[tuple[float, float]]:
        out = []
        for x in payload.get(key) or []:
            try:
                p, s = float(x["price"]), float(x["size"])
            except (TypeError, ValueError, KeyError):
                continue
            if math.isfinite(p) and math.isfinite(s) and s > 0.0:
                out.append((p, s))
        return out

    bids, asks = _levels("bids"), _levels("asks")
    bid = max((p for p, _ in bids), default=float("nan"))
    ask = min((p for p, _ in asks), default=float("nan"))
    bid_sz = math.fsum(s for p, s in bids if p == bid) if math.isfinite(bid) else 0.0
    ask_sz = math.fsum(s for p, s in asks if p == ask) if math.isfinite(ask) else 0.0
    return Top(bid=bid, bid_size=bid_sz, ask=ask, ask_size=ask_sz)


class BookPoller:
    """Session-reusing top-of-book reader."""

    def __init__(self, session=None, host: str | None = None,
                 timeout: float = REQUEST_TIMEOUT_S) -> None:
        if session is None:
            import requests
            session = requests.Session()
        self.session = session
        self.host = (host or hosts.CLOB).rstrip("/")
        self.timeout = timeout
        self.requests_made = 0
        self.failures = 0

    def top(self, token: str) -> Top:
        """Top of book for one token. Never raises."""
        if not token:
            return EMPTY
        self.requests_made += 1
        try:
            r = self.session.get(f"{self.host}/book", params={"token_id": token},
                                 headers=hosts.UA, timeout=self.timeout)
            r.raise_for_status()
            return parse_top(r.json())
        except Exception:  # noqa: BLE001 — an unreadable book is "no quote"
            self.failures += 1
            return EMPTY
