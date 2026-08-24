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
    # the splice accessor must not reach the REAL dump under test
    monkeypatch.setattr(cs.wallet, "activity_since", lambda addr, floor, **kw: rows)
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
    monkeypatch.setattr(cs.wallet, "activity_since", lambda addr, floor, **kw: list(rows))
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
    charts, ONE table, tape — nothing else builds a Table.

    The charts row is deliberately inside this test rather than exempt from
    it. Its two panels are plain text (braille lines in a Text), so the rule
    still holds by the only measure that matters: exactly one `build_*_table`
    in the whole render layer, and no second grid competing with the windows
    panel for the operator's eye.
    """
    import inspect

    import watch_charts
    import watch_ui

    src = inspect.getsource(cw.crypto_watch.callback)
    assert src.count("split_column") == 1
    assert 'Layout(name="arms"' not in src
    assert [n for n in ("head", "charts", "windows", "tape") if f'name="{n}"' in src] == [
        "head", "charts", "windows", "tape"]
    builders = [n for n in dir(watch_ui)
                if n.startswith("build_") and n.endswith("_table")]
    assert builders == ["build_windows_table"], builders
    assert "Table" not in inspect.getsource(watch_charts)


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
             odds=None, regime_row=None, peers=()):
    """A WatchFetcher with every network seam replaced. The scoreboard seam
    is _tape_scoreboard — the SAME function `pmt crypto stats` runs, which
    is the whole point: one acquisition path, one truth.

    The regime gauge is stubbed for the same reason the tape feed and the
    corpus tails are pointed at nothing: each is backed by a file this box may
    or may not have, and no test of the OTHER fetchers may depend on which.
    `peers` is the peer-wallet list, empty by default so an operator's own
    PMT_FLEET_WALLETS cannot change what these tests assert."""
    state = cw.WatchState()
    monkeypatch.setattr(cw.regime, "latest", lambda *a, **k: regime_row)
    monkeypatch.setattr(cw.wallet, "peer_wallets", lambda *a, **k: list(peers))
    # A tape feed pointed at nothing, with the control plane stubbed dead: the
    # fetchers under test here are the OTHER four, and none of them may be
    # made to depend on whether this box happens to have a tape file.
    f = cw.WatchFetcher(state, sliding_floor=0.0,
                        tape_feed=cw.TapeFeed(path="/nonexistent/tape.jsonl"),
                        feed_tail=cw.FeedTail(rtds_dir="/nonexistent/rtds",
                                              spot_dir="/nonexistent/spot"))
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
    for name in ("status", "sb", "bal", "odds", "tape", "regime", "feeds", "peers"):
        monkeypatch.setattr(f, f"fetch_{name}", lambda n=name: ran.append(n))

    f.tick(1000.0)
    # first tick primes everything
    assert sorted(ran) == ["bal", "feeds", "odds", "peers", "regime", "sb",
                           "status", "tape"]
    ran.clear()

    f.tick(1001.0)                                   # nothing is due yet
    assert ran == []
    f.tick(1002.0)
    # engine, tape and the corpus tails: 2s. The tails are local-file reads of
    # bytes already appended, which is why they can ride the fast lane.
    assert sorted(ran) == ["feeds", "status", "tape"]
    ran.clear()
    f.tick(1010.0)
    assert sorted(ran) == ["feeds", "sb", "status", "tape"]    # scoreboard: 10s
    ran.clear()
    f.tick(1030.0)
    assert sorted(ran) == ["feeds", "odds", "sb", "status", "tape"]  # marks: 30s
    ran.clear()
    f.tick(1060.0)
    # balance, the regime gauge and the peer wallets share the slowest lane:
    # two are wallet calls, the third a tail read of a file that advances only
    # on settlement.
    assert sorted(ran) == ["bal", "feeds", "odds", "peers", "regime", "sb",
                           "status", "tape"]


# ---------- the regime gauge: a file read, on the worker, that gates nothing ----------

def test_the_regime_fetch_publishes_the_newest_corpus_row(monkeypatch):
    row = {"slug": "btc-updown-5m-1787538600", "end": 1787538900,
           "fleet_persist": 0.715, "fleet_n": 50}
    state, f = _fetcher(monkeypatch, regime_row=row)
    f.fetch_regime()
    assert state.read()["regime"] == row


def test_the_regime_fetch_touches_no_network_and_never_recomputes(monkeypatch):
    """A dashboard that re-derived the gauge would walk the whole book tape
    once a minute to repaint one line. It reads the tail of the corpus file
    `pmt crypto regime` writes, and nothing else."""
    state, f = _fetcher(monkeypatch)
    monkeypatch.setattr(cw, "_engine_post", _raiser(AssertionError("engine touched")))
    monkeypatch.setattr(cw, "_api", _raiser(AssertionError("api touched")))
    monkeypatch.setattr(cw.regime, "estimate",
                        _raiser(AssertionError("gauge recomputed")))
    f.fetch_regime()
    assert state.read()["regime"] is None      # cold start: None, not a zero


def test_a_failed_regime_read_keeps_the_last_good_gauge(monkeypatch):
    row = {"slug": "btc-updown-5m-1787538600", "end": 1787538900,
           "fleet_persist": 0.715, "fleet_n": 50}
    state, f = _fetcher(monkeypatch, regime_row=row)
    f.tick(0.0)
    assert state.read()["regime"] == row
    monkeypatch.setattr(cw.regime, "latest", _raiser(OSError("disk gone")))
    f.tick(10_000.0)                                  # must not raise
    # A regime is a slow-moving fact: the previous read is still the best
    # answer, and its `old` age label already says how much to trust it.
    assert state.read()["regime"] == row


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


_ID_AT = _AGG_AT + watch_ui._TAPE_AGG_WIDTH + 1   # the identity cell's column


def _ident(ln: str) -> tuple:
    """(symbols, duration) off a tape line's identity cell — one window's own
    label ("btc 5m 23:40") or a merged line's symbol set ("btc,eth+2 5m")."""
    parts = ln[_ID_AT:_ID_AT + watch_ui._TAPE_SLUG_WIDTH].strip().split(" ")
    if len(parts) < 2:
        return (), ""
    return tuple(parts[0].split("+")[0].split(",")), parts[1]


