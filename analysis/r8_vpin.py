#!/usr/bin/env python3
"""R8 VPIN prototype (ROADMAP.md Phase 2, R8 "near-even late-flow guard").

Volume-bucket VPIN (Easley/Lopez de Prado/O'Hara) over two flow sources:

  1. Polymarket prints — book-tape's signed print-flow fields
     (up_tbuy/up_tsell/dn_tbuy/dn_tsell), shipped 2026-08-23 05:30Z. Only
     hours of corpus exist; this script validates the pipeline first
     (see `validate_poly_pipeline`) rather than trusting the field names.
  2. Binance aggTrades for BTCUSDT via data-api.binance.vision, backfilled
     for the same wall-clock windows the book-tape covers. `isBuyerMaker`
     gives the true taker side directly — no tick-rule/BVC classification
     needed on either source, since Polymarket's tape and Binance's
     aggTrades both carry a real aggressor side.

Per btc window (5m/15m, joined to outcomes.jsonl): VPIN at the first real
fire (entry-relevant), VPIN with 2 minutes left on the clock, and whether
the fired side lost when its checkpoint VPIN was high — the Bartlett &
O'Hara (2026) claim applied to us as the taker. Also computes the cheap v1
alternative the roadmap names — a consecutive-same-side print counter —
and checks whether it tracks VPIN closely enough to gate on directly
(cheaper: no bucketing, just a running streak length).

Run: cd pmtrader && uv run python ../analysis/r8_vpin.py
"""
from __future__ import annotations

import argparse
import json
import math
import time
from collections import defaultdict
from pathlib import Path

import requests

HOME = Path.home()
BOOK_TAPE = HOME / ".pmt/engine/book-tape.jsonl"
UPDOWN_TAPE = HOME / ".pmt/engine/updown-tape.jsonl"
OUTCOMES = HOME / ".pmt/corpus/outcomes.jsonl"

BINANCE_DATA = "https://data-api.binance.vision"
POLY_DATA = "https://data-api.polymarket.com"
REQUEST_TIMEOUT = 15

CACHE_DIR = Path("/tmp/claude-1000/-var-home-hunter/35f80f35-e0c9-4e4d-80ea-9c5602f70444/scratchpad")
CACHE_DIR.mkdir(parents=True, exist_ok=True)

DUR_SECONDS = {"5m": 300, "15m": 900, "4h": 14400}


# ---------------------------------------------------------------------------
# Volume-bucket VPIN core (shared by both flow sources)
# ---------------------------------------------------------------------------

def bucket_events(events: list[tuple[float, float, float]], bucket_size: float) -> list[dict]:
    """Split a chronological (t, buy_vol, sell_vol) stream into fixed-volume
    buckets, splitting an event across a bucket boundary so no volume is
    dropped or double counted (the standard VPIN bucketing rule).

    Returns closed buckets only: {"t_close", "buy", "sell", "vol"}. A
    trailing partial bucket (didn't reach bucket_size) is dropped — it has
    no defined VPIN contribution yet.
    """
    buckets: list[dict] = []
    bt = bs = bv = 0.0
    for t, buy, sell in events:
        total = buy + sell
        if total <= 0:
            continue
        buy_frac = buy / total
        remaining = total
        while remaining > 1e-12:
            space = bucket_size - bv
            take = min(space, remaining)
            bt += take * buy_frac
            bs += take * (1.0 - buy_frac)
            bv += take
            remaining -= take
            if bv >= bucket_size - 1e-9:
                buckets.append({"t_close": t, "buy": bt, "sell": bs, "vol": bv})
                bt = bs = bv = 0.0
    return buckets


def rolling_vpin(buckets: list[dict], n: int) -> list[float | None]:
    """VPIN_i = mean over the trailing n buckets of |buy-sell|/vol. None
    until n buckets have closed (standard warm-up gap)."""
    out: list[float | None] = []
    imb: list[float] = []
    for b in buckets:
        imb.append(abs(b["buy"] - b["sell"]) / b["vol"] if b["vol"] > 0 else 0.0)
        if len(imb) < n:
            out.append(None)
        else:
            out.append(sum(imb[-n:]) / n)
    return out


