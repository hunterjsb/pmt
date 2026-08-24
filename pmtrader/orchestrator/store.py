"""The neutral store: one DynamoDB table, and a fake that lies about nothing.

Table `pmt-fleet`, single-table, `pk`/`sk` strings:

    pk="series#<series>"  sk="lease"    the lease. One item per series, and the
                                        ONLY authority on who may trade it.
    pk="node#<node>"      sk="hb"       a node's heartbeat. TTL'd.
    pk="fleet"            sk="config"   the fleet kill switch.

Two things about this table that are easy to get wrong
------------------------------------------------------
**TTL is garbage collection, never correctness.** DynamoDB's TTL deletes items
"typically within 48 hours" of expiry — it is a housekeeping sweep, not a
clock. Every deadline in this system is decided by reading `expires_at` off the
item and comparing it to a skew-checked local clock. The `ttl` attribute exists
so a decommissioned node's heartbeat eventually stops cluttering the fleet
view, and for nothing else. Leases carry no TTL at all: a lease item that
vanished mid-flight would read as "unheld" and be instantly claimable, which is
precisely the interval the grace budget exists to prevent.

**The CAS carries the version; the client carries the clock.** A condition
expression cannot see DynamoDB's clock, so the time half of every decision is
necessarily client-side. What makes that safe is that the epoch CAS refuses any
write whose read was stale: if the item changed at all between the read and the
write, the write fails. So a client only ever acts on an image it has just
proven current, and `fleet.clock`'s skew bound covers the rest.
"""

from __future__ import annotations

import threading
import time
from typing import Any

from .lease import Lease

TABLE_NAME = "pmt-fleet"
REGION = "eu-west-1"

# Heartbeats outlive a long weekend of downtime so a node that has been off for
# days still shows up in the fleet view as "off since ...", rather than
# silently ceasing to exist — a node that disappears looks like a node that was
# never configured, and those need very different responses.
HEARTBEAT_TTL_S = 7 * 86400


class CASFailed(RuntimeError):
    """A conditional write lost. Someone else mutated the item first.

    Never an error in the operational sense: it is the protocol working. The
    caller re-reads and re-decides.
    """


class StoreUnavailable(RuntimeError):
    """The store could not be reached or answered an error.

    Distinct from CASFailed on purpose. A lost CAS means "you are not the
    holder"; an unavailable store means "nobody can become one", and those take
    the fleet to opposite places (see the home/failover asymmetry in lease.py).
    """


def lease_pk(series: str) -> str:
    return f"series#{series}"


def node_pk(node: str) -> str:
    return f"node#{node}"


# --- serialisation ---------------------------------------------------------
# Kept as free functions so the fake and the real client share one shape, and
# a round-trip test can prove they do.


def lease_to_item(lease: Lease) -> dict[str, Any]:
    return {
        "pk": lease_pk(lease.series),
        "sk": "lease",
        "series": lease.series,
        "holder": lease.holder,
        "epoch": int(lease.epoch),
        "expires_at": float(lease.expires_at),
        "released": bool(lease.released),
        "home_extension_s": float(lease.home_extension_s),
        "grace_s": float(lease.grace_s),
        "holder_is_home": bool(lease.holder_is_home),
    }


def item_to_lease(item: dict[str, Any]) -> Lease:
    return Lease(
        series=item["series"],
        holder=item["holder"],
        epoch=int(item["epoch"]),
        expires_at=float(item["expires_at"]),
        released=bool(item.get("released", False)),
        home_extension_s=float(item.get("home_extension_s", 0.0)),
        grace_s=float(item.get("grace_s", 0.0)),
        holder_is_home=bool(item.get("holder_is_home", True)),
    )


