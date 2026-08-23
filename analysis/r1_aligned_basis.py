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

Run: cd pmtrader && uv run python ../analysis/r1_aligned_basis.py
"""
from __future__ import annotations

import argparse
import bisect
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "pmtrader"))

from polymarket import chainlink as ck  # noqa: E402  (needs sys.path patch above)
from polymarket.fit import fetch_klines  # noqa: E402

STALE_S = 600           # no round in the prior 10 min -> feed is stale, drop the window
FETCH_BUFFER_H = 2.0     # backfill a little past the analysis window so edge minutes aren't starved
# 2026-08-23 live per-arm guards (ROADMAP.md) — what the aligned p95/p99 below gets checked against.
GUARD_BP_LIVE = {"btc": 3.0, "eth": 8.0, "sol": 10.0, "xrp": None, "doge": None}

# polygon-rpc.com is currently 401ing (dead) — chainlink.py still lists it first as the
# "canonical gateway" fallback, which costs ~2s of retries+backoff per RPC batch. Reorder
# in-process only (no edit to the shared module) since a 48h backfill is thousands of batches.
if ck._RPC_URLS and "polygon-rpc.com" in ck._RPC_URLS[0]:
    ck._RPC_URLS = ck._RPC_URLS[1:] + ck._RPC_URLS[:1]


# ---------- corpus extension: only walk the hours not already on disk (gentle on public RPCs) ----------

def extend_corpus_backward(symbol: str, target_hours: float) -> int:
    """Walk further back from the corpus's oldest known round to cover `target_hours`.

    Mirrors chainlink.fetch_rounds' walk, but starts at the existing corpus's
    oldest round instead of the live latest — re-fetching hours we already
    have would double the RPC load on the public endpoints for no reason.
    """
    existing = ck.load_corpus(symbol)
    if not existing:
        return ck.append_corpus(symbol, ck.fetch_rounds(symbol, hours=target_hours))
    existing.sort(key=lambda r: r["round_id"])
    oldest = existing[0]
    cutoff = time.time() - target_hours * 3600
    if oldest["updated_at"] <= cutoff:
        return 0
    feed = ck.FEEDS[symbol]
    addr, scale = feed["address"], 10 ** feed["decimals"]
    phase, agg = ck.split_round_id(oldest["round_id"])
    agg -= 1
    collected = []
    while agg >= 0:
        ids = list(range(agg, max(agg - ck.BATCH_SIZE, -1), -1))
        calls = [(addr, ck._encode_round_id_call(ck.join_round_id(phase, i))) for i in ids]
        raws = ck._eth_call_batch(calls)
        done = False
        for i, raw in zip(ids, raws):
            if raw is None:
                continue
            rd = ck._decode_round(raw)
            if rd["updated_at"] <= 0 or rd["updated_at"] < cutoff:
                done = True  # zero updated_at = phase start; below cutoff = out of window
                break
            collected.append(rd)
        if done:
            break
        agg -= ck.BATCH_SIZE
        time.sleep(0.05)  # be polite to the public endpoint
    rows = [{"round_id": r["round_id"], "price": r["answer"] / scale, "updated_at": r["updated_at"]}
            for r in collected]
    return ck.append_corpus(symbol, rows)


def extend_all(target_hours: float, symbols: list[str]) -> None:
    for sym in symbols:
        try:
            topped = ck.append_corpus(sym, ck.fetch_rounds(sym, hours=3.0))  # close the gap since last fetch
        except Exception as e:
            print(f"  {sym:5s} top-up failed: {e}")
            topped = 0
        try:
            backfilled = extend_corpus_backward(sym, target_hours)
        except Exception as e:
            print(f"  {sym:5s} backfill failed: {e}")
            backfilled = 0
        print(f"  {sym:5s} +{topped:4d} recent  +{backfilled:5d} backfilled")


# ---------- Chainlink TWAP over an arbitrary [start, end) window ----------

def twap_over_window(rounds: list[dict], ts_list: list[int], start: int, end: int) -> float | None:
    """Time-weighted average of the step function (answer holds until the next round).

    None if there's no round at/before `start` (no carry-in value) or the
    feed was already stale entering the window (no update in the prior STALE_S).
    """
    idx = bisect.bisect_right(ts_list, start) - 1
    if idx < 0 or start - ts_list[idx] > STALE_S:
        return None
    total, cur_t, j, n = 0.0, start, idx, len(ts_list)
    while cur_t < end:
        nxt_t = min(ts_list[j + 1], end) if j + 1 < n else end
        total += rounds[j]["price"] * (nxt_t - cur_t)
        cur_t, j = nxt_t, j + 1
    return total / (end - start)


# ---------- Binance proxy mark: (open+close)/2 per minute — same shape the engine prices with ----------

def fetch_minute_marks(binance_symbol: str, start_s: int, end_s: int) -> dict[int, float]:
    out: dict[int, float] = {}
    cursor_ms = (start_s // 60) * 60_000
    end_ms = (end_s // 60) * 60_000
    while cursor_ms <= end_ms:
        kl = fetch_klines(binance_symbol, "1m", start_ms=cursor_ms)
        if not kl:
            break
        for k in kl:
            t_ms = int(k[0])
            if t_ms > end_ms:
                break
            out[t_ms // 1000] = (float(k[1]) + float(k[4])) / 2  # (open+close)/2, the engine's mark
        if len(kl) < 1000:
            break
        cursor_ms = int(kl[-1][0]) + 60_000
    return out


# ---------- the two variants ----------

def per_minute_basis(rounds: list[dict], marks: dict[int, float]) -> list[dict]:
    """aligned_basis_bp(m) = (chainlink_twap(m)/binance_mark(m) - 1) * 1e4 for each minute [m, m+60)."""
    if not rounds:
        return []
    ts_list = [r["updated_at"] for r in rounds]
    first_m = ((rounds[0]["updated_at"] // 60) + 1) * 60
    last_m = ((rounds[-1]["updated_at"] // 60) + 1) * 60
    out = []
    for m in range(first_m, last_m, 60):
        twap = twap_over_window(rounds, ts_list, m, m + 60)
        mark = marks.get(m)
        if twap is None or not mark:
            continue
        out.append({"t": m, "basis_bp": (twap / mark - 1) * 1e4})
    return out


def settlement_basis(rounds: list[dict], marks: dict[int, float], period_s: int, ck_window_s: int) -> list[dict]:
    """The error that actually decides wins/losses: Chainlink's real settlement TWAP
    vs the last Binance mark the model banks (the minute ending at the boundary)."""
    if not rounds:
        return []
    ts_list = [r["updated_at"] for r in rounds]
    first_end = ((rounds[0]["updated_at"] // period_s) + 1) * period_s
    last_end = ((rounds[-1]["updated_at"] // period_s) + 1) * period_s
    out = []
    for end in range(first_end, last_end, period_s):
        twap = twap_over_window(rounds, ts_list, end - ck_window_s, end)
        mark = marks.get(end - 60)
        if twap is None or not mark:
            continue
        out.append({"t": end, "basis_bp": (twap / mark - 1) * 1e4})
    return out


# ---------- stats + report ----------

def abs_stats(rows: list[dict]) -> dict | None:
    vals = sorted(abs(r["basis_bp"]) for r in rows)
    n = len(vals)
    if n == 0:
        return None
    mean = sum(vals) / n
    std = (sum((v - mean) ** 2 for v in vals) / (n - 1)) ** 0.5 if n > 1 else 0.0
    return {
        "n": n, "mean": mean, "std": std,
        "p50": ck._percentile(vals, 50), "p90": ck._percentile(vals, 90),
        "p95": ck._percentile(vals, 95), "p99": ck._percentile(vals, 99),
        "max": vals[-1],
    }


def signed_bias(rows: list[dict]) -> float:
    vals = [r["basis_bp"] for r in rows]
    return sum(vals) / len(vals) if vals else 0.0


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
        extend_all(args.hours + FETCH_BUFFER_H, args.symbols)
        print()

    cutoff = time.time() - args.hours * 3600
    per_min_stats: dict[str, dict | None] = {}
    settle5_stats: dict[str, dict | None] = {}
    settle15_stats: dict[str, dict | None] = {}

    print("stats are all on |aligned_basis_bp| (n / mean / std / p50 / p90 / p95 / p99 / max)\n")
    for sym in args.symbols:
        rounds = sorted(ck.load_corpus(sym, since=cutoff), key=lambda r: r["updated_at"])
        if not rounds:
            print(f"{sym.upper()}: no corpus data")
            continue
        span_h = (rounds[-1]["updated_at"] - rounds[0]["updated_at"]) / 3600
        marks = fetch_minute_marks(ck.BINANCE_SYMBOL[sym], rounds[0]["updated_at"], rounds[-1]["updated_at"] + 60)

        pm = per_minute_basis(rounds, marks)
        s5 = settlement_basis(rounds, marks, period_s=300, ck_window_s=30)
        s15 = settlement_basis(rounds, marks, period_s=900, ck_window_s=60)
        per_min_stats[sym], settle5_stats[sym], settle15_stats[sym] = abs_stats(pm), abs_stats(s5), abs_stats(s15)

        print(f"{sym.upper()}/USD  ({len(rounds)} rounds, {span_h:.1f}h span)")
        print_stats_row("per-minute", per_min_stats[sym])
        print_stats_row("settlement-5m", settle5_stats[sym])
        print_stats_row("settlement-15m", settle15_stats[sym])
        if pm:
            print(f"  {'bias (signed)':16s}  per-min mean={signed_bias(pm):+.2f}bp  "
                  f"5m mean={signed_bias(s5):+.2f}bp  15m mean={signed_bias(s15):+.2f}bp")

        # day-over-day regime check on the per-minute series: first half of the window vs second half
        if len(pm) > 20:
            mid = len(pm) // 2
            d1, d2 = abs_stats(pm[:mid]), abs_stats(pm[mid:])
            if d1 and d2:
                shifted = d2["p95"] > 1.5 * d1["p95"] or d1["p95"] > 1.5 * d2["p95"]
                flag = "  <-- regime shift" if shifted else ""
                print(f"  day1 p95={d1['p95']:.2f}bp (n={d1['n']})   day2 p95={d2['p95']:.2f}bp (n={d2['n']}){flag}")

        guard = GUARD_BP_LIVE.get(sym)
        if guard is not None and per_min_stats[sym]:
            p95 = per_min_stats[sym]["p95"]
            verdict = "covers p95" if guard >= p95 else "TOO TIGHT vs p95"
            print(f"  live guard {guard:.1f}bp vs aligned per-minute p95 {p95:.2f}bp -> {verdict}")
        print()


if __name__ == "__main__":
    main()
