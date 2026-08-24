"""What a node says about itself, every 30 seconds.

The heartbeat is OBSERVABILITY, not authority. Nothing in the lease protocol
consults it — a node's right to trade comes from its lease and nothing else,
which is exactly what makes the protocol partition-safe. The heartbeat exists
so a human (and the checker) can see why the fleet is in the shape it is in.

That separation is worth guarding jealously. The tempting bug is to make a
failover claim conditional on "the home node's heartbeat is stale", which reads
like a safety improvement and is the opposite: it re-imports the liveness
judgement the lease design exists to eliminate, and it fails in precisely the
network-partition case where both nodes see a stale peer.

The clean-shutdown stamp
------------------------
Copied wholesale from the mubs worker (`bin/mubs-worker::shutdown_heartbeat`),
because Hunter powers this box off every night on purpose and a fleet that
pages him for it is a fleet he will mute. On SIGTERM the daemon writes one last
heartbeat carrying `shutdown`, and `put_heartbeat` replaces the row whole, so
the stamp self-clears with the first live beat after the next boot. A wedge
leaves no stamp and still pages.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

from polymarket.updown_slugs import dur_label, parse_updown_slug

# Below this, a node cannot meaningfully take over anyone's series — it may
# still be perfectly healthy on its own book, but it is not a candidate for
# more. Reported, never enforced: `balance_ok` is a fact on the status board,
# and a human decides what it means.
DEFAULT_MIN_BALANCE_USDC = 25.0

# A feed this stale means the engine is gated anyway (the engine's own
# MAX_SPOT_AGE_S is 5s); 30s is the "something is actually wrong" line rather
# than the "a packet was late" line.
DEFAULT_MAX_FEED_AGE_S = 30.0


def series_of(slug: str) -> str | None:
    """`btc-updown-15m-1787543100` -> `btc-updown-15m`.

    Rebuilt from the parsed parts rather than rsplit, so it inherits
    updown_slugs' one duration formatter. An `rsplit("-", 1)` here would agree
    with it on every slug that exists today and disagree the first time a
    duration ever gains a suffix.
    """
    w = parse_updown_slug(slug)
    if w is None:
        return None
    return f"{w['symbol']}-updown-{dur_label(w['dur_s'])}"


def series_held(paths: list[str | Path]) -> list[str]:
    """The distinct series this node currently has armed, from arms-state.json.

    Reads more than one path because `updown2` keeps its own state file — a
    deliberate split upstream (each strategy's `save` writes a whole snapshot
    and deletes on empty, so a shared file would have them erase each other),
    which means a node's true holdings are the union.

    An absent file is not an error: the engine DELETES arms-state.json when the
    last arm goes away, so "no file" means "no arms" and must read as empty
    rather than as a failure to observe.
    """
    out: list[str] = []
    for path in paths:
        p = Path(path).expanduser()
        try:
            raw = json.loads(p.read_text())
        except FileNotFoundError:
            continue
        except (OSError, json.JSONDecodeError):
            # A torn or unreadable state file is a real signal, but it is not
            # this function's to raise: report what we could see and let the
            # caller's `engine_active` carry the alarm.
            continue
        for arm in raw.get("arms") or []:
            s = series_of(str(arm.get("slug", "")))
            if s and s not in out:
                out.append(s)
        for roll in raw.get("rolls") or []:
            s = series_of(str((roll.get("params") or {}).get("slug", "")))
            if s and s not in out:
                out.append(s)
    return sorted(out)


def engine_snapshot(fetch=None) -> dict[str, Any] | None:
    """The engine's `/status`, or None if it is not answering.

    Uses the best-effort `fetch` rather than the strict `get`: an unreachable
    engine is a fact to report every 30 seconds, not a reason for the daemon
    to exit.
    """
    if fetch is None:
        from engine import fetch as _fetch

        fetch = _fetch
    try:
        return fetch("/status")
    except Exception:  # noqa: BLE001 — the daemon must outlive any client bug
        return None


def build(
    node: str,
    *,
    arms_paths: list[str | Path],
    status: dict[str, Any] | None,
    now: float | None = None,
    min_balance: float = DEFAULT_MIN_BALANCE_USDC,
    shutdown: bool = False,
) -> dict[str, Any]:
    """The heartbeat item. Pure — `status` and the files are read by the caller."""
    now = time.time() if now is None else now
    held = series_held(arms_paths)

    if status is None:
        engine_active = False
        balance = None
        feed_age = None
        halted = None
        ticks = None
    else:
        halted = bool(status.get("halted", False))
        engine_active = not halted
        try:
            balance = float(status.get("balance_usdc"))
        except (TypeError, ValueError):
            balance = None
        # A disconnected socket has no meaningful last-event age; report the
        # absence rather than a stale small number that reads as healthy.
        if status.get("ws_connected"):
            ms = status.get("ws_last_event_age_ms")
            feed_age = None if ms is None else float(ms) / 1000.0
        else:
            feed_age = None
        ticks = status.get("tick_count")

    hb: dict[str, Any] = {
        "node": node,
        "ts": float(now),
        "engine_active": bool(engine_active),
        "series_held": held,
        "balance_ok": bool(balance is not None and balance >= min_balance),
        # `feed_age` is the brief's name; -1 is the wire form of "unknown",
        # because the item is typed and None would need a NULL the readers
        # would each have to remember to handle.
        "feed_age": -1.0 if feed_age is None else float(feed_age),
    }
    if balance is not None:
        hb["balance_usdc"] = float(balance)
    if halted is not None:
        hb["halted"] = bool(halted)
    if ticks is not None:
        hb["tick_count"] = int(ticks)
    if shutdown:
        hb["shutdown"] = float(now)
    return hb


def is_stale(hb: dict[str, Any], *, now: float, stale_after_s: float) -> bool:
    try:
        return (now - float(hb.get("ts", 0))) > stale_after_s
    except (TypeError, ValueError):
        return True


def shutdown_clean(hb: dict[str, Any] | None) -> bool:
    """Whether this node's newest heartbeat says it was stopped on purpose.

    A missing heartbeat reads as NOT clean — page rather than silently skip a
    real wedge. That asymmetry is the whole lesson of the 2026-08-22 mubs page
    and its inverse: quiet on an intentional off, loud on an absence.
    """
    return bool(hb and hb.get("shutdown"))
