#!/usr/bin/env python3
"""Feed A/B (binance vs rtds) for the 5m fleet — corpus survey, guard sizing,
and the `--params` files the replay harness runs.

Read-only against ~/.pmt. Writes only under the directory given by --work.

Three jobs:

  survey   what the RTDS recorder corpus actually covers, and which 5m
           windows are therefore comparable between the two feeds. A
           stream-fed window with no corpus behind it is REFUSED by replay
           (by design), so an A/B that let the baseline run windows the
           variant cannot is not an A/B.

  guard    size a stream-appropriate `basis_guard_bp`. The deployed binance
           guards were sized at ~p90 of the disagreement, at one instant,
           between the series the arm prices off and the series the market
           settles on (analysis/chainlink_stream_scout.md §"Rung 4's margin
           error"). On rtds the venue term of that disagreement is
           identically zero — the arm reads the settlement series. What is
           left is the arm's one remaining substitution: it reads Chainlink
           SPOT (`crypto_prices_chainlink`) where the settlement quantity is
           the 60s TWAP (`crypto_prices_twap_sixty`). Measure that, the same
           way, same clock, same estimator — and measure the binance version
           of the identical quantity as the control that validates the
           method against the guards already deployed.

  params   emit the baseline / variant params arrays.

Usage:
  uv run --project pmtrader python analysis/feed_ab.py --work <dir>
"""

import argparse
import json
import math
import os
import pathlib
import sys
from collections import defaultdict

PMT = pathlib.Path(os.environ.get("PMT_HOME", pathlib.Path.home() / ".pmt"))
CORPUS = PMT / "corpus"
RTDS_FILE_GLOB = "rtds-*.jsonl"
SYMS = ["btc", "eth", "sol", "bnb", "xrp"]
BIN_SYMBOL = {s: f"{s.upper()}USDT" for s in SYMS}
RTDS_SYMBOL = {s: f"{s}/usd" for s in SYMS}
TOPIC_TWAP60 = "crypto_prices_twap_sixty"
TOPIC_SPOT = "crypto_prices_chainlink"


def pct(xs, q):
    if not xs:
        return float("nan")
    xs = sorted(xs)
    i = min(len(xs) - 1, max(0, int(math.ceil(q * len(xs))) - 1))
    return xs[i]


def med(xs):
    return pct(xs, 0.5)


# ---------------------------------------------------------------- corpus


def load_corpus():
    """(samples, gaps). samples[sym][topic] = [(t_recv, ts_s, value)] sorted."""
    files = sorted((CORPUS / "rtds").glob(RTDS_FILE_GLOB))
    if not files:
        sys.exit(f"no {RTDS_FILE_GLOB} under {CORPUS/'rtds'}")
    want_sym = set(RTDS_SYMBOL.values())
    samples = {s: {TOPIC_TWAP60: [], TOPIC_SPOT: []} for s in SYMS}
    inv = {v: k for k, v in RTDS_SYMBOL.items()}
    gaps = []
    lines = 0
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
                if not any(s in line for s in want_sym):
                    continue
                d = json.loads(line)
                sym = inv.get(d.get("symbol"))
                if sym is None:
                    continue
                topic = d.get("topic")
                if topic not in (TOPIC_TWAP60, TOPIC_SPOT):
                    continue
                samples[sym][topic].append((d["t_recv"], d["ts"] / 1000.0, d["value"]))
    for sym in SYMS:
        for t in samples[sym]:
            samples[sym][t].sort(key=lambda r: r[0])
    return samples, gaps, lines, [str(f) for f in files]


def corpus_span(samples):
    lo, hi = -math.inf, math.inf
    per = {}
    for sym in SYMS:
        a = samples[sym][TOPIC_TWAP60]
        b = samples[sym][TOPIC_SPOT]
        if not a or not b:
            per[sym] = None
            continue
        s = (max(a[0][0], b[0][0]), min(a[-1][0], b[-1][0]))
        per[sym] = s
        lo, hi = max(lo, s[0]), min(hi, s[1])
    return (lo, hi), per


# ------------------------------------------------------------- book tape


