#!/usr/bin/env python3
"""15m stream-fed settle-rule A/B — params, preflight, aggregation.

The A/B analysis/hybrid_ab.md said to run once the stream-fed model exists:
`settle_rule` hybrid vs range_avg on 15m windows, `feed=rtds`, replayed
`--mode full` so the book, the exits and the fill sim are all real.

Three subcommands, driven by analysis/fifteen_stream_ab.sh:

  preflight  Does a runnable A/B exist at all? `--mode full` iterates BOOK-TAPE
             windows and a `feed=rtds` window additionally needs the RTDS corpus
             to cover [start, end] (pmengine replay/rtds.rs RtdsTimeline::build
             refuses anything shorter rather than replaying off klines). The
             answer is the INTERSECTION of those two tapes, and it is allowed to
             be empty — an empty intersection is the finding, not a failure to
             route around. Exits 2 when it is.
  params     Per-slug ArmParams for one settle_rule, 15m only.
  report     Δnet between two replay --out files, with a bootstrap CI over
             windows (analysis/aggression_sweep.md's method: only the DELTA is
             read, never the sim's absolute pnl).

Usage:  cd pmtrader && uv run python ../analysis/fifteen_stream_ab.py preflight
"""
import argparse
import json
import os
import random
import sys

BOOK = os.path.expanduser("~/.pmt/engine/book-tape.jsonl")
RTDS = os.path.expanduser("~/.pmt/corpus/rtds")
OUTCOMES = os.path.expanduser("~/.pmt/corpus/outcomes.jsonl")

# 15m-parked-era fleet sizes and the live per-symbol static guards.
SYMBOLS = {
    "btc": dict(symbol="BTCUSDT", size=400.0, clip=50.0, guard=6.0, sigma=9.0),
    "eth": dict(symbol="ETHUSDT", size=300.0, clip=40.0, guard=6.0, sigma=25.0),
    "sol": dict(symbol="SOLUSDT", size=200.0, clip=25.0, guard=10.0, sigma=12.0),
}
THETA = 0.3
DUR_S = 900


def utc(t):
    import datetime
    return datetime.datetime.fromtimestamp(t, datetime.timezone.utc).strftime("%H:%M:%SZ")


# --- tapes -------------------------------------------------------------

def book_windows(path, dur_tag="-15m-"):
    """{slug: (first_t, last_t, records)} for every window the book tape holds."""
    out = {}
    for line in open(path):
        try:
            r = json.loads(line)
        except ValueError:
            continue
        slug = r.get("slug") or ""
        if dur_tag not in slug:
            continue
        t = r.get("t")
        if t is None:
            continue
        a = out.setdefault(slug, [t, t, 0])
        a[0] = min(a[0], t)
        a[1] = max(a[1], t)
        a[2] += 1
    return out


def rtds_spans(path, symbols):
    """{rtds_symbol: (first_t_recv, last_t_recv, n)} — the span
    RtdsTimeline::build checks a window against."""
    files = ([os.path.join(path, f) for f in sorted(os.listdir(path))
              if f.startswith("rtds-") and f.endswith(".jsonl")]
             if os.path.isdir(path) else [path])
    needles = {s: f'"symbol": "{s}"' for s in symbols}
    span = {}
    for fp in files:
        for line in open(fp):
            for sym, needle in needles.items():
                if needle not in line:
                    continue
                try:
                    t = json.loads(line)["t_recv"]
                except (ValueError, KeyError):
                    break
                a = span.setdefault(sym, [t, t, 0])
                a[0] = min(a[0], t)
                a[1] = max(a[1], t)
                a[2] += 1
                break
    return span, files


def slug_parts(slug):
    coin, _, dur, start = slug.split("-")
    return coin, int(dur.rstrip("m")) * 60, int(start)


# --- preflight ---------------------------------------------------------

