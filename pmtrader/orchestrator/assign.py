"""The assignment map: who is home for what, who covers it, and on what terms.

Config, not state. It lives in the repo at `orchestrator/assignments.json` so a
change to who owns what is a reviewable diff rather than a console edit, and it
is read identically by every node.

Safety does not rest on the two copies matching
------------------------------------------------
It would be reasonable to worry that a stale map on one box could make two
nodes both believe they are home for a series, and both take the store-outage
availability branch. They cannot, and the reason is worth stating because it is
what lets the map be a file at all:

**the lease item is the authority, not the map.** There is exactly one lease
item per series, it names exactly one `holder`, and it records `holder_is_home`
as decided at acquire time. A node's availability branch keys off the lease it
holds — not off its own opinion of the map. Two nodes cannot both hold one
lease (that is the CAS), so two nodes cannot both be "the home holder" of one
series no matter how badly the map has diverged.

The map decides who is *allowed to try*. Getting it wrong costs a refused
acquire and a confusing log line, not a self-match. Validation below is
therefore about catching operator error early, not about holding the invariant.

The prefix trap
---------------
`PMENGINE_SERIES_ALLOWLIST` matches by PREFIX, because a series' slugs carry a
per-window suffix. That means a series key which is a prefix of another series
key silently claims it: an engine allowed `btc-updown` is allowed
`btc-updown-15m-1787543100` too. If those two keys had different homes, the
partition would be a fiction. `validate` refuses that map.
"""

from __future__ import annotations

import dataclasses
import json
import os
from pathlib import Path

from polymarket.updown_slugs import parse_updown_slug

from .lease import DEFAULT_HOME_EXTENSION_S, Assignment, grace_floor

MAP_VERSION = 1

# Repo-root `orchestrator/assignments.json`, found from this file rather than
# from cwd: the daemon runs under systemd with WorkingDirectory=pmtrader, and a
# relative path would resolve to the wrong place exactly when nobody is looking.
DEFAULT_MAP_PATH = Path(__file__).resolve().parents[2] / "orchestrator" / "assignments.json"

MAP_PATH_ENV = "PMT_FLEET_MAP"
NODE_ENV = "PMT_FLEET_NODE"


class MapInvalid(ValueError):
    """The assignment map is malformed. Fatal — never downgraded to a warning.

    A map this module cannot vouch for is a map nobody should act on, and the
    daemon exits 2 on it (the same REFUSED code pilot2 uses, so a systemd
    `RestartPreventExitStatus=2` keeps a config error visibly stopped instead
    of bouncing).
    """


@dataclasses.dataclass(frozen=True)
class FleetMap:
    nodes: dict[str, dict]
    series: dict[str, Assignment]
    # Whether leases are the live authority yet. FALSE through phase 1, when
    # the table holds heartbeats and no lease items at all.
    #
    # Explicit rather than inferred. Inferring it from "there are no lease
    # items" would read a catastrophically emptied table as a quiet phase-1
    # fleet — failure matrix row 17, the one hazard the protocol does not
    # defend against — and suppress the only alarm that would have caught it.
    leases_active: bool = False

    def node_active(self, node: str) -> bool:
        """Whether `node` is expected to be running the doctor right now.

        A node that has not been brought up yet is not a dark node. The EU box
        cannot beat until its IAM policy is approved and attached, and a
        checker that pages about that every cadence until then is a checker
        nobody leaves running.
        """
        return bool((self.nodes.get(node) or {}).get("doctor_active", True))

    def for_node(self, node: str) -> list[Assignment]:
        """Every series `node` may ever hold, home or failover."""
        return [a for a in self.series.values() if a.covers(node)]

    def home_of(self, node: str) -> list[Assignment]:
        return [a for a in self.series.values() if a.home == node]

    def failover_of(self, node: str) -> list[Assignment]:
        return [a for a in self.series.values() if a.failover == node]


def series_window_dur_s(series: str) -> int | None:
    """Window duration implied by the series key itself.

    The slug format encodes it (`btc-updown-15m-<start>`), so a map that
    declares a duration disagreeing with its own key is a typo we can catch
    for free — and a wrong duration here mis-sizes the grace budget, which is
    the one number in this system that must not be wrong.
    """
    w = parse_updown_slug(f"{series}-0")
    return None if w is None else w["dur_s"]


def load(path: str | os.PathLike | None = None) -> FleetMap:
    p = Path(path or os.environ.get(MAP_PATH_ENV) or DEFAULT_MAP_PATH)
    try:
        raw = json.loads(p.read_text())
    except FileNotFoundError as e:
        raise MapInvalid(f"no assignment map at {p}") from e
    except json.JSONDecodeError as e:
        raise MapInvalid(f"{p}: not valid JSON: {e}") from e
    return parse(raw, source=str(p))


