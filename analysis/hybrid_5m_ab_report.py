"""Aggregate the four replay reports hybrid_5m_ab.py produces.

Only windows carrying a truth row are scored — an ungraded window's "pnl" is
the TWAP proxy grading itself, which is exactly the read this whole exercise
refuses. The aggregate row the replay emits is dropped for the same reason.
"""

from __future__ import annotations

import json
import os
import sys
from collections import defaultdict
from pathlib import Path

WORK = Path(os.environ.get("AB_WORK", "/tmp/claude-1000/-var-home-hunter/"
                           "35f80f35-e0c9-4e4d-80ea-9c5602f70444/scratchpad/ab"))
INCIDENT = 1787505300


def load(name, rule):
    truth = {}
    for line in (WORK / f"truth-{name}.jsonl").read_text().splitlines():
        if line.strip():
            r = json.loads(line)
            truth[r["slug"]] = r["winner"]
    rows = []
    for line in (WORK / f"report-{name}-{rule}.jsonl").read_text().splitlines():
        r = json.loads(line)
        if "fleet" not in r or r["slug"] not in truth:
            continue
        rows.append(r)
    return rows, truth


def tally(rows):
    t = {"fired": 0, "clips": 0, "notional": 0.0, "pnl": 0.0, "w": 0, "l": 0,
         "fees": 0.0, "n": len(rows)}
    for r in rows:
        s = r["sim"]
        if s["fires"] == 0:
            continue
        t["fired"] += 1
        t["clips"] += s["fires"]
        t["notional"] += s["notional"]
        t["pnl"] += s["pnl"]
        t["fees"] += s["fees"]
        if s["pnl"] > 0:
            t["w"] += 1
        elif s["pnl"] < 0:
            t["l"] += 1
    return t


def line(label, t):
    return (f"{label:<22} n={t['n']:<4} fired={t['fired']:<4} clips={t['clips']:<5} "
            f"notional=${t['notional']:>10,.0f}  pnl=${t['pnl']:>9,.2f}  "
            f"W-L={t['w']}-{t['l']}")


if __name__ == "__main__":
    for name in ("wallet", "walbook"):
        print(f"\n{'=' * 78}\n== truth set: {name}\n{'=' * 78}")
        data = {rule: load(name, rule) for rule in ("range_avg", "hybrid")}
        for rule in ("range_avg", "hybrid"):
            print(line(f"FLEET {rule}", tally(data[rule][0])))
        print()
        for rule in ("range_avg", "hybrid"):
            per = defaultdict(list)
            for r in data[rule][0]:
                per[r["slug"].split("-")[0]].append(r)
            for sym in sorted(per):
                print(line(f"  {sym} {rule}", tally(per[sym])))
            print()

    # The incident, window by window.
    print(f"\n{'=' * 78}\n== the five-arm event, epoch {INCIDENT} (17:15:00Z)\n{'=' * 78}")
    for rule in ("range_avg", "hybrid"):
        rows, truth = load("walbook", rule)
        inc = [r for r in rows if r["slug"].endswith(str(INCIDENT))]
        tot = sum(r["sim"]["pnl"] for r in inc)
        print(f"\n-- {rule}: {len(inc)} arms, net ${tot:,.2f}")
        for r in sorted(inc, key=lambda r: r["slug"]):
            s = r["sim"]
            print(f"   {r['slug']:<32} truth={truth[r['slug']]:<5} clips={s['fires']:<3} "
                  f"notional=${s['notional']:>8,.2f}  pnl=${s['pnl']:>8,.2f}  "
                  f"real_fires={r['real']['fires']}")
