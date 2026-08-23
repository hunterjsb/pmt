"""Pure-seam tests for the crypto watch dashboard: margin-regex parsing,
evidence/countdown color thresholds, the tape log's fixed-width alignment,
the risk header's committed/undecided math, per-window list collection +
win-imputation (wallet/tape monkeypatched — no live network), the
sliding/all-time dual scoreboard, and terminal-mode helpers.

Every dashboard render function must tolerate a missing/partial eval (an
engine restart mid-watch leaves last_eval None or half-built) — several
tests below exist specifically to pin that behavior down.
"""

from __future__ import annotations

import json
import sys
import time

import click
import pytest

import cli_crypto as cc


# ---------- margin-regex / gated-reason compaction ----------

def test_gated_reason_compact_parses_basis_guard():
    reason = ("basis guard: projected margin -4.9bp inside 6.0bp noise band "
              "[banked -3.2bp cushion 9.3bp]")
    assert cc._gated_reason_compact(reason) == "margin -4.9 vs 6.0bp"


def test_gated_reason_compact_positive_margin():
    reason = ("basis guard: projected margin +12.3bp inside 6.0bp noise band "
              "[banked 1bp cushion 2bp]")
    assert cc._gated_reason_compact(reason) == "margin +12.3 vs 6.0bp"


def test_gated_reason_compact_falls_back_to_raw_when_regex_misses():
    reason = "window 42% elapsed, firing opens at 50%"
    assert cc._gated_reason_compact(reason) == reason


def test_gated_reason_compact_handles_missing_reason():
    assert cc._gated_reason_compact(None) == "gated"
    assert cc._gated_reason_compact("") == "gated"


# ---------- evidence column color thresholds ----------

def test_evidence_style_banked_decided_is_always_green():
    assert cc._evidence_style(0.1, 9.3, True) == "green"


def test_evidence_style_ratio_thresholds():
    assert cc._evidence_style(9.3, 9.3, False) == "green"   # |banked| == cushion
    assert cc._evidence_style(3.0, 9.3, False) == "yellow"  # 0.3*9.3=2.79 <= 3.0 < 9.3
    assert cc._evidence_style(1.0, 9.3, False) == "dim"     # below theta-cleared band
    assert cc._evidence_style(1.0, 0.0, False) == "dim"     # zero cushion: no div-by-zero


def test_evidence_markup_missing_fields_render_dash():
    assert cc._evidence_markup({}) == "[dim]—[/dim]"
    assert cc._evidence_markup({"banked_bp": 1.0}) == "[dim]—[/dim]"  # cushion missing


# ---------- countdown column thresholds ----------

def test_countdown_style_thresholds():
    assert cc._countdown_style(30) == "bold red"   # < 60s
    assert cc._countdown_style(250) == "white"     # < 5min
    assert cc._countdown_style(400) == "dim"       # > 5min


def test_countdown_markup_unparseable_slug_is_dash():
    assert cc._countdown_markup("not-a-slug", 1000.0) == "[dim]—[/dim]"


def test_countdown_markup_past_end_shows_zero():
    slug = "btc-updown-5m-1000"  # end = 1300
    assert cc._countdown_markup(slug, 2000.0) == "[dim]0:00[/dim]"


# ---------- mode text ----------

def test_mode_text_prefers_quiesce_window_state():
    assert cc._mode_text({"state": "flip"}) == "flip"
    assert cc._mode_text({"state": "quiesce"}) == "quiesce"


def test_mode_text_falls_back_to_armed_mode_field():
    assert cc._mode_text({"state": "armed", "mode": "safe"}) == "safe"
    assert cc._mode_text({"state": "armed", "mode": "spec"}) == "spec"


def test_mode_text_missing_is_dash():
    assert cc._mode_text({}) == "—"


# ---------- tape log fixed-width alignment ----------

