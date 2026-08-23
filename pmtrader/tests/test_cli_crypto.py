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

import pytest

import cli_crypto as cc


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
    c_start, _ = now - 4000, now - 3700       # gamma-WIN, no redeem yet (imputed), sliding
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
# ---------- `crypto arm` basis-guard default ----------

def test_arm_basis_guard_defaults_to_the_measured_per_symbol_value():
    # A bare arm on an alt must NOT get a flat band (docs/LESSONS.md#L32).
    assert cc._resolve_basis_guard(None, "BTCUSDT") == (6.0, None)
    assert cc._resolve_basis_guard(None, "ETHUSDT") == (8.0, None)
    assert cc._resolve_basis_guard(None, "SOLUSDT") == (10.0, None)


def test_arm_basis_guard_explicit_always_wins():
    for symbol in ("ETHUSDT", "XRPUSDT", "NOTAPAIR"):
        assert cc._resolve_basis_guard(2.5, symbol) == (2.5, None)


def test_arm_basis_guard_unmeasured_symbol_falls_back_loudly():
    from polymarket.constants import BASIS_NOISE_BP

    guard, warning = cc._resolve_basis_guard(None, "XRPUSDT")
    assert guard == BASIS_NOISE_BP
    assert warning and "XRPUSDT" in warning and "--basis-guard" in warning


def test_arm_basis_guard_unknown_symbol_falls_back_loudly():
    guard, warning = cc._resolve_basis_guard(None, "PEPEUSDT")
    assert guard == 3.0
    assert warning is not None


# ---------- `crypto arm --feed` ----------

def _arm(monkeypatch, evaluated, **kwargs):
    """Run `crypto arm` with the market pricing + engine post stubbed, and
    return the payload that would have gone to the engine."""
    from click.testing import CliRunner

    sent = {}
    monkeypatch.setattr("polymarket.crypto.eval_updown", lambda ref: evaluated)

    def fake_post(path, payload):
        sent.update(payload)
        return {"armed": evaluated["slug"]}

    monkeypatch.setattr(cc, "_engine_post", fake_post)
    args = ["https://polymarket.com/event/x", "--size", "100"]
    for k, v in kwargs.items():
        args += [f"--{k}", str(v)]
    result = CliRunner().invoke(cc.crypto_arm, args)
    return result, sent


_TWAP_MARKET = {
    "slug": "xrp-updown-5m-1787442000", "kind": "twap", "symbol": "XRPUSDT",
    "tokens": {"up": "1", "down": "2"}, "start": 1787442000.0, "end": 1787442300.0,
    "sigma_bp_per_min": 14.0, "fee_rate": 0.07, "rem_s": 250.0, "verdict": "ok",
}


def test_arm_feed_defaults_to_binance_and_plumbs_rtds_through(monkeypatch):
    result, payload = _arm(monkeypatch, dict(_TWAP_MARKET))
    assert result.exit_code == 0, result.output
    assert payload["feed"] == "binance", "the default must stay the old engine"

    result, payload = _arm(monkeypatch, dict(_TWAP_MARKET), feed="rtds")
    assert result.exit_code == 0, result.output
    assert payload["feed"] == "rtds"
    assert "feed rtds" in result.output


def test_arm_refuses_rtds_on_a_close_open_market(monkeypatch):
    # The settlement stream has no candle opens; the engine refuses this
    # too, but catching it here names the flag that caused it.
    market = dict(_TWAP_MARKET, kind="close_open")
    result, payload = _arm(monkeypatch, market, feed="rtds")
    assert result.exit_code != 0
    assert "close_open" in result.output and "--feed binance" in result.output
    assert payload == {}, "nothing reached the engine"


def test_arm_rejects_an_unknown_feed(monkeypatch):
    result, payload = _arm(monkeypatch, dict(_TWAP_MARKET), feed="coinbase")
    assert result.exit_code != 0
    assert payload == {}


# ---------- `crypto outcomes`: the folded-in oracle corpus refresh ----------
#  (`pmt crypto oracle` used to be a manual prerequisite of this command; the
#   fetch now runs inside it, so grading can never silently read a stale corpus
#   because the operator forgot the first step.)

