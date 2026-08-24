"""Reading the settlement stream back for a symbol Binance does not list.

The rules under test are mirrors of `updown_rtds.rs::route_sample`, so these
are as much a pin on that contract as on this module: if the closes vector or
the mark keying drifts, the pre-flight starts handing the engine a vol floor
measured on a series the engine never sees.
"""

from __future__ import annotations

import json

import pytest

from polymarket import rtds, rtds_read as rr

SYM = "hype/usd"
T0 = 1787538000  # a 5m window boundary


def _spot(ts: int, px: float, symbol: str = SYM) -> dict:
    return {"t_recv": ts + 0.2, "topic": rtds.TOPIC_SPOT, "symbol": symbol,
            "ts": ts * 1000, "value": px,
            "full_accuracy_value": str(int(px * 10 ** 18)), "window_s": None}


def _twap(ts: int, px: float, window_s: int = 60, symbol: str = SYM) -> dict:
    topic = rtds.TOPIC_TWAP60 if window_s == 60 else rtds.TOPIC_TWAP30
    return {"t_recv": ts + 0.2, "topic": topic, "symbol": symbol,
            "ts": ts * 1000, "value": px,
            "full_accuracy_value": str(int(px * 10 ** 18)), "window_s": window_s}


def _corpus(tmp_path, records, day="20260824"):
    d = tmp_path / "rtds"
    d.mkdir(exist_ok=True)
    with open(d / f"rtds-{day}.jsonl", "a") as fh:
        for r in records:
            fh.write(json.dumps(r) + "\n")
    return d


# ---------- the reverse tail reader ----------

def test_reverse_lines_yields_newest_first_and_loses_nothing(tmp_path):
    path = tmp_path / "x.jsonl"
    path.write_text("".join(f"line{i}\n" for i in range(5000)))
    got = list(rr.reverse_lines(path))
    assert got[0] == b"line4999"
    assert got[-1] == b"line0"
    assert len(got) == 5000


def test_reverse_lines_drops_a_line_the_budget_cut_in_half(tmp_path):
    # A truncated JSON object is not a record; better absent than half-parsed.
    path = tmp_path / "x.jsonl"  # >1 chunk, so the budget really does stop early
    path.write_text("".join(f"{'p' * 100}{i}\n" for i in range(20000)))
    got = list(rr.reverse_lines(path, budget_bytes=1))
    assert got and all(line.startswith(b"p") for line in got)
    assert b"p" * 100 + b"0" not in got, "the oldest line was never read"


def test_read_back_stops_at_the_horizon_and_ignores_other_symbols(tmp_path):
    rows = []
    for i in range(600):
        rows.append(_spot(T0 - 600 + i, 80.0 + i * 0.001))
        rows.append(_spot(T0 - 600 + i, 3.0, symbol="xrp/usd"))
    d = _corpus(tmp_path, rows)

    got = rr.read_back(SYM, T0 - 60, directory=d)
    assert {r["symbol"] for r in got} == {SYM}
    assert len(got) == 60
    assert got[0]["ts"] // 1000 == T0 - 60, "oldest first"
    assert got[-1]["ts"] // 1000 == T0 - 1


