"""Tests for the watch dashboard's fetch/render thread split.

The rule these exist to defend: watch is a VIEW of the stats acquisition, not
a second one. `test_watch_fetch_sb_is_the_stats_acquisition_path` asserts that
by identity, so re-introducing a cache or a "cheaper" incremental walk for the
dashboard fails a named test rather than drifting the P&L quietly (which is
exactly what it did for five patches before 2026-08-23).

The rest pin the split itself: every source has its own cadence, every failure
keeps the last good value, and no fetch may ever block a repaint — plus the
terminal-mode helpers the input loop pages keys through.
"""

from __future__ import annotations

import sys
import time

import pytest

import cli_crypto_stats as cs
import cli_crypto_watch as cw
from polymarket import wallet


# ---------- the one that must never be "optimized" ----------

def test_watch_fetch_sb_is_the_stats_acquisition_path(monkeypatch):
    """Watch's scoreboard fetch IS cli_crypto_stats._tape_scoreboard.

    Not an equivalent function, not a warm copy, not an incremental refresh of
    one: the same object. An in-memory ledger over this feed lived here until
    2026-08-23 and drifted the dashboard's P&L away from `stats` five separate
    ways (mutating redeem rows, seam re-serves, mid-walk mutations, ...) —
    polymarket/wallet.py carries the autopsy.

    If you are reading this because you just broke it: the answer to a slow
    walk is a floor at strategy genesis, never a second acquisition path.
    """
    # 1. the global fetch_sb resolves is literally the stats function
    assert cw._tape_scoreboard is cs._tape_scoreboard

    # 2. ...and fetch_sb calls THAT global, rather than holding a private copy
    #    or a cache that only happens to agree on the first call.
    calls: list[tuple] = []
    monkeypatch.setattr(cw, "_tape_scoreboard",
                        lambda floor, sliding_floor=None: calls.append((floor, sliding_floor)) or {})
    cw.WatchFetcher(cw.WatchState(), sliding_floor=1234.0).fetch_sb()
    assert calls == [(0.0, 1234.0)], "fetch_sb did not go through the module-level acquisition path"


def test_a_graded_trade_always_reaches_the_watch_windows_table(monkeypatch):
    """End-to-end over the ONE acquisition path: whatever score_activity
    grades is what the dashboard's windows panel paints.

    Both halves are pinned here because the dashboard has lost each of them:
    a DECIDED window must render, and a FILLED-but-undecided one must render
    too (the BNB window of 2026-08-23 14:35-14:40 was invisible for ~4
    minutes between its fill and its redeem row, with the arm already rolled
    and the fire scrolled off the tape).
    """
    import watch_ui
    from rich.console import Console

    now = int(time.time())
    done_start, live_start = now - 5000, now - 400   # live_start ends inside grace
    done, live = f"eth-updown-5m-{done_start}", f"bnb-updown-5m-{live_start}"
    rows = [
        {"type": "TRADE", "side": "BUY", "usdcSize": 9.0, "size": 10.0,
         "slug": done, "timestamp": done_start + 60},
        {"type": "REDEEM", "usdcSize": 10.0, "outcome": "up",
         "slug": done, "timestamp": done_start + 330},
        {"type": "TRADE", "side": "BUY", "usdcSize": 19.44, "size": 20.0,
         "slug": live, "timestamp": live_start + 183},
    ]
    fires = [{"ev": "fire", "slug": s, "side": "up", "fair": 1.0, "t": 0}
             for s in (done, live)]
    monkeypatch.setattr(cs.wallet, "funder_address", lambda: "0xabc")
    monkeypatch.setattr(cs.wallet, "fetch_wallet_activity", lambda addr, floor: rows)
    monkeypatch.setattr(cs.tape, "iter_records", lambda *a, **k: iter(fires))
    monkeypatch.setattr(cs, "_gamma_resolution_cached", lambda slug: None)

    state = cw.WatchState()
    cw.WatchFetcher(state, sliding_floor=0.0).fetch_sb()   # the real stats walk
    sb = state.read()["sb"]

    c = Console(record=True, width=200)   # the folded table's natural width
    c.print(watch_ui.build_windows_table(sb, time.time(), limit=cw.WINDOWS_MAX_ROWS))
    out = c.export_text()
    assert "eth 5m" in out, "a decided trade vanished between the grade and the table"
    assert "bnb 5m" in out, "a filled trade is invisible until its redeem posts"
    assert "riding" in out
    # ...and the strip's at-a-glance glyphs came through the merge with it.
    assert "◆" in out and "✓" in out
    assert watch_ui.windows_title(sb) == "windows · 0 live · 1 riding · last 1 decided"


