#!/usr/bin/env python3
"""Roll the feed A/B replay reports up into the tables analysis/feed_ab.md prints.

  uv run --project pmtrader python analysis/feed_ab_report.py --work <dir>
"""

import argparse
import json
import pathlib
import datetime
from collections import defaultdict

SYMS = ["btc", "eth", "sol", "bnb", "xrp"]
VARIANTS = ["base", "rtds_tw30", "rtds_liveguard", "rtds_streamguard", "rtds_floorguard"]
WARMUP_S = 7200.0


def z(ts):
    return datetime.datetime.fromtimestamp(ts, datetime.UTC).strftime("%H:%M")


def load(path):
    """slug -> report, aggregate excluded."""
    out = {}
    if not path.exists():
        return out
    for line in open(path):
        d = json.loads(line)
        if "aggregate" in d["slug"]:
            continue
        out[d["slug"]] = d
    return out


def tally(reports, slugs=None):
    t = {"windows": 0, "fired": 0, "clips": 0, "w": 0, "ls": 0, "flat": 0,
         "net": 0.0, "notional": 0.0, "fees": 0.0, "peak": 0.0}
    for slug, r in reports.items():
        if slugs is not None and slug not in slugs:
            continue
        s = r["sim"]
        t["windows"] += 1
        t["peak"] = max(t["peak"], s.get("max_committed") or 0.0)
        if not s["fires"]:
            continue
        t["fired"] += 1
        t["clips"] += s["fires"]
        t["notional"] += s["notional"]
        t["fees"] += s["fees"]
        pnl = s.get("pnl")
        if pnl is None:
            t["flat"] += 1
            continue
        t["net"] += pnl
        if pnl > 1e-9:
            t["w"] += 1
        elif pnl < -1e-9:
            t["ls"] += 1
        else:
            t["flat"] += 1
    t["ron"] = t["net"] / t["notional"] * 100.0 if t["notional"] else float("nan")
    return t


