"""Tests for the wallet acquisition + grading path behind `pmt crypto stats`.

The pipeline is monkeypatched at the wallet/tape seams — no live network. What
these pin is the grading contract every consumer of the scoreboard inherits:
which windows count, when a win is imputed, what the per-series median says
that the total hides, and that the raw rows are handed back only when a caller
asks to reuse them.
"""

from __future__ import annotations

import time

import pytest

import cli_crypto_stats as cs
from cli_crypto import crypto_group


def _install_fake_pipeline(monkeypatch, rows, fires_by_slug, gamma_by_slug):
    monkeypatch.setattr(cs.wallet, "funder_address", lambda: "0xabc")
    monkeypatch.setattr(cs.wallet, "fetch_wallet_activity", lambda addr, floor: rows)

    def fake_iter_records(path, evs=None, floor=None):
        out = []
        for recs in fires_by_slug.values():
            out.extend(recs)
        return iter(out)

    monkeypatch.setattr(cs.tape, "iter_records", fake_iter_records)
    monkeypatch.setattr(cs, "_gamma_resolution_cached",
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
    sb = cs._tape_scoreboard(0.0, sliding_floor=sliding_floor)

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

    sb = cs._tape_scoreboard(0.0)
    assert len(sb["windows"]) == 12
    end_ts = [w["end_ts"] for w in sb["windows"]]
    assert end_ts == sorted(end_ts, reverse=True)


def test_tape_scoreboard_without_sliding_floor_omits_sliding_key(monkeypatch):
    _install_fake_pipeline(monkeypatch, [], {}, {})
    sb = cs._tape_scoreboard(0.0)
    assert "sliding" not in sb


# ---------- imputed win pnl (inline fixture) ----------

def test_impute_win_pnl_no_sells():
    # 20 shares bought for $18 -> a real WIN redeems at $20, so pnl ~= +$2
    # even before any redeem row has posted.
    assert cs._impute_win_pnl(buy_usd=18.0, sell_usd=0.0, buy_shares=20.0) == pytest.approx(2.0)


def test_impute_win_pnl_with_partial_sell():
    assert cs._impute_win_pnl(buy_usd=18.0, sell_usd=3.0, buy_shares=20.0) == pytest.approx(5.0)


# ---------- per-series median (the row the totals hide) ----------

def test_series_carries_the_median_window_beside_the_total(monkeypatch):
    # Three windows on one series: +1, +1, -8. The sum says the series is
    # broken; the median says its typical window pays. Both are true, and the
    # report needs both.
    now = int(time.time())
    rows = []
    for i, (usd, redeem) in enumerate([(10.0, 11.0), (10.0, 11.0), (10.0, 2.0)]):
        start = now - 20000 - i * 400
        slug = f"btc-updown-5m-{start}"
        rows.append({"type": "TRADE", "side": "BUY", "usdcSize": usd, "size": usd,
                     "slug": slug, "timestamp": start + 10})
        rows.append({"type": "REDEEM", "usdcSize": redeem, "outcome": "up",
                     "slug": slug, "timestamp": start + 320})
    _install_fake_pipeline(monkeypatch, rows, {}, {})

    s = cs._tape_scoreboard(0.0)["series"]["btc 5m"]
    assert s["pnl"] == pytest.approx(-6.0)   # +1 +1 -8
    assert s["med"] == pytest.approx(1.0)
    assert "pnls" not in s  # the accumulator never leaks into the payload


def test_series_median_is_none_while_every_window_is_still_riding(monkeypatch):
    now = int(time.time())
    start = now - 60
    rows = [{"type": "TRADE", "side": "BUY", "usdcSize": 5.0, "size": 5.0,
             "slug": f"btc-updown-5m-{start}", "timestamp": start + 10}]
    _install_fake_pipeline(monkeypatch, rows, {}, {})
    assert cs._tape_scoreboard(0.0)["series"]["btc 5m"]["med"] is None


def test_scoreboard_hands_back_the_raw_rows_only_when_asked(monkeypatch):
    # The wallet walk is the slowest thing the report does; --gates and the
    # maker attribution reuse it rather than paginating a second time.
    _install_fake_pipeline(monkeypatch, [], {}, {})
    assert "activity" not in cs._tape_scoreboard(0.0)
    assert cs._tape_scoreboard(0.0, keep_activity=True)["activity"] == []


# ---------- stats blocks over a box with no tapes ----------

def test_stats_blocks_on_a_machine_with_no_engine_tapes(monkeypatch):
    # tape.iter_records yields nothing when the file is absent; every block
    # must come back empty so the renderer omits it, never raise.
    monkeypatch.setattr(cs.tape, "iter_records", lambda *a, **k: iter(()))
    blocks = cs._stats_blocks({"eff_windows": [], "activity": []}, {}, 0.0)
    assert blocks["flags"] == {}
    assert blocks["maker"]["rested"] == 0 and blocks["chase"]["acks"] == 0
    assert blocks["fleet"]["peak_undecided"] is None


def test_stats_blocks_splits_the_one_tape_read_into_evals_and_fires(monkeypatch):
    recs = [{"ev": "eval", "slug": "btc-updown-5m-1787452500", "t": 1.0,
             "fleet_room": 100.0, "sides": []},
            {"ev": "fire", "slug": "btc-updown-5m-1787452500", "t": 2.0,
             "ask": 0.94, "limit": 0.96}]

    def fake_iter(path, floor=None, evs=None):
        if path == cs.tape.ORDER_TAPE:
            return iter(())
        return iter(recs)

    monkeypatch.setattr(cs.tape, "iter_records", fake_iter)
    blocks = cs._stats_blocks({"eff_windows": [], "activity": []},
                              {"fleet_undecided_cap": 350.0}, 0.0)
    assert blocks["fleet"]["ticks"] == 1          # the eval record only
    assert blocks["chase"]["chase_n"] == 1        # the fire record only
    assert blocks["chase"]["chased"] == 1


# ---------- the shadow command folded into stats ----------

def test_shadow_is_gone_and_lives_on_as_a_stats_flag():
    # Its gate cost/saved summary is `pmt crypto stats --gates` now: one
    # report, one wallet walk, one place the operator looks. (That the command
    # itself is gone from the group is test_cli_crypto.py's half.)
    assert "shadow" not in set(crypto_group.commands)
    assert callable(cs._gates_report)


def test_stats_exposes_full_and_gates_without_changing_the_default():
    opts = {p.name for p in cs.crypto_stats.params}
    assert {"since", "full", "gates", "as_json"} <= opts
    defaults = {p.name: p.default for p in cs.crypto_stats.params}
    assert defaults["full"] is False and defaults["gates"] is False
