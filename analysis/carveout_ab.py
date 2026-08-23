#!/usr/bin/env python3
"""Carve-out A/B driver: tighten the `banked_decided` waiver and the
last-120s full-budget unlock, and price each tightening over the same
recorded night.

Same shape as analysis/r7_fleet_ab.py — build per-window params, run
`pmengine replay --mode full --fleet-cap`, reconcile the runs into one
table — because a knob's number only means anything when the corpus, the
driver and every other param are held fixed and the knob is the ONLY thing
that varies.

Variants (every one equal-or-tighter than live, by construction):

  base         live policy, the baseline every delta is measured against
  k100         decided_k 1.00 — a no-op control. MUST reproduce base
                 exactly; if it doesn't, the plumbing is lying.
  k125 k150    decidedness needs |margin| > k * cushion
  stale30      decidedness refused for 30s after a staleness gate
  m3 m5 m10    the late unlock's ceiling held to m * clip_usdc
  combo*       the best-looking combinations

Reads FROZEN tape copies and runs under a SHADOW $HOME (L33 + the campaign's
read-only rule): `--mode full` writes a klines cache under $HOME/.pmt/corpus,
so pointing HOME at a copy is what keeps the live ~/.pmt untouched.
"""
import argparse
import collections
import json
import os
import subprocess
import sys

WORK = "/var/home/hunter/Desktop/code/pmt-carveout-work"
TAPE = os.path.join(WORK, "updown-tape-frozen.jsonl")
BOOK = os.path.join(WORK, "book-tape-frozen.jsonl")
OUTCOMES = os.path.join(WORK, "outcomes-frozen.jsonl")
SHADOW_HOME = os.path.join(WORK, "home")
RTDS = os.path.join(SHADOW_HOME, ".pmt/corpus/rtds")
ARMS = os.path.expanduser("~/.pmt/engine/arms-state.json")

SYMBOL = {"btc": "BTCUSDT", "eth": "ETHUSDT", "sol": "SOLUSDT",
          "xrp": "XRPUSDT", "bnb": "BNBUSDT", "doge": "DOGEUSDT"}

# The windows the campaign named, by start epoch.
NAMED = {
    1787505300: "17:15Z five-arm",
    1787462100: "05:15Z eth/sol",
    1787508300: "18:05Z 0.04bp flip",
}

VARIANTS = {
    "base":    {},
    "k100":    {"decided_k": 1.0},
    "k125":    {"decided_k": 1.25},
    "k150":    {"decided_k": 1.5},
    "stale30": {"decided_stale_s": 30.0},
    "m3":      {"late_clip_mult": 3.0},
    "m5":      {"late_clip_mult": 5.0},
    "m10":     {"late_clip_mult": 10.0},
    "k110":    {"decided_k": 1.10},
    "k115":    {"decided_k": 1.15},
    "k135":    {"decided_k": 1.35},
    # Probe, not a candidate: a 10-minute stale window is far wider than any
    # policy would ship. If even THIS moves nothing, the knob is unreachable
    # in this harness rather than merely unbinding.
    "stale600": {"decided_stale_s": 600.0},
    "k125+m5":       {"decided_k": 1.25, "late_clip_mult": 5.0},
    "k125+stale30":  {"decided_k": 1.25, "decided_stale_s": 30.0},
    "k125+m5+stale30": {"decided_k": 1.25, "late_clip_mult": 5.0,
                        "decided_stale_s": 30.0},
    "k150+m3+stale30": {"decided_k": 1.5, "late_clip_mult": 3.0,
                        "decided_stale_s": 30.0},
    # Duration-scoped. The whole measured effect of decided_k lives in the
    # 15m book, where docs/LESSONS.md L39 / analysis/fourh_fit.md already say
    # the range_avg "banked mass" is a momentum proxy that lies with
    # duration. These two isolate that from the 5m book, which is where the
    # fleet's actual edge is. `@` scopes a variant to one duration token.
    "k125@15m": {"decided_k": 1.25, "@": "15m"},
    "k150@15m": {"decided_k": 1.5, "@": "15m"},
    "k125@5m":  {"decided_k": 1.25, "@": "5m"},
    "k150@5m":  {"decided_k": 1.5, "@": "5m"},
}


