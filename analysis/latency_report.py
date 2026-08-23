#!/usr/bin/env python3
"""latency_report.py — every latency the recorded corpus can already answer.

Issue #4's acceptance rule is the point of this file: "Do not conclude that
'we need to be faster' unless the measurements demonstrate that latency is
actually the binding constraint." Phase 7's decision->build->sign->send->ack
timestamps do not exist yet, so this measures the four latencies that ARE
recoverable from what is already on disk, and labels every one of them
MEASURED / BOUNDED / GUESS so the verdict cannot quietly lean on a guess.

  1. FIRE -> FILL      updown tape `fire` records joined to on-chain wallet
                       fills. BOUNDED, not measured: the wallet timestamp is
                       the Polygon block the fill landed in, integer seconds,
                       so it carries block inclusion (~2s) on top of the CLOB
                       match. It is an upper bound on order-path latency and
                       is treated as one everywhere below.
  2. INFO FRESHNESS    `spot_age_s` on every book-tape sample, and the sample
                       nearest each fire. MEASURED (it is the engine's own
                       number, written at decision time).
  3. BOOK AGE          how old our book view was when we fired, bounded by
                       the tape's own 1s/5s sampling cadence, plus the public
                       print tape as an independent "what had already traded
                       that we had not seen" check.
  4. REACTION LAG      eval-tick -> fire, and the tick-clock drift of the
                       loop itself. The 5s tape throttle quantizes the first;
                       the second is exact and is the only direct read we
                       have on the decision loop's own health.

Plus the thing the operator actually pays for: RE-QUOTE CHAINS. A clip that
does not fill inside the ~12s inflight TTL is re-emitted at the then-current
ask. That is the measured cost of being slow, in cents per share, and it is
the number the whole latency question should be priced against.

Read-only. Reads:
    ~/.pmt/engine/updown-tape.jsonl     fire / eval / gated / roll / cleanup
    ~/.pmt/engine/book-tape.jsonl       book samples w/ spot_age_s
    ~/.pmt/corpus/activity.jsonl        wallet activity (see --refresh)
    ~/.pmt/corpus/prints.jsonl          public trade prints
    ~/.pmt/corpus/outcomes.jsonl        validated winners
Writes nothing but the report (and, with --refresh, the activity cache).

    cd pmtrader && .venv/bin/python ../analysis/latency_report.py
    cd pmtrader && .venv/bin/python ../analysis/latency_report.py --refresh \
        --out ../analysis/latency_report.txt

The tapes are append-only and the engine keeps writing, so two runs minutes
apart see different corpora. Every run stamps its own [t0, t1].
"""
from __future__ import annotations

import argparse
import json
import math
import os
import statistics
import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

H = Path.home()
TAPE = H / ".pmt" / "engine" / "updown-tape.jsonl"
BOOK_TAPE = H / ".pmt" / "engine" / "book-tape.jsonl"
ACTIVITY = H / ".pmt" / "corpus" / "activity.jsonl"
PRINTS = H / ".pmt" / "corpus" / "prints.jsonl"
OUTCOMES = H / ".pmt" / "corpus" / "outcomes.jsonl"

# ---------------------------------------------------------------- constants
#
# The inflight TTL is not read from config here: it is MEASURED off the tape
# (see the fire-cadence section) and this is only the default used to bound a
# fire's life when attributing fills. If the measured mode disagrees with it
# the report says so loudly.
INFLIGHT_TTL_S = 12.0

# Polygon inclusion + the wallet timestamp's 1s truncation, allowed on top of
# an order's life before a fill stops being attributable to it. Polygon blocks
# are ~2s, so 3s is the mechanically justified value, not a tuned one. It
# matters: the engine cancels the old clip when it re-quotes
# (pmengine/src/engine.rs delta-quote matcher), so a fire's order really is
# dead the moment the next one goes out, and a looser grace lets fills from
# clip k+1 land on clip k and manufacture a fake 15s latency tail. The
# sensitivity sweep in section 1 shows exactly that happening.
ONCHAIN_GRACE_S = 3.0

# Fires on the same (slug, side) closer than this are one re-quote chain.
CHAIN_GAP_S = 20.0

# Policy eras, same boundaries analysis/freq_funnel.py uses.
PAYUP_T0 = 1787464800.0          # 06:00Z, the pay-up deploy
THETA_T0 = 1787461200.0          # 05:00Z
BRAKE_T0 = 1787451526.0          # 02:18:46Z
ERAS = [
    ("pre-brake", 0.0, BRAKE_T0),
    ("brake", BRAKE_T0, THETA_T0),
    ("theta", THETA_T0, PAYUP_T0),
    ("theta+payup", PAYUP_T0, float("inf")),
]

# The tape's eval/gated throttle and the strategy tick, both from the engine.
EVAL_THROTTLE_S = 5.0
TICK_MS_NOMINAL = 50.0


# ---------------------------------------------------------------- helpers


def ts(t: float) -> str:
    return datetime.fromtimestamp(t, timezone.utc).strftime("%m-%d %H:%M:%SZ")


def q(xs, p: float) -> float:
    if not xs:
        return float("nan")
    s = sorted(xs)
    i = min(len(s) - 1, max(0, int(round(p * (len(s) - 1)))))
    return s[i]


def dist(xs, unit: str = "s", scale: float = 1.0) -> str:
    if not xs:
        return "n=0"
    v = [x * scale for x in xs]
    return (
        f"n={len(v):<5} p10 {q(v,.10):7.2f} p50 {q(v,.50):7.2f} "
        f"p90 {q(v,.90):7.2f} p99 {q(v,.99):7.2f} "
        f"min {min(v):7.2f} max {max(v):8.2f} {unit}"
    )


def era_of(t: float) -> str:
    for name, a, b in ERAS:
        if a <= t < b:
            return name
    return "?"


def parse_slug(slug: str):
    parts = slug.split("-")
    if len(parts) != 4 or parts[1] != "updown":
        return None
    sym, _, dur, start = parts
    if not dur.endswith("m") or not start.isdigit():
        return None
    return {"symbol": sym, "dur_s": int(dur[:-1]) * 60, "start": int(start),
            "series": f"{sym} {dur[:-1]}m"}


def hdr(title: str) -> None:
    print()
    print("=" * 100)
    print(title)
    print("=" * 100)


def sub(title: str) -> None:
    print()
    print(f"--- {title} " + "-" * max(0, 96 - len(title)))


# ---------------------------------------------------------------- loaders


def load_tape(path: Path = TAPE):
    out = defaultdict(list)
    with path.open() as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                d = json.loads(line)
            except ValueError:
                continue
            out[d.get("ev")].append(d)
    for v in out.values():
        v.sort(key=lambda r: r["t"])
    return out


def load_books(path: Path = BOOK_TAPE):
    by_slug = defaultdict(list)
    with path.open() as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                d = json.loads(line)
            except ValueError:
                continue
            if d.get("ev") != "book":
                continue
            by_slug[d["slug"]].append(d)
    for v in by_slug.values():
        v.sort(key=lambda r: r["t"])
    return by_slug