def test_no_armed_symbol_is_starved_off_the_panel():
    """Four of seven armed symbols never appeared at all: the cursor kept the
    lexically-first slug of each tick and threw the rest of the fleet away, so
    a quiet panel read as a quiet fleet."""
    raws = _fleet_tape(5000)
    _, lines = _replay(raws)
    idents = [_ident(_click.unstyle(ln)) for ln in lines]
    for base in _FLEET:
        sym, _, dur = base.split("-")
        assert any(sym in syms and d == dur for syms, d in idents), \
            f"{sym} {dur} is missing"


# ---------- cross-crypto aggregation ----------
#
# The fleet evaluates every arm on ONE tick, so five arms hitting the same gate
# for the same reason printed five near-identical rows, and the busiest series
# filled the visible tail on its own. The operator's read of that panel was
# that the other cryptos were not being evaluated at all. These pin the fix:
# one line, the symbols it covers, the combined count — and conservation still
# holding over it, because a merge SUMMARISES and a drop LOSES.

_LOCKSTEP = ("btc", "eth", "sol", "xrp", "bnb")


def _lockstep_tape(ticks: int = 40, t0: float = 1787500000.0) -> list[str]:
    """A quiet fleet: every arm refused by the same gate on every tick, its own
    margin steady inside tolerance. The shape that flooded the panel."""
    out = []
    for i in range(ticks):
        t = t0 + i * _TICK_S
        for j, sym in enumerate(_LOCKSTEP):
            out.append({"t": t, "ev": "gated", "slug": f"{sym}-updown-5m-{int(t0)}",
                        "margin_bp": -4.0 - j * 0.1 + (i % 3) * 0.1, "guard_bp": 6.0,
                        "reason": "basis guard: projected margin inside"})
    return [_json.dumps(r) for r in out]


def test_one_gate_across_the_fleet_is_one_line_naming_every_symbol():
    raws = _lockstep_tape(40)
    accepted, lines = _replay(raws)
    assert accepted == len(raws)
    assert len(lines) == 1, [_click.unstyle(ln) for ln in lines]
    ln = _click.unstyle(lines[0])
    assert _ident(ln) == (("bnb", "btc", "eth"), "5m")   # +2 counts the rest
    assert "×200" in ln                                  # 5 arms × 40 ticks
    assert "-4.4…-4.0/6.0bp" in ln    # the fleet's spread, not one per symbol


def test_a_merged_line_still_accounts_for_every_record_it_absorbed():
    """Conservation over the merge: a line standing for five arms must claim
    exactly the records it swallowed, no more and no fewer."""
    raws = _lockstep_tape(40)
    _, lines = _replay(raws)
    assert _absorbed(lines) == len(raws)


def test_a_quiet_arm_stays_visible_under_a_loud_ones_flood():
    """The operator's actual report: "I literally only see btc". One busy
    series used to fill the tail with a row per tick while four steady arms
    each added a fifth of the same. Merged, the fleet holds one line — and it
    reopens when its own line scrolls past _OWN_LOOKBACK, so it keeps a place
    in the last screenful rather than drifting off the top of it."""
    raws = []
    for i in range(60):
        t = 1787500000.0 + i * _TICK_S
        # btc's read moves past tolerance every tick: a fresh line each time
        raws.append(_json.dumps({
            "t": t, "ev": "eval", "slug": "btc-updown-5m-1787500000",
            "p_up": 0.40 + i * 0.005, "rho": 0.1, "committed": 12.0,
            "sides": [{"side": "up", "ask": 0.52, "net": 0.01, "safety": 0.4},
                      {"side": "down", "ask": 0.49, "net": -0.01, "safety": -0.4}]}))
        for sym in ("eth", "sol", "xrp", "bnb"):
            raws.append(_json.dumps({
                "t": t, "ev": "gated", "slug": f"{sym}-updown-5m-1787500000",
                "margin_bp": -4.0, "guard_bp": 6.0,
                "reason": "basis guard: projected margin inside"}))
    _, lines = _replay(raws)
    assert _absorbed(lines) == len(raws)
    tail = [_ident(_click.unstyle(ln)) for ln in lines[-20:]]
    for sym in ("eth", "sol", "xrp", "bnb"):
        assert any(sym in syms for syms, _ in tail), f"{sym} scrolled off the tail"


def test_a_collapsed_line_never_claims_more_records_than_it_absorbed():
    """The other half of conservation: a run whose line scrolled out of reach
    used to append a SECOND line restating the whole count, so the panel added
    up to 8% MORE records than the tape carried."""
    raws = _fleet_tape(2000)
    _, lines = _replay(raws)
    assert _absorbed(lines) == 2000


# ---------- the charts row ----------
#
# Two line charts, both drawn off data the worker already holds. The rules they
# are built on, and what each of these pins:
#
#   * a P&L figure comes from the WALLET GRADE and nowhere else — the same
#     `eff_windows` the header's ledger is folded from, never a mark, never a
#     stated fair off the tape;
#   * a second engine is a second WALLET, walked through the SAME
#     `_tape_scoreboard`, and shallowly enough that this box's activity dump
#     can never be spliced onto another account's rows;
#   * the feed charts read bytes APPENDED to the recorder corpora since the
#     last poll, on the worker — never a re-read, never on the render thread;
#   * every one of them drops rather than degrades into something wrong: a
#     screen too short loses the whole row, an unreachable peer loses its line,
#     a missing spot corpus loses the lead block whole.

import json                        # noqa: E402
import watch_charts as wc          # noqa: E402
import watch_feeds as wf           # noqa: E402


