#!/usr/bin/env python3
"""15m re-opening study — settlement-rule A/B on the stream, and the guard floor.

The successor `analysis/feed_ab.md` §10.3 owes ("a settle_rule=terminal A/B at
settle_tw 60 — that's where the $676 is") and the run
`analysis/fifteen_stream_fit.md` §4 could not do, because 15m book coverage
ended 08:08:57Z and the RTDS corpus began 08:28:55Z. The books-only observer
arms that §4 asked for went up at 17:06Z, so the two tapes now overlap and the
A/B is runnable for the first time.

Read-only against ~/.pmt. Writes only under --work.

Subcommands:

  survey   corpus + book + outcomes coverage at 15m, the stream noise floor
           measured the way analysis/feed_ab.py measured it, and the
           `--params` arrays for every leg. The comparable set is the
           intersection the harness can actually run: in the book tape,
           graded in outcomes.jsonl, and fully inside the RTDS corpus. A
           stream-fed window with no corpus behind it is REFUSED by replay by
           design, so a baseline allowed to run windows the variants cannot is
           not a baseline.

  report   grade the legs against each other: fired / W-L / net / notional /
           RoN, the per-window sign test, and the jackknife the feed A/B ran.
           Only DELTAS are read as evidence — the fill sim's absolute pnl is
           not wallet truth (analysis/hybrid_ab.md).

Usage:
  uv run --project pmtrader python analysis/fifteen_rerun.py survey --work DIR
  uv run --project pmtrader python analysis/fifteen_rerun.py report --work DIR
"""

import argparse
import bisect
import json
import math
import os
import pathlib
import random
import sys
from collections import defaultdict

PMT = pathlib.Path(os.environ.get("PMT_HOME", pathlib.Path.home() / ".pmt"))
CORPUS = PMT / "corpus"

# sol is carried through every table but never through a verdict: the vol
# study refuses it at 15m (P(|move| > 10bp in 7.5min) = 66%), so it is
# reported as a third data point and is not a candidate to re-open.
SYMS = ["btc", "eth", "sol"]
VERDICT_SYMS = ["btc", "eth"]
RTDS_SYMBOL = {s: f"{s}/usd" for s in SYMS}
BIN_SYMBOL = {s: f"{s.upper()}USDT" for s in SYMS}
TOPIC_TWAP60 = "crypto_prices_twap_sixty"
TOPIC_SPOT = "crypto_prices_chainlink"
DUR_S = 900.0
SETTLE_TW_S = 60.0

# Sizes are held FIXED across every leg and every window at the level the 15m
# fleet actually ran before it was parked (the eval tape's own `roll` records:
# btc/eth 350, sol 150, max fire notional 49.92 / 49.98 / 24.91). They cannot
# be read per-window off the tape the way analysis/feed_ab.py reads the 5m
# ones, because every 15m `roll` since 17:00Z carries the OBSERVER size of
# $1. Identical across legs, so it cannot bias a delta; it does set the level,
# and the level is the harness's, not the wallet's.
PARKED = {
    "btc": dict(size=350.0, clip=50.0),
    "eth": dict(size=350.0, clip=50.0),
    "sol": dict(size=150.0, clip=25.0),
}
# Cold-start sigma fallback only — the vol floor uses live trailing sigma once
# the feed holds history. Taken from the 15m arms' own as-armed values.
SIGMA = {"btc": 9.0, "eth": 25.0, "sol": 12.0}


def utc(t):
    import datetime
    return datetime.datetime.fromtimestamp(t, datetime.timezone.utc).strftime("%H:%M:%SZ")


def pct(xs, q):
    if not xs:
        return float("nan")
    xs = sorted(xs)
    i = min(len(xs) - 1, max(0, int(math.ceil(q * len(xs))) - 1))
    return xs[i]


def med(xs):
    return pct(xs, 0.5)


# ---------------------------------------------------------------- corpus