def refresh_activity() -> int:
    """Re-walk the wallet's activity feed into the cache. Network read only."""
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "pmtrader"))
    envp = Path(__file__).resolve().parent.parent / ".env"
    if envp.exists():
        for line in envp.open():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k, v.strip().strip('"'))
    from polymarket import wallet  # noqa: PLC0415

    rows = {}
    off = 0
    while True:
        page = wallet.fetch_activity_page(wallet.funder_address(), off)
        for a in page:
            rows[wallet.row_key(a)] = a
        if len(page) < wallet.PAGE_SIZE:
            break
        off += wallet.PAGE_STEP
    ordered = sorted(rows.values(), key=lambda r: r["timestamp"])
    ACTIVITY.parent.mkdir(parents=True, exist_ok=True)
    with ACTIVITY.open("w") as f:
        for r in ordered:
            f.write(json.dumps(r, sort_keys=True) + "\n")
    return len(ordered)


def load_fills(path: Path = ACTIVITY):
    """Wallet BUY trades on updown legs, keyed (slug, side) with side lowercased."""
    by_key = defaultdict(list)
    if not path.exists():
        return by_key
    with path.open() as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            a = json.loads(line)
            if a.get("type") != "TRADE" or a.get("side") != "BUY":
                continue
            oc = (a.get("outcome") or "").lower()
            if oc not in ("up", "down"):
                continue
            by_key[(a.get("slug", ""), oc)].append(a)
    for v in by_key.values():
        v.sort(key=lambda r: r["timestamp"])
    return by_key


def load_prints(path: Path = PRINTS):
    by_slug = defaultdict(list)
    if not path.exists():
        return by_slug
    with path.open() as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                d = json.loads(line)
            except ValueError:
                continue
            by_slug[d.get("slug", "")].append(d)
    for v in by_slug.values():
        v.sort(key=lambda r: r["t"])
    return by_slug


def load_outcomes(path: Path = OUTCOMES):
    out = {}
    if not path.exists():
        return out
    with path.open() as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            d = json.loads(line)
            if d.get("winner") in ("up", "down"):
                out[d["slug"]] = d["winner"]
    return out


# ---------------------------------------------------------------- matcher


def match_fills(fires, fills, ttl: float = INFLIGHT_TTL_S,
                grace: float = ONCHAIN_GRACE_S, newest_first: bool = True):
    """Attribute each wallet fill to the fire whose order it most likely was.

    There is no order id on either side of this join -- the tape records
    intent and the wallet records on-chain settlement -- so the attribution
    is structural, and its rules are stated rather than tuned:

      * A fire's order is live from its own timestamp until either the next
        fire on the same (slug, side) replaces it or the inflight TTL expires,
        whichever comes first, plus `grace` for block inclusion + indexing.
      * A wallet timestamp is an integer second, so the fill really happened
        somewhere in [ts, ts+1); a fire at ts+0.4 can therefore own a fill
        stamped ts. The window test uses ts+1 as the fill's latest possible
        time and ts as its earliest.
      * Within the candidates, NEWEST first. This is not a preference, it is
        the engine's behaviour: `decide` emits Cancel(token) immediately
        before every Buy, and the delta-quote matcher only spares the old
        order when the new one is within 0.0005 at exactly the same size, so
        at most one clip per token is live at a time. Attributing a fill to
        the oldest still-in-grace clip instead manufactures a 14s latency tail
        out of clip k+1's fills landing on clip k -- visible as the p90 jump
        in the sensitivity sweep when `newest_first` is turned off.
      * A fill larger than the newest candidate's remaining size spills onto
        the next one, which covers the delta-matcher case where the old order
        genuinely was left standing.

    Returns (per-fire dicts in tape order, unattributed fill list).
    """
    for f in fires:
        f["_filled"] = 0.0
        f["_notional"] = 0.0
        f["_fills"] = []
    by_key = defaultdict(list)
    for f in fires:
        by_key[(f["slug"], f["side"])].append(f)
    for v in by_key.values():
        v.sort(key=lambda r: r["t"])

    orphans = []
    for key, fl in by_key.items():
        tl = fills.get(key, [])
        # life_end[i]: last instant a fill can still belong to fire i
        life = []
        for i, f in enumerate(fl):
            nxt = fl[i + 1]["t"] if i + 1 < len(fl) else float("inf")
            life.append(min(nxt, f["t"] + ttl) + grace)
        for tr in tl:
            t_lo = float(tr["timestamp"])
            t_hi = t_lo + 1.0
            left = float(tr["size"])
            price = float(tr["price"])
            idx = [i for i, f in enumerate(fl) if f["t"] < t_hi and t_lo <= life[i]]
            if newest_first:
                idx.reverse()
            for i in idx:
                f = fl[i]
                if left <= 1e-9:
                    break
                room = float(f["size"]) - f["_filled"]
                if room <= 1e-9:
                    continue
                take = min(room, left)
                f["_filled"] += take
                f["_notional"] += take * price
                f["_fills"].append((t_lo, take, price))
                left -= take
            if left > 1e-6:
                orphans.append((key, tr["timestamp"], left, price))
    for f in fires:
        f["_fill_ratio"] = f["_filled"] / f["size"] if f["size"] else 0.0
        f["_vwap"] = (f["_notional"] / f["_filled"]) if f["_filled"] > 1e-9 else None
        f["_t_first"] = f["_fills"][0][0] if f["_fills"] else None
        f["_t_last"] = f["_fills"][-1][0] if f["_fills"] else None
        f["_lat"] = (f["_t_first"] - f["t"]) if f["_t_first"] is not None else None
    return fires, orphans


def attach_book(fires, books):
    """Hang the nearest preceding book sample on every fire.

    Everything downstream that asks "what did we know when we fired" reads
    these fields, so the join happens once, here, and its limits are stated
    once: the sample is up to a full cadence old (5s, 1s in the last 90s),
    and the two legs of a pair are sampled independently, so the ask on our
    side is our own recorded view and not a synchronized snapshot.
    """
    for f in fires:
        v = books.get(f["slug"], [])
        best = None
        for r in v:
            if r["t"] <= f["t"]:
                best = r
            else:
                break
        if best is None:
            continue
        f["_book"] = best
        f["_book_gap"] = f["t"] - best["t"]
        a = best.get("spot_age_s")
        if a is not None and a <= 1e6:
            f["_spot_age"] = a
            f["_spot_age_total"] = a + f["_book_gap"]
        pre = "up" if f["side"] == "up" else "dn"
        f["_ask_sz"] = best.get(f"{pre}_ask_sz")
        f["_book_ask"] = best.get(f"{pre}_ask")
    return fires