def test_tape_head_is_fixed_width_regardless_of_slug_length():
    short = cc._tape_head({"t": 1700000100, "slug": "btc-updown-5m-1700000000"})
    long = cc._tape_head({"t": 1700000100, "slug": "doge-updown-60m-1700000000"})
    assert len(short) == len(long) == 24  # "HH:MM:SS  " (10) + slug padded to 14


def test_tape_render_columns_align_across_event_types():
    slug = "doge-updown-60m-1700000000"  # exercises the widest display() form
    base_t = 1700000100
    head = cc._tape_head({"t": base_t, "slug": slug})
    fire = {"t": base_t, "slug": slug, "ev": "fire", "side": "up", "ask": 0.97,
            "fair": 0.99, "net": 0.01, "size": 10, "committed": 5.0, "rho": 0.1,
            "mode": "safe"}
    eval_ = {"t": base_t, "slug": slug, "ev": "eval", "p_up": 0.98, "rho": 0.2,
              "committed": 5.0, "banked_decided": False, "sides": []}
    gated = {"t": base_t, "slug": slug, "ev": "gated",
              "reason": "window 42% elapsed, firing opens at 50%"}
    roll = {"t": base_t, "slug": slug, "ev": "roll", "size": 25}

    rendered = {name: click.unstyle(cc._tape_render(json.dumps(r)))
                for name, r in (("fire", fire), ("eval", eval_),
                                 ("gated", gated), ("roll", roll))}
    for name, line in rendered.items():
        assert line[:len(head)] == head, name
        assert line[len(head)] == " ", name
        # the field after the fixed-width event tag starts at the same offset
        # for every event type — this is the actual alignment claim.
        assert line[len(head) + 1 + cc._TAPE_TAG_WIDTH] == " ", name


# ---------- risk header: committed / undecided ----------

def test_risk_committed_sums_filled_and_flags_undecided():
    arms = {
        "a": {"filled_usdc": 20.0, "eval": {"banked_decided": True}},
        "b": {"filled_usdc": 15.0, "eval": {"banked_decided": False}},
        "c": {"filled_usdc": 5.0, "eval": None},  # missing eval -> undecided
        "d": "not-a-dict",  # defensive: corrupt status entry, must not raise
    }
    committed, undecided = cc._risk_committed(arms)
    assert committed == 40.0
    assert undecided == 20.0  # b (15) + c (5)


def test_risk_committed_empty_or_none():
    assert cc._risk_committed({}) == (0.0, 0.0)
    assert cc._risk_committed(None) == (0.0, 0.0)


def test_build_risk_header_color_thresholds():
    sb = {"riding_n": 0, "riding_usd": 0.0}

    def undecided(amount):
        status = {"arms": {"a": {"filled_usdc": amount, "eval": {"banked_decided": False}}}}
        return cc.build_risk_header(status, {"total": 1000.0}, sb)

    assert "[red]" in undecided(600.0)
    assert "[yellow]" in undecided(400.0)
    line = undecided(200.0)
    assert "[red]" not in line and "[yellow]" not in line


def test_build_risk_header_missing_balance_shows_ellipsis():
    line = cc.build_risk_header({}, {}, {})
    assert "capital …" in line


# ---------- recent-windows strip ----------

def test_window_chip_formats_win_and_loss():
    win = cc._window_chip({"slug": "btc-updown-5m-1700000000", "won": True,
                            "pnl": 12.0, "est": False})
    loss = cc._window_chip({"slug": "eth-updown-15m-1700000000", "won": False,
                             "pnl": -44.0, "est": False})
    assert win == "[green]✓ btc5 +12[/green]"
    assert loss == "[red]✗ eth15 -44[/red]"


def test_window_chip_estimated_is_dim_not_win_loss_colored():
    chip = cc._window_chip({"slug": "sol-updown-5m-1700000000", "won": True,
                             "pnl": 3.0, "est": True})
    assert chip.startswith("[dim]") and "[green]" not in chip