class FleetStore:
    """DynamoDB-backed store. The real one.

    Every method raises `StoreUnavailable` rather than leaking botocore
    exceptions, because the caller's decision tree branches on "store reachable
    or not" and nothing finer.
    """

    def __init__(self, table: str = TABLE_NAME, region: str = REGION, client=None):
        self.table = table
        self.region = region
        self._client = client
        # Offset between this node's clock and the store's, refreshed from the
        # `Date` header on every round trip. See fleet/clock.py.
        self.clock_offset_s: float | None = None

    @property
    def client(self):
        if self._client is None:
            import boto3

            self._client = boto3.client("dynamodb", region_name=self.region)
        return self._client

    # -- clock ------------------------------------------------------------
    def _absorb_clock(self, response: dict[str, Any]) -> None:
        """Learn this node's clock offset from the response's Date header.

        The store is the reference clock for the whole protocol, which is
        convenient and also correct: it is the one clock both nodes provably
        share a view of.
        """
        from .clock import offset_from_response

        off = offset_from_response(response)
        if off is not None:
            self.clock_offset_s = off

    def _call(self, op: str, **kwargs):
        from botocore.exceptions import BotoCoreError, ClientError

        try:
            resp = getattr(self.client, op)(TableName=self.table, **kwargs)
        except ClientError as e:
            code = e.response.get("Error", {}).get("Code", "")
            if code == "ConditionalCheckFailedException":
                # Even a losing CAS gets a dated HTTP response — worth having.
                self._absorb_clock(e.response)
                raise CASFailed(f"{op}: condition failed") from e
            raise StoreUnavailable(f"{op}: {code or e}") from e
        except BotoCoreError as e:
            raise StoreUnavailable(f"{op}: {type(e).__name__}: {e}") from e
        self._absorb_clock(resp)
        return resp

    # -- leases -----------------------------------------------------------
    def get_lease(self, series: str) -> Lease | None:
        resp = self._call(
            "get_item",
            Key={"pk": {"S": lease_pk(series)}, "sk": {"S": "lease"}},
            ConsistentRead=True,  # a stale read here is a stale epoch, not a bug
        )
        item = resp.get("Item")
        return item_to_lease(_from_dynamo(item)) if item else None

    def put_lease(self, lease: Lease, *, expected_epoch: int | None) -> None:
        """Write the lease iff it is still at `expected_epoch`.

        `expected_epoch=None` means "iff no lease item exists at all". This is
        the single mutating primitive: acquire, renew and release are all a
        `put_lease` with a bumped epoch, which is what makes "every mutation
        bumps epoch" true by construction rather than by discipline.
        """
        if expected_epoch is None:
            cond = "attribute_not_exists(pk)"
            values = {}
        else:
            cond = "epoch = :e"
            values = {":e": {"N": str(int(expected_epoch))}}
        kwargs: dict[str, Any] = {
            "Item": _to_dynamo(lease_to_item(lease)),
            "ConditionExpression": cond,
        }
        if values:
            kwargs["ExpressionAttributeValues"] = values
        self._call("put_item", **kwargs)

    # -- heartbeats -------------------------------------------------------
    def put_heartbeat(self, hb: dict[str, Any]) -> None:
        """Write a node's heartbeat, replacing the row whole, newest-wins.

        Whole-row replacement is deliberate and copied from the mubs worker: it
        makes the clean-shutdown stamp self-clearing. The marker is written by
        the last beat before exit and is gone the moment the node beats again
        after boot, so nobody has to remember to clear it.

        The condition (`ts <= :ts`) makes the row monotonic. Two daemons for
        one node is a real configuration accident — the systemd unit plus a
        hand-started one — and without this the fleet view flaps backwards
        between them, which reads as a node whose clock is broken. It also
        stops a delayed retry from resurrecting a pre-shutdown beat on top of
        the clean-shutdown stamp and re-arming the pager for a box that is
        deliberately off. A lost CAS here is not worth retrying: a newer beat
        already landed, which is the outcome we wanted anyway.
        """
        item = dict(hb)
        item["pk"] = node_pk(item["node"])
        item["sk"] = "hb"
        item["ttl"] = int(time.time()) + HEARTBEAT_TTL_S
        self._call(
            "put_item",
            Item=_to_dynamo(item),
            ConditionExpression="attribute_not_exists(pk) OR ts <= :ts",
            ExpressionAttributeValues={":ts": {"N": repr(float(item["ts"]))}},
        )

    def get_heartbeat(self, node: str) -> dict[str, Any] | None:
        resp = self._call(
            "get_item",
            Key={"pk": {"S": node_pk(node)}, "sk": {"S": "hb"}},
            ConsistentRead=True,
        )
        item = resp.get("Item")
        return _from_dynamo(item) if item else None

    def all_heartbeats(self) -> list[dict[str, Any]]:
        """Every node heartbeat in the table.

        A scan, and unapologetically: this table holds one item per node and
        one per series — tens of items, forever. An index would cost more to
        maintain than the scan costs to run.
        """
        out: list[dict[str, Any]] = []
        kwargs: dict[str, Any] = {
            "FilterExpression": "sk = :sk",
            "ExpressionAttributeValues": {":sk": {"S": "hb"}},
        }
        while True:
            resp = self._call("scan", **kwargs)
            out.extend(_from_dynamo(i) for i in resp.get("Items", []))
            last = resp.get("LastEvaluatedKey")
            if not last:
                return out
            kwargs["ExclusiveStartKey"] = last

    def all_leases(self) -> list[Lease]:
        out: list[Lease] = []
        kwargs: dict[str, Any] = {
            "FilterExpression": "sk = :sk",
            "ExpressionAttributeValues": {":sk": {"S": "lease"}},
        }
        while True:
            resp = self._call("scan", **kwargs)
            out.extend(item_to_lease(_from_dynamo(i)) for i in resp.get("Items", []))
            last = resp.get("LastEvaluatedKey")
            if not last:
                return out
            kwargs["ExclusiveStartKey"] = last

    # -- kill switch ------------------------------------------------------
    def failover_disabled(self) -> bool:
        """The fleet-wide freeze on failover claims.

        Unreadable reads as DISABLED. The kill switch is a safety control, and
        a safety control that fails open is decoration — if we cannot tell
        whether Hunter has frozen failover, the answer is that we do not claim.
        A missing config item is a different thing from an unreadable one and
        reads as enabled: that is the table's resting state.
        """
        try:
            resp = self._call(
                "get_item",
                Key={"pk": {"S": "fleet"}, "sk": {"S": "config"}},
                ConsistentRead=True,
            )
        except StoreUnavailable:
            return True
        item = resp.get("Item")
        if not item:
            return False
        return bool(_from_dynamo(item).get("disabled", False))

    def set_failover_disabled(self, disabled: bool) -> None:
        self._call(
            "put_item",
            Item=_to_dynamo(
                {"pk": "fleet", "sk": "config", "disabled": bool(disabled),
                 "updated_at": time.time()}
            ),
        )


