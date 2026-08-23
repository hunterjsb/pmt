"""`pmt crypto watch` — the full-screen live dashboard's fetch/render split.

Two threads and nothing else: a main loop that reads keys and repaints with
ZERO network, and one daemon worker that owns every network call and publishes
whole result objects into a WatchState. Why it is split at all:
docs/LESSONS.md#L28.

The scoreboard the worker fetches is `cli_crypto_stats._tape_scoreboard` —
literally the function `pmt crypto stats` runs. Watch is a VIEW of the same
acquisition, never a second one; see that module's docstring for what the
alternative cost.

Pairs with watch_ui.py, which owns every render function this uses.
"""

from __future__ import annotations

import threading
import time

import click
from rich.table import Table

import watch_ui
from cli_common import _api, _parse_since
from cli_crypto_stats import _tape_scoreboard
from engine import post as _engine_post
from polymarket import tape
from watch_ui import (
    _SB_EMPTY, _cbreak_stdin, _controls_panel, _restore_stdin, _wait_key,
    build_arms_table, build_header_panel, build_trades_table,
    build_windows_strip, trade_rows, trades_title,
)


# ---------- watch: the render/fetch split ----------
#
# The dashboard runs two threads and nothing else:
#
#   main   — input + render, ZERO network. Polls the tty at 20Hz (the select
#            timeout IS the loop's pacing) and repaints at 1Hz, or instantly
#            when a key changed UI state.
#   worker — one daemon thread owning every network call, each on its own
#            cadence, publishing whole result objects into a WatchState.
#
# Why it is split at all: docs/LESSONS.md#L28.


# Fetch cadences — keep in sync with the line in _controls_panel().
ENGINE_EVERY_S = 2.0
SB_EVERY_S = 10.0
BAL_EVERY_S = 60.0
WORKER_INTERVAL_S = 0.25  # how often the worker checks what's due
# 'q' must feel instant. An idle worker exits its wait immediately; one stuck
# mid-fetch is simply abandoned (daemon thread, the process is leaving anyway)
# rather than holding the operator's terminal for the length of an HTTP call.
WORKER_JOIN_S = 0.25
KEY_POLL_S = 0.05         # 20Hz key polling — the perceived-latency budget
RENDER_EVERY_S = 1.0      # repaint cadence when no key changed anything

# Trades panel geometry. TRADES_MAX_ROWS is a VIEW cap, and the panel title
# names it ("last N decided · M riding") — a cap the operator can't see reads
# as a dropped trade, which is the confusion this panel exists to end.
TRADES_MAX_ROWS = 6
TRADES_CHROME = 6         # panel border (2) + table border/header/rule (4)
MIN_TAPE_ROWS = 6         # the tape never gets squeezed below this for a trade row


class WatchState:
    """The single hand-off point between the fetch thread and the render loop.

    The worker never mutates a published value in place — it builds a whole
    new result object and swaps it in — so a reader can never catch a
    half-built scoreboard. read() takes the lock and copies the mapping,
    handing the renderer one internally consistent snapshot per frame.
    """

    _FIELDS = ("status", "bal", "sb", "sb_stale", "sb_fetched_at", "err")

    def __init__(self, sb: dict | None = None) -> None:
        self._lock = threading.Lock()
        self._d: dict = {
            "status": {}, "bal": {},
            "sb": dict(_SB_EMPTY) if sb is None else sb,
            # Not stale, just not fetched yet: sb_fetched_at None already
            # renders as the header's "—" data-age, which is the honest cue
            # while the first walk is still in flight.
            "sb_stale": False, "sb_fetched_at": None, "err": None,
        }

    def update(self, **kw) -> None:
        unknown = set(kw) - set(self._FIELDS)
        if unknown:
            raise KeyError(f"unknown WatchState field(s): {sorted(unknown)}")
        with self._lock:
            self._d.update(kw)

    def read(self) -> dict:
        with self._lock:
            return dict(self._d)