def load_corpus(rtds_dir):
    """(samples, logged gaps, lines, files). samples[sym][topic] = [(t_recv, ts_s, v)]."""
    rtds_dir = pathlib.Path(rtds_dir)
    files = sorted(rtds_dir.glob("rtds-*.jsonl")) if rtds_dir.is_dir() else [rtds_dir]
    if not files:
        sys.exit(f"no rtds-*.jsonl under {rtds_dir}")
    want = set(RTDS_SYMBOL.values())
    inv = {v: k for k, v in RTDS_SYMBOL.items()}
    samples = {s: {TOPIC_TWAP60: [], TOPIC_SPOT: []} for s in SYMS}
    gaps, lines = [], 0
    for f in files:
        with open(f) as fh:
            for line in fh:
                lines += 1
                if '"ev"' in line and '"symbol"' not in line:
                    d = json.loads(line)
                    if d.get("ev") == "gap":
                        gaps.append((d["t_last"], d["t_recv"], d["down_s"], d["reason"]))
                    continue
                if TOPIC_TWAP60 not in line and TOPIC_SPOT not in line:
                    continue
                if not any(s in line for s in want):
                    continue
                d = json.loads(line)
                sym = inv.get(d.get("symbol"))
                if sym is None or d.get("topic") not in (TOPIC_TWAP60, TOPIC_SPOT):
                    continue
                samples[sym][d["topic"]].append((d["t_recv"], d["ts"] / 1000.0, d["value"]))
    for sym in SYMS:
        for t in samples[sym]:
            samples[sym][t].sort(key=lambda r: r[0])
    return samples, gaps, lines, [str(f) for f in files]


def data_gaps(samples, min_s=5.0):
    """Recorder holes read off the DATA — the gap log only covers stalls a
    RUNNING recorder noticed, never one it died through (feed_ab.py:data_gaps)."""
    out = []
    for sym in SYMS:
        rows = samples[sym][TOPIC_SPOT]
        for (a, _, _), (b, _, _) in zip(rows, rows[1:]):
            if b - a > min_s:
                out.append((a, b, b - a, f"{sym} spot spacing"))
    out.sort()
    merged = []
    for g in out:
        if merged and g[0] <= merged[-1][1]:
            last = merged[-1]
            merged[-1] = (last[0], max(last[1], g[1]), max(last[1], g[1]) - last[0], last[3])
        else:
            merged.append(g)
    return merged


def corpus_span(samples):
    lo, hi, per = -math.inf, math.inf, {}
    for sym in SYMS:
        a, b = samples[sym][TOPIC_TWAP60], samples[sym][TOPIC_SPOT]
        if not a or not b:
            per[sym] = None
            continue
        s = (max(a[0][0], b[0][0]), min(a[-1][0], b[-1][0]))
        per[sym] = s
        lo, hi = max(lo, s[0]), min(hi, s[1])
    return (lo, hi), per


# ------------------------------------------------------------- book tape


def load_book(book_tape, dur_tag="-15m-"):
    """(windows, binance spot series). One pass — the tape is 45MB."""
    wins, spot = {}, defaultdict(list)
    with open(book_tape) as fh:
        for line in fh:
            if dur_tag not in line:
                continue
            try:
                d = json.loads(line)
            except Exception:
                continue
            slug = d.get("slug") or ""
            if dur_tag not in slug:
                continue
            sym = slug.split("-")[0]
            if sym not in SYMS:
                continue
            t = d.get("t")
            if t is None:
                continue
            e = wins.get(slug)
            if e is None:
                wins[slug] = [1, t, t]
            else:
                e[0] += 1
                e[1] = min(e[1], t)
                e[2] = max(e[2], t)
            s = d.get("spot")
            if s:
                spot[sym].append((t - (d.get("spot_age_s") or 0.0), s))
    for s in spot:
        spot[s].sort()
    return wins, spot


# ------------------------------------------------------- guard sizing


class StepSeries:
    """Last value received at or before t — the shape a live consumer holds."""

    def __init__(self, rows):
        self.t = [r[0] for r in rows]
        self.v = [r[2] if len(r) > 2 else r[1] for r in rows]

    def at(self, t):
        j = bisect.bisect_right(self.t, t) - 1
        if j < 0:
            return None, None
        return self.v[j], self.t[j]


