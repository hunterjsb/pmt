"""The store: compare-and-set under contention, and the shape of the real
DynamoDB calls the fake stands in for.

The fake is only worth having if it is faithful about the one thing that
matters — that two writers on one epoch produce one winner — and only worth
trusting if something also checks that the real client issues the condition the
fake is imitating. Both are here.
"""

from __future__ import annotations

import threading
import time
from typing import Any

import pytest
from orchestrator.clock import offset_from_response, parse_http_date
from orchestrator.lease import Lease
from orchestrator.store import (
    HEARTBEAT_TTL_S,
    CASFailed,
    FakeStore,
    FleetStore,
    StoreUnavailable,
    _from_dynamo,
    _to_dynamo,
    item_to_lease,
    lease_to_item,
)


def L(**kw) -> Lease:
    base = dict(series="btc-updown-5m", holder="desktop", epoch=1, expires_at=1000.0,
                released=False, home_extension_s=600.0, grace_s=420.0, holder_is_home=True)
    base.update(kw)
    return Lease(**base)


# --- contention ------------------------------------------------------------

def test_only_one_of_many_claimants_on_one_epoch_can_win():
    """The double-claim race, run for real.

    Every thread reads the same epoch, every thread decides it is claimable,
    and every thread issues a CAS on that epoch. Exactly one may land — this is
    the property the entire non-overlap argument reduces to.
    """
    store = FakeStore()
    store.put_lease(L(holder="desktop", epoch=7), expected_epoch=None)

    winners: list[str] = []
    losers: list[str] = []
    barrier = threading.Barrier(12)
    lock = threading.Lock()

    def claim(name: str):
        barrier.wait()          # release them all at once
        try:
            store.put_lease(L(holder=name, epoch=8), expected_epoch=7)
        except CASFailed:
            with lock:
                losers.append(name)
        else:
            with lock:
                winners.append(name)

    threads = [threading.Thread(target=claim, args=(f"n{i}",)) for i in range(12)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert len(winners) == 1, f"{len(winners)} winners: {winners}"
    assert len(losers) == 11
    assert store.get_lease("btc-updown-5m").holder == winners[0]


def test_create_only_is_exclusive_too():
    """`expected_epoch=None` is the first-writer-wins case: an unheld series."""
    store = FakeStore()
    outcomes: list[bool] = []
    barrier = threading.Barrier(8)
    lock = threading.Lock()

    def create(name: str):
        barrier.wait()
        try:
            store.put_lease(L(holder=name, epoch=1), expected_epoch=None)
        except CASFailed:
            ok = False
        else:
            ok = True
        with lock:
            outcomes.append(ok)

    ts = [threading.Thread(target=create, args=(f"n{i}",)) for i in range(8)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    assert sum(outcomes) == 1


def test_a_stale_read_can_never_win_a_cas():
    """The reason a client may act on its own clock at all.

    A claimant that reads epoch 3, then sleeps while the holder renews to 4,
    is holding a decision made on data that is no longer true. The CAS refuses
    it — so every write that lands was decided on an image that was current.
    """
    store = FakeStore()
    store.put_lease(L(epoch=3), expected_epoch=None)
    seen = store.get_lease("btc-updown-5m")

    store.put_lease(L(epoch=4, expires_at=2000.0), expected_epoch=3)   # holder renews

    with pytest.raises(CASFailed):
        store.put_lease(L(holder="euw", epoch=seen.epoch + 1), expected_epoch=seen.epoch)


def test_every_mutation_bumps_the_epoch_so_the_chain_is_totally_ordered():
    store = FakeStore()
    store.put_lease(L(epoch=1), expected_epoch=None)
    for e in range(1, 20):
        store.put_lease(L(epoch=e + 1), expected_epoch=e)
    assert store.get_lease("btc-updown-5m").epoch == 20
    # Replaying any earlier epoch is refused, which is what makes the history
    # a chain and not a set.
    for e in range(1, 20):
        with pytest.raises(CASFailed):
            store.put_lease(L(epoch=e + 1), expected_epoch=e)


# --- expiry / TTL ----------------------------------------------------------

def test_heartbeats_carry_a_ttl_and_leases_do_not():
    """TTL is garbage collection here, never correctness.

    A lease with a TTL could vanish mid-flight and read as unheld — instantly
    claimable, which is exactly the interval grace exists to prevent. DynamoDB
    deletes TTL'd items "typically within 48 hours" anyway, so it could not be
    a deadline even if we wanted one.
    """
    store = FakeStore()
    store.put_heartbeat({"node": "desktop", "ts": time.time(), "series_held": []})
    hb = store.get_heartbeat("desktop")
    assert hb["ttl"] > time.time()
    assert hb["ttl"] <= time.time() + HEARTBEAT_TTL_S + 1

    store.put_lease(L(), expected_epoch=None)
    assert "ttl" not in lease_to_item(L())


def test_expiry_is_read_off_the_item_not_inferred_from_the_items_existence():
    # An expired lease is still THERE, and still names its holder — that is how
    # a claimant knows whose fence to budget for.
    store = FakeStore()
    store.put_lease(L(expires_at=0.0, holder="desktop"), expected_epoch=None)
    got = store.get_lease("btc-updown-5m")
    assert got is not None and got.holder == "desktop" and got.expires_at == 0.0


def test_heartbeats_are_monotonic_so_two_daemons_cannot_flap_the_view():
    store = FakeStore()
    store.put_heartbeat({"node": "desktop", "ts": 2000.0, "series_held": []})
    with pytest.raises(CASFailed):
        store.put_heartbeat({"node": "desktop", "ts": 1000.0, "series_held": []})
    assert store.get_heartbeat("desktop")["ts"] == 2000.0


def test_a_late_retry_cannot_resurrect_a_beat_over_the_shutdown_stamp():
    # The pager consequence: an in-flight beat landing after the clean-shutdown
    # marker would make a deliberate poweroff look like a wedge.
    store = FakeStore()
    store.put_heartbeat({"node": "desktop", "ts": 100.0, "series_held": []})
    store.put_heartbeat({"node": "desktop", "ts": 200.0, "series_held": [], "shutdown": 200.0})
    with pytest.raises(CASFailed):
        store.put_heartbeat({"node": "desktop", "ts": 150.0, "series_held": []})
    assert store.get_heartbeat("desktop").get("shutdown") == 200.0


# --- outage semantics ------------------------------------------------------

def test_an_unavailable_store_raises_rather_than_reporting_an_empty_fleet():
    # "No heartbeats" and "cannot read heartbeats" must never be the same
    # answer: one says the fleet is dead, the other says we are blind.
    store = FakeStore()
    store.unavailable = True
    for call in (lambda: store.get_lease("x"), store.all_heartbeats, store.all_leases):
        with pytest.raises(StoreUnavailable):
            call()


def test_an_unreadable_kill_switch_reads_as_disabled():
    """A safety control that fails open is decoration."""
    store = FakeStore()
    store.unavailable = True
    assert store.failover_disabled() is True


def test_a_missing_kill_switch_item_is_the_tables_resting_state_and_reads_enabled():
    assert FakeStore().failover_disabled() is False


def test_the_kill_switch_round_trips():
    store = FakeStore()
    store.set_failover_disabled(True)
    assert store.failover_disabled() is True
    store.set_failover_disabled(False)
    assert store.failover_disabled() is False


# --- serialisation ---------------------------------------------------------

def test_lease_round_trips_through_the_item_form():
    original = L(holder="euw", epoch=42, expires_at=1755990123.5, released=True,
                 holder_is_home=False, grace_s=1200.0, home_extension_s=0.0)
    assert item_to_lease(lease_to_item(original)) == original


def test_dynamo_typed_values_round_trip_every_shape_this_table_holds():
    item = {
        "pk": "node#desktop", "sk": "hb", "ts": 1755990123.5, "epoch": 7,
        "engine_active": True, "released": False, "series_held": ["btc-updown-5m", "eth-updown-5m"],
        "feed_age": -1.0, "nothing": None,
    }
    assert _from_dynamo(_to_dynamo(item)) == item


def test_floats_survive_with_full_precision():
    # `expires_at` is a deadline. A serialiser that rounds it is a serialiser
    # that moves a fence.
    t = 1755990123.123456
    assert _from_dynamo(_to_dynamo({"expires_at": t}))["expires_at"] == t


# --- the real client's calls ----------------------------------------------

class _RecordingClient:
    """Enough of a DynamoDB client to prove FleetStore issues the right calls."""

    def __init__(self, item: dict[str, Any] | None = None, date: str | None = None):
        self.item = item
        self.calls: list[tuple[str, dict]] = []
        self.date = date

    def _resp(self, body=None):
        r = dict(body or {})
        if self.date:
            r["ResponseMetadata"] = {"HTTPHeaders": {"date": self.date}}
        return r

    def put_item(self, **kw):
        self.calls.append(("put_item", kw))
        return self._resp()

    def get_item(self, **kw):
        self.calls.append(("get_item", kw))
        return self._resp({"Item": self.item} if self.item else {})

    def scan(self, **kw):
        self.calls.append(("scan", kw))
        return self._resp({"Items": [self.item] if self.item else []})


def test_put_lease_sends_an_epoch_condition_and_a_create_only_condition():
    c = _RecordingClient()
    s = FleetStore(client=c)

    s.put_lease(L(epoch=5), expected_epoch=4)
    _, kw = c.calls[-1]
    assert kw["ConditionExpression"] == "epoch = :e"
    assert kw["ExpressionAttributeValues"] == {":e": {"N": "4"}}

    s.put_lease(L(epoch=1), expected_epoch=None)
    _, kw = c.calls[-1]
    assert kw["ConditionExpression"] == "attribute_not_exists(pk)"
    assert "ExpressionAttributeValues" not in kw


def test_put_heartbeat_sends_the_monotonic_condition():
    c = _RecordingClient()
    FleetStore(client=c).put_heartbeat({"node": "euw", "ts": 1000.0, "series_held": []})
    _, kw = c.calls[-1]
    assert kw["ConditionExpression"] == "attribute_not_exists(pk) OR ts <= :ts"
    assert kw["ExpressionAttributeValues"] == {":ts": {"N": "1000.0"}}
    assert kw["Item"]["pk"] == {"S": "node#euw"}


def test_reads_are_strongly_consistent():
    # An eventually-consistent read of a lease returns an old epoch, whose CAS
    # then fails — safe, but it turns every claim into a retry loop for no
    # reason. Ask for the current one.
    c = _RecordingClient(item=_to_dynamo(lease_to_item(L())))
    s = FleetStore(client=c)
    s.get_lease("btc-updown-5m")
    assert c.calls[-1][1]["ConsistentRead"] is True


def test_the_store_learns_this_nodes_clock_offset_from_the_response_date():
    # 2026-08-24T00:00:00Z, and a local clock 90 seconds ahead of it.
    c = _RecordingClient(date="Mon, 24 Aug 2026 00:00:00 GMT")
    true_t = parse_http_date("Mon, 24 Aug 2026 00:00:00 GMT")
    off = offset_from_response(c._resp(), now=true_t + 90.0)
    assert off == pytest.approx(90.0, abs=1.0)

    # And the real store absorbs it on any round trip, including a losing CAS.
    s = FleetStore(client=c)
    s.get_lease("btc-updown-5m")
    assert s.clock_offset_s is not None


def test_an_undated_response_leaves_the_offset_unmeasured():
    assert offset_from_response({}) is None
    assert offset_from_response({"ResponseMetadata": {"HTTPHeaders": {"date": "nonsense"}}}) is None