def test_a_riding_position_stays_on_the_panel_through_a_roll_until_it_grades(monkeypatch):
    """The operator's ask: "don't leave the position too early when switching
    markets/quiescing so we can see how each position resolves".

    Three frames off the ONE acquisition path: the arm fills window A, then
    rolls to window B (nothing filled there yet), then A's redeem row posts.
    A must be on the windows panel in all three — riding through the roll, then
    decided — because the wallet, not the arm, is what retires a position. The
    arm rolling to B is passed in as the LIVE arm each frame, so this also pins
    that the live head never displaces the position it rolled off.
    """
    import watch_ui
    from rich.console import Console

    # A closed two minutes ago (inside the 300s no-redeem grace, so still
    # genuinely undecided); B is the window the arm rolled into and is live.
    now = int(time.time())
    a_start, b_start = now - 420, now - 120
    a, b = f"btc-updown-5m-{a_start}", f"btc-updown-5m-{b_start}"
    filled_a = {"type": "TRADE", "side": "BUY", "usdcSize": 19.44, "size": 20.0,
                "slug": a, "timestamp": a_start + 180}
    redeem_a = {"type": "REDEEM", "usdcSize": 20.0, "outcome": "up",
                "slug": a, "timestamp": a_start + 320}
    fires = [{"ev": "fire", "slug": a, "side": "up", "fair": 1.0, "t": 0}]
    rows = [filled_a]

    monkeypatch.setattr(cs.wallet, "funder_address", lambda: "0xabc")
    monkeypatch.setattr(cs.wallet, "fetch_wallet_activity", lambda addr, floor: list(rows))
    monkeypatch.setattr(cs.tape, "iter_records", lambda *a_, **k: iter(fires))
    monkeypatch.setattr(cs, "_gamma_resolution_cached", lambda slug: None)

    def panel_text(live=a):
        state = cw.WatchState()
        cw.WatchFetcher(state, sliding_floor=0.0).fetch_sb()
        sb = state.read()["sb"]
        arms = {live: {"filled_usdc": 0.0, "roll": True,
                       "eval": {"state": "armed", "committed": 0.0}}}
        c = Console(record=True, width=200)
        c.print(watch_ui.build_windows_table(sb, time.time(), arms=arms,
                                             limit=cw.WINDOWS_MAX_ROWS))
        return sb, c.export_text()

    # 1. filled, window A still open
    sb, out = panel_text()
    assert "btc 5m" in out and "riding" in out
    assert [w["slug"] for w in sb["riding_windows"]] == [a]

    # 2. the arm has rolled to B and armed it — A is nobody's current window
    #    any more, and used to vanish from every per-arm view at this point.
    fires.append({"ev": "fire", "slug": b, "side": "up", "fair": 1.0, "t": 0})
    sb, out = panel_text(live=b)
    assert [w["slug"] for w in sb["riding_windows"]] == [a], "the rolled-off position was dropped"
    assert "riding" in out
    # A (riding) and B (the arm's new live window) are two rows, not one.
    assert out.count("btc 5m") == 2
    assert "◆" in out and "○" in out

    # 3. the wallet grades it — and only now does it stop riding
    rows.append(redeem_a)
    sb, out = panel_text(live=b)
    assert sb["riding_windows"] == []
    assert [w["slug"] for w in sb["windows"]] == [a]
    assert "btc 5m" in out and "riding" not in out
    assert "✓" in out


def test_windows_panel_never_starves_the_tape_and_never_paints_a_clipped_box():
    """The panel is sized to the rows it will actually paint. Rich clips a
    Layout slot that overflows, and a table cut off below its last row (no
    bottom border) reads as a crash rather than as a cap."""
    # Roomy screen, plenty of windows: the view cap is what bites.
    assert cw.windows_rows_shown(50, 20) == cw.WINDOWS_MAX_ROWS
    # Few windows: don't reserve rows for windows that don't exist.
    assert cw.windows_rows_shown(50, 2) == 2
    # Cramped screen: the tape keeps its floor, one window row still survives.
    assert cw.windows_rows_shown(16, 20) == 1
    for h in range(16, 60):
        for head_h in (cw.HEAD_MIN_H, cw.HEAD_MIN_H + 2):
            n = cw.windows_rows_shown(h, 20, head_h)
            assert 1 <= n <= cw.WINDOWS_MAX_ROWS
            # head + panel: everything left over is the tape's.
            left = h - head_h - (n + cw.WINDOWS_CHROME)
            assert left >= cw.MIN_TAPE_ROWS or n == 1


def test_the_folded_away_panels_paid_for_the_tables_rows():
    """The one table inherited the strip's three rows and the arms table's
    whole slot; the decided tail may not have got shorter now that the live
    head sits above it."""
    assert cw.WINDOWS_MAX_ROWS == 16   # 8 + the strip's 3 + the arms slot's 5
    for gone in ("STRIP_H", "TRADES_MAX_ROWS"):
        assert not hasattr(cw, gone), gone
    import watch_ui
    # The arms table is not merely unused: it is gone, so nothing can quietly
    # bring a second table back.
    assert not hasattr(watch_ui, "build_arms_table")
    assert not hasattr(watch_ui, "build_windows_strip")


def test_the_dashboard_paints_exactly_one_table():
    """The operator's ask, and the thing that regressed once already: header,
    ONE table, tape — nothing else builds a Table."""
    import inspect

    import watch_ui

    src = inspect.getsource(cw.crypto_watch.callback)
    assert src.count("split_column") == 1
    assert 'Layout(name="arms"' not in src
    assert [n for n in ("head", "windows", "tape") if f'name="{n}"' in src] == [
        "head", "windows", "tape"]
    builders = [n for n in dir(watch_ui)
                if n.startswith("build_") and n.endswith("_table")]
    assert builders == ["build_windows_table"], builders


def test_a_taller_header_costs_the_tape_rows_not_the_windows_floor():
    # The header grows a row for the settlement feed, one for an unreachable
    # engine and one for a render error; the windows panel keeps its floor of
    # one row either way.
    roomy = cw.windows_rows_shown(50, 20, cw.HEAD_MIN_H)
    taller = cw.windows_rows_shown(50, 20, cw.HEAD_MIN_H + 2)
    assert roomy == taller == cw.WINDOWS_MAX_ROWS  # a roomy screen absorbs it
    assert cw.windows_rows_shown(16, 20, cw.HEAD_MIN_H + 2) == 1


# ---------- the controls modal: keys in, dashboard back ----------

def test_the_modal_lists_exactly_the_keys_the_watch_handles():
    """Both directions. A key with no line in the panel is undiscoverable; a
    line with no handler behind it is a lie the operator will act on."""
    import string

    import watch_ui

    listed = {k for k, _label, _d in watch_ui.WATCH_KEYS if k is not None}
    for key in listed:
        # Every listed key does something in at least one state (esc with
        # nothing open is correctly inert).
        assert any(cw.handle_key(key, open_) != (False, open_, False)
                   for open_ in (False, True)), key
    # Nothing else does anything, in either state.
    for key in string.printable:
        if key.lower() in listed:
            continue
        for open_ in (False, True):
            assert cw.handle_key(key, open_) == (False, open_, False), key
    assert cw.handle_key(None, False) == (False, False, False)