# --- typed-value plumbing --------------------------------------------------
# Hand-rolled rather than boto3.dynamodb.types: the value space here is four
# scalars and a string list, and a serialiser we can read end to end is worth
# more than one that handles Decimal edge cases we never produce.


def _to_dynamo(item: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for k, v in item.items():
        if isinstance(v, bool):
            out[k] = {"BOOL": v}
        elif isinstance(v, (int, float)):
            out[k] = {"N": repr(v) if isinstance(v, float) else str(v)}
        elif isinstance(v, (list, tuple)):
            out[k] = {"L": [{"S": str(x)} for x in v]}
        elif v is None:
            out[k] = {"NULL": True}
        else:
            out[k] = {"S": str(v)}
    return out


def _from_dynamo(item: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for k, tv in item.items():
        (t, v), = tv.items()
        if t == "BOOL":
            out[k] = v
        elif t == "N":
            f = float(v)
            out[k] = int(f) if f.is_integer() and "." not in v and "e" not in v.lower() else f
        elif t == "L":
            out[k] = [list(x.values())[0] for x in v]
        elif t == "NULL":
            out[k] = None
        else:
            out[k] = v
    return out


class FakeStore:
    """In-memory store with real conditional-write semantics.

    The point of this class is the lock. Two threads racing `put_lease` on one
    epoch must produce exactly one winner and one `CASFailed`, because that is
    the property the whole safety argument rests on, and a fake that serialises
    by accident of the GIL would let a broken protocol test green.
    """

    def __init__(self):
        self._items: dict[tuple[str, str], dict[str, Any]] = {}
        self._lock = threading.Lock()
        self.clock_offset_s: float | None = 0.0
        # Test hooks.
        self.unavailable = False
        self.calls: list[str] = []
        # Fires between the condition check and the write, to open the race
        # window deterministically instead of hoping for a scheduler slice.
        self.on_cas_check = None

    def _guard(self, op: str) -> None:
        self.calls.append(op)
        if self.unavailable:
            raise StoreUnavailable(f"{op}: fake store is down")

    def get_lease(self, series: str) -> Lease | None:
        self._guard("get_lease")
        with self._lock:
            item = self._items.get((lease_pk(series), "lease"))
            return item_to_lease(dict(item)) if item else None

    def put_lease(self, lease: Lease, *, expected_epoch: int | None) -> None:
        self._guard("put_lease")
        key = (lease_pk(lease.series), "lease")
        with self._lock:
            cur = self._items.get(key)
            ok = cur is None if expected_epoch is None else (
                cur is not None and int(cur["epoch"]) == int(expected_epoch)
            )
            if self.on_cas_check is not None:
                # Called INSIDE the lock: a hook that tried to race here would
                # deadlock, which is the honest signal that the real store
                # gives no such window either.
                self.on_cas_check(lease, expected_epoch, ok)
            if not ok:
                seen = None if cur is None else cur["epoch"]
                raise CASFailed(
                    f"put_lease({lease.series}): expected epoch {expected_epoch}, found {seen}"
                )
            self._items[key] = lease_to_item(lease)

    def put_heartbeat(self, hb: dict[str, Any]) -> None:
        self._guard("put_heartbeat")
        item = dict(hb)
        item["pk"] = node_pk(item["node"])
        item["sk"] = "hb"
        item["ttl"] = int(time.time()) + HEARTBEAT_TTL_S
        with self._lock:
            cur = self._items.get((item["pk"], "hb"))
            if cur is not None and float(cur.get("ts", 0)) > float(item["ts"]):
                raise CASFailed(
                    f"put_heartbeat({item['node']}): a newer beat "
                    f"({cur['ts']}) already landed"
                )
            self._items[(item["pk"], "hb")] = item

    def get_heartbeat(self, node: str) -> dict[str, Any] | None:
        self._guard("get_heartbeat")
        with self._lock:
            item = self._items.get((node_pk(node), "hb"))
            return dict(item) if item else None

    def all_heartbeats(self) -> list[dict[str, Any]]:
        self._guard("all_heartbeats")
        with self._lock:
            return [dict(v) for (_, sk), v in self._items.items() if sk == "hb"]

    def all_leases(self) -> list[Lease]:
        self._guard("all_leases")
        with self._lock:
            return [
                item_to_lease(dict(v)) for (_, sk), v in self._items.items() if sk == "lease"
            ]

    def failover_disabled(self) -> bool:
        try:
            self._guard("failover_disabled")
        except StoreUnavailable:
            return True
        with self._lock:
            return bool(self._items.get(("fleet", "config"), {}).get("disabled", False))

    def set_failover_disabled(self, disabled: bool) -> None:
        self._guard("set_failover_disabled")
        with self._lock:
            self._items[("fleet", "config")] = {
                "pk": "fleet", "sk": "config", "disabled": bool(disabled),
            }
