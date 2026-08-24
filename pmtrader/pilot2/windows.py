"""Window discovery — which slug is trading now, and its two token ids.

Windows sit on a fixed grid, so the slug of the window a series is trading
right now is COMPUTED (`series.current_slug`) and gamma is only ever asked to
confirm it and hand back the clobTokenIds. That keeps discovery to one request
per window per series — 12/hour for a 5m series — instead of a scan.

THE `closed` FLAG. Gamma's `/markets?slug=X` defaults to `closed=false` and
answers `[]` for every SETTLED window, which parses as "not resolved yet" and
rides forever. That default is exactly how three resolved-lost windows hid
-$272.35 from the ledger for 13-25h on 2026-08-23. Discovery wants open markets
and leaves the default alone; RESOLUTION must pin `closed=true`, and
`resolution()` below does.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass

from polymarket import hosts
from polymarket.outcomes import gamma_resolution

from . import series as series_mod

REQUEST_TIMEOUT_S = 8.0

# A discovered window is immutable for its whole life (its token ids and bounds
# cannot change), so it is cached until it is swept.
_SWEEP_AFTER_S = 3600.0


@dataclass(frozen=True)
class Window:
    slug: str
    series: str
    symbol: str          # rtds symbol, "doge/usd"
    start: float
    end: float
    dur_s: int
    token_up: str
    token_down: str
    fee_rate: float

    def elapsed_frac(self, now: float) -> float:
        return max(0.0, min(1.0, (now - self.start) / max(self.dur_s, 1)))


def _get(session, url: str, params: dict) -> object | None:
    try:
        r = session.get(url, params=params, headers=hosts.UA, timeout=REQUEST_TIMEOUT_S)
        r.raise_for_status()
        return r.json()
    except Exception:  # noqa: BLE001 — a gamma miss is "no window yet", not a crash
        return None


def parse_market(slug: str, markets: object) -> Window | None:
    """A gamma `/markets?slug=` response -> Window, or None.

    The bounds come from the SLUG, not from the payload's dates: the slug's
    trailing epoch is the grid the market actually settles on, and it is
    available offline, which is what makes the pilot's window arithmetic
    testable without a network.
    """
    if not isinstance(markets, list) or not markets:
        return None
    m = markets[0]
    if not isinstance(m, dict):
        return None
    bounds = series_mod.window_bounds(slug)
    if bounds is None:
        return None
    try:
        outcomes = json.loads(m.get("outcomes") or "[]")
        tokens = json.loads(m.get("clobTokenIds") or "[]")
    except (TypeError, ValueError):
        return None
    by_outcome = {str(o).lower(): t for o, t in zip(outcomes, tokens)}
    up, down = by_outcome.get("up"), by_outcome.get("down")
    if not up or not down:
        return None
    ser = slug.rsplit("-", 1)[0]
    sym = series_mod.symbol_of(ser)
    if sym is None:
        return None
    start, end = bounds
    return Window(
        slug=slug, series=ser, symbol=sym, start=start, end=end,
        dur_s=int(end - start), token_up=str(up), token_down=str(down),
        fee_rate=float((m.get("feeSchedule") or {}).get("rate") or 0.0),
    )


class WindowCache:
    """One gamma lookup per window per series, then never again."""

    def __init__(self, session=None) -> None:
        if session is None:
            import requests
            session = requests.Session()
        self.session = session
        self._by_slug: dict[str, Window | None] = {}
        self._missed_at: dict[str, float] = {}
        self.lookups = 0

    def current(self, ser: str, now: float | None = None) -> Window | None:
        """The window `ser` is trading at `now`, or None if gamma has not
        published it yet (a fresh window can lag its own grid slot by seconds).
        A miss is retried, but not on every poll — that would turn one absent
        market into a sustained request storm against gamma."""
        now = time.time() if now is None else now
        slug = series_mod.current_slug(ser, now)
        if slug is None:
            return None
        if slug in self._by_slug:
            return self._by_slug[slug]
        last_miss = self._missed_at.get(slug, 0.0)
        if now - last_miss < 15.0:
            return None
        self.lookups += 1
        w = parse_market(slug, _get(self.session, f"{hosts.GAMMA}/markets", {"slug": slug}))
        if w is None:
            self._missed_at[slug] = now
            return None
        self._by_slug[slug] = w
        return w

    def sweep(self, now: float | None = None) -> None:
        """Forget windows whose settlement grace has long passed."""
        now = time.time() if now is None else now
        for slug in [s for s, w in self._by_slug.items()
                     if w is not None and now > w.end + _SWEEP_AFTER_S]:
            self._by_slug.pop(slug, None)
            self._missed_at.pop(slug, None)


def resolution(slug: str, session=None) -> dict:
    """{'resolved': bool, 'winner': 'up'|'down'|None} for a settled window.

    `closed=true` is PINNED and load-bearing — see the module docstring. A
    window that genuinely has not settled still answers `[]` here, which is
    the honest "still riding".
    """
    if session is None:
        import requests
        session = requests.Session()
    payload = _get(session, f"{hosts.GAMMA}/markets", {"slug": slug, "closed": "true"})
    if payload is None:
        return {"resolved": False, "winner": None, "reachable": False}
    out = gamma_resolution(payload if isinstance(payload, list) else [])
    return {**out, "reachable": True}