def _cols(values, n=None):
    """Dot-column values for a chart `n` cells wide (2 dot columns per cell)."""
    return list(values) if n is None else list(values)[: 2 * n]


# -- the canvas --

def test_a_braille_chart_draws_a_line_and_not_a_spray_of_dots():
    """At four dot rows a scatter of unconnected dots reads as noise. Every
    cell of a dense series must carry ink, and a jump must be joined."""
    ramp = [float(i) for i in range(40)]
    row = wc.sparkline(ramp, 20)
    assert len(row) == 20
    assert wc.BRAILLE_BLANK not in row and " " not in row
    # A one-column vertical jump is a stroke: the cell holds more than the two
    # dots its endpoints would set on their own.
    step = wc.sparkline([0.0, 0.0, 10.0, 10.0], 2)
    assert sum(bin(ord(c) - 0x2800).count("1") for c in step) > 4


def test_a_flat_series_never_renders_as_a_spike():
    """A quiet minute of chainlink prints is one price repeated. Dividing by a
    zero span would put every dot on one rail and read as a move."""
    flat = wc.sparkline([77_000.0] * 40, 20)
    assert len(set(flat)) == 1                      # one glyph, all the way
    assert flat[0] not in ("⠉", "⣀")                # neither rail


def test_a_value_off_the_scale_lands_on_the_rail_rather_than_vanishing():
    """A chart that silently loses its outlier is worse than one whose outlier
    sits on the edge — the exact figure is in the field beside it either way."""
    row = wc.sparkline([0.0, 500.0], 1, lo=-1.0, hi=1.0)
    assert (ord(row[0]) - 0x2800) & 0x08            # dot column 1, top row


def test_a_missing_sample_breaks_the_line_instead_of_being_drawn_through():
    row = wc.sparkline([1.0, None, None, None, None, 1.0], 3)
    assert row[1] == wc.BRAILLE_BLANK               # nothing invented in the gap
    assert row[0] != wc.BRAILLE_BLANK and row[2] != wc.BRAILLE_BLANK


# -- columns --

def test_downsample_carries_forward_and_never_invents_a_leading_value():
    """Carry-forward is right for a step series (a P&L curve IS one between
    settlements) and honest for a price gap. What it may never do is imply a
    value before the first sample — that would draw a flat zero where the
    answer is "no history yet"."""
    cols = wc.downsample([(50.0, 7.0), (90.0, 9.0)], 10, 0.0, 100.0)
    assert cols[:5] == [None] * 5                   # nothing before the first
    assert cols[5:9] == [7.0] * 4                   # carried, not interpolated
    assert cols[9] == 9.0


def test_downsample_keeps_the_last_sample_in_a_column():
    cols = wc.downsample([(1.0, 1.0), (2.0, 2.0), (3.0, 3.0)], 1, 0.0, 10.0)
    assert cols == [3.0]


def test_downsample_ignores_samples_outside_the_axis():
    cols = wc.downsample([(-5.0, 99.0), (5.0, 1.0), (500.0, 99.0)], 4, 0.0, 10.0)
    assert 99.0 not in cols


# -- P&L: the wallet grade, cumulated --

def _graded(pnl, end_ts, exit_ts=0.0):
    return {"pnl": pnl, "end_ts": end_ts, "exit_ts": exit_ts, "won": pnl > 0}


def test_cumulative_pnl_is_the_running_wallet_grade_in_settlement_order():
    now = 1_787_500_000.0
    curve = wc.cumulative_pnl([_graded(-5.0, now - 100), _graded(+2.0, now - 300),
                               _graded(+1.0, now - 50)])
    assert curve == [(now - 300, 2.0), (now - 100, -3.0), (now - 50, -2.0)]


def test_a_riding_window_is_not_pnl():
    """An undecided position has no verdict, so it has no line on this chart.
    Marking it to market here would put a number on the panel that the wallet
    has never agreed to — the exact drift the header's ledger refuses."""
    now = 1_787_500_000.0
    riding = {"pnl": None, "end_ts": now - 60, "exit_ts": 0.0, "won": None}
    assert wc.cumulative_pnl([riding, _graded(4.0, now - 30)]) == [(now - 30, 4.0)]


def test_the_pnl_curve_rebases_at_the_trailing_floor():
    """A 24h chart answers "what did today do". Opened at the all-time level
    it would flatten the day into a rounding error."""
    now = 1_787_500_000.0
    windows = [_graded(-900.0, now - 40 * 3600), _graded(+3.0, now - 3600),
               _graded(+2.0, now - 60)]
    cols, net = wc.pnl_series(windows, 8, now, 24 * 3600)
    assert net == 5.0                                # yesterday's -900 is not in it
    assert cols[0] == 0.0                            # the line opens on the axis
    assert cols[-1] == 5.0


def test_the_closing_figure_is_not_read_off_the_last_column():
    """A settlement landing inside the final column must not round the
    headline figure away: the columns are the picture, the number is the
    answer."""
    now = 1_787_500_000.0
    cols, net = wc.pnl_series([_graded(1.0, now - 3), _graded(1.0, now - 1)],
                              4, now, 3600.0)
    assert net == 2.0 and cols[-1] == 2.0


def test_the_exit_row_is_when_the_money_landed():
    now = 1_787_500_000.0
    w = _graded(3.0, now - 500, exit_ts=now - 100)
    assert wc.settle_ts(w) == now - 100
    assert wc.settle_ts(_graded(3.0, now - 500)) == now - 500


# -- P&L: one line per engine --

def _snap_with(**kw):
    base = {"sb": {}, "sb_fetched_at": None, "peers": {}, "feeds": {},
            "status": {}, "node": "desktop"}
    base.update(kw)
    return base