def vpin_at(buckets: list[dict], vpins: list[float | None], t: float) -> tuple[float | None, int]:
    """VPIN reading from the last bucket closed at or before t, plus how
    many buckets had closed by then (0 => no reading possible)."""
    idx = -1
    for i, b in enumerate(buckets):
        if b["t_close"] <= t:
            idx = i
        else:
            break
    if idx < 0:
        return None, 0
    return vpins[idx], idx + 1


def consecutive_same_side_at(events: list[tuple[float, str]], t: float) -> int | None:
    """Cheap v1: length of the run of consecutive same-side prints ending
    at or before t. `events` is a chronological (timestamp, side) stream
    at print (not bucket) granularity — this is the whole appeal of the
    counter, it needs no volume math, just a running side comparison."""
    streak = 0
    last_side = None
    seen = False
    for ts, side in events:
        if ts > t:
            break
        seen = True
        if side == last_side:
            streak += 1
        else:
            streak = 1
            last_side = side
    return streak if seen else None


def pearson(xs: list[float], ys: list[float]) -> float | None:
    n = len(xs)
    if n < 3:
        return None
    mx, my = sum(xs) / n, sum(ys) / n
    sxy = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    sxx = sum((x - mx) ** 2 for x in xs)
    syy = sum((y - my) ** 2 for y in ys)
    if sxx <= 0 or syy <= 0:
        return None
    return sxy / math.sqrt(sxx * syy)


# ---------------------------------------------------------------------------
# Source 1: Polymarket prints from book-tape
# ---------------------------------------------------------------------------

def load_book_tape() -> list[dict]:
    rows = []
    with open(BOOK_TAPE) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return rows


def validate_poly_pipeline(rows: list[dict]) -> dict:
    """Does the book-tape's print-flow actually carry prints? Checked
    against the tape's own totals, then cross-checked live against
    Polymarket's public trades feed (independent of the engine's recorder)
    so a "zero" verdict here can't be blamed on updown markets being
    quiet — if live volume shows otherwise, the recorder is broken."""
    total_rows = len(rows)
    nonzero_rows = 0
    total_buy = total_sell = 0.0
    ts = [r["t"] for r in rows if r.get("t")]
    for r in rows:
        buy = (r.get("up_tbuy") or 0) + (r.get("dn_tbuy") or 0)
        sell = (r.get("up_tsell") or 0) + (r.get("dn_tsell") or 0)
        if buy or sell:
            nonzero_rows += 1
        total_buy += buy
        total_sell += sell

    span_s = (max(ts) - min(ts)) if ts else 0.0
    result = {
        "total_rows": total_rows,
        "span_hours": round(span_s / 3600, 2),
        "distinct_slugs": len({r.get("slug") for r in rows}),
        "nonzero_print_rows": nonzero_rows,
        "total_buy_vol": total_buy,
        "total_sell_vol": total_sell,
        "verdict": None,
        "live_crosscheck": None,
    }

    # Live, model-free cross-check: recent global Polymarket trades, how
    # many are on *-updown-* markets. If this is nonzero while the tape
    # is all-zero, the tape's recorder — not market activity — is broken.
    try:
        resp = requests.get(f"{POLY_DATA}/trades", params={"limit": 500}, timeout=REQUEST_TIMEOUT)
        resp.raise_for_status()
        trades = resp.json()
        updown = [t for t in trades if "updown" in (t.get("slug") or "")]
        result["live_crosscheck"] = {
            "sampled": len(trades),
            "updown_trades": len(updown),
            "newest_ts": max((t.get("timestamp", 0) for t in trades), default=None),
            "example_slugs": sorted({t["slug"] for t in updown})[:5],
        }
    except requests.RequestException as e:
        result["live_crosscheck"] = {"error": str(e)}

    if nonzero_rows == 0 and total_rows > 0:
        live = result["live_crosscheck"] or {}
        if live.get("updown_trades", 0) > 0:
            result["verdict"] = (
                f"BROKEN: {total_rows} book-tape rows over {result['span_hours']}h, "
                f"{result['distinct_slugs']} slugs, 0 nonzero print rows — yet the live "
                f"Polymarket trades feed shows {live['updown_trades']}/{live['sampled']} "
                "recent global trades are on updown markets RIGHT NOW "
                f"(e.g. {live.get('example_slugs')}). Markets are actively trading; the "
                "book-tape's print-flow recorder (pmengine's public-trades REST poller, "
                "engine.rs trade_tape_handle -> client.rs get_market_trades_since) is not "
                "landing fills into the tape. Poly-side VPIN cannot be computed on this "
                "corpus until that's fixed — do not ship R8 gated on this feed yet."
            )
        else:
            result["verdict"] = "INCONCLUSIVE: tape is all-zero and live cross-check unavailable/also empty."
    elif total_rows == 0:
        result["verdict"] = "NO DATA: book-tape.jsonl is empty or missing."
    else:
        result["verdict"] = f"OK: {nonzero_rows}/{total_rows} rows carry nonzero print flow."
    return result


