"""Q2 — is the structurally-idle first half of a window informative or noise?

Three questions the operator asked:
  a) spread width and depth, first half vs second half
  b) how often the early book is two-sided at all
  c) does the early mid predict the winner — if it's a coin flip, early takers
     are donating and a maker collects; if it's predictive, quantify how much.

Outcome truth: the wallet-first validated corpus where it reaches, else the
window's own terminal book (a settled updown pair quotes 0.999/0.001). The
two are cross-checked on every window where both exist and the agreement
rate is printed — if that isn't ~100%, nothing below is trustworthy.

Run: uv run python analysis/firsthalf_q2_bookstruct.py
"""

from __future__ import annotations

import sys
from collections import Counter, defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from firsthalf_lib import (  # noqa: E402
    load_book_windows,
    load_outcomes,
    mid_up,
    parse_slug,
    pct,
    sample_at,
    terminal_winner,
    wilson,
)


def spread(r: dict, tok: str) -> float | None:
    b, a = r.get(f"{tok}_bid"), r.get(f"{tok}_ask")
    return None if (b is None or a is None) else a - b


def build_truth(wins: dict[str, list[dict]]) -> tuple[dict[str, str], dict]:
    validated = load_outcomes()
    truth: dict[str, str] = {}
    agree = disagree = 0
    inferred = 0
    for slug, rows in wins.items():
        v = validated.get(slug)
        t = terminal_winner(rows)
        if v and t:
            if v == t:
                agree += 1
            else:
                disagree += 1
        if v:
            truth[slug] = v
        elif t:
            truth[slug] = t
            inferred += 1
    return truth, {
        "validated": sum(1 for s in wins if s in validated),
        "inferred": inferred,
        "agree": agree,
        "disagree": disagree,
    }


def main() -> None:
    wins = load_book_windows()
    truth, meta = build_truth(wins)

    print("=" * 74)
    print("Q2  EARLY-BOOK STRUCTURE")
    print("=" * 74)
    print(f"windows in book corpus     : {len(wins)}")
    print(f"outcome from wallet/chainlink: {meta['validated']}   inferred from terminal book: {meta['inferred']}")
    print(f"cross-check where both exist : {meta['agree']} agree / {meta['disagree']} disagree")
    print(f"windows with usable outcome  : {len(truth)}")
    print()

    # ---- (a) spread + depth by half -------------------------------------
    halves: dict[str, dict[str, list]] = {
        h: {"spread": [], "bidsz": [], "pairbid": [], "pairask": []} for h in ("first", "second")
    }
    for rows in wins.values():
        for r in rows:
            h = "first" if r["frac"] < 0.5 else "second"
            for tok in ("up", "dn"):
                s = spread(r, tok)
                if s is not None:
                    halves[h]["spread"].append(s)
                sz = r.get(f"{tok}_bid_sz")
                if sz is not None:
                    halves[h]["bidsz"].append(sz)
            ub, db = r.get("up_bid"), r.get("dn_bid")
            ua, da = r.get("up_ask"), r.get("dn_ask")
            if ub is not None and db is not None:
                halves[h]["pairbid"].append(ub + db)
            if ua is not None and da is not None:
                halves[h]["pairask"].append(ua + da)

    print("--- (a) spread / depth by window half ---")
    print(f"{'metric':<28} {'first half':>22} {'second half':>22}")
    for label, key, scale in (
        ("top-of-book spread (c) p50", "spread", 100),
        ("top-of-book spread (c) p90", "spread", 100),
        ("best-bid size (shares) p50", "bidsz", 1),
        ("best-bid size (shares) p90", "bidsz", 1),
        ("pair bid-sum p50", "pairbid", 1),
        ("pair ask-sum p50", "pairask", 1),
    ):
        q = 0.9 if "p90" in label else 0.5
        a = scale * pct(halves["first"][key], q)
        b = scale * pct(halves["second"][key], q)
        print(f"{label:<28} {a:>22.3f} {b:>22.3f}")
    print()

    # ---- (b) two-sidedness over window life ------------------------------
    print("--- (b) book completeness by decile of window life ---")
    dec: dict[int, Counter] = defaultdict(Counter)
    for rows in wins.values():
        for r in rows:
            d = min(9, int(r["frac"] * 10))
            dec[d]["n"] += 1
            up_ok = r.get("up_bid") is not None and r.get("up_ask") is not None
            dn_ok = r.get("dn_bid") is not None and r.get("dn_ask") is not None
            if up_ok and dn_ok:
                dec[d]["both2s"] += 1
            elif up_ok or dn_ok:
                dec[d]["one2s"] += 1
            else:
                dec[d]["none"] += 1
    print(f"{'decile':<8} {'n':>7} {'both 2-sided':>14} {'one 2-sided':>13} {'neither':>9}")
    for d in range(10):
        c = dec[d]
        n = max(c["n"], 1)
        print(
            f"{d/10:.1f}-{(d+1)/10:.1f} {c['n']:>7} {100*c['both2s']/n:>13.1f}% "
            f"{100*c['one2s']/n:>12.1f}% {100*c['none']/n:>8.1f}%"
        )
    print()

    # ---- (c) is the early mid predictive? --------------------------------
    print("--- (c) early mid vs realized winner ---")
    for label, frac in (("2min-in (5m only)", None), ("frac 0.15", 0.15), ("frac 0.25", 0.25),
                        ("frac 0.40", 0.40), ("frac 0.50", 0.50), ("frac 0.75", 0.75)):
        rows_used = []
        for slug, rows in wins.items():
            if slug not in truth:
                continue
            w = parse_slug(slug)
            if frac is None:
                if w["dur_s"] != 300:
                    continue
                f = 120.0 / w["dur_s"]
            else:
                f = frac
            r = sample_at(rows, f)
            if r is None:
                continue
            m = mid_up(r)
            if m is None:
                continue
            rows_used.append((m, truth[slug] == "up"))
        report_bucket(label, rows_used)
    print()

    # Per-duration split at the midpoint — 5m and 15m bank evidence at
    # different rates, so their "half way" is not the same information state.
    print("--- (c2) frac 0.25 split by duration ---")
    for dur in (300, 900):
        rows_used = []
        for slug, rows in wins.items():
            if slug not in truth or parse_slug(slug)["dur_s"] != dur:
                continue
            r = sample_at(rows, 0.25)
            if r is None:
                continue
            m = mid_up(r)
            if m is not None:
                rows_used.append((m, truth[slug] == "up"))
        report_bucket(f"{dur//60}m windows", rows_used)


def report_bucket(label: str, rows: list[tuple[float, bool]]) -> None:
    if not rows:
        print(f"  {label:<20} (no data)")
        return
    conf = [(m, up) for m, up in rows if m > 0.60 or m < 0.40]
    k = sum(1 for m, up in conf if (up if m > 0.5 else not up))
    n = len(conf)
    lo, hi = wilson(k, n)
    # Brier-style: does the mid beat a flat 0.5 prior on the whole sample?
    br_mid = sum((m - (1.0 if up else 0.0)) ** 2 for m, up in rows) / len(rows)
    br_half = sum((0.5 - (1.0 if up else 0.0)) ** 2 for m, up in rows) / len(rows)
    print(
        f"  {label:<20} n_all={len(rows):>3}  |mid-0.5|>0.10: n={n:>3} "
        f"favoured side won {k}/{n}"
        + (f" = {100*k/n:5.1f}% (95% CI {100*lo:.0f}-{100*hi:.0f}%)" if n else "")
        + f"   Brier mid={br_mid:.4f} vs flat-0.5={br_half:.4f}"
    )


if __name__ == "__main__":
    main()
