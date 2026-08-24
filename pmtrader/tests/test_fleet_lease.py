"""The lease protocol: grace arithmetic, the skew bound, fencing, and the
property the whole thing exists for — never two holders inside one interval.
"""

from __future__ import annotations

import random

import pytest
from orchestrator.lease import (
    DEFAULT_HOME_EXTENSION_S,
    FENCE_CHECK_S,
    MAX_PAIR_SKEW_S,
    MAX_SKEW_S,
    STOP_LATENCY_S,
    TERM_S,
    Assignment,
    Lease,
    can_acquire,
    claimable_at,
    fence_deadline,
    grace_floor,
    should_fence,
    skew_ok,
)

FIVE_M, FIFTEEN_M = 300.0, 900.0


def lease(**kw) -> Lease:
    base = dict(
        series="btc-updown-5m", holder="desktop", epoch=1, expires_at=1000.0,
        released=False, home_extension_s=DEFAULT_HOME_EXTENSION_S,
        grace_s=420.0, holder_is_home=True,
    )
    base.update(kw)
    return Lease(**base)


def assign(**kw) -> Assignment:
    base = dict(
        series="btc-updown-5m", home="desktop", failover="euw",
        window_dur_s=FIVE_M, grace_s=420.0,
    )
    base.update(kw)
    return Assignment(**base)


# --- grace arithmetic ------------------------------------------------------

def test_grace_floor_covers_a_whole_window_plus_every_delay_term():
    # The window term is the load-bearing one: the departing holder may only be
    # able to stop at a window boundary (the roll refusal is the enforcement
    # point we can lean on hardest), so a grace shorter than a window cannot
    # promise it is quiet.
    f = grace_floor(FIVE_M)
    assert f == FIVE_M + FENCE_CHECK_S + STOP_LATENCY_S + MAX_PAIR_SKEW_S
    assert f > FIVE_M
    assert grace_floor(FIFTEEN_M) > FIFTEEN_M


def test_the_shipped_graces_clear_their_floors_and_hunters_stated_minimums():
    # Hunter's floors from the brief: >= 6min for 5m-only, >= 20min for
    # 15m-capable. Both shipped values must satisfy the computed floor AND those.
    assert 420.0 >= grace_floor(FIVE_M) and 420.0 >= 360.0
    assert 1200.0 >= grace_floor(FIFTEEN_M) and 1200.0 >= 1200.0


def test_a_15m_grace_is_never_enough_for_a_15m_series_if_sized_for_5m():
    # The bug this guards: copying a 5m series' grace onto a 15m one. It looks
    # generous (420s > 300s) and is short by more than a whole window.
    assert 420.0 < grace_floor(FIFTEEN_M)


def test_assignment_falls_back_to_the_floor_when_grace_is_unset():
    a = assign(grace_s=None, window_dur_s=FIFTEEN_M)
    assert a.effective_grace_s() == grace_floor(FIFTEEN_M)


# --- the clock bound -------------------------------------------------------

@pytest.mark.parametrize("offset,ok", [
    (0.0, True), (MAX_SKEW_S, True), (-MAX_SKEW_S, True),
    (MAX_SKEW_S + 0.01, False), (-MAX_SKEW_S - 0.01, False),
    (3600.0, False),
])
def test_skew_bound_is_symmetric_and_inclusive(offset, ok):
    assert skew_ok(offset) is ok


def test_an_unmeasured_clock_is_not_an_ok_clock():
    # None must fail closed. A node that defaults its unknown offset to zero is
    # a node asserting a fact it has not checked, about the one input that
    # could make two disjoint intervals overlap in real time.
    assert skew_ok(None) is False
    assert skew_ok(float("nan")) is False


def test_a_skewed_node_may_not_acquire_even_when_the_lease_is_long_dead():
    ok, why = can_acquire(
        lease(expires_at=0.0), assign(), node="euw", now=1e9,
        offset_s=MAX_SKEW_S + 1, failover_disabled=False,
    )
    assert not ok and "clock offset" in why


def test_a_skewed_holder_fences_itself_immediately():
    # It still holds a perfectly valid, unexpired lease. It stops anyway: it can
    # no longer promise to stop on time, and the promise is the product.
    l = lease(expires_at=10_000.0)
    must, why = should_fence(l, node="desktop", now=1000.0, offset_s=99.0)
    assert must and "clock offset" in why


# --- fencing and the home extension ---------------------------------------

