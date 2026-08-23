#!/usr/bin/env python3
"""R2 quarter-Kelly sizing simulator (ROADMAP.md Phase 2, R2).

Replaces the live engine's flat clip_usdc with a fractional-Kelly clip size
and replays it, resize-only, over every recorded FIRE in the post-brake
corpus — same entry price/timing as reality, only the notional differs.
Three sizing policies are compared on identical fire events:

    (a) actual   — the flat clip exactly as traded (recorded `size`/`ask`)
    (b) kelly-raw — quarter-Kelly on the fire's own stated `fair`
    (c) kelly-cal — quarter-Kelly on `fair` shrunk toward the wallet-measured
                    hit rate for its stated-fair bucket (isotonic over buckets)

Kelly derivation (binary payout, net of the exchange fee):
    A share pays $1 on win, $0 on lose, bought at ask `a`. Polymarket's
    taker fee is `fee = fee_rate * min(a, 1-a)` per share (transcribed from
    pmengine/src/strategies/updown.rs: `p.fee_rate * ask.min(1.0-ask)`,
    fee_rate=0.07 live — see pmtrader/polymarket/crypto.py::taker_fee).
    Let c = a + fee (effective cost per $1-payout share; the whole stake is
    spent whether you win or lose, whether or not the fee is separately
    itemized). Staking a fraction f of bankroll buys shares = f*B/c; if you
    win you hold shares worth $1 each (net profit = f*B*(1-c)/c), if you
    lose the whole stake is gone (-f*B). This is a plain 2-outcome Kelly bet
    with edge (p-c) and "odds" b=(1-c)/c, whose closed form is the textbook
    p - q/b = (p-c)/(1-c). Note p - c = p_fair - ask - fee is exactly the
    `net` field the engine already tapes, so full Kelly is net/(1-c) and
    quarter-Kelly is 0.25x that, in units of FRACTION OF BANKROLL STAKED
    (cash outlay including fee — a few percent higher than the live
    engine's ask-only clip_usdc convention at deep-discount asks, immaterial
    for the near-1.0 asks that dominate this corpus; see size_usdc()).

Calibration (policy c): bucket every joined fire by its stated `fair`,
compute each bucket's realized win rate, then fit a monotonic (isotonic,
via pool-adjacent-violators) map bucket -> calibrated probability. This is
the "current reality check: stated fair >=0.95 hits 92%" line from
ROADMAP.md R2, measured fresh on this corpus slice instead of assumed.
Calibration is fit IN-SAMPLE on the same fires it sizes (a known look-ahead
simplification for a first-pass sizing study — the walk-forward version
belongs on the replay harness once more nights land; see the module-level
caveat printed in the report).

Data:
  ~/.pmt/engine/updown-tape.jsonl  — fire records: {ev:"fire", t, slug,
      side, ask, fair, net, size, ...}. `size` is shares (already
      floor()'d to book/room limits by the live engine).
  ~/.pmt/corpus/outcomes.jsonl     — wallet/Chainlink-validated {slug,
      winner}. Windows the corpus can't validate are dropped, never
      guessed (see pmtrader/polymarket/outcomes.py) — this script inherits
      that policy: unresolved fires are excluded from P&L, reported by
      count only.

Scope: FIRE events with t >= 1787452500 (the post-brake era named in the
task — R9's safety gate + window-brake latch went live 2026-08-23 05:00Z,
this cutoff sits shortly before it in the same corrected-basis-guard night).

Run: cd pmtrader && uv run python ../analysis/r2_kelly_sim.py
     cd pmtrader && uv run python ../analysis/r2_kelly_sim.py --cap-fracs 0.1,0.15,0.25
"""
from __future__ import annotations

import argparse
import json
from collections import defaultdict
from pathlib import Path

TAPE_PATH = Path.home() / ".pmt" / "engine" / "updown-tape.jsonl"
OUTCOMES_PATH = Path.home() / ".pmt" / "corpus" / "outcomes.jsonl"

CUTOFF_T = 1787452500.0          # post-brake era, per task
DEFAULT_FEE_RATE = 0.07          # pmengine ArmParams.fee_rate live default
DEFAULT_BANKROLL = 1250.0
DEFAULT_KELLY_FRAC = 0.25
DEFAULT_CAP_FRACS = (0.10, 0.15, 0.25)
MIN_CLIP_USDC = 1.0              # below this a "sized" clip is treated as not firing