def test_h_toggles_the_modal_and_restores_the_dashboard():
    # A toggle repaints now, not on the next second — the modal must feel
    # instant at the 20Hz key poll.
    quit_now, show, dirty = cw.handle_key("h", False)
    assert (quit_now, show, dirty) == (False, True, True)
    assert cw.handle_key("h", True) == (False, False, True)


def test_esc_and_q_close_the_modal_before_q_can_quit():
    """A foreground panel the quit key punched through would cost the operator
    their dashboard on a stray press."""
    assert cw.handle_key("\x1b", True) == (False, False, True)
    assert cw.handle_key("q", True) == (False, False, True)   # closes, does NOT quit
    assert cw.handle_key("q", False)[0] is True               # ...and then quits
    # An idle esc changes nothing and forces no repaint.
    assert cw.handle_key("\x1b", False) == (False, False, False)


def test_the_modal_is_a_pure_renderable_and_never_touches_the_fetchers():
    """It is FOREGROUND, not a pause: the worker thread and every cadence are
    untouched, so dismissing restores a live frame rather than a frozen one."""
    import inspect

    import watch_ui

    # Source past the docstring (3.13+ dedents __doc__, so it is not a
    # substring of the source and can't be subtracted).
    body = inspect.getsource(watch_ui.build_help_modal).split('"""')[-1]
    for forbidden in ("fetch", "requests", "_api", "post("):
        assert forbidden not in body, forbidden
    # ...and the loop keeps rebuilding the dashboard behind it, so dismissing
    # restores the current frame rather than the one 'h' was pressed on.
    loop = inspect.getsource(cw.crypto_watch.callback)  # click wraps the command
    assert "live.update(_modal(" in loop and "else layout" in loop


def _fetcher(monkeypatch, *, sb=None, status=None, bal=None, sb_boom=None,
             odds=None):
    """A WatchFetcher with every network seam replaced. The scoreboard seam
    is _tape_scoreboard — the SAME function `pmt crypto stats` runs, which
    is the whole point: one acquisition path, one truth."""
    state = cw.WatchState()
    # A tape feed pointed at nothing, with the control plane stubbed dead: the
    # fetchers under test here are the OTHER four, and none of them may be
    # made to depend on whether this box happens to have a tape file.
    f = cw.WatchFetcher(state, sliding_floor=0.0,
                        tape_feed=cw.TapeFeed(path="/nonexistent/tape.jsonl"))
    monkeypatch.setattr(cw, "_engine_get", lambda *a, **k: None)
    monkeypatch.setattr(wallet, "funder_address", lambda: "0xabc")
    monkeypatch.setattr(cw.positions, "fetch_positions",
                        _raiser(odds) if isinstance(odds, BaseException)
                        else (lambda addr, *a, **k: odds or []))
    def _fake_scoreboard(floor, sliding_floor=None):
        if sb_boom and sb_boom[0] is not None:
            raise sb_boom[0]
        return sb or {"wins": 1}
    monkeypatch.setattr(cw, "_tape_scoreboard", _fake_scoreboard)
    monkeypatch.setattr(cw, "_engine_post",
                        (lambda *a, **k: status) if not isinstance(status, BaseException)
                        else _raiser(status))
    monkeypatch.setattr(cw, "_api", (lambda: _FakeApi(bal)) if not isinstance(bal, BaseException)
                        else _raiser(bal))
    return state, f


def _raiser(exc):
    def boom(*a, **k):
        raise exc
    return boom


class _FakeApi:
    def __init__(self, bal):
        self._bal = bal

    def get_usdc_balance(self):
        return self._bal


def test_watch_state_defaults_and_snapshot_isolation():
    st = cw.WatchState()
    snap = st.read()
    assert snap["status"] == {} and snap["bal"] == {}
    assert snap["sb_stale"] is False and snap["sb_fetched_at"] is None
    assert snap["err"] is None
    snap["status"] = {"arms": {"x": {}}}       # a renderer mangling its own copy
    assert st.read()["status"] == {}           # must not reach the shared state


def test_watch_state_update_swaps_whole_objects():
    st = cw.WatchState()
    sb = {"wins": 3, "losses": 1}
    st.update(sb=sb, sb_fetched_at=123.0)
    assert st.read()["sb"] is sb               # swapped in whole, not merged
    assert st.read()["sb_fetched_at"] == 123.0
    with pytest.raises(KeyError):
        st.update(bogus=1)                     # typo'd field must not vanish silently


def test_fetcher_scoreboard_is_the_stats_path(monkeypatch):
    state, f = _fetcher(monkeypatch, sb={"wins": 7})
    f.fetch_sb()
    snap = state.read()
    assert snap["sb"] == {"wins": 7}
    assert snap["sb_stale"] is False and snap["sb_fetched_at"] is not None


def test_fetcher_scoreboard_failure_keeps_last_value_and_marks_stale(monkeypatch):
    boom = [None]
    state, f = _fetcher(monkeypatch, sb={"wins": 7}, sb_boom=boom)
    f.tick(0.0)                                # first pass: everything succeeds
    good = state.read()["sb"]
    assert good == {"wins": 7}

    boom[0] = ConnectionError("data-api down")
    f.tick(1000.0)
    snap = state.read()
    assert snap["sb"] is good                  # last good numbers still on screen
    assert snap["sb_stale"] is True
    assert "ConnectionError" in snap["err"]

    boom[0] = None                             # recovery clears both markers
    f.tick(2000.0)
    snap = state.read()
    assert snap["sb_stale"] is False and snap["err"] is None


def test_fetcher_status_failure_is_belted_including_systemexit(monkeypatch):
    # engine.post() sys.exit()s when the engine is unreachable — SystemExit is
    # not an Exception, so a bare `except Exception` would kill the worker.
    state, f = _fetcher(monkeypatch, status=SystemExit(1))
    f.tick(0.0)                                # must not raise
    assert state.read()["status"] == {}         # arms table renders "engine unreachable"