def poly_vpin_per_window(rows: list[dict], bucket_size: float, n: int) -> dict[str, dict]:
    """VPIN per slug, per side (up/down), from book-tape deltas. Each row
    already IS a (t, buy, sell) delta-since-last-sample event (computed by
    the engine's own `flow()` closure) — no re-aggregation needed."""
    by_slug: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        s = r.get("slug")
        if s:
            by_slug[s].append(r)

    out = {}
    for slug, rs in by_slug.items():
        rs.sort(key=lambda r: r["t"])
        up_events = [(r["t"], r.get("up_tbuy") or 0.0, r.get("up_tsell") or 0.0) for r in rs]
        dn_events = [(r["t"], r.get("dn_tbuy") or 0.0, r.get("dn_tsell") or 0.0) for r in rs]
        up_buckets = bucket_events(up_events, bucket_size)
        dn_buckets = bucket_events(dn_events, bucket_size)
        out[slug] = {
            "up_buckets": len(up_buckets),
            "dn_buckets": len(dn_buckets),
            "up_vpin_series": rolling_vpin(up_buckets, n),
            "dn_vpin_series": rolling_vpin(dn_buckets, n),
            "up_buckets_raw": up_buckets,
            "dn_buckets_raw": dn_buckets,
        }
    return out


# ---------------------------------------------------------------------------
# Source 2: Binance aggTrades (backfillable)
# ---------------------------------------------------------------------------

def fetch_aggtrades(symbol: str, start_ms: int, end_ms: int, cache_tag: str) -> list[dict]:
    """Paginate data-api.binance.vision aggTrades over [start_ms, end_ms).
    Cached to scratchpad — BTCUSDT aggTrades run ~40-45/s, a 60min pull is
    ~150k rows / ~150 requests and worth not repeating."""
    cache_path = CACHE_DIR / f"aggtrades_{symbol}_{cache_tag}.jsonl"
    if cache_path.exists():
        trades = []
        with open(cache_path) as f:
            for line in f:
                trades.append(json.loads(line))
        return trades

    trades: list[dict] = []
    cur = start_ms
    session = requests.Session()
    req_n = 0
    while cur < end_ms:
        req_n += 1
        resp = session.get(
            f"{BINANCE_DATA}/api/v3/aggTrades",
            params={"symbol": symbol, "startTime": cur, "endTime": end_ms, "limit": 1000},
            timeout=REQUEST_TIMEOUT,
        )
        resp.raise_for_status()
        batch = resp.json()
        if not batch:
            break
        trades.extend(batch)
        last_t = batch[-1]["T"]
        if last_t <= cur:
            break  # guard against a non-advancing cursor
        cur = last_t + 1
        if len(batch) < 1000:
            break  # short page => caught up to end_ms
    with open(cache_path, "w") as f:
        for t in trades:
            f.write(json.dumps(t) + "\n")
    print(f"  fetched {len(trades)} {symbol} aggTrades over {req_n} requests -> {cache_path}")
    return trades