def guard_survey(samples, book_spot, span, gaps, stale_s=5.0):
    """The same three disagreements analysis/feed_ab.py measured, on the same
    clock, with the same estimator — so the number is comparable to the one
    that reproduced the deployed binance guards.

      rtds    |chainlink spot - twap60|  the substitution a STREAM-fed arm
              still makes: it prices the unformed remainder off the settlement
              object's instantaneous value where the settlement quantity is
              that value's 60s trailing mean.
      binance |binance spot - twap60|    the same substitution PLUS the
              cross-venue basis — the quantity the live guards were sized on.
      venue   |binance spot - chainlink spot|  the pure venue term.

    Duration-independent by construction (both 5m and 15m settle on the sixty
    topic once `--settle-tw 60` is passed), so the 15m floor and the 5m floor
    are the same measurement over a different span. Both are reported.
    """
    lo, hi = span
    gap_iv = sorted((a, b) for (a, b, _, _) in gaps)

    def in_gap(t):
        j = bisect.bisect_right(gap_iv, (t, math.inf)) - 1
        return j >= 0 and gap_iv[j][1] >= t

    out = {}
    for sym in SYMS:
        tw = StepSeries(samples[sym][TOPIC_TWAP60])
        bs = StepSeries([(t, None, v) for (t, v) in book_spot.get(sym, [])])
        d = {"rtds": [], "binance": [], "venue": []}
        for (t_recv, _ts, spot) in samples[sym][TOPIC_SPOT]:
            if not (lo <= t_recv <= hi) or in_gap(t_recv):
                continue
            w, wt = tw.at(t_recv)
            if w is None or t_recv - wt > stale_s or w <= 0:
                continue
            d["rtds"].append(abs(spot / w - 1.0) * 1e4)
            b, bt = bs.at(t_recv)
            if b is not None and t_recv - bt <= stale_s and b > 0:
                d["binance"].append(abs(b / w - 1.0) * 1e4)
                d["venue"].append(abs(b / spot - 1.0) * 1e4)
        out[sym] = d
    return out


# ------------------------------------------------------------ params


def build_params(slugs, feed, rule, guard_by_sym, settle_tw_s, live):
    """One params entry per window slug. Everything except the leg's own three
    knobs (feed / settle_rule / guard) is IDENTICAL across legs — that is the
    only way the delta means anything (analysis/aggression_sweep.md)."""
    out = []
    for slug in sorted(slugs, key=lambda s: (float(s.rsplit("-", 1)[1]), s)):
        sym = slug.split("-")[0]
        start = float(slug.rsplit("-", 1)[1])
        p = dict(live[sym])
        p.update({
            "slug": slug, "kind": "twap", "symbol": BIN_SYMBOL[sym],
            # Token ids are per-window and replay never places an order; the
            # book view joins on the tape's own sides, not on these.
            "token_up": f"{slug}-up", "token_down": f"{slug}-down",
            "start": start, "end": start + DUR_S,
            "sigma_bp_per_min": SIGMA[sym],
            "size_usdc": PARKED[sym]["size"], "clip_usdc": PARKED[sym]["clip"],
            "min_fair": 0.97, "theta": 0.3, "roll": False, "maker_bid": False,
            "feed": feed, "settle_rule": rule,
            "basis_guard_bp": guard_by_sym[sym],
            "settle_tw_s": settle_tw_s,
        })
        out.append(p)
    return out


def as_armed_params(slugs, arms_state):
    """The 15m arms EXACTLY as they are armed right now — the reproduction leg.

    Nothing is substituted: size $1, clip $1, min_fair 1.0, theta 1.0,
    feed binance, settle_tw_s 0. If this leg fires anything the reproduction
    is broken.
    """
    live = {a["slug"].split("-")[0]: a for a in arms_state["arms"]
            if "-updown-15m-" in a["slug"]}
    out = []
    for slug in sorted(slugs, key=lambda s: (float(s.rsplit("-", 1)[1]), s)):
        sym = slug.split("-")[0]
        base = live.get(sym)
        if base is None:
            continue
        start = float(slug.rsplit("-", 1)[1])
        p = dict(base)
        p.update({"slug": slug, "start": start, "end": start + DUR_S,
                  "token_up": f"{slug}-up", "token_down": f"{slug}-down",
                  "roll": False})
        out.append(p)
    return out


# -------------------------------------------------------------- survey