def test_both_engines_reach_the_pnl_panel_and_the_fleet_line_is_their_sum():
    """Hunter's ask, literally: "line charts for both P&Ls". This box off the
    scoreboard the header already prints, the peer off its own wallet walk,
    and a fleet line that is the two ledgers summed — safe to sum because the
    series partition means the two accounts never hold the same window."""
    now = 1_787_500_000.0
    snap = _snap_with(
        sb={"eff_windows": [_graded(+10.0, now - 600)]}, sb_fetched_at=now,
        peers={"eu": {"windows": [_graded(-4.0, now - 300)], "net": -4.0}})
    rows = wc.pnl_rows(snap, 60, now)
    labels = [r.split("[/dim]")[0].split("]")[-1].strip() for r in rows]
    assert labels == ["desktop", "eu", "fleet"]
    assert "+10.00" in rows[0] and "-4.00" in rows[1] and "+6.00" in rows[2]


def test_a_lone_engine_gets_no_fleet_line():
    """A "fleet" that is one box restating itself is a second row saying
    nothing."""
    now = 1_787_500_000.0
    snap = _snap_with(sb={"eff_windows": [_graded(1.0, now - 10)]}, sb_fetched_at=now)
    assert len(wc.pnl_rows(snap, 60, now)) == 1


def test_an_unreachable_peer_has_no_line_rather_than_a_flat_one():
    """"this box made nothing today" and "we cannot reach this box's ledger"
    are opposite facts and may not share a rendering."""
    now = 1_787_500_000.0
    snap = _snap_with(sb={"eff_windows": []}, sb_fetched_at=now,
                      peers={"eu": {"windows": None}})
    assert [e["label"] for e in wc.engine_curves(snap, now)] == ["desktop"]
    # ...whereas a peer that answered with nothing to show DOES get its line.
    snap["peers"] = {"eu": {"windows": []}}
    assert [e["label"] for e in wc.engine_curves(snap, now)] == ["desktop", "eu", "fleet"]


def test_no_pnl_line_at_all_until_the_first_wallet_walk_lands():
    """sb_fetched_at is the "—" the header's data-age already paints. A zero
    line drawn before the first walk is a confident claim about a ledger we
    have not read."""
    assert wc.pnl_rows(_snap_with(), 60, 1_787_500_000.0) == []


# -- the peer walk: same acquisition, another account --

def test_the_peer_walk_is_the_stats_acquisition_path_with_an_address(monkeypatch):
    """Same rule as fetch_sb, and for the same reason: two engines' P&L lines
    sitting one above the other must be two runs of ONE definition of a win."""
    calls = []
    state, f = _fetcher(monkeypatch, peers=[("eu", "0xdead")])
    monkeypatch.setattr(cw, "_tape_scoreboard",
                        lambda floor, **kw: calls.append((floor, kw)) or
                        {"eff_windows": [], "net": 0.0})
    f.fetch_peers()
    assert len(calls) == 1
    floor, kw = calls[0]
    assert kw["addr"] == "0xdead"
    # Shallow enough that wallet.activity_since never reaches this box's dump,
    # and the local tape has no opinion about another box's fires.
    assert time.time() - floor <= wallet.IMMUTABLE_AFTER_S
    assert kw["tape_records"] == []


def test_a_peer_walk_may_never_splice_this_boxs_activity_dump(monkeypatch):
    """The dump is ONE wallet's immutable past. Splicing it under another
    account's fresh rows would invent a ledger — refused, loudly, rather than
    mixed."""
    import cli_crypto_stats as cs

    monkeypatch.setattr(cs.wallet, "activity_since",
                        lambda *a, **k: pytest.fail("walked before refusing"))
    with pytest.raises(ValueError, match="splice"):
        cs._tape_scoreboard(0.0, addr="0xdead")


def test_one_flaky_peer_never_blanks_the_other(monkeypatch):
    state, f = _fetcher(monkeypatch, peers=[("eu", "0xaaa"), ("ap", "0xbbb")])

    def scoreboard(floor, **kw):
        if kw["addr"] == "0xbbb":
            raise ConnectionError("data-api down")
        return {"eff_windows": [_graded(1.0, floor + 10)], "net": 1.0}

    monkeypatch.setattr(cw, "_tape_scoreboard", scoreboard)
    f.fetch_peers()                                   # must not raise
    peers = state.read()["peers"]
    assert peers["eu"]["windows"] and peers["ap"]["windows"] is None


def test_a_peer_that_answered_once_keeps_its_line_through_a_later_failure(monkeypatch):
    state, f = _fetcher(monkeypatch, peers=[("eu", "0xaaa")])
    monkeypatch.setattr(cw, "_tape_scoreboard",
                        lambda floor, **kw: {"eff_windows": [_graded(1.0, floor + 5)],
                                             "net": 1.0})
    f.fetch_peers()
    good = state.read()["peers"]["eu"]
    monkeypatch.setattr(cw, "_tape_scoreboard",
                        _raiser(ConnectionError("data-api down")))
    f.fetch_peers()
    assert state.read()["peers"]["eu"] == good        # last good curve stands


def test_a_box_with_no_peer_configured_makes_no_extra_wallet_call(monkeypatch):
    state, f = _fetcher(monkeypatch)                  # peers=() by default
    monkeypatch.setattr(cw, "_tape_scoreboard",
                        _raiser(AssertionError("walked with no peer configured")))
    f.fetch_peers()
    assert state.read()["peers"] == {}


def test_peer_wallets_drops_junk_and_never_returns_this_box():
    env = {"PM_FUNDER_ADDRESS": "0xMINE",
           "PMT_FLEET_WALLETS": "eu=0xdead, =0xbeef, nope, mine=0xmine, ap=0xf00d"}
    assert wallet.peer_wallets(env) == [("eu", "0xdead"), ("ap", "0xf00d")]
    assert wallet.peer_wallets({}) == []
    assert wallet.node_label({}) == "local"
    assert wallet.node_label({"PMT_FLEET_NODE": "desktop"}) == "desktop"


# -- feeds: the underlying against the window's own strike --