def test_fetcher_never_rebinds_process_stdout(monkeypatch):
    # Rich resolves sys.stdout at write time, so a worker that swaps it (e.g.
    # contextlib.redirect_stdout to hush engine.post's error print) makes the
    # render thread see a non-tty and stop painting. The worker must leave
    # sys.stdout alone even while a fetch is failing loudly.
    seen: list = []

    def noisy(*a, **k):
        seen.append(sys.stdout)
        print("Cannot reach pmengine")          # what engine.post() does before exiting
        raise SystemExit(1)

    state, f = _fetcher(monkeypatch, status=None)
    monkeypatch.setattr(cw, "_engine_post", noisy)
    before = sys.stdout
    f.tick(0.0)
    assert seen == [before]        # unchanged DURING the call
    assert sys.stdout is before    # and after it


def test_fetcher_balance_failure_keeps_last_capital(monkeypatch):
    state, f = _fetcher(monkeypatch, bal={"total": 500.0})
    f.tick(0.0)
    assert state.read()["bal"] == {"total": 500.0}
    monkeypatch.setattr(cw, "_api", _raiser(RuntimeError("rpc down")))
    f.tick(10_000.0)
    assert state.read()["bal"] == {"total": 500.0}  # blanking capital would be worse


def test_fetcher_tick_honors_per_source_cadences(monkeypatch):
    state, f = _fetcher(monkeypatch, bal={"total": 1.0})
    ran: list[str] = []
    for name in ("status", "sb", "bal", "odds", "tape"):
        monkeypatch.setattr(f, f"fetch_{name}", lambda n=name: ran.append(n))

    f.tick(1000.0)
    # first tick primes everything
    assert sorted(ran) == ["bal", "odds", "sb", "status", "tape"]
    ran.clear()

    f.tick(1001.0)                                   # nothing is due yet
    assert ran == []
    f.tick(1002.0)
    assert sorted(ran) == ["status", "tape"]          # engine + tape: 2s
    ran.clear()
    f.tick(1010.0)
    assert sorted(ran) == ["sb", "status", "tape"]    # scoreboard: 10s
    ran.clear()
    f.tick(1030.0)
    assert sorted(ran) == ["odds", "sb", "status", "tape"]  # position marks: 30s
    ran.clear()
    f.tick(1060.0)
    assert sorted(ran) == ["bal", "odds", "sb", "status", "tape"]  # balance: 60s


# ---------- current odds: a display feed, on the slow lane ----------

_POSITION = {"slug": "bnb-updown-5m-1787510100", "outcome": "Up", "curPrice": 0.99}


def test_fetcher_publishes_current_marks_keyed_by_window_and_side(monkeypatch):
    state, f = _fetcher(monkeypatch, odds=[_POSITION])
    f.fetch_odds()
    assert state.read()["odds"] == {("bnb-updown-5m-1787510100", "up"): 0.99}


def test_current_marks_go_blank_on_failure_rather_than_going_stale(monkeypatch):
    # A mark is a live quote. A 10-minute-old one beside a live entry price
    # reads as "the position hasn't moved", which is worse than saying nothing.
    state, f = _fetcher(monkeypatch, odds=[_POSITION])
    f.tick(0.0)
    assert state.read()["odds"]
    monkeypatch.setattr(cw.positions, "fetch_positions",
                        _raiser(ConnectionError("data-api down")))
    f.tick(10_000.0)                                  # must not raise
    assert state.read()["odds"] == {}


def test_the_marks_fetch_never_calls_the_engine(monkeypatch):
    # The control plane is not a price feed: this whole column is served off
    # the public data-api and the funder address, nothing else.
    state, f = _fetcher(monkeypatch, odds=[_POSITION])
    monkeypatch.setattr(cw, "_engine_post", _raiser(AssertionError("engine touched")))
    f.fetch_odds()
    assert state.read()["odds"]


def test_the_marks_cadence_is_slow_enough_to_never_be_a_hot_loop():
    assert cw.ODDS_EVERY_S >= 30.0


def test_fetcher_loop_exits_on_stop_flag(monkeypatch):
    import threading

    state, f = _fetcher(monkeypatch)
    stop = threading.Event()
    th = threading.Thread(target=f.loop, args=(stop,), daemon=True)
    th.start()
    stop.set()
    th.join(timeout=cw.WORKER_JOIN_S)
    assert not th.is_alive()  # 'q' must never wait out a poll interval


def test_render_path_is_never_blocked_by_a_slow_fetch(monkeypatch):
    """The whole point of the split: a multi-second wallet walk on the worker
    must not delay the loop that reads keys and repaints."""
    import threading

    state, f = _fetcher(monkeypatch)
    started = threading.Event()

    def slow_scoreboard(floor, sliding_floor=None):
        started.set()
        time.sleep(1.5)  # the full wallet walk, now the sb fetch's real cost
        return {"wins": 1}

    monkeypatch.setattr(cw, "_tape_scoreboard", slow_scoreboard)
    stop = threading.Event()
    th = threading.Thread(target=f.loop, args=(stop,), daemon=True)
    th.start()
    try:
        assert started.wait(1.0), "worker never entered the slow fetch"
        # 20 render-loop passes while the worker is stuck mid-fetch.
        t0 = time.monotonic()
        for _ in range(20):
            snap = state.read()
            assert "sb" in snap
        elapsed = time.monotonic() - t0
        assert elapsed < 0.2, f"render reads blocked for {elapsed:.2f}s"
    finally:
        stop.set()
        th.join(timeout=cw.WORKER_JOIN_S + 2)


# ---------- terminal mode + key polling ----------

class _FakeStdin:
    def __init__(self, isatty=True, chars=""):
        self._isatty = isatty
        self._chars = chars

    def isatty(self):
        return self._isatty

    def read(self, n):
        ch, self._chars = self._chars[:n], self._chars[n:]
        return ch

    def fileno(self):
        return 0


def test_cbreak_stdin_noop_when_not_a_tty(monkeypatch):
    monkeypatch.setattr(cw.sys, "stdin", _FakeStdin(isatty=False))
    assert cw._cbreak_stdin() is None
    cw._restore_stdin(None)  # must not raise


def test_poll_key_none_when_not_a_tty(monkeypatch):
    monkeypatch.setattr(cw.sys, "stdin", _FakeStdin(isatty=False))
    assert cw._poll_key() is None


