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

# Polymarket crypto_fees_v2 taker rate, exponent 1. The live per-market
# `feeSchedule.rate` is authoritative whenever a market has been fetched
# (crypto.parse_semantics reads it); this is the fallback for the pure
# paths that never touch the network — shadow's ledger, sizing math.
FEE_RATE = 0.07

# Chainlink-TWAP vs Binance-proxy disagreement band (bp of final margin),
# as a flat default. Real per-symbol guards are MEASURED and live in
# chainlink.GUARD_BP — this is only the fallback for a symbol with no
# measured corpus yet, and the "is this a coin flip" band in the one-shot
# `pmt crypto updown` pricing view.
BASIS_NOISE_BP = 3.0


def taker_fee(price: float, fee_rate: float = FEE_RATE) -> float:
    """crypto_fees_v2, exponent 1: per-share fee on the cheaper side of the
    pair. Mirrors pmengine's `p.fee_rate * ask.min(1.0 - ask)`."""
    return fee_rate * min(price, 1.0 - price)