def binance_events(trades: list[dict]) -> tuple[list[tuple[float, float, float]], list[tuple[float, str]]]:
    """(t, buy_vol, sell_vol) per trade for bucketing, and (t, side) for
    the streak counter. isBuyerMaker=True means the buyer posted the
    resting order — the SELLER was the aggressor, i.e. a taker sell."""
    bucket_events_, side_events = [], []
    for t in trades:
        ts = t["T"] / 1000.0
        q = float(t["q"])
        is_buyer_maker = t["m"]
        if is_buyer_maker:
            bucket_events_.append((ts, 0.0, q))
            side_events.append((ts, "sell"))
        else:
            bucket_events_.append((ts, q, 0.0))
            side_events.append((ts, "buy"))
    return bucket_events_, side_events


# ---------------------------------------------------------------------------
# Outcomes / fires join
# ---------------------------------------------------------------------------

def load_outcomes() -> dict[str, str]:
    outs = {}
    if OUTCOMES.exists():
        with open(OUTCOMES) as f:
            for line in f:
                d = json.loads(line)
                outs[d["slug"]] = d["winner"]
    return outs


def load_first_fires() -> dict[str, dict]:
    """First `fire` event per slug from updown-tape — the entry-relevant
    timestamp + side actually taken."""
    fires: dict[str, dict] = {}
    if not UPDOWN_TAPE.exists():
        return fires
    with open(UPDOWN_TAPE) as f:
        for line in f:
            try:
                d = json.loads(line)
            except json.JSONDecodeError:
                continue
            if d.get("ev") != "fire":
                continue
            s = d["slug"]
            if s not in fires or d["t"] < fires[s]["t"]:
                fires[s] = d
    return fires


def parse_slug(slug: str) -> tuple[str, str, int] | None:
    parts = slug.split("-")
    if len(parts) != 4 or parts[1] != "updown":
        return None
    sym, _, dur, epoch = parts
    if dur not in DUR_SECONDS:
        return None
    return sym, dur, int(epoch)


# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------