def cmd_preflight(a):
    wins = book_windows(a.book, a.dur)
    syms = {f"{c}/usd" for c in SYMBOLS}
    span, files = rtds_spans(a.rtds, syms)

    tag = a.dur.strip("-")
    print(f"book tape   {a.book}")
    if not wins:
        print(f"            NO {tag} windows at all — the engine never subscribed a {tag} book.")
    else:
        lo = min(v[0] for v in wins.values())
        hi = max(v[1] for v in wins.values())
        print(f"            {len(wins)} {tag} windows, {utc(lo)} -> {utc(hi)}")
    print(f"rtds corpus {a.rtds}  ({len(files)} file(s))")
    for sym in sorted(span):
        f, l, n = span[sym]
        print(f"            {sym:9s} {utc(f)} -> {utc(l)}  ({n:,} samples)")

    runnable, reasons = [], []
    for slug in sorted(wins):
        try:
            coin, dur, start = slug_parts(slug)
        except ValueError:
            continue
        if coin not in SYMBOLS:
            continue
        end = start + dur
        sym = f"{coin}/usd"
        s = span.get(sym)
        if not s:
            reasons.append((slug, f"rtds corpus carries no {sym}"))
        elif s[0] > start or s[1] < end:
            reasons.append((slug, f"rtds corpus {utc(s[0])}..{utc(s[1])} misses "
                                  f"{utc(start)}..{utc(end)} — short by "
                                  f"{max(s[0] - start, 0):.0f}s at the start, "
                                  f"{max(end - s[1], 0):.0f}s at the end"))
        else:
            runnable.append(slug)

    print(f"\nrunnable {tag} windows (book AND rtds): {len(runnable)}")
    for slug in runnable:
        print(f"  {slug}")
    if not runnable:
        gap = None
        if wins and span:
            book_hi = max(v[1] for v in wins.values())
            rtds_lo = min(v[0] for v in span.values())
            gap = rtds_lo - book_hi
        print("\n  NONE. The A/B cannot run — this is a tape-coverage fact, not a bug.")
        if gap is not None:
            print(f"  {tag} book coverage ends {utc(book_hi)}; the RTDS corpus begins "
                  f"{utc(rtds_lo)} — a {gap / 60:.0f}min hole with no overlap. 15m is parked,")
            print(f"  so the engine stopped subscribing {tag} books before the recorder started.")
        print(f"\n  To get a runnable A/B, the engine must SUBSCRIBE {tag} books while the RTDS")
        print("  recorder runs. It does not need to trade them: a books-only observer arm")
        print("  (--size 0, or an arm whose gates can never fire) subscribes the tokens and")
        print("  feeds book-tape.jsonl, which is the only missing half. Both tapes then cover")
        print("  the same wall-clock hours and this preflight goes green.")
        for slug, why in reasons[:6]:
            print(f"    - {slug}: {why}")
        if len(reasons) > 6:
            print(f"    ... and {len(reasons) - 6} more")
        return 2
    return 0


# --- params ------------------------------------------------------------

def cmd_params(a):
    wins = book_windows(a.book, a.dur)
    entries = []
    for slug in sorted(wins):
        try:
            coin, dur, start = slug_parts(slug)
        except ValueError:
            continue
        cfg = SYMBOLS.get(coin)
        if not cfg:
            continue
        entries.append({
            "slug": slug, "kind": "twap", "symbol": cfg["symbol"],
            # Book-tape replay keys on slug; token ids are cosmetic offline.
            "token_up": f"{slug}-u", "token_down": f"{slug}-d",
            "start": float(start), "end": float(start + dur),
            "sigma_bp_per_min": cfg["sigma"],   # cold-start fallback only
            "fee_rate": 0.07,
            "size_usdc": cfg["size"], "clip_usdc": cfg["clip"],
            "min_edge": 0.015, "max_price": 0.985, "quiesce_secs": 20.0,
            "basis_guard_bp": cfg["guard"], "side_filter": None,
            "min_fair": 0.97, "min_elapsed_frac": 0.0, "clip_cooldown_s": 2.0,
            "early_frac": 0.2, "early_min_edge": 0.08,
            # 120s on a 900s window is 13% of it, not the 40% it is at 5m.
            # Left at the live value on purpose: this A/B varies settle_rule
            # and nothing else, so late_rem_s is a SEPARATE sweep.
            "late_rem_s": 120.0,
            "rho_block": -0.25, "pay_up_max": 0.02, "p_cap": 1.0,
            "theta": THETA, "settle_rule": a.rule, "manip_push_bp": 25.0,
            "roll": False, "feed": "rtds", "maker_bid": False,
        })
    with open(a.out, "w") as fh:
        json.dump(entries, fh)
    print(f"[params] {len(entries)} {a.dur.strip('-')} windows, settle_rule={a.rule} "
          f"-> {a.out}", file=sys.stderr)
    return 0


# --- report ------------------------------------------------------------

def load_report(path):
    rows = {}
    for line in open(path):
        r = json.loads(line)
        if "aggregate" in r.get("slug", ""):
            continue
        rows[r["slug"]] = r
    return rows


