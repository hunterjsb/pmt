"""Pure tests for the Chainlink roundId phase math and the Chainlink-vs-Binance basis join.

No network — RPC calls are monkeypatched with hand-encoded ABI responses.
"""

import pytest

from polymarket import chainlink
from polymarket.chainlink import (
    aligned_stats,
    basis_stats,
    join_basis,
    join_round_id,
    per_minute_basis,
    settlement_basis,
    signed_bias,
    split_round_id,
    twap_over_window,
)


def _encode_round(round_id: int, answer: int, started_at: int, updated_at: int,
                   answered_in_round: int) -> str:
    """Hand-encode a latestRoundData/getRoundData ABI response (5 x 32-byte words)."""
    words = [round_id, answer % (1 << 256), started_at, updated_at, answered_in_round]
    return "0x" + b"".join(w.to_bytes(32, "big") for w in words).hex()


# ---------- roundId phase math ----------

def test_split_round_id_matches_observed_btc_round():
    # a real BTC/USD Polygon roundId observed live: phase 3, aggregator round 0x39d757
    round_id = 0x3000000000039d757
    assert split_round_id(round_id) == (3, 0x39d757)


def test_join_round_id_roundtrip():
    for phase, agg in [(1, 0), (1, 1), (3, 0x39d757), (0xFFFF, (1 << 64) - 1)]:
        rid = join_round_id(phase, agg)
        assert split_round_id(rid) == (phase, agg)


def test_split_round_id_masks_only_low_64_bits():
    # phase 2 must not leak into the aggregator half or vice versa
    rid = join_round_id(2, 5)
    phase, agg = split_round_id(rid)
    assert phase == 2 and agg == 5
    assert rid == (2 << 64) | 5


# ---------- fetch_rounds: walk + phase-boundary stop (synthetic RPC) ----------

def _rig_synthetic_feed(monkeypatch, phase: int, latest_agg: int):
    """Fake a feed with real rounds at aggregatorRoundId 1..latest_agg (agg 0 is the
    zero-sentinel that marks the phase boundary, matching what Polygon actually returns).
    """
    by_agg = {
        agg: {"round_id": join_round_id(phase, agg), "answer": 7_700_000_000_000 + agg,
              "started_at": 1_000 + agg * 100, "updated_at": 1_000 + agg * 100,
              "answered_in_round": join_round_id(phase, agg)}
        for agg in range(1, latest_agg + 1)
    }

    def fake_eth_call(to, data):
        assert data == chainlink.SEL_LATEST_ROUND_DATA
        return _encode_round(**by_agg[latest_agg])

    def fake_eth_call_batch(calls):
        out = []
        for _to, data in calls:
            round_id = int(data[len(chainlink.SEL_GET_ROUND_DATA):], 16)
            _, agg = split_round_id(round_id)
            if agg in by_agg:
                out.append(_encode_round(**by_agg[agg]))
            else:
                # phase-start sentinel: everything zero, including updated_at
                out.append(_encode_round(round_id=round_id, answer=0, started_at=0,
                                          updated_at=0, answered_in_round=0))
        return out

    monkeypatch.setattr(chainlink, "_eth_call", fake_eth_call)
    monkeypatch.setattr(chainlink, "_eth_call_batch", fake_eth_call_batch)


def test_fetch_rounds_stops_at_phase_boundary(monkeypatch):
    _rig_synthetic_feed(monkeypatch, phase=1, latest_agg=5)
    rounds = chainlink.fetch_rounds("btc", hours=1e9)  # cutoff far in the past — never trips
    assert [r["round_id"] for r in rounds] == [join_round_id(1, agg) for agg in range(1, 6)]
    assert rounds[0]["updated_at"] == 1100  # agg 1 — oldest real round, agg 0 sentinel excluded
    assert rounds[-1]["updated_at"] == 1500
    # decimals-adjusted (FEEDS["btc"]["decimals"] == 8)
    assert rounds[-1]["price"] == pytest.approx(77_000.00005)


def test_fetch_rounds_respects_hours_cutoff(monkeypatch):
    _rig_synthetic_feed(monkeypatch, phase=1, latest_agg=5)
    # cutoff excludes updated_at < now - hours; pin "now" so only agg 3,4,5 (>=1300) survive
    monkeypatch.setattr(chainlink.time, "time", lambda: 1_600.0)
    rounds = chainlink.fetch_rounds("btc", hours=300 / 3600)  # cutoff = 1300
    assert [r["updated_at"] for r in rounds] == [1300, 1400, 1500]


# ---------- basis join + stats ----------