def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--symbol", default="BTCUSDT")
    ap.add_argument("--poly-symbol", default="btc", help="updown slug symbol prefix to join against")
    ap.add_argument("--minutes", type=float, default=65.0, help="how much of the book-tape window to backfill from Binance")
    ap.add_argument("--bucket-shares", type=float, default=50.0, help="Poly-side bucket size (shares)")
    ap.add_argument("--n", type=int, default=20, help="rolling VPIN bucket count")
    args = ap.parse_args()

    print("=" * 78)
    print("PHASE A — Polymarket print-flow pipeline validation")
    print("=" * 78)
    rows = load_book_tape()
    validation = validate_poly_pipeline(rows)
    for k, v in validation.items():
        if k not in ("verdict",):
            print(f"  {k}: {v}")
    print(f"\n  VERDICT: {validation['verdict']}\n")

    outcomes = load_outcomes()
    fires = load_first_fires()

    poly_slugs_with_outcome = sorted(
        {r["slug"] for r in rows if r.get("slug") in outcomes}
    )
    poly_vpin = poly_vpin_per_window(rows, args.bucket_shares, args.n)
    poly_bucket_totals = sum(v["up_buckets"] + v["dn_buckets"] for v in poly_vpin.values())
    print(f"  Poly-side VPIN attempted on {len(poly_slugs_with_outcome)} outcome-joined windows, "
          f"bucket={args.bucket_shares} shares, n={args.n} -> {poly_bucket_totals} total closed buckets "
          f"across ALL {len(poly_vpin)} slugs (expect 0 while the pipeline is broken).")

    print()
    print("=" * 78)
    print(f"PHASE B — Binance aggTrades VPIN backfill ({args.symbol})")
    print("=" * 78)

    # Anchor the Binance pull to the book-tape's own time span so the same
    # wall-clock windows can be joined to outcomes.jsonl.
    ts = [r["t"] for r in rows if r.get("t")]
    tape_start, tape_end = min(ts), max(ts)
    start_ms = int(tape_start * 1000)
    end_ms = min(int((tape_start + args.minutes * 60) * 1000), int(tape_end * 1000))
    cache_tag = f"{start_ms}_{end_ms}"
    print(f"  pulling [{start_ms}, {end_ms}) (~{args.minutes:.0f} min of the {((tape_end - tape_start) / 60):.0f} min book-tape span)")
    t0 = time.time()
    trades = fetch_aggtrades(args.symbol, start_ms, end_ms, cache_tag)
    print(f"  {len(trades)} trades, {time.time() - t0:.1f}s")
    if not trades:
        print("  no trades fetched — aborting Binance analysis")
        return

    bucket_ev, side_ev = binance_events(trades)
    total_vol = sum(b + s for _, b, s in bucket_ev)
    # Size buckets for ~40-60 buckets per 5-minute window so n=20 rolling
    # has warm-up room even inside a single 5m arm's life.
    minutes_covered = (trades[-1]["T"] - trades[0]["T"]) / 60000.0
    target_buckets = max(1, round(minutes_covered / 5.0 * 50))
    bucket_size = total_vol / target_buckets
    print(f"  total base volume {total_vol:.2f} {args.symbol[:3]} over {minutes_covered:.1f} min "
          f"-> bucket_size={bucket_size:.4f} ({target_buckets} target buckets), n={args.n} rolling")

    buckets = bucket_events(bucket_ev, bucket_size)
    vpins = rolling_vpin(buckets, args.n)
    print(f"  {len(buckets)} closed buckets, {sum(1 for v in vpins if v is not None)} with a defined VPIN")

    # Per-window join.
    window_rows = []
    for slug in poly_slugs_with_outcome:
        parsed = parse_slug(slug)
        if not parsed:
            continue
        sym, dur, epoch = parsed
        if sym != args.poly_symbol:
            continue
        dur_s = DUR_SECONDS[dur]
        w_open, w_close = float(epoch), float(epoch) + dur_s
        if w_open < tape_start or w_close > (end_ms / 1000.0):
            continue  # outside the Binance-backfilled span

        fire = fires.get(slug)
        if fire:
            entry_t, entry_fallback = fire["t"], False
        else:
            entry_t, entry_fallback = w_open + 0.4 * dur_s, True  # mid-window proxy, no real entry

        final2m_t = w_close - 120.0

        entry_vpin, entry_nbuckets = vpin_at(buckets, vpins, entry_t)
        final2m_vpin, final2m_nbuckets = vpin_at(buckets, vpins, final2m_t)
        entry_streak = consecutive_same_side_at(side_ev, entry_t)
        final2m_streak = consecutive_same_side_at(side_ev, final2m_t)

        side_taken = fire["side"] if fire else None
        winner = outcomes[slug]
        won = (winner == side_taken) if side_taken else None

        window_rows.append({
            "slug": slug, "dur": dur, "winner": winner,
            "fired": fire is not None, "side_taken": side_taken, "won": won,
            "entry_t_is_fallback": entry_fallback,
            "entry_vpin": entry_vpin, "entry_nbuckets": entry_nbuckets, "entry_streak": entry_streak,
            "final2m_vpin": final2m_vpin, "final2m_nbuckets": final2m_nbuckets, "final2m_streak": final2m_streak,
        })

    print(f"\n  {len(window_rows)} {args.poly_symbol} windows joined (outcome + inside Binance backfill span)")

    print()
    print("=" * 78)
    print("PHASE C — VPIN vs outcomes, and the cheap streak-counter check")
    print("=" * 78)

    have_final2m = [w for w in window_rows if w["final2m_vpin"] is not None]
    print(f"  windows with a defined final-2min VPIN: {len(have_final2m)}/{len(window_rows)} "
          f"(needs >= n={args.n} buckets closed by close-120s)")
    if have_final2m:
        vals = [w["final2m_vpin"] for w in have_final2m]
        print(f"  final2m VPIN: min={min(vals):.4f} median={sorted(vals)[len(vals)//2]:.4f} max={max(vals):.4f}")

    fired = [w for w in window_rows if w["fired"] and w["final2m_vpin"] is not None]
    print(f"\n  fired windows with a final2m VPIN reading: {len(fired)}")
    high_low = None
    if len(fired) >= 4:
        srt = sorted(fired, key=lambda w: w["final2m_vpin"])
        mid = len(srt) // 2
        lo, hi = srt[:mid], srt[mid:]
        lo_loss = sum(1 for w in lo if w["won"] is False) / len(lo)
        hi_loss = sum(1 for w in hi if w["won"] is False) / len(hi)
        high_low = {
            "low_group_n": len(lo), "low_group_loss_rate": lo_loss,
            "low_group_vpin_range": (lo[0]["final2m_vpin"], lo[-1]["final2m_vpin"]),
            "high_group_n": len(hi), "high_group_loss_rate": hi_loss,
            "high_group_vpin_range": (hi[0]["final2m_vpin"], hi[-1]["final2m_vpin"]),
        }
        direction_note = "[high>low, consistent with Bartlett/O'Hara]" if hi_loss > lo_loss else "[NOT high>low on this corpus]"
        print(f"  median-split on final2m VPIN: low group loses {lo_loss:.0%} (n={len(lo)}), "
              f"high group loses {hi_loss:.0%} (n={len(hi)}) {direction_note}")
    else:
        print("  fewer than 4 fired+VPIN'd windows — too small to split, reporting raw only:")
        for w in fired:
            print(f"    {w['slug']}  side={w['side_taken']} won={w['won']} final2m_vpin={w['final2m_vpin']:.4f}")

    # Cheap v1: does the streak counter track VPIN?
    paired = [(w["final2m_vpin"], w["final2m_streak"]) for w in window_rows
              if w["final2m_vpin"] is not None and w["final2m_streak"] is not None]
    paired += [(w["entry_vpin"], w["entry_streak"]) for w in window_rows
               if w["entry_vpin"] is not None and w["entry_streak"] is not None]
    r = pearson([p[0] for p in paired], [p[1] for p in paired]) if paired else None
    print(f"\n  streak-counter vs VPIN: n={len(paired)} checkpoints, pearson r={r if r is None else round(r, 3)}")
    if r is not None:
        verdict = "CORRELATES — cheap v1 tracks VPIN, ship the cheap gate first" if abs(r) >= 0.5 else \
                  "WEAK/NO correlation on this corpus — cheap v1 is not a safe stand-in for VPIN yet"
        print(f"  -> {verdict}")

    streak_split = None
    if len(fired) >= 4:
        srt = sorted([w for w in fired if w["final2m_streak"] is not None], key=lambda w: w["final2m_streak"])
        if len(srt) >= 4:
            mid = len(srt) // 2
            lo, hi = srt[:mid], srt[mid:]
            lo_loss = sum(1 for w in lo if w["won"] is False) / len(lo)
            hi_loss = sum(1 for w in hi if w["won"] is False) / len(hi)
            streak_split = {"low_group_n": len(lo), "low_group_loss_rate": lo_loss,
                             "high_group_n": len(hi), "high_group_loss_rate": hi_loss}
            print(f"  median-split on final2m streak: low loses {lo_loss:.0%} (n={len(lo)}), "
                  f"high loses {hi_loss:.0%} (n={len(hi)})")

    report = {
        "poly_pipeline_validation": {k: v for k, v in validation.items()},
        "poly_vpin_bucket_totals_all_slugs": poly_bucket_totals,
        "binance": {
            "symbol": args.symbol, "n_trades": len(trades), "total_volume": total_vol,
            "bucket_size": bucket_size, "n_buckets_closed": len(buckets), "rolling_n": args.n,
        },
        "windows": window_rows,
        "final2m_high_low_split": high_low,
        "streak_vs_vpin_pearson_r": r,
        "streak_high_low_split": streak_split,
    }
    out_path = CACHE_DIR / "r8_vpin_report.json"
    with open(out_path, "w") as f:
        json.dump(report, f, indent=2, default=str)
    print(f"\nFull report -> {out_path}")


if __name__ == "__main__":
    main()