class WatchFetcher:
    """Every network call the watch dashboard makes, on one daemon thread.

    Each source has its own cadence and its own belt: a failure keeps the
    last good value and surfaces as staleness in the header, never as a
    traceback and never as a dead dashboard. Being on a worker thread is
    what buys the scoreboard the right to do the honest full wallet walk —
    the render loop never waits on it. See fetch_sb.
    """

    def __init__(self, state: WatchState, sliding_floor: float) -> None:
        self.state = state
        self.sliding_floor = sliding_floor
        self._due: dict[str, float] = {"status": 0.0, "sb": 0.0, "bal": 0.0}

    # -- individual fetches: each may raise; tick() belts them --

    def fetch_status(self) -> None:
        # engine.post() prints its own red error before sys.exit()ing. Let it —
        # Live's alternate screen paints over it on the next frame. Do NOT hush
        # it with contextlib.redirect_stdout: docs/LESSONS.md#L29.
        status = _engine_post("/strategies/updown/command", {"action": "status"})
        self.state.update(status=status if isinstance(status, dict) else {})

    def fetch_sb(self) -> None:
        # THE SAME function `pmt crypto stats` runs — one code path, one
        # truth, pinned by a named test. The incremental ledger this replaced
        # disagreed with stats five separate ways; polymarket/wallet.py holds
        # that autopsy, and it is the thing to read before "optimizing" here.
        sb = _tape_scoreboard(0.0, sliding_floor=self.sliding_floor)
        self.state.update(sb=sb, sb_stale=False, sb_fetched_at=time.time(), err=None)

    def fetch_bal(self) -> None:
        self.state.update(bal=_api().get_usdc_balance() or {})

    # -- failure handling: last good value + a visible marker --

    def _status_failed(self, exc: BaseException) -> None:
        # Deliberately NOT the last good arms: a stale committed-$ figure on a
        # trading dashboard is worse than the arms table's red "engine
        # unreachable or no arms", which is what an empty status renders as.
        self.state.update(status={})

    def _sb_failed(self, exc: BaseException) -> None:
        self.state.update(sb_stale=True, err=f"scoreboard: {type(exc).__name__}"[:100])

    def _bal_failed(self, exc: BaseException) -> None:
        pass  # keep the last capital figure; a flaky balance call shouldn't blank it

    def tick(self, now: float) -> None:
        """Run whatever is due at `now`. Never raises."""
        for name, every, fetch, failed in (
            ("status", ENGINE_EVERY_S, self.fetch_status, self._status_failed),
            ("sb", SB_EVERY_S, self.fetch_sb, self._sb_failed),
            ("bal", BAL_EVERY_S, self.fetch_bal, self._bal_failed),
        ):
            if now < self._due[name]:
                continue
            self._due[name] = now + every
            try:
                fetch()
            except (Exception, SystemExit) as e:
                # engine.post() sys.exit()s on failure — SystemExit isn't an
                # Exception, so it must be named explicitly.
                failed(e)

    def loop(self, stop: threading.Event, interval: float = WORKER_INTERVAL_S) -> None:
        """Thread body. stop.wait() returns the moment the flag is set, so
        quitting never waits out a full interval."""
        while not stop.is_set():
            try:
                self.tick(time.time())
            except Exception as e:  # a bug in tick() itself must not kill the feed
                self.state.update(err=f"{type(e).__name__}: {e}"[:100])
            stop.wait(interval)


@click.command("watch")
@click.option("--since", type=float, default=None,
              help="Sliding-window floor for the header's recent P&L: "
                   "hours-ago if small, raw unix epoch if large (default: "
                   "last 6h — a live dashboard cares about the recent pulse). "
                   "All-time P&L, and the riding/recent-windows figures, "
                   "always walk the full wallet history regardless of this.")
