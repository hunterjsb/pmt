#!/usr/bin/env python3
"""The terminal-gate A/B — three replay experiments on one frozen corpus.

  (a) `late_terminal_agree` on the 5m stream fleet. Refuse a fire inside the
      final `late_rem_s` when the arm's OWN settlement series disagrees with
      the side, or reads inside CK_NOISE_FLOOR_BP. The designated acceptance
      window is the xrp 20:20Z V-window, frozen as
      fixtures/xrp-updown-5m-1787516400.

  (b) `settle_rule = terminal` with the partial/projected settlement TWAP
      banked. analysis/fifteen_rerun.md §4 proved terminal unreachable — 0
      fires in 48 windows, theta necessary AND sufficient. This asks whether
      banking a projection makes it reachable and what it earns.

      REPORTED TWICE, at theta 0.3 AND at matched selectivity. The terminal
      cushion is ~1.68x range_avg's, so at constant theta the rule change is
      mechanically an entry-rate cut and a P&L delta reads as a rule verdict
      when it is arithmetic (analysis/cushion_calibration.md). The matched
      leg sweeps theta on the terminal arm until fired-count lands on the
      baseline's, and reports the theta that got there.

  (c) `latch_release_on_proof`. gate_shadow_2.md §4 #1 measured the latched
      cohort at 113 opens / 95% winners / +$136 over 9.8h. ROADMAP:304 wants
      a replay A/B before any brake loosens; this is it.

Read-only against ~/.pmt. Writes only under --work.

Usage:
  uv run --project pmtrader python analysis/terminal_gate_ab.py survey --work DIR
  uv run --project pmtrader python analysis/terminal_gate_ab.py report --work DIR
"""

import argparse
import collections
import json
import math
import pathlib
import random

# The live 5m fleet, read off arms-state at the freeze. btc/eth/xrp are the
# stream arms — (a) and (c) are their experiments. sol is the BINANCE control:
# `term_bp` is None there, so the late gate must move it by exactly zero, and
# a zero that is measured beats a zero that is asserted.
STREAM_5M = ["btc", "eth", "xrp"]
BINANCE_5M = ["sol"]
# (b)'s 15m half. sol 15m is refused by the vol study and is carried as a
# third data point only, never a verdict.
FIFTEEN = ["btc", "eth"]

BIN_SYMBOL = {s: f"{s.upper()}USDT" for s in ["btc", "eth", "sol", "xrp", "bnb"]}
SETTLE_TW_S = 60.0

# 15m sizes at the level the fleet ran before it was parked — the same values
# analysis/fifteen_rerun.md used, so (b)'s 15m half is directly comparable to
# the study that found terminal unreachable. Identical across legs.
PARKED_15M = {"btc": dict(size=350.0, clip=50.0, guard=6.0, sigma=9.0),
              "eth": dict(size=350.0, clip=50.0, guard=8.0, sigma=25.0)}


def utc(t):
    import datetime
    return datetime.datetime.fromtimestamp(
        t, datetime.timezone.utc).strftime("%H:%M:%SZ")


# ------------------------------------------------------------- inputs


def book_windows(path):
    """(slug -> [first_t, last_t]) for every window the book tape covers."""
    wins = collections.defaultdict(lambda: [1e18, -1e18])
    with open(path) as fh:
        for line in fh:
            i = line.find('"slug":"')
            if i < 0:
                continue
            slug = line[i + 8:line.find('"', i + 8)]
            k = line.find('"t":')
            if k < 0:
                continue
            t = float(line[k + 4:line.find(",", k)])
            w = wins[slug]
            w[0] = min(w[0], t)
            w[1] = max(w[1], t)
    return wins


def outcomes(path):
    out = {}
    with open(path) as fh:
        for line in fh:
            r = json.loads(line)
            out[r["slug"]] = r["winner"]
    return out


