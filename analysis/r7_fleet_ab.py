#!/usr/bin/env python3
"""R7 fleet-cap A/B driver: build the params, run `pmengine replay
--fleet-cap`, reconcile the runs into one table.

Where analysis/r7_fleet_cap.py asks "what would a cap have done to the
fills that actually happened", this asks the question a deploy needs:
"what does the cap do to the engine we are about to ship, over the same
recorded market data". Same corpus, same driver, same params — the only
thing that varies between runs is the cap, which is the only way a cap's
number means anything.

  policy=today  — theta 0.3 + the window brake latch (what ships)
  policy=prer9  — theta 0 and the brakes lifted: the policy the night's
                  REAL fills were built under, i.e. the one r7_fleet_cap.py's
                  counterfactual implicitly measured
  policy=scaled — today's policy at today's per-symbol budgets on every
                  window (the fleet scale-up this cap is a prerequisite for)

Reads frozen tape copies, never the live ones (L33 — the live tape is
append-only, so a re-run of a study is not a re-run). Freeze them with:

    cp ~/.pmt/engine/updown-tape.jsonl ~/.pmt/corpus/r7-updown-tape-frozen.jsonl
    cp ~/.pmt/engine/book-tape.jsonl   ~/.pmt/corpus/r7-book-tape-frozen.jsonl

Note on coverage: the book/spot recorder only started at 02:45:20Z on
2026-08-23, so `--mode full` (real depth, real bids, live exits) cannot see
the first ~4.7h of that night — which is exactly where the whole of the
r7_fleet_cap.py counterfactual lives. `--mode evals` reaches the full night
but fills against an unbounded book. Run both; they answer different halves.
"""
import argparse
import collections
import json
import os
import subprocess
import sys

CORPUS = os.path.expanduser("~/.pmt/corpus")
TAPE = os.path.join(CORPUS, "r7-updown-tape-frozen.jsonl")
BOOK = os.path.join(CORPUS, "r7-book-tape-frozen.jsonl")
OUTCOMES = os.path.join(CORPUS, "outcomes.jsonl")
ARMS = os.path.expanduser("~/.pmt/engine/arms-state.json")

SYMBOL = {"btc": "BTCUSDT", "eth": "ETHUSDT", "sol": "SOLUSDT",
          "xrp": "XRPUSDT", "bnb": "BNBUSDT", "doge": "DOGEUSDT"}
# Lifted brake thresholds = the pre-brake policy; there is a unit test in
# replay.rs pinning that these reproduce the old engine's fires.
LIFTED = {"distrust_net": 1e9, "avg_down_tol": 1e9}


# --- params ------------------------------------------------------------

def build_params(policy, out_path):
    """Per-window params for every window either tape can replay.

    Per-window where the tape actually knows: `size_usdc` from the window's
    own `roll` record (its real as-armed budget), `basis_guard_bp` from the
    minimum `guard_bp` the window recorded (the static param the live
    dynamic guard raised from). Everything else is the fleet's current
    per-symbol policy — sigma is only eval_model's cold-start fallback, so
    a stale seed costs nothing once replay's klines are in hand.
    """
    live = {a["symbol"]: a for a in json.load(open(ARMS))["arms"]}
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
        if not symbol:
            continue
        t = live.get(symbol) or live[next(iter(live))]
        if policy == "scaled":
            size = t["size_usdc"]
        else:
            series = "-".join(slug.split("-")[:3])
            size = roll_size.get(slug) or first_size.get(series, (0, 100.0))[1]
        e = dict(t)
        e.update({
            "slug": slug, "symbol": symbol,
            "token_up": f"{slug}-u", "token_down": f"{slug}-d",
            "start": float(start), "end": float(start + dur_m * 60),
            "size_usdc": size,
            "basis_guard_bp": guard_floor.get(slug, t["basis_guard_bp"]),
            "theta": t["theta"] if policy == "today" else 0.0,
            "roll": False,
        })
        if policy == "prer9":
            e["tunables"] = LIFTED
        entries.append(e)

    with open(out_path, "w") as fh:
        json.dump(entries, fh)
    sizes = collections.Counter(e["size_usdc"] for e in entries)
    print(f"[params] {len(entries)} windows -> {out_path}  sizes={dict(sorted(sizes.items()))}",
          file=sys.stderr)
    return out_path


# --- runs ---------------------------------------------------------------