def bootstrap(deltas, n=10000, seed=7):
    if not deltas:
        return 0.0, 0.0
    rnd = random.Random(seed)
    k = len(deltas)
    sums = sorted(sum(rnd.choice(deltas) for _ in range(k)) for _ in range(n))
    return sums[int(n * 0.025)], sums[int(n * 0.975)]


def cmd_report(a):
    base, var = load_report(a.a), load_report(a.b)
    shared = sorted(set(base) & set(var))
    if not shared:
        print(f"no windows in common between {a.a} and {a.b} — nothing to compare")
        return 2

    def tot(rows, k):
        return sum(rows[s]["sim"][k] or 0.0 for s in shared)

    def fires(rows):
        return sum(rows[s]["sim"]["fires"] for s in shared)

    def wl(rows):
        w = sum(1 for s in shared
                if (rows[s]["sim"]["pnl"] or 0.0) > 0 and rows[s]["sim"]["fires"])
        l = sum(1 for s in shared
                if (rows[s]["sim"]["pnl"] or 0.0) < 0 and rows[s]["sim"]["fires"])
        return f"{w}-{l}"

    deltas = [(var[s]["sim"]["pnl"] or 0.0) - (base[s]["sim"]["pnl"] or 0.0) for s in shared]
    lo, hi = bootstrap(deltas)
    durs = sorted({s.split("-")[2] for s in shared})
    print(f"\n{'=' * 84}\nstream-fed settle-rule A/B — {len(shared)} graded "
          f"{'/'.join(durs)} windows\n{'=' * 84}")
    # W-L is per WINDOW (a window with any fires, graded by its net), not per clip.
    hdr = f"{'rule':>12} {'fired':>7} {'clips':>7} {'notional':>11} {'pnl':>11} {'W-L/win':>8}"
    print(hdr + "\n" + "-" * len(hdr))
    for lab, rows in ((a.label_a, base), (a.label_b, var)):
        fired = sum(1 for s in shared if rows[s]["sim"]["fires"])
        print(f"{lab:>12} {fired:>7} {fires(rows):>7} {tot(rows, 'notional'):>10.0f}$ "
              f"{tot(rows, 'pnl'):>+10.2f}$ {wl(rows):>8}")
    print(f"\nΔnet ({a.label_b} - {a.label_a}) = {sum(deltas):+.2f}$   "
          f"bootstrap CI95 [{lo:+.0f}, {hi:+.0f}]  (10k resamples over {len(shared)} windows)")
    print("Only the DELTA is read: the fill sim's absolute pnl is not wallet truth "
          "(analysis/hybrid_ab.md).")
    if len(shared) < 20:
        print(f"\n  WARNING: {len(shared)} windows is a screen, not a verdict. A CI this wide "
              "cannot separate\n  the rules; report the sign and the sample, never the point "
              "estimate alone.")
    worst = sorted(zip(shared, deltas), key=lambda x: x[1])
    for slug, d in worst[:5] + worst[-5:]:
        if abs(d) < 1e-9:
            continue
        print(f"    {slug:32s} {d:+9.2f}$  "
              f"{a.label_a}=${base[slug]['sim']['pnl'] or 0:+.2f} "
              f"{a.label_b}=${var[slug]['sim']['pnl'] or 0:+.2f} "
              f"won={var[slug]['sim']['outcome'] or '?'}")
    return 0


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("preflight")
    p.add_argument("--book", default=BOOK)
    p.add_argument("--rtds", default=RTDS)
    # 15m is the study; --dur -5m- points the SAME harness at the duration whose
    # tapes do overlap, which is how the wiring gets tested without faking a 15m
    # answer that does not exist.
    p.add_argument("--dur", default="-15m-")
    p.set_defaults(fn=cmd_preflight)

    p = sub.add_parser("params")
    p.add_argument("--rule", required=True, choices=("hybrid", "range_avg", "terminal"))
    p.add_argument("--out", required=True)
    p.add_argument("--book", default=BOOK)
    p.add_argument("--dur", default="-15m-")
    p.set_defaults(fn=cmd_params)

    p = sub.add_parser("report")
    p.add_argument("--a", required=True)
    p.add_argument("--b", required=True)
    p.add_argument("--label-a", default="range_avg")
    p.add_argument("--label-b", default="hybrid")
    p.set_defaults(fn=cmd_report)

    a = ap.parse_args()
    sys.exit(a.fn(a))


if __name__ == "__main__":
    main()