def corpus_span(rtds_dir):
    """Per-symbol (first, last) receive time in the recorder corpus.

    Only the spot topic is scanned: it is the densest series and it bounds
    the others, and a full parse of a 200MB day costs a minute for a number
    that is used to reject windows at the edges.
    """
    span = {}
    for f in sorted(pathlib.Path(rtds_dir).glob("rtds-*.jsonl")):
        with open(f) as fh:
            for line in fh:
                if '"crypto_prices_chainlink"' not in line:
                    continue
                i = line.find('"symbol": "')
                if i < 0:
                    i = line.find('"symbol":"')
                    if i < 0:
                        continue
                    sym = line[i + 10:line.find('"', i + 10)]
                else:
                    sym = line[i + 11:line.find('"', i + 11)]
                k = line.find('"t_recv":')
                if k < 0:
                    k = line.find('"t_recv": ')
                if k < 0:
                    continue
                t = float(line[line.find(":", k) + 1:line.find(",", k)])
                cur = span.get(sym)
                if cur is None:
                    span[sym] = [t, t]
                else:
                    cur[0] = min(cur[0], t)
                    cur[1] = max(cur[1], t)
    return span


def comparable(wins, outc, span, syms, dur_s, need_corpus):
    """Windows every leg can answer for.

    In the book tape, graded, and — when a leg is stream-fed — with the
    corpus spanning the settlement reference through the close. A baseline
    allowed to run windows the variant is refused on is not a baseline.
    """
    out = {s: [] for s in syms}
    tag = f"-updown-{int(dur_s // 60)}m-"
    for slug in wins:
        sym = slug.split("-")[0]
        if sym not in out or tag not in slug:
            continue
        if slug not in outc:
            continue
        start = float(slug.rsplit("-", 1)[1])
        end = start + dur_s
        if need_corpus:
            cs = span.get(f"{sym}/usd")
            if not cs or cs[0] > start or cs[1] < end:
                continue
        out[sym].append(slug)
    for s in out:
        out[s].sort(key=lambda x: float(x.rsplit("-", 1)[1]))
    return out


# ------------------------------------------------------------- params


def arm_defaults(arms_state, sym, dur):
    """The live arm's own gate params, or None when that arm is not up."""
    for a in arms_state["arms"]:
        if a["slug"].startswith(f"{sym}-updown-{dur}-"):
            return a
    return None


def build(slugs, dur_s, base_by_sym, override, tunables=None):
    """One params entry per window. Everything outside `override` is held
    IDENTICAL across legs — the only way a delta means anything."""
    out = []
    for slug in sorted(slugs, key=lambda s: (float(s.rsplit("-", 1)[1]), s)):
        sym = slug.split("-")[0]
        base = base_by_sym.get(sym)
        if base is None:
            continue
        start = float(slug.rsplit("-", 1)[1])
        p = dict(base)
        p.update({
            "slug": slug, "kind": "twap", "symbol": BIN_SYMBOL[sym],
            # Per-window token ids; replay never places an order and the book
            # view joins on the tape's own sides, not on these.
            "token_up": f"{slug}-up", "token_down": f"{slug}-down",
            "start": start, "end": start + dur_s, "roll": False,
        })
        p.update(override.get(sym, override) if isinstance(
            override.get(sym), dict) else override)
        if tunables:
            p["tunables"] = dict(tunables)
        out.append(p)
    return out


