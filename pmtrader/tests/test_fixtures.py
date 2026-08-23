"""Pure tests for the characterization-fixture freezer. No network, no disk —
everything here is inline tape/activity/kline rows.

The rules under test are the ones a fixture cannot be wrong about: wallet-only
grading, params reconstructed from what the tape actually proves, nothing
address- or key-shaped in the file, and a renderer that round-trips.
"""

import json

import pytest

from polymarket.fixtures import (
    BOOK_KEYS,
    FIXTURE_KEYS,
    FIXTURE_VERSION,
    FixtureError,
    build_fixture,
    build_outcome,
    build_params,
    kline_slice,
    render_fixture,
    rtds_slice,
    rtds_symbol,
    secret_scan,
    sha256_records,
    slice_tape,
    trim_book_record,
    wallet_accounting,
)

SLUG = "btc-updown-15m-1787449500"

LIVE_ARM = {
    "slug": "btc-updown-5m-1787488500", "kind": "twap", "symbol": "BTCUSDT",
    "token_up": "37569315412307913531769841871632096812809146275489798887870639857173925284578",
    "token_down": "31203261710524184454227929117277797522756416976728960578082875828334911886202",
    "start": 1787488500.0, "end": 1787488800.0, "sigma_bp_per_min": 3.9,
    "fee_rate": 0.07, "size_usdc": 700.0, "min_edge": 0.015, "max_price": 0.985,
    "quiesce_secs": 20.0, "basis_guard_bp": 6.0, "side_filter": None,
    "min_fair": 0.97, "min_elapsed_frac": 0.0, "clip_usdc": 100.0,
    "clip_cooldown_s": 2.0, "early_frac": 0.2, "early_min_edge": 0.08,
    "late_rem_s": 120.0, "rho_block": -0.25, "pay_up_max": 0.02, "p_cap": 1.0,
    "theta": 0.3, "settle_rule": "range_avg", "manip_push_bp": 25.0,
    "roll": True, "feed": "binance", "maker_bid": False,
}


# ---------- slicing ----------

def test_slice_tape_takes_one_window_in_time_order():
    recs = [
        {"slug": SLUG, "t": 20.0, "ev": "eval"},
        {"slug": "btc-updown-15m-1787450400", "t": 15.0, "ev": "eval"},
        {"slug": SLUG, "t": 10.0, "ev": "fire"},
        {"ev": "cleanup"},  # no slug
    ]
    got = slice_tape(recs, SLUG)
    assert [r["t"] for r in got] == [10.0, 20.0]


def test_slice_tape_never_prefix_matches():
    # "btc-updown-15m-17874495000" starts with the slug; a fixture is ONE window.
    recs = [{"slug": SLUG + "0", "t": 1.0}]
    assert slice_tape(recs, SLUG) == []


def test_trim_book_record_keeps_only_what_replay_reads():
    rec = {
        "t": 1.0, "ev": "book", "slug": SLUG, "spot": 100.0, "spot_age_s": 0.5,
        "up_bid": 0.4, "up_bid_sz": 10.0, "up_ask": 0.5, "up_ask_sz": 20.0,
        "dn_bid": 0.5, "dn_bid_sz": 30.0, "dn_ask": 0.6, "dn_ask_sz": 40.0,
        # corpus for other studies, not decision inputs
        "up_tbuy": 1.0, "up_tn": 2, "up_tsell": 3.0, "up_src": "ws", "up_age_ms": 12,
    }
    trimmed = trim_book_record(rec)
    assert set(trimmed) == set(BOOK_KEYS)
    assert list(trimmed) == [k for k in BOOK_KEYS]  # order stable for clean diffs


def test_kline_slice_spans_the_full_lookback_and_reports_holes():
    start, end = 3600, 4200
    lo = (start - 2700) // 60 * 60
    rows = [{"t": t, "o": 1.0, "c": 2.0} for t in range(lo, end, 60)]
    sliced, missing = kline_slice(rows, start, end)
    assert missing == []
    assert len(sliced) == (end - lo) // 60
    assert sliced[0]["t"] == lo and sliced == sorted(sliced, key=lambda r: r["t"])

    holed, missing = kline_slice([r for r in rows if r["t"] != lo + 120], start, end)
    assert missing == [lo + 120]
    assert len(holed) == len(sliced) - 1


