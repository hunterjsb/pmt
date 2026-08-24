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
        # size > 0: a real worthless redemption (shares burned for $0), the
        # one shape rule 2 may still grade as an exact LOSS — a size-0 row is
        # a stub and says nothing (see the stub-redeem test below).
        {"type": "REDEEM", "usdcSize": 0.0, "size": 9.0,
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


def test_zero_share_stub_redeem_does_not_grade_a_loss(monkeypatch):
    """A 0-share/$0 REDEEM row is a stub, not a redemption — the data-api
    posted them beside two resolution-confirmed WINS (2026-08-23 23:01Z,
    -$38.69 of phantom loss). It must be ignored so the window falls through
    to the gamma cross-check and the imputed-win payout, instead of locking
    grade_window's $0-redeem rule into an exact LOSS."""
    now = int(time.time())
    start = now - 4000
    slug = f"bnb-updown-5m-{start}"
    rows = [
        {"type": "TRADE", "side": "BUY", "usdcSize": 28.88, "size": 30.0,
         "outcome": "Up", "slug": slug, "timestamp": start + 10},
        {"type": "REDEEM", "usdcSize": 0.0, "size": 0.0, "outcome": "Up",
         "slug": slug, "timestamp": start + 3400},
    ]
    _install_fake_pipeline(monkeypatch, rows, {}, {slug: {"resolved": True,
                                                           "winner": "up"}})

    sb = cs._tape_scoreboard(0.0)
    assert sb["wins"] == 1 and sb["losses"] == 0
    row = next(w for w in sb["windows"] if w["slug"] == slug)
    assert row["won"] is True and row["est"] is True  # payout imputed until the real redeem posts
    assert row["pnl"] == pytest.approx(30.0 - 28.88)


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


def test_a_filled_window_carries_its_identity_while_it_is_still_riding(monkeypatch):
    """Modelled on the real BNB window of 2026-08-23 14:35-14:40: filled at
    14:38, closed at 14:40, redeem row not posted until 14:41:43. For those
    ~4 minutes it was decided nowhere, and `riding_n`/`riding_usd` alone could
    not be rendered as a trade — so the dashboard showed nothing at all.
    """
    now = int(time.time())
    start = now - 400
    slug = f"bnb-updown-5m-{start}"       # ends now-100: inside the 300s grace
    rows = [
        {"type": "TRADE", "side": "BUY", "usdcSize": 9.62688, "size": 10.0,
         "slug": slug, "timestamp": start + 183},
        {"type": "TRADE", "side": "BUY", "usdcSize": 9.81372, "size": 10.0,
         "slug": slug, "timestamp": start + 216},
    ]
    fires = {slug: [{"ev": "fire", "slug": slug, "side": "up", "fair": 1.0,
                      "t": start + 183}]}
    _install_fake_pipeline(monkeypatch, rows, fires, {})

    sb = cs._tape_scoreboard(0.0)
    assert sb["riding_n"] == 1 and sb["riding_usd"] == pytest.approx(19.4406)
    assert sb["windows"] == []            # decided-only, unchanged contract

    r, = sb["riding_windows"]
    assert r["slug"] == slug and r["side"] == "up"
    assert r["won"] is None and r["pnl"] is None   # no verdict, never a fake $0
    assert r["notional"] == pytest.approx(19.4406)
    assert r["shares"] == pytest.approx(20.0)
    assert r["entry_px"] == pytest.approx(0.97203)
    assert r["end_ts"] == start + 300


def test_decided_windows_carry_the_side_and_entry_a_trade_row_needs(monkeypatch):
    now = int(time.time())
    start = now - 5000
    slug = f"eth-updown-5m-{start}"
    rows = [
        {"type": "TRADE", "side": "BUY", "usdcSize": 9.0, "size": 10.0,
         "slug": slug, "timestamp": start + 60},
        {"type": "REDEEM", "usdcSize": 10.0, "outcome": "down",
         "slug": slug, "timestamp": start + 330},
    ]
    fires = {slug: [{"ev": "fire", "slug": slug, "side": "down", "fair": 0.9,
                      "t": start + 60}]}
    _install_fake_pipeline(monkeypatch, rows, fires, {})

    w, = cs._tape_scoreboard(0.0)["windows"]
    assert w["side"] == "down"
    assert w["shares"] == pytest.approx(10.0)
    assert w["entry_px"] == pytest.approx(0.9)     # notional alone can't say it
    assert w["pnl"] == pytest.approx(1.0)


def test_tape_scoreboard_without_sliding_floor_omits_sliding_key(monkeypatch):
    _install_fake_pipeline(monkeypatch, [], {}, {})
    sb = cs._tape_scoreboard(0.0)
    assert "sliding" not in sb


# ---------- a LOST window is only ever visible in the market's resolution ----------
#
# The class of bug these pin (2026-08-23, real money): a losing position pays
# $0 and posts NO wallet row at all, so grading W-L off wallet rows alone books
# every win and can never book a silent loss. Three resolved-lost windows sat
# "riding" for 13-25h hiding -$272.35.

def test_a_resolved_window_we_lost_grades_as_a_loss_and_leaves_riding(monkeypatch):
    """Filled position + no redeem row of any kind + the market resolved the
    OTHER way. There is no wallet evidence and there never will be — the
    resolution is the only thing that can decide this window."""
    now = int(time.time())
    start = now - 50_000                       # long past the settlement grace
    slug = f"eth-updown-5m-{start}"
    rows = [
        {"type": "TRADE", "side": "BUY", "usdcSize": 147.91, "size": 1736.0,
         "outcome": "Down", "slug": slug, "timestamp": start + 186},
    ]
    fires = {slug: [{"ev": "fire", "slug": slug, "side": "down", "fair": 0.99,
                      "t": start + 186}]}
    _install_fake_pipeline(monkeypatch, rows, fires,
                            {slug: {"resolved": True, "winner": "up"}})

    sb = cs._tape_scoreboard(0.0)
    assert sb["riding_n"] == 0 and sb["riding_windows"] == []
    assert (sb["wins"], sb["losses"]) == (0, 1)
    assert sb["net"] == pytest.approx(-147.91)   # the whole stake, not $0
    w, = sb["windows"]
    assert w["won"] is False and w["est"] is False   # confirmed, not a guess


def test_a_resolved_loss_is_graded_even_after_its_fire_rotated_off_the_tape(monkeypatch):
    """The tape rotates; the wallet's BUY rows don't. Without the wallet-side
    fallback the resolution can't be matched to a side and the window rides
    forever — which is how btc-updown-5m-1787419200 showed side=None."""
    now = int(time.time())
    start = now - 50_000
    slug = f"sol-updown-5m-{start}"
    rows = [
        {"type": "TRADE", "side": "BUY", "usdcSize": 106.12, "size": 237.0,
         "outcome": "Down", "slug": slug, "timestamp": start + 210},
    ]
    _install_fake_pipeline(monkeypatch, rows, {},   # no fire record at all
                            {slug: {"resolved": True, "winner": "up"}})

    sb = cs._tape_scoreboard(0.0)
    assert sb["riding_n"] == 0
    assert (sb["wins"], sb["losses"]) == (0, 1)
    w, = sb["windows"]
    assert w["side"] == "down" and w["won"] is False


def test_a_window_bought_on_both_sides_stays_riding_rather_than_guessing(monkeypatch):
    """Two sides held and no fire: which one did we hold? Nothing here knows,
    so it says nothing — the fallback is evidence, not a coin flip."""
    now = int(time.time())
    start = now - 50_000
    slug = f"xrp-updown-5m-{start}"
    rows = [
        {"type": "TRADE", "side": "BUY", "usdcSize": 5.0, "size": 6.0,
         "outcome": "Up", "slug": slug, "timestamp": start + 60},
        {"type": "TRADE", "side": "BUY", "usdcSize": 5.0, "size": 6.0,
         "outcome": "Down", "slug": slug, "timestamp": start + 90},
    ]
    _install_fake_pipeline(monkeypatch, rows, {},
                            {slug: {"resolved": True, "winner": "up"}})

    sb = cs._tape_scoreboard(0.0)
    assert sb["riding_n"] == 1 and (sb["wins"], sb["losses"]) == (0, 0)


def test_a_window_sold_flat_is_decided_by_its_realized_pnl_not_by_a_redeem(monkeypatch):
    """btc-updown-5m-1787419200: bought 100sh @0.43, closed the lot out @0.80
    before settlement. Nothing is left to redeem, so no wallet row and no
    resolution is ever coming — waiting for one rode it for 26h at $44.72."""
    now = int(time.time())
    start = now - 90_000
    slug = f"btc-updown-5m-{start}"
    rows = [
        {"type": "TRADE", "side": "BUY", "usdcSize": 44.7157, "size": 100.0,
         "outcome": "Down", "slug": slug, "timestamp": start + 121},
        {"type": "TRADE", "side": "SELL", "usdcSize": 79.9989, "size": 99.998625,
         "outcome": "Down", "slug": slug, "timestamp": start + 201},
    ]
    # Resolution says our side won — irrelevant, the shares were already gone.
    _install_fake_pipeline(monkeypatch, rows, {},
                            {slug: {"resolved": True, "winner": "down"}})

    sb = cs._tape_scoreboard(0.0)
    assert sb["riding_n"] == 0 and sb["riding_windows"] == []
    assert (sb["wins"], sb["losses"]) == (1, 0)
    w, = sb["windows"]
    # Realized, not imputed: the $1/share payout it never collected would be
    # a $100 phantom on a window that made $35.
    assert w["pnl"] == pytest.approx(35.2832) and w["est"] is False


def test_the_resolution_lookup_asks_gamma_for_closed_markets(monkeypatch):
    """THE bug. /markets?slug=X defaults to closed=false, so every SETTLED
    window answers `[]` — which parses as "not resolved yet" and rides
    forever. Drop the flag and the hidden-loss class comes straight back."""
    seen = {}

    class _Resp:
        def raise_for_status(self):
            pass

        def json(self):
            return [{"outcomes": '["Up", "Down"]', "outcomePrices": '["1", "0"]'}]

    def _get(url, params=None, headers=None, timeout=None):
        seen.update(params or {})
        return _Resp()

    import requests
    monkeypatch.setattr(requests, "get", _get)
    cs._GAMMA_CACHE.clear()
    try:
        assert cs._gamma_resolution_cached("eth-updown-5m-1787462100") == {
            "resolved": True, "winner": "up"}
    finally:
        cs._GAMMA_CACHE.clear()
    assert seen.get("closed") == "true", "settled markets are invisible without it"


# ---------- imputed win pnl (inline fixture) ----------

def test_impute_win_pnl_no_sells():
    # 20 shares bought for $18 -> a real WIN redeems at $20, so pnl ~= +$2
    # even before any redeem row has posted.
    assert cs._impute_win_pnl(buy_usd=18.0, sell_usd=0.0, buy_shares=20.0) == pytest.approx(2.0)


def test_impute_win_pnl_only_imputes_the_shares_still_held():
    # 20 bought for $18, 5 of them sold for $3: only 15 shares are left to
    # redeem at $1. Imputing all 20 AND banking the $3 books those 5 twice
    # (it would say +$5 on a window actually worth $15 + $3 - $18 = $0).
    assert cs._impute_win_pnl(buy_usd=18.0, sell_usd=3.0, buy_shares=20.0,
                               sell_shares=5.0) == pytest.approx(0.0)


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


def test_gates_report_reads_each_chainlink_corpus_once_per_symbol(monkeypatch, tmp_path):
    """The Chainlink corpus is per SYMBOL. The window universe is per WINDOW.

    Keying the load off `windows` re-read and re-parsed each symbol's whole
    ~6k-round corpus file once per window — over a 1422-window universe that
    is ~240 reads of every file to build a 6-entry dict, measured at 31.7s of
    a 43s `--gates` run, and it grows every day the fleet trades. `pmt crypto
    outcomes` has always keyed off the symbol set; this was the copy that lost
    the dedupe.

    Pins the CALL COUNT, because the shape is the bug: a wall-time assertion
    would pass on a fast box with the quadratic still in place.
    """
    import json as _json

    from polymarket import chainlink as ck
    from polymarket import outcomes as oc

    # 18 closed windows over 2 symbols: the old shape asks for 18 corpus reads.
    starts = [1787000000 + 300 * i for i in range(9)]
    tape_file = tmp_path / "updown-tape.jsonl"
    with tape_file.open("w") as fh:
        for s in starts:
            for sym in ("btc", "eth"):
                fh.write(_json.dumps({"ev": "eval", "t": s + 10, "sides": [],
                                      "slug": f"{sym}-updown-5m-{s}"}) + "\n")
    monkeypatch.setattr(cs.tape, "UPDOWN_TAPE", str(tape_file))

    loads: list[str] = []
    monkeypatch.setattr(ck, "load_corpus",
                        lambda sym, since=None: (loads.append(sym), [])[1])
    # The corpus on disk is the operator's; a test never touches it.
    monkeypatch.setattr(oc, "load_outcomes", lambda *a, **k: {})
    monkeypatch.setattr(oc, "write_outcomes", lambda *a, **k: None)

    cs._gates_report([], 0.0)

    assert sorted(loads) == ["btc", "eth"], (
        f"expected one corpus read per symbol over {len(starts) * 2} windows, "
        f"got {len(loads)}: "
        f"{ {s: loads.count(s) for s in sorted(set(loads))} }")
