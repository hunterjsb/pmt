"""The series lease — the one thing standing between two engines and a self-match.

Two engines under one operator must never quote the same series at the same
instant (CLAUDE.md, "Series partition"). A liveness-based failover cannot
deliver that: during a network partition each side sees a dead peer and both
trade. So the right to trade a series is not derived from anyone's opinion
about who is alive. It is a LEASE, and the lease intervals are non-overlapping
by construction.

Three rules carry the whole safety argument. Everything else in this file is
arithmetic in service of them.

  R1  A node may have live orders on S only while it holds an unexpired lease
      on S, and it stops at its own `fence` deadline whether or not it can
      reach anything.
  R2  A claimant may acquire only after `fence + grace(S)` — where `grace` is
      sized so the previous holder is provably quiet by then — and only via a
      compare-and-set on the lease's `epoch`.
  R3  Every mutation bumps `epoch`, and every mutation is conditional on the
      epoch the writer read. DynamoDB serialises conditional writes on one
      item, so the lease's history is a totally ordered chain with exactly one
      holder per link.

R3 kills the double-claim race outright: two claimants that both read epoch 42
and both decide "claimable" issue two CASes on epoch 42, and exactly one lands.
R1+R2 kill the temporal overlap ACROSS links. See orchestrator/DESIGN.md for
the full argument and the failure matrix.

The asymmetry that makes a store outage survivable
--------------------------------------------------
A series has exactly one HOME node and (optionally) one FAILOVER node. Both
trade only under a lease. They differ in one place only: what happens when a
holder cannot renew.

  * home holder      -> keeps trading for `home_extension_s` past expiry
  * failover holder  -> stops at expiry, immediately

That asymmetry is safe because a claim is a store WRITE. If the store is
unreachable nobody can acquire, so the set of nodes trading S can only shrink;
and the one node that keeps going is the home node, which is unique by the
assignment map. Two nodes can never both take the availability branch for one
series. The claimant budgets the extension too — it reads `holder` off the
lease item and applies exactly the same function the holder applied to itself,
so both sides compute the same deadline from the same recorded fact.

`home_extension_s` is the dial between store-outage availability and
crash-failover latency. It buys nothing during a graceful handover, because a
clean release short-circuits every deadline here.
"""

from __future__ import annotations

import dataclasses
import math

# --- the clock bound -------------------------------------------------------
# Every deadline below is written on one node's clock and read on another's,
# so the protocol is only as good as the bound on their disagreement. This is
# not an assumption: fleet.clock measures each node's offset against the
# store's own `Date` header on every round trip, and a node past this bound
# refuses to acquire and fences itself immediately (see `skew_ok`).
#
# 5s is ~50x the offset an NTP-disciplined host actually runs at (both boxes
# are: chronyd on the desktop, the Amazon Time Sync Service on the EU box).
# It is deliberately loose enough that the guard only ever fires on a real
# clock fault, and tight enough to stay a small term in the grace budget.
MAX_SKEW_S = 5.0

# Worst-case disagreement between two participating nodes: both may sit at
# opposite edges of the bound.
MAX_PAIR_SKEW_S = 2 * MAX_SKEW_S

# --- protocol timings ------------------------------------------------------
# A lease is renewed every RENEW_INTERVAL_S and is good for TERM_S, so three
# consecutive renew failures are survivable before the fence bites.
TERM_S = 90.0
RENEW_INTERVAL_S = 30.0

# How often a holder re-checks its own fence deadline. Bounds how late it can
# notice it has lapsed, and is therefore a term in the grace budget.
FENCE_CHECK_S = 5.0

# Decide-to-quiet: from "I am past my fence" to "no order of mine is live on
# that book". MEASURED, not assumed: 13 graceful stops on 2026-08-23/24 ran
# p50 7s, max 31s — which breached the original 30s allowance by a second.
# 60s is 2x the observed worst; re-derive if a stop ever logs past 45s.
STOP_LATENCY_S = 60.0

# Default store-outage tolerance for a HOME holder. Ten minutes covers every
# realistic DynamoDB blip (throttles, transient 5xx, an SDK retry storm) while
# keeping crash failover inside ~19 minutes for a 5m series. Per-series
# override lives in the assignment map.
#
#   home_extension_s = 0    -> fastest failover, a store outage stops the fleet
#   home_extension_s = 1800 -> ride out a half-hour outage, ~48min failover
DEFAULT_HOME_EXTENSION_S = 600.0