def test_join_basis_computes_bp_and_drops_unmatched_or_zero():
    rounds = [
        {"round_id": 1, "price": 100.0, "updated_at": 10},     # minute-open 0ms -> has a close
        {"round_id": 2, "price": 200.0, "updated_at": 70},     # minute-open 60000ms -> no close on file
        {"round_id": 3, "price": 300.0, "updated_at": 130},    # minute-open 120000ms -> close present but 0
    ]
    minute_closes = {0: 99.0, 120_000: 0.0}  # 60_000 deliberately absent
    rows = join_basis(rounds, minute_closes)
    assert len(rows) == 1
    assert rows[0]["round_id"] == 1
    assert rows[0]["basis_bp"] == pytest.approx((100.0 / 99.0 - 1) * 1e4)


def test_basis_stats_percentiles_and_shape():
    rows = [{"basis_bp": v} for v in [1.0, 2.0, 3.0, 4.0, 5.0]]
    s = basis_stats(rows)
    assert s["n"] == 5
    assert s["mean"] == pytest.approx(3.0)
    assert s["std"] == pytest.approx(1.5811388300841898)
    assert s["p50"] == pytest.approx(3.0)
    assert s["p5"] == pytest.approx(1.2)
    assert s["p95"] == pytest.approx(4.8)
    assert s["max_abs"] == pytest.approx(5.0)
    assert s["p95_abs"] == pytest.approx(4.8)


def test_basis_stats_empty_is_none():
    assert basis_stats([]) is None


def test_basis_stats_handles_negative_basis_via_abs():
    rows = [{"basis_bp": v} for v in [-10.0, -1.0, 1.0, 2.0, 10.0]]
    s = basis_stats(rows)
    assert s["mean"] == pytest.approx(0.4)
    # max_abs looks at magnitude regardless of sign
    assert s["max_abs"] == pytest.approx(10.0)


# ---------- aligned basis: TWAP-vs-TWAP (R1) ----------

def _rounds(prices_by_t: dict[int, float]) -> list[dict]:
    return [{"round_id": i, "price": p, "updated_at": t}
            for i, (t, p) in enumerate(sorted(prices_by_t.items()))]


def test_twap_over_window_holds_step_value():
    rounds = _rounds({0: 100.0, 30: 200.0})
    ts = [r["updated_at"] for r in rounds]
    # half the window at 100, half at 200 -> average 150
    assert twap_over_window(rounds, ts, 0, 60) == pytest.approx(150.0)


def test_twap_over_window_none_without_carry_in():
    rounds = _rounds({100: 100.0})
    ts = [r["updated_at"] for r in rounds]
    assert twap_over_window(rounds, ts, 0, 60) is None  # no round at/before start=0


def test_twap_over_window_none_when_stale_entering():
    rounds = _rounds({0: 100.0})
    ts = [r["updated_at"] for r in rounds]
    # start=1000 is >ALIGNED_STALE_S past the only round
    assert twap_over_window(rounds, ts, 1000, 1060) is None


def test_per_minute_basis_matches_marks():
    rounds = _rounds({0: 100.0, 60: 102.0})
    marks = {60: 101.0}  # minute [60,120) needs a round before 60 -> carries 102.0
    out = per_minute_basis(rounds, marks)
    assert len(out) == 1
    assert out[0]["t"] == 60
    assert out[0]["basis_bp"] == pytest.approx((102.0 / 101.0 - 1) * 1e4)


def test_settlement_basis_uses_ck_window_and_prior_minute_mark():
    # 305 just past the 300s boundary so last_end covers it; doesn't affect the [270,300) TWAP itself
    rounds = _rounds({0: 100.0, 270: 110.0, 305: 115.0})
    marks = {240: 105.0}  # mark(end - 60) for end=300
    out = settlement_basis(rounds, marks, period_s=300, ck_window_s=30)
    assert len(out) == 1
    assert out[0]["t"] == 300
    assert out[0]["basis_bp"] == pytest.approx((110.0 / 105.0 - 1) * 1e4)


def test_aligned_stats_shape_and_percentiles():
    rows = [{"basis_bp": v} for v in [-5.0, 1.0, 2.0, 3.0, 4.0]]
    s = aligned_stats(rows)
    assert s["n"] == 5
    assert s["p50"] == pytest.approx(3.0)  # median of sorted abs values [1,2,3,4,5]
    assert s["max"] == pytest.approx(5.0)


def test_aligned_stats_empty_is_none():
    assert aligned_stats([]) is None


def test_signed_bias_keeps_sign_unlike_aligned_stats():
    rows = [{"basis_bp": v} for v in [-10.0, -10.0]]
    assert signed_bias(rows) == pytest.approx(-10.0)
    assert signed_bias([]) == 0.0
