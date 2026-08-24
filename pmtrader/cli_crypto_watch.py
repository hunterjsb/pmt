"""`pmt crypto watch` — the full-screen live dashboard's fetch/render split.

Two threads and nothing else: a main loop that reads keys and repaints with
ZERO network, and one daemon worker that owns every network call and publishes
whole result objects into a WatchState. Why it is split at all:
docs/LESSONS.md#L28.

The scoreboard the worker fetches is `cli_crypto_stats._tape_scoreboard` —
literally the function `pmt crypto stats` runs. Watch is a VIEW of the same
acquisition, never a second one; see that module's docstring for what the
alternative cost.

The tape panel has two sources behind one cursor (TapeFeed): the local file
the render loop seeks, and — when that file isn't this box's, because
PMENGINE_CONTROL_URL points down an SSM tunnel — the engine's `GET /tape` on
the worker.

Pairs with watch_ui.py, which owns every render function this uses.
"""

from __future__ import annotations

import json
import os
import select
import sys
import termios
import threading
import time
import tty

import click

import watch_ui
from cli_common import _api, _parse_since
from cli_crypto_stats import _tape_scoreboard
from engine import fetch as _engine_get
from engine import post as _engine_post
from polymarket import positions, regime, tape, wallet
from watch_ui import (
    _SB_EMPTY, build_header_panel, build_help_modal, build_windows_table,
    header_height, tape_title, window_rows, windows_title,
)


# ---------- terminal mode + key polling ----------

def _cbreak_stdin() -> tuple[int, list] | None:
    """Put stdin in cbreak (no line buffering, no echo) so a single 'q'
    keypress is visible without Enter — SIGINT stays enabled (cbreak, unlike
    raw mode, leaves ISIG alone), so Ctrl-C still works. None when stdin
    isn't a real tty (piped input, a test harness) — termios would just raise.
    """
    if not sys.stdin.isatty():
        return None
    fd = sys.stdin.fileno()
    old = termios.tcgetattr(fd)
    tty.setcbreak(fd)
    return fd, old


def _restore_stdin(saved: tuple[int, list] | None) -> None:
    """Undo _cbreak_stdin — must run even on an exception, or the shell is
    left echo-less after the dashboard exits."""
    if saved is None:
        return
    fd, old = saved
    try:
        termios.tcsetattr(fd, termios.TCSADRAIN, old)
    except Exception:
        pass


def _poll_key(timeout: float = 0.0) -> str | None:
    """The waiting keypress (lowercased) or None. `timeout` is the select
    wait in seconds; 0 (the default) polls without blocking at all.

    os.read on the raw fd, NEVER sys.stdin.read — see docs/LESSONS.md#L30.
    """
    if not sys.stdin.isatty():
        return None
    try:
        fd = sys.stdin.fileno()
        ready, _, _ = select.select([fd], [], [], timeout)
        if not ready:
            return None
        ch = os.read(fd, 1)
        return ch.decode(errors="ignore").lower() or None
    except Exception:
        return None


def _wait_key(timeout: float) -> str | None:
    """Wait up to `timeout` for one keypress — the watch loop's ONLY pacing.

    The loop sleeps inside select(), so a keypress wakes it within
    microseconds instead of sitting in the tty buffer behind a sleep(1) and
    whatever network work followed it. With no tty (piped input, a test
    harness) there's nothing to select on, so it just paces the loop.
    """
    if not sys.stdin.isatty():
        time.sleep(timeout)
        return None
    return _poll_key(timeout)


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
# The regime gauge advances only when a window RESOLVES and the outcomes
# corpus is refreshed — minutes at best, hours in practice. A 60s tail-read of
# a local JSONL is already far faster than the quantity moves, and it is the
# one worker source that touches no network at all.
REGIME_EVERY_S = 60.0
TAPE_EVERY_S = 2.0        # remote-tape poll — see TapeFeed; free while local is fresh
# A local tape whose newest record is older than this is not being written by
# an engine on THIS box. 30s because the fleet evaluates on a 5s throttle, so a
# healthy tape is never half a minute cold while anything is armed — but a
# genuinely idle desktop crosses it too, and TapeFeed is built for that to be
# harmless (the poll comes back empty and the panel says nothing).
TAPE_STALE_S = 30.0
TAPE_LIMIT = 500          # the engine's own hard cap; ask for it explicitly
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
# 16 because this is the dashboard's ONLY table and it carries the fleet's
# LIVE windows at the top: a five-arm fleet spends five rows before the
# decided tail even starts.
WINDOWS_MAX_ROWS = 16
WINDOWS_CHROME = 6        # panel border (2) + table border/header/rule (4)
MIN_TAPE_ROWS = 6         # the tape never gets squeezed below this for a window row
HEAD_MIN_H = 5            # header border + the four rows it always paints
# What a Panel costs the renderable inside it: two border columns plus its one
# space of padding either side. The windows table picks its column set from the
# room it will actually get, not from the console width.
PANEL_CHROME_W = 4