def cmd_survey(a):
    work = pathlib.Path(a.work)
    work.mkdir(parents=True, exist_ok=True)

    print("[1/4] reading the frozen tapes ...", flush=True)
    wins = book_windows(a.book_tape)
    outc = outcomes(a.outcomes)
    arms = json.loads(pathlib.Path(a.arms_state).read_text())
    print(f"      {len(wins)} book window(s), {len(outc)} graded")

    print("[2/4] scanning the RTDS corpus span ...", flush=True)
    span = corpus_span(a.rtds_dir)
    for s in sorted(span):
        print(f"      {s:9s} {utc(span[s][0])} .. {utc(span[s][1])}")

    print("[3/4] comparable sets ...", flush=True)
    c5_stream = comparable(wins, outc, span, STREAM_5M, 300.0, True)
    c5_binance = comparable(wins, outc, span, BINANCE_5M, 300.0, False)
    c15 = comparable(wins, outc, span, FIFTEEN, 900.0, True)
    for name, c in [("5m stream", c5_stream), ("5m binance", c5_binance),
                    ("15m", c15)]:
        print(f"      {name:11s} " + "  ".join(
            f"{s} {len(c[s])}" for s in c))

    print("[4/4] writing params ...", flush=True)

    # ---- 5m: the live arms, exactly as armed, minus the roll ------------
    live5 = {}
    for s in STREAM_5M + BINANCE_5M:
        base = arm_defaults(arms, s, "5m")
        if base is None:
            raise SystemExit(f"no live 5m arm for {s} in arms-state")
        live5[s] = {k: v for k, v in base.items()
                    if k not in ("slug", "start", "end", "token_up",
                                 "token_down", "roll")}

    sets = {"stream": (c5_stream, 300.0, live5),
            "binctl": (c5_binance, 300.0, live5)}
    # (a) and (c): the ONLY thing that moves is the Tunables knob.
    knob_legs = {
        "live": None,
        "lta": {"late_terminal_agree": True},
        "latch": {"latch_release_on_proof": True},
    }
    for gname, (cset, dur, base) in sets.items():
        slugs = [s for sy in cset for s in cset[sy]]
        for leg, tun in knob_legs.items():
            arr = build(slugs, dur, base, {}, tun)
            (work / f"params-{gname}-{leg}.json").write_text(
                json.dumps(arr, indent=1))
        print(f"      {gname:7s} {len(slugs):3d} window(s) x {len(knob_legs)} leg(s)")

    # ---- (b) settle_rule, 5m and 15m ------------------------------------
    # 5m runs on the live arms; 15m on the parked sizes fifteen_rerun used.
    live15 = {s: {k: v for k, v in arm_defaults(arms, s, "5m").items()
                  if k not in ("slug", "start", "end", "token_up",
                               "token_down", "roll")} for s in FIFTEEN}
    for s in FIFTEEN:
        live15[s].update({
            "size_usdc": PARKED_15M[s]["size"],
            "clip_usdc": PARKED_15M[s]["clip"],
            "basis_guard_bp": PARKED_15M[s]["guard"],
            "sigma_bp_per_min": PARKED_15M[s]["sigma"],
            "feed": "rtds", "settle_tw_s": SETTLE_TW_S,
            # The 15m fleet never ran a maker bid or a pay-up.
            "maker_bid": False, "pay_up_max": 0.0,
        })

    rule_sets = {"rule5": ([s for sy in c5_stream for s in c5_stream[sy]],
                           300.0, live5),
                 "rule15": ([s for sy in c15 for s in c15[sy]], 900.0, live15)}
    thetas = [float(t) for t in a.thetas.split(",")]
    for gname, (slugs, dur, base) in rule_sets.items():
        (work / f"params-{gname}-range_avg.json").write_text(json.dumps(
            build(slugs, dur, base, {"settle_rule": "range_avg"}), indent=1))
        for th in thetas:
            tag = f"{th:g}".replace(".", "")
            (work / f"params-{gname}-terminal_th{tag}.json").write_text(
                json.dumps(build(slugs, dur, base,
                                 {"settle_rule": "terminal", "theta": th}),
                           indent=1))
        print(f"      {gname:7s} {len(slugs):3d} window(s) x "
              f"{1 + len(thetas)} leg(s)")

    meta = {
        "frozen_at": a.frozen_at,
        "sets": {"stream": c5_stream, "binctl": c5_binance, "fifteen": c15},
        "rule5": [s for sy in c5_stream for s in c5_stream[sy]],
        "rule15": [s for sy in c15 for s in c15[sy]],
        "thetas": thetas,
        "corpus_span": {k: v for k, v in span.items()},
    }
    (work / "meta.json").write_text(json.dumps(meta, indent=1))
    print(f"      meta.json written to {work}")
    return 0


# ------------------------------------------------------------- report


def load_rows(path):
    rows = {}
    with open(path) as fh:
        for line in fh:
            r = json.loads(line)
            slug = r.get("slug") or ""
            if not slug or "aggregate" in slug:
                continue
            rows[slug] = r
    return rows


def binom_p(better, worse):
    n = better + worse
    if n == 0:
        return 1.0
    tail = sum(math.comb(n, k) for k in range(0, min(better, worse) + 1))
    return min(1.0, 2.0 * tail / (2.0 ** n))


def bootstrap(deltas, n=10000, seed=7):
    if not deltas:
        return 0.0, 0.0
    rnd = random.Random(seed)
    k = len(deltas)
    sums = sorted(sum(rnd.choice(deltas) for _ in range(k)) for _ in range(n))
    return sums[int(n * 0.025)], sums[int(n * 0.975)]


def stats(rows, slugs):
    fired = clips = w = l = 0
    net = notional = 0.0
    for s in slugs:
        r = rows.get(s)
        if not r:
            continue
        sim = r["sim"]
        f = sim.get("fires") or 0
        clips += f
        pnl = sim.get("pnl") or 0.0
        net += pnl
        notional += sim.get("notional") or 0.0
        if f:
            fired += 1
            if pnl > 0:
                w += 1
            elif pnl < 0:
                l += 1
    return dict(fired=fired, clips=clips, W=w, L=l, net=net, notional=notional,
                ron=(net / notional * 100.0) if notional else 0.0)