def test_a_failover_holder_gets_no_extension_and_fences_at_expiry():
    l = lease(holder="euw", holder_is_home=False, expires_at=1000.0)
    assert fence_deadline(l) == 1000.0
    assert should_fence(l, node="euw", now=1000.0, offset_s=0.0)[0]
    assert not should_fence(l, node="euw", now=999.0, offset_s=0.0)[0]


def test_a_home_holder_rides_its_extension_through_a_store_outage():
    l = lease(holder="desktop", holder_is_home=True, expires_at=1000.0,
              home_extension_s=600.0)
    assert fence_deadline(l) == 1600.0
    # 5 minutes past expiry with no store in sight: still trading, on purpose.
    assert not should_fence(l, node="desktop", now=1300.0, offset_s=0.0)[0]
    # ...and it does stop. The extension is a bound, not an exemption.
    assert should_fence(l, node="desktop", now=1600.0, offset_s=0.0)[0]


def test_the_claimant_budgets_the_same_extension_the_holder_takes():
    # This is the crux: both sides compute one function of one recorded fact
    # (`holder_is_home`), so the fence and the claim bar cannot disagree.
    l = lease(holder="desktop", holder_is_home=True, expires_at=1000.0,
              home_extension_s=600.0, grace_s=420.0)
    assert claimable_at(l) == 1000.0 + 600.0 + 420.0
    assert claimable_at(l) - fence_deadline(l) == 420.0

    l2 = lease(holder="euw", holder_is_home=False, expires_at=1000.0,
               home_extension_s=600.0, grace_s=420.0)
    assert claimable_at(l2) == 1420.0
    assert claimable_at(l2) - fence_deadline(l2) == 420.0


def test_home_extension_zero_is_the_fast_failover_end_of_the_dial():
    l = lease(home_extension_s=0.0, expires_at=1000.0, grace_s=420.0)
    assert fence_deadline(l) == 1000.0
    assert claimable_at(l) == 1420.0


# --- release ---------------------------------------------------------------

def test_a_clean_release_is_claimable_at_once_and_skips_every_deadline():
    # The nightly poweroff. `released` is only written after the holder has
    # confirmed it has no live orders, so there is nothing left to wait out.
    l = lease(released=True, expires_at=1000.0, home_extension_s=600.0, grace_s=420.0)
    assert claimable_at(l) == 1000.0
    assert fence_deadline(l) == 1000.0
    ok, _ = can_acquire(l, assign(), node="euw", now=1000.5,
                        offset_s=0.0, failover_disabled=False)
    assert ok


def test_a_released_lease_fences_its_own_former_holder():
    l = lease(holder="desktop", released=True, expires_at=1e9)
    assert should_fence(l, node="desktop", now=0.0, offset_s=0.0)[0]


# --- acquisition rules -----------------------------------------------------

def test_a_node_the_map_does_not_name_can_never_acquire():
    ok, why = can_acquire(None, assign(), node="stranger", now=0.0,
                          offset_s=0.0, failover_disabled=False)
    assert not ok and "neither home" in why


def test_the_kill_switch_freezes_failover_claims_but_not_the_home_nodes_return():
    a = assign()
    dead = lease(holder="euw", holder_is_home=False, expires_at=0.0, grace_s=420.0)

    ok, why = can_acquire(dead, a, node="euw", now=1e6, offset_s=0.0, failover_disabled=True)
    assert ok  # it is the holder — renewal, not a claim

    fresh = lease(holder="desktop", expires_at=0.0, grace_s=420.0, home_extension_s=0.0)
    ok, why = can_acquire(fresh, a, node="euw", now=1e6, offset_s=0.0, failover_disabled=True)
    assert not ok and "kill switch" in why

    # The home node reclaiming its own series is how the fleet returns to rest.
    # Freezing that would strand the series on the covering box forever.
    ok, _ = can_acquire(dead, a, node="desktop", now=1e6, offset_s=0.0, failover_disabled=True)
    assert ok


def test_an_unheld_series_is_claimable_and_a_live_one_is_not():
    a = assign()
    ok, why = can_acquire(None, a, node="desktop", now=0.0, offset_s=0.0, failover_disabled=False)
    assert ok and why == "unheld"

    live = lease(expires_at=2000.0)
    ok, why = can_acquire(live, a, node="euw", now=1000.0, offset_s=0.0, failover_disabled=False)
    assert not ok and "held by desktop" in why


def test_the_claim_bar_is_strictly_after_the_holders_fence_by_exactly_grace():
    l = lease(holder="desktop", expires_at=1000.0, home_extension_s=600.0, grace_s=420.0)
    a = assign()
    bar = claimable_at(l)
    for t in (bar - 0.01, bar):
        assert not can_acquire(l, a, node="euw", now=t, offset_s=0.0, failover_disabled=False)[0]
    assert can_acquire(l, a, node="euw", now=bar + 0.01, offset_s=0.0, failover_disabled=False)[0]


