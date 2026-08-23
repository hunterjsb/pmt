"""Q4 — does first-half taker print flow predict the winner? (VPIN-lite, R8's input)

Runs on the backfilled print corpus, not the engine's flow fields (those
recorded ~5% of prints; see firsthalf_harvest_prints.py). Because UP and DOWN
are two mirrored views of ONE book (proved in the study: 76/76 live
simultaneous snapshots satisfy up_ask == 1 - dn_bid exactly), flow has to be
folded onto a single axis before it means anything:

    up-pressure = buy(UP) + sell(DOWN) - sell(UP) - buy(DOWN)

A taker buying DOWN is a taker selling UP; counting the two separately (as a
raw per-token tally would) cancels the signal.

Two questions, both answered on the first half only:
  1. Raw predictiveness: does early flow imbalance call the winner?
  2. INCREMENTAL predictiveness: does it add anything over the early mid, which
     Q2 showed is already calibrated? Only the increment is a legal new input
     to R5/R8 sizing — anything already in the price is not new information.

Run: uv run python analysis/firsthalf_q4_flow.py
"""

from __future__ import annotations

import bisect
import sys
from collections import Counter, defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from firsthalf_lib import load_book_windows, mid_up, parse_slug, sample_at, wilson  # noqa: E402
from firsthalf_q2_bookstruct import build_truth  # noqa: E402
from firsthalf_q3_maker import load_prints  # noqa: E402


def flow_features(prints: list[dict], w: dict, lo: float, hi: float) -> dict | None:
    """Signed up-pressure over [lo, hi] of window life, folded to one axis."""
    up_press = 0.0
    tot = 0.0
    n = 0
    for p in prints:
        f = (p["t"] - w["start"]) / w["dur_s"]
        if f < lo or f > hi:
            continue
        sz = p["size"]
        tot += sz
        n += 1
        sign = 1.0 if p["outcome"] == "up" else -1.0
        if p["side"] == "sell":
            sign = -sign
        up_press += sign * sz
    if tot <= 0:
        return None
    return {"imb": up_press / tot, "vol": tot, "n": n}


def report(label: str, rows: list[tuple[float, bool]], thresh: float) -> None:
    conf = [(x, up) for x, up in rows if abs(x) >= thresh]
    k = sum(1 for x, up in conf if (up if x > 0 else not up))
    n = len(conf)
    if n == 0:
        print(f"  {label:<26} n=0")
        return
    lo, hi = wilson(k, n)
    print(
        f"  {label:<26} n={n:>4}/{len(rows):<4} flow-side won {k:>3}/{n:<4} = "
        f"{100*k/n:5.1f}%  (95% CI {100*lo:4.0f}-{100*hi:<4.0f}%)"
    )


def main() -> None:
    wins = load_book_windows()
    truth, _ = build_truth(wins)
    prints = load_prints()
    slugs = [s for s in wins if s in prints and s in truth]

    print("=" * 78)
    print("Q4  EARLY PRINT FLOW AS A PREDICTOR (VPIN-lite)")
    print("=" * 78)
    print(f"windows with book + prints + outcome: {len(slugs)}")
    tot_prints = sum(len(prints[s]) for s in slugs)
    print(f"prints in corpus: {tot_prints:,} (engine's own flow fields recorded ~656 in the same span)")
    print()

    print("--- 1. raw: does signed first-half flow call the winner? ---")
    for lo, hi, lab in ((0.0, 0.25, "flow over frac 0.00-0.25"),
                        (0.0, 0.50, "flow over frac 0.00-0.50"),
                        (0.25, 0.50, "flow over frac 0.25-0.50")):
        rows = []
        for slug in slugs:
            w = parse_slug(slug)
            f = flow_features(prints[slug], w, lo, hi)
            if f is None:
                continue
            rows.append((f["imb"], truth[slug] == "up"))
        for th in (0.0, 0.2, 0.5):
            report(f"{lab} |imb|>={th:.1f}", rows, th)
        print()

    print("--- 2. incremental over the early mid (the only part that is NEW info) ---")
    print("    residual = flow imbalance, bucketed by whether it AGREES with the mid")
    for frac in (0.25, 0.50):
        agree = Counter()
        disagree = Counter()
        for slug in slugs:
            w = parse_slug(slug)
            r = sample_at(wins[slug], frac)
            if r is None:
                continue
            m = mid_up(r)
            f = flow_features(prints[slug], w, 0.0, frac)
            if m is None or f is None:
                continue
            if abs(m - 0.5) < 0.05 or abs(f["imb"]) < 0.1:
                continue
            mid_up_side = m > 0.5
            flow_up_side = f["imb"] > 0
            won_up = truth[slug] == "up"
            box = agree if mid_up_side == flow_up_side else disagree
            box["n"] += 1
            box["mid_right"] += 1 if (won_up == mid_up_side) else 0
        for name, box in (("flow AGREES with mid", agree), ("flow DISAGREES with mid", disagree)):
            n, k = box["n"], box["mid_right"]
            if n == 0:
                print(f"  frac {frac:.2f}  {name:<24} n=0")
                continue
            lo_, hi_ = wilson(k, n)
            print(
                f"  frac {frac:.2f}  {name:<24} n={n:>4}  mid's side won {k:>3}/{n:<4} = "
                f"{100*k/n:5.1f}%  (95% CI {100*lo_:4.0f}-{100*hi_:<4.0f}%)"
            )
        print()

    print("--- 3. is taker BUY flow informed? (the maker's adverse-selection tax) ---")
    print("    for every first-half print, compare its price to the eventual payoff")
    tax: dict[str, Counter] = defaultdict(Counter)
    for slug in slugs:
        w = parse_slug(slug)
        won_up = truth[slug] == "up"
        rows_b = wins[slug]
        ts = [r["t"] for r in rows_b]
        for p in prints[slug]:
            f = (p["t"] - w["start"]) / w["dur_s"]
            if f < 0 or f > 0.5:
                continue
            i = bisect.bisect_right(ts, p["t"]) - 1
            if i < 0:
                continue
            m = mid_up(rows_b[i])
            if m is None:
                continue
            tok_won = won_up if p["outcome"] == "up" else (not won_up)
            payoff = 1.0 if tok_won else 0.0
            # taker's own P&L per share, ignoring fees, vs the mid at the time
            edge = (payoff - p["price"]) if p["side"] == "buy" else (p["price"] - payoff)
            mid_tok = m if p["outcome"] == "up" else 1.0 - m
            vs_mid = (payoff - mid_tok) if p["side"] == "buy" else (mid_tok - payoff)
            k = p["side"]
            tax[k]["sz"] += p["size"]
            tax[k]["edge"] += edge * p["size"]
            tax[k]["vsmid"] += vs_mid * p["size"]
            tax[k]["n"] += 1
    print(f"{'taker side':<12} {'prints':>8} {'shares':>12} {'taker edge/share':>18} {'vs mid/share':>14}")
    for k in ("buy", "sell"):
        c = tax[k]
        if not c["sz"]:
            continue
        print(
            f"{k:<12} {c['n']:>8} {c['sz']:>12,.0f} {100*c['edge']/c['sz']:>17.2f}c "
            f"{100*c['vsmid']/c['sz']:>13.2f}c"
        )
    print()
    print("    'vs mid/share' IS the maker's adverse-selection cost per share, with the")
    print("    sign flipped: the maker takes the other side of every one of these prints.")


if __name__ == "__main__":
    main()