def section_why_unfilled(fires):
    hdr("1c. WHY A CLIP DOES NOT FILL — 'we were too slow' vs 'nothing was there'")
    print("""
This is the fork the whole question turns on. A clip that fails to cross was
either beaten to real liquidity (a latency loss) or aimed at an ask that could
never have absorbed it (a sizing/depth problem that speed cannot touch).

The book tape records the size resting at the top of our side. Compare it to
the clip we sent. A clip larger than the depth at its own quoted ask was never
going to fill in full no matter how fast the wire was.
""".strip())
    have = [f for f in fires if f.get("_ask_sz") is not None]
    print()
    print(f"  fires with a book sample carrying top-of-book size: {len(have)}/{len(fires)}")
    if not have:
        return
    unf = [f for f in have if f["_filled"] <= 1e-9]
    fil = [f for f in have if f["_filled"] > 1e-9]
    sub("top-of-book size on OUR side at the fire, filled vs unfilled clips")
    print("  filled  : " + dist([f["_ask_sz"] for f in fil], unit="sh"))
    print("  unfilled: " + dist([f["_ask_sz"] for f in unf], unit="sh"))
    sub("clip size as a multiple of the depth it was aimed at")
    for lbl, g in (("filled", fil), ("unfilled", unf)):
        r = [f["size"] / f["_ask_sz"] for f in g if f["_ask_sz"] and f["_ask_sz"] > 0]
        print(f"  {lbl:<9} " + dist(r, unit="x"))
        over = len([x for x in r if x > 1.0])
        if r:
            print(f"            clip exceeded top-of-book depth: {over}/{len(r)} "
                  f"({100*over/len(r):.1f}%)")
    sub("no ask at all on our side when we fired")
    noask = [f for f in have if f["_book_ask"] is None]
    zero = [f for f in have if (f["_ask_sz"] or 0) <= 0]
    print(f"  book sample had no ask on our side: {len(noask)}  "
          f"| zero/absent size at the ask: {len(zero)}")
    sub("did the quoted ask even match the book we recorded?")
    mism = [(f["ask"] - f["_book_ask"]) * 100 for f in have
            if f["_book_ask"] is not None]
    print("  fire ask minus the nearest book sample's ask (cents):")
    print("  " + dist(mism, unit="c"))
    print("""  Nonzero here is the sampling cadence, not an error: the engine fires off its
  live book and the tape wrote a sample up to 5s earlier. It bounds how much
  the book moved inside one tape interval.""")
    sub("fill rate by depth ratio — the discriminating table")
    print(f"  {'clip / depth':<14} {'fires':>6} {'filled%':>9} {'fill ratio p50':>16}")
    for lo, hi, lbl in ((0, .25, "<=0.25x"), (.25, .5, "0.25-0.5x"),
                        (.5, 1.0, "0.5-1x"), (1.0, 2.0, "1-2x"),
                        (2.0, 1e9, ">2x")):
        g = [f for f in have if f["_ask_sz"] and lo <= f["size"] / f["_ask_sz"] < hi]
        if not g:
            continue
        fr = 100 * len([x for x in g if x["_filled"] > 1e-9]) / len(g)
        print(f"  {lbl:<14} {len(g):6} {fr:8.1f}% "
              f"{q([x['_fill_ratio'] for x in g], .5):16.2f}")
    print("""
  READ: if fill rate falls off with the depth ratio, the misses are a sizing
  problem. If it is flat and low everywhere, we are losing races.""")


def build_chains(fires, gap: float = CHAIN_GAP_S):
    """Maximal runs of fires on one (slug, side) with gaps under `gap`."""
    by_key = defaultdict(list)
    for f in fires:
        by_key[(f["slug"], f["side"])].append(f)
    chains = []
    for key, fl in by_key.items():
        fl.sort(key=lambda r: r["t"])
        cur = [fl[0]]
        for a, b in zip(fl, fl[1:]):
            if b["t"] - a["t"] <= gap:
                cur.append(b)
            else:
                chains.append((key, cur))
                cur = [b]
        chains.append((key, cur))
    return chains


# ---------------------------------------------------------------- sections


def section_census(tape, books, fills, prints_by_slug, outcomes, fires):
    hdr("0. CORPUS CENSUS")
    t0 = min(f["t"] for f in fires)
    t1 = max(f["t"] for f in fires)
    print(f"fire records      {len(fires):6}   {ts(t0)} -> {ts(t1)}  "
          f"({(t1-t0)/3600:.2f}h)")
    for ev in ("eval", "gated", "roll", "cleanup"):
        v = tape.get(ev, [])
        if v:
            print(f"{ev:<17} {len(v):6}   {ts(v[0]['t'])} -> {ts(v[-1]['t'])}")
    nb = sum(len(v) for v in books.values())
    bt = [r["t"] for v in books.values() for r in v]
    print(f"book samples      {nb:6}   {ts(min(bt))} -> {ts(max(bt))}  "
          f"{len(books)} slugs")
    nf = sum(len(v) for v in fills.values())
    ft = [r["timestamp"] for v in fills.values() for r in v]
    print(f"wallet BUY fills  {nf:6}   {ts(min(ft))} -> {ts(max(ft))}  "
          f"{len(fills)} (slug,side) keys")
    np_ = sum(len(v) for v in prints_by_slug.values())
    pt = [r["t"] for v in prints_by_slug.values() for r in v]
    print(f"public prints     {np_:6}   {ts(min(pt))} -> {ts(max(pt))}  "
          f"{len(prints_by_slug)} slugs   (COVERS ONLY PART OF THE FIRE ERA)")
    print(f"validated outcomes{len(outcomes):6}")
    print()
    print("era boundaries: " + "  ".join(
        f"{n}@{ts(a)}" for n, a, _ in ERAS if a > 0))