def test_poll_key_reads_q_when_ready(monkeypatch):
    monkeypatch.setattr(cw.sys, "stdin", _FakeStdin(isatty=True, chars="q"))
    monkeypatch.setattr(cw.select, "select", lambda r, w, x, t: ([0], [], []))
    monkeypatch.setattr(cw.os, "read", lambda fd, n: b"q")
    assert cw._poll_key() == "q"


def test_poll_key_returns_other_keys_lowercased(monkeypatch):
    monkeypatch.setattr(cw.sys, "stdin", _FakeStdin(isatty=True, chars="x"))
    monkeypatch.setattr(cw.select, "select", lambda r, w, x, t: ([0], [], []))
    monkeypatch.setattr(cw.os, "read", lambda fd, n: b"H")
    assert cw._poll_key() == "h"


def test_poll_key_none_when_nothing_ready(monkeypatch):
    monkeypatch.setattr(cw.sys, "stdin", _FakeStdin(isatty=True, chars=""))
    monkeypatch.setattr(cw.select, "select", lambda r, w, x, t: ([], [], []))
    assert cw._poll_key() is None


def test_quit_requested_swallows_select_errors(monkeypatch):
    monkeypatch.setattr(cw.sys, "stdin", _FakeStdin(isatty=True))

    def boom(*a, **k):
        raise OSError("bad fd")

    monkeypatch.setattr(cw.select, "select", boom)
    assert cw._poll_key() is None  # never raises, dashboard must survive


def test_poll_key_passes_timeout_through_to_select(monkeypatch):
    seen = {}

    def fake_select(r, w, x, t):
        seen["timeout"] = t
        return ([], [], [])

    monkeypatch.setattr(cw.sys, "stdin", _FakeStdin(isatty=True))
    monkeypatch.setattr(cw.select, "select", fake_select)
    assert cw._poll_key() is None
    assert seen["timeout"] == 0.0          # default stays non-blocking
    assert cw._poll_key(0.05) is None
    assert seen["timeout"] == 0.05         # the watch loop's 20Hz pacing


def test_wait_key_without_a_tty_paces_instead_of_spinning(monkeypatch):
    slept = []
    monkeypatch.setattr(cw.sys, "stdin", _FakeStdin(isatty=False))
    monkeypatch.setattr(cw.time, "sleep", lambda s: slept.append(s))
    assert cw._wait_key(0.05) is None
    assert slept == [0.05]  # no tty to select on -> the sleep is the pacing


# ---------- the tape's two sources behind one cursor ----------
#
# `pmt crypto watch` reads the tape off ~/.pmt/engine — the ENGINE's disk, not
# the operator's, the moment PMENGINE_CONTROL_URL points down an SSM tunnel.
# TapeFeed adds the control plane as a fallback. These pin the two halves of
# the deal: the desktop path is untouched (and provably makes no request), and
# the remote path never renders a record the panel already has.

import watch_ui  # noqa: E402 — the section below renders through the real collapser


def _ev(t, **kw):
    """A complete eval record — every field _render_record reads, so these
    tests exercise the real collapser rather than a shape it silently drops."""
    return {"t": t, "ev": "eval", "slug": "btc-updown-5m-1", "p_up": 0.5,
            "sig_bp": 1.0, "rho": 0.0, "committed": 0.0, "sides": [], **kw}


def _tape_file(tmp_path, *ts, name="updown-tape.jsonl"):
    """A local tape file whose records land at the given absolute `t`s."""
    import json as _j
    p = tmp_path / name
    p.write_text("".join(_j.dumps(_ev(t)) + "\n" for t in ts))
    return str(p)


def _remote(records, truncated=False):
    """A stub control plane, recording every call it is asked to make."""
    calls: list[tuple] = []

    def _get(path, params=None, **kw):
        calls.append((path, dict(params or {})))
        batch = records.pop(0) if records else []
        return {"records": batch, "truncated": truncated,
                "cursor": batch[-1]["t"] if batch else None}
    return _get, calls


def test_a_fresh_local_tape_makes_zero_control_plane_calls(monkeypatch, tmp_path):
    """THE pin on the desktop path. The engine's own box already has the whole
    tape on disk; a watch there must not start asking the trading loop's
    control plane for it, on any cadence, ever."""
    feed = cw.TapeFeed(path=_tape_file(tmp_path, time.time() - 1.0))
    _get, calls = _remote([[{"t": 9e9, "ev": "fire"}]])
    monkeypatch.setattr(cw, "_engine_get", _get)

    for _ in range(20):
        feed.poll()

    assert calls == [], "a fresh local tape must serve the panel by itself"
    assert feed.remote is False


def test_a_missing_local_tape_falls_back_to_the_control_plane(monkeypatch, tmp_path):
    feed = cw.TapeFeed(path=str(tmp_path / "nothing-here.jsonl"))
    _get, calls = _remote([[{"t": 1000.0, "ev": "fire", "side": "up"}]])
    monkeypatch.setattr(cw, "_engine_get", _get)

    from collections import deque
    feed.poll()
    assert [c[0] for c in calls] == ["/tape"]
    feed.drain(watch_ui.TapeCollapser(), deque(maxlen=200))
    assert feed.remote is True


def test_a_frozen_local_tape_falls_back_to_the_control_plane(monkeypatch, tmp_path):
    """A laptop that once ran the engine has a real tape file — hours cold.
    Its presence must not be mistaken for its being the live one."""
    stale = time.time() - (cw.TAPE_STALE_S + 60)
    feed = cw.TapeFeed(path=_tape_file(tmp_path, stale))
    _get, calls = _remote([[{"t": 9e9, "ev": "fire"}]])
    monkeypatch.setattr(cw, "_engine_get", _get)

    assert feed.local_is_fresh() is False
    feed.poll()
    assert [c[0] for c in calls] == ["/tape"]


