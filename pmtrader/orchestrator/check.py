"""`--check`: what the fleet looks like right now, and whether it needs a human.

The exit code answers exactly one question — **does something need attention?**
— and not the question it would be natural to answer instead, "is everything
running?". Those differ on the case this fleet hits every single night: Hunter
powers the desktop off on purpose. A checker that goes red on that is a checker
whose red means nothing by the end of the week.

    0  nothing needs attention (a cleanly-shut-down node is nothing)
    1  something needs attention — a wedged node, a lease that expired
       un-released, a series nobody holds
    2  the assignment map is refused (config error; matches pilot2's exit 2 so
       `RestartPreventExitStatus=2` keeps it visibly stopped)
    3  the store is unreachable — this says nothing about the engines, only
       that the doctor is blind, and it is deliberately NOT the same code as a
       sick fleet

`--strict` folds "cleanly off" back into 1, for the times you want to ask the
other question.
"""

from __future__ import annotations

import time
from typing import Any

from . import heartbeat as hb_mod
from .assign import FleetMap
from .lease import Lease, claimable_at, fence_deadline
from .store import StoreUnavailable

OK, ATTENTION, REFUSED, BLIND = 0, 1, 2, 3

# A node beats every 30s. Three missed beats is a node that is not merely busy.
DEFAULT_STALE_AFTER_S = 120.0


def _age(then: float | None, now: float) -> str:
    if then is None:
        return "never"
    d = max(0.0, now - float(then))
    if d < 90:
        return f"{d:.0f}s"
    if d < 5400:
        return f"{d / 60:.0f}m"
    return f"{d / 3600:.1f}h"


def gather(store, fmap: FleetMap, *, now: float | None = None) -> dict[str, Any]:
    """One read of the whole fleet. Raises StoreUnavailable if it cannot."""
    now = time.time() if now is None else now
    beats = {h.get("node"): h for h in store.all_heartbeats()}
    leases = {lease.series: lease for lease in store.all_leases()}
    return {
        "now": now,
        "heartbeats": beats,
        "leases": leases,
        "failover_disabled": store.failover_disabled(),
        "clock_offset_s": getattr(store, "clock_offset_s", None),
    }