def test_the_strike_is_the_reference_the_arm_was_priced_against():
    """One keying convention with polymarket.rtds_read.twap_marks and
    polymarket.crypto._model_twap — the print at `m+60` averages minute `m`,
    so a window opening at `start` reads its strike at `start - 60`. A
    different key here and the chart's zero line is not the market's."""
    start = 1_787_500_000.0
    assert wc.target_of({start - 60: 77_000.0}, start) == 77_000.0
    assert wc.target_of({start: 77_000.0}, start) is None
    assert wc.target_of(None, start) is None
    assert wc.target_of({start - 60: 0.0}, start) is None      # never a zero strike


def test_the_target_delta_is_the_basis_guards_own_axis():
    """bp above/below the strike — the same quantity the `gated  margin -4.9
    vs 6.0bp` cell reports, so the chart and the table share one axis."""
    assert wc.delta_bp(100.5, 100.0) == pytest.approx(50.0)
    assert wc.delta_bp(99.5, 100.0) == pytest.approx(-50.0)
    assert wc.delta_bp(100.0, None) is None and wc.delta_bp(None, 100.0) is None


def test_the_strike_is_always_on_the_feed_chart():
    """A window 20-40bp above its strike is entirely on one side. The axis
    stretches to include zero rather than centring on it, so the shape
    survives AND the strike stays the rail the line is measured off."""
    lo, hi = wc.span_with_zero([20.0, 30.0, 41.0])
    assert lo == 0.0 and hi == 41.0
    lo, hi = wc.span_with_zero([-3.0, 5.0])
    assert lo == -3.0 and hi == 5.0
    # A window that has barely moved is not drawn as a full-scale swing.
    lo, hi = wc.span_with_zero([0.05, -0.05])
    assert hi - lo >= 2.0


def test_the_side_we_hold_decides_the_colour_and_an_unfilled_arm_has_none():
    assert wc._side_style("up", 4.0) == "green"
    assert wc._side_style("up", -4.0) == "red"
    assert wc._side_style("down", -4.0) == "green"
    assert wc._side_style(None, 4.0) == "dim"        # nothing held, no stake
    assert wc._side_style("up", None) == "dim"       # no strike, no sign


def _armed_snap(now, syms=("btc",), dur="5m", start=None, feeds=None, **pos):
    start = int(now - 60) if start is None else start
    arms = {f"{s}-updown-{dur}-{start}": {"eval": {"state": "armed"}, "roll": True}
            for s in syms}
    sb = {"riding_windows": [dict({"slug": f"{s}-updown-{dur}-{start}"}, **pos)
                             for s in syms if pos], "windows": []}
    return _snap_with(status={"arms": arms}, sb=sb, feeds=feeds or {})


def test_a_feed_row_carries_the_path_the_strike_and_our_bet():
    now = 1_787_500_100.0
    start = int(now - 60)
    feeds = {"chain": {"btc": [(now - 30, 77_000.0), (now - 1, 77_077.0)]},
             "marks": {"btc": {start - 60: 77_000.0}}, "spot": {}, "venue": {}}
    snap = _armed_snap(now, feeds=feeds, start=start, side="up", entry_px=0.62)
    row = wc.feed_row(wc.armed_windows(snap)[0], feeds, 60, now)
    assert "btc 5m" in row and "▲" in row            # identity + the side we took
    assert "+10.0bp" in row                          # 77,077 vs a 77,000 strike
    assert "[green]" in row                          # up, and the price is above


def test_a_symbol_the_corpus_cannot_price_gets_no_row_rather_than_an_empty_one():
    """A price chart with no prices in it is a claim about the feed's health,
    and the header's own feed row is where that belongs."""
    now = 1_787_500_100.0
    snap = _armed_snap(now, feeds={"chain": {}, "marks": {}})
    assert wc.feed_row(wc.armed_windows(snap)[0], snap["feeds"], 60, now) is None


def test_a_window_with_no_strike_still_plots_its_price_and_says_so():
    now = 1_787_500_100.0
    feeds = {"chain": {"btc": [(now - 10, 77_000.0), (now - 1, 77_050.0)]},
             "marks": {}}
    snap = _armed_snap(now, feeds=feeds)
    row = wc.feed_row(wc.armed_windows(snap)[0], feeds, 60, now)
    assert "tgt —" in row and "bp" not in row


def test_the_feed_panel_never_paints_half_a_lead_comparison():
    """Two lines of one comparison or none. The panel used to build the
    symbol rows first and truncate from the bottom, which left the settlement
    line on screen with nothing to compare it against."""
    now = 1_787_500_100.0
    start = int(now - 60)
    syms = ("btc", "eth", "sol", "xrp", "bnb", "doge", "hype")
    feeds = {"chain": {s: [(now - 20, 100.0), (now - 1, 101.0)] for s in syms},
             "spot": {"btc": [(now - 20, 100.0), (now - 1, 101.0)]},
             "venue": {"btc": "binance"},
             "marks": {s: {start - 60: 100.0} for s in syms}}
    rows = wc.feeds_rows(_armed_snap(now, syms=syms, start=start, feeds=feeds), 60, now)
    assert len(rows) <= wc.CHARTS_MAX_INNER
    assert sum(1 for r in rows if "rtds" in r) == 1
    assert sum(1 for r in rows if "binance" in r) == 1
    # ...and every armed window the panel could not fit is counted, never
    # silently dropped.
    assert any("more armed" in r for r in rows)


def test_the_lead_block_drops_whole_when_a_venue_is_missing():
    now = 1_787_500_100.0
    feeds = {"chain": {"btc": [(now - 20, 100.0), (now - 1, 101.0)]},
             "spot": {}, "venue": {}, "marks": {}}
    rows = wc.feeds_rows(_armed_snap(now, feeds=feeds), 60, now)
    assert rows and not any("rtds" in r for r in rows)