# Calibration bucket edges over stated fair — coarse at the top where the
# corpus actually lives (this policy only ever fires fair >= ~0.90) so PAVA
# has real per-bucket counts to pool instead of one point per unique float.
CAL_EDGES = [0.0, 0.90, 0.95, 0.97, 0.98, 0.99, 0.995, 0.999, 1.0 + 1e-9]


# ---------------------------------------------------------------------------
# the pure sizing function
# ---------------------------------------------------------------------------

def size_usdc(
    p_fair: float,
    ask: float,
    fee_rate: float = DEFAULT_FEE_RATE,
    bankroll: float = DEFAULT_BANKROLL,
    kelly_frac: float = DEFAULT_KELLY_FRAC,
    cap_frac: float = 0.15,
    committed_in_window: float = 0.0,
) -> float:
    """Clip notional (USDC stake, inclusive of fee) from fractional Kelly.

    p_fair: model's probability the side being sized wins.
    ask: taker price for that side's share (0,1).
    fee_rate: Polymarket taker fee rate (fraction of min(ask,1-ask) per share).
    bankroll: total bankroll the Kelly fraction is taken against.
    kelly_frac: fraction of full Kelly to actually stake (0.25 = quarter-Kelly).
    cap_frac: hard per-window ceiling as a fraction of bankroll — the
        window's total sizeable notional (across every clip in that window)
        never exceeds cap_frac*bankroll, mirroring the live engine's
        `room = cap - committed` envelope (see updown.rs decide()).
    committed_in_window: notional this policy has already sized for OTHER
        clips in the same window, so the cap binds cumulatively per window,
        not per call. Pass 0.0 (default) if sizing a single clip in isolation.

    Returns 0.0 if the edge is non-positive, the price is degenerate
    (ask<=0 or >=1), or the window's cap is already exhausted.
    """
    if not (0.0 < ask < 1.0):
        return 0.0
    fee = fee_rate * min(ask, 1.0 - ask)
    c = ask + fee                      # effective cost per $1-payout share
    if not (0.0 < c < 1.0):
        return 0.0
    edge = p_fair - c                  # == the engine's `net` when p_fair==fair
    if edge <= 0.0:
        return 0.0
    f_full = edge / (1.0 - c)          # full-Kelly fraction of bankroll (stake)
    f_stake = kelly_frac * f_full
    notional = max(0.0, f_stake) * bankroll
    room = max(0.0, cap_frac * bankroll - committed_in_window)
    return min(notional, room)


def _selftest() -> None:
    """Sanity checks run at import time — cheap, no I/O, catches sign errors."""
    # No edge -> zero size.
    assert size_usdc(0.5, 0.5, cap_frac=1.0) == 0.0
    assert size_usdc(0.90, 0.95, cap_frac=1.0) == 0.0  # fair below cost
    # Degenerate prices -> zero, never a crash.
    assert size_usdc(0.9, 0.0, cap_frac=1.0) == 0.0
    assert size_usdc(0.9, 1.0, cap_frac=1.0) == 0.0
    # Positive-edge bet sizes something positive and respects the cap.
    s = size_usdc(0.97, 0.80, fee_rate=0.07, bankroll=1000.0, kelly_frac=0.25, cap_frac=1.0)
    assert s > 0.0
    s_capped = size_usdc(0.97, 0.80, bankroll=1000.0, kelly_frac=0.25, cap_frac=0.01)
    assert abs(s_capped - 10.0) < 1e-9, s_capped  # cap_frac*bankroll = 10
    # committed_in_window eats into room even when Kelly wants more.
    s_partial = size_usdc(0.97, 0.80, bankroll=1000.0, kelly_frac=0.25, cap_frac=0.10,
                           committed_in_window=95.0)
    assert abs(s_partial - 5.0) < 1e-9, s_partial
    s_exhausted = size_usdc(0.97, 0.80, bankroll=1000.0, kelly_frac=0.25, cap_frac=0.10,
                             committed_in_window=100.0)
    assert s_exhausted == 0.0
    # Overwhelming edge is bankroll-capped, never explodes past cap_frac.
    s_huge = size_usdc(0.999999, 0.05, bankroll=1000.0, kelly_frac=1.0, cap_frac=0.15)
    assert abs(s_huge - 150.0) < 1e-6, s_huge


# ---------------------------------------------------------------------------
# corpus loading
# ---------------------------------------------------------------------------

