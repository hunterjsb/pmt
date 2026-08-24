"""The EV gate and the blend weight — `calibrated_model.md`'s policy, ported.

The gate is thesis B: the edge is probability QUALITY, not direction, so there
is no `min_fair` and no price-level gate of any kind. Take whichever side
clears costs, at any price, both sides. The blend was profitable in EVERY price
bucket of the replay and made its best returns BELOW 0.6 — precisely the region
`min_fair` forbids and precisely where the incumbent, when it went there, lost
7-57c on the dollar.

Cost model, verbatim from `calfit/ev_policy.py`:

    edge = p_side - ask - taker_fee(ask)          fire iff edge >= MIN_EDGE

`ask` is the quoted ask, so paying the spread is already inside the arithmetic
— we are the taker and never assume a mid fill. `taker_fee` is the live
`crypto_fees_v2` schedule, `0.07 * p * (1-p)` as MEASURED off the wallet (see
`polymarket.constants`), the same function pmengine charges; it is imported
from `polymarket.constants` rather than re-typed, so the two can never drift.
The gate is at its most sensitive to this at mid price, which is exactly where
thesis B says the edge lives — the old `min(p, 1-p)` shape charged double
there and priced the lane out.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from polymarket.constants import taker_fee

from . import predict

# Net cents of edge required to fire. The report's replay ran at 0.02 and its
# sensitivity table is monotone in trade count and POSITIVE at every setting
# tested (+$1,564 / +$1,459 / +$931 / +$556 / +$137 at 0.01 / 0.02 / 0.03 /
# 0.05 / 0.08) — no knife edge, so this is a choice about throughput, not about
# whether the edge exists.
MIN_EDGE = 0.02

# --- the blend weight ------------------------------------------------------

# The walk-forward weight refit on EARLIER folds only climbed 0.00 -> 0.20 ->
# 0.40 -> 0.55 across the corpus, converging on the full-sample optimum (0.5)
# from below as evidence accumulated. The report is explicit that the ENDPOINT
# is not earned — "that trajectory is the finding" — so the pilot seeds at the
# last fold's value and refits from its OWN graded history, never freezing.
W_SEED = 0.55

# The report's own guard: `wf_blend` skips a fold with fewer than 400 training
# rows. Below this the fit is noise and the seed stands.
MIN_FIT_ROWS = 400

# The grid `wf_blend` searches: np.linspace(0, 1, 21).
W_GRID: tuple[float, ...] = tuple(i / 20.0 for i in range(21))

W_SOURCE_SEED = "seed"
W_SOURCE_FIT = "fit"


def fit_blend_weight(rows: list[tuple[float, float, int]]) -> tuple[float, str, int]:
    """(w, source, n) minimising Brier over W_GRID — `wf_blend`'s search, exactly.

    `rows` are (model_p_up, book_p_up, y) from RESOLVED windows only, which is
    what makes this walk-forward by construction: a row cannot exist until its
    window has settled, so no fit ever sees a row it is later scored on.

    Rows missing either estimator are dropped, never repaired. `devig` returns
    nan on a one-sided book on purpose and substituting `1 - other_side` there
    would manufacture a market opinion that does not exist.
    """
    usable = [(m, b, y) for m, b, y in rows
              if math.isfinite(m) and math.isfinite(b) and y in (0, 1)]
    if len(usable) < MIN_FIT_ROWS:
        return W_SEED, W_SOURCE_SEED, len(usable)
    best_w, best_brier = W_SEED, float("inf")
    for w in W_GRID:
        brier = math.fsum((w * m + (1.0 - w) * b - y) ** 2 for m, b, y in usable) / len(usable)
        if brier < best_brier:
            best_w, best_brier = w, brier
    return best_w, W_SOURCE_FIT, len(usable)


# --- the EV decision -------------------------------------------------------

@dataclass(frozen=True)
class Decision:
    """One side of one window, priced. `fire` is the EV verdict ALONE — risk
    law is a separate, later refusal, so the tape can distinguish 'no edge'
    from 'edge we were not allowed to take'."""

    side: str            # "up" | "down"
    p_side: float        # blended P(this side wins)
    ask: float
    ask_size: float
    fee: float
    edge: float
    fire: bool


def side_decision(side: str, p_side: float, ask: float, ask_size: float,
                  min_edge: float = MIN_EDGE) -> Decision | None:
    """EV for one side, or None when the side is not priceable.

    An ask of exactly 0 or 1 is not a price — it is a book with nothing on it
    or a resolved market — and `ev_policy.replay` skips both.
    """
    if not math.isfinite(p_side) or not math.isfinite(ask) or not (0.0 < ask < 1.0):
        return None
    fee = taker_fee(ask)
    edge = p_side - ask - fee
    return Decision(side=side, p_side=p_side, ask=ask, ask_size=ask_size,
                    fee=fee, edge=edge, fire=edge >= min_edge)


def evaluate(model_p_up: float, book_up_ask: float, book_dn_ask: float,
             book_up_ask_sz: float, book_dn_ask_sz: float, w: float,
             min_edge: float = MIN_EDGE) -> tuple[float, float, list[Decision]]:
    """(book_p_up, blend_p_up, [up decision, down decision]).

    The de-vig uses the two ASKS as the market's opinion. That is the same
    quantity the fill is taken at, so the estimator and the execution price are
    never read off different sides of the book.
    """
    book_p_up = predict.devig(book_up_ask, book_dn_ask)
    blend_p_up = predict.blended_p_up(model_p_up, book_up_ask, book_dn_ask, w)
    decisions = []
    for side, p_side, ask, sz in (
            ("up", blend_p_up, book_up_ask, book_up_ask_sz),
            ("down", 1.0 - blend_p_up, book_dn_ask, book_dn_ask_sz)):
        d = side_decision(side, p_side, ask, sz, min_edge)
        if d is not None:
            decisions.append(d)
    return book_p_up, blend_p_up, decisions


def realized_pnl(shares: float, ask: float, won: bool) -> float:
    """`ev_policy.replay`'s accounting, unchanged: a winner pays $1/share and
    the loss leg is exactly -100% of notional. Fees are charged either way."""
    return shares * ((1.0 if won else 0.0) - ask - taker_fee(ask))
