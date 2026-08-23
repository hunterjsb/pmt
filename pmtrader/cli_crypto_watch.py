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
from polymarket import positions, tape, wallet
from watch_ui import (
    _SB_EMPTY, _cbreak_stdin, _restore_stdin, _wait_key, build_arms_table,
    build_header_panel, build_help_modal, build_windows_table, header_height,
    window_rows, windows_title,
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


# Fetch cadences — keep in sync with watch_ui._REFRESH_LINE.
ENGINE_EVERY_S = 2.0
SB_EVERY_S = 10.0
ODDS_EVERY_S = 30.0       # per-position marks: a display feed, not a control input
BAL_EVERY_S = 60.0
WORKER_INTERVAL_S = 0.25  # how often the worker checks what's due
# 'q' must feel instant. An idle worker exits its wait immediately; one stuck
# mid-fetch is simply abandoned (daemon thread, the process is leaving anyway)
# rather than holding the operator's terminal for the length of an HTTP call.
WORKER_JOIN_S = 0.25
KEY_POLL_S = 0.05         # 20Hz key polling — the perceived-latency budget
RENDER_EVERY_S = 1.0      # repaint cadence when no key changed anything

# Windows panel geometry. WINDOWS_MAX_ROWS is a VIEW cap, and the panel title
# names it ("N of M") — a cap the operator can't see reads as a dropped
# window, which is the confusion this panel exists to end.
# 11, not 8: the panel absorbed the recent-windows strip's three rows, and it
# now also carries the fleet's LIVE windows at the top, so a five-arm fleet
# spends five rows before the decided tail starts. At 8 the tail a roll cycle
# left behind was pushed off before the operator saw how any of it resolved.
WINDOWS_MAX_ROWS = 11
WINDOWS_CHROME = 6        # panel border (2) + table border/header/rule (4)
MIN_TAPE_ROWS = 6         # the tape never gets squeezed below this for a window row
HEAD_MIN_H = 5            # header border + the four rows it always paints


def windows_rows_shown(console_h: int, arms_h: int, n_rows: int,
                       head_h: int = HEAD_MIN_H) -> int:
    """Window rows this screen can hold: what there is, capped, and never so
    many that the tape stops being readable.

    The panel is BUILT to this number rather than clipped to it — a table cut
    off mid-box below its last visible row reads as a crash, not as a cap —
    and the panel title repeats it, so a short screen says "6 of 14" instead
    of silently looking like the whole ledger.

    `head_h` is the header panel's live height (watch_ui.header_height): it
    grows a row for the settlement feed and for a render error, and the tape
    is what pays for that, never the windows panel's floor of one row.
    """
    room = console_h - head_h - arms_h - MIN_TAPE_ROWS - WINDOWS_CHROME
    return max(1, min(WINDOWS_MAX_ROWS, n_rows, room))


def handle_key(key: str | None, show_help: bool) -> tuple[bool, bool, bool]:
    """One keypress -> `(quit, show_help, dirty)`.

    THE key contract. watch_ui.WATCH_KEYS lists exactly what this reacts to and
    a test drives it both ways, so a key with no panel line (or a panel line
    with no key behind it) fails rather than ships.

    `q` closes the modal before it quits: a foreground panel that the quit key
    punched straight through would cost the operator their dashboard on a
    stray press. Ctrl-C is a signal, not a key, and still leaves from anywhere.
    """
    if key == "h":
        return False, not show_help, True
    if key == "\x1b":
        # Dirty only if it actually closed something — an idle esc must not
        # force a repaint the loop's 1Hz cadence didn't ask for.
        return False, False, show_help
    if key == "q":
        return (False, False, True) if show_help else (True, show_help, False)
    return False, show_help, False


class WatchState:
    """The single hand-off point between the fetch thread and the render loop.

    The worker never mutates a published value in place — it builds a whole
    new result object and swaps it in — so a reader can never catch a
    half-built scoreboard. read() takes the lock and copies the mapping,
    handing the renderer one internally consistent snapshot per frame.
    """

    _FIELDS = ("status", "bal", "sb", "sb_stale", "sb_fetched_at", "err", "odds")

    def __init__(self, sb: dict | None = None) -> None:
        self._lock = threading.Lock()
        self._d: dict = {
            "status": {}, "bal": {},
            "sb": dict(_SB_EMPTY) if sb is None else sb,
            # Not stale, just not fetched yet: sb_fetched_at None already
            # renders as the header's "—" data-age, which is the honest cue
            # while the first walk is still in flight.
            "sb_stale": False, "sb_fetched_at": None, "err": None,
            # Current per-position marks; empty until the first slow fetch
            # lands, and the trades table renders "—" for a mark it lacks.
            "odds": {},
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
        self._due: dict[str, float] = {"status": 0.0, "sb": 0.0, "bal": 0.0,
                                       "odds": 0.0}

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

    def fetch_odds(self) -> None:
        # A DISPLAY feed on the slowest cadence that still answers "what is
        # the position worth now" — never an input to grading (the wallet is
        # ground truth) and never a call into the engine's control plane.
        rows = positions.fetch_positions(wallet.funder_address())
        self.state.update(odds=positions.current_odds(rows))

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

    def _odds_failed(self, exc: BaseException) -> None:
        # Blank, not last-good: a mark is a live quote, and a stale one beside
        # a live entry price would read as "the position hasn't moved". The
        # trades table paints "—" and says nothing rather than something wrong.
        self.state.update(odds={})

    def tick(self, now: float) -> None:
        """Run whatever is due at `now`. Never raises."""
        for name, every, fetch, failed in (
            ("status", ENGINE_EVERY_S, self.fetch_status, self._status_failed),
            ("sb", SB_EVERY_S, self.fetch_sb, self._sb_failed),
            ("odds", ODDS_EVERY_S, self.fetch_odds, self._odds_failed),
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
                   "All-time P&L, and the riding/windows-table figures, "
                   "always walk the full wallet history regardless of this.")
def crypto_watch(since: float | None) -> None:
    """Full-screen live dashboard: risk header + arms + windows + streaming tape."""
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

    def windows_panel(rows: int) -> Panel:
        sb, arms = snap["sb"], snap["status"].get("arms")
        return Panel(build_windows_table(sb, _t.time(), arms=arms, limit=rows,
                                         odds=snap.get("odds")),
                     title=windows_title(sb, arms, rows),
                     # The strip carried the "h" hint; it lives on the panel
                     # that gained the strip's glyphs.
                     subtitle="[dim]h · controls[/dim]", border_style="dim")

    def _modal(width: int):
        """The controls panel centred over the whole screen — a modal reads as
        one because it is the only thing on it, not because Rich composites."""
        from rich.align import Align
        return Align.center(build_help_modal(width), vertical="middle")

    def tape_panel(height: int) -> Panel:
        shown = list(lines)[-max(height - 2, 1):]
        body = Text.from_ansi("\n".join(shown))
        # One tape record, one row. A wrapped line silently costs the panel a
        # second row, so the "last N lines" arithmetic above stops holding and
        # records scroll out of a panel that looks like it has space.
        body.no_wrap, body.overflow = True, "ellipsis"
        return Panel(body, title="tape", border_style="dim")

    layout = Layout()
    layout.split_column(
        Layout(name="head", size=HEAD_MIN_H),
        Layout(name="arms", size=10),
        Layout(name="windows", size=WINDOWS_MAX_ROWS + WINDOWS_CHROME),
        Layout(name="tape", ratio=1),
    )

    show_help = False
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
                quit_now, show_help, dirty = handle_key(key, show_help)
                if quit_now:
                    break
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
                    # The header grows a row for the settlement feed and one
                    # for a render error; size the slot to what it will paint
                    # or Rich clips the row that says what broke.
                    layout["head"].size = header_height(snap, render_err)
                    n_win = windows_rows_shown(
                        live.console.size.height, layout["arms"].size,
                        len(window_rows(snap["sb"], snap["status"].get("arms"))),
                        layout["head"].size)
                    layout["windows"].size = n_win + WINDOWS_CHROME
                    layout["head"].update(header())
                    layout["arms"].update(arms_table())
                    layout["windows"].update(windows_panel(n_win))
                    h = (live.console.size.height - layout["head"].size
                         - layout["arms"].size - layout["windows"].size)
                    layout["tape"].update(tape_panel(h))
                    render_err = None
                except KeyboardInterrupt:
                    raise
                except (Exception, SystemExit) as e:
                    render_err = f"{type(e).__name__}: {e}"[:100]
                    try:
                        layout["head"].size = header_height(snap, render_err)
                        layout["head"].update(header())
                    except Exception:
                        pass
                # The modal is FOREGROUND: it takes the screen while open. The
                # dashboard behind it is still rebuilt every frame above (and
                # the fetch worker never pauses), so dismissing restores the
                # live frame, not the one that was up when 'h' was pressed.
                live.update(_modal(live.console.size.width) if show_help else layout)
                live.refresh()
    except KeyboardInterrupt:
        pass
    finally:
        stop.set()
        worker.join(timeout=WORKER_JOIN_S)  # daemon thread — never hang the exit
        _restore_stdin(saved_term)
