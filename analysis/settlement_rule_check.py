#!/usr/bin/env python3
"""Which settlement rule do the up/down markets ACTUALLY use? (2026-08-23)

Commissioned by the 4h tier fit (analysis/fourh_fit.md). The 4h replay said
84% of windows become "banked-decided" and win ~100% of the time — while the
live 4h book priced those same windows at 0.75-0.95. When our model and a real
market disagree that violently, one of them is wrong. This settles it against
ground truth (gamma's resolved outcomePrices) instead of arguing.

Candidate rules for "the TWAP of the time range vs the price at the beginning
of that range" (the description is genuinely ambiguous):

  avg_vs_prevmin   mean of the range's minute marks vs the mark of the minute
                   BEFORE the range     <- what pmengine's eval_model computes
  last_vs_prevmin  the FINAL minute's mark vs the mark of the minute before
                   the range            <- the Chainlink 60s-TWAP stream read
                                           at range end vs at range start
  avg_vs_firstmin / last_vs_firstmin / avg_vs_prevavg   other readings

The decisive statistic is the head-to-head on the SUBSET WHERE THE TWO RULES
DISAGREE: whichever rule matches resolution there is the one the market uses.

Read-only: gamma reads (batched, 20 slugs per request) + the local kline
corpus. Never touches the engine.

Run: cd pmtrader && uv run python ../analysis/settlement_rule_check.py
     ... --durations 4h --back 60
     ... --symbols BTCUSDT ETHUSDT --durations 5m 15m --back 300
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "pmtrader"))
import fourh_fit as ff  # noqa: E402
from polymarket import hosts  # noqa: E402

SHORT = {"BTCUSDT": "btc", "ETHUSDT": "eth", "SOLUSDT": "sol",
         "XRPUSDT": "xrp", "DOGEUSDT": "doge", "BNBUSDT": "bnb"}
DURS = {"5m": 300, "15m": 900, "4h": 14400}
BATCH = 20
TIMEOUT = 25


def resolutions(slugs: list[str]) -> dict[str, str]:
    """slug -> 'up'/'down' for every RESOLVED slug in the list (gamma batches
    up to BATCH slugs per request; unresolved and unknown slugs are omitted)."""
    out: dict[str, str] = {}
    for i in range(0, len(slugs), BATCH):
        chunk = slugs[i:i + BATCH]
        try:
            r = requests.get(f"{hosts.GAMMA}/events",
                             params=[("slug", s) for s in chunk],
                             headers=hosts.UA, timeout=TIMEOUT)
            if r.status_code != 200:
                continue
            events = r.json() or []
        except Exception:  # noqa: BLE001 — public host hiccups are routine
            time.sleep(0.5)
            continue
        for ev in events:
            mkts = [m for m in (ev.get("markets") or []) if not m.get("archived")]
            if not mkts:
                continue
            m = mkts[0]
            try:
                outs = json.loads(m.get("outcomes") or "[]")
                prices = [float(x) for x in json.loads(m.get("outcomePrices") or "[]")]
            except (TypeError, ValueError):
                continue
            if len(outs) != 2 or len(prices) != 2 or max(prices) < 0.99:
                continue      # outcomePrices only pin to 1/0 once UMA settles
            out[ev.get("slug")] = str(outs[prices.index(max(prices))]).lower()
        time.sleep(0.1)
    return out


def rules(series: dict, start: int, end: int) -> dict[str, bool] | None:
    """Each candidate rule's verdict (True = 'up') for one window."""
    idx = series["t_to_idx"]
    marks = series["marks"]
    if (start - 60) not in idx or (end - 60) not in idx or start not in idx:
        return None
    ref_prev, ref_first = marks[idx[start - 60]], marks[idx[start]]
    last = marks[idx[end - 60]]
    _c, avg = ff.r6.range_avg(series, start, end)
    _c2, prev_avg = ff.r6.range_avg(series, start - (end - start), start)
    if avg is None:
        return None
    return {
        "avg_vs_prevmin": avg >= ref_prev,          # <- pmengine eval_model
        "avg_vs_firstmin": avg >= ref_first,
        "last_vs_prevmin": last >= ref_prev,        # <- pmtrader outcomes.py
        "last_vs_firstmin": last >= ref_first,
        "avg_vs_prevavg": prev_avg is not None and avg >= prev_avg,
    }, ((avg / ref_prev - 1.0) * 1e4, (last / ref_prev - 1.0) * 1e4)