def crypto_watch(since: float | None) -> None:
    """Full-screen live dashboard: risk header + arms + trades + streaming tape."""
    import time as _t
    from collections import deque
    from datetime import datetime, timezone

    from rich.layout import Layout
    from rich.live import Live
    from rich.panel import Panel
    from rich.text import Text

    WATCH_DEFAULT_LOOKBACK_H = 6.0  # sliding recent window — it's a live dashboard, not the ledger

    collapser = watch_ui.TapeCollapser()

    floor = _parse_since(since) if since else (_t.time() - WATCH_DEFAULT_LOOKBACK_H * 3600)
    floor_label = ("all time" if floor <= 0 else
                   datetime.fromtimestamp(floor, tz=timezone.utc).strftime("since %m-%d %H:%MZ"))
    lines: deque = deque(maxlen=200)
    offset = 0
    try:
        with open(tape.UPDOWN_TAPE) as fh:
            for raw in fh.readlines()[-120:]:
                collapser.add(raw, lines)
            offset = fh.tell()
    except OSError:
        pass

    # All network lives on the worker; the loop below only ever reads this
    # snapshot, so the first paint is immediate (data age "—" until the first
    # wallet walk lands). See docs/LESSONS.md#L28.
    state = WatchState()
    stop = threading.Event()
    fetcher = WatchFetcher(state, floor)
    worker = threading.Thread(target=fetcher.loop, args=(stop,),
                              name="pmt-watch-fetch", daemon=True)
    snap = state.read()
    render_err: str | None = None

    def header() -> Panel:
        return build_header_panel(snap, floor_label, render_err)

    def arms_table() -> Table:
        return build_arms_table(snap["status"].get("arms"), _t.time())

    def strip_panel() -> Panel:
        sb = snap["sb"]
        return Panel(Text.from_markup(build_windows_strip(sb.get("windows"),
                                                          sb.get("riding_windows"))),
                     title="recent windows", subtitle="[dim]h = controls[/dim]",
                     border_style="dim")

    def trades_panel() -> Panel:
        sb = snap["sb"]
        return Panel(build_trades_table(sb, _t.time(), limit=TRADES_MAX_ROWS),
                     title=trades_title(sb), border_style="dim")

    def trades_size(console_h: int, arms_h: int) -> int:
        """Rows for the trades panel: what it actually has, capped, and never
        so many that the tape stops being readable."""
        room = console_h - 4 - 3 - arms_h - MIN_TAPE_ROWS - TRADES_CHROME
        n = min(TRADES_MAX_ROWS, len(trade_rows(snap["sb"])), room)
        return max(1, n) + TRADES_CHROME

    def tape_panel(height: int) -> Panel:
        shown = list(lines)[-max(height - 2, 1):]
        return Panel(Text.from_ansi("\n".join(shown)), title="tape", border_style="dim")

    layout = Layout()
    layout.split_column(
        Layout(name="head", size=4),
        Layout(name="arms", size=10),
        Layout(name="trades", size=TRADES_MAX_ROWS + TRADES_CHROME),
        Layout(name="strip", size=3),
        Layout(name="tape", ratio=1),
    )

    show_controls = False
    next_render = 0.0
    saved_term = _cbreak_stdin()
    worker.start()
    try:
        with Live(layout, refresh_per_second=4, screen=True) as live:
            while True:
                # The ONLY wait in this loop, and it's the key wait: 20Hz, so
                # 'q'/'h' are seen within ~50ms no matter what the worker is
                # doing. Nothing below this line touches the network.
                key = _wait_key(KEY_POLL_S)
                if key == "q":
                    break
                dirty = False
                if key == "h":
                    show_controls = not show_controls
                    dirty = True  # a toggle repaints now, not on the next second
                now = time.time()
                if not dirty and now < next_render:
                    continue
                next_render = now + RENDER_EVERY_S
                snap = state.read()
                # Final belt: neither the tape file nor a render bug may tear
                # the dashboard down. Note it in the header and keep painting;
                # only Ctrl+C or 'q' stops this.
                try:
                    # Local file, seek+read from the last offset: sub-
                    # millisecond, so the render never waits on it. Belted
                    # because a torn mid-write line can be undecodable bytes,
                    # which is a UnicodeDecodeError, not an OSError.
                    try:
                        with open(tape.UPDOWN_TAPE) as fh:
                            fh.seek(offset)
                            for raw in fh:
                                collapser.add(raw, lines)
                            offset = fh.tell()
                    except OSError:
                        pass
                    layout["arms"].size = max(len(snap["status"].get("arms") or {}), 1) + 4
                    layout["trades"].size = trades_size(live.console.size.height,
                                                         layout["arms"].size)
                    layout["head"].update(header())
                    layout["arms"].update(arms_table())
                    layout["trades"].update(trades_panel())
                    layout["strip"].update(
                        _controls_panel() if show_controls else strip_panel())
                    h = (live.console.size.height - 4 - 3
                         - layout["arms"].size - layout["trades"].size)
                    layout["tape"].update(tape_panel(h))
                    render_err = None
                except KeyboardInterrupt:
                    raise
                except (Exception, SystemExit) as e:
                    render_err = f"{type(e).__name__}: {e}"[:100]
                    try:
                        layout["head"].update(header())
                    except Exception:
                        pass
                live.refresh()
    except KeyboardInterrupt:
        pass
    finally:
        stop.set()
        worker.join(timeout=WORKER_JOIN_S)  # daemon thread — never hang the exit
        _restore_stdin(saved_term)