def parse(raw: dict, *, source: str = "<memory>") -> FleetMap:
    if raw.get("version") != MAP_VERSION:
        raise MapInvalid(
            f"{source}: version {raw.get('version')!r}, expected {MAP_VERSION}. "
            "Refusing to guess at a format this code does not know."
        )
    nodes = raw.get("nodes") or {}
    if not isinstance(nodes, dict) or not nodes:
        raise MapInvalid(f"{source}: 'nodes' must be a non-empty object")

    series: dict[str, Assignment] = {}
    for key, spec in (raw.get("series") or {}).items():
        if not isinstance(spec, dict):
            raise MapInvalid(f"{source}: series {key!r} must be an object")
        dur = spec.get("window_dur_s") or series_window_dur_s(key)
        if not dur:
            raise MapInvalid(
                f"{source}: series {key!r} is not a parseable updown series key and "
                "declares no window_dur_s — the grace budget cannot be sized without one"
            )
        series[key] = Assignment(
            series=key,
            home=spec.get("home", ""),
            failover=spec.get("failover") or None,
            window_dur_s=float(dur),
            grace_s=None if spec.get("grace_s") is None else float(spec["grace_s"]),
            home_extension_s=float(spec.get("home_extension_s", DEFAULT_HOME_EXTENSION_S)),
            arm_template=spec.get("arm_template") or None,
            failover_priority=int(spec.get("failover_priority", 100)),
        )

    fm = FleetMap(nodes=nodes, series=series, leases_active=bool(raw.get("leases_active", False)))
    problems = validate(fm)
    if problems:
        raise MapInvalid(f"{source}:\n  - " + "\n  - ".join(problems))
    return fm


def validate(fm: FleetMap) -> list[str]:
    """Every reason this map should not be acted on. Empty list = good."""
    problems: list[str] = []
    if not fm.series:
        problems.append("no series assigned — the map does nothing")

    keys = sorted(fm.series)
    for a in fm.series.values():
        if not a.home:
            problems.append(f"{a.series}: no home node")
        elif a.home not in fm.nodes:
            problems.append(f"{a.series}: home {a.home!r} is not a declared node")
        if a.failover is not None:
            if a.failover not in fm.nodes:
                problems.append(f"{a.series}: failover {a.failover!r} is not a declared node")
            if a.failover == a.home:
                problems.append(
                    f"{a.series}: failover is the same node as home ({a.home}) — "
                    "that is not a failover, it is a typo"
                )

        declared = series_window_dur_s(a.series)
        if declared is not None and abs(declared - a.window_dur_s) > 0.5:
            problems.append(
                f"{a.series}: window_dur_s={a.window_dur_s:.0f} contradicts the "
                f"{declared}s the series key itself encodes"
            )

        floor = grace_floor(a.window_dur_s)
        if a.effective_grace_s() < floor:
            problems.append(
                f"{a.series}: grace_s={a.effective_grace_s():.0f} is below the "
                f"{floor:.0f}s floor for a {a.window_dur_s:.0f}s window. Rounding this "
                "up silently is not on offer: a grace that does not cover the old "
                "holder's stop is how two engines end up on one book."
            )
        if a.home_extension_s < 0:
            problems.append(f"{a.series}: home_extension_s must not be negative")
        # A failover with no template is a plan to take a lease and then not
        # know what to arm — the takeover would succeed and trade nothing,
        # which looks identical to a takeover that failed.
        if a.failover and not a.arm_template:
            problems.append(
                f"{a.series}: names a failover ({a.failover}) but no arm_template — "
                "the covering node would hold the lease and have nothing to arm"
            )

    # The prefix trap. Prefix matching is what makes the engine's allowlist work
    # across window suffixes, and it is also what makes an overlapping pair of
    # series keys a partition that does not partition.
    for i, k in enumerate(keys):
        for other in keys[i + 1:]:
            if other.startswith(k):
                a, b = fm.series[k], fm.series[other]
                problems.append(
                    f"series key {k!r} is a prefix of {other!r} — PMENGINE_SERIES_ALLOWLIST "
                    f"matches by prefix, so allowing {k!r} silently allows {other!r} "
                    f"(homes: {a.home} and {b.home}). Use disjoint keys."
                )
    return problems


def this_node(explicit: str | None = None) -> str:
    """This node's fleet id.

    Explicit flag, then `PMT_FLEET_NODE`, then the hostname. The hostname
    fallback is a convenience for the desktop and a hazard everywhere else, so
    the daemon prints which of the three it used at startup.
    """
    if explicit:
        return explicit
    env = os.environ.get(NODE_ENV)
    if env:
        return env
    import socket

    return socket.gethostname().split(".")[0]