def windows_rows_shown(console_h: int, n_rows: int,
                       head_h: int = HEAD_MIN_H) -> int:
    """Window rows this screen can hold: what there is, capped, and never so
    many that the tape stops being readable.

    The panel is BUILT to this number rather than clipped to it — a table cut
    off mid-box below its last visible row reads as a crash, not as a cap —
    and the panel title repeats it, so a short screen says "6 of 14" instead
    of silently looking like the whole ledger.

    `head_h` is the header panel's live height (watch_ui.header_height): it
    grows a row for the settlement feed, for an unreachable engine and for a
    render error, and the tape is what pays for that, never the windows
    panel's floor of one row.
    """
    room = console_h - head_h - MIN_TAPE_ROWS - WINDOWS_CHROME
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

    _FIELDS = ("status", "bal", "sb", "sb_stale", "sb_fetched_at", "err",
               "odds", "regime")

    def __init__(self, sb: dict | None = None) -> None:
        self._lock = threading.Lock()
        self._d: dict = {
            "status": {}, "bal": {},
            # The regime gauge's newest corpus row, or None on a box that has
            # never run `pmt crypto regime`. None IS the cold-start value the
            # header row is built to drop — never a zeroed dict.
            "regime": None,
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


class TapeFeed:
    """Where the tape panel's records come from, and what it has already seen.

    Two sources, one cursor. The LOCAL file is primary: on the engine's own
    box `~/.pmt/engine/updown-tape.jsonl` is the whole story, the render loop
    seeks it in microseconds and this class never touches the network. But
    that path reads the ENGINE's disk, not the operator's, the moment
    PMENGINE_CONTROL_URL points down an SSM tunnel — which is why a watch on a
    laptop showed a tape panel with almost nothing in it. So when the local
    file is missing or frozen, the fetch worker polls the engine's
    `GET /tape?since=<cursor>` instead and feeds the SAME TapeCollapser: a
    record is a record whatever carried it.

    The cursor is what makes mixing them safe. Every record the panel accepted
    advanced it, from either source, so a handover in either direction cannot
    re-render what is already on screen. It is also the `since` the poll sends,
    which is what keeps the payload to "what's new" rather than a tape slice
    per tick.

    Nothing here runs on the desktop's happy path: local-fresh short-circuits
    before the request, so an engine on this box sees ZERO tape calls.
    """

    def __init__(self, path: str | None = None,
                 stale_after: float = TAPE_STALE_S,
                 limit: int = TAPE_LIMIT) -> None:
        self._lock = threading.Lock()
        self._path = tape.UPDOWN_TAPE if path is None else path
        self._stale_after = stale_after
        self._limit = limit
        self._cursor = 0.0
        # Identities (tape.record_key) of the records already accepted AT
        # _cursor — see accept(). Bounded by the fleet's arm count, and reset
        # to empty the moment the cursor moves on.
        self._seen: set[str] = set()
        self._pending: list[str] = []
        self._remote = False
        self._gap = False

    # -- render side: the cursor gate every record passes through --

    def accept(self, raw: str, collapser, lines) -> bool:
        """Gate one raw tape line on the cursor, then render it.

        The gate is `(t, identity)`, NOT `t` alone. The engine writes one
        record per armed token per fleet tick and every one of them carries
        that tick's `t` — up to 14. A strictly-increasing `t` cursor keeps the
        first of each tick and silently throws the rest of the fleet away:
        measured on the live tape that is 79% of all records, 100% of ROLLs,
        and four of seven armed symbols that never reach the panel at all.
        So a record older than the cursor is still refused (a re-read from a
        stale offset), and a record AT the cursor is refused only when this
        exact record is already on the panel.

        A line with no readable `t` is passed through ungated rather than
        dropped — the collapser has its own opinion about records it can't
        parse, and silently swallowing one here would hide a record the panel
        exists to show.
        """
        t = tape.record_t(raw)
        if t is not None:
            key = tape.record_key(raw)
            with self._lock:
                if t < self._cursor:
                    return False
                if t > self._cursor:
                    self._cursor, self._seen = t, set()
                elif key is not None and key in self._seen:
                    return False
                if key is not None:
                    self._seen.add(key)
        collapser.add(raw, lines)
        return True

    def drain(self, collapser, lines) -> None:
        """Render whatever the worker fetched since the last frame."""
        with self._lock:
            pending, self._pending = self._pending, []
            gap, self._gap = self._gap, False
        if gap:
            # The engine said its byte window didn't reach our cursor: records
            # we will never see sit between the last line on the panel and the
            # first of these. Nothing may collapse across that seam — a run
            # spanning it would state a count and a span that never happened.
            collapser.break_runs()
        served = False
        for raw in pending:
            served |= self.accept(raw, collapser, lines)
        if served:
            # Claimed HERE rather than at fetch time. A record the local file
            # had already supplied is not the remote serving the panel: a
            # desktop whose fleet wakes after an idle spell does cross the
            # staleness line and does fetch, but its own file wins the race
            # every frame, so the title never flickers to remote.
            with self._lock:
                self._remote = True

    @property
    def remote(self) -> bool:
        """True once a poll has delivered records the local file did not
        already have — i.e. the panel is showing the engine's tape, not this
        box's. Not merely "the local file looked stale": an idle desktop is
        stale and still entirely local."""
        with self._lock:
            return self._remote

    @property
    def gap(self) -> bool:
        """A fetched-but-not-yet-drained batch that the engine marked
        `truncated` — its byte window didn't reach our cursor, or it hit the
        record cap. Recency is what a remote tape is for, so a gap is accepted
        and continued from, never retried; all it changes is that the
        collapser must not merge across it. Cleared by drain()."""
        with self._lock:
            return self._gap

    # -- worker side: one bounded request, only when the file can't serve --

    def local_is_fresh(self, now: float | None = None) -> bool:
        newest = tape.newest_t(self._path)
        if newest is None:
            return False
        return (time.time() if now is None else now) - newest < self._stale_after

    def poll(self) -> None:
        """One tape tick on the fetch worker."""
        if self.local_is_fresh():
            with self._lock:
                self._remote = False
            return
        with self._lock:
            since = self._cursor
        body = _engine_get("/tape", {"since": since, "limit": self._limit})
        if not isinstance(body, dict):
            return  # unreachable, or a tunnel mid-reconnect: keep the last panel
        records = body.get("records") or []
        with self._lock:
            if not records:
                # A reachable engine with nothing new. Says nothing about the
                # source, so the title doesn't move: an idle desktop must not
                # start claiming its own tape is remote.
                return
            self._gap = self._gap or bool(body.get("truncated"))
            self._pending.extend(json.dumps(r) for r in records)


class WatchFetcher:
    """Every network call the watch dashboard makes, on one daemon thread.

    Each source has its own cadence and its own belt: a failure keeps the
    last good value and surfaces as staleness in the header, never as a
    traceback and never as a dead dashboard. Being on a worker thread is
    what buys the scoreboard the right to do the honest full wallet walk —
    the render loop never waits on it. See fetch_sb.
    """

    def __init__(self, state: WatchState, sliding_floor: float,
                 tape_feed: TapeFeed | None = None) -> None:
        self.state = state
        self.sliding_floor = sliding_floor
        # The tape is the one source that publishes somewhere other than
        # WatchState: it's a queue the renderer consumes exactly once, not a
        # value it re-reads every frame. See TapeFeed.
        self.tape_feed = TapeFeed() if tape_feed is None else tape_feed
        self._due: dict[str, float] = {"status": 0.0, "sb": 0.0, "bal": 0.0,
                                       "odds": 0.0, "tape": 0.0, "regime": 0.0}

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

    def fetch_tape(self) -> None:
        # ONE bounded GET, and only when the local file can't serve — the
        # cursor keeps it to what's new and the engine caps it at 500 records.
        # On the desktop this returns without making a request at all.
        self.tape_feed.poll()

    def fetch_regime(self) -> None:
        # A tail read of ~/.pmt/corpus/regime.jsonl and nothing else. It rides
        # the fetch thread rather than the render loop for one reason: the
        # render loop is allowed ZERO I/O it can be blocked on, and the gauge
        # is the only header row backed by a file the dashboard doesn't write.
        # It never computes the gauge — that is `pmt crypto regime`'s job, and
        # a dashboard that re-derived it would walk the whole book tape once a
        # minute to repaint one line.
        self.state.update(regime=regime.latest())

    # -- failure handling: last good value + a visible marker --

    def _status_failed(self, exc: BaseException) -> None:
        # Deliberately NOT the last good arms: a stale committed-$ figure on a
        # trading dashboard is worse than the red "engine unreachable or no
        # arms" an empty status renders as.
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

    def _regime_failed(self, exc: BaseException) -> None:
        # Keep the last good gauge. A regime read is a slow-moving fact about
        # the market, so the previous one is still the best answer available —
        # and its `old` age label already says how much to trust it.
        pass

    def _tape_failed(self, exc: BaseException) -> None:
        # Keep the records already on the panel and the cursor that got them.
        # A tape is a log: what it showed a second ago is still true, and the
        # next successful poll resumes from the same cursor with no gap.
        pass

    def tick(self, now: float) -> None:
        """Run whatever is due at `now`. Never raises."""
        for name, every, fetch, failed in (
            ("status", ENGINE_EVERY_S, self.fetch_status, self._status_failed),
            ("sb", SB_EVERY_S, self.fetch_sb, self._sb_failed),
            ("odds", ODDS_EVERY_S, self.fetch_odds, self._odds_failed),
            ("bal", BAL_EVERY_S, self.fetch_bal, self._bal_failed),
            ("tape", TAPE_EVERY_S, self.fetch_tape, self._tape_failed),
            ("regime", REGIME_EVERY_S, self.fetch_regime, self._regime_failed),
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
    """Full-screen live dashboard: risk header + the windows table + streaming tape."""
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
    # Seeding goes through the feed, not straight at the collapser: it leaves
    # the cursor on the newest local record, which is exactly the `since` the
    # remote poll should open with if this box turns out not to be the engine's.
    tape_feed = TapeFeed()
    offset = 0
    try:
        with open(tape.UPDOWN_TAPE) as fh:
            for raw in fh.readlines()[-120:]:
                tape_feed.accept(raw, collapser, lines)
            offset = fh.tell()
    except OSError:
        pass

    # All network lives on the worker; the loop below only ever reads this
    # snapshot, so the first paint is immediate (data age "—" until the first
    # wallet walk lands). See docs/LESSONS.md#L28.
    state = WatchState()
    stop = threading.Event()
    fetcher = WatchFetcher(state, floor, tape_feed=tape_feed)
    worker = threading.Thread(target=fetcher.loop, args=(stop,),
                              name="pmt-watch-fetch", daemon=True)
    snap = state.read()
    render_err: str | None = None

    def header() -> Panel:
        return build_header_panel(snap, floor_label, render_err)

    def windows_panel(rows: int, width: int) -> Panel:
        sb, arms = snap["sb"], snap["status"].get("arms")
        return Panel(build_windows_table(sb, _t.time(), arms=arms, limit=rows,
                                         odds=snap.get("odds"),
                                         width=width - PANEL_CHROME_W),
                     title=windows_title(sb, arms, rows),
                     # The "h" hint rides the panel whose glyphs the modal
                     # explains.
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
        return Panel(body, title=tape_title(tape_feed.remote), border_style="dim")

    # Three slots, one of them a table: header, the windows table, the tape.
    layout = Layout()
    layout.split_column(
        Layout(name="head", size=HEAD_MIN_H),
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
                                tape_feed.accept(raw, collapser, lines)
                            offset = fh.tell()
                    except OSError:
                        pass
                    # Whatever the worker pulled off the control plane since
                    # the last frame — nothing at all unless the file above
                    # went cold. Still zero network on this thread.
                    tape_feed.drain(collapser, lines)
                    # The header grows a row for the settlement feed, one for
                    # an unreachable engine and one for a render error; size
                    # the slot to what it will paint or Rich clips the row
                    # that says what broke.
                    layout["head"].size = header_height(
                        snap, render_err, live.console.size.width)
                    n_win = windows_rows_shown(
                        live.console.size.height,
                        len(window_rows(snap["sb"], snap["status"].get("arms"))),
                        layout["head"].size)
                    layout["windows"].size = n_win + WINDOWS_CHROME
                    layout["head"].update(header())
                    layout["windows"].update(
                        windows_panel(n_win, live.console.size.width))
                    h = (live.console.size.height - layout["head"].size
                         - layout["windows"].size)
                    layout["tape"].update(tape_panel(h))
                    render_err = None
                except KeyboardInterrupt:
                    raise
                except (Exception, SystemExit) as e:
                    render_err = f"{type(e).__name__}: {e}"[:100]
                    try:
                        layout["head"].size = header_height(
                            snap, render_err, live.console.size.width)
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