def test_the_two_lead_lines_share_one_scale_and_each_is_centred_on_itself():
    """The rtds reference and a spot venue carry a standing basis wider than
    half a minute's range, so an absolute scale pins each to an opposite rail
    and the shapes stop being comparable. Centred, the lead reads as the
    horizontal offset it is."""
    now = 1_787_500_100.0
    shape = [1.0, 1.0, 2.0, 2.0]
    chain = [(now - 28 + i * 8, 100.0 + v) for i, v in enumerate(shape)]
    spot = [(now - 28 + i * 8, 5_000.0 + v) for i, v in enumerate(shape)]
    c_cols = wc.deviation_cols(chain, 2, now - 30, now)
    s_cols = wc.deviation_cols(spot, 2, now - 30, now)
    assert [v for v in c_cols if v is not None] == [v for v in s_cols if v is not None]
    lo, hi = wc.shared_span(c_cols, s_cols)
    assert wc.sparkline(c_cols, 2, lo, hi) == wc.sparkline(s_cols, 2, lo, hi)


def test_armed_windows_take_the_side_and_entry_from_the_wallet_not_the_arm():
    """Same merge the windows table makes: the engine's /status is the only
    source of an arm with no fill yet, the scoreboard the only source of a
    side and an entry price."""
    now = 1_787_500_100.0
    snap = _armed_snap(now, side="down", entry_px=0.44)
    w = wc.armed_windows(snap)[0]
    assert w["live"] is True and w["side"] == "down" and w["entry_px"] == 0.44


def test_armed_windows_lead_with_whatever_settles_first():
    now = 1_787_500_100.0
    arms = {"btc-updown-15m-1787500000": {"eval": {}},
            "eth-updown-5m-1787500000": {"eval": {}}}
    rows = wc.armed_windows(_snap_with(status={"arms": arms}))
    assert [r["slug"].split("-")[0] for r in rows] == ["eth", "btc"]


# -- geometry: the charts row is the first thing to go --

def test_a_box_with_nothing_to_chart_paints_no_charts_row():
    """The whole degradation contract. No peer wallet, no armed symbol, no
    corpus: no row at all, rather than an empty box taking rows off the tape."""
    assert wc.charts_inner_height(_snap_with(), 120) == 0


def test_the_charts_row_is_dropped_whole_when_the_screen_cannot_hold_it():
    """Braille loses its vertical resolution first, so a squeezed chart would
    still be drawn and still be read — just wrongly. It goes whole."""
    assert cw.charts_rows_shown(50, cw.HEAD_MIN_H, 8) == 8
    assert cw.charts_rows_shown(20, cw.HEAD_MIN_H, 8) == 0
    assert cw.charts_rows_shown(50, cw.HEAD_MIN_H, 0) == 0


def test_the_charts_row_never_costs_the_tape_its_floor_or_the_table_its_row():
    for h in range(16, 60):
        for head_h in (cw.HEAD_MIN_H, cw.HEAD_MIN_H + 2):
            charts_h = cw.charts_rows_shown(h, head_h, 8)
            n = cw.windows_rows_shown(h, 20, head_h, charts_h)
            assert 1 <= n <= cw.WINDOWS_MAX_ROWS
            left = h - head_h - charts_h - (n + cw.WINDOWS_CHROME)
            assert left >= cw.MIN_TAPE_ROWS or n == 1


def test_a_panel_too_narrow_for_a_line_keeps_the_numbers():
    """A smudge is worse than no chart. The figures are the answer either
    way, and they never go."""
    now = 1_787_500_000.0
    snap = _snap_with(sb={"eff_windows": [_graded(3.0, now - 10)]}, sb_fetched_at=now)
    wide, narrow = wc.pnl_rows(snap, 60, now), wc.pnl_rows(snap, 26, now)
    assert "+3.00" in wide[0] and "+3.00" in narrow[0]
    assert "⠀" not in narrow[0] and len(narrow[0]) < len(wide[0])


def test_the_two_panels_split_the_row():
    assert wc.split_widths(120) == (60, 60)
    assert sum(wc.split_widths(121)) == 121
    assert wc.split_widths(0) == (0, 0)


# -- the corpus tails: appended bytes only, on the worker --

def test_the_corpus_tail_reads_only_what_was_appended(tmp_path):
    """The rule the module exists to hold. rtds alone is ~240MB/day, so a
    re-read per poll — or per minute — is the blocking-call-in-the-render-loop
    shape the dashboard was split in two to avoid."""
    p = tmp_path / "rtds-20260824.jsonl"
    p.write_text('{"a": 1}\n{"a": 2}\n')
    tail = wf.CorpusTail(tmp_path, "rtds-*.jsonl", seed_bytes=1 << 20)
    assert tail.read_new() == ['{"a": 1}', '{"a": 2}']
    assert tail.read_new() == []                       # nothing appended
    with p.open("a") as fh:
        fh.write('{"a": 3}\n')
    assert tail.read_new() == ['{"a": 3}']             # ...and only the new one


def test_a_record_still_being_written_is_left_for_the_next_poll(tmp_path):
    p = tmp_path / "rtds-20260824.jsonl"
    p.write_text('{"a": 1}\n{"a": 2')                  # torn mid-write append
    tail = wf.CorpusTail(tmp_path, "rtds-*.jsonl")
    assert tail.read_new() == ['{"a": 1}']
    with p.open("a") as fh:
        fh.write('}\n')
    assert tail.read_new() == ['{"a": 2}']             # completed, then served


def test_the_first_sight_of_a_corpus_seeds_from_its_tail_not_its_head(tmp_path):
    """A day's file opened mid-run holds hours of history no chart plots.
    Reading it from byte 0 is the whole-corpus read this class refuses."""
    p = tmp_path / "rtds-20260824.jsonl"
    p.write_text("".join(f'{{"i": {i}}}\n' for i in range(2000)))
    tail = wf.CorpusTail(tmp_path, "rtds-*.jsonl", seed_bytes=64)
    lines = tail.read_new()
    assert len(lines) < 20 and lines[-1] == '{"i": 1999}'
    assert all(ln.startswith("{") for ln in lines)     # never half a record