def load_book_windows(book_tape):
    """5m windows present in the book tape: slug -> (n_records, t_lo, t_hi)."""
    out = {}
    with open(book_tape) as fh:
        for line in fh:
            if "-5m-" not in line:
                continue
            try:
                d = json.loads(line)
            except Exception:
                continue
            slug = d.get("slug")
            if not slug or "-5m-" not in slug:
                continue
            t = d.get("t")
            if t is None:
                continue
            e = out.get(slug)
            if e is None:
                out[slug] = [1, t, t]
            else:
                e[0] += 1
                e[1] = min(e[1], t)
                e[2] = max(e[2], t)
    return out


def load_book_spot(book_tape):
    """sym -> [(t, spot)] from the recorder's own binance stamp, sorted."""
    out = defaultdict(list)
    with open(book_tape) as fh:
        for line in fh:
            if "-5m-" not in line:
                continue
            try:
                d = json.loads(line)
            except Exception:
                continue
            slug = d.get("slug") or ""
            sym = slug.split("-")[0]
            if sym not in SYMS:
                continue
            spot = d.get("spot")
            t = d.get("t")
            if not spot or t is None:
                continue
            age = d.get("spot_age_s") or 0.0
            out[sym].append((t - age, spot))
    for s in out:
        out[s].sort()
    return out


# ------------------------------------------------------- guard sizing


class StepSeries:
    """Last value received at or before t — the shape a live consumer holds."""

    def __init__(self, rows):
        self.t = [r[0] for r in rows]
        self.v = [r[2] if len(r) > 2 else r[1] for r in rows]
        self.i = 0

    def at(self, t):
        import bisect

        j = bisect.bisect_right(self.t, t) - 1
        if j < 0:
            return None, None
        return self.v[j], self.t[j]


def guard_survey(samples, book_spot, span, gaps, stale_s=5.0):
    """Per symbol, three disagreements in bp, on one clock.

      rtds     |chainlink spot - twap60|  — what a STREAM-fed arm still
               substitutes: it prices the remaining path off the settlement
               object's instantaneous value where the settlement quantity is
               that value's 60s trailing mean.
      binance  |binance spot  - twap60|   — the same substitution PLUS the
               cross-venue basis. This is the quantity
               analysis/chainlink_stream_scout.md sized the deployed guards
               against; reproducing it here is what validates the method.
      venue    |binance spot - chainlink spot| — the pure cross-venue term,
               i.e. exactly the part the migration deletes.

    Sampled on the RTDS spot topic's own 1 Hz clock inside `span`, which is
    the clock a stream-fed arm actually reads. A sample whose counterpart is
    older than the engine's own staleness tolerance is dropped rather than
    counted — a live arm gates there instead of pricing, so counting it as
    basis error would charge the guard for something the stale gate already
    refuses. Recorder-gap intervals are dropped for the same reason.
    """
    lo, hi = span
    gap_iv = sorted((a, b) for (a, b, _, _) in gaps)

    def in_gap(t):
        import bisect

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


def relay_survey(samples, span):
    """Relay lag (t_recv - ts) and the bp it costs.

    `lag_s` is how far behind observation time the stream runs. `lag_bp` is
    what that lag is worth: the twap60 print in hand at t against the print
    stamped for t, which is the only error a stream-fed arm's REFERENCE and
    banked marks can carry (they are the settlement values themselves).
    """
    lo, hi = span
    out = {}
    for sym in SYMS:
        rows = [r for r in samples[sym][TOPIC_TWAP60] if lo <= r[0] <= hi]
        lag = [t - ts for (t, ts, _) in rows]
        by_ts = sorted((ts, v) for (_t, ts, v) in rows)
        ts_list = [r[0] for r in by_ts]
        val = [r[1] for r in by_ts]
        held = StepSeries(rows)
        bp = []
        import bisect

        for (t_recv, _ts, _v) in rows:
            j = bisect.bisect_right(ts_list, t_recv) - 1
            if j < 0:
                continue
            true_v = val[j]
            got, _ = held.at(t_recv)
            if got and true_v:
                bp.append(abs(got / true_v - 1.0) * 1e4)
        out[sym] = {"lag_s": lag, "lag_bp": bp}
    return out


