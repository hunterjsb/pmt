"""The EV gate's arithmetic, including the fee, and the blend weight's rule.

The gate is `calfit/ev_policy.py`'s cost model verbatim:

    edge = p_side - ask - taker_fee(ask),   fire iff edge >= MIN_EDGE

Paying the spread is inside it: `ask` is the QUOTED ASK, which is what a taker
pays, so there is no separate spread term to forget. The fee is the live
crypto_fees_v2 schedule and is charged on both sides of the pair.
"""

from __future__ import annotations

import math

import pytest

from pilot2 import policy
from polymarket.constants import taker_fee


def test_min_edge_is_the_reports_value():
    assert policy.MIN_EDGE == 0.02


def test_fee_is_the_live_schedule_not_a_local_copy():
    """0.07 * min(p, 1-p) — imported from polymarket.constants so the pilot
    and pmengine cannot charge different fees for the same fill."""
    assert taker_fee(0.70) == 0.07 * min(0.70, 1.0 - 0.70)
    assert taker_fee(0.30) == 0.07 * 0.30
    assert taker_fee(0.50) == 0.07 * 0.50
    assert taker_fee(0.95) == pytest.approx(taker_fee(0.05)), \
        "the cheaper side of the pair, either way"


def test_edge_is_probability_minus_ask_minus_fee():
    d = policy.side_decision("up", 0.80, 0.70, 100.0)
    assert d.fee == taker_fee(0.70)
    assert d.edge == 0.80 - 0.70 - taker_fee(0.70)


def test_gate_fires_exactly_at_min_edge_and_not_below():
    ask = 0.70
    fee = taker_fee(ask)
    at = policy.side_decision("up", ask + fee + policy.MIN_EDGE, ask, 100.0)
    assert at.fire, "the gate is >=, matching `if edge < min_edge: continue`"
    below = policy.side_decision("up", ask + fee + policy.MIN_EDGE - 1e-9, ask, 100.0)
    assert not below.fire


def test_the_fee_alone_can_close_the_gate():
    """A raw 2c edge that ignores the fee fires; the real cost model does not.
    At ask 0.50 the fee is 3.5c, which is bigger than min_edge on its own."""
    ask = 0.50
    p = ask + 0.02
    naive_edge = p - ask
    assert naive_edge >= policy.MIN_EDGE
    d = policy.side_decision("up", p, ask, 100.0)
    assert d.edge < 0, "the fee at 0.50 is 3.5c and swamps a 2c gross edge"
    assert not d.fire


def test_no_price_level_gate_min_fair_does_not_exist_here():
    """Thesis B: the edge is probability quality, not direction. The blend was
    profitable in EVERY price bucket and best BELOW 0.6 — the region min_fair
    forbids. A cheap ask with a big edge must fire."""
    cheap = policy.side_decision("down", 0.45, 0.20, 50.0)
    assert cheap.fire and cheap.ask < 0.5
    rich = policy.side_decision("up", 0.99, 0.95, 50.0)
    assert rich.fire and rich.ask > 0.9


def test_unpriceable_asks_are_refused_not_guessed():
    assert policy.side_decision("up", 0.9, 0.0, 10.0) is None
    assert policy.side_decision("up", 0.9, 1.0, 10.0) is None
    assert policy.side_decision("up", 0.9, float("nan"), 10.0) is None
    assert policy.side_decision("up", float("nan"), 0.7, 10.0) is None


def test_evaluate_prices_both_sides_from_one_blend():
    book_p, blend_p, ds = policy.evaluate(0.90, 0.60, 0.42, 500.0, 500.0, 0.55)
    assert book_p == 0.60 / (0.60 + 0.42)
    assert blend_p == 0.55 * 0.90 + 0.45 * book_p
    assert [d.side for d in ds] == ["up", "down"]
    assert ds[0].p_side == blend_p
    assert ds[1].p_side == 1.0 - blend_p


def test_evaluate_with_a_one_sided_book_leaves_the_model_standing_alone():
    """60% of rows in the corpus have no two-sided quote. On those the model is
    not competing with a market — it is the only estimator in the room."""
    book_p, blend_p, ds = policy.evaluate(0.90, 0.60, float("nan"), 500.0, 0.0, 0.55)
    assert math.isnan(book_p)
    assert blend_p == 0.90, "no book -> the model stands alone, un-shrunk"
    assert [d.side for d in ds] == ["up"], "the unquoted side is not tradeable"


# --- the blend weight ------------------------------------------------------

def test_weight_seeds_at_the_last_walk_forward_fold():
    """0.00 -> 0.20 -> 0.40 -> 0.55 across four folds. The pilot starts where
    the evidence got to and refits; it never freezes."""
    assert policy.W_SEED == 0.55
    assert policy.MIN_FIT_ROWS == 400
    assert policy.W_GRID == tuple(i / 20.0 for i in range(21)), "np.linspace(0, 1, 21)"


def test_weight_stays_at_the_seed_below_the_fit_floor():
    rows = [(0.9, 0.5, 1)] * (policy.MIN_FIT_ROWS - 1)
    w, source, n = policy.fit_blend_weight(rows)
    assert (w, source, n) == (policy.W_SEED, policy.W_SOURCE_SEED, policy.MIN_FIT_ROWS - 1)


def test_weight_fit_is_the_brier_grid_search():
    """A history where the model is always right and the book always wrong
    must fit w = 1.0; the reverse must fit w = 0.0."""
    model_right = [(1.0, 0.0, 1)] * policy.MIN_FIT_ROWS
    w, source, n = policy.fit_blend_weight(model_right)
    assert (w, source, n) == (1.0, policy.W_SOURCE_FIT, policy.MIN_FIT_ROWS)

    book_right = [(0.0, 1.0, 1)] * policy.MIN_FIT_ROWS
    assert policy.fit_blend_weight(book_right)[0] == 0.0


def test_weight_fit_drops_rows_missing_either_estimator():
    """A one-sided book yields nan, and nan is never repaired into a number."""
    rows = [(1.0, 0.0, 1)] * policy.MIN_FIT_ROWS + [(0.5, float("nan"), 1)] * 50
    w, _source, n = policy.fit_blend_weight(rows)
    assert n == policy.MIN_FIT_ROWS and w == 1.0


def test_realized_pnl_is_the_replays_accounting():
    """A winner pays $1/share; the loss leg is exactly -100% of notional.
    Fees are charged either way."""
    shares, ask = 10.0, 0.70
    fee = taker_fee(ask)
    assert policy.realized_pnl(shares, ask, True) == shares * (1.0 - ask - fee)
    assert policy.realized_pnl(shares, ask, False) == shares * (0.0 - ask - fee)
    assert policy.realized_pnl(shares, ask, False) < -shares * ask, "fees on top of the wipeout"