def test_kline_slice_dedupes_and_drops_junk():
    lo = (3600 - 2700) // 60 * 60
    rows = [{"t": lo, "o": 1.0, "c": 2.0}, {"t": lo, "o": 1.0, "c": 9.0},
            {"t": lo + 60, "o": None, "c": 2.0}, {"t": "x", "o": 1.0, "c": 2.0}]
    sliced, missing = kline_slice(rows, 3600, 3660)
    assert len(sliced) == 1 and sliced[0]["c"] == 9.0  # last write wins, deduped by t
    assert lo + 60 in missing  # the unparseable row left a hole rather than a guess


# ---------- wallet truth ----------

def _activity(slug=SLUG):
    return [
        {"slug": slug, "type": "TRADE", "side": "BUY", "usdcSize": 100.0,
         "size": 130.0, "outcome": "Up"},
        {"slug": slug, "type": "TRADE", "side": "BUY", "usdcSize": 20.0,
         "size": 25.0, "outcome": "Up"},
        {"slug": slug, "type": "TRADE", "side": "SELL", "usdcSize": 5.0, "size": 6.0},
        {"slug": slug, "type": "REDEEM", "usdcSize": 0.0, "size": 0.0, "outcome": "Up"},
        {"slug": "other-updown-5m-1", "type": "TRADE", "side": "BUY", "usdcSize": 999.0},
    ]


def test_wallet_accounting_sums_one_window_only():
    a = wallet_accounting(_activity(), SLUG)
    assert a["buy"] == 120.0
    assert a["buy_shares"] == 155.0
    assert a["buy_side"] == "up"
    assert a["sell"] == 5.0
    assert a["redeem"] == 0.0
    assert a["redeem_seen"] is True
    # A $0 redeem names nothing (L22) — the winner comes from the graded corpus.
    assert a["redeem_outcome"] is None
    assert a["pnl"] == -115.0


def test_build_outcome_refuses_a_derived_label():
    acct = wallet_accounting(_activity(), SLUG)
    with pytest.raises(FixtureError, match="chainlink-graded"):
        build_outcome({"slug": SLUG, "winner": "down", "source": "chainlink"}, acct, SLUG)
    with pytest.raises(FixtureError, match="book-graded"):
        build_outcome({"slug": SLUG, "winner": "down", "source": "book"}, acct, SLUG)


def test_build_outcome_refuses_an_ungraded_window():
    acct = wallet_accounting(_activity(), SLUG)
    with pytest.raises(FixtureError, match="no graded outcome"):
        build_outcome(None, acct, SLUG)


def test_build_outcome_carries_the_wallet_accounting():
    acct = wallet_accounting(_activity(), SLUG)
    o = build_outcome({"slug": SLUG, "winner": "down", "source": "wallet"}, acct, SLUG)
    assert o["winner"] == "down" and o["source"] == "wallet"
    assert o["buy"] == 120.0 and o["pnl"] == -115.0
    assert "slug" not in o  # the fixture already names the slug


# ---------- params as armed ----------

def _tape(**kw):
    """A minimal window: a roll, two evals, two fires."""
    recs = [
        {"ev": "roll", "t": 1787449500.0, "slug": SLUG, "size": 500.0},
        {"ev": "eval", "t": 1787449600.0, "slug": SLUG, "p_up": 0.9,
         "sides": [{"side": "up", "ask": 0.8}]},
        {"ev": "fire", "t": 1787449610.0, "slug": SLUG, "side": "up",
         "ask": 0.8, "size": 55.0, "mode": "spec"},          # $44.00
        {"ev": "fire", "t": 1787449700.0, "slug": SLUG, "side": "up",
         "ask": 0.94, "size": 52.0, "mode": "safe"},          # $48.88
    ]
    if kw.get("guard_bp") is not None:
        for r in recs:
            if r["ev"] == "eval":
                r["guard_bp"] = kw["guard_bp"]
    if kw.get("safety_brake"):
        recs[1]["sides"][0]["brake"] = "safety"
    return recs


