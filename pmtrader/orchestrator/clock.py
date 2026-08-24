"""This node's clock, measured against the store's.

Every deadline in the lease protocol is written on one node's clock and
enforced on another's, so a silent clock fault is the one input that could
make two provably-disjoint intervals overlap in real time. Rather than assume
NTP is working, measure it: every DynamoDB response carries an HTTP `Date`
header, so each round trip is a free sample of the store's clock. The store is
the natural reference — it is the one clock both nodes provably share a view
of, and it is the clock the lease item's timestamps are effectively minted
against.

A node past `lease.MAX_SKEW_S` refuses to acquire and fences itself out of any
lease it holds (`lease.should_fence`). That is a hard stop, not a warning: a
node that cannot place itself in time cannot promise to stop on time, and the
promise is the entire product.

Resolution caveat, stated because it bounds what this can detect: HTTP `Date`
is whole seconds, so a single sample carries up to ~1s of quantisation plus
the round-trip. That is why the bound is 5s and not 500ms — the guard is sized
to catch a clock that is WRONG (a drifted VM, a bad RTC, a chrony that never
stepped), not to discipline one that is merely imprecise.
"""

from __future__ import annotations

import email.utils
import time
from typing import Any


def parse_http_date(value: str) -> float | None:
    """RFC 7231 date -> epoch seconds, or None if it will not parse."""
    try:
        dt = email.utils.parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    return None if dt is None else dt.timestamp()


def offset_from_response(response: dict[str, Any], now: float | None = None) -> float | None:
    """local_clock - store_clock, in seconds. Positive means we are ahead.

    Returns None when the response carries no usable Date, which the caller
    must treat as "unmeasured" — and `lease.skew_ok(None)` is False, so an
    unmeasured clock fails closed rather than defaulting to zero.
    """
    meta = response.get("ResponseMetadata") or {}
    headers = meta.get("HTTPHeaders") or {}
    raw = headers.get("date") or headers.get("Date")
    if not raw:
        return None
    server = parse_http_date(raw)
    if server is None:
        return None
    return (time.time() if now is None else now) - server