def test_the_first_remote_poll_opens_at_the_newest_local_record(monkeypatch, tmp_path):
    """A stale local file still tells us where the panel got to. Opening the
    cursor at 0 instead would re-serve every record already on screen."""
    from collections import deque
    stale = time.time() - 4000
    feed = cw.TapeFeed(path=_tape_file(tmp_path, stale - 20, stale - 10, stale))
    collapser, lines = watch_ui.TapeCollapser(), deque(maxlen=200)
    with open(feed._path) as fh:
        for raw in fh:
            feed.accept(raw, collapser, lines)

    _get, calls = _remote([[]])
    monkeypatch.setattr(cw, "_engine_get", _get)
    feed.poll()
    assert calls[0][1]["since"] == stale


def test_the_remote_cursor_advances_and_never_refetches(monkeypatch, tmp_path):
    from collections import deque
    feed = cw.TapeFeed(path=str(tmp_path / "absent.jsonl"))
    _get, calls = _remote([
        [_ev(100.0), _ev(101.0)],
        [_ev(102.0)],
        [],
    ])
    monkeypatch.setattr(cw, "_engine_get", _get)
    collapser, lines = watch_ui.TapeCollapser(), deque(maxlen=200)

    feed.poll(); feed.drain(collapser, lines)
    assert calls[0][1]["since"] == 0.0          # cold: whatever the window holds
    feed.poll(); feed.drain(collapser, lines)
    assert calls[1][1]["since"] == 101.0        # exactly where the last batch ended
    feed.poll()
    assert calls[2][1]["since"] == 102.0
    # An empty answer must not rewind the cursor.
    feed.poll()
    assert calls[3][1]["since"] == 102.0


def test_the_remote_poll_asks_for_a_bounded_slice(monkeypatch, tmp_path):
    """Cursor plus cap: the payload a tunnel has to carry is bounded at both
    ends, which is the whole watch-load discipline this feed inherits."""
    feed = cw.TapeFeed(path=str(tmp_path / "absent.jsonl"))
    _get, calls = _remote([[]])
    monkeypatch.setattr(cw, "_engine_get", _get)
    feed.poll()
    assert calls[0][1]["limit"] == cw.TAPE_LIMIT <= 500


def test_a_record_the_panel_already_has_is_never_rendered_twice(monkeypatch, tmp_path):
    """The cursor is shared by BOTH sources, so a local file that thaws while
    the remote is serving (or the reverse) can't double-paint the overlap."""
    from collections import deque
    import json as _j
    feed = cw.TapeFeed(path=str(tmp_path / "absent.jsonl"))
    collapser, lines = watch_ui.TapeCollapser(), deque(maxlen=200)

    fire = {"t": 500.0, "ev": "fire", "side": "up", "size": 10, "ask": 0.9,
            "fair": 0.99, "net": 0.09, "rho": 0.1, "committed": 9.0}
    _get, _ = _remote([[fire]])
    monkeypatch.setattr(cw, "_engine_get", _get)
    feed.poll()
    feed.drain(collapser, lines)
    assert len(lines) == 1

    # The same record arriving off the local file: already seen, already shown.
    assert feed.accept(_j.dumps(fire), collapser, lines) is False
    assert len(lines) == 1
    # ...and an older one, which is what a re-read from a stale offset yields.
    assert feed.accept(_j.dumps({**fire, "t": 499.0}), collapser, lines) is False
    assert len(lines) == 1


def test_a_remote_record_renders_exactly_as_a_local_one(monkeypatch, tmp_path):
    """Same TapeCollapser, same line. A record is a record whatever carried
    it, so the panel can't develop a second look for remote windows."""
    from collections import deque
    import json as _j
    fire = {"t": 500.0, "ev": "fire", "side": "up", "size": 10, "ask": 0.9,
            "fair": 0.99, "net": 0.09, "rho": 0.1, "committed": 9.0}

    local_lines: deque = deque(maxlen=200)
    cw.TapeFeed(path=str(tmp_path / "a.jsonl")).accept(
        _j.dumps(fire), watch_ui.TapeCollapser(), local_lines)

    feed = cw.TapeFeed(path=str(tmp_path / "b.jsonl"))
    _get, _ = _remote([[fire]])
    monkeypatch.setattr(cw, "_engine_get", _get)
    remote_lines: deque = deque(maxlen=200)
    feed.poll()
    feed.drain(watch_ui.TapeCollapser(), remote_lines)

    assert list(remote_lines) == list(local_lines)


def test_a_truncation_marker_is_accepted_and_the_feed_continues(monkeypatch, tmp_path):
    """A remote tape is a recency feed. When the engine says its byte window
    couldn't reach our cursor, the answer is to take what arrived and move the
    cursor on — never to retry, and never to stall waiting for the gap."""
    from collections import deque
    feed = cw.TapeFeed(path=str(tmp_path / "absent.jsonl"))
    _get, calls = _remote([[_ev(900.0)], []], truncated=True)
    monkeypatch.setattr(cw, "_engine_get", _get)
    collapser, lines = watch_ui.TapeCollapser(), deque(maxlen=200)

    feed.poll()
    assert feed.gap is True
    feed.drain(collapser, lines)
    assert feed.gap is False, "the marker is consumed by the render that handled it"
    assert len(lines) == 1
    feed.poll()
    assert calls[1][1]["since"] == 900.0     # continued from what arrived


def test_nothing_collapses_across_a_gap_the_engine_admitted_to(monkeypatch, tmp_path):
    """Two identical evals of one arm normally fold into a single run line
    counting both. If records went missing between them, that count and its
    span are fiction — so a truncated batch breaks the open runs first."""
    from collections import deque
    feed = cw.TapeFeed(path=str(tmp_path / "absent.jsonl"))
    collapser, lines = watch_ui.TapeCollapser(), deque(maxlen=200)

    # Back to back, no gap: one collapsed line.
    _get, _ = _remote([[_ev(100.0)], [_ev(101.0)]])
    monkeypatch.setattr(cw, "_engine_get", _get)
    feed.poll(); feed.drain(collapser, lines)
    feed.poll(); feed.drain(collapser, lines)
    assert len(lines) == 1

    # Same two reads, but the engine says it skipped over records to get here.
    feed2 = cw.TapeFeed(path=str(tmp_path / "absent2.jsonl"))
    collapser2, lines2 = watch_ui.TapeCollapser(), deque(maxlen=200)
    _get2, _ = _remote([[_ev(100.0)]])
    monkeypatch.setattr(cw, "_engine_get", _get2)
    feed2.poll(); feed2.drain(collapser2, lines2)
    _get3, _ = _remote([[_ev(101.0)]], truncated=True)
    monkeypatch.setattr(cw, "_engine_get", _get3)
    feed2.poll(); feed2.drain(collapser2, lines2)
    assert len(lines2) == 2, "a run was allowed to span records it never saw"