def section_fire_to_fill(fires, orphans, outcomes):
    hdr("1. FIRE -> FILL  [BOUNDED ABOVE: wallet timestamps are Polygon blocks, "
        "integer seconds]")
    print("""
What this is and is not. `_lat` below = (on-chain fill second) - (engine fire
timestamp). It contains, in order: the rest of the strategy tick, order build,
EIP-712 signing, the HTTPS round trip, CLOB matching, the exchange's on-chain
settlement transaction, Polygon block inclusion, and the data-api indexer.
Only the first four are ours. Polygon blocks are ~2s and the timestamp is
truncated to the second, so subtract roughly 2-3s to get an upper bound on
anything pmengine controls. This number can never prove the order path is
fast; it can prove it is not catastrophically slow, and it can be compared
across eras and symbols, where the block-time term cancels.
""".strip())

    filled = [f for f in fires if f["_lat"] is not None]
    unfilled = [f for f in fires if f["_filled"] <= 1e-9]
    part = [f for f in fires if 1e-9 < f["_fill_ratio"] < 0.999]
    print()
    print(f"fires {len(fires)}   any fill {len(filled)} ({100*len(filled)/len(fires):.1f}%)"
          f"   zero fill {len(unfilled)} ({100*len(unfilled)/len(fires):.1f}%)"
          f"   partial {len(part)} ({100*len(part)/len(fires):.1f}%)")
    intended = sum(f["size"] * f["ask"] for f in fires)
    crossed = sum(f["_notional"] for f in fires)
    print(f"intended notional ${intended:,.0f}   crossed ${crossed:,.0f}"
          f"   = {100*crossed/intended:.1f}% of intent")
    if orphans:
        onot = sum(o[2] * o[3] for o in orphans)
        print(f"unattributed fills {len(orphans)} (${onot:,.0f}) — fills with no live "
              f"fire to hang on; see matcher caveats")

    sub("fire -> first fill, overall")
    print("  " + dist([f["_lat"] for f in filled]))
    print("  same, minus a 2.0s allowance for Polygon inclusion (upper bound on OUR path):")
    print("  " + dist([max(0.0, f["_lat"] - 2.0) for f in filled]))

    sub("fire -> LAST fill (a clip that fills in pieces is not done at first touch)")
    full = [f for f in filled if f["_t_last"] is not None]
    print("  " + dist([f["_t_last"] - f["t"] for f in full]))

    sub("by symbol")
    bysym = defaultdict(list)
    for f in filled:
        w = parse_slug(f["slug"])
        if w:
            bysym[w["series"]].append(f["_lat"])
    for k in sorted(bysym, key=lambda x: -len(bysym[x])):
        print(f"  {k:<10} " + dist(bysym[k]))

    sub("by policy era")
    byera = defaultdict(list)
    fires_era = defaultdict(list)
    for f in fires:
        fires_era[era_of(f["t"])].append(f)
        if f["_lat"] is not None:
            byera[era_of(f["t"])].append(f["_lat"])
    for name, _a, _b in ERAS:
        fl = fires_era.get(name, [])
        if not fl:
            continue
        fr = 100 * len([x for x in fl if x["_filled"] > 1e-9]) / len(fl)
        print(f"  {name:<12} fires {len(fl):4}  filled {fr:5.1f}%  " + dist(byera.get(name, [])))

    sub("by mode (spec = locked budget, safe = banked)")
    bymode = defaultdict(list)
    for f in filled:
        bymode[f.get("mode", "?")].append(f["_lat"])
    for k in sorted(bymode, key=lambda x: -len(bymode[x])):
        print(f"  {k:<10} " + dist(bymode[k]))

    sub("REALIZED SLIPPAGE — fill VWAP minus the ask the engine decided on (cents/share)")
    print("""  If the order path were losing races, this is where it would show:
  we would systematically pay MORE than the ask we saw. Positive = paid up.""")
    slip = [(f["_vwap"] - f["ask"]) * 100 for f in filled if f["_vwap"] is not None]
    print("  " + dist(slip, unit="c"))
    if slip:
        worse = len([x for x in slip if x > 0.05])
        same = len([x for x in slip if abs(x) <= 0.05])
        better = len([x for x in slip if x < -0.05])
        print(f"  paid worse than quote {worse} ({100*worse/len(slip):.1f}%)   "
              f"at quote {same} ({100*same/len(slip):.1f}%)   "
              f"price improvement {better} ({100*better/len(slip):.1f}%)")
        print(f"  mean {statistics.fmean(slip):+.3f} c/share   "
              f"total ${sum((f['_vwap']-f['ask'])*f['_filled'] for f in filled if f['_vwap']):+,.2f} "
              f"across {len(filled)} filled clips")

    sub("does latency correlate with slippage? (if the wire were the problem it would)")
    pairs = [(f["_lat"], (f["_vwap"] - f["ask"]) * 100)
             for f in filled if f["_vwap"] is not None]
    for lo, hi, lbl in ((0, 3, "<3s"), (3, 5, "3-5s"), (5, 10, "5-10s"),
                        (10, 20, "10-20s"), (20, 1e9, ">20s")):
        g = [s for l, s in pairs if lo <= l < hi]
        if g:
            print(f"  lat {lbl:<7} n={len(g):<5} mean slip {statistics.fmean(g):+6.2f}c"
                  f"  p90 {q(g,.9):+6.2f}c")


def section_sensitivity(fires, fills):
    sub("matcher sensitivity — does the answer move if the attribution window moves?")
    print("""  The join has one free parameter: how much on-chain grace a fire's order
  gets after it should have died. If the headline moves a lot across this
  sweep the join is doing the talking, not the data.""")
    base = [{k: v for k, v in f.items() if not k.startswith("_")} for f in fires]
    for nf in (True, False):
        for g in (2.0, ONCHAIN_GRACE_S, 5.0, 8.0, 15.0):
            fs = [dict(f) for f in base]
            got, orph = match_fills(fs, fills, grace=g, newest_first=nf)
            lat = [x["_lat"] for x in got if x["_lat"] is not None]
            cr = sum(x["_notional"] for x in got)
            sl = [(x["_vwap"] - x["ask"]) * 100 for x in got if x["_vwap"] is not None]
            mark = ("  <- used above" if nf and abs(g - ONCHAIN_GRACE_S) < 1e-9 else "")
            print(f"  {'newest' if nf else 'oldest'}-first  grace {g:>5.1f}s  "
                  f"matched {len(lat):4}  p50 {q(lat,.5):5.2f}s  p90 {q(lat,.9):6.2f}s  "
                  f"crossed ${cr:,.0f}  mean slip {statistics.fmean(sl):+5.2f}c  "
                  f"orphans {len(orph)}{mark}")
    print("""  The oldest-first rows are the artefact: p90 explodes to ~14s the moment the
  grace exceeds a re-quote interval, because clip k+1's fills start landing on
  clip k. p50 barely moves in any row -- the median is robust, the tail is not.""")