def assess(
    snap: dict[str, Any],
    fmap: FleetMap,
    *,
    stale_after_s: float = DEFAULT_STALE_AFTER_S,
    strict: bool = False,
) -> dict[str, Any]:
    """Turn a snapshot into findings. Pure — every test drives this directly."""
    now = snap["now"]
    beats: dict[str, dict] = snap["heartbeats"]
    leases: dict[str, Lease] = snap["leases"]

    nodes = []
    attention: list[str] = []
    notes: list[str] = []

    for name in sorted(fmap.nodes):
        hb = beats.get(name)
        stale = hb is None or hb_mod.is_stale(hb, now=now, stale_after_s=stale_after_s)
        clean = hb_mod.shutdown_clean(hb)
        if not fmap.node_active(name):
            # Declared but not yet brought up. Shown, never alarming.
            nodes.append({
                "node": name, "state": "not-deployed", "last_beat_s": None,
                "engine_active": False, "balance_ok": False, "feed_age": None,
                "series_held": list(hb.get("series_held") or []) if hb else [],
            })
            notes.append(f"{name}: doctor not deployed yet (doctor_active=false in the map)")
            continue
        if stale and clean:
            state = "off-clean"
            notes.append(f"{name} shut down cleanly {_age(hb.get('shutdown'), now)} ago")
            if strict:
                attention.append(f"{name} is off (cleanly) — --strict")
        elif stale:
            state = "STALE"
            # A node that never beat at all is a different problem from one
            # that stopped: say which, because the fixes are unrelated.
            seen = "no heartbeat ever" if hb is None else f"last beat {_age(hb.get('ts'), now)} ago"
            attention.append(f"{name} is dark with no clean-shutdown marker ({seen})")
        elif not hb.get("engine_active"):
            state = "halted"
            attention.append(f"{name} is beating but its engine is halted")
        else:
            state = "up"
        nodes.append(
            {
                "node": name,
                "state": state,
                "last_beat_s": None if hb is None else now - float(hb.get("ts", 0)),
                "engine_active": bool(hb and hb.get("engine_active")),
                "balance_ok": bool(hb and hb.get("balance_ok")),
                "feed_age": None if hb is None else hb.get("feed_age"),
                "series_held": list(hb.get("series_held") or []) if hb else [],
            }
        )

    series = []
    for key in sorted(fmap.series):
        a = fmap.series[key]
        lease = leases.get(key)
        if lease is None:
            state = "unheld" if fmap.leases_active else "n/a"
            holder = "-"
            detail = "no lease item" if fmap.leases_active else "leases not active yet"
            if fmap.leases_active:
                attention.append(f"{key}: nobody holds it (home {a.home} has not acquired)")
        else:
            holder = lease.holder
            fence = fence_deadline(lease)
            if lease.released:
                state = "released"
                detail = f"released by {holder}, claimable now"
                notes.append(f"{key}: {holder} released it cleanly")
            elif now < lease.expires_at:
                state = "held"
                detail = f"renews ok, {lease.expires_at - now:.0f}s left"
            elif now < fence:
                state = "extending"
                detail = (
                    f"expired {_age(lease.expires_at, now)} ago, riding its "
                    f"{lease.home_extension_s:.0f}s home extension "
                    f"({fence - now:.0f}s left)"
                )
                attention.append(
                    f"{key}: {holder} has not renewed for {_age(lease.expires_at, now)} "
                    "— it is on its store-outage extension"
                )
            else:
                state = "LAPSED"
                ready = claimable_at(lease)
                detail = (
                    f"past fence by {_age(fence, now)}; claimable "
                    + ("now" if now >= ready else f"in {ready - now:.0f}s")
                )
                attention.append(f"{key}: {holder} lapsed un-released — nobody is trading it")
            # A holder the map does not recognise is a live wash-trade risk,
            # not a cosmetic mismatch: it means a box is quoting a series the
            # partition never gave it.
            if not a.covers(holder):
                attention.append(
                    f"{key}: held by {holder}, which the map lists as neither home "
                    f"({a.home}) nor failover ({a.failover})"
                )
            if holder != a.home and state in ("held", "extending"):
                notes.append(f"{key}: FAILED OVER to {holder} (home is {a.home})")

        series.append(
            {
                "series": key, "home": a.home, "failover": a.failover,
                "holder": holder, "state": state, "detail": detail,
                "grace_s": a.effective_grace_s(),
            }
        )

    # Held-but-not-armed and armed-but-not-held. The first is benign (a lease
    # taken a moment before the arm). The second is the one that matters: a
    # node trading a series it does not hold is the exact thing this system
    # exists to make impossible, so if it is ever observed, say so loudly.
    #
    # Only once leases ARE the authority. Through phase 1 every armed series is
    # legitimately lease-less, and shouting about all eight of them would train
    # exactly the reflex this checker needs not to train.
    if fmap.leases_active:
        for n in nodes:
            for s in n["series_held"]:
                lease = leases.get(s)
                if s in fmap.series and (lease is None or lease.holder != n["node"]):
                    attention.append(
                        f"{n['node']} has {s} ARMED but does not hold its lease "
                        f"(holder: {lease.holder if lease else 'nobody'})"
                    )
    else:
        notes.append(
            "leases are NOT the authority yet (leases_active=false) — series rows are "
            "informational and the engines run on their static PMENGINE_SERIES_ALLOWLIST"
        )

    if snap.get("failover_disabled"):
        notes.append("failover is DISABLED fleet-wide (kill switch is on)")

    return {
        "now": now,
        "nodes": nodes,
        "series": series,
        "attention": attention,
        "notes": notes,
        "failover_disabled": bool(snap.get("failover_disabled")),
        "rc": ATTENTION if attention else OK,
    }


def render(report: dict[str, Any]) -> str:
    now = report["now"]
    lines = [f"pmt fleet — {time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime(now))}", ""]

    lines.append("nodes")
    for n in report["nodes"]:
        beat = "never" if n["last_beat_s"] is None else f"{n['last_beat_s']:.0f}s ago"
        feed = n["feed_age"]
        feed_s = "?" if feed is None or feed < 0 else f"{feed:.1f}s"
        held = ",".join(n["series_held"]) or "-"
        lines.append(
            f"  {n['node']:<10} {n['state']:<9} beat {beat:<10} "
            f"feed {feed_s:<7} balance {'ok' if n['balance_ok'] else 'LOW':<4} armed: {held}"
        )

    lines.append("")
    lines.append("series")
    for s in report["series"]:
        via = "" if s["holder"] == s["home"] else f"  (home {s['home']})"
        lines.append(
            f"  {s['series']:<20} {s['state']:<10} {s['holder']:<10}{via}  {s['detail']}"
        )

    if report["notes"]:
        lines.append("")
        lines.append("notes")
        lines.extend(f"  - {n}" for n in report["notes"])

    lines.append("")
    if report["attention"]:
        lines.append("NEEDS ATTENTION")
        lines.extend(f"  ! {a}" for a in report["attention"])
    else:
        lines.append("all good")
    return "\n".join(lines)


def run(
    store,
    fmap: FleetMap,
    *,
    stale_after_s: float = DEFAULT_STALE_AFTER_S,
    strict: bool = False,
    now: float | None = None,
) -> tuple[int, dict[str, Any] | None, str]:
    try:
        snap = gather(store, fmap, now=now)
    except StoreUnavailable as e:
        return BLIND, None, f"pmt fleet — STORE UNREACHABLE: {e}"
    report = assess(snap, fmap, stale_after_s=stale_after_s, strict=strict)
    return report["rc"], report, render(report)