def load_outcomes(path: Path = OUTCOMES_PATH) -> dict[str, str]:
    out = {}
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            d = json.loads(line)
            out[d["slug"]] = d["winner"]
    return out


def load_fires(path: Path = TAPE_PATH, cutoff_t: float = CUTOFF_T) -> list[dict]:
    fires = []
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                d = json.loads(line)
            except ValueError:
                continue
            if d.get("ev") == "fire" and d.get("t", 0.0) >= cutoff_t:
                fires.append(d)
    fires.sort(key=lambda d: d["t"])
    return fires


def join_fires(fires: list[dict], outcomes: dict[str, str]) -> tuple[list[dict], list[dict]]:
    """(joined, unresolved) — joined fires get a `win: bool` field."""
    joined, unresolved = [], []
    for d in fires:
        w = outcomes.get(d["slug"])
        if w is None:
            unresolved.append(d)
            continue
        joined.append({**d, "win": d["side"] == w, "winner": w})
    return joined, unresolved


# ---------------------------------------------------------------------------
# fee/pnl helpers shared by all three policies
# ---------------------------------------------------------------------------

def fee_per_share(ask: float, fee_rate: float = DEFAULT_FEE_RATE) -> float:
    return fee_rate * min(ask, 1.0 - ask)


def pnl_from_shares(shares: float, ask: float, win: bool, fee_rate: float = DEFAULT_FEE_RATE) -> float:
    """Realized P&L for `shares` bought at `ask`, per replay.rs::apply_fills:
    pnl = shares*1{win} - shares*ask - shares*fee."""
    fee = fee_per_share(ask, fee_rate)
    return shares * (1.0 if win else 0.0) - shares * ask - shares * fee


def pnl_from_notional(notional: float, ask: float, win: bool, fee_rate: float = DEFAULT_FEE_RATE) -> float:
    """Realized P&L when `notional` is the total cash staked (inclusive of
    fee — the size_usdc() convention): shares = notional/c, pnl =
    notional*(1-c)/c if win else -notional."""
    if notional <= 0.0:
        return 0.0
    fee = fee_per_share(ask, fee_rate)
    c = ask + fee
    if not (0.0 < c < 1.0):
        return 0.0
    return notional * (1.0 - c) / c if win else -notional


# ---------------------------------------------------------------------------
# calibration: bucket win-rate + isotonic (PAVA) fit
# ---------------------------------------------------------------------------

def bucket_index(fair: float, edges: list[float] = CAL_EDGES) -> int:
    for i in range(len(edges) - 1):
        if edges[i] <= fair < edges[i + 1]:
            return i
    return len(edges) - 2


def pava(values: list[float], weights: list[float]) -> list[float]:
    """Pool-adjacent-violators: weighted isotonic (non-decreasing) fit.
    Returns fitted values, same length/order as the input."""
    stack: list[list[float]] = []  # [sum(w*v), sum(w), count]
    for v, w in zip(values, weights):
        block = [v * w, w, 1]
        while stack and stack[-1][0] / stack[-1][1] > block[0] / block[1]:
            prev = stack.pop()
            block = [prev[0] + block[0], prev[1] + block[1], prev[2] + block[2]]
        stack.append(block)
    fitted = []
    for sum_wv, sum_w, cnt in stack:
        fitted.extend([sum_wv / sum_w] * cnt)
    return fitted


def fit_calibration(joined: list[dict], edges: list[float] = CAL_EDGES) -> dict:
    """Bucket every joined fire by stated fair, isotonic-fit the win rate.

    Returns {"table": [rows with n/wins/raw_rate/fit_rate per populated
    bucket], "lookup": {bucket_idx: calibrated_p}}. Empty buckets are
    dropped before PAVA so a thin tail doesn't distort neighboring bins.
    """
    n_buckets = len(edges) - 1
    n = [0] * n_buckets
    wins = [0] * n_buckets
    for f in joined:
        b = bucket_index(f["fair"], edges)
        n[b] += 1
        wins[b] += 1 if f["win"] else 0

    populated = [b for b in range(n_buckets) if n[b] > 0]
    raw_rates = [wins[b] / n[b] for b in populated]
    weights = [float(n[b]) for b in populated]
    fitted = pava(raw_rates, weights)

    lookup = {b: p for b, p in zip(populated, fitted)}
    table = [
        {
            "bucket": f"[{edges[b]:.3f},{edges[b+1]:.3f})",
            "n": n[b], "wins": wins[b],
            "raw_rate": raw_rates[i], "fit_rate": fitted[i],
        }
        for i, b in enumerate(populated)
    ]
    return {"table": table, "lookup": lookup, "edges": edges}