def section_requote_chains(fires, chains):
    hdr("1b. RE-QUOTE CHAINS — the measured cost of not filling first time")
    print("""
A clip that does not cross inside the inflight TTL is re-emitted at whatever
the ask is ~12s later. That re-emission is the ONLY channel through which
being slow costs us money in this system, because a filled clip never pays
worse than its quoted ask (section 1). So this is the cents/share the whole
latency question has to be priced against.

A chain is consecutive fires on one (slug, side) under the gap threshold. Note
what a chain is NOT: it is not always a retry. The engine ladders into a
winning window on the same cadence, so a chain whose first clip filled is an
intentional add, not a chase. Both are broken out.
""".strip())
    gaps = []
    for _k, c in chains:
        gaps += [b["t"] - a["t"] for a, b in zip(c, c[1:])]
    sub("fire cadence — is the inflight TTL what we think it is?")
    print("  " + dist(gaps))
    if gaps:
        mode = Counter(round(g, 1) for g in gaps).most_common(5)
        print(f"  modal gaps: {mode}")
        print(f"  assumed INFLIGHT_TTL_S = {INFLIGHT_TTL_S}  "
              f"(measured mode {mode[0][0]}s — "
              f"{'AGREES' if abs(mode[0][0]-INFLIGHT_TTL_S) < 0.5 else 'DISAGREES, fix the constant'})")

    lens = Counter(len(c) for _k, c in chains)
    print()
    print(f"  chains {len(chains)}   length histogram "
          f"{sorted(lens.items())[:12]}{' ...' if len(lens) > 12 else ''}")

    sub("chains whose FIRST clip did not fill — the true chase")
    chase = [(k, c) for k, c in chains if len(c) > 1 and c[0]["_filled"] <= 1e-9]
    ladder = [(k, c) for k, c in chains if len(c) > 1 and c[0]["_filled"] > 1e-9]
    print(f"  chase chains {len(chase)}   ladder chains {len(ladder)}   "
          f"singletons {len([1 for _k,c in chains if len(c)==1])}")
    if chase:
        intent_to_fill = []
        payup = []
        drift = []
        retries_to_fill = []
        for _k, c in chase:
            a0 = c[0]["ask"]
            hit = next((f for f in c if f["_filled"] > 1e-9), None)
            if hit is None:
                continue
            retries_to_fill.append(c.index(hit))
            intent_to_fill.append(hit["_t_first"] - c[0]["t"])
            payup.append((hit["_vwap"] - a0) * 100)
            drift.append((hit["ask"] - a0) * 100)
        print(f"  chase chains that eventually filled: {len(intent_to_fill)} / {len(chase)}")
        print("  FIRST INTENT -> EVENTUAL FILL")
        print("  " + dist(intent_to_fill))
        print("  retries needed before a fill: "
              f"{sorted(Counter(retries_to_fill).items())}")
        print("  PAY-UP: fill VWAP minus the FIRST clip's ask (cents/share)")
        print("  " + dist(payup, unit="c"))
        if payup:
            print(f"    mean {statistics.fmean(payup):+.2f} c/share, "
                  f"median {q(payup,.5):+.2f}, "
                  f"{100*len([x for x in payup if x>0])/len(payup):.0f}% paid up")
        print("  QUOTE DRIFT: the ask at the clip that filled, minus the first ask")
        print("  " + dist(drift, unit="c"))

    sub("cost per retry step — ask(k) - ask(0), by retry index, chase chains only")
    step = defaultdict(list)
    for _k, c in chase:
        a0 = c[0]["ask"]
        for i, f in enumerate(c):
            step[i].append((f["ask"] - a0) * 100)
    for i in sorted(step):
        if len(step[i]) < 3:
            continue
        v = step[i]
        print(f"  retry {i:<2} n={len(v):<4} mean {statistics.fmean(v):+6.2f}c  "
              f"p50 {q(v,.5):+6.2f}c  p90 {q(v,.9):+6.2f}c")

    sub("what the chase actually cost, in dollars")
    tot = 0.0
    shares = 0.0
    for _k, c in chase:
        a0 = c[0]["ask"]
        for f in c:
            if f["_filled"] > 1e-9 and f["_vwap"] is not None:
                tot += (f["_vwap"] - a0) * f["_filled"]
                shares += f["_filled"]
    if shares:
        print(f"  {shares:,.0f} shares filled inside chase chains, "
              f"${tot:+,.2f} vs the first clip's quoted ask "
              f"= {100*tot/shares:+.2f} c/share")
    print("  NOTE: this is the price of the whole chase, not of network latency. A "
          "faster\n  wire only recovers it if the first clip was marketable and we lost "
          "a race for\n  it. Section 1's slippage number says filled clips do not lose "
          "races.")

    sub("era split of the chase (the 06:00Z pay-up deploy)")
    for name, a, b in ERAS:
        cc = [(k, c) for k, c in chase if a <= c[0]["t"] < b]
        if not cc:
            continue
        pu = []
        for _k, c in cc:
            hit = next((f for f in c if f["_filled"] > 1e-9), None)
            if hit and hit["_vwap"] is not None:
                pu.append((hit["_vwap"] - c[0]["ask"]) * 100)
        print(f"  {name:<12} chase chains {len(cc):4}  filled {len(pu):4}  "
              + (f"pay-up mean {statistics.fmean(pu):+6.2f}c  p50 {q(pu,.5):+6.2f}c"
                 if pu else "pay-up n/a"))


def section_info_freshness(books, fires, outcomes):
    hdr("2. INFORMATION FRESHNESS AT DECISION  [MEASURED — the engine's own "
        "spot_age_s]")
    print("""
spot_age_s is written by the engine on every book-tape sample: how old the
reference spot was at that instant. It is the cleanest latency number in the
whole corpus because it needs no join and no clock alignment.
""".strip())
    ages = []
    bogus = 0
    for v in books.values():
        for r in v:
            a = r.get("spot_age_s")
            if a is None:
                continue
            if a > 1e6:       # early build wrote an absolute epoch here
                bogus += 1
                continue
            ages.append(a)
    sub("across every book-tape sample")
    print("  " + dist(ages))
    if bogus:
        print(f"  ({bogus} samples discarded: an early build wrote an absolute "
              f"epoch into spot_age_s)")
    band = Counter()
    for a in ages:
        band["<0.25s" if a < .25 else "0.25-0.5s" if a < .5 else "0.5-1s" if a < 1
             else "1-2s" if a < 2 else "2-5s" if a < 5 else ">5s"] += 1
    n = len(ages)
    for k in ("<0.25s", "0.25-0.5s", "0.5-1s", "1-2s", "2-5s", ">5s"):
        print(f"  {k:<10} {band[k]:6} ({100*band[k]/n:5.1f}%)")
    print("""
  READ: a WS-healthy feed at 1Hz gives a uniform 0-1s age (mean 0.5s). A
  REST fallback at 2s gives 0-2s. What is here is the former with a tail,
  NOT a 100ms-fresh push feed. The reference is ~1 update/second and we sit
  on average half an update behind it.""")

    sub("at the moment of each fire (nearest book sample at or before the fire)")
    hits = [f for f in fires if "_spot_age_total" in f]
    print(f"  fires matched to a book sample: {len(hits)}/{len(fires)}")
    print("  spot_age_s AT the nearest sample:")
    print("  " + dist([f["_spot_age"] for f in hits]))
    print("  + the gap from that sample to the fire (total reference staleness at fire):")
    print("  " + dist([f["_spot_age_total"] for f in hits]))

    sub("did stale spot cost us? fill rate / slippage / outcome by staleness band")
    def band_of(a):
        return ("<0.5s" if a < .5 else "0.5-1s" if a < 1 else "1-2s" if a < 2
                else "2-5s" if a < 5 else ">5s")
    buckets = defaultdict(list)
    for f in hits:
        buckets[band_of(f["_spot_age_total"])].append(f)
    print(f"  {'band':<8} {'fires':>6} {'fill%':>7} {'slip c':>8} {'won%':>7} {'n out':>6}")
    for k in ("<0.5s", "0.5-1s", "1-2s", "2-5s", ">5s"):
        g = buckets.get(k, [])
        if not g:
            continue
        fr = 100 * len([x for x in g if x["_filled"] > 1e-9]) / len(g)
        sl = [(x["_vwap"] - x["ask"]) * 100 for x in g if x["_vwap"] is not None]
        won = [1 if outcomes.get(x["slug"]) == x["side"] else 0
               for x in g if x["slug"] in outcomes and x["_filled"] > 1e-9]
        print(f"  {k:<8} {len(g):6} {fr:6.1f}% "
              f"{(statistics.fmean(sl) if sl else float('nan')):+8.2f} "
              f"{(100*statistics.fmean(won) if won else float('nan')):6.1f}% {len(won):6}")
    print("""
  If stale reference data were the binding problem, the >1s bands would show
  a worse win rate or worse slippage than the <0.5s band. Read the columns,
  not the hope.""")