def _rig_outcomes(monkeypatch, tmp_path, *, refresh, corpus=None, redeem_outcome="up"):
    """Run `crypto outcomes` over a synthetic tape with the RPC + data-api stubbed.

    Returns (result, calls) where `calls` is the ordered log of refresh /
    corpus-load / grade so a test can pin that the fetch precedes the grade.
    """
    from click.testing import CliRunner

    from polymarket import chainlink as ck
    from polymarket import outcomes as oc

    now = int(time.time())
    start = now - 3600
    slug = f"btc-updown-5m-{start}"
    end = start + 300

    updown = tmp_path / "updown-tape.jsonl"
    updown.write_text(json.dumps({"ev": "eval", "slug": slug, "t": start + 10}) + "\n")
    book = tmp_path / "book-tape.jsonl"
    book.write_text("")
    monkeypatch.setattr(cc.tape, "UPDOWN_TAPE", str(updown))
    monkeypatch.setattr(cc.tape, "BOOK_TAPE", str(book))

    calls: list[str] = []

    def fake_refresh(symbols, since, now_):
        calls.append(f"refresh:{','.join(symbols)}")
        return {s: dict(refresh) for s in symbols}

    def fake_load_corpus(sym, since=None):
        calls.append(f"load:{sym}")
        return list(corpus or [])

    monkeypatch.setattr(ck, "refresh_corpus", fake_refresh)
    monkeypatch.setattr(ck, "load_corpus", fake_load_corpus)

    real_build = oc.build_outcomes

    def fake_build(*a, **kw):
        calls.append("grade")
        return real_build(*a, **kw)

    monkeypatch.setattr(oc, "build_outcomes", fake_build)
    monkeypatch.setattr(cc.wallet, "funder_address", lambda: "0xabc")
    monkeypatch.setattr(cc.wallet, "fetch_wallet_activity", lambda addr, floor: [
        {"type": "REDEEM", "usdcSize": 11.0, "outcome": redeem_outcome,
         "slug": slug, "timestamp": end + 30},
    ])

    out_file = tmp_path / "outcomes.jsonl"
    args = ["--out", str(out_file), "--since", str(start - 60)]
    result = CliRunner().invoke(cc.crypto_outcomes, args)
    return result, calls, out_file


_REFRESH_OK = {"new": 7, "hours": 3.0, "error": None}


def test_outcomes_refreshes_the_oracle_corpus_before_grading(monkeypatch, tmp_path):
    result, calls, out_file = _rig_outcomes(monkeypatch, tmp_path, refresh=_REFRESH_OK)

    assert result.exit_code == 0, result.output
    assert calls.index("refresh:btc") < calls.index("load:btc") < calls.index("grade"), calls
    assert "oracle corpus: BTC +7 rounds" in result.output
    assert out_file.exists(), "grading still ran and wrote the outcomes file"


def test_outcomes_degrades_to_the_existing_corpus_when_the_fetch_fails(monkeypatch, tmp_path):
    # A dead Polygon RPC must not cost us the wallet-graded windows in the same
    # run — it warns, then grades off whatever corpus is already on disk.
    failed = {"new": 0, "hours": 0.0, "error": "all RPC urls failed"}
    result, calls, out_file = _rig_outcomes(monkeypatch, tmp_path, refresh=failed)

    assert result.exit_code == 0, result.output
    assert "oracle refresh failed" in result.output
    assert "all RPC urls failed" in result.output
    assert "oracle corpus:" not in result.output, "nothing was fetched, don't claim it was"
    assert "grade" in calls and calls.index("load:btc") < calls.index("grade")
    assert json.loads(out_file.read_text().strip())["source"] == "wallet"


def test_outcomes_fetch_only_stops_before_grading(monkeypatch, tmp_path):
    from click.testing import CliRunner

    from polymarket import chainlink as ck

    now = int(time.time())
    start = now - 3600
    updown = tmp_path / "updown-tape.jsonl"
    updown.write_text(json.dumps({"ev": "eval", "slug": f"btc-updown-5m-{start}",
                                   "t": start + 10}) + "\n")
    monkeypatch.setattr(cc.tape, "UPDOWN_TAPE", str(updown))
    monkeypatch.setattr(cc.tape, "BOOK_TAPE", str(tmp_path / "missing.jsonl"))

    refreshed: list[list[str]] = []

    def fake_refresh(symbols, since, now_):
        refreshed.append(list(symbols))
        return {s: dict(_REFRESH_OK) for s in symbols}

    monkeypatch.setattr(ck, "refresh_corpus", fake_refresh)

    def boom(*a, **kw):  # the cron form needs no wallet and must not call the data-api
        raise AssertionError("fetch-only must not touch the wallet")

    monkeypatch.setattr(cc.wallet, "funder_address", boom)
    monkeypatch.setattr(cc.wallet, "fetch_wallet_activity", boom)

    out_file = tmp_path / "outcomes.jsonl"
    result = CliRunner().invoke(cc.crypto_outcomes,
                                 ["--out", str(out_file), "--fetch-only"])

    assert result.exit_code == 0, result.output
    assert refreshed == [["btc"]]
    assert not out_file.exists(), "fetch-only never rewrites the outcomes corpus"


# ---------- removed commands stay removed ----------

def test_spot_and_oracle_are_gone_from_the_crypto_group():
    # `spot` duplicated updown/trigger/watch's own price line; `oracle` was only
    # ever a manual prerequisite of `outcomes`, which now fetches for itself.
    names = set(cc.crypto_group.commands)
    assert "spot" not in names and "oracle" not in names
    assert not hasattr(cc, "crypto_spot") and not hasattr(cc, "crypto_oracle")


def test_spot_price_helper_survives_because_pricing_still_uses_it():
    # Only the CLI wrapper went away; eval_updown reads the same Binance tick.
    from polymarket.crypto import spot_price

    assert callable(spot_price)