def test_a_day_rollover_is_read_from_the_start(tmp_path):
    """The new file opens empty, so every byte of it is new — seeding from its
    tail would skip the day's first minutes."""
    (tmp_path / "rtds-20260824.jsonl").write_text('{"a": 1}\n')
    tail = wf.CorpusTail(tmp_path, "rtds-*.jsonl", seed_bytes=1 << 20)
    tail.read_new()
    (tmp_path / "rtds-20260825.jsonl").write_text('{"a": 2}\n{"a": 3}\n')
    assert tail.read_new() == ['{"a": 2}', '{"a": 3}']


def test_a_truncated_corpus_starts_over_rather_than_seeking_past_the_end(tmp_path):
    p = tmp_path / "rtds-20260824.jsonl"
    p.write_text('{"a": 1}\n{"a": 2}\n')
    tail = wf.CorpusTail(tmp_path, "rtds-*.jsonl")
    tail.read_new()
    p.write_text('{"a": 9}\n')                          # rotated under us
    assert tail.read_new() == ['{"a": 9}']


def test_a_stalled_worker_skips_forward_instead_of_parsing_the_backlog(tmp_path):
    """A suspended laptop must not answer by parsing an hour of corpus on one
    tick — it jumps to the recent end, which is the only part any chart plots."""
    p = tmp_path / "rtds-20260824.jsonl"
    p.write_text('{"a": 0}\n')
    tail = wf.CorpusTail(tmp_path, "rtds-*.jsonl", max_chunk=64)
    tail.read_new()
    with p.open("a") as fh:
        fh.write("".join(f'{{"i": {i}}}\n' for i in range(500)))
    lines = tail.read_new()
    assert 0 < len(lines) < 20 and lines[-1] == '{"i": 499}'


def test_a_missing_corpus_directory_is_no_samples_and_no_traceback():
    tail = wf.CorpusTail("/nonexistent/corpus", "rtds-*.jsonl")
    assert tail.read_new() == []


# -- the tails, joined into per-symbol paths --

def _rtds_line(topic, sym, ts, value):
    return json.dumps({"t_recv": ts, "topic": topic, "symbol": f"{sym}/usd",
                       "ts": int(ts * 1000), "value": value,
                       "full_accuracy_value": None,
                       "window_s": 60 if "sixty" in topic else None})


def _feed_tail(tmp_path):
    (tmp_path / "rtds").mkdir(exist_ok=True)
    (tmp_path / "spot").mkdir(exist_ok=True)
    return wf.FeedTail(rtds_dir=tmp_path / "rtds", spot_dir=tmp_path / "spot")


def test_the_feed_tail_banks_the_strike_the_arm_was_priced_against(tmp_path):
    """The tailed mark and polymarket.rtds_read.twap_marks must key a window's
    strike identically, or the chart's zero line is not the one the engine
    gated on. Same corpus, both readers, one answer."""
    from polymarket import rtds, rtds_read

    minute = 1_787_500_020 // 60 * 60 + 60             # a clean minute boundary
    rows = [_rtds_line(rtds.TOPIC_TWAP60, "btc", minute + 0.4, 77_000.0),
            _rtds_line(rtds.TOPIC_TWAP60, "btc", minute + 31.0, 77_999.0)]
    (tmp_path / "rtds").mkdir(exist_ok=True)
    (tmp_path / "rtds" / "rtds-20260824.jsonl").write_text("\n".join(rows) + "\n")

    tailed = _feed_tail(tmp_path).poll(now=minute + 60)["marks"]["btc"]
    read_back = rtds_read.twap_marks("btc/usd", 0.0, directory=tmp_path / "rtds")
    assert tailed == read_back == {float(minute - 60): 77_000.0}
    # ...and that IS the key polymarket.crypto._model_twap reads a window's
    # reference at.
    assert wc.target_of(tailed, float(minute)) == 77_000.0


def test_the_chainlink_path_is_on_the_settlement_clock(tmp_path):
    """`ts`, never `t_recv`: every boundary these markets settle on is defined
    on the chainlink clock, and our receive clock is the lookahead the corpus
    format exists to keep out."""
    from polymarket import rtds

    (tmp_path / "rtds").mkdir(exist_ok=True)
    (tmp_path / "rtds" / "rtds-20260824.jsonl").write_text(
        json.dumps({"t_recv": 9_999.0, "topic": rtds.TOPIC_SPOT,
                    "symbol": "btc/usd", "ts": 1_787_500_000_000,
                    "value": 77_000.0, "window_s": None}) + "\n")
    chain = _feed_tail(tmp_path).poll(now=1_787_500_001.0)["chain"]
    assert chain["btc"] == [(1_787_500_000.0, 77_000.0)]


def test_the_spot_path_is_on_the_venues_clock(tmp_path):
    """t_exch, for the same reason: a null there silently re-creates the
    ~1.7s lookahead that made the lead read backwards (polymarket/spot.py)."""
    now = 1_787_500_000.0
    (tmp_path / "spot").mkdir(exist_ok=True)
    (tmp_path / "spot" / "spot-binance-20260824.jsonl").write_text("\n".join([
        json.dumps({"t_recv": now, "venue": "binance", "ev": "start"}),
        json.dumps({"t_recv": now + 5, "t_exch": now, "venue": "binance",
                    "sym": "btc", "kind": "trade", "px": 77_000.0}),
    ]) + "\n")
    snap = _feed_tail(tmp_path).poll(now=now + 1)
    assert snap["spot"]["btc"] == [(now, 77_000.0)]     # the venue's clock
    assert snap["venue"]["btc"] == "binance"