# --- the property ----------------------------------------------------------

class _Node:
    """A node as the safety argument models it: a lease, a fence, and a clock
    that may be wrong by up to the bound."""

    def __init__(self, name: str, is_home: bool, offset: float):
        self.name = name
        self.is_home = is_home
        self.offset = offset          # local = true + offset
        self.held: Lease | None = None
        self.trading_until: float | None = None   # TRUE time its last order dies

    def now(self, true_t: float) -> float:
        return true_t + self.offset


def _simulate(seed: int, *, steps: int = 4000) -> list[tuple[float, set[str]]]:
    """Drive two nodes through crashes, partitions and store outages, and record
    who is actually trading (in TRUE time) at every tick.

    The model is deliberately pessimistic about stopping: when a node fences, its
    orders stay live for FENCE_CHECK_S + STOP_LATENCY_S + a whole window — the
    worst case grace_floor is sized for, and the case where the only enforcement
    that works is the roll refusal at a window boundary.
    """
    rng = random.Random(seed)
    a = assign(window_dur_s=FIVE_M, grace_s=420.0, home_extension_s=600.0)
    grace = a.effective_grace_s()

    nodes = [
        _Node("desktop", True, rng.uniform(-MAX_SKEW_S, MAX_SKEW_S)),
        _Node("euw", False, rng.uniform(-MAX_SKEW_S, MAX_SKEW_S)),
    ]
    stored: Lease | None = None
    epoch = 0
    tape: list[tuple[float, set[str]]] = []
    stop_lag = FENCE_CHECK_S + STOP_LATENCY_S + FIVE_M

    t = 0.0
    for _ in range(steps):
        t += rng.uniform(1.0, 20.0)
        store_up = rng.random() > 0.15                     # store outages
        rng.shuffle(nodes)                                  # no fixed ordering luck

        for n in nodes:
            reachable = store_up and rng.random() > 0.15    # per-node partition
            crashed = rng.random() < 0.02

            if crashed:
                # A crash stops orders at once; the lease is left to lapse.
                n.held, n.trading_until = None, None
                continue

            # Fail-safe first: fencing must work with nothing reachable.
            must, _ = should_fence(n.held, node=n.name, now=n.now(t), offset_s=n.offset)
            if must and n.held is not None:
                if n.trading_until is None:
                    n.trading_until = t + stop_lag
                n.held = None

            if not reachable:
                continue

            # Renew (bumps epoch; conditional on holder+epoch).
            if n.held is not None and stored is not None and stored.holder == n.name \
                    and stored.epoch == n.held.epoch:
                epoch += 1
                stored = Lease(
                    series=a.series, holder=n.name, epoch=epoch,
                    expires_at=n.now(t) + TERM_S, released=False,
                    home_extension_s=a.home_extension_s, grace_s=grace,
                    holder_is_home=n.is_home,
                )
                n.held = stored
                continue

            # Acquire.
            ok, _why = can_acquire(
                stored, a, node=n.name, now=n.now(t),
                offset_s=n.offset, failover_disabled=False,
            )
            if ok and (stored is None or stored.holder != n.name):
                epoch += 1
                stored = Lease(
                    series=a.series, holder=n.name, epoch=epoch,
                    expires_at=n.now(t) + TERM_S, released=False,
                    home_extension_s=a.home_extension_s, grace_s=grace,
                    holder_is_home=n.is_home,
                )
                n.held = stored
                n.trading_until = None      # it is live again from here

        live = set()
        for n in nodes:
            if n.held is not None:
                live.add(n.name)
            elif n.trading_until is not None and t < n.trading_until:
                live.add(n.name)             # fenced, orders not yet dead
            elif n.trading_until is not None:
                n.trading_until = None
        tape.append((t, live))
    return tape


@pytest.mark.parametrize("seed", range(25))
def test_no_interleaving_ever_puts_two_nodes_on_one_book(seed):
    for t, live in _simulate(seed):
        assert len(live) <= 1, f"seed {seed}: two holders live at t={t:.1f}: {sorted(live)}"


def test_the_simulation_is_not_vacuous():
    # A test that never lets anyone trade would pass the assertion above and
    # prove nothing. Both nodes must actually get turns, including the failover.
    seen: set[str] = set()
    for seed in range(25):
        for _t, live in _simulate(seed):
            seen |= live
    assert seen == {"desktop", "euw"}, seen