# ------------------------------------------------------------ params


def window_bounds(slug):
    start = float(slug.rsplit("-", 1)[1])
    return start, start + 300.0


def as_armed_sizes(tape):
    """Per-window `size_usdc` / `clip_usdc` as they actually ran.

    `size` is read straight off the tape's own `roll` record — the size the
    strategy re-armed that window with. `clip` is not recorded, so it is
    read back off the fires: a clip fires at `clip_usdc` unless the window's
    remaining budget truncates it, so the level in force is the MAX fire
    notional in a neighbourhood, not the max in the window. Both
    forward-fill, because both are step functions an operator moves.
    """
    size = defaultdict(dict)   # sym -> {start: size}
    firemax = defaultdict(dict)  # sym -> {start: max fire notional}
    with open(tape) as fh:
        for line in fh:
            if "-5m-" not in line:
                continue
            try:
                d = json.loads(line)
            except Exception:
                continue
            slug = d.get("slug") or ""
            sym = slug.split("-")[0]
            if sym not in SYMS or "-updown-5m-" not in slug:
                continue
            start = float(slug.rsplit("-", 1)[1])
            ev = d.get("ev")
            if ev == "roll" and d.get("size"):
                size[sym][start] = float(d["size"])
            elif ev == "fire":
                n = float(d.get("ask") or 0) * float(d.get("size") or 0)
                if n > 0:
                    firemax[sym][start] = max(firemax[sym].get(start, 0.0), n)
    return size, firemax


def _nearest_ff(table, t):
    """Forward-filled step lookup, falling back to the earliest entry."""
    if not table:
        return None
    ks = sorted(table)
    prev = [k for k in ks if k <= t]
    return table[prev[-1]] if prev else table[ks[0]]


def clip_by_size_level(size_tbl, firemax, arms_state):
    """clip in force at each size level, read off that level's own fires.

    Size and clip are one operator action, so the level is keyed on the
    size it shipped with. Inside a level the max fire notional IS the clip
    (a smaller fire is a budget-truncated one); across a level it is stable.
    A level that never fired falls back to the live clip/size ratio.
    """
    ratio = {}
    for a in arms_state["arms"]:
        if "-updown-5m-" in a["slug"]:
            ratio[a["slug"].split("-")[0]] = a["clip_usdc"] / max(a["size_usdc"], 1e-9)
    out = defaultdict(dict)
    for sym in SYMS:
        by_level = defaultdict(float)
        for start, sz in size_tbl.get(sym, {}).items():
            n = firemax.get(sym, {}).get(start)
            if n:
                by_level[sz] = max(by_level[sz], n)
        for sz in {v for v in size_tbl.get(sym, {}).values()}:
            out[sym][sz] = by_level.get(sz) or sz * ratio.get(sym, 0.1)
    return out


def build_params(arms_state, slugs, feed, settle_tw_s, guard_by_sym, size_tbl, clip_lvl):
    """One params entry per window slug, off the live per-symbol arm."""
    live = {}
    for a in arms_state["arms"]:
        s = a["slug"]
        if "-updown-5m-" not in s:
            continue
        live[s.split("-")[0]] = a
    out = []
    for slug in sorted(slugs, key=lambda s: (float(s.rsplit("-", 1)[1]), s)):
        sym = slug.split("-")[0]
        base = live.get(sym)
        if base is None:
            continue
        p = dict(base)
        start, end = window_bounds(slug)
        p["slug"] = slug
        p["start"] = start
        p["end"] = end
        # Token ids are per-window and replay never places an order; the
        # book view is joined on the tape's own sides, not on these. Keep
        # them slug-unique so nothing can collide across windows.
        p["token_up"] = f"{slug}-up"
        p["token_down"] = f"{slug}-down"
        p["feed"] = feed
        sz = _nearest_ff(size_tbl.get(sym, {}), start)
        if sz:
            p["size_usdc"] = sz
            cl = clip_lvl.get(sym, {}).get(sz)
            if cl:
                p["clip_usdc"] = round(cl, 2)
        if feed == "rtds":
            p["settle_tw_s"] = settle_tw_s
        if guard_by_sym is not None:
            p["basis_guard_bp"] = guard_by_sym[sym]
        out.append(p)
    return out