def test_an_unreachable_engine_keeps_the_panel_and_the_cursor(monkeypatch, tmp_path):
    """A tunnel that blinks must cost nothing: the lines already on screen are
    still true, and the next good poll resumes with no gap of our own making."""
    from collections import deque
    feed = cw.TapeFeed(path=str(tmp_path / "absent.jsonl"))
    _get, calls = _remote([[_ev(700.0)]])
    monkeypatch.setattr(cw, "_engine_get", _get)
    collapser, lines = watch_ui.TapeCollapser(), deque(maxlen=200)
    feed.poll(); feed.drain(collapser, lines)

    monkeypatch.setattr(cw, "_engine_get", lambda *a, **k: None)  # engine.fetch on failure
    feed.poll()
    assert len(lines) == 1
    assert feed.remote is True

    monkeypatch.setattr(cw, "_engine_get", _get)
    feed.poll()
    assert calls[-1][1]["since"] == 700.0


def test_an_empty_remote_answer_never_claims_the_panel_is_remote(monkeypatch, tmp_path):
    """An idle desktop crosses the staleness line (nothing armed, nothing
    written) and does poll — but the poll comes back empty, so the panel is
    still showing its own file and must keep saying so."""
    feed = cw.TapeFeed(path=str(tmp_path / "absent.jsonl"))
    _get, calls = _remote([[], [], []])
    monkeypatch.setattr(cw, "_engine_get", _get)
    for _ in range(3):
        feed.poll()
    assert calls and feed.remote is False


def test_the_tape_poll_is_belted_by_the_fetcher_like_every_other_source(monkeypatch):
    state, f = _fetcher(monkeypatch)
    monkeypatch.setattr(f.tape_feed, "poll", _raiser(ConnectionError("tunnel down")))
    f.tick(0.0)                                  # must not raise
    f.tick(10_000.0)


def test_the_remote_tape_cadence_matches_the_control_planes_other_poll():
    # One extra small request per engine poll, not a second cadence to reason
    # about — and the analysis that cleared watch of the 2026-08-23 blackout
    # was measured at exactly this rate.
    assert cw.TAPE_EVERY_S == cw.ENGINE_EVERY_S


def test_a_desktop_waking_from_an_idle_spell_never_claims_remote(monkeypatch, tmp_path):
    """The one case that could make the desktop's title flicker: nothing armed
    for a while, so the local tape crosses the staleness line and the poll
    goes out — then the fleet wakes and BOTH sources carry the same records.
    The file is read first every frame, so the remote never serves anything
    and the panel keeps saying what it is."""
    from collections import deque
    import json as _j
    feed = cw.TapeFeed(path=_tape_file(tmp_path, time.time() - 900))
    collapser, lines = watch_ui.TapeCollapser(), deque(maxlen=200)
    assert feed.local_is_fresh() is False

    woke = _ev(time.time())
    _get, calls = _remote([[woke]])
    monkeypatch.setattr(cw, "_engine_get", _get)
    feed.poll()                                   # fetched, not yet rendered
    assert calls, "an idle local tape is stale and does poll"

    feed.accept(_j.dumps(woke), collapser, lines)  # the render loop's file read
    feed.drain(collapser, lines)                   # ...gets there first
    assert len(lines) == 1
    assert feed.remote is False, "the local file served this; the title must not move"


# ---------- conservation: the panel adds up to the tape ----------
#
# THE law this section exists for, and the one the tape panel silently broke:
#
#     every record the panel is fed is either RENDERED as its own line, or
#     counted inside a rendered run's ×N. Nothing is unaccounted.
#
# A collapse SUMMARISES — the counter says what it absorbed. A drop LOSES. The
# two are indistinguishable on screen, which is why they have to be told apart
# by a test. Measured on the live 25MB `updown-tape.jsonl` before the fix, a
# 5,000-record slice put 1,014 records on the panel and threw 3,965 away:
# 100% of ROLLs, 90% of window closes, and four of the seven armed symbols
# never appeared at all. The cause was `TapeFeed.accept` gating on `t` alone
# while the engine writes ONE `t` per fleet tick shared by every arm's record.
#
# The fixture below is that shape — one `t` per tick, every arm speaking on it
# — so the suite carries the bug's own conditions rather than a tidy tape that
# never had them.

import json as _json           # noqa: E402
import random as _random       # noqa: E402
import re as _re               # noqa: E402

import click as _click         # noqa: E402

_FLEET = ("btc-updown-5m", "eth-updown-5m", "sol-updown-5m", "xrp-updown-5m",
          "btc-updown-15m", "eth-updown-15m", "sol-updown-15m")
_TICK_S = 5.0                  # the engine's eval throttle
_AGG_AT = 9                    # _tape_head: "HH:MM:SS " then the fixed ×N cell