def grace_floor(
    window_dur_s: float,
    *,
    fence_check_s: float = FENCE_CHECK_S,
    stop_latency_s: float = STOP_LATENCY_S,
    pair_skew_s: float = MAX_PAIR_SKEW_S,
) -> float:
    """The smallest `grace` that keeps the departing holder provably quiet.

    Four terms, each a real thing that can delay the moment the old holder's
    last order stops being live:

      window_dur_s   the departing holder may only be able to stop at a WINDOW
                     BOUNDARY. `pmt crypto disarm` does pull orders mid-window,
                     but the enforcement point we can lean on hardest is the
                     in-strategy roll refusal, which acts when the roll chain
                     tries to re-arm. Budgeting a whole window means the
                     argument survives even if roll refusal is the ONLY thing
                     that works — which is the assumption to make about a node
                     sick enough to have lost its lease.
      fence_check_s  how late it can notice it lapsed.
      stop_latency_s cancel round-trip once it has noticed.
      pair_skew_s    the two clocks may disagree by this much, worst case, and
                     the claimant's `now` is the one being compared against a
                     deadline written on the holder's.

    Returns seconds. The map's `grace_s` is validated against this and a
    smaller value is refused, not rounded up — a grace that does not cover the
    stop is the one bug in this system that costs money quietly.
    """
    return window_dur_s + fence_check_s + stop_latency_s + pair_skew_s


def skew_ok(offset_s: float | None, *, bound_s: float = MAX_SKEW_S) -> bool:
    """Whether this node's clock is close enough to the store's to participate.

    `None` (never measured, or the last measurement failed) is NOT ok: a node
    that cannot establish its own offset cannot honour a deadline expressed on
    someone else's clock.
    """
    if offset_s is None or math.isnan(offset_s):
        return False
    return abs(offset_s) <= bound_s


@dataclasses.dataclass(frozen=True)
class Assignment:
    """Who owns a series, who covers it, and how long the handover must wait.

    Config, not state. Written by the operator, read by every node, and
    validated by `fleet.assign` before anything acts on it.
    """

    series: str
    home: str
    window_dur_s: float
    failover: str | None = None
    grace_s: float | None = None
    home_extension_s: float = DEFAULT_HOME_EXTENSION_S
    # The arm the failover node would raise if it takes over. Deliberately
    # conservative and deliberately NOT a copy of the home node's arm: a box
    # covering for another box is the wrong moment to discover the size was
    # tuned for different capital.
    arm_template: dict | None = None
    # Claim order when a node could cover more series than its capital allows.
    # Lower goes first. This matters more than it looks: the EU box holds ~182
    # pUSD against a $60 total-exposure cap, so it can meaningfully cover two
    # of the desktop's eight series, not eight. Without an order, which two it
    # gets is whichever the loop happened to reach.
    failover_priority: int = 100

    def effective_grace_s(self) -> float:
        """The configured grace, or the floor for this window duration."""
        floor = grace_floor(self.window_dur_s)
        return floor if self.grace_s is None else float(self.grace_s)

    def covers(self, node: str) -> bool:
        """Whether `node` may ever hold this series at all."""
        return node == self.home or node == self.failover

    def role_of(self, node: str) -> str | None:
        if node == self.home:
            return "home"
        if node == self.failover:
            return "failover"
        return None


@dataclasses.dataclass(frozen=True)
class Lease:
    """The lease item as it sits in the store.

    `epoch` is the version counter and the linearisation point — every
    mutation bumps it and every mutation is conditional on it.
    """

    series: str
    holder: str
    epoch: int
    expires_at: float
    released: bool = False
    # Copied in at acquire time so a claimant computing deadlines never has to
    # trust its own copy of the map to match the holder's. If the operator
    # edits the map mid-flight, the in-flight lease still resolves under the
    # terms it was taken out on.
    home_extension_s: float = DEFAULT_HOME_EXTENSION_S
    grace_s: float = 0.0
    holder_is_home: bool = True