def table(title, base_name, legs, slugs, note=""):
    """One A/B table: every leg against `base_name`, with the sign test, the
    jackknife (Δ with the single largest window removed) and a bootstrap CI."""
    print(f"\n{'=' * 104}")
    print(title)
    if note:
        print(note)
    print("=" * 104)
    hdr = (f"{'leg':>22} {'fired':>6} {'clips':>6} {'W-L':>7} {'net $':>10} "
           f"{'RoN':>8} {'Δnet':>10} {'Δ ex-top':>10} {'b/w':>7} {'sign p':>7} "
           f"{'CI95':>20}")
    print(hdr + "\n" + "-" * len(hdr))
    base = legs[base_name]
    bs = stats(base, slugs)
    print(f"{base_name:>22} {bs['fired']:6d} {bs['clips']:6d} "
          f"{bs['W']:3d}-{bs['L']:<3d} {bs['net']:10.2f} {bs['ron']:7.2f}% "
          f"{'—':>10} {'—':>10} {'—':>7} {'—':>7} {'—':>20}")
    for name, rows in legs.items():
        if name == base_name:
            continue
        st = stats(rows, slugs)
        deltas = []
        better = worse = 0
        for s in slugs:
            b = (base.get(s, {}).get("sim") or {}).get("pnl") or 0.0
            v = (rows.get(s, {}).get("sim") or {}).get("pnl") or 0.0
            d = v - b
            deltas.append(d)
            if d > 1e-9:
                better += 1
            elif d < -1e-9:
                worse += 1
        dn = sum(deltas)
        top = max(deltas, key=abs) if deltas else 0.0
        lo, hi = bootstrap(deltas)
        print(f"{name:>22} {st['fired']:6d} {st['clips']:6d} "
              f"{st['W']:3d}-{st['L']:<3d} {st['net']:10.2f} {st['ron']:7.2f}% "
              f"{dn:+10.2f} {dn - top:+10.2f} {better:3d}/{worse:<3d} "
              f"{binom_p(better, worse):7.3f} [{lo:+8.2f},{hi:+8.2f}]")