def row(name, t, base=None):
    s = (f"| {name} | {t['fired']} | {t['clips']} | {t['w']}-{t['ls']} | "
         f"{t['net']:+.2f} | {t['notional']:.2f} | {t['ron']:+.2f}% |")
    if base is not None:
        s += (f" {t['net'] - base['net']:+.2f} | "
              f"{t['notional'] - base['notional']:+.2f} | "
              f"{t['ron'] - base['ron']:+.2f}pp |")
    return s


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--work", required=True)
    ap.add_argument("--outcomes", default=str(pathlib.Path.home() / ".pmt/corpus/outcomes.jsonl"))
    args = ap.parse_args()
    work = pathlib.Path(args.work)
    meta = json.load(open(work / "meta.json"))
    corpus_start = meta["corpus_span"][0]
    warm_from = corpus_start + WARMUP_S

    data = {}
    for sym in SYMS + ["fleet"]:
        for v in VARIANTS:
            data[(sym, v)] = load(work / f"out-{sym}-{v}.jsonl")

    print("## Per-symbol (comparable window set)\n")
    hdr = ("| variant | windows fired | clips | W-L | net $ | notional $ | RoN |"
           " Δnet | Δnotional | ΔRoN |")
    sep = "|---|---|---|---|---|---|---|---|---|---|"
    for sym in SYMS:
        base = tally(data[(sym, "base")])
        print(f"\n### {sym} 5m — {base['windows']} comparable window(s)\n")
        print(hdr)
        print(sep)
        print(row("binance (baseline)", base) + "  |  |  |")
        for v in VARIANTS[1:]:
            print(row(v, tally(data[(sym, v)]), base))

    print("\n## Fleet (one shared $500 cap, all five symbols interleaved)\n")
    fb = tally(data[("fleet", "base")])
    print(hdr)
    print(sep)
    print(row(f"binance (baseline, {fb['windows']} win)", fb) + "  |  |  |")
    for v in VARIANTS[1:]:
        print(row(v, tally(data[("fleet", v)]), fb))

    print("\n## Warm-history subset (windows with the full 2h RTDS warmup)\n")
    print(hdr)
    print(sep)
    for sym in SYMS:
        warm = {s for s in data[(sym, "base")]
                if float(s.rsplit("-", 1)[1]) >= warm_from}
        b = tally(data[(sym, "base")], warm)
        print(row(f"{sym} binance ({len(warm)} win)", b) + "  |  |  |")
        for v in VARIANTS[1:]:
            print(row(f"{sym} {v}", tally(data[(sym, v)], warm), b))

    print("\n## Windows a recorder gap overlaps\n")
    for sym in SYMS:
        gh = meta["gap_hit"].get(sym, [])
        hit = {g[0] for g in gh}
        b = tally(data[(sym, "base")], hit)
        rl = tally(data[(sym, "rtds_liveguard")], hit)
        print(f"- **{sym}**: {len(hit)}/{len(data[(sym,'base')])} window(s) touched by a "
              f"recorder gap. binance fired {b['fired']} ({b['net']:+.2f}), "
              f"rtds fired {rl['fired']} ({rl['net']:+.2f}).")

    print("\n## The 17:15Z window (1787505300)\n")
    print("| symbol | truth | variant | clips | notional $ | pnl $ |")
    print("|---|---|---|---|---|---|")
    outcomes = {}
    for line in open(args.outcomes):
        d = json.loads(line)
        outcomes[d["slug"]] = d["winner"]
    for sym in SYMS:
        slug = f"{sym}-updown-5m-1787505300"
        for v in VARIANTS:
            r = data[(sym, v)].get(slug)
            if not r:
                print(f"| {sym} | {outcomes.get(slug,'?')} | {v} | (not replayed) | | |")
                continue
            s = r["sim"]
            pnl = s.get("pnl")
            print(f"| {sym} | {outcomes.get(slug,'?')} | {v} | {s['fires']} | "
                  f"{s['notional']:.2f} | {0.0 if pnl is None else pnl:+.2f} |")

    print("\n## Biggest per-window movers (rtds_streamguard vs binance)\n")
    print("| window | UTC | truth | binance clips/pnl | rtds clips/pnl | Δpnl |")
    print("|---|---|---|---|---|---|")
    movers = []
    for sym in SYMS:
        for slug, rb in data[(sym, "base")].items():
            rv = data[(sym, "rtds_streamguard")].get(slug)
            if not rv:
                continue
            pb = rb["sim"].get("pnl") or 0.0
            pv = rv["sim"].get("pnl") or 0.0
            if abs(pv - pb) > 1e-6:
                movers.append((pv - pb, slug, rb, rv))
    movers.sort(key=lambda m: -abs(m[0]))
    for d, slug, rb, rv in movers[:20]:
        t = float(slug.rsplit("-", 1)[1])
        print(f"| `{slug}` | {z(t)} | {outcomes.get(slug,'?')} | "
              f"{rb['sim']['fires']} / {rb['sim'].get('pnl') or 0.0:+.2f} | "
              f"{rv['sim']['fires']} / {rv['sim'].get('pnl') or 0.0:+.2f} | {d:+.2f} |")

    print("\n## Every losing window, by variant\n")
    print("The fleet wins ~95% of the windows it enters, so the P&L is decided "
          "by the few it loses. This is that list.\n")
    print("| variant | losing windows | total loss $ | winning windows | total win $ |")
    print("|---|---|---|---|---|")
    for v in VARIANTS:
        losers, winners = [], []
        for sym in SYMS:
            for slug, r in data[(sym, v)].items():
                pnl = r["sim"].get("pnl")
                if not r["sim"]["fires"] or pnl is None:
                    continue
                (losers if pnl < -1e-9 else winners).append((pnl, slug))
        losers.sort()
        print(f"| {v} | {len(losers)} | {sum(p for p, _ in losers):+.2f} | "
              f"{len(winners)} | {sum(p for p, _ in winners):+.2f} |")
    print()
    for v in VARIANTS:
        losers = []
        for sym in SYMS:
            for slug, r in data[(sym, v)].items():
                pnl = r["sim"].get("pnl")
                if r["sim"]["fires"] and pnl is not None and pnl < -1e-9:
                    losers.append((pnl, slug))
        losers.sort()
        s = ", ".join(f"`{slug}` {z(float(slug.rsplit('-',1)[1]))} {p:+.0f}"
                      for p, slug in losers)
        print(f"- **{v}**: {s}")

    print("\n## Robustness of the per-symbol Δnet\n")
    print("The A/B is one day and the P&L is concentrated. For each variant: how "
          "many windows moved at all, how the paired per-window Δ splits by sign, "
          "and what Δnet becomes with the single largest-|Δ| window dropped.\n")
    print("| symbol | variant | windows moved | Δ>0 | Δ<0 | Δnet | Δnet less top mover |")
    print("|---|---|---|---|---|---|---|")
    for sym in SYMS:
        for v in VARIANTS[1:]:
            ds = []
            for slug, rb in data[(sym, "base")].items():
                rv = data[(sym, v)].get(slug)
                if not rv:
                    continue
                d = (rv["sim"].get("pnl") or 0.0) - (rb["sim"].get("pnl") or 0.0)
                if abs(d) > 1e-6:
                    ds.append(d)
            tot = sum(ds)
            top = max(ds, key=abs) if ds else 0.0
            print(f"| {sym} | {v} | {len(ds)} | {sum(1 for d in ds if d > 0)} | "
                  f"{sum(1 for d in ds if d < 0)} | {tot:+.2f} | {tot - top:+.2f} |")

    print("\n## Refusal census (every graded 5m window, stream-fed)\n")
    for name in ("census-rtds", "census-base"):
        path = work / f"stderr-{name}.txt"
        if not path.exists():
            continue
        noparams = defaultdict(int)
        corpus = defaultdict(int)
        for line in open(path):
            if "skipping" not in line:
                continue
            slug = line.split("skipping '")[1].split("'")[0]
            sym = slug.split("-")[0]
            dur = slug.split("-")[2]
            if "no --params entry" in line:
                noparams[(sym, dur)] += 1
            else:
                corpus[sym] += 1
        print(f"- **{name}** — out of scope (no params entry): "
              f"{sum(noparams.values())} "
              f"[{', '.join(f'{k[0]} {k[1]}: {v}' for k, v in sorted(noparams.items()))}]")
        print(f"  - refused for want of corpus: {sum(corpus.values())} "
              f"[{', '.join(f'{k}: {v}' for k, v in sorted(corpus.items()))}]")

    print("\n## Sim vs live (baseline fidelity check)\n")
    print("| symbol | live fires | sim fires | live notional | sim notional |")
    print("|---|---|---|---|---|")
    for sym in SYMS:
        lf = sum(r["real"]["fires"] for r in data[(sym, "base")].values())
        ln = sum(r["real"]["notional"] for r in data[(sym, "base")].values())
        sf = sum(r["sim"]["fires"] for r in data[(sym, "base")].values())
        sn = sum(r["sim"]["notional"] for r in data[(sym, "base")].values())
        print(f"| {sym} | {lf} | {sf} | {ln:.2f} | {sn:.2f} |")


if __name__ == "__main__":
    main()