def test_build_windows_strip_empty():
    assert cc.build_windows_strip([]) == "[dim]no resolved windows yet[/dim]"
    assert cc.build_windows_strip(None) == "[dim]no resolved windows yet[/dim]"


# ---------- arms table: missing/partial eval tolerance (4d) ----------

def test_build_arms_table_no_arms_shows_placeholder():
    from rich.console import Console

    t = cc.build_arms_table({}, now=1000.0)
    c = Console(record=True, width=140)
    c.print(t)
    assert "engine unreachable or no arms" in c.export_text()


def test_build_arms_table_tolerates_none_and_partial_eval():
    from rich.console import Console

    arms = {
        "btc-updown-5m-9999999999": {"filled_usdc": 10.0, "roll": True, "eval": None},
        "eth-updown-15m-8888888888": {"filled_usdc": 5.0, "roll": False,
                                       "eval": {"state": "armed"}},  # no p_up/rho/sides
        "not-a-real-slug": {},  # missing filled_usdc/roll entirely
    }
    t = cc.build_arms_table(arms, now=1000.0)  # must not raise
    c = Console(record=True, width=160)
    c.print(t)
    out = c.export_text()
    assert "—" in out  # missing fields degrade gracefully, not a traceback
    assert "$10.00" in out


def test_build_arms_table_column_geometry_is_identical_across_states():
    from rich.console import Console

    def col_boundaries(arms):
        t = cc.build_arms_table(arms, now=1700000000.0)
        c = Console(record=True, width=140)
        c.print(t)
        lines = [ln for ln in c.export_text().splitlines() if ln.startswith("│")]
        return [[i for i, ch in enumerate(ln) if ch == "│"] for ln in lines]

    short = col_boundaries({"btc-updown-5m-1700000300": {"filled_usdc": 1.0, "eval": None}})
    long_reason = ("basis guard: projected margin -4.9bp inside 6.0bp noise band "
                   "[banked -3.2bp cushion 9.3bp] and then some extra unexpected text")
    long_ = col_boundaries({
        "btc-updown-5m-1700000300": {"filled_usdc": 1.0,
                                      "eval": {"state": "gated", "reason": long_reason}},
        "eth-updown-15m-1700000900": {"filled_usdc": 2.0, "eval": {
            "state": "armed", "p_up": 0.8732, "mode": "safe", "rho": 0.4,
            "banked_bp": 12.3, "cushion_bp": 9.3, "banked_decided": True,
            "committed": 2.0, "sides": [{"side": "up", "safety": 0.9}],
        }},
    })
    assert short[0] == long_[0] == long_[1]


# ---------- per-window list collection + dual sliding/all-time scoreboard
#             (inline fixture: wallet/tape monkeypatched, no network) ----------

def _install_fake_pipeline(monkeypatch, rows, fires_by_slug, gamma_by_slug):
    monkeypatch.setattr(cc.wallet, "funder_address", lambda: "0xabc")
    monkeypatch.setattr(cc.wallet, "fetch_wallet_activity", lambda addr, floor: rows)

    def fake_iter_records(path, evs=None, floor=None):
        out = []
        for recs in fires_by_slug.values():
            out.extend(recs)
        return iter(out)

    monkeypatch.setattr(cc.tape, "iter_records", fake_iter_records)
    monkeypatch.setattr(cc, "_gamma_resolution_cached",
                         lambda slug: gamma_by_slug.get(slug))