def test_a_torn_corpus_line_never_reaches_a_chart(tmp_path, monkeypatch):
    """Belt with a mark. A write that finished badly loses its sample for
    good, so it is noted — but it may not take the poll, or the dashboard,
    down with it."""
    from polymarket import errlog, rtds

    noted = []
    monkeypatch.setattr(errlog, "note",
                        lambda site, exc, **kw: noted.append(site))
    (tmp_path / "rtds").mkdir(exist_ok=True)
    (tmp_path / "rtds" / "rtds-20260824.jsonl").write_text(
        '{"ts": 1787500000000, "topic": "x"\n'          # never closed
        + _rtds_line(rtds.TOPIC_SPOT, "btc", 1_787_500_001.0, 77_000.0) + "\n")
    snap = _feed_tail(tmp_path).poll(now=1_787_500_002.0)
    assert snap["chain"]["btc"] == [(1_787_500_001.0, 77_000.0)]
    assert any("rtds_line" in s for s in noted)


def test_samples_older_than_the_widest_axis_are_dropped(tmp_path):
    from polymarket import rtds

    now = 1_787_500_000.0
    (tmp_path / "rtds").mkdir(exist_ok=True)
    (tmp_path / "rtds" / "rtds-20260824.jsonl").write_text("\n".join(
        _rtds_line(rtds.TOPIC_SPOT, "btc", now - age, 77_000.0)
        for age in (10_000, 100, 1)) + "\n")
    chain = _feed_tail(tmp_path).poll(now=now)["chain"]
    assert [t for t, _ in chain["btc"]] == [now - 100, now - 1]


def test_the_feeds_fetch_touches_no_network(monkeypatch, tmp_path):
    state, f = _fetcher(monkeypatch)
    monkeypatch.setattr(cw, "_engine_post", _raiser(AssertionError("engine touched")))
    monkeypatch.setattr(cw, "_engine_get", _raiser(AssertionError("engine touched")))
    monkeypatch.setattr(cw, "_api", _raiser(AssertionError("api touched")))
    monkeypatch.setattr(cw, "_tape_scoreboard", _raiser(AssertionError("wallet walked")))
    f.feed_tail = _feed_tail(tmp_path)
    f.fetch_feeds()
    assert state.read()["feeds"]["chain"] == {}        # a cold corpus, not a crash


def test_a_failed_feeds_poll_keeps_the_last_snapshot(monkeypatch, tmp_path):
    """The charts freeze rather than blank, which is the honest read: those
    prices DID print, they just stopped arriving — and every feed row carries
    its window's own countdown, so a frozen panel beside a running clock says
    so."""
    from polymarket import rtds

    now = 1_787_500_000.0
    (tmp_path / "rtds").mkdir(exist_ok=True)
    (tmp_path / "rtds" / "rtds-20260824.jsonl").write_text(
        _rtds_line(rtds.TOPIC_SPOT, "btc", now - 1, 77_000.0) + "\n")
    state, f = _fetcher(monkeypatch)
    f.feed_tail = _feed_tail(tmp_path)
    f.fetch_feeds()
    good = state.read()["feeds"]
    f.feed_tail = None                                  # the poll now raises
    f.tick(1e12)
    assert state.read()["feeds"] is good


def test_the_backfill_reads_the_corpus_once_per_window_not_once_per_tick(
        monkeypatch, tmp_path):
    """A window whose opening print predates the dashboard needs ONE bounded
    reverse read. Rediscovering "the recorder was down then" every two seconds
    is the corpus re-read this module exists to refuse."""
    reads = []
    monkeypatch.setattr(wf.rtds_read, "twap_marks",
                        lambda sym, since, **kw: reads.append(sym) or {})
    tail = _feed_tail(tmp_path)
    now = 1_787_500_000.0
    slug = f"btc-updown-5m-{int(now - 120)}"
    for _ in range(5):
        tail.poll([slug], now=now)
    assert reads == ["btc/usd"]
    tail.poll([slug], now=now + wf.BACKFILL_RETRY_S + 1)
    assert len(reads) == 2                              # throttled, not abandoned


def test_a_backfilled_strike_is_never_re_read(monkeypatch, tmp_path):
    reads = []
    start = 1_787_500_000
    monkeypatch.setattr(wf.rtds_read, "twap_marks",
                        lambda sym, since, **kw: reads.append(sym) or
                        {float(start - 60): 77_000.0})
    tail = _feed_tail(tmp_path)
    slug = f"btc-updown-5m-{start}"
    for _ in range(3):
        snap = tail.poll([slug], now=start + 300 + wf.BACKFILL_RETRY_S * 3)
    assert reads == ["btc/usd"]
    assert wc.target_of(snap["marks"]["btc"], float(start)) == 77_000.0


def test_a_backfill_that_raises_leaves_the_panel_without_a_strike(
        monkeypatch, tmp_path):
    from polymarket import errlog

    noted = []
    monkeypatch.setattr(errlog, "note", lambda site, exc, **kw: noted.append(site))
    monkeypatch.setattr(wf.rtds_read, "twap_marks",
                        _raiser(OSError("corpus unreadable")))
    now = 1_787_500_000.0
    snap = _feed_tail(tmp_path).poll([f"btc-updown-5m-{int(now)}"], now=now)
    assert snap["marks"] == {}
    assert any("backfill" in s for s in noted)


def test_every_top_level_module_the_dashboard_imports_ships_with_the_package():
    """pyproject's `py-modules` list is not a formality.

    A top-level module missing from it imports perfectly from the source tree
    and ImportErrors the moment the package is INSTALLED — which is what `pmt
    crypto watch` did the first time the charts row landed, with a traceback
    the alternate screen never even got the chance to paint over. Tests import
    from the tree, so nothing else in this file can catch it.
    """
    import pathlib
    import tomllib

    root = pathlib.Path(cw.__file__).parent
    listed = set(tomllib.loads((root / "pyproject.toml").read_text())
                 ["tool"]["setuptools"]["py-modules"])
    loaded = {name for name, mod in sys.modules.items()
              if "." not in name and getattr(mod, "__file__", None)
              and pathlib.Path(mod.__file__).parent == root}
    assert loaded <= listed, sorted(loaded - listed)