def calibrated_fair(fair: float, calibration: dict) -> float:
    b = bucket_index(fair, calibration["edges"])
    return calibration["lookup"].get(b, fair)  # unpopulated bucket -> no shrink available


# ---------------------------------------------------------------------------
# simulation: resize the same recorded fires under a policy, per-window cap
# ---------------------------------------------------------------------------

def max_drawdown(equity_curve: list[float]) -> float:
    """Peak-to-trough max drawdown (positive dollar number) over a
    chronological cumulative-P&L series."""
    peak = 0.0
    worst = 0.0
    running = 0.0
    for pnl in equity_curve:
        running += pnl
        peak = max(peak, running)
        worst = min(worst, running - peak)
    return -worst


def simulate_actual(joined: list[dict], fee_rate: float = DEFAULT_FEE_RATE) -> dict:
    """Policy (a): exactly what was traded — recorded size/ask, no resizing."""
    clips = []
    for f in joined:
        pnl = pnl_from_shares(f["size"], f["ask"], f["win"], fee_rate)
        clips.append({**f, "notional": f["size"] * f["ask"], "pnl": pnl})
    return _summarize(clips)


def simulate_kelly(
    joined: list[dict],
    fair_fn,
    bankroll: float = DEFAULT_BANKROLL,
    kelly_frac: float = DEFAULT_KELLY_FRAC,
    cap_frac: float = 0.15,
    fee_rate: float = DEFAULT_FEE_RATE,
) -> dict:
    """Policies (b)/(c): resize each fire via size_usdc(fair_fn(f), ...),
    tracking cumulative committed notional per window (slug) so the
    cap_frac*bankroll ceiling binds across a window's multiple clips, same
    as the live engine's room tracking."""
    committed_by_slug: dict[str, float] = defaultdict(float)
    clips = []
    for f in joined:
        slug = f["slug"]
        p_fair = fair_fn(f)
        notional = size_usdc(
            p_fair, f["ask"], fee_rate=fee_rate, bankroll=bankroll,
            kelly_frac=kelly_frac, cap_frac=cap_frac,
            committed_in_window=committed_by_slug[slug],
        )
        if notional < MIN_CLIP_USDC:
            continue
        committed_by_slug[slug] += notional
        pnl = pnl_from_notional(notional, f["ask"], f["win"], fee_rate)
        clips.append({**f, "sized_fair": p_fair, "notional": notional, "pnl": pnl})
    return _summarize(clips)


def _summarize(clips: list[dict]) -> dict:
    clips_by_t = sorted(clips, key=lambda c: c["t"])
    by_window: dict[str, float] = defaultdict(float)
    for c in clips_by_t:
        by_window[c["slug"]] += c["pnl"]
    # window-level equity curve, ordered by each window's first clip time
    window_first_t = {}
    for c in clips_by_t:
        window_first_t.setdefault(c["slug"], c["t"])
    window_order = sorted(by_window, key=lambda s: window_first_t[s])
    window_curve = [by_window[s] for s in window_order]

    total_pnl = sum(c["pnl"] for c in clips_by_t)
    total_notional = sum(c["notional"] for c in clips_by_t)
    largest_window_loss = -min(window_curve, default=0.0)
    largest_clip_loss = -min((c["pnl"] for c in clips_by_t), default=0.0)
    return {
        "clips": clips_by_t,
        "n_clips": len(clips_by_t),
        "n_windows": len(by_window),
        "total_pnl": total_pnl,
        "total_notional": total_notional,
        "max_drawdown": max_drawdown(window_curve),
        "largest_window_loss": largest_window_loss,
        "largest_clip_loss": largest_clip_loss,
        "by_window": dict(by_window),
    }


# ---------------------------------------------------------------------------
# report
# ---------------------------------------------------------------------------