def test_tape_scoreboard_collects_windows_riding_and_sliding(monkeypatch):
    now = int(time.time())
    a_start, a_end = now - 5000, now - 4700   # paid WIN, within sliding floor
    b_start, b_end = now - 100000, now - 99700  # $0 LOSS, outside sliding floor
    c_start, c_end = now - 4000, now - 3700   # gamma-WIN, no redeem yet (imputed), sliding
    d_start, _ = now - 60, now + 240           # still open/riding

    c_slug = f"sol-updown-5m-{c_start}"
    rows = [
        {"type": "TRADE", "side": "BUY", "usdcSize": 10.0, "size": 11.0,
         "slug": f"btc-updown-5m-{a_start}", "timestamp": a_start + 10},
        {"type": "REDEEM", "usdcSize": 11.0, "outcome": "up",
         "slug": f"btc-updown-5m-{a_start}", "timestamp": a_end + 30},
        {"type": "TRADE", "side": "BUY", "usdcSize": 8.0, "size": 9.0,
         "slug": f"eth-updown-5m-{b_start}", "timestamp": b_start + 10},
        {"type": "REDEEM", "usdcSize": 0.0,
         "slug": f"eth-updown-5m-{b_start}", "timestamp": b_end + 30},
        {"type": "TRADE", "side": "BUY", "usdcSize": 9.0, "size": 10.0,
         "slug": c_slug, "timestamp": c_start + 10},
        {"type": "TRADE", "side": "BUY", "usdcSize": 5.0, "size": 5.5,
         "slug": f"xrp-updown-5m-{d_start}", "timestamp": d_start + 10},
    ]
    fires = {c_slug: [{"ev": "fire", "slug": c_slug, "side": "up", "fair": 0.9,
                        "t": c_start + 10}]}
    gamma = {c_slug: {"resolved": True, "winner": "up"}}
    _install_fake_pipeline(monkeypatch, rows, fires, gamma)

    sliding_floor = now - 10000  # covers A, C, D; excludes B
    sb = cc._tape_scoreboard(0.0, sliding_floor=sliding_floor)

    assert sb["wins"] == 2 and sb["losses"] == 1  # all-time: A, C win; B loses
    assert sb["riding_n"] == 1 and sb["riding_usd"] == pytest.approx(5.0)  # D only
    assert sb["sliding"]["wins"] == 2 and sb["sliding"]["losses"] == 0
    assert sb["sliding"]["net"] == pytest.approx(2.0)  # A (+1) + C imputed (+1)
    assert sb["sliding"]["estimated"] == 1  # C's pnl is imputed

    windows = sb["windows"]
    assert [w["slug"] for w in windows] == [c_slug, f"btc-updown-5m-{a_start}",
                                             f"eth-updown-5m-{b_start}"]  # newest first
    c_row = next(w for w in windows if w["slug"] == c_slug)
    assert c_row["won"] is True and c_row["est"] is True
    assert c_row["pnl"] == pytest.approx(1.0)  # 10 shares*$1 - $9 buy
    b_row = next(w for w in windows if w["slug"].startswith("eth"))
    assert b_row["won"] is False and b_row["est"] is False  # exact, included not open


def test_tape_scoreboard_windows_capped_at_twelve(monkeypatch):
    now = int(time.time())
    rows = []
    for i in range(13):
        start = now - 10000 - i * 400
        slug = f"btc-updown-5m-{start}"
        rows.append({"type": "TRADE", "side": "BUY", "usdcSize": 1.0, "size": 1.0,
                     "slug": slug, "timestamp": start + 10})
        rows.append({"type": "REDEEM", "usdcSize": 1.0, "outcome": "up",
                     "slug": slug, "timestamp": start + 320})
    _install_fake_pipeline(monkeypatch, rows, {}, {})

    sb = cc._tape_scoreboard(0.0)
    assert len(sb["windows"]) == 12
    end_ts = [w["end_ts"] for w in sb["windows"]]
    assert end_ts == sorted(end_ts, reverse=True)


def test_tape_scoreboard_without_sliding_floor_omits_sliding_key(monkeypatch):
    _install_fake_pipeline(monkeypatch, [], {}, {})
    sb = cc._tape_scoreboard(0.0)
    assert "sliding" not in sb


# ---------- imputed win pnl (inline fixture) ----------

def test_impute_win_pnl_no_sells():
    # 20 shares bought for $18 -> a real WIN redeems at $20, so pnl ~= +$2
    # even before any redeem row has posted.
    assert cc._impute_win_pnl(buy_usd=18.0, sell_usd=0.0, buy_shares=20.0) == pytest.approx(2.0)


