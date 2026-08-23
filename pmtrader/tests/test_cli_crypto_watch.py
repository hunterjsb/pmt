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


def _fetcher(monkeypatch, *, sb=None, status=None, bal=None, sb_boom=None):
    """A WatchFetcher with every network seam replaced. The scoreboard seam
    is _tape_scoreboard — the SAME function `pmt crypto stats` runs, which
    is the whole point: one acquisition path, one truth."""
    state = cw.WatchState()
    f = cw.WatchFetcher(state, sliding_floor=0.0)
    monkeypatch.setattr(wallet, "funder_address", lambda: "0xabc")
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
    for name in ("status", "sb", "bal"):
        monkeypatch.setattr(f, f"fetch_{name}", lambda n=name: ran.append(n))

    f.tick(1000.0)
    assert sorted(ran) == ["bal", "sb", "status"]   # first tick primes everything
    ran.clear()

    f.tick(1001.0)                                   # nothing is due yet
    assert ran == []
    f.tick(1002.0)
    assert ran == ["status"]                         # engine: 2s
    ran.clear()
    f.tick(1010.0)
    assert sorted(ran) == ["sb", "status"]            # scoreboard: 10s
    ran.clear()
    f.tick(1060.0)
    assert sorted(ran) == ["bal", "sb", "status"]     # balance: 60s


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