def build_params(tun, out_path):
    """Per-window params for every window the tapes can replay.

    `size_usdc` from the window's own `roll` record (its real as-armed
    budget), `basis_guard_bp` from the minimum `guard_bp` it recorded.
    Everything else is the fleet's current per-symbol policy. Identical to
    r7_fleet_ab.py's policy=today, plus the variant's tunables.
    """
    live = {a["symbol"]: a for a in json.load(open(ARMS))["arms"]}
    # Two arms per symbol in the live store (a $1 canary and the real one);
    # the real arm is the bigger budget.
    best = {}
    for a in live.values():
        cur = best.get(a["symbol"])
        if cur is None or a["size_usdc"] > cur["size_usdc"]:
            best[a["symbol"]] = a
    live = best

    slugs, roll_size, guard_floor, first_size = set(), {}, {}, {}
    for path in (BOOK, TAPE):
        for line in open(path):
            try:
                r = json.loads(line)
            except ValueError:
                continue
            slug = r.get("slug")
            if not slug:
                continue
            slugs.add(slug)
            if r.get("ev") == "roll":
                roll_size[slug] = r["size"]
                k = "-".join(slug.split("-")[:3])
                if k not in first_size or r["t"] < first_size[k][0]:
                    first_size[k] = (r["t"], r["size"])
            g = r.get("guard_bp")
            if isinstance(g, (int, float)):
                guard_floor[slug] = min(guard_floor.get(slug, g), g)

    entries = []
    for slug in sorted(slugs):
        try:
            coin, _, dur, start = slug.split("-")
            dur_m, start = int(dur.rstrip("m")), int(start)
        except ValueError:
            continue
        symbol = SYMBOL.get(coin)
        if not symbol or symbol not in live:
            continue
        t = live[symbol]
        series = "-".join(slug.split("-")[:3])
        size = roll_size.get(slug) or first_size.get(series, (0, 100.0))[1]
        e = dict(t)
        e.update({
            "slug": slug, "symbol": symbol,
            "token_up": f"{slug}-u", "token_down": f"{slug}-d",
            "start": float(start), "end": float(start + dur_m * 60),
            "size_usdc": size,
            "basis_guard_bp": guard_floor.get(slug, t["basis_guard_bp"]),
            "roll": False,
        })
        scope = tun.get("@") if tun else None
        if tun and (scope is None or scope == dur):
            e["tunables"] = {k: v for k, v in tun.items() if k != "@"}
        entries.append(e)

    with open(out_path, "w") as fh:
        json.dump(entries, fh)
    return out_path, len(entries)


def replay(binary, params, cap, out, mode="full"):
    env = dict(os.environ, HOME=SHADOW_HOME)
    cmd = [binary, "replay", "--mode", mode, "--slug", "",
           "--tape", TAPE, "--book-tape", BOOK, "--params", params,
           "--outcomes", OUTCOMES, "--rtds-corpus", RTDS,
           "--fleet-cap", str(cap), "--out", out]
    r = subprocess.run(cmd, env=env, stdout=subprocess.DEVNULL,
                       stderr=subprocess.PIPE, text=True)
    if r.returncode != 0:
        sys.stderr.write(r.stderr[-4000:])
        raise SystemExit(f"replay failed ({r.returncode}) for {out}")
    return out


def load(path):
    rows = [json.loads(l) for l in open(path)]
    return {r["slug"]: r for r in rows if "aggregate" not in r["slug"]}


def start_of(slug):
    try:
        return int(slug.split("-")[3])
    except (IndexError, ValueError):
        return None


