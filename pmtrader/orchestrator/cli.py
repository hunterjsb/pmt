"""`pmt-fleet` — beat | check | map | table | kill-switch.

argparse, not click, and a standalone entry point rather than a `pmt`
subcommand: this is a long-running service with its own unit and its own state,
and the house rule (pilot2's, and it applies unchanged here) is that a service
whose exit code is a gate wants the smallest possible entry point. It also
keeps the doctor one typo further from the fleet's arms — and `pmt crypto
fleet` already means something else entirely (the R7 un-decided exposure cap),
so the name was taken.

PHASE 1 PLACES NO ORDERS AND TOUCHES NO LEASE. `beat` writes heartbeats and
nothing else; `check` reads. The lease protocol in `lease.py` is built and
tested but nothing calls its mutating path yet — that is phase 2, and it does
not get built until Hunter has read orchestrator/DESIGN.md.
"""

from __future__ import annotations

import argparse
import json
import signal
import sys
import time
from pathlib import Path

from . import check as check_mod
from . import heartbeat as hb_mod
from . import notify as notify_mod
from .assign import MapInvalid, load, this_node, validate
from .store import CASFailed, FleetStore, StoreUnavailable

DEFAULT_INTERVAL_S = 30.0
DEFAULT_ARMS = [
    "~/.pmt/engine/arms-state.json",
    "~/.pmt/engine/updown2-arms-state.json",
]


def _log(msg: str) -> None:
    print(f"{time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())} {msg}", flush=True)


def _store(args) -> FleetStore:
    return FleetStore(table=args.table, region=args.region)


def _map(args):
    return load(args.map)


# --- beat ------------------------------------------------------------------

def cmd_beat(args) -> int:
    """Write this node's heartbeat every `--interval` seconds, forever.

    Nothing here is allowed to be fatal except a bad map. A store outage, an
    unreachable engine, a missing arms file — all of those are conditions to
    REPORT, and a daemon that exits on them is a daemon that is not running the
    next time someone needs to know.
    """
    try:
        fmap = _map(args)
    except MapInvalid as e:
        _log(f"REFUSED: {e}")
        return check_mod.REFUSED

    node = this_node(args.node)
    if node not in fmap.nodes:
        _log(
            f"REFUSED: this node is {node!r}, which the map does not declare "
            f"(known: {', '.join(sorted(fmap.nodes))}). Set PMT_FLEET_NODE or --node."
        )
        return check_mod.REFUSED

    store = _store(args)
    arms = [Path(p).expanduser() for p in (args.arms or DEFAULT_ARMS)]
    _log(
        f"beating as {node!r} every {args.interval:.0f}s -> {args.table}/{args.region}; "
        f"arms={','.join(str(a) for a in arms)}"
    )
    _log(
        f"home: {', '.join(a.series for a in fmap.home_of(node)) or '-'} | "
        f"failover: {', '.join(a.series for a in fmap.failover_of(node)) or '-'}"
    )

    stopping = {"now": False}

    def _on_term(signum, _frame):
        _log(f"signal {signal.Signals(signum).name} — writing the clean-shutdown beat")
        stopping["now"] = True

    signal.signal(signal.SIGTERM, _on_term)
    signal.signal(signal.SIGINT, _on_term)

    def beat(shutdown: bool) -> None:
        status = hb_mod.engine_snapshot()
        hb = hb_mod.build(
            node,
            arms_paths=arms,
            status=status,
            min_balance=args.min_balance,
            shutdown=shutdown,
        )
        try:
            store.put_heartbeat(hb)
        except CASFailed:
            # A newer beat for this node already landed — a second daemon, or a
            # retry arriving late. The row is more current than ours; leave it.
            _log("beat skipped: a newer heartbeat already landed for this node")
            return
        except StoreUnavailable as e:
            _log(f"beat failed (store): {e}")
            return
        off = store.clock_offset_s
        clock = "unmeasured" if off is None else f"{off:+.2f}s"
        _log(
            f"beat ok engine={'up' if hb['engine_active'] else 'down'} "
            f"feed={hb['feed_age']:.1f}s balance={'ok' if hb['balance_ok'] else 'LOW'} "
            f"armed={len(hb['series_held'])} clock={clock}"
        )

    deadline = 0.0
    while not stopping["now"]:
        if time.time() >= deadline:
            beat(shutdown=False)
            deadline = time.time() + args.interval
            if args.once:
                return check_mod.OK
        if args.duration and time.time() > args.duration_started + args.duration:
            break
        time.sleep(0.5)

    beat(shutdown=True)
    _log("clean shutdown recorded — this will not page")
    return check_mod.OK


# --- check -----------------------------------------------------------------

def cmd_check(args) -> int:
    try:
        fmap = _map(args)
    except MapInvalid as e:
        print(f"pmt fleet — REFUSED: {e}", file=sys.stderr)
        return check_mod.REFUSED

    rc, report, text = check_mod.run(
        _store(args), fmap, stale_after_s=args.stale_after, strict=args.strict
    )
    if args.json:
        print(json.dumps(report if report is not None else {"error": text}, default=str))
    else:
        print(text)

    if args.notify != "dry" or args.notify_cmd:
        _page_if_needed(args, report, rc)
    return rc