def test_build_params_prefers_the_tape_over_the_arm_store():
    p, prov = build_params(SLUG, _tape(guard_bp=3.0), LIVE_ARM)
    assert p["size_usdc"] == 500.0
    assert prov["size_usdc"].startswith("tape:")
    assert p["basis_guard_bp"] == 3.0
    assert prov["basis_guard_bp"].startswith("tape:")
    # Largest fired clip $48.88 rounds up to the $50 the arm actually ran.
    assert p["clip_usdc"] == 50.0
    assert prov["clip_usdc"].startswith("tape:")


def test_build_params_falls_back_to_the_series_roll_then_the_arm_store():
    no_roll = [r for r in _tape() if r["ev"] != "roll"]
    p, prov = build_params(SLUG, no_roll, LIVE_ARM, series_roll_size=300.0)
    assert p["size_usdc"] == 300.0
    assert "series" in prov["size_usdc"]

    p, prov = build_params(SLUG, no_roll, LIVE_ARM)
    assert p["size_usdc"] == LIVE_ARM["size_usdc"]
    assert prov["size_usdc"].startswith("inherited")


def test_build_params_keeps_a_pre_r9_window_pre_r9():
    # No `safety` brake in the tape => theta was 0 when this window ran, because
    # the brake only exists when theta > 0 (updown_model::safety_gate_blocks).
    p, prov = build_params(SLUG, _tape(), LIVE_ARM)
    assert p["theta"] == 0.0
    assert "pre-R9" in prov["theta"]

    p, prov = build_params(SLUG, _tape(safety_brake=True), LIVE_ARM)
    assert p["theta"] == LIVE_ARM["theta"]
    assert "safety` brake recorded" in prov["theta"]


def test_build_params_never_invents_a_pay_up_chase():
    # A slice cut before the fire record carried `limit` cannot prove a
    # chase — 0 replays the clip at the ask it recorded.
    p, prov = build_params(SLUG, _tape(), LIVE_ARM)
    assert p["pay_up_max"] == 0.0
    assert "NOT RECOVERABLE" in prov["pay_up_max"]


def test_build_params_reads_the_chase_off_a_tape_that_records_the_limit():
    # With `limit` on the record, the largest limit-over-ask is a tight
    # lower bound on the armed budget — the clip_usdc rule applied to the
    # pay-up budget. This is what closes fixtures/README.md gap 2.
    recs = _tape()
    for r in recs:
        if r["ev"] == "fire":
            r["limit"] = r["ask"]
    recs[-1]["limit"] = 0.955          # chased 1.5c above a 0.94 ask
    p, prov = build_params(SLUG, recs, LIVE_ARM)
    assert p["pay_up_max"] == pytest.approx(0.015)
    assert prov["pay_up_max"].startswith("tape:")


def test_build_params_records_a_recorded_window_that_simply_never_chased():
    # `limit == ask` on every clip is EVIDENCE of no chase, not absence of
    # evidence — and the provenance has to say which of the two it is.
    recs = _tape()
    for r in recs:
        if r["ev"] == "fire":
            r["limit"] = r["ask"]
    p, prov = build_params(SLUG, recs, LIVE_ARM)
    assert p["pay_up_max"] == 0.0
    assert "no clip chased" in prov["pay_up_max"]
    assert "NOT RECOVERABLE" not in prov["pay_up_max"]


# ---------- the rtds slice ----------

def test_rtds_symbol_mirrors_the_rust_mapping():
    assert rtds_symbol("XRPUSDT") == "xrp/usd"
    assert rtds_symbol("ETHUSD") == "eth/usd"
    assert rtds_symbol("DOGEUSDC") == "doge/usd"
    assert rtds_symbol("USDT") is None


def _rtds_rows(lo, hi, symbol="xrp/usd", value=2.5):
    """1 Hz chainlink plus a 30s and 60s TWAP print every second."""
    rows = []
    for ts in range(lo, hi):
        for topic, w in (("crypto_prices_chainlink", None),
                         ("crypto_prices_twap_thirty", 30),
                         ("crypto_prices_twap_sixty", 60)):
            rows.append({"t_recv": ts + 0.2, "topic": topic, "symbol": symbol,
                         "ts": ts * 1000, "value": value,
                         "full_accuracy_value": str(int(value * 1e18)),
                         "window_s": w})
    return rows


