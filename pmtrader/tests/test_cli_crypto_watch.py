"""Tests for the watch dashboard's fetch/render thread split.

The rule these exist to defend: watch is a VIEW of the stats acquisition, not
a second one. `test_watch_fetch_sb_is_the_stats_acquisition_path` asserts that
by identity, so re-introducing a cache or a "cheaper" incremental walk for the
dashboard fails a named test rather than drifting the P&L quietly (which is
exactly what it did for five patches before 2026-08-23).

The rest pin the split itself: every source has its own cadence, every failure
keeps the last good value, and no fetch may ever block a repaint.
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


def test_a_graded_trade_always_reaches_the_watch_trades_table(monkeypatch):
    """End-to-end over the ONE acquisition path: whatever score_activity
    grades is what the dashboard's trades panel paints.

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

    c = Console(record=True, width=90)
    c.print(watch_ui.build_trades_table(sb, time.time(), limit=cw.TRADES_MAX_ROWS))
    out = c.export_text()
    assert "eth 5m" in out, "a decided trade vanished between the grade and the table"
    assert "bnb 5m" in out, "a filled trade is invisible until its redeem posts"
    assert "riding" in out
    assert watch_ui.trades_title(sb) == "trades · last 1 decided · 1 riding"


def test_a_riding_position_stays_on_the_panel_through_a_roll_until_it_grades(monkeypatch):
    """The operator's ask: "don't leave the position too early when switching
    markets/quiescing so we can see how each position resolves".

    Three frames off the ONE acquisition path: the arm fills window A, then
    rolls to window B (nothing filled there yet), then A's redeem row posts.
    A must be on the trades panel in all three — riding through the roll, then
    decided — because the wallet, not the arm, is what retires a position.
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

    def panel_text():
        state = cw.WatchState()
        cw.WatchFetcher(state, sliding_floor=0.0).fetch_sb()
        sb = state.read()["sb"]
        c = Console(record=True, width=100)
        c.print(watch_ui.build_trades_table(sb, time.time(), limit=cw.TRADES_MAX_ROWS))
        return sb, c.export_text()

    # 1. filled, window A still open
    sb, out = panel_text()
    assert "btc 5m" in out and "riding" in out
    assert [w["slug"] for w in sb["riding_windows"]] == [a]

    # 2. the arm has rolled to B and armed it — A is nobody's current window
    #    any more, and used to vanish from every per-arm view at this point.
    fires.append({"ev": "fire", "slug": b, "side": "up", "fair": 1.0, "t": 0})
    sb, out = panel_text()
    assert [w["slug"] for w in sb["riding_windows"]] == [a], "the rolled-off position was dropped"
    assert "riding" in out

    # 3. the wallet grades it — and only now does it stop riding
    rows.append(redeem_a)
    sb, out = panel_text()
    assert sb["riding_windows"] == []
    assert [w["slug"] for w in sb["windows"]] == [a]
    assert "btc 5m" in out and "riding" not in out


def test_trades_panel_never_starves_the_tape_and_never_paints_a_clipped_box():
    """The panel is sized to the rows it will actually paint. Rich clips a
    Layout slot that overflows, and a table cut off below its last row (no
    bottom border) reads as a crash rather than as a cap."""
    # Roomy screen, plenty of trades: the view cap is what bites.
    assert cw.trades_rows_shown(50, 12, 16) == cw.TRADES_MAX_ROWS
    # Few trades: don't reserve rows for trades that don't exist.
    assert cw.trades_rows_shown(50, 12, 2) == 2
    # Cramped screen: the tape keeps its floor, one trade row still survives.
    assert cw.trades_rows_shown(30, 12, 16) == 1
    for h in range(20, 60):
        for head_h in (cw.HEAD_MIN_H, cw.HEAD_MIN_H + 2):
            n = cw.trades_rows_shown(h, 12, 16, head_h)
            assert 1 <= n <= cw.TRADES_MAX_ROWS
            # head + strip + arms + panel: what's left is the tape's.
            left = h - head_h - cw.STRIP_H - 12 - (n + cw.TRADES_CHROME)
            assert left >= cw.MIN_TAPE_ROWS or n == 1


def test_a_taller_header_costs_the_tape_rows_not_the_trades_floor():
    # The header grows a row for the settlement feed and one for a render
    # error; the trades panel keeps its floor of one row either way.
    roomy = cw.trades_rows_shown(50, 12, 16, cw.HEAD_MIN_H)
    taller = cw.trades_rows_shown(50, 12, 16, cw.HEAD_MIN_H + 2)
    assert roomy == taller == cw.TRADES_MAX_ROWS  # a roomy screen absorbs it
    assert cw.trades_rows_shown(31, 12, 16, cw.HEAD_MIN_H + 2) == 1


def _fetcher(monkeypatch, *, sb=None, status=None, bal=None, sb_boom=None,
             odds=None):
    """A WatchFetcher with every network seam replaced. The scoreboard seam
    is _tape_scoreboard — the SAME function `pmt crypto stats` runs, which
    is the whole point: one acquisition path, one truth."""
    state = cw.WatchState()
    f = cw.WatchFetcher(state, sliding_floor=0.0)
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
    for name in ("status", "sb", "bal", "odds"):
        monkeypatch.setattr(f, f"fetch_{name}", lambda n=name: ran.append(n))

    f.tick(1000.0)
    assert sorted(ran) == ["bal", "odds", "sb", "status"]  # first tick primes everything
    ran.clear()

    f.tick(1001.0)                                   # nothing is due yet
    assert ran == []
    f.tick(1002.0)
    assert ran == ["status"]                         # engine: 2s
    ran.clear()
    f.tick(1010.0)
    assert sorted(ran) == ["sb", "status"]            # scoreboard: 10s
    ran.clear()
    f.tick(1030.0)
    assert sorted(ran) == ["odds", "sb", "status"]    # position marks: 30s
    ran.clear()
    f.tick(1060.0)
    assert sorted(ran) == ["bal", "odds", "sb", "status"]  # balance: 60s


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
