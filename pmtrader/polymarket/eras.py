"""Policy ERAS — when the fleet's strategy actually changed, and why.

The all-time record on `pmt crypto stats` is the ledger of record and stays
that way. But it sums windows traded under policies that no longer exist:
the pre-brake blowups, the xrp basis losses, a book read off a 2s REST
poller. "Are we winning NOW" and "what has this book ever done" are two
questions, and one number cannot answer both. This registry is the second
answer, and it is a registry rather than a `--since` flag because a floor
the operator picks after seeing the score is not evidence of anything.

Each entry pins a DEPLOY MOMENT — the commit, or the deploy the repo
records — that changed what the engine does, with the citation for it in a
comment beside the row. Four rules keep that honest:

  * **Exhaustive.** The first era opens at epoch 0 and each era runs to the
    next one's start, so every window ever traded lands in exactly ONE era.
    Nothing can be dropped by omission and `for_start()` never returns None.
  * **No era is ever hidden.** The by-era table prints every row — the
    winners and the disasters — including eras with nothing in them.
    `--era` scopes the REST of the report, never this table.
  * **All-time stays the headline.** An era is context for the ledger, not
    a replacement for it.
  * **A boundary is a fleet DEPLOY, not a per-window claim.** `stream` means
    the engine could read the settlement stream from that moment on, not
    that any given window did — the by-symbol table's feed column answers
    that, per arm, where it belongs.

Boundaries are only ever appended, and only with a repo-citable moment: git
log, ROADMAP.md, analysis/*.md, or the `era` tags on the characterization
fixtures. A policy change that cannot be dated from the repo's own record
does not get a boundary, because a boundary nobody can check is a way to
move the line after seeing the score.

Names follow the fixture era vocabulary (pmengine/fixtures/README.md, the
`era` key in each fixture) wherever the two overlap.
"""

from __future__ import annotations

import math
import time
from typing import NamedTuple


class Era(NamedTuple):
    """One policy regime. `start` is a WINDOW START epoch, never a row time —
    the same quantity score_activity's floor selects on, so a window belongs
    to the era whose policy actually priced it rather than to whenever its
    redeem happened to post."""

    name: str
    start: float
    why: str


# Ordered oldest-first. Boundary epochs are 2026-08-22/23 UTC; each is a
# commit timestamp (`git log --format=%ct`) or the deploy the repo records,
# and the comment above each row is the receipt.
ERAS: tuple[Era, ...] = (
    # Open-ended on the left ON PURPOSE: epoch 0 means no window ever traded
    # can fall outside the registry. Covers the first two nights — the −$370
    # btc-15m blowup (pmengine/fixtures/btc-updown-15m-1787449500.json, era
    # tag `pre-brake`), the −$318 window 8b3c156 was cut to fix, and the xrp
    # basis losses that struck xrp off the tradeable list (ROADMAP.md:14
    # "per-arm basis guards (BTC 3bp, alts 6bp, XRP off)", :100 "XRP not
    # tradeable via Binance proxy").
    Era("pre-brake", 0.0,
        "no brakes, no theta — the -$370 btc window and the xrp basis losses"),

    # 625621d @ 1787451100 = 2026-08-23T02:11:40Z — "pmengine updown:
    # book-distrust + no-averaging-down brakes, time-based unlock". The three
    # brakes ROADMAP.md:286 forbids ever loosening. Corroborated by the
    # fixtures: btc-updown-15m-1787449500 (start 1787449500, before this) is
    # the only one tagged `pre-brake`; nothing after it is.
    Era("brakes", 1787451100.0,
        "15c book-distrust + 2c no-averaging-down brakes, time-based unlock (625621d)"),

    # ROADMAP.md:194 — "R9 Entry gate: banked evidence, not clock % —
    # DEPLOYED 2026-08-23 05:00Z (7b1948d, --theta 0.3 --min-elapsed 0
    # fleet-wide)". 05:00:00Z = 1787461200. The commit landed 11.5 min
    # earlier (7b1948d @ 1787460509 = 04:48:29Z); the DEPLOY is the boundary,
    # because a window that ran at 04:55Z ran the old binary. Same commit
    # shipped the window brake LATCH. Corroborated by the fixtures:
    # eth-updown-15m-1787461200 starts exactly on this boundary and is tagged
    # `post-theta`; btc-updown-15m-1787458500 (04:15Z) is `pre-theta`.
    # The dark-shipped pay-up buffer + print-flow recorder (6242ceb @
    # 1787462202, ROADMAP.md:33) land INSIDE this era — the fixtures tag the
    # 06:00Z+ btc windows `payup-era` — and get no boundary of their own
    # because pay-up shipped dark and bites only on a per-arm `--pay-up`.
    Era("theta", 1787461200.0,
        "R9 theta entry gate retires the 50% clock gate, + the window brake latch (7b1948d)"),

    # cc1704a @ 1787481547 = 2026-08-23T10:39:07Z — "Merge WS-first
    # authoritative book", landing 4549a6a "the websocket was never carrying
    # the book — now it is": before this every book the strategies read came
    # off the 2s REST poller, and the latency report's p50 2.2-3.2s book age
    # was the poller's cadence, not the wire's. Same change window carries
    # the fleet-wide un-decided cap (7a3b11d, dark) and the size scale-up
    # analysis/aggression_sweep.md:5 reads off as "today's scaled sizes (btc
    # 700/100, eth 600/75, sol 200/25, bnb 100/10)" against ROADMAP.md:276's
    # 400/50 baseline — book source AND size both moved here, hence the name.
    # 46fcb0f @ 1787481999 (sync strategy-load must subscribe too) is a
    # follow-up inside the same window, not a second boundary.
    Era("ws+scale", 1787481547.0,
        "WS-first authoritative book replaces the 2s REST poller, sizes scaled up (cc1704a)"),

    # e296336 @ 1787484570 = 2026-08-23T11:29:30Z — "updown: read the
    # settlement stream itself — RTDS-fed arms (dark)". Corroborated by the
    # first stream-fed window: pmengine/fixtures/xrp-updown-5m-1787485200.json,
    # era tag `post-rtds`, start 1787485200 — 10.5 min after this boundary
    # (b7b981a, "freeze the first stream-fed xrp window (post-rtds era)").
    # It shipped dark (ROADMAP.md:107, "no arm uses rtds until the operator
    # arms one"), so windows in that 10.5-min gap were still binance-fed and
    # are still filed here: the boundary is the deploy, and dating it off the
    # first stream-fed window instead would date an era by its outcome.
    Era("stream", 1787484570.0,
        "arms can read the Chainlink settlement stream itself, --feed rtds (e296336)"),
)


