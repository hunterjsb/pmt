"""`pilot2` — run | grade | status | series.

argparse, not click: this is a standalone service with three verbs and its
exit code is a gate, so the smallest possible entry point is the right one.

LIVE IS OFF UNLESS BOTH SWITCHES ARE THROWN. `--live` alone runs shadow and
says so; `PILOT2_LIVE=1` alone runs shadow and says so. Both, and the
configured series are checked against every engine-owned series before a client
is even built — a refusal there is fatal, not a warning.
"""

from __future__ import annotations

import argparse
import json
import signal
import sys
import threading
import time

from . import books, execution, state, status
from . import grade as grade_mod
from . import series as series_mod


def _log(msg: str) -> None:
    print(f"{time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())} {msg}", flush=True)


def cmd_run(args) -> int:
    from . import service, stream

    execution.load_env_files()
    want_live = execution.live_enabled(args.live)
    if args.live and not want_live:
        _log("--live given but PILOT2_LIVE=1 is not set — running SHADOW. "
             "Both switches are required.")

    try:
        live_series = series_mod.live_series() if want_live else []
    except series_mod.SeriesRefused as e:
        _log(f"REFUSED: {e}")
        return 2

    clob = None
    if want_live:
        try:
            creds = execution.read_creds()
            clob = execution.build_client()
        except execution.LiveRefused as e:
            _log(f"REFUSED: {e}")
            return 2
        except Exception as e:  # noqa: BLE001
            _log(f"could not build the trading client: {type(e).__name__}: {e}")
            return 2
        _log(f"LIVE ARMED — {json.dumps(creds.describe())} series={','.join(live_series)}")

    st = stream.StreamState()
    stop = threading.Event()
    feed = threading.Thread(target=stream.run_feed, args=(st, stop),
                            kwargs={"log": _log}, daemon=True, name="pilot2-stream")
    feed.start()

    pilot = service.Pilot(home=args.home, live=want_live, live_series=live_series,
                          stream=st, clob_client=clob, log=_log)

    def _handle(signum, _frame):
        _log(f"signal {signal.Signals(signum).name} — closing")
        stop.set()

    signal.signal(signal.SIGTERM, _handle)
    signal.signal(signal.SIGINT, _handle)

    if args.duration:
        threading.Timer(args.duration, stop.set).start()
    rc = pilot.run(stop, interval_s=args.interval)
    stop.set()
    feed.join(timeout=5)
    return rc


def cmd_grade(args) -> int:
    summary = grade_mod.run(args.home, log=_log)
    if args.json:
        print(json.dumps(summary, indent=2))
    return 0


def cmd_status(args) -> int:
    s = status.summarise(args.home, since_s=args.since)
    if args.json:
        print(json.dumps(s, indent=2))
    else:
        print(status.render(s))
        print("  series")
        print(status.series_view())
    return 0


def cmd_series(args) -> int:
    print(status.series_view())
    try:
        series_mod.live_series()
    except series_mod.SeriesRefused:
        return 2
    return 0


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog="pilot2",
        description="Strategy 2.0 interim pilot — calibrated terminal physics, "
                    "blended with the book, EV-gated. Shadow by default.")
    ap.add_argument("--home", default=None,
                    help=f"state directory (default ${state.HOME_ENV} or ~/.pmt/pilot2)")
    sub = ap.add_subparsers(dest="cmd", required=True)

    r = sub.add_parser("run", help="the poll loop (shadow unless --live AND PILOT2_LIVE=1)")
    r.add_argument("--live", action="store_true",
                   help="arm real orders. Requires PILOT2_LIVE=1 as well.")
    r.add_argument("--interval", type=float, default=books.POLL_INTERVAL_S,
                   help="book poll seconds (default %(default)s)")
    r.add_argument("--duration", type=float, default=None,
                   help="stop after N seconds (testing; default: forever)")
    r.set_defaults(func=cmd_run)

    g = sub.add_parser("grade", help="score settled shadow decisions and refit the blend weight")
    g.add_argument("--json", action="store_true")
    g.set_defaults(func=cmd_grade)

    s = sub.add_parser("status", help="what has been seen, would have been traded, and is held")
    s.add_argument("--since", type=float, default=None, help="only the last N seconds")
    s.add_argument("--json", action="store_true")
    s.set_defaults(func=cmd_status)

    v = sub.add_parser("series", help="print the series partition and validate it (exit 2 if refused)")
    v.set_defaults(func=cmd_series)
    return ap


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
