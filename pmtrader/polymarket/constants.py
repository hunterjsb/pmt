"""Trading constants shared across the crypto up/down stack.

These numbers were each defined in two or three places (crypto.py's
BASIS_NOISE_BP vs the `crypto arm --basis-guard` default, shadow.py's
FEE_RATE vs crypto.py's fee handling) and drifted independently — a
consolidation with nothing enforcing it. One definition each, here.

Deliberately dependency-free (no requests, no network modules) so the pure
modules — shadow.py's ledger math above all — can import it without
dragging I/O into their import graph.
"""

from __future__ import annotations

# Polymarket crypto_fees_v2 taker rate. The live per-market
# `feeSchedule.rate` is authoritative whenever a market has been fetched
# (crypto.parse_semantics reads it); this is the fallback for the pure
# paths that never touch the network — shadow's ledger, sizing math.
# Every updown series prices at 0.07; the rest of the wallet shows 0.03 /
# 0.04 / 0.05 too, so the RATE is per-market and only the shape is fixed.
FEE_RATE = 0.07

# Chainlink-TWAP vs Binance-proxy disagreement band (bp of final margin),
# as a flat default. Real per-symbol guards are MEASURED and live in
# chainlink.GUARD_BP — this is only the fallback for a symbol with no
# measured corpus yet, and the "is this a coin flip" band in the one-shot
# `pmt crypto updown` pricing view.
BASIS_NOISE_BP = 3.0


def taker_fee(price: float, fee_rate: float = FEE_RATE) -> float:
    """Per-share taker fee: `rate * p * (1 - p)`. Mirrors pmengine's
    `crate::fees::taker_fee` — one shape in two languages.

    MEASURED, not transcribed. Recovering the realized fee from
    `~/.pmt/corpus/activity.jsonl` as `usdcSize - price*size` matches this
    on 1017 of 1017 fee-bearing updown fills, with no size dependence and no
    per-series difference; across the whole wallet the implied rate lands on
    0.030/0.040/0.050/0.070 and nothing else, so the RATE is per-market and
    the SHAPE is universal. This replaced `rate * min(p, 1-p)`, which is
    contradicted by 822 of those 1017 rows and over-charges by
    `1/max(p, 1-p)` — 2x at p = 0.50, where the mid-price lane lives.

    A resting (maker) fill pays exactly 0.0 — 526 of 526 wallet rows — so a
    maker path calls nothing here rather than passing a rate of zero.
    """
    return fee_rate * price * (1.0 - price)
