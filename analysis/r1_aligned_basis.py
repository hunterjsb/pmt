#!/usr/bin/env python3
"""R1 aligned basis — TWAP-vs-TWAP Chainlink-vs-Binance (ROADMAP.md Phase 2, R1).

Tonight's first measurement (`pmt crypto basis`) compared point-in-time
Chainlink rounds against 1m Binance closes — that bakes in up to 60s of pure
timing noise on top of real basis (worst outliers all clustered in one
flash-move minute) and over-states the true settlement error. This measures
what actually matters instead:

  1. per-minute aligned basis: time-weighted Chainlink TWAP over [m, m+60)
     vs the engine's own Binance mark ((open+close)/2 of the 1m kline) for
     the same minute — an apples-to-apples rolling series.
  2. settlement-shaped basis: at every real settlement boundary, Chainlink's
     actual settlement TWAP (30s @5m close, 60s @15m close) vs the last
     Binance mark the model banks (minute ending at that boundary) — this is
     the error that actually decides wins/losses at the wire.

The measurement itself now lives in polymarket.chainlink (also used by
`pmt crypto basis --aligned`) — this script is the deep-dive wrapper: same
numbers, plus the day-over-day regime check and live-guard verdict that a
one-off study needs but the CLI report doesn't.

Run: cd pmtrader && uv run python ../analysis/r1_aligned_basis.py
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "pmtrader"))

from polymarket import chainlink as ck  # noqa: E402  (needs sys.path patch above)


def extend_all(target_hours: float, symbols: list[str]) -> None:
    for sym, r in ck.extend_all(target_hours, symbols).items():
        if r["top_up_error"]:
            print(f"  {sym:5s} top-up failed: {r['top_up_error']}")
        if r["backfill_error"]:
            print(f"  {sym:5s} backfill failed: {r['backfill_error']}")
        print(f"  {sym:5s} +{r['topped']:4d} recent  +{r['backfilled']:5d} backfilled")


def print_stats_row(label: str, s: dict | None) -> None:
    if s is None:
        print(f"  {label:16s}  no data")
        return
    print(f"  {label:16s}  n={s['n']:5d}  mean={s['mean']:6.2f}  std={s['std']:6.2f}  "
          f"p50={s['p50']:6.2f}  p90={s['p90']:6.2f}  p95={s['p95']:6.2f}  p99={s['p99']:6.2f}  max={s['max']:7.2f}")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--symbols", nargs="+", default=ck.SYMBOLS)
    ap.add_argument("--hours", type=float, default=48.0, help="analysis window (min per R1 spec)")
    ap.add_argument("--no-fetch", action="store_true", help="skip corpus extension, use what's on disk")
    args = ap.parse_args()

    if not args.no_fetch:
        print(f"extending corpus to >= {args.hours:.0f}h for {args.symbols} ...")
        extend_all(args.hours + ck.ALIGNED_FETCH_BUFFER_H, args.symbols)
        print()

    cutoff = time.time() - args.hours * 3600
    per_min_stats: dict[str, dict | None] = {}

    print("stats are all on |aligned_basis_bp| (n / mean / std / p50 / p90 / p95 / p99 / max)\n")
    for sym in args.symbols:
        rounds = sorted(ck.load_corpus(sym, since=cutoff), key=lambda r: r["updated_at"])
        if not rounds:
            print(f"{sym.upper()}: no corpus data")
            continue
        span_h = (rounds[-1]["updated_at"] - rounds[0]["updated_at"]) / 3600
        marks = ck.fetch_minute_marks(ck.BINANCE_SYMBOL[sym], rounds[0]["updated_at"], rounds[-1]["updated_at"] + 60)

        pm = ck.per_minute_basis(rounds, marks)
        s5 = ck.settlement_basis(rounds, marks, period_s=300, ck_window_s=30)
        s15 = ck.settlement_basis(rounds, marks, period_s=900, ck_window_s=60)
        per_min_stats[sym] = ck.aligned_stats(pm)
        settle5_stats, settle15_stats = ck.aligned_stats(s5), ck.aligned_stats(s15)

        print(f"{sym.upper()}/USD  ({len(rounds)} rounds, {span_h:.1f}h span)")
        print_stats_row("per-minute", per_min_stats[sym])
        print_stats_row("settlement-5m", settle5_stats)
        print_stats_row("settlement-15m", settle15_stats)
        if pm:
            print(f"  {'bias (signed)':16s}  per-min mean={ck.signed_bias(pm):+.2f}bp  "
                  f"5m mean={ck.signed_bias(s5):+.2f}bp  15m mean={ck.signed_bias(s15):+.2f}bp")

        # day-over-day regime check on the per-minute series: first half of the window vs second half
        if len(pm) > 20:
            mid = len(pm) // 2
            d1, d2 = ck.aligned_stats(pm[:mid]), ck.aligned_stats(pm[mid:])
            if d1 and d2:
                shifted = d2["p95"] > 1.5 * d1["p95"] or d1["p95"] > 1.5 * d2["p95"]
                flag = "  <-- regime shift" if shifted else ""
                print(f"  day1 p95={d1['p95']:.2f}bp (n={d1['n']})   day2 p95={d2['p95']:.2f}bp (n={d2['n']}){flag}")

        # The deployed guards themselves — this script used to keep its own
        # copy of them, which drifted (btc 3 vs the live 6) and quietly
        # graded the wrong number against p95.
        guard = ck.GUARD_BP.get(sym)
        if guard is not None and per_min_stats[sym]:
            p95 = per_min_stats[sym]["p95"]
            verdict = "covers p95" if guard >= p95 else "TOO TIGHT vs p95"
            print(f"  live guard {guard:.1f}bp vs aligned per-minute p95 {p95:.2f}bp -> {verdict}")
        print()


if __name__ == "__main__":
    main()