def test_impute_win_pnl_with_partial_sell():
    assert cc._impute_win_pnl(buy_usd=18.0, sell_usd=3.0, buy_shares=20.0) == pytest.approx(5.0)


# ---------- terminal cbreak / quit helpers ----------

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
    monkeypatch.setattr(cc.sys, "stdin", _FakeStdin(isatty=False))
    assert cc._cbreak_stdin() is None
    cc._restore_stdin(None)  # must not raise


def test_poll_key_none_when_not_a_tty(monkeypatch):
    monkeypatch.setattr(cc.sys, "stdin", _FakeStdin(isatty=False))
    assert cc._poll_key() is None


def test_poll_key_reads_q_when_ready(monkeypatch):
    monkeypatch.setattr(cc.sys, "stdin", _FakeStdin(isatty=True, chars="q"))
    monkeypatch.setattr(cc.select, "select", lambda r, w, x, t: ([0], [], []))
    monkeypatch.setattr(cc.os, "read", lambda fd, n: b"q")
    assert cc._poll_key() == "q"


def test_poll_key_returns_other_keys_lowercased(monkeypatch):
    monkeypatch.setattr(cc.sys, "stdin", _FakeStdin(isatty=True, chars="x"))
    monkeypatch.setattr(cc.select, "select", lambda r, w, x, t: ([0], [], []))
    monkeypatch.setattr(cc.os, "read", lambda fd, n: b"H")
    assert cc._poll_key() == "h"


def test_poll_key_none_when_nothing_ready(monkeypatch):
    monkeypatch.setattr(cc.sys, "stdin", _FakeStdin(isatty=True, chars=""))
    monkeypatch.setattr(cc.select, "select", lambda r, w, x, t: ([], [], []))
    assert cc._poll_key() is None


def test_quit_requested_swallows_select_errors(monkeypatch):
    monkeypatch.setattr(cc.sys, "stdin", _FakeStdin(isatty=True))

    def boom(*a, **k):
        raise OSError("bad fd")

    monkeypatch.setattr(cc.select, "select", boom)
    assert cc._poll_key() is None  # never raises, dashboard must survive


def test_controls_panel_renders():
    from rich.console import Console
    import io
    con = Console(file=io.StringIO(), width=160)
    con.print(cc._controls_panel())
    out = con.file.getvalue()
    assert "quit" in out and "controls" in out


def test_poll_key_passes_timeout_through_to_select(monkeypatch):
    seen = {}

    def fake_select(r, w, x, t):
        seen["timeout"] = t
        return ([], [], [])

    monkeypatch.setattr(cc.sys, "stdin", _FakeStdin(isatty=True))
    monkeypatch.setattr(cc.select, "select", fake_select)
    assert cc._poll_key() is None
    assert seen["timeout"] == 0.0          # default stays non-blocking
    assert cc._poll_key(0.05) is None
    assert seen["timeout"] == 0.05         # the watch loop's 20Hz pacing


def test_wait_key_without_a_tty_paces_instead_of_spinning(monkeypatch):
    slept = []
    monkeypatch.setattr(cc.sys, "stdin", _FakeStdin(isatty=False))
    monkeypatch.setattr(cc.time, "sleep", lambda s: slept.append(s))
    assert cc._wait_key(0.05) is None
    assert slept == [0.05]  # no tty to select on -> the sleep is the pacing


# ---------- watch: the render/fetch thread split ----------

class _FakeLedger:
    """Stands in for wallet.ActivityLedger: records refreshes, serves rows."""

    def __init__(self, rows=None, boom=None):
        self.rows = list(rows or [])
        self.refreshes = 0
        self.boom = boom

    def refresh(self, addr):
        self.refreshes += 1
        if self.boom:
            raise self.boom
        return 0