def cmd_report(a):
    work = pathlib.Path(a.work)
    meta = json.loads((work / "meta.json").read_text())
    sets = meta["sets"]

    def rows_for(gname, leg):
        p = work / f"out-{gname}-{leg}.jsonl"
        return load_rows(p) if p.exists() else None

    def shared(gname, legs, slugs):
        return [s for s in slugs if all(s in r for r in legs.values())]

    # ---- (a) the terminal-consistency late gate -------------------------
    stream_slugs = [s for sy in sets["stream"] for s in sets["stream"][sy]]
    legs = {n: r for n in ("live", "lta")
            if (r := rows_for("stream", n)) is not None}
    if len(legs) == 2:
        sl = shared("stream", legs, stream_slugs)
        table(f"(a) late_terminal_agree — 5m STREAM fleet, {len(sl)} graded "
              f"window(s) (btc/eth/xrp on rtds), fleet-cap {a.cap}",
              "live", legs, sl)
        forfeit_report(legs["live"], legs["lta"], sl)

    bin_slugs = [s for sy in sets["binctl"] for s in sets["binctl"][sy]]
    legs = {n: r for n in ("live", "lta")
            if (r := rows_for("binctl", n)) is not None}
    if len(legs) == 2:
        sl = shared("binctl", legs, bin_slugs)
        table(f"(a-control) late_terminal_agree — 5m BINANCE arm (sol), "
              f"{len(sl)} window(s). term_bp is None here, so the gate must "
              f"move nothing.", "live", legs, sl)

    # ---- (b) settle_rule = terminal with partial banking -----------------
    for gname, label in (("rule15", "btc/eth 15m"), ("rule5", "5m stream")):
        slugs = meta["rule15" if gname == "rule15" else "rule5"]
        legs = {}
        r = rows_for(gname, "range_avg")
        if r is None:
            continue
        legs["range_avg"] = r
        for th in meta["thetas"]:
            tag = f"{th:g}".replace(".", "")
            rr = rows_for(gname, f"terminal_th{tag}")
            if rr is not None:
                legs[f"terminal th{th:g}"] = rr
        # The selectivity levers. theta is reported down to 0 because the
        # headline finding is that it does NOT move terminal's entry rate —
        # the waiver chain does — so a "matched selectivity" leg has to be
        # named on the axis that actually binds, or not claimed at all.
        msel = "msel15" if gname == "rule15" else "msel5"
        for leg, label2 in (("terminal_th0", "terminal th0"),
                            ("terminal_latch", "terminal+latch"),
                            ("terminal_latch_th0", "terminal+latch th0"),
                            ("terminal_latch_th01", "terminal+latch th0.1")):
            rr = rows_for(msel, leg)
            if rr is not None and label2 not in legs:
                legs[label2] = rr
        if len(legs) < 2:
            continue
        sl = shared(gname, legs, slugs)
        base_fired = stats(legs["range_avg"], sl)["fired"]
        th_legs = [n for n in legs if n.startswith("terminal th")]
        th_fired = {stats(legs[n], sl)["fired"] for n in th_legs}
        matched = min(
            (n for n in legs if n != "range_avg"),
            key=lambda n: abs(stats(legs[n], sl)["fired"] - base_fired),
            default=None)
        note = (f"    baseline fired {base_fired} window(s). theta sweep "
                f"{sorted(th_fired)} fired — "
                + ("theta does NOT move the entry rate, so selectivity cannot "
                   "be matched on the theta axis; the binding constraint is "
                   "the decidedness waiver (budget unlock + the latch), not "
                   "the entry gate."
                   if len(th_fired) == 1 else
                   "theta moves the entry rate; the matched leg is below.")
                + f" Closest leg on any axis: {matched} "
                  f"(fired {stats(legs[matched], sl)['fired']}).")
        table(f"(b) settle_rule=terminal + partial banking — {label}, "
              f"{len(sl)} graded window(s), fleet-cap {a.cap}",
              "range_avg", legs, sl, note)

    # ---- (c) the window-latch release -----------------------------------
    legs = {n: r for n in ("live", "latch")
            if (r := rows_for("stream", n)) is not None}
    if len(legs) == 2:
        sl = shared("stream", legs, stream_slugs)
        table(f"(c) latch_release_on_proof — 5m STREAM fleet, {len(sl)} "
              f"graded window(s), fleet-cap {a.cap}", "live", legs, sl)
    return 0


def forfeit_report(base, var, slugs):
    """What the gate refused, split by whether refusing was right.

    The whole question for a REFUSAL gate: it can only ever cost money on
    windows the baseline won. Anything it takes off a losing window is the
    point; anything it takes off a winning one is the bill.
    """
    saved = forfeit = 0.0
    n_saved = n_forf = n_same = 0
    worst = []
    for s in slugs:
        b = (base.get(s, {}).get("sim") or {}).get("pnl") or 0.0
        v = (var.get(s, {}).get("sim") or {}).get("pnl") or 0.0
        d = v - b
        if abs(d) < 1e-9:
            n_same += 1
        elif b < 0:
            saved += d
            n_saved += 1
            worst.append((d, s, b, v))
        else:
            forfeit += d
            n_forf += 1
            worst.append((d, s, b, v))
    print(f"\n    untouched {n_same} window(s) · refused into a LOSING window "
          f"{n_saved} (Δ {saved:+.2f}) · refused into a WINNING window "
          f"{n_forf} (Δ {forfeit:+.2f})")
    worst.sort(key=lambda x: x[0])
    for d, s, b, v in worst[:6]:
        print(f"      {s:32s} {b:+9.2f} -> {v:+9.2f}   {d:+9.2f}")
    if len(worst) > 6:
        print(f"      ... and {len(worst) - 6} more")


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("survey")
    s.add_argument("--work", required=True)
    s.add_argument("--book-tape", required=True)
    s.add_argument("--outcomes", required=True)
    s.add_argument("--arms-state", required=True)
    s.add_argument("--rtds-dir", required=True)
    s.add_argument("--frozen-at", default="")
    s.add_argument("--thetas", default="0.3,0.25,0.2,0.15,0.1")
    s.set_defaults(fn=cmd_survey)
    r = sub.add_parser("report")
    r.add_argument("--work", required=True)
    r.add_argument("--cap", default="500")
    r.set_defaults(fn=cmd_report)
    a = ap.parse_args()
    raise SystemExit(a.fn(a))


if __name__ == "__main__":
    main()