def _fleet_tape(n: int = 5000, t0: float = 1787500000.0, seed: int = 7) -> list[str]:
    """`n` raw tape lines shaped like the live fleet's.

    One `t` per tick, shared by every armed arm — and doubled at a window
    boundary, where each arm emits its close AND its roll on that same `t`
    (the live tape's max multiplicity is 14, which is exactly this).
    """
    rnd = _random.Random(seed)
    out: list[dict] = []
    start = {s: t0 for s in _FLEET}
    t = t0
    while len(out) < n:
        for base in _FLEET:
            dur = 300 if base.endswith("5m") else 900
            slug = f"{base}-{int(start[base])}"
            if t >= start[base] + dur:
                out.append({"t": t, "ev": "cleanup", "slug": slug})
                start[base] = t
                out.append({"t": t, "ev": "roll", "size": 100.0,
                            "slug": f"{base}-{int(t)}"})
                continue
            draw = rnd.random()
            if draw < 0.01:
                out.append({"t": t, "ev": "fire", "slug": slug, "side": "up",
                            "size": 20.0, "ask": 0.88, "fair": 0.9412,
                            "net": 0.0612, "rho": 0.31, "committed": 17.6})
            elif draw < 0.7:
                out.append({"t": t, "ev": "gated", "slug": slug,
                            "margin_bp": round(rnd.uniform(-9, 1), 2),
                            "guard_bp": 6.0, "up_ask": 0.52, "dn_ask": 0.49,
                            "reason": "basis guard: projected margin inside"})
            else:
                p = round(rnd.uniform(0.3, 0.7), 4)
                out.append({"t": t, "ev": "eval", "slug": slug, "p_up": p,
                            "rho": round(rnd.uniform(-0.4, 0.4), 2),
                            "committed": round(rnd.uniform(0, 200), 2),
                            "sides": [
                                {"side": "up", "ask": 0.52, "safety": 0.4,
                                 "net": round(rnd.uniform(-0.05, 0.05), 4)},
                                # The maker-quoting side: no ask, and so no net
                                # at all. 2.2% of live evals carry one.
                                {"side": "down", "ask": None, "safety": -0.4,
                                 "maker_px": 0.985, "maker_size": 10.0}
                                if draw > 0.95 else
                                {"side": "down", "ask": 0.49, "safety": -0.4,
                                 "net": round(rnd.uniform(-0.05, 0.05), 4)}]})
        t += _TICK_S
    return [_json.dumps(r) for r in out[:n]]


def _absorbed(lines) -> int:
    """How many records the panel's own lines SAY they stand for: one each,
    plus whatever a collapsed line's ×N counter claims it swallowed. Read out
    of the fixed aggregation cell, which is where every line type puts it."""
    total = 0
    for ln in lines:
        cell = _click.unstyle(ln)[_AGG_AT:_AGG_AT + watch_ui._TAPE_AGG_WIDTH]
        m = _re.search(r"×(\d+)", cell)
        total += int(m.group(1)) if m else 1
    return total


def _replay(raws):
    """Drive the real TapeFeed + TapeCollapser over raw lines, exactly as the
    render loop does. An unbounded list, not the panel's deque: conservation is
    a fact about the collapser, and a maxlen would scroll the evidence off."""
    feed = cw.TapeFeed(path="/nonexistent/no-local-tape.jsonl")
    collapser, lines = watch_ui.TapeCollapser(), []
    accepted = sum(1 for raw in raws if feed.accept(raw, collapser, lines))
    return accepted, lines


def test_every_record_is_rendered_or_counted_in_a_rendered_run():
    """THE conservation law. 5,000 records in; rendered + absorbed = 5,000."""
    raws = _fleet_tape(5000)
    accepted, lines = _replay(raws)

    assert accepted == 5000, f"the cursor dropped {5000 - accepted} records"
    assert _absorbed(lines) == 5000, (
        f"{len(lines)} lines account for {_absorbed(lines)} of 5000 records")
    assert len(lines) < 5000, "nothing collapsed at all — the rule stopped working"


def test_the_fleet_shares_one_t_per_tick_which_is_what_broke_the_cursor():
    """The fixture's own premise, pinned: if this ever stops holding, the
    conservation test above is passing on a tape the engine does not write."""
    ts = [_json.loads(r)["t"] for r in _fleet_tape(5000)]
    from collections import Counter
    mult = Counter(ts)
    assert max(mult.values()) >= 14
    assert sum(1 for a, b in zip(ts, ts[1:]) if a == b) > len(ts) * 0.7


def test_every_fire_reaches_the_panel():
    """A fire is a real trade and a singular event. Two live ones were
    suppressed by the cursor; a fire that leaves no line is a trade the
    operator never saw."""
    raws = _fleet_tape(5000)
    fires = sum(1 for r in raws if _json.loads(r)["ev"] == "fire")
    _, lines = _replay(raws)
    rendered = sum(1 for ln in lines if "FIRE UP" in _click.unstyle(ln))
    assert fires > 0 and rendered == fires


def test_every_roll_reaches_the_panel_as_the_consolidated_line():
    """100% of ROLLs were dropped: the roll and its close share the close's
    `t` with every other arm on the fleet. Each roll gets exactly one line,
    and it is the merged close→armed one."""
    raws = _fleet_tape(5000)
    rolls = sum(1 for r in raws if _json.loads(r)["ev"] == "roll")
    _, lines = _replay(raws)
    roll_lines = [ln for ln in lines if "ROLL" in _click.unstyle(ln)]
    assert rolls > 0 and len(roll_lines) == rolls
    assert all("closed → next window armed" in _click.unstyle(ln)
               for ln in roll_lines[:-1])  # the last may be a half pair at the cut


def test_no_armed_symbol_is_starved_off_the_panel():
    """Four of seven armed symbols never appeared at all: the cursor kept the
    lexically-first slug of each tick and threw the rest of the fleet away, so
    a quiet panel read as a quiet fleet."""
    raws = _fleet_tape(5000)
    _, lines = _replay(raws)
    plain = [_click.unstyle(ln) for ln in lines]
    for base in _FLEET:
        sym, _, dur = base.split("-")
        assert any(f"{sym} {dur} " in ln for ln in plain), f"{sym} {dur} is missing"


def test_a_collapsed_line_never_claims_more_records_than_it_absorbed():
    """The other half of conservation: a run whose line scrolled out of reach
    used to append a SECOND line restating the whole count, so the panel added
    up to 8% MORE records than the tape carried."""
    raws = _fleet_tape(2000)
    _, lines = _replay(raws)
    assert _absorbed(lines) == 2000