def cmd_survey(a):
    work = pathlib.Path(a.work)
    work.mkdir(parents=True, exist_ok=True)

    print("[1/5] loading RTDS corpus ...", flush=True)
    samples, logged, lines, files = load_corpus(a.rtds_dir)
    span, per_sym = corpus_span(samples)
    gaps = data_gaps(samples)
    print(f"      {lines:,} line(s) over {len(files)} file(s)")
    print(f"      self-logged gaps {len(logged)} / {sum(g[2] for g in logged):.0f}s   "
          f"holes in the DATA {len(gaps)} / {sum(g[2] for g in gaps):.0f}s "
          f"(worst {max(g[2] for g in gaps):.0f}s)")
    print(f"      common span {utc(span[0])} .. {utc(span[1])}")
    for s in SYMS:
        lo, hi = per_sym[s]
        print(f"      {s:4s} twap60={len(samples[s][TOPIC_TWAP60]):7,d} "
              f"spot={len(samples[s][TOPIC_SPOT]):7,d}  {utc(lo)}..{utc(hi)}")

    print("[2/5] scanning the 15m book tape ...", flush=True)
    wins, book_spot = load_book(a.book_tape)
    outcomes = {}
    for line in open(a.outcomes):
        d = json.loads(line)
        outcomes[d["slug"]] = d
    n15 = sum(1 for s in outcomes if "-updown-15m-" in s)
    print(f"      {len(wins)} 15m window(s) in the book tape; "
          f"{len(outcomes)} graded outcome(s), {n15} of them 15m")

    comparable, excluded = defaultdict(list), defaultdict(lambda: defaultdict(list))
    binance_set = defaultdict(list)      # book + graded, corpus not required
    for slug, (n, tlo, thi) in sorted(wins.items()):
        sym = slug.split("-")[0]
        start = float(slug.rsplit("-", 1)[1])
        end = start + DUR_S
        if slug not in outcomes:
            excluded[sym]["ungraded"].append(slug)
            continue
        binance_set[sym].append(slug)
        lo, hi = per_sym[sym]
        # RtdsTimeline::build refuses a window the corpus does not span, and
        # the range-start reference is the twap60 print at start-60.
        if start - 60.0 < lo or end > hi:
            excluded[sym]["outside_corpus"].append(slug)
            continue
        comparable[sym].append(slug)
    for s in SYMS:
        ex = excluded[s]
        print(f"      {s:4s} comparable={len(comparable[s]):3d}  "
              f"binance-reachable={len(binance_set[s]):3d}  "
              f"outside_corpus={len(ex['outside_corpus']):3d} "
              f"ungraded={len(ex['ungraded']):3d}")
    for s in SYMS:
        for slug in comparable[s]:
            n, tlo, thi = wins[slug]
            start = float(slug.rsplit("-", 1)[1])
            print(f"        {slug:32s} book {n:4d} rec  {utc(tlo)}..{utc(thi)}  "
                  f"(window {utc(start)}..{utc(start + DUR_S)})  won={outcomes[slug]['winner']}"
                  f"  [{outcomes[slug]['source']}]")

    # Book coverage inside a window is NOT the same as the window being in the
    # tape: the observer arms went up mid-window, so the first one starts late.
    coverage = {}
    for s in SYMS:
        for slug in comparable[s]:
            n, tlo, thi = wins[slug]
            start = float(slug.rsplit("-", 1)[1])
            coverage[slug] = {"records": n, "first_frac": (tlo - start) / DUR_S,
                              "last_frac": (thi - start) / DUR_S}

    gap_hit = defaultdict(list)
    for s in SYMS:
        for slug in comparable[s]:
            start = float(slug.rsplit("-", 1)[1])
            for (ga, gb, ds, why) in gaps:
                if ga < start + DUR_S and gb > start:
                    gap_hit[s].append((slug, round(ds, 1), why))
                    break

    print("[3/5] sizing the stream guard ...", flush=True)
    # Two spans: the whole corpus (the biggest n, and directly comparable to
    # analysis/feed_ab.md §3) and the study span alone (the hours this A/B
    # actually replays). The floor is never allowed to be the looser of the two.
    study_lo = min((float(s.rsplit("-", 1)[1]) - 60.0
                    for sy in SYMS for s in comparable[sy]), default=span[0])
    study_hi = max((float(s.rsplit("-", 1)[1]) + DUR_S
                    for sy in SYMS for s in comparable[sy]), default=span[1])
    surveys = {"corpus": guard_survey(samples, book_spot, span, gaps),
               "study": guard_survey(samples, book_spot, (study_lo, study_hi), gaps)}
    guard_rows = []
    for sym in SYMS:
        row = {"sym": sym}
        for which, gs in surveys.items():
            for k in ("rtds", "binance", "venue"):
                xs = gs[sym][k]
                row[f"{which}_{k}"] = {"n": len(xs), "med": med(xs), "p90": pct(xs, 0.90),
                                       "p99": pct(xs, 0.99),
                                       "max": max(xs) if xs else float("nan")}
        guard_rows.append(row)
        for which in ("corpus", "study"):
            for k in ("rtds", "binance", "venue"):
                c = row[f"{which}_{k}"]
                print(f"      {sym:4s} {which:6s} {k:8s} n={c['n']:6d} med={c['med']:6.2f} "
                      f"p90={c['p90']:6.2f} p99={c['p99']:6.2f}")

    arms_state = json.load(open(a.arms_state))
    live_arm = {a_["slug"].split("-")[0]: a_ for a_ in arms_state["arms"]
                if "-updown-15m-" in a_["slug"]}
    live_guard = {s: live_arm[s]["basis_guard_bp"] for s in SYMS if s in live_arm}
    stream_guard = {r["sym"]: float(math.ceil(max(r["corpus_rtds"]["p90"],
                                                  r["study_rtds"]["p90"])))
                    for r in guard_rows}
    # The measurement sets a FLOOR, not a target — it says how thin a margin
    # the feed can no longer distinguish from nothing, never that a margin it
    # CAN resolve is worth trading. So: max(live, floor). Never looser than
    # the measurement supports, never looser than what is deployed today.
    floor_guard = {s: max(stream_guard[s], live_guard.get(s, 0.0)) for s in SYMS}
    print(f"      live guard            {live_guard}")
    print(f"      stream floor ceil p90 {stream_guard}")
    print(f"      recommended max(l,f)  {floor_guard}")

    print("[4/5] writing params ...", flush=True)
    # `live` carries the gate parameters the 15m fleet ran on before it was
    # parked, NOT the observer arm's. min_elapsed 0 / pay_up / cooldown /
    # early_* / rho_block / manip_push are the fleet-wide values every live
    # arm carries today; they are identical on every leg.
    live = {s: {"fee_rate": 0.07, "min_edge": 0.015, "max_price": 0.985,
                "quiesce_secs": 20.0, "side_filter": None, "min_elapsed_frac": 0.0,
                "clip_cooldown_s": 2.0, "early_frac": 0.2, "early_min_edge": 0.08,
                "late_rem_s": 120.0, "rho_block": -0.25, "pay_up_max": 0.05,
                "p_cap": 1.0, "manip_push_bp": 25.0} for s in SYMS}

    legs = {
        # The live posture, at the sizes the fleet would re-open at.
        "base":          ("binance", "range_avg", live_guard, 0.0),
        # (a) the momentum proxy, on the settlement series.
        "rtds_range_avg": ("rtds", "range_avg", floor_guard, SETTLE_TW_S),
        # (b) evidence from momentum, risk from settlement arithmetic.
        "rtds_hybrid":   ("rtds", "hybrid", floor_guard, SETTLE_TW_S),
        # (c) the rule the market actually settles on.
        "rtds_terminal": ("rtds", "terminal", floor_guard, SETTLE_TW_S),
    }
    groups = {"fleet": SYMS, "pair": VERDICT_SYMS, **{s: [s] for s in SYMS}}
    for gname, gsyms in groups.items():
        slugs = [s for sy in gsyms for s in comparable[sy]]
        if not slugs:
            continue
        for leg, (feed, rule, guard, tw) in legs.items():
            arr = build_params(slugs, feed, rule, guard, tw, live)
            (work / f"params-{gname}-{leg}.json").write_text(json.dumps(arr, indent=1))
        print(f"      {gname:6s} {len(slugs):3d} window(s) x {len(legs)} leg(s)")

    # The as-armed reproduction leg, over exactly the windows the observer
    # arms were up for.
    arr = as_armed_params([s for sy in SYMS for s in comparable[sy]], arms_state)
    (work / "params-fleet-asarmed.json").write_text(json.dumps(arr, indent=1))
    print(f"      asarmed {len(arr)} window(s) at the live observer params")

    # Context leg: the binance posture over every graded 15m window the book
    # tape holds, corpus or no corpus. Not part of the A/B — the variants
    # cannot reach it — but it is the only read on the 15m book that is more
    # than one evening deep.
    arr = build_params([s for sy in SYMS for s in binance_set[sy]],
                       "binance", "range_avg", live_guard, 0.0, live)
    (work / "params-wide-base.json").write_text(json.dumps(arr, indent=1))
    arr = build_params([s for sy in SYMS for s in binance_set[sy]],
                       "binance", "terminal", live_guard, SETTLE_TW_S, live)
    (work / "params-wide-terminal.json").write_text(json.dumps(arr, indent=1))
    print(f"      wide    {sum(len(binance_set[s]) for s in SYMS)} graded window(s), "
          f"binance only")

    # Refusal census: every graded 15m window, stream-fed. Running it is how
    # the refusal count comes from the HARNESS rather than from this script.
    arr = build_params([s for sy in SYMS for s in binance_set[sy]],
                       "rtds", "range_avg", floor_guard, SETTLE_TW_S, live)
    (work / "params-census-rtds.json").write_text(json.dumps(arr, indent=1))

    print("[5/5] meta ...", flush=True)
    meta = {
        "corpus_files": files, "corpus_lines": lines,
        "corpus_span": span, "per_sym_span": per_sym,
        "study_span": [study_lo, study_hi],
        "recorder_holes": [{"t_last": x[0], "t_back": x[1], "down_s": x[2], "why": x[3]}
                           for x in gaps],
        "recorder_selflogged": [{"t_last": x[0], "t_back": x[1], "down_s": x[2], "why": x[3]}
                                for x in logged],
        "comparable": {s: comparable[s] for s in SYMS},
        "binance_set": {s: binance_set[s] for s in SYMS},
        "excluded": {s: dict(excluded[s]) for s in SYMS},
        "coverage": coverage,
        "gap_hit": {s: gap_hit[s] for s in SYMS},
        "guard_rows": guard_rows,
        "live_guard": live_guard, "stream_guard": stream_guard, "floor_guard": floor_guard,
        "parked_sizes": PARKED, "settle_tw_s": SETTLE_TW_S,
        "outcomes": {s: outcomes[s] for sy in SYMS for s in binance_set[sy]},
    }
    (work / "meta.json").write_text(json.dumps(meta, indent=1))
    print(f"      wrote {work / 'meta.json'}")
    return 0