def run(symbols: list[str], dur_label: str, back: int, theta: float) -> None:
    dur = DURS[dur_label]
    now = int(time.time())
    names = ["avg_vs_prevmin", "avg_vs_firstmin", "last_vs_prevmin",
             "last_vs_firstmin", "avg_vs_prevavg"]
    score = dict.fromkeys(names, 0)
    n = 0
    # head-to-head on the disagreement subset
    h2h = {"n": 0, "avg": 0, "last": 0, "avg_margins": [], "last_margins": []}
    # engine-gate impact: model side at first fire vs the resolution
    gate = {"n": 0, "wrong": 0}
    for sym in symbols:
        series = ff.r6.build_series(ff.r6.load_cache(sym))
        if not series["ts"]:
            continue
        starts = [(now // dur) * dur - k * dur for k in range(1, back + 1)]
        starts = [s for s in starts if ff.r6.window_ready(series, s, s + dur)]
        res = resolutions([f"{SHORT[sym]}-updown-{dur_label}-{s}" for s in starts])
        for s in starts:
            slug = f"{SHORT[sym]}-updown-{dur_label}-{s}"
            if slug not in res:
                continue
            got = rules(series, s, s + dur)
            if got is None:
                continue
            verdicts, (m_avg, m_last) = got
            up = res[slug] == "up"
            n += 1
            for name in names:
                score[name] += 1 if verdicts[name] == up else 0
            if verdicts["avg_vs_prevmin"] != verdicts["last_vs_prevmin"]:
                h2h["n"] += 1
                h2h["avg"] += 1 if verdicts["avg_vs_prevmin"] == up else 0
                h2h["last"] += 1 if verdicts["last_vs_prevmin"] == up else 0
                h2h["avg_margins"].append(abs(m_avg))
                h2h["last_margins"].append(abs(m_last))
            # what the live entry gate would have bought
            t = s + 60
            while t <= s + dur - 15:
                st = ff.tick_state(series, ff.GUARD_BP[sym], s, s + dur, t)
                if st is not None and st["gate_ok"] and st["safety"] >= theta:
                    gate["n"] += 1
                    gate["wrong"] += 0 if (st["side"] == res[slug]) else 1
                    break
                t += 15
    print(f"\n=== {dur_label}  ({', '.join(SHORT[s] for s in symbols)}; "
          f"{n} resolved windows with corpus coverage) ===")
    for name in sorted(names, key=lambda k: -score[k]):
        tag = "   <- pmengine eval_model" if name == "avg_vs_prevmin" else (
              "   <- pmtrader outcomes.py" if name == "last_vs_prevmin" else "")
        print(f"  {name:<18s} {score[name]:>5d}/{n}  {100.0 * score[name] / max(n, 1):6.2f}%{tag}")
    if h2h["n"]:
        print(f"  HEAD-TO-HEAD on the {h2h['n']} windows where the two rules DISAGREE:")
        print(f"     range-average rule correct: {h2h['avg']:>4d}  "
              f"({100.0 * h2h['avg'] / h2h['n']:.1f}%)   "
              f"median |margin| {ff.pct(h2h['avg_margins'], .5):.1f}bp")
        print(f"     terminal-TWAP rule correct: {h2h['last']:>4d}  "
              f"({100.0 * h2h['last'] / h2h['n']:.1f}%)   "
              f"median |margin| {ff.pct(h2h['last_margins'], .5):.1f}bp")
    else:
        print("  (the two rules never disagreed in this sample)")
    if gate["n"]:
        print(f"  ENGINE GATE (theta={theta}): {gate['n']} windows would have taken a first clip; "
              f"the side it would have bought lost {gate['wrong']} of them "
              f"({100.0 * gate['wrong'] / gate['n']:.1f}%)")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--symbols", nargs="+", default=list(SHORT))
    ap.add_argument("--durations", nargs="+", default=["4h", "15m", "5m"])
    ap.add_argument("--back", type=int, default=120, help="windows back per symbol")
    ap.add_argument("--theta", type=float, default=0.3, help="entry gate for the impact line")
    args = ap.parse_args()
    print("Settlement-rule check — candidate rules vs gamma's resolved outcomePrices")
    for d in args.durations:
        run(args.symbols, d, args.back, args.theta)
    print("\nThe head-to-head line is the decisive one: on windows where the two readings of the "
          "description disagree, only one of them can match resolution.")


if __name__ == "__main__":
    main()