# -------------------------------------------------------------- main


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--work", required=True)
    ap.add_argument("--book-tape", default=str(PMT / "engine" / "book-tape.jsonl"))
    ap.add_argument("--arms-state", default=str(PMT / "engine" / "arms-state.json"))
    ap.add_argument("--outcomes", default=str(CORPUS / "outcomes.jsonl"))
    ap.add_argument("--settle-tw", type=float, default=60.0)
    args = ap.parse_args()

    work = pathlib.Path(args.work)
    work.mkdir(parents=True, exist_ok=True)

    print("[1/4] loading RTDS corpus ...", flush=True)
    samples, gaps, lines, files = load_corpus()
    span, per_sym_span = corpus_span(samples)
    print(f"      {lines} line(s) over {len(files)} file(s); {len(gaps)} recorder gap(s), "
          f"{sum(g[2] for g in gaps):.0f}s total")
    print(f"      common span {span[0]:.0f}..{span[1]:.0f}")
    for s in SYMS:
        a, b = per_sym_span[s]
        print(f"      {s:4s} twap60={len(samples[s][TOPIC_TWAP60]):6d} "
              f"spot={len(samples[s][TOPIC_SPOT]):6d}  {a:.0f}..{b:.0f}")

    print("[2/4] scanning book tape ...", flush=True)
    bw = load_book_windows(args.book_tape)
    book_spot = load_book_spot(args.book_tape)
    outcomes = {}
    for line in open(args.outcomes):
        d = json.loads(line)
        outcomes[d["slug"]] = d
    print(f"      {len(bw)} 5m window(s) in the book tape, {len(outcomes)} graded outcome(s)")

    # Comparable set: in the book tape, graded, and fully inside the corpus
    # (start..end, plus the 60s reference print before it).
    comparable = defaultdict(list)
    excluded = defaultdict(lambda: defaultdict(list))
    for slug, (n, tlo, thi) in sorted(bw.items()):
        sym = slug.split("-")[0]
        if sym not in SYMS:
            continue
        start, end = window_bounds(slug)
        a, b = per_sym_span[sym]
        if start < a or end > b:
            excluded[sym]["outside_corpus"].append(slug)
            continue
        if slug not in outcomes:
            excluded[sym]["ungraded"].append(slug)
            continue
        comparable[sym].append(slug)
    for sym in SYMS:
        ex = excluded[sym]
        print(f"      {sym:4s} comparable={len(comparable[sym]):4d}  "
              f"outside_corpus={len(ex['outside_corpus']):4d} ungraded={len(ex['ungraded']):3d}")

    # Windows a recorder gap overlaps — the recorder is a SECOND subscriber,
    # so its dropped samples are not the engine's, and any stream-side
    # gating inside these windows is a corpus artefact.
    gap_hit = defaultdict(list)
    for sym in SYMS:
        for slug in comparable[sym]:
            start, end = window_bounds(slug)
            for (a, b, ds, reason) in gaps:
                if a < end and b > start:
                    gap_hit[sym].append((slug, round(ds, 1), reason))
                    break

    print("[3/4] sizing the stream guard ...", flush=True)
    gs = guard_survey(samples, book_spot, span, gaps)
    rl = relay_survey(samples, span)
    guard_rows = []
    for sym in SYMS:
        r = gs[sym]
        row = {"sym": sym}
        for k in ("rtds", "binance", "venue"):
            xs = r[k]
            row[k] = {
                "n": len(xs),
                "med": med(xs),
                "p90": pct(xs, 0.90),
                "p99": pct(xs, 0.99),
                "max": max(xs) if xs else float("nan"),
            }
        row["relay"] = {
            "n": len(rl[sym]["lag_s"]),
            "lag_med_s": med(rl[sym]["lag_s"]),
            "lag_p95_s": pct(rl[sym]["lag_s"], 0.95),
            "lag_bp_med": med(rl[sym]["lag_bp"]),
            "lag_bp_p90": pct(rl[sym]["lag_bp"], 0.90),
        }
        guard_rows.append(row)
        for k in ("rtds", "binance", "venue"):
            c = row[k]
            print(f"      {sym:4s} {k:8s} n={c['n']:6d} med={c['med']:6.2f} "
                  f"p90={c['p90']:6.2f} p99={c['p99']:6.2f} max={c['max']:8.2f}")
        rr = row["relay"]
        print(f"      {sym:4s} relay    lag med={rr['lag_med_s']:.2f}s p95={rr['lag_p95_s']:.2f}s "
              f"-> bp med={rr['lag_bp_med']:.3f} p90={rr['lag_bp_p90']:.3f}")

    # The deployed guards round p90 to a whole bp. Keep that grain.
    stream_guard = {r["sym"]: float(math.ceil(r["rtds"]["p90"])) for r in guard_rows}
    print(f"      stream guard (ceil p90): {stream_guard}")

    print("[4/4] writing params ...", flush=True)
    arms_state = json.load(open(args.arms_state))
    live_guard = {}
    for a in arms_state["arms"]:
        if "-updown-5m-" in a["slug"]:
            live_guard[a["slug"].split("-")[0]] = a["basis_guard_bp"]
    size_tbl, firemax = as_armed_sizes(str(PMT / "engine" / "updown-tape.jsonl"))
    clip_lvl = clip_by_size_level(size_tbl, firemax, arms_state)

    variants = {
        "base": ("binance", None),
        "rtds_liveguard": ("rtds", None),
        "rtds_streamguard": ("rtds", stream_guard),
    }
    armed = {}
    fleet = defaultdict(list)
    for sym in SYMS:
        slugs = comparable[sym]
        if not slugs:
            continue
        for name, (feed, guard) in variants.items():
            arr = build_params(arms_state, slugs, feed, args.settle_tw, guard, size_tbl, clip_lvl)
            (work / f"params-{sym}-{name}.json").write_text(json.dumps(arr, indent=1))
            fleet[name].extend(arr)
            if name == "base":
                armed[sym] = {
                    p["slug"]: {"size": p["size_usdc"], "clip": p["clip_usdc"]} for p in arr
                }
        lv = sorted({(v["size"], v["clip"]) for v in armed[sym].values()})
        print(f"      {sym}: {len(slugs)} window(s) x 3 variant(s); size/clip levels {lv}")
    for name, arr in fleet.items():
        (work / f"params-fleet-{name}.json").write_text(json.dumps(arr, indent=1))
    print(f"      fleet: {len(fleet['base'])} window(s) x 3 variant(s)")

    # Refusal census: every graded 5m window in the book tape, stream-fed —
    # including the ones the corpus does not reach. Running this is how the
    # count of refusals comes from the harness rather than from this script.
    allwin = [s for s in bw if s.split("-")[0] in SYMS and s in outcomes]
    arr = build_params(arms_state, allwin, "rtds", args.settle_tw, stream_guard,
                       size_tbl, clip_lvl)
    (work / "params-fleet-allwindows_rtds.json").write_text(json.dumps(arr, indent=1))
    arr = build_params(arms_state, allwin, "binance", args.settle_tw, None, size_tbl, clip_lvl)
    (work / "params-fleet-allwindows_base.json").write_text(json.dumps(arr, indent=1))
    print(f"      refusal census set: {len(allwin)} graded window(s)")

    meta = {
        "corpus_files": files,
        "corpus_lines": lines,
        "corpus_span": span,
        "per_sym_span": per_sym_span,
        "recorder_gaps": [
            {"t_last": a, "t_back": b, "down_s": ds, "reason": r} for (a, b, ds, r) in gaps
        ],
        "comparable": {s: comparable[s] for s in SYMS},
        "excluded": {s: {k: v for k, v in excluded[s].items()} for s in SYMS},
        "gap_hit": {s: gap_hit[s] for s in SYMS},
        "guard_rows": guard_rows,
        "stream_guard": stream_guard,
        "live_guard": live_guard,
        "as_armed": armed,
        "settle_tw_s": args.settle_tw,
    }
    (work / "meta.json").write_text(json.dumps(meta, indent=1))
    print(f"      wrote {work/'meta.json'}")


if __name__ == "__main__":
    main()