# -------------------------------------------------------------- report


def load_rows(path):
    rows = {}
    with open(path) as fh:
        for line in fh:
            r = json.loads(line)
            slug = r.get("slug") or ""
            if "aggregate" in slug or not slug:
                continue
            rows[slug] = r
    return rows


def binom_p(better, worse):
    """Two-sided sign test on the windows a variant actually moved."""
    n = better + worse
    if n == 0:
        return 1.0
    c = lambda n, k: math.comb(n, k)
    tail = sum(c(n, k) for k in range(0, min(better, worse) + 1))
    return min(1.0, 2.0 * tail / (2.0 ** n))


def bootstrap(deltas, n=10000, seed=7):
    if not deltas:
        return 0.0, 0.0
    rnd = random.Random(seed)
    k = len(deltas)
    sums = sorted(sum(rnd.choice(deltas) for _ in range(k)) for _ in range(n))
    return sums[int(n * 0.025)], sums[int(n * 0.975)]


def leg_stats(rows, slugs):
    fired = clips = 0
    net = notional = 0.0
    w = l = 0
    for s in slugs:
        r = rows.get(s)
        if not r:
            continue
        sim = r["sim"]
        f = sim.get("fires") or 0
        clips += f
        pnl = sim.get("pnl") or 0.0
        notional += sim.get("notional") or 0.0
        net += pnl
        if f:
            fired += 1
            if pnl > 0:
                w += 1
            elif pnl < 0:
                l += 1
    return {"fired": fired, "clips": clips, "W": w, "L": l, "net": net,
            "notional": notional, "ron": (net / notional * 100.0) if notional else 0.0}