def test_read_back_crosses_a_daily_rotation(tmp_path):
    # A 120-minute sigma lookback taken just after UTC midnight lives in two
    # files; the recorder rotates on UTC day and this must not notice.
    d = _corpus(tmp_path, [_spot(T0 - 300, 79.0)], day="20260823")
    _corpus(tmp_path, [_spot(T0, 80.0)], day="20260824")
    got = rr.read_back(SYM, T0 - 600, directory=d)
    assert [r["ts"] // 1000 for r in got] == [T0 - 300, T0]


# ---------- price precedence ----------

def test_price_prefers_the_exact_e18_string_over_the_display_float():
    rec = {"full_accuracy_value": "80600398263285972992", "value": 80.6}
    assert rr.price_of(rec) == pytest.approx(80.600398263285972992, abs=1e-12)
    assert rr.price_of(rec) != 80.6


def test_price_falls_back_to_the_display_float_and_rejects_junk():
    assert rr.price_of({"full_accuracy_value": None, "value": 80.6}) == 80.6
    assert rr.price_of({"full_accuracy_value": "nope", "value": None}) is None
    assert rr.price_of({"full_accuracy_value": "0", "value": 0}) is None
    assert rr.price_of({"full_accuracy_value": None, "value": True}) is None


# ---------- spot ----------

def test_corpus_spot_takes_the_freshest_print(tmp_path):
    d = _corpus(tmp_path, [_spot(T0 - 3, 80.1), _spot(T0 - 2, 80.2), _spot(T0 - 1, 80.3),
                           _twap(T0, 79.0)])
    px, ts = rr.corpus_spot(SYM, now=T0, directory=d)
    assert px == pytest.approx(80.3)
    assert ts == T0 - 1


def test_corpus_spot_is_none_when_the_recorder_went_quiet(tmp_path):
    d = _corpus(tmp_path, [_spot(T0 - 4000, 80.0)])
    assert rr.corpus_spot(SYM, now=T0, directory=d) is None


def test_corpus_spot_is_none_for_a_symbol_the_stream_never_carried(tmp_path):
    d = _corpus(tmp_path, [_spot(T0 - 1, 80.0)])
    assert rr.corpus_spot("pepe/usd", now=T0, directory=d) is None


# ---------- closes / sigma (updown_rtds.rs::route_sample parity) ----------

def test_minute_closes_bank_the_first_print_of_each_new_minute(tmp_path):
    # The router pushes the FIRST sample of a new minute onto `closes`. Taking
    # the last one instead would silently shift the whole vol series.
    rows = [_spot(T0 - 180, 80.0), _spot(T0 - 170, 81.0), _spot(T0 - 121, 82.0),
            _spot(T0 - 120, 83.0), _spot(T0 - 61, 84.0), _spot(T0 - 60, 85.0),
            _spot(T0 - 30, 86.0)]
    d = _corpus(tmp_path, rows)
    got = rr.minute_closes(SYM, T0 - 600, directory=d)
    assert got == [(T0 - 180, 80.0), (T0 - 120, 83.0), (T0 - 60, 85.0)]


def test_corpus_sigma_matches_the_binance_estimator_on_the_same_closes(tmp_path):
    from polymarket.fit import realized_sigma

    px = [80.0 + (i % 7) * 0.05 for i in range(30)]
    d = _corpus(tmp_path, [_spot(T0 - 60 * (30 - i), p) for i, p in enumerate(px)])
    sig, n = rr.corpus_sigma(SYM, now=T0, directory=d)
    assert n == 30
    assert sig == pytest.approx(realized_sigma(px, 29))


def test_corpus_sigma_is_none_when_history_is_too_cold_to_estimate(tmp_path):
    # A brand-new stream symbol has no klines corpus either — the caller has
    # to be told to pass --sigma-bp, not handed a 0 that reads as "no vol".
    d = _corpus(tmp_path, [_spot(T0 - 120, 80.0), _spot(T0 - 60, 80.1)])
    assert rr.corpus_sigma(SYM, now=T0, directory=d) is None


# ---------- twap marks ----------

def test_twap_marks_key_the_minute_the_print_averages(tmp_path):
    # The print AT m+60 averages [m, m+60), so it keys as minute m — which is
    # what makes per_min[start-60] the settlement print at the window's start.
    d = _corpus(tmp_path, [_twap(T0 - 60, 79.5), _twap(T0, 80.5)])
    marks = rr.twap_marks(SYM, T0 - 600, directory=d)
    assert marks[float(T0 - 120)] == pytest.approx(79.5)
    assert marks[float(T0 - 60)] == pytest.approx(80.5)


def test_twap_marks_ignore_prints_that_missed_the_minute_boundary(tmp_path):
    # Only prints within MARK_TOL_S can ever become a mark; the other ~57 a
    # minute are read by nobody, here or in the engine.
    d = _corpus(tmp_path, [_twap(T0 + rr.MARK_TOL_S, 80.0), _twap(T0 + 30, 99.0)])
    marks = rr.twap_marks(SYM, T0 - 600, directory=d)
    assert marks == {float(T0 - 60): pytest.approx(80.0)}


def test_twap_marks_select_the_settlement_width(tmp_path):
    d = _corpus(tmp_path, [_twap(T0, 80.0, window_s=60), _twap(T0, 70.0, window_s=30)])
    assert rr.twap_marks(SYM, T0 - 600, window_s=60, directory=d)[float(T0 - 60)] == pytest.approx(80.0)
    assert rr.twap_marks(SYM, T0 - 600, window_s=30, directory=d)[float(T0 - 60)] == pytest.approx(70.0)


# ---------- live socket fallback ----------

def test_live_spot_returns_none_rather_than_raising_when_the_socket_is_gone(monkeypatch):
    # The caller's error names the whole fallback chain; a websockets
    # traceback out of here would bury it.
    def boom(*a, **kw):
        raise OSError("no route to host")

    monkeypatch.setattr("websockets.sync.client.connect", boom)
    assert rr.live_spot(SYM, timeout_s=0.1) is None