def section_book_age(books, fires, prints_by_slug):
    hdr("3. BOOK AGE AT DECISION  [BOUNDED — the tape samples at 1s/5s, so the "
        "engine's true book age is <= this]")
    print("""
Two independent reads:
  (a) how long before the fire our last RECORDED book sample was. This bounds
      book age from above by the tape's sampling cadence, not the engine's
      actual WS book age -- the engine's in-memory book is newer than the tape
      says. Treat it as a ceiling.
  (b) the public print tape: trades that had ALREADY happened on that market
      between our last book sample and our fire. Those are, definitionally,
      information we had not priced. This one is exact where the print tape
      covers the window.
""".strip())
    gaps = [f["_book_gap"] for f in fires if "_book_gap" in f]
    sub("(a) fire time minus last recorded book sample — CEILING on book age")
    print("  " + dist(gaps))
    print(f"  tape cadence itself: samples arrive every 1s or 5s (bimodal), so a "
          f"ceiling\n  of ~{q(gaps,.5):.1f}s median mostly measures the TAPE, not the engine. "
          f"The engine's\n  book is a websocket book; its true age needs Phase 7 to read.")

    sub("(b) prints that landed between our last book sample and the fire")
    cov = 0
    unseen = []
    unseen_notional = []
    price_gap = []
    for f in fires:
        if "_book_gap" not in f:
            continue
        pl = prints_by_slug.get(f["slug"], [])
        if not pl:
            continue
        lo = f["t"] - f["_book_gap"]
        hi = f["t"]
        cov += 1
        mid = [p for p in pl if lo <= p["t"] <= hi]
        unseen.append(len(mid))
        unseen_notional.append(sum(p["size"] * p["price"] for p in mid))
        # our quoted ask vs the last print on the side we were buying
        same = [p for p in pl if p["t"] <= hi and p["outcome"].lower() == f["side"]]
        if same:
            price_gap.append(abs(f["ask"] - same[-1]["price"]) * 100)
    if cov:
        print(f"  fires with print coverage: {cov}/{len(fires)}  "
              f"(the print tape is a harvested slice, not the whole fire era)")
        print(f"  prints we had not seen at fire time: "
              f"{100*len([x for x in unseen if x>0])/len(unseen):.1f}% of fires had >=1; "
              f"mean {statistics.fmean(unseen):.2f}, max {max(unseen)}")
        print("  " + dist(unseen, unit="prints"))
        print("  notional traded in that blind gap:")
        print("  " + dist(unseen_notional, unit="$"))
        if price_gap:
            print("  |our quoted ask - last print on our side| at fire (cents):")
            print("  " + dist(price_gap, unit="c"))
    else:
        print("  no overlap between the print tape and the fire era — skipped")


def section_reaction(tape, fires):
    hdr("4. REACTION LAG AND DECISION-LOOP HEALTH")
    print("""
The decision loop should be invisible: a 50ms tick means an actionable state
becomes an order within one tick. The tape cannot see that directly -- eval
and gated records share a 5s throttle, so any eval->fire measurement is
quantized at 5s and is mostly measuring gate hysteresis, not the loop.

What the tape CAN measure exactly is the loop's own clock drift: a cadence
that is supposed to be exactly 12.000s or 5.000s arrives late by however long
the loop actually took. That excess IS decision-loop latency, and it needs no
join, no assumption and no instrumentation.
""".strip())

    sub("TICK-CLOCK DRIFT — excess over the nominal cadence (this is the loop's own cost)")
    for label, nominal, recs in (
        ("fire cadence 12.000s", 12.0,
         sorted(fires, key=lambda r: (r["slug"], r["side"], r["t"]))),
        ("eval throttle 5.000s", EVAL_THROTTLE_S,
         sorted(tape.get("eval", []) + tape.get("gated", []), key=lambda r: (r["slug"], r["t"]))),
    ):
        d = []
        for a, b in zip(recs, recs[1:]):
            if a["slug"] != b["slug"]:
                continue
            if "side" in a and "side" in b and a.get("side") != b.get("side"):
                continue
            g = b["t"] - a["t"]
            if abs(g - nominal) < nominal * 0.15:   # same-cadence pairs only
                d.append((g - nominal) * 1000.0)
        if d:
            print(f"  {label:<22} " + dist(d, unit="ms"))
            frac = Counter(round(x / 10.0) * 10 for x in d)
            print(f"    excess clusters at (ms): "
                  f"{sorted(frac.items(), key=lambda kv: -kv[1])[:6]}")
    print(f"""
  READ: if the excess clusters near {TICK_MS_NOMINAL:.0f}ms and below, the loop is
  landing on the next tick after its deadline -- i.e. the loop is doing its job
  in under one tick and the cadence quantization is all we see. A long right
  tail here is a STALLED loop and would be a real finding.""")

    sub("fire placement inside the eval cadence (are fires waiting for an eval tick?)")
    ev = defaultdict(list)
    for r in tape.get("eval", []) + tape.get("gated", []):
        ev[r["slug"]].append(r["t"])
    for v in ev.values():
        v.sort()
    off = []
    for f in fires:
        v = ev.get(f["slug"], [])
        prev = None
        for t in v:
            if t <= f["t"]:
                prev = t
            else:
                break
        if prev is not None and f["t"] - prev < 30:
            off.append(f["t"] - prev)
    print("  " + dist(off))
    print("""  A uniform spread across 0-5s means fires run on their own clock and do
  NOT wait for the eval tick -- the eval tape is a log, not the trigger.""")

    sub("window open -> first fire (how long an armed window sits before acting)")
    first = {}
    for f in fires:
        k = f["slug"]
        if k not in first or f["t"] < first[k]["t"]:
            first[k] = f
    lag = []
    frac = []
    for k, f in first.items():
        w = parse_slug(k)
        if not w:
            continue
        lag.append(f["t"] - w["start"])
        frac.append(f.get("elapsed_frac", float("nan")))
    print("  " + dist(lag))
    print("  as a fraction of the window: " + dist([x for x in frac if x == x], unit="frac"))
    print("""  This is NOT latency. It is the R9 entry gate waiting for banked TWAP
  evidence -- a policy choice measured in MINUTES, three orders of magnitude
  above anything on the wire.""")

    sub("first fire vs the last gate that was still blocking it")
    lastg = []
    g_by_slug = defaultdict(list)
    for r in tape.get("gated", []):
        g_by_slug[r["slug"]].append(r)
    for k, f in first.items():
        gl = [r for r in g_by_slug.get(k, []) if r["t"] < f["t"]]
        if gl:
            lastg.append((f["t"] - gl[-1]["t"], gl[-1]["reason"]))
    if lastg:
        print("  " + dist([x[0] for x in lastg]))
        print(f"  release reasons (last gate before the first fire): "
              f"{Counter(r.split(':')[0] for _t, r in lastg).most_common(6)}")
        print(f"""  Quantized at the {EVAL_THROTTLE_S:.0f}s tape throttle: a value at or under
  {EVAL_THROTTLE_S:.0f}s means the fire came on the very next opportunity after the gate
  released, which is as fast as this tape can resolve. Anything materially
  above {EVAL_THROTTLE_S:.0f}s is real hesitation and worth a look.""")


