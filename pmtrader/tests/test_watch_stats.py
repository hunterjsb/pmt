"""Pure-math tests for the gamewatch coupling stats."""

from polymarket.gamewatch import corr_and_lag, match_latencies, pearson, resample


def test_pearson_perfect():
    assert abs(pearson([1, 2, 3, 4], [2, 4, 6, 8]) - 1.0) < 1e-9
    assert abs(pearson([1, 2, 3, 4], [8, 6, 4, 2]) + 1.0) < 1e-9


def test_resample_ffill():
    s = [(0.0, 1.0), (2.5, 2.0)]
    assert resample(s, 0, 4, 1.0) == [1.0, 1.0, 1.0, 2.0, 2.0]
    # None before the first observation
    assert resample(s, -2, 0, 1.0) == [None, None, 1.0]


def test_corr_and_lag_detects_shift():
    # market = espn shifted 6s later; both are noisy-free random walks
    import random

    rng = random.Random(7)
    espn, walk = [], 0.5
    for t in range(0, 300):
        walk += rng.uniform(-0.01, 0.01)
        espn.append((float(t), walk))
    mkt = [(t + 6.0, v) for t, v in espn]
    out = corr_and_lag(espn, mkt)
    assert out["best_lag"] is not None
    assert 4 <= out["best_lag"] <= 8
    assert out["best_r"] > 0.8


def test_match_latencies_pairs_and_leads():
    events = [
        {"mono": 100.0, "d": +0.05},   # market reacts 6s later
        {"mono": 200.0, "d": -0.04},   # market led by 3s
        {"mono": 300.0, "d": +0.03},   # no reaction
    ]
    moves = [
        {"mono": 106.0, "d": +0.02},
        {"mono": 197.0, "d": -0.02},
        {"mono": 310.0, "d": -0.02},   # wrong direction for event 3
    ]
    out = match_latencies(events, moves, max_wait=60, lead=20)
    assert out["n"] == 2
    assert out["led"] == 1
    assert out["unmatched"] == 1
    assert out["min"] == -3.0 and out["max"] == 6.0