def signature_scan(all_fires: list[dict], ask_thresh: float = 0.50, fair_thresh: float = 0.98) -> list[dict]:
    """Windows where the book crashed below ask_thresh on some clip while the
    model's fair stayed >= fair_thresh on some clip — the book-vs-model
    divergence fingerprint shared by all 3 confirmed losses in scope.
    Includes unresolved windows (status 'unresolved') so a live near-miss
    with the same signature isn't invisible just because it hasn't settled."""
    by_slug: dict[str, list[dict]] = defaultdict(list)
    for f in all_fires:
        by_slug[f["slug"]].append(f)
    out = []
    for slug, fs in by_slug.items():
        min_ask = min(f["ask"] for f in fs)
        max_fair = max(f["fair"] for f in fs)
        if min_ask < ask_thresh and max_fair >= fair_thresh:
            if "win" not in fs[0]:
                status = "unresolved"
            else:
                status = "WIN" if fs[0]["win"] else "LOSS"
            out.append({"slug": slug, "min_ask": min_ask, "max_fair": max_fair,
                        "n": len(fs), "status": status})
    out.sort(key=lambda r: r["slug"])
    return out


def fmt_money(x: float) -> str:
    return f"-${-x:,.2f}" if x < 0 else f"${x:,.2f}"


def print_report(
    joined: list[dict],
    unresolved: list[dict],
    calibration: dict,
    bankroll: float,
    kelly_frac: float,
    cap_fracs: tuple[float, ...],
    fee_rate: float,
) -> None:
    print("=" * 78)
    print("R2 quarter-Kelly sizing simulator")
    print("=" * 78)
    print(f"corpus: {TAPE_PATH}")
    print(f"outcomes: {OUTCOMES_PATH}")
    print(f"cutoff t >= {CUTOFF_T:.0f}  |  bankroll ${bankroll:,.0f}  "
          f"|  kelly_frac {kelly_frac}  |  fee_rate {fee_rate}")
    n_windows = len({f['slug'] for f in joined})
    print(f"fires: {len(joined)} joined to outcomes across {n_windows} windows, "
          f"{len(unresolved)} unresolved (excluded — corpus rule: dropped, never guessed)")
    if unresolved:
        u_slugs = sorted({f["slug"] for f in unresolved})
        print(f"  unresolved slugs: {', '.join(u_slugs)}")
    wins = sum(1 for f in joined if f["win"])
    print(f"raw hit rate over joined fires: {wins}/{len(joined)} = {wins/len(joined):.1%}")
    print()

    print("-" * 78)
    print("calibration map (stated-fair bucket -> isotonic-fit realized win rate)")
    print("-" * 78)
    print(f"{'bucket':>18} {'n':>5} {'wins':>5} {'raw rate':>10} {'fit rate':>10} {'shrink':>8}")
    for row in calibration["table"]:
        shrink = row["raw_rate"] - row["fit_rate"]
        # approximate bucket midpoint's stated fair for a "shrink" display
        print(f"{row['bucket']:>18} {row['n']:>5} {row['wins']:>5} "
              f"{row['raw_rate']:>10.3f} {row['fit_rate']:>10.3f} {shrink:>+8.3f}")
    print()

    losing_slugs = sorted({f["slug"] for f in joined if not f["win"]})
    print(f"losing windows in scope: {len(losing_slugs)}  ->  {', '.join(losing_slugs)}")
    print()

    print("-" * 78)
    print("book-disagrees-model signature scan (min ask < 0.50 while max fair >= 0.98)")
    print("-" * 78)
    print("  same fingerprint as all 3 confirmed losses: the book crashed toward the")
    print("  opposite side while the model stayed pinned near-certain. Flags any window")
    print("  matching it, resolved or not, so an unresolved near-miss isn't silently")
    print("  dropped from the picture.")
    sig = signature_scan(joined + unresolved)
    for row in sig:
        print(f"  {row['slug']:>32}  min_ask={row['min_ask']:.2f}  max_fair={row['max_fair']:.4f}"
              f"  n_clips={row['n']}  status={row['status']}")
    print()

    actual = simulate_actual(joined, fee_rate)
    print("-" * 78)
    print("policy (a) actual — flat clips as traded")
    print("-" * 78)
    _print_policy_block(actual)
    print()

    for cap_frac in cap_fracs:
        print("=" * 78)
        print(f"cap_frac = {cap_frac}   (per-window ceiling ${cap_frac*bankroll:,.2f})")
        print("=" * 78)

        raw = simulate_kelly(joined, lambda f: f["fair"], bankroll, kelly_frac, cap_frac, fee_rate)
        print(f"policy (b) kelly-raw — quarter-Kelly on stated fair")
        _print_policy_block(raw)
        print()

        cal = simulate_kelly(
            joined, lambda f: calibrated_fair(f["fair"], calibration),
            bankroll, kelly_frac, cap_frac, fee_rate,
        )
        print(f"policy (c) kelly-cal — quarter-Kelly on calibrated fair")
        _print_policy_block(cal)
        print()

        print(f"per-loss-window exposure — actual vs kelly-raw vs kelly-cal notional/pnl:")
        _print_loss_window_table(losing_slugs, actual, raw, cal)
        print()
        _print_verdict(cap_frac, losing_slugs, actual, raw, cal)
        print()