def _fetcher(monkeypatch, *, ledger=None, sb=None, status=None, bal=None):
    """A WatchFetcher with every network seam replaced."""
    state = cc.WatchState()
    f = cc.WatchFetcher(state, sliding_floor=0.0, ledger=ledger or _FakeLedger())
    monkeypatch.setattr(cc.wallet, "funder_address", lambda: "0xabc")
    monkeypatch.setattr(cc, "score_activity",
                        lambda rows, floor, sliding_floor=None: sb or {"wins": 1})
    monkeypatch.setattr(cc, "_engine_post",
                        (lambda *a, **k: status) if not isinstance(status, BaseException)
                        else _raiser(status))
    monkeypatch.setattr(cc, "_api", (lambda: _FakeApi(bal)) if not isinstance(bal, BaseException)
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
    st = cc.WatchState()
    snap = st.read()
    assert snap["status"] == {} and snap["bal"] == {}
    assert snap["sb_stale"] is False and snap["sb_fetched_at"] is None
    assert snap["err"] is None
    snap["status"] = {"arms": {"x": {}}}       # a renderer mangling its own copy
    assert st.read()["status"] == {}           # must not reach the shared state


def test_watch_state_update_swaps_whole_objects():
    st = cc.WatchState()
    sb = {"wins": 3, "losses": 1}
    st.update(sb=sb, sb_fetched_at=123.0)
    assert st.read()["sb"] is sb               # swapped in whole, not merged
    assert st.read()["sb_fetched_at"] == 123.0
    with pytest.raises(KeyError):
        st.update(bogus=1)                     # typo'd field must not vanish silently


def test_fetcher_publishes_scoreboard_off_the_ledger(monkeypatch):
    led = _FakeLedger(rows=[{"slug": "x"}])
    state, f = _fetcher(monkeypatch, ledger=led, sb={"wins": 7})
    f.fetch_sb()
    snap = state.read()
    assert led.refreshes == 1                  # incremental refresh, not a full walk
    assert snap["sb"] == {"wins": 7}
    assert snap["sb_stale"] is False and snap["sb_fetched_at"] is not None


def test_fetcher_scoreboard_failure_keeps_last_value_and_marks_stale(monkeypatch):
    led = _FakeLedger()
    state, f = _fetcher(monkeypatch, ledger=led, sb={"wins": 7})
    f.tick(0.0)                                # first pass: everything succeeds
    good = state.read()["sb"]
    assert good == {"wins": 7}

    led.boom = ConnectionError("data-api down")
    f.tick(1000.0)
    snap = state.read()
    assert snap["sb"] is good                  # last good numbers still on screen
    assert snap["sb_stale"] is True
    assert "ConnectionError" in snap["err"]

    led.boom = None                            # recovery clears both markers
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
    monkeypatch.setattr(cc, "_engine_post", noisy)
    before = sys.stdout
    f.tick(0.0)
    assert seen == [before]        # unchanged DURING the call
    assert sys.stdout is before    # and after it


def test_fetcher_balance_failure_keeps_last_capital(monkeypatch):
    state, f = _fetcher(monkeypatch, bal={"total": 500.0})
    f.tick(0.0)
    assert state.read()["bal"] == {"total": 500.0}
    monkeypatch.setattr(cc, "_api", _raiser(RuntimeError("rpc down")))
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
    th.join(timeout=cc.WORKER_JOIN_S)
    assert not th.is_alive()  # 'q' must never wait out a poll interval


def test_render_path_is_never_blocked_by_a_slow_fetch(monkeypatch):
    """The whole point of the split: a multi-second wallet walk on the worker
    must not delay the loop that reads keys and repaints."""
    import threading

    led = _FakeLedger()
    state, f = _fetcher(monkeypatch, ledger=led)
    started = threading.Event()

    def slow_refresh(addr):
        started.set()
        time.sleep(1.5)  # a full wallet walk, the old inline cost
        return 0

    led.refresh = slow_refresh
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
        th.join(timeout=cc.WORKER_JOIN_S + 2)