def test_rtds_slice_thins_history_to_one_close_a_minute_and_keeps_the_window_dense():
    start, end = 1_787_442_300, 1_787_442_600
    rows = _rtds_rows(start - 7200, end + 120)
    kept, coverage = rtds_slice(rows, "xrp/usd", start, end)
    spot = [r for r in kept if r["topic"] == "crypto_prices_chainlink"]
    dense = [r for r in spot if r["ts"] // 1000 >= start - 300]
    sparse = [r for r in spot if r["ts"] // 1000 < start - 300]

    # Inside the dense lead: every print, because spot_ts is receive-time
    # freshness and a thinned slice would gate on staleness that never was.
    assert len(dense) == 300 + (end - start) + 120
    # Before it: exactly one a minute — the close the router would bank.
    minutes = {r["ts"] // 1000 // 60 for r in sparse}
    assert len(sparse) == len(minutes) == (7200 - 300) // 60
    assert coverage == (start - 7200 + 0.2, end + 120 - 1 + 0.2)


def test_rtds_slice_keeps_only_prints_that_could_ever_be_a_mark():
    start, end = 1_787_442_300, 1_787_442_600
    kept, _ = rtds_slice(_rtds_rows(start - 7200, end + 120), "xrp/usd", start, end)
    for topic in ("crypto_prices_twap_thirty", "crypto_prices_twap_sixty"):
        marks = [r for r in kept if r["topic"] == topic]
        # Within MARK_TOL_S of the minute, and nothing else: a print at :31
        # can never bank as a mark, so it is weight with no effect.
        assert {r["ts"] // 1000 % 60 for r in marks} == {0, 1, 2}
    # BOTH widths survive. A 5m arm reads the 30s and a 15m arm the 60s, and
    # keeping both means a settle_tw_secs re-spec cannot orphan the slice.
    assert any(r["topic"] == "crypto_prices_twap_sixty" for r in kept)


def test_rtds_slice_drops_the_recorder_field_replay_never_reads():
    start, end = 1_787_442_300, 1_787_442_600
    kept, _ = rtds_slice(_rtds_rows(start - 7200, end + 120), "xrp/usd", start, end)
    assert all("window_s" not in r for r in kept), "the topic already names the width"
    assert all(set(r) <= set(("t_recv", "topic", "symbol", "ts", "value",
                              "full_accuracy_value")) for r in kept)


def test_rtds_slice_takes_one_symbol_and_reports_an_absent_one():
    start, end = 1_787_442_300, 1_787_442_600
    rows = _rtds_rows(start - 7200, end + 120) + _rtds_rows(start, end, symbol="doge/usd")
    kept, _ = rtds_slice(rows, "xrp/usd", start, end)
    assert {r["symbol"] for r in kept} == {"xrp/usd"}
    # A symbol the corpus never carried reports no coverage at all, which is
    # what the freezer refuses on.
    assert rtds_slice(rows, "sol/usd", start, end) == ([], None)


def test_rtds_slice_coverage_reports_a_corpus_that_misses_the_window():
    start, end = 1_787_442_300, 1_787_442_600
    # Recorder died 100s before the close.
    kept, coverage = rtds_slice(_rtds_rows(start - 600, end - 100), "xrp/usd", start, end)
    assert kept, "what it does have is still returned"
    assert coverage[1] < end, "and the caller can see the hole"


def test_build_params_synthesizes_tokens_and_kills_the_roll_chain():
    p, prov = build_params(SLUG, _tape(), LIVE_ARM)
    assert p["token_up"] == f"{SLUG}-up" and p["token_down"] == f"{SLUG}-down"
    assert LIVE_ARM["token_up"] not in json.dumps(p)
    assert p["roll"] is False
    assert p["slug"] == SLUG and p["symbol"] == "BTCUSDT"
    assert p["start"] == 1787449500.0 and p["end"] == 1787450400.0


def test_build_params_records_overrides_and_lifted_tunables():
    p, prov = build_params(SLUG, _tape(), LIVE_ARM,
                           overrides={"basis_guard_bp": 3, "late_rem_s": 360},
                           lifted_tunables=True)
    assert p["basis_guard_bp"] == 3 and p["late_rem_s"] == 360
    assert prov["basis_guard_bp"] == "operator override"
    assert p["tunables"] == {"distrust_net": 1e9, "avg_down_tol": 1e9}
    assert "brakes lifted" in prov["tunables"]


def test_build_params_rejects_an_unusable_slug():
    with pytest.raises(FixtureError):
        build_params("not-a-slug", _tape(), LIVE_ARM)
    with pytest.raises(FixtureError, match="no Binance symbol"):
        build_params("zzz-updown-5m-1787449500", _tape(), LIVE_ARM)


# ---------- redaction ----------

def test_secret_scan_catches_addresses_hashes_and_token_ids():
    addr = "0x" + "ab" * 20
    txhash = "0x" + "cd" * 32
    token = "3" * 77
    hits = secret_scan(f"funder {addr} tx {txhash} token {token}")
    assert addr in hits and txhash in hits and token in hits


def test_secret_scan_is_clean_on_a_real_tape_slice():
    text = json.dumps(_tape() + [trim_book_record(
        {"t": 1.0, "ev": "book", "slug": SLUG, "spot": 100.0, "spot_age_s": 0.5,
         "up_ask": 0.5, "up_ask_sz": 20.0})])
    assert secret_scan(text) == []


def test_secret_scan_takes_caller_supplied_needles():
    hits = secret_scan("nothing shaped like a secret, but PLAINSECRETVALUE is here",
                       needles=["PLAINSECRETVALUE", "", "short"])
    assert hits == ["PLAINSECRE..."]


# ---------- assembly / rendering ----------

def _fixture(expect=None):
    params, prov = build_params(SLUG, _tape(guard_bp=3.0), LIVE_ARM)
    acct = wallet_accounting(_activity(), SLUG)
    outcome = build_outcome({"slug": SLUG, "winner": "down", "source": "wallet"}, acct, SLUG)
    lo = (1787449500 - 2700) // 60 * 60
    klines = [{"t": t, "o": 1.0, "c": 2.0} for t in range(lo, 1787450400, 60)]
    return build_fixture(
        SLUG, "evals", params, prov, outcome, _tape(guard_bp=3.0), [], klines,
        "the -$370 anatomy", "docs/LESSONS.md#L8", ["pre-brake"],
        ["all_fires_side:up"], {"frozen_at": "2026-08-23T00:00:00Z"}, expect,
    )


def test_build_fixture_shape_matches_the_rust_reader():
    fx = _fixture()
    assert fx["fixture_version"] == FIXTURE_VERSION
    assert fx["window_utc"] == ["2026-08-23T01:45:00Z", "2026-08-23T02:00:00Z"]
    assert fx["expect"] is None  # unblessed until pmengine writes it
    assert fx["mode"] == "evals"
    # Additive: a Binance fixture carries an empty rtds slice and every
    # fixture frozen before the stream existed still loads.
    assert fx["rtds"] == []
    assert list(fx) == [k for k in FIXTURE_KEYS], "serde field order"


def test_render_fixture_round_trips_and_keeps_records_one_per_line():
    fx = _fixture()
    text = render_fixture(fx)
    assert json.loads(text) == fx
    assert '"evals": [\n' in text
    # One record per line is what makes a blessed diff readable.
    body = text.split('"evals": [\n', 1)[1].split("\n ]", 1)[0]
    assert len(body.splitlines()) == len(fx["evals"])


def test_render_fixture_leads_with_the_serde_field_order():
    text = render_fixture(_fixture())
    order = [line.split('"')[1] for line in text.splitlines()
             if line.startswith(' "')]
    assert order[:4] == ["fixture_version", "slug", "mode", "teaches"]


def test_sha256_records_is_order_sensitive_and_key_order_stable():
    a = [{"t": 1, "ev": "eval"}, {"t": 2, "ev": "fire"}]
    assert sha256_records(a) == sha256_records([{"ev": "eval", "t": 1}, {"ev": "fire", "t": 2}])
    assert sha256_records(a) != sha256_records(list(reversed(a)))