def validate(registry: tuple[Era, ...] = ERAS) -> None:
    """Refuse a registry that could hide or double-count a window.

    Runs at import, so a bad boundary is an ImportError on the developer's
    machine rather than a quietly wrong scoreboard on the operator's.
    """
    if not registry:
        raise ValueError("era registry is empty — every window needs an era")
    if registry[0].start != 0.0:
        raise ValueError(
            f"first era {registry[0].name!r} starts at {registry[0].start}, not 0 — "
            "windows before it would belong to no era")
    seen: set[str] = set()
    prev: float | None = None
    for e in registry:
        if not e.name or e.name != e.name.strip() or " " in e.name:
            raise ValueError(f"era name {e.name!r} must be a non-empty bare token")
        if e.name in seen:
            raise ValueError(f"duplicate era name {e.name!r}")
        seen.add(e.name)
        if not e.why.strip():
            raise ValueError(f"era {e.name!r} has no stated reason")
        # Strictly increasing: equal starts would make one era zero-length and
        # ambiguous, decreasing starts would overlap two eras over one window.
        if prev is not None and e.start <= prev:
            raise ValueError(
                f"era {e.name!r} starts at {e.start}, not after the previous {prev} — "
                "era starts must be strictly increasing")
        prev = e.start


def bounds(era: Era | str, registry: tuple[Era, ...] = ERAS) -> tuple[float, float]:
    """The era's half-open [start, end) window-start range.

    Half-open is what makes the registry a partition: a window whose start
    lands exactly on a boundary belongs to the NEW era, and to only that one.
    The last era ends at +inf — it is still running.
    """
    name = era.name if isinstance(era, Era) else era
    for i, e in enumerate(registry):
        if e.name == name:
            end = registry[i + 1].start if i + 1 < len(registry) else math.inf
            return e.start, end
    raise KeyError(f"unknown era {name!r}")


def by_name(name: str, registry: tuple[Era, ...] = ERAS) -> Era | None:
    for e in registry:
        if e.name == name:
            return e
    return None


def names(registry: tuple[Era, ...] = ERAS) -> list[str]:
    return [e.name for e in registry]


def for_start(start: float, registry: tuple[Era, ...] = ERAS) -> Era:
    """The era a window with this START epoch was traded under.

    Never None: the registry opens at 0, so a negative-or-zero start still
    lands in the first era rather than falling out of the ledger.
    """
    hit = registry[0]
    for e in registry:
        if start >= e.start:
            hit = e
        else:
            break
    return hit


def current(now: float | None = None, registry: tuple[Era, ...] = ERAS) -> Era:
    """The era the clock is in — the one the identity line names.

    Same rule as for_start(): the newest era whose deploy has happened. A
    clock dragged back before a boundary reports the older era, which is the
    behaviour a replay or a test wants.
    """
    return for_start(time.time() if now is None else now, registry)


validate()