def cmd_report(a):
    work = pathlib.Path(a.work)
    meta = json.loads((work / "meta.json").read_text())
    comparable = meta["comparable"]
    legs = a.legs.split(",")

    for gname in a.groups.split(","):
        gsyms = SYMS if gname == "fleet" else (VERDICT_SYMS if gname == "pair" else [gname])
        slugs = [s for sy in gsyms for s in comparable.get(sy, [])]
        avail = {}
        for leg in legs:
            p = work / f"out-{gname}-{leg}.jsonl"
            if p.exists():
                avail[leg] = load_rows(p)
        if not avail:
            continue
        shared = [s for s in slugs if all(s in r for r in avail.values())]
        print(f"\n{'=' * 100}")
        print(f"{gname.upper()}  —  {len(shared)} graded 15m window(s) "
              f"({', '.join(gsyms)}), fleet-cap {a.cap}")
        print("=" * 100)
        hdr = (f"{'leg':>16} {'fired':>6} {'clips':>6} {'W-L':>7} {'net $':>10} "
               f"{'notional $':>11} {'RoN':>8} {'Δnet':>10} {'Δ ex-top':>10} "
               f"{'b/w':>7} {'sign p':>7}")
        print(hdr + "\n" + "-" * len(hdr))
        base = avail.get(a.baseline)
        base_st = leg_stats(base, shared) if base else None
        for leg in legs:
            rows = avail.get(leg)
            if not rows:
                continue
            st = leg_stats(rows, shared)
            dn = dx = float("nan")
            bw = ""
            p = float("nan")
            if base is not None and leg != a.baseline:
                deltas = [((rows[s]["sim"]["pnl"] or 0.0) - (base[s]["sim"]["pnl"] or 0.0))
                          for s in shared]
                dn = sum(deltas)
                moved = [d for d in deltas if abs(d) > 1e-9]
                top = max(moved, key=abs) if moved else 0.0
                dx = dn - top
                better = sum(1 for d in moved if d > 0)
                worse = len(moved) - better
                bw = f"{better}/{worse}"
                p = binom_p(better, worse)
            print(f"{leg:>16} {st['fired']:>6} {st['clips']:>6} "
                  f"{st['W']}-{st['L']:<5} {st['net']:>+10.2f} {st['notional']:>11.2f} "
                  f"{st['ron']:>+7.2f}% "
                  f"{('' if math.isnan(dn) else f'{dn:+.2f}'):>10} "
                  f"{('' if math.isnan(dx) else f'{dx:+.2f}'):>10} "
                  f"{bw:>7} {('' if math.isnan(p) else f'{p:.3f}'):>7}")
        if base_st is None:
            continue
        # Per-window detail for the legs that moved anything.
        for leg in legs:
            if leg == a.baseline or leg not in avail:
                continue
            rows = avail[leg]
            deltas = [(s, (rows[s]["sim"]["pnl"] or 0.0) - (base[s]["sim"]["pnl"] or 0.0))
                      for s in shared]
            moved = [(s, d) for s, d in deltas if abs(d) > 1e-9]
            if not moved:
                continue
            lo, hi = bootstrap([d for _, d in deltas])
            print(f"\n  {leg}: {len(moved)} window(s) moved; "
                  f"bootstrap CI95 [{lo:+.0f}, {hi:+.0f}]")
            for s, d in sorted(moved, key=lambda x: x[1]):
                print(f"    {s:32s} {d:+9.2f}   base={base[s]['sim']['pnl'] or 0:+8.2f} "
                      f"{leg}={rows[s]['sim']['pnl'] or 0:+8.2f}  "
                      f"fires {base[s]['sim']['fires']}->{rows[s]['sim']['fires']}  "
                      f"won={rows[s]['sim']['outcome']}")
    return 0