def _page_if_needed(args, report, rc: int) -> None:
    """Raise or clear the mubs attention, with the latch discipline mubs uses.

    The latch is only ever set on a page that was ACCEPTED. Latching a page
    that failed to send is how a fleet goes quietly dark — it silences the
    retry that would have got through.
    """
    n = notify_mod.Notifier(args.notify, cmd=args.notify_cmd, log=_log)
    latch = notify_mod.read_latch()
    paged = bool(latch.get("paged"))

    if rc == check_mod.BLIND:
        # The doctor is blind, not the fleet sick. Say exactly that: a page
        # that claims the engines are down when we simply cannot see them
        # sends Hunter looking in the wrong place.
        if not paged and n.page("fleet doctor cannot reach the pmt-fleet store", node=args.node or ""):
            notify_mod.write_latch(True, detail="store unreachable")
        return

    if rc == check_mod.ATTENTION and report:
        detail = "; ".join(report["attention"])[: notify_mod.DETAIL_MAX]
        if not paged and n.page(detail, node=args.node or ""):
            notify_mod.write_latch(True, detail=detail)
        return

    if rc == check_mod.OK and paged:
        if n.resolve(node=args.node or ""):
            notify_mod.write_latch(False)


# --- map / table / kill-switch --------------------------------------------

def cmd_map(args) -> int:
    try:
        fmap = _map(args)
    except MapInvalid as e:
        print(f"REFUSED: {e}", file=sys.stderr)
        return check_mod.REFUSED
    problems = validate(fmap)
    print(f"nodes: {', '.join(sorted(fmap.nodes))}")
    for key in sorted(fmap.series):
        a = fmap.series[key]
        print(
            f"  {key:<20} home={a.home:<10} failover={str(a.failover):<10} "
            f"window={a.window_dur_s:.0f}s grace={a.effective_grace_s():.0f}s "
            f"home_ext={a.home_extension_s:.0f}s "
            f"arm={'yes' if a.arm_template else 'NONE'}"
        )
    if problems:
        print("\nPROBLEMS", file=sys.stderr)
        for p in problems:
            print(f"  - {p}", file=sys.stderr)
        return check_mod.REFUSED
    print("\nmap is valid")
    return check_mod.OK


def cmd_table(args) -> int:
    from .table import create, spec

    if args.dry_run:
        print(json.dumps(spec(args.table), indent=2))
        return check_mod.OK
    create(args.table, args.region, log=_log)
    return check_mod.OK


def cmd_kill_switch(args) -> int:
    store = _store(args)
    try:
        if args.on:
            store.set_failover_disabled(True)
            print("failover DISABLED fleet-wide — no node may claim a failover lease")
        elif args.off:
            store.set_failover_disabled(False)
            print("failover enabled")
        else:
            print("failover is " + ("DISABLED" if store.failover_disabled() else "enabled"))
    except StoreUnavailable as e:
        print(f"store unreachable: {e}", file=sys.stderr)
        return check_mod.BLIND
    return check_mod.OK


# --- parser ----------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    from .store import REGION, TABLE_NAME

    ap = argparse.ArgumentParser(
        prog="pmt-fleet",
        description="Cross-node health for the pmt trading fleet. Phase 1: reads and "
                    "heartbeats only — no leases are taken and no orders are placed.",
    )
    ap.add_argument("--table", default=TABLE_NAME)
    ap.add_argument("--region", default=REGION)
    ap.add_argument("--map", default=None, help="assignment map JSON (default: orchestrator/assignments.json)")
    ap.add_argument("--node", default=None, help="this node's fleet id (default: $PMT_FLEET_NODE, then hostname)")
    sub = ap.add_subparsers(dest="cmd", required=True)

    b = sub.add_parser("beat", help="write this node's heartbeat on a loop")
    b.add_argument("--interval", type=float, default=DEFAULT_INTERVAL_S)
    b.add_argument("--arms", action="append", default=None,
                   help="path to an arms-state.json (repeatable; defaults cover updown + updown2)")
    b.add_argument("--min-balance", type=float, default=hb_mod.DEFAULT_MIN_BALANCE_USDC)
    b.add_argument("--once", action="store_true", help="one beat, then exit")
    b.add_argument("--duration", type=float, default=0.0, help="stop after N seconds")
    b.set_defaults(func=cmd_beat)

    c = sub.add_parser("check", help="print fleet status; nonzero if it needs attention")
    c.add_argument("--stale-after", type=float, default=check_mod.DEFAULT_STALE_AFTER_S)
    c.add_argument("--strict", action="store_true",
                   help="treat a cleanly-shut-down node as needing attention too")
    c.add_argument("--json", action="store_true")
    c.add_argument("--notify", choices=["dry", "mubs", "cmd"], default="dry",
                   help="dry (print only), mubs (mubs-attention Lambda), cmd (--notify-cmd)")
    c.add_argument("--notify-cmd", default=None,
                   help="command receiving the alert JSON on stdin; nonzero exit = not sent")
    c.set_defaults(func=cmd_check)

    m = sub.add_parser("map", help="print and validate the assignment map")
    m.set_defaults(func=cmd_map)

    t = sub.add_parser("table", help="create the DynamoDB table (idempotent)")
    t.add_argument("--dry-run", action="store_true", help="print the CreateTable spec, create nothing")
    t.set_defaults(func=cmd_table)

    k = sub.add_parser("kill-switch", help="freeze or unfreeze all failover claims")
    k.add_argument("--on", action="store_true", help="freeze failover")
    k.add_argument("--off", action="store_true", help="unfreeze failover")
    k.set_defaults(func=cmd_kill_switch)

    return ap


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    args.duration_started = time.time()
    return args.func(args)