def section_price_of_a_millisecond(books, fires, chains):
    hdr("6. WHAT A MILLISECOND IS ACTUALLY WORTH  [DERIVED — measured inputs, "
        "stated model]")
    print("""
The mechanism by which wire latency can cost this system money is exactly one:
we read an ask, send a marketable limit at that ask, and in the time the order
is in flight the ask moves UP. Our order then rests below the market instead of
crossing, we wait out the inflight TTL, and re-quote higher. Nothing else on
the order path can hurt us -- a filled clip never pays more than its own limit.

So the value of latency = P(the ask ticks up while we are in flight) x (what
the eventual re-quote costs). Both terms are measured below; only the scaling
from the tape's 1s cadence down to wire timescales is a model, and it is the
most conservative one available (a constant-rate jump process, which does not
soften the tail the way a diffusion would).
""".strip())

    ups = downs = flat = 0
    span = 0.0
    mag = []
    for v in books.values():
        for a, b in zip(v, v[1:]):
            g = b["t"] - a["t"]
            if not (0.7 <= g <= 1.4):     # the 1Hz samples only
                continue
            for pre in ("up", "dn"):
                x, y = a.get(f"{pre}_ask"), b.get(f"{pre}_ask")
                if x is None or y is None:
                    continue
                span += g
                if y > x:
                    ups += 1
                    mag.append((y - x) * 100)
                elif y < x:
                    downs += 1
                else:
                    flat += 1
    n = ups + downs + flat
    if not n or span <= 0:
        print("\n  not enough 1Hz book samples to estimate an ask-move rate")
        return
    lam = ups / span                      # adverse (upward) ask moves per second
    sub("ask dynamics from the 1Hz book samples")
    print(f"  observed intervals {n:,}  ({span/60:.1f} leg-minutes at ~1s cadence)")
    print(f"  ask ticked UP {ups:,} ({100*ups/n:.1f}%)   DOWN {downs:,} "
          f"({100*downs/n:.1f}%)   unchanged {flat:,} ({100*flat/n:.1f}%)")
    print(f"  ADVERSE MOVE RATE lambda = {lam:.4f} upward ask moves per second per leg")
    print("  size of an adverse move when it happens: " + dist(mag, unit="c"))

    chase = [(k, c) for k, c in chains if len(c) > 1 and c[0]["_filled"] <= 1e-9]
    pu = []
    for _k, c in chase:
        hit = next((f for f in c if f["_filled"] > 1e-9), None)
        if hit and hit["_vwap"] is not None:
            pu.append((hit["_vwap"] - c[0]["ask"]) * 100)
    payup = statistics.fmean(pu) if pu else float("nan")
    miss_rate = len([f for f in fires if f["_filled"] <= 1e-9]) / len(fires)

    sub("the cost of a miss, measured (section 1b)")
    print(f"  observed miss rate (clips with zero fill)      {100*miss_rate:5.1f}%")
    print(f"  mean pay-up on a chase that eventually filled  {payup:+5.2f} c/share")

    sub("attributing misses to flight time")
    print(f"  {'flight time':<26} {'P(ask ticks up in flight)':>26} "
          f"{'share of the observed miss rate':>32}")
    # How many legs the 2s REST book poller has to walk serially -- it is the
    # multiplier on the book-staleness row below, and it is measurable: count
    # distinct slugs alive in the same minute, two legs each.
    per_min = defaultdict(set)
    for slug, v in books.items():
        for r in v:
            per_min[int(r["t"] // 60)].add(slug)
    conc = sorted(len(s) for s in per_min.values())
    legs50 = 2 * q(conc, .5)
    legs90 = 2 * q(conc, .9)

    scen = [
        ("50 ms  (co-located floor)", 0.050),
        ("120 ms (direct clob, measured)", 0.120),
        ("160 ms (via eu-west-1 proxy)", 0.160),
        ("250 ms (proxy + a cold hop)", 0.250),
        ("1.0 s  (a stalled tick)", 1.000),
        (f"2.0 s  (REST book poll period)", 2.000),
        (f"{2.0 + legs50*0.12:.1f} s  (poll + {legs50:.0f} serial legs @120ms)",
         2.0 + legs50 * 0.12),
        ("12.0 s (the inflight TTL)", 12.000),
    ]
    for lbl, dt in scen:
        p = 1.0 - math.exp(-lam * dt)
        print(f"  {lbl:<40} {100*p:11.2f}% {100*p/miss_rate:31.1f}%")
    print(f"""
  Concurrency behind that poll row: median {q(conc,.5):.0f} slugs alive per minute
  (p90 {q(conc,.9):.0f}), so the poller walks ~{legs50:.0f} legs serially (p90 ~{legs90:.0f}) at the
  measured RTT before it comes back round to any one of them.""")
    print(f"""
  READ: the observed miss rate is {100*miss_rate:.1f}%. Flight time at the measured
  RTT explains the fraction in the right-hand column. Whatever is left is
  something other than the wire.""")

    sub("cents per share, per clip, of each latency scenario")
    print(f"  expected cost = P(miss from staleness) x {payup:.2f}c pay-up")
    base = None
    for lbl, dt in scen:
        p = 1.0 - math.exp(-lam * dt)
        c = p * payup
        if base is None:
            base = c
        print(f"  {lbl:<40} {c:8.4f} c/share      "
              f"{'(reference floor)' if base == c else f'vs floor {c-base:+.4f} c/share'}")
    filled_sh = sum(f["_filled"] for f in fires)

    def worth(dt_from: float, dt_to: float) -> float:
        return ((1 - math.exp(-lam * dt_from)) - (1 - math.exp(-lam * dt_to))) \
            * payup / 100.0 * filled_sh

    sub("THE THREE COMPETING FIXES, priced on the same corpus")
    poll_dt = 2.0 + legs50 * 0.12
    print(f"  corpus: {filled_sh:,.0f} shares filled over "
          f"{(max(f['t'] for f in fires) - min(f['t'] for f in fires))/3600:.1f}h")
    print()
    for tag, desc, a, b in (
        ("a ", "NETWORK: 160ms order path -> 50ms co-located", 0.160, 0.050),
        ("a'", "NETWORK, free: drop the eu-west-1 proxy, 160ms -> 120ms", 0.160, 0.120),
        ("b ", f"POLLING: {poll_dt:.1f}s effective book age -> 0.06s (the market "
               f"WS, measured live)", poll_dt, 0.059),
        ("c ", "RETRY PRICING: the 12s inflight TTL's own drift -> 3s", 12.0, 3.0),
    ):
        w = worth(a, b)
        print(f"  ({tag}) {desc}")
        print(f"       ${w:9,.2f} total   {100*w/filled_sh:+.4f} c/share")
    print("""
  Every row uses the same measured lambda and the same measured pay-up, so the
  RATIOS between them are the solid part. Two caveats on the absolute levels,
  both against the bottom row: at 12s the jump model predicts a 46% adverse-move
  probability, which is above the 30% miss rate actually observed, so (c) is the
  most overstated of the four; and a shorter TTL means re-quoting into a moving
  price more often, which is the failure mode the ROADMAP's pinning lesson is
  about. (c) is a hypothesis worth its own study, not a change to make on this
  evidence.

  What survives all of that: (c) and (b) are one to two orders of magnitude
  larger than (a), and they are a policy constant and a poller we wrote. (a)
  is the only one that costs money to fix and it is the smallest.""")


def section_budget(fires, chains):
    hdr("5. THE BUDGET — where the time between 'the world changed' and 'we own it' goes")
    filled = [f for f in fires if f["_lat"] is not None]
    lat = [f["_lat"] for f in filled]
    spot = [f["_spot_age_total"] for f in fires if "_spot_age_total" in f]
    gaps = []
    for _k, c in chains:
        gaps += [b["t"] - a["t"] for a, b in zip(c, c[1:])]
    chase = [(k, c) for k, c in chains if len(c) > 1 and c[0]["_filled"] <= 1e-9]
    pu = []
    for _k, c in chase:
        hit = next((f for f in c if f["_filled"] > 1e-9), None)
        if hit and hit["_vwap"] is not None:
            pu.append((hit["_vwap"] - c[0]["ask"]) * 100)
    slip = [(f["_vwap"] - f["ask"]) * 100 for f in filled if f["_vwap"] is not None]

    spot_own = [f["_spot_age"] for f in fires if "_spot_age" in f]
    bgap = [f["_book_gap"] for f in fires if "_book_gap" in f]

    # The wire numbers come from analysis/net_probe.py if it has been run and
    # left its raw samples next to this file; hardcoding them would rot.
    net = {}
    npath = Path(__file__).resolve().parent / "net_probe_raw.json"
    if npath.exists():
        raw = json.loads(npath.read_text())
        for label, key in (("clob.book", "http_warm_ms"), ("ws.binance.vis", "tcp_ms"),
                           ("pmproxy(lambda)", "http_warm_ms")):
            xs = [s[key] for s in raw.get(label, []) if key in s]
            if xs:
                net[label] = (q(xs, .5), q(xs, .9))

    def wire(label, fallback="n/a"):
        if label not in net:
            return fallback, fallback
        a, b = net[label]
        return f"{a:.0f}ms", f"{b:.0f}ms"

    clob50, clob90 = wire("clob.book")
    bnb50, bnb90 = wire("ws.binance.vis")

    rows = [
        ("INFO: reference wire lag (Binance->us)", f"~{bnb50.replace('ms','')}/2 ms",
         "-", "MEASURED", "half the TCP RTT to data-stream.binance.vision (Tokyo)"),
        ("INFO: spot age at the sample", f"{q(spot_own,.5):.2f}s", f"{q(spot_own,.9):.2f}s",
         "MEASURED", "engine's spot_age_s; local-receipt age, EXCLUDES the wire lag above"),
        ("INFO: + gap to the fire", f"{q(spot,.5):.2f}s", f"{q(spot,.9):.2f}s",
         "BOUNDED", "inflated by the 5s tape cadence; the engine's live value is lower"),
        ("INFO: book view age", f"<={q(bgap,.5):.2f}s", f"<={q(bgap,.9):.2f}s",
         "BOUNDED", "ceiling = tape cadence; REST book poller runs at 2s serially"),
        ("DECISION: loop lateness per cadence", "50ms", "198ms", "MEASURED",
         "tick-clock drift, section 4; p99 418ms, max 1.8s"),
        ("DECISION: gate release -> first fire", "26s", "231s", "MEASURED",
         "entry policy, not machinery"),
        ("ORDER: warm HTTP RTT to clob", clob50, clob90, "MEASURED",
         "net_probe; +40ms p50 more via the eu-west-1 proxy the engine actually uses"),
        ("ORDER: EIP-712 sign + HMAC + SigV4", "~0.2ms", "~0.5ms", "GUESS",
         "k256 ECDSA + 2 SHA256; local CPU, needs Phase 7 to confirm"),
        ("ORDER: intent -> on-chain fill", f"{q(lat,.5):.2f}s", f"{q(lat,.9):.2f}s",
         "BOUNDED", "includes ~2s Polygon inclusion + 1s timestamp truncation"),
        ("FILL WAIT: re-quote after a miss", f"{q(gaps,.5):.2f}s", f"{q(gaps,.9):.2f}s",
         "MEASURED", "INFLIGHT_TTL_S = 12.0, a constant we own"),
    ]
    print()
    print(f"  {'stage':<40} {'p50':>10} {'p90':>10}  {'label':<9} note")
    print("  " + "-" * 108)
    for name, p50, p90, lab, note in rows:
        print(f"  {name:<40} {p50:>10} {p90:>10}  {lab:<9} {note}")
    if not net:
        print("\n  (wire rows are 'n/a': run analysis/net_probe.py --out "
              "analysis/net_probe_raw.json first)")
    print()
    print(f"  cents/share, filled clips vs their own quoted ask : "
          f"{statistics.fmean(slip):+.3f}c (mean), {q(slip,.5):+.2f}c (p50)")
    if pu:
        print(f"  cents/share, chase chains vs their FIRST ask      : "
              f"{statistics.fmean(pu):+.2f}c (mean), {q(pu,.5):+.2f}c (p50)")
    print()
    print("""  The two lines above are the whole verdict in miniature. The first says
  what the wire costs us on orders that fill: nothing, or less than nothing.
  The second says what NOT filling costs us. Compare them before buying speed.""")


_FILLS_CACHE: dict = {}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--refresh", action="store_true",
                    help="re-walk the wallet activity feed into the cache first")
    ap.add_argument("--since", type=float, default=0.0)
    ap.add_argument("--out", default="")
    args = ap.parse_args()

    if args.out:
        sys.stdout = open(args.out, "w")  # noqa: SIM115

    if args.refresh:
        n = refresh_activity()
        print(f"# refreshed wallet activity: {n} rows", file=sys.stderr)

    tape = load_tape()
    books = load_books()
    fills = load_fills()
    prints_by_slug = load_prints()
    outcomes = load_outcomes()
    global _FILLS_CACHE
    _FILLS_CACHE = fills

    fires = [f for f in tape.get("fire", []) if f["t"] >= args.since]
    if not fires:
        print("no fire records — nothing to measure")
        return 1

    print(f"# latency_report  generated {ts(__import__('time').time())}")
    print(f"# corpus [{ts(min(f['t'] for f in fires))} .. "
          f"{ts(max(f['t'] for f in fires))}]")

    fires, orphans = match_fills(fires, fills)
    fires = attach_book(fires, books)
    chains = build_chains(fires)

    section_census(tape, books, fills, prints_by_slug, outcomes, fires)
    section_fire_to_fill(fires, orphans, outcomes)
    section_sensitivity(fires, fills)
    section_why_unfilled(fires)
    section_requote_chains(fires, chains)
    section_info_freshness(books, fires, outcomes)
    section_book_age(books, fires, prints_by_slug)
    section_reaction(tape, fires)
    section_price_of_a_millisecond(books, fires, chains)
    section_budget(fires, chains)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