# --------------------------------------------------------------- depth

def cmd_depth(a):
    """Where the terminal rule's evidence arrives, and whether anything is
    still on offer when it does.

    `terminal_lock` banks NOTHING while `rem > tw`: before the settlement
    TWAP starts forming there is nothing locked, so `banked_margin_bp` is
    identically 0 and `side_safety` is identically 0. The theta gate
    (`safety_gate_blocks`) refuses the FIRST clip of a window whenever
    `safety < theta`, so a terminal arm at the deployed theta=0.3 cannot
    open a position until `rem <= 60s`. This measures what the book is
    doing in that last minute.
    """
    work = pathlib.Path(a.work)
    meta = json.loads((work / "meta.json").read_text())
    study = {s for v in meta["comparable"].values() for s in v}
    winner = {k: v["winner"] for k, v in meta["outcomes"].items()}

    samples, _, _, _ = load_corpus(a.rtds_dir)
    twap = {s: StepSeries(samples[s][TOPIC_TWAP60]) for s in SYMS}
    spot = {s: StepSeries(samples[s][TOPIC_SPOT]) for s in SYMS}

    rows = defaultdict(list)
    with open(a.book_tape) as fh:
        for line in fh:
            if "-15m-" not in line:
                continue
            try:
                r = json.loads(line)
            except Exception:
                continue
            if (r.get("slug") or "") in study:
                rows[r["slug"]].append(r)

    def bucket(rem):
        if rem <= 60.0:
            return "rem<=60  settlement window"
        if rem <= 120.0:
            return "60<rem<=120  late unlock"
        if rem <= 300.0:
            return "120<rem<=300"
        return "rem>300"

    order = ["rem>300", "120<rem<=300", "60<rem<=120  late unlock",
             "rem<=60  settlement window"]
    st = defaultdict(lambda: defaultdict(int))
    term_win = defaultdict(set)
    for slug, rs in rows.items():
        sym = slug.split("-")[0]
        start = float(slug.rsplit("-", 1)[1])
        end = start + DUR_S
        ref, _ = twap[sym].at(start - 60.0)
        won = winner.get(slug)
        for r in rs:
            rem = end - r["t"]
            b = bucket(rem)
            c = st[b]
            c["ticks"] += 1
            wa = r["up_ask"] if won == "up" else r["dn_ask"]
            if wa is None:
                c["winner_unbuyable"] += 1
            else:
                c["winner_ask"] += 1
                if wa <= 0.985:
                    c["winner_ask_le_max"] += 1
                if wa <= 0.955:
                    c["winner_ask_le_955"] += 1
            # The side the TERMINAL margin points at on this tick, and whether
            # the book is offering it at all.
            sp, spt = spot[sym].at(r["t"])
            if ref and sp and r["t"] - spt <= 5.0:
                side = "up" if sp >= ref else "down"
                ta = r["up_ask"] if side == "up" else r["dn_ask"]
                c["margin_ticks"] += 1
                if ta is not None and ta <= 0.985:
                    c["margin_side_buyable"] += 1
                    if b.startswith("rem<=60"):
                        term_win[slug].add(side)
    print(f"{'bucket':30s} {'ticks':>7} {'winner ask':>16} {'<=0.985':>14} "
          f"{'<=0.955':>14} {'terminal side buyable':>24}")
    for b in order:
        c = st[b]
        n = c["ticks"] or 1
        m = c["margin_ticks"] or 1
        print(f"{b:30s} {c['ticks']:>7} "
              f"{c['winner_ask']:>7} ({c['winner_ask'] / n * 100:4.1f}%) "
              f"{c['winner_ask_le_max']:>6} ({c['winner_ask_le_max'] / n * 100:4.1f}%) "
              f"{c['winner_ask_le_955']:>6} ({c['winner_ask_le_955'] / n * 100:4.1f}%) "
              f"{c['margin_side_buyable']:>13} ({c['margin_side_buyable'] / m * 100:4.1f}% of {c['margin_ticks']})")
    print(f"\nwindows whose TERMINAL side is buyable at all inside rem<=60: "
          f"{len(term_win)} of {len(rows)}")
    for slug in sorted(term_win):
        print(f"    {slug}  side(s) {sorted(term_win[slug])}  won={winner.get(slug)}")
    return 0


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("survey")
    p.add_argument("--work", required=True)
    p.add_argument("--book-tape", default=str(PMT / "engine" / "book-tape.jsonl"))
    p.add_argument("--arms-state", default=str(PMT / "engine" / "arms-state.json"))
    p.add_argument("--outcomes", default=str(CORPUS / "outcomes.jsonl"))
    p.add_argument("--rtds-dir", default=str(CORPUS / "rtds"))
    p.set_defaults(fn=cmd_survey)

    p = sub.add_parser("report")
    p.add_argument("--work", required=True)
    p.add_argument("--groups", default="fleet,pair,btc,eth,sol")
    p.add_argument("--legs", default="base,rtds_range_avg,rtds_hybrid,rtds_terminal")
    p.add_argument("--baseline", default="base")
    p.add_argument("--cap", default="500")
    p.set_defaults(fn=cmd_report)

    p = sub.add_parser("depth")
    p.add_argument("--work", required=True)
    p.add_argument("--book-tape", default=str(PMT / "engine" / "book-tape.jsonl"))
    p.add_argument("--rtds-dir", default=str(CORPUS / "rtds"))
    p.set_defaults(fn=cmd_depth)

    a = ap.parse_args()
    sys.exit(a.fn(a))


if __name__ == "__main__":
    main()