def replay(binary, mode, params, cap, out):
    subprocess.run(
        [binary, "replay", "--mode", mode, "--slug", "",
         "--tape", TAPE, "--book-tape", BOOK, "--params", params,
         "--outcomes", OUTCOMES, "--fleet-cap", str(cap), "--out", out],
        check=True, stdout=subprocess.DEVNULL,
    )
    return out


def load(path):
    rows = [json.loads(l) for l in open(path)]
    return {r["slug"]: r for r in rows if "aggregate" not in r["slug"]}


def table(tag, runs):
    """runs: [(cap, path)], cap 0 first — the uncapped interleaved baseline."""
    base = load(runs[0][1])
    tot = lambda rows, k: sum(r["sim"][k] or 0.0 for r in rows.values())
    print(f"\n{'=' * 78}\n{tag}\n{'=' * 78}")
    print(f"corpus: {len(base)} windows   baseline (cap off): "
          f"fires={sum(r['sim']['fires'] for r in base.values())}  "
          f"notional=${tot(base, 'notional'):.0f}  pnl=${tot(base, 'pnl'):+.2f}")
    hdr = (f"\n{'cap':>6} {'wins hit':>9} {'blocked$':>10} {'avoided':>10} "
           f"{'missed':>10} {'reshaped':>10} {'NET':>10} {'$net/$blk':>10} {'blocks':>8}")
    print(hdr + "\n" + "-" * len(hdr))
    detail = {}
    for cap, path in runs[1:]:
        run = load(path)
        blocked = avoided = missed = reshaped = 0.0
        hit, rows = 0, []
        for slug, r in run.items():
            d = (r["sim"]["pnl"] or 0.0) - (base[slug]["sim"]["pnl"] or 0.0)
            b = base[slug]["sim"]["notional"] - r["sim"]["notional"]
            if b <= 1e-9:
                # Not cut, but not identical either: a deferred clip fires
                # a tick later at a different ask, and the no-averaging-down
                # anchor moves with it. Counted apart, never hidden.
                reshaped += d
                continue
            hit += 1
            blocked += b
            if d >= 0:
                avoided += d
            else:
                missed += -d
            rows.append((slug, b, d, base[slug]["sim"]["notional"],
                         base[slug]["sim"]["pnl"], r["sim"]["outcome"]))
        net = tot(run, "pnl") - tot(base, "pnl")
        blks = sum(r["fleet"]["blocks"] for r in run.values())
        print(f"{cap:>6} {hit:>9} {blocked:>10.0f} {avoided:>+10.0f} {-missed:>+10.0f} "
              f"{reshaped:>+10.0f} {net:>+10.0f} "
              f"{(net / blocked if blocked else 0):>10.2f} {blks:>8}")
        detail[cap] = sorted(rows, key=lambda x: x[2])
    return detail


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--bin", default="pmengine/target/release/pmengine")
    ap.add_argument("--mode", default="full", choices=("full", "evals"))
    ap.add_argument("--policy", default="today", choices=("today", "prer9", "scaled"))
    ap.add_argument("--caps", default="250,350")
    ap.add_argument("--work", default="/tmp/r7-ab")
    ap.add_argument("--detail", action="store_true", help="list every window the cap touched")
    a = ap.parse_args()

    os.makedirs(a.work, exist_ok=True)
    params = build_params(a.policy, os.path.join(a.work, f"params-{a.policy}.json"))
    runs = [(0, replay(a.bin, a.mode, params, 0,
                       os.path.join(a.work, f"{a.mode}-{a.policy}-cap0.jsonl")))]
    for cap in (int(c) for c in a.caps.split(",")):
        runs.append((cap, replay(a.bin, a.mode, params, cap,
                                 os.path.join(a.work, f"{a.mode}-{a.policy}-cap{cap}.jsonl"))))

    detail = table(f"mode={a.mode}  policy={a.policy}", runs)
    if a.detail:
        for cap, rows in detail.items():
            print(f"\n  cap=${cap} — every window the cap touched:")
            for slug, b, d, notional, pnl, won in rows:
                print(f"    {slug:32s} won={won or '?':4s} "
                      f"uncapped=${notional:7.2f}/${pnl:+8.2f}  "
                      f"blocked=${b:7.2f}  delta=${d:+8.2f}")


if __name__ == "__main__":
    main()