def fence_deadline(lease: Lease) -> float:
    """When the HOLDER must have stopped trading, on the holder's own clock.

    A home holder gets `home_extension_s` past expiry to ride out a store
    outage; a failover holder gets nothing. A released lease fences at once —
    the holder has already declared itself quiet.
    """
    if lease.released:
        return lease.expires_at
    if lease.holder_is_home:
        return lease.expires_at + lease.home_extension_s
    return lease.expires_at


def claimable_at(lease: Lease) -> float:
    """The earliest time another node may acquire, on the claimant's clock.

    The claimant applies the SAME extension the holder applies to itself,
    derived from `holder_is_home` as recorded on the item. Both sides compute
    one function of one fact, which is what makes the two intervals disjoint
    rather than merely usually-disjoint.

    A cleanly released lease is claimable immediately: `released` is only ever
    written after the holder has confirmed it has no live orders, so there is
    nothing left to wait for. That is what makes the nightly poweroff a
    zero-downtime handover instead of a `grace`-long hole.
    """
    if lease.released:
        return lease.expires_at
    return fence_deadline(lease) + lease.grace_s


def can_acquire(
    lease: Lease | None,
    assignment: Assignment,
    *,
    node: str,
    now: float,
    offset_s: float | None,
    failover_disabled: bool,
) -> tuple[bool, str]:
    """May `node` acquire `assignment.series` right now? With the reason why not.

    The reason string is the whole point of the return shape — this decision
    gets logged on every failed attempt, and "not yet" and "not ever" are very
    different operational situations.
    """
    role = assignment.role_of(node)
    if role is None:
        return False, (
            f"{node} is neither home ({assignment.home}) nor failover "
            f"({assignment.failover}) for {assignment.series}"
        )

    if not skew_ok(offset_s):
        shown = "unmeasured" if offset_s is None else f"{offset_s:+.1f}s"
        return False, (
            f"clock offset {shown} exceeds the {MAX_SKEW_S}s bound — this node "
            "cannot honour a deadline written on another node's clock"
        )

    # A holder renewing is not a claim, and the order of these two checks is
    # the difference. The kill switch freezes NEW takeovers; it does not yank a
    # takeover already in progress, because that would fence a live failover
    # holder mid-window and strand the series until its home node returned —
    # a much larger action than "stop failing over", and not the one Hunter
    # asked the switch for. Ending an in-flight takeover is a deliberate,
    # separate act (disarm the covering node, let its lease lapse).
    if lease is not None and lease.holder == node:
        return True, "already held by this node — renew, not acquire"

    # The kill switch freezes FAILOVER claims only. A home node reclaiming its
    # own series is how the fleet returns to its resting state, and freezing
    # that would strand a series on the covering box with no way back.
    if role == "failover" and failover_disabled:
        return False, "failover is disabled fleet-wide (pmt-fleet kill switch)"

    if lease is None:
        return True, "unheld"

    ready = claimable_at(lease)
    if now <= ready:
        if lease.released:
            return False, f"held by {lease.holder} (released, claimable now)"
        return False, (
            f"held by {lease.holder} until {ready:.0f} "
            f"({ready - now:.0f}s to go: expiry+{fence_deadline(lease) - lease.expires_at:.0f}s "
            f"fence +{lease.grace_s:.0f}s grace)"
        )

    return True, f"{lease.holder} lapsed {now - ready:.0f}s past its claim bar"


def should_fence(
    lease: Lease | None,
    *,
    node: str,
    now: float,
    offset_s: float | None,
) -> tuple[bool, str]:
    """Must `node` stop trading this series right now?

    This is the fail-safe half of the protocol and it runs on local state
    only: it must reach the right answer with the store unreachable, which is
    exactly the case it exists for.
    """
    if lease is None:
        return True, "no lease held"
    if lease.holder != node:
        return True, f"lease now held by {lease.holder}"
    if lease.released:
        return True, "lease released by this node"
    if not skew_ok(offset_s):
        shown = "unmeasured" if offset_s is None else f"{offset_s:+.1f}s"
        return True, (
            f"clock offset {shown} exceeds the {MAX_SKEW_S}s bound — a node that "
            "cannot place itself in time cannot promise to stop on time"
        )
    fence = fence_deadline(lease)
    if now >= fence:
        return True, f"past fence by {now - fence:.0f}s (expiry {lease.expires_at:.0f})"
    return False, f"held, {fence - now:.0f}s of fence left"


def next_expiry(now: float, *, term_s: float = TERM_S) -> float:
    return now + term_s