def summarize(run):
    """W-L / net over windows this variant actually took a position in."""
    w = l = 0
    net = 0.0
    notional = fires = 0
    for r in run.values():
        s = r["sim"]
        if s["fires"] == 0:
            continue
        pnl = s["pnl"] or 0.0
        net += pnl
        notional += s["notional"]
        fires += s["fires"]
        if pnl > 0:
            w += 1
        elif pnl < 0:
            l += 1
    return {"W": w, "L": l, "net": net, "notional": notional, "fires": fires}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--bin", default=os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "pmengine/target/release/pmengine"))
    ap.add_argument("--cap", type=float, default=500.0)
    ap.add_argument("--work", default=os.path.join(WORK, "ab"))
    ap.add_argument("--only", default=None, help="comma-separated variant subset")
    ap.add_argument("--mode", default="full", choices=("full", "evals"))
    ap.add_argument("--json-out", default=os.path.join(WORK, "carveout-ab.json"))
    a = ap.parse_args()
    os.makedirs(a.work, exist_ok=True)

    names = a.only.split(",") if a.only else list(VARIANTS)
    if "base" not in names:
        names.insert(0, "base")

    runs = {}
    for name in names:
        params, n = build_params(VARIANTS[name], os.path.join(a.work, f"params-{name}.json"))
        out = replay(a.bin, params, a.cap,
                     os.path.join(a.work, f"run-{name}.jsonl"), a.mode)
        runs[name] = load(out)
        print(f"[run] {name:18} {n} windows -> {out}", file=sys.stderr)

    base = runs["base"]
    bs = summarize(base)
    print(f"\ncorpus: {len(base)} windows, fleet cap ${a.cap:.0f}, --mode {a.mode}")
    print(f"BASELINE (live policy): {bs['W']}W-{bs['L']}L  {bs['fires']} fires  "
          f"${bs['notional']:,.0f} notional  net ${bs['net']:+,.2f}")

    hdr = (f"\n{'variant':<18} {'W':>4} {'L':>4} {'net':>10} {'delta':>9} "
           f"{'forfeit':>9} {'avoided':>9} {'reshaped':>9} {'cut$':>9} {'wins lost':>9}")
    print(hdr + "\n" + "-" * len(hdr.strip()))
    table, detail = {}, {}
    for name in names:
        run = runs[name]
        s = summarize(run)
        forfeit = avoided = reshaped = cut = 0.0
        wins_lost = 0
        rows = []
        for slug, r in run.items():
            b = base[slug]
            d = (r["sim"]["pnl"] or 0.0) - (b["sim"]["pnl"] or 0.0)
            cutn = b["sim"]["notional"] - r["sim"]["notional"]
            if abs(cutn) <= 1e-9 and abs(d) <= 1e-9:
                continue
            if cutn <= 1e-9:
                # Not cut, but not identical: a deferred clip fires a tick
                # later at a different ask. Counted apart, never hidden.
                reshaped += d
                continue
            cut += cutn
            if d >= 0:
                avoided += d
            else:
                forfeit += -d
                if (b["sim"]["pnl"] or 0.0) > 0:
                    wins_lost += 1
            rows.append((slug, cutn, d, b["sim"]["pnl"], r["sim"]["pnl"]))
        delta = s["net"] - bs["net"]
        print(f"{name:<18} {s['W']:>4} {s['L']:>4} {s['net']:>+10.2f} {delta:>+9.2f} "
              f"{-forfeit:>+9.2f} {avoided:>+9.2f} {reshaped:>+9.2f} "
              f"{cut:>9.0f} {wins_lost:>9}")
        table[name] = dict(s, delta=delta, forfeit=forfeit, avoided=avoided,
                           reshaped=reshaped, cut=cut, wins_lost=wins_lost)
        detail[name] = sorted(rows, key=lambda x: x[2])

    # --- the three named events ------------------------------------------
    print("\n" + "=" * 96)
    print("NAMED EVENTS — per-variant P&L on the windows the campaign named")
    print("=" * 96)
    hdr = f"\n{'variant':<18}" + "".join(f"{lbl:>26}" for lbl in NAMED.values())
    print(hdr + "\n" + "-" * len(hdr.strip()))
    named_tbl = {}
    for name in names:
        run = runs[name]
        cells, vals = [], {}
        for start in NAMED:
            sub = [r for s, r in run.items() if start_of(s) == start]
            pnl = sum(r["sim"]["pnl"] or 0.0 for r in sub)
            n = sum(r["sim"]["notional"] for r in sub)
            f = sum(r["sim"]["fires"] for r in sub)
            vals[start] = {"pnl": pnl, "notional": n, "fires": f}
            cells.append(f"{pnl:>+11.2f} ({f:>2}f ${n:>6.0f})")
        named_tbl[name] = vals
        print(f"{name:<18}" + "".join(f"{c:>26}" for c in cells))

    with open(a.json_out, "w") as fh:
        json.dump({"baseline": bs, "table": table, "named": named_tbl,
                   "detail": {k: v[:25] for k, v in detail.items()},
                   "cap": a.cap}, fh, indent=1)
    print(f"\n[wrote] {a.json_out}", file=sys.stderr)


if __name__ == "__main__":
    main()