def _print_policy_block(result: dict) -> None:
    print(f"  clips fired:        {result['n_clips']}  across {result['n_windows']} windows")
    print(f"  total notional:     {fmt_money(result['total_notional'])}")
    print(f"  total P&L:          {fmt_money(result['total_pnl'])}")
    print(f"  max drawdown:       {fmt_money(-result['max_drawdown'])}")
    print(f"  largest window loss:{fmt_money(-result['largest_window_loss'])}")
    print(f"  largest clip loss:  {fmt_money(-result['largest_clip_loss'])}")


def _print_verdict(cap_frac: float, losing_slugs: list[str], actual: dict, raw: dict, cal: dict) -> None:
    """Per-loss-window verdict at this cap_frac, in LOSS MAGNITUDE (positive
    dollars, bigger = worse) so the two comparisons that matter read
    unambiguously: cal vs raw isolates calibration's effect at a fixed cap;
    cal vs actual is the net effect vs what really happened that night
    (which used a much smaller flat clip_usdc than any cap_frac here sizes
    up to — see the module docstring's per-clip-vs-per-window cap caveat).
    Derived fresh from the sim results each call — never hardcoded."""
    print(f"  verdict at cap_frac={cap_frac}  (loss magnitude, bigger = worse):")
    for slug in losing_slugs:
        a_loss = -actual["by_window"].get(slug, 0.0)
        r_loss = -raw["by_window"].get(slug, 0.0)
        c_loss = -cal["by_window"].get(slug, 0.0)
        vs_raw = c_loss - r_loss      # calibration's effect at the same cap
        vs_actual = c_loss - a_loss   # net effect vs what really happened
        print(f"    {slug}:")
        print(f"      actual {a_loss:>8.2f}  raw {r_loss:>8.2f}  cal {c_loss:>8.2f}   "
              f"cal-vs-raw {vs_raw:>+8.2f}  cal-vs-actual {vs_actual:>+8.2f}")


def _print_loss_window_table(losing_slugs: list[str], actual: dict, raw: dict, cal: dict) -> None:
    def window_notional(result: dict, slug: str) -> float:
        return sum(c["notional"] for c in result["clips"] if c["slug"] == slug)

    print(f"  {'slug':>32} {'actual $':>10} {'kelly-raw $':>12} {'kelly-cal $':>12} "
          f"{'actual pnl':>11} {'raw pnl':>10} {'cal pnl':>10}")
    for slug in losing_slugs:
        a_n, a_p = window_notional(actual, slug), actual["by_window"].get(slug, 0.0)
        r_n, r_p = window_notional(raw, slug), raw["by_window"].get(slug, 0.0)
        c_n, c_p = window_notional(cal, slug), cal["by_window"].get(slug, 0.0)
        print(f"  {slug:>32} {a_n:>10.2f} {r_n:>12.2f} {c_n:>12.2f} "
              f"{a_p:>11.2f} {r_p:>10.2f} {c_p:>10.2f}")


# ---------------------------------------------------------------------------

def main() -> None:
    _selftest()
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--bankroll", type=float, default=DEFAULT_BANKROLL)
    ap.add_argument("--kelly-frac", type=float, default=DEFAULT_KELLY_FRAC)
    ap.add_argument("--fee-rate", type=float, default=DEFAULT_FEE_RATE)
    ap.add_argument("--cutoff", type=float, default=CUTOFF_T)
    ap.add_argument("--cap-fracs", type=str, default=",".join(str(x) for x in DEFAULT_CAP_FRACS))
    args = ap.parse_args()
    cap_fracs = tuple(float(x) for x in args.cap_fracs.split(","))

    outcomes = load_outcomes()
    fires = load_fires(cutoff_t=args.cutoff)
    joined, unresolved = join_fires(fires, outcomes)
    if not joined:
        print("no joined fires in scope — nothing to simulate")
        return
    calibration = fit_calibration(joined)
    print_report(joined, unresolved, calibration, args.bankroll, args.kelly_frac,
                 cap_fracs, args.fee_rate)


if __name__ == "__main__":
    main()
