"""Do Polymarket's liquidity rewards invert the negative maker verdict?

analysis/firsthalf_research.md priced passive quoting on the crypto updown books
with ZERO reward income and found it negative everywhere (-1.9 to -2.5c/share of
adverse selection against a one-tick spread). Polymarket separately advertises a
$1M/month liquidity-rewards program over exactly these markets. This driver asks
whether the second fact rescues the first.

Six stages, each independently runnable:

  config   which updown markets carry a funded reward program, at what
           rate_per_day, max_spread and min_size. Read the CLOB, not gamma:
           gamma's market.clobRewards is null on 14 of the 21 series that the
           CLOB is in fact funding, so a gamma-only read concludes the program
           is switched off when it is fully live.
  depth    ~/.pmt/engine/book-tape.jsonl (L1) -> qualifying-liquidity census and
           the score-share curve per symbol. Valid because at the operative
           max_spread of 1.5c with a 1c tick, only the touch can score (proved in
           stage `live`), so L1 IS the full qualifying book.
  live     the sampler's full-depth tape -> proves that L1-sufficiency claim, and
           records max_spread flipping 4.5c -> 1.5c a minute or two into each new
           window (4.5c is a creation-time default, not the operative value).
  adverse  maker fill simulation on the book tape: reward income against realised
           adverse selection, with and without the model's own p_up gate.
  net      config sweep (size x spread x active-fraction) with bootstrap CIs.
  collect  the collector that feeds `live`: polls full CLOB depth plus the reward
           config for every live 5m/15m/4h window. Books are not backfillable, so
           this has to be run forward before `live` has anything to read.

Everything except `config` and `collect` is offline and reads ~/.pmt read-only.
"""

from __future__ import annotations

import argparse
import json
import os
import random
import statistics
import time
from collections import Counter, defaultdict

BOOK_TAPE = os.path.expanduser("~/.pmt/engine/book-tape.jsonl")
UPDOWN_TAPE = os.path.expanduser("~/.pmt/engine/updown-tape.jsonl")

UA = {"User-Agent": "pmtrader/1.0"}
GAMMA = "https://gamma-api.polymarket.com"
CLOB = "https://clob.polymarket.com"

SYMS = ["btc", "eth", "sol", "bnb", "xrp", "doge", "hype"]
TF_SECS = {"5m": 300, "15m": 900, "4h": 14400}

# docs.polymarket.com/programs/liquidity-rewards, August crypto-TWAP allocation.
# Monthly $ per symbol per timeframe. The docs call these "configured reward
# caps"; stage `config` checks which are actually live on-chain.
MONTHLY = {
    "5m": {"btc": 300_000, "eth": 50_000, "sol": 50_000, "hype": 50_000,
           "xrp": 50_000, "bnb": 25_000, "doge": 25_000},
    "15m": {"btc": 225_000, "eth": 25_000, "sol": 25_000, "hype": 25_000,
            "xrp": 25_000, "bnb": 12_500, "doge": 12_500},
    "4h": {"btc": 50_000, "eth": 10_000, "sol": 10_000, "hype": 10_000,
           "xrp": 10_000, "bnb": 5_000, "doge": 5_000},
}
# The 4h rate_per_day values observed live are monthly/30 exactly, so the program
# divides by 30, not by the calendar month length.
MONTH_DAYS = 30.0

C_SCALE = 3.0          # single-sided divisor, "currently 3.0 on all markets"
BAND = (0.10, 0.90)    # outside this, liquidity MUST be two-sided to score
TICK = 0.01


# ---------------------------------------------------------------- scoring core

def S(v_cents: float, s_cents: float, b: float = 1.0) -> float:
    """Order position score. v = max qualifying spread, s = spread from the
    size-cutoff-adjusted midpoint, both in cents. b is the in-game multiplier,
    which is 1 on crypto updown (there is no game)."""
    if s_cents >= v_cents or v_cents <= 0:
        return 0.0
    return ((v_cents - s_cents) / v_cents) ** 2 * b


def q_min(q_one: float, q_two: float, mid: float, c: float = C_SCALE) -> float:
    """Equation 4. Inside [0.10, 0.90] single-sided liquidity scores at 1/c;
    outside it, liquidity must be double-sided or it scores zero."""
    if BAND[0] <= mid <= BAND[1]:
        return max(min(q_one, q_two), max(q_one, q_two) / c)
    return min(q_one, q_two)


def side_scores(up_bids, up_asks, dn_bids, dn_asks, mid_up, mid_dn, v, min_size):
    """Q_one / Q_two over a full book pair.

    Q_one groups {bids on m, asks on m'} -- the long-UP exposure side.
    Q_two groups {asks on m, bids on m'} -- the short-UP exposure side.
    Orders below min_size do not qualify and are dropped.
    """
    q1 = q2 = 0.0
    for p, sz in up_bids:
        if sz >= min_size:
            q1 += S(v, (mid_up - p) * 100.0) * sz
    for p, sz in dn_asks:
        if sz >= min_size:
            q1 += S(v, (p - mid_dn) * 100.0) * sz
    for p, sz in up_asks:
        if sz >= min_size:
            q2 += S(v, (p - mid_up) * 100.0) * sz
    for p, sz in dn_bids:
        if sz >= min_size:
            q2 += S(v, (mid_dn - p) * 100.0) * sz
    return q1, q2


def pot_per_window(sym: str, tf: str) -> float:
    """Daily rate for the series, spread over the windows that share the day.

    rate_per_day is configured per market, and a market that lives only part of
    the UTC day accrues its pro-rata slice: a 5m window is 5/1440 of a day. The
    4h numbers reconcile exactly this way ($1666.67/day x 6 windows/day = the
    $50k/mo btc-4h allocation), which is what pins the interpretation.
    """
    monthly = MONTHLY[tf].get(sym)
    if monthly is None:
        return 0.0
    per_day = monthly / MONTH_DAYS
    windows_per_day = 86400.0 / TF_SECS[tf]
    return per_day / windows_per_day


# ------------------------------------------------------------ stage: collect

def stage_collect(args):
    """Poll full CLOB depth + reward config for every live updown window.

    The L1 book tape cannot answer "how much depth is within v of mid", and books
    are not backfillable, so this runs forward. It also catches rewardsMaxSpread
    flipping 4.5c -> 1.5c shortly after a window is created, which is only
    visible if you are watching when it happens.
    """
    import requests
    from concurrent.futures import ThreadPoolExecutor

    out = args.depth_tape or "depth-tape.jsonl"
    meta_cache: dict[str, tuple[float, dict | None]] = {}
    slot = [time.monotonic()]

    def throttle():
        now = time.monotonic()
        s = max(now, slot[0])
        slot[0] = s + 1.0 / 10.0
        d = s - time.monotonic()
        if d > 0:
            time.sleep(d)

    def meta(slug):
        hit = meta_cache.get(slug)
        now = time.monotonic()
        # short TTL: max_spread changes mid-window and a stale read hides it
        if hit and now - hit[0] < 25.0:
            return hit[1]
        throttle()
        try:
            d = requests.get(f"{GAMMA}/markets", params={"slug": slug},
                             headers=UA, timeout=15).json()
        except Exception:
            return hit[1] if hit else None
        if not d:
            meta_cache[slug] = (now, None)
            return None
        m = d[0]
        try:
            toks = json.loads(m.get("clobTokenIds") or "[]")
        except Exception:
            toks = []
        v = dict(cid=m.get("conditionId"),
                 up=toks[0] if len(toks) > 0 else None,
                 dn=toks[1] if len(toks) > 1 else None,
                 min_size=m.get("rewardsMinSize"),
                 max_spread=m.get("rewardsMaxSpread"),
                 rewarded=bool(m.get("clobRewards")),
                 closed=m.get("closed"))
        meta_cache[slug] = (now, v)
        return v

    def book(tok):
        throttle()
        try:
            r = requests.get(f"{CLOB}/book", params={"token_id": tok},
                             headers=UA, timeout=15)
            if r.status_code != 200:
                return None
            d = r.json()
        except Exception:
            return None
        def lv(rs):
            o = []
            for x in rs or []:
                try:
                    o.append([float(x["price"]), float(x["size"])])
                except Exception:
                    pass
            return o
        return lv(d.get("bids")), lv(d.get("asks"))

    def one(slug, t):
        m = meta(slug)
        if not m or m.get("closed") or not m.get("up") or not m.get("dn"):
            return None
        u = book(m["up"])
        d = book(m["dn"])
        if u is None or d is None:
            return None
        return dict(t=t, slug=slug, cid=m["cid"], min_size=m["min_size"],
                    max_spread=m["max_spread"], rewarded=m["rewarded"],
                    up_bids=u[0], up_asks=u[1], dn_bids=d[0], dn_asks=d[1])

    t_end = time.time() + args.collect_secs
    n = 0
    print("collecting to %s for %.0fs, every %.0fs"
          % (out, args.collect_secs, args.collect_period))
    with open(out, "a") as f:
        while time.time() < t_end:
            t0 = time.time()
            slugs = [f"{s}-updown-{tf}-{int(t0 // sec) * sec}"
                     for s in SYMS for tf, sec in TF_SECS.items()]
            with ThreadPoolExecutor(max_workers=8) as ex:
                for row in ex.map(lambda s: one(s, t0), slugs):
                    if row:
                        f.write(json.dumps(row) + "\n")
                        n += 1
            f.flush()
            slp = args.collect_period - (time.time() - t0)
            if slp > 0:
                time.sleep(slp)
    print("wrote %d samples" % n)


# ------------------------------------------------------------- stage: config

def stage_config(args):
    """Which updown markets carry a FUNDED reward program right now.

    The CLOB's own /rewards/markets/{condition_id} is authoritative -- it is what
    the reward engine scores against. gamma's market.clobRewards mirror lags it
    and is null on markets that ARE funded, so trusting gamma alone reads the
    program as switched off when it is not.
    """
    import requests

    now = int(time.time())
    out = []
    print("live reward-config probe  (%s UTC)" % time.strftime("%Y-%m-%dT%H:%M:%SZ",
                                                               time.gmtime(now)))
    print("authority = CLOB /rewards/markets/{cid}; gamma shown for contrast")
    print()
    print("%-26s %-8s %-8s %-12s %-10s %-9s %s" % (
        "slug", "minSize", "maxSprd", "clob rate/d", "pot/window", "gamma", "cfg id"))
    for sym in SYMS:
        for tf, secs in TF_SECS.items():
            base = (now // secs) * secs
            slug = f"{sym}-updown-{tf}-{base}"
            try:
                d = requests.get(f"{GAMMA}/markets", params={"slug": slug},
                                 headers=UA, timeout=20).json()
            except Exception as e:
                print("%-26s ERR %s" % (slug, e))
                continue
            if not d:
                print("%-26s (not in gamma)" % slug)
                continue
            m = d[0]
            cid = m.get("conditionId")
            gamma_cr = m.get("clobRewards") or []
            rate = None
            cfg_id = None
            ms = m.get("rewardsMinSize")
            v = m.get("rewardsMaxSpread")
            try:
                rr = requests.get(f"{CLOB}/rewards/markets/{cid}", headers=UA,
                                  timeout=20).json()
                rows = rr.get("data") or []
                if rows:
                    cfg = (rows[0].get("rewards_config") or [{}])[0]
                    rate = cfg.get("rate_per_day")
                    cfg_id = cfg.get("id")
                    ms = rows[0].get("rewards_min_size", ms)
                    v = rows[0].get("rewards_max_spread", v)
            except Exception:
                pass
            # rate_per_day is a RATE: a market live for only part of the UTC day
            # accrues its pro-rata slice.
            pot = (rate or 0.0) * TF_SECS[tf] / 86400.0
            row = dict(slug=slug, cid=cid, min_size=ms, max_spread=v,
                       clob_rate_per_day=rate, clob_cfg_id=cfg_id,
                       gamma_funded=bool(gamma_cr), pot_per_window=pot,
                       docs_pot_per_window=pot_per_window(sym, tf))
            out.append(row)
            print("%-26s %-8s %-8s %-12s %-10s %-9s %s" % (
                slug, ms, v, rate if rate else "-", "$%.2f" % pot,
                bool(gamma_cr), cfg_id))

    print()
    funded = [r for r in out if r["clob_rate_per_day"]]
    print("funded per the CLOB: %d / %d probed" % (len(funded), len(out)))
    print("funded per gamma:    %d / %d  <-- gamma under-reports"
          % (sum(1 for r in out if r["gamma_funded"]), len(out)))
    print("total live rate/day across funded updown markets: $%.2f"
          % sum(r["clob_rate_per_day"] or 0 for r in funded))
    print()
    print("live rate/day vs the docs' August allocation / 30:")
    print("%-10s %14s %14s %8s" % ("series", "live rate/day", "docs/30", "match"))
    for r in out:
        sym, tf, _ = parse_slug(r["slug"])
        docs = MONTHLY[tf].get(sym, 0) / MONTH_DAYS
        live = r["clob_rate_per_day"]
        ok = "yes" if live and abs(live - docs) < max(1.0, 0.01 * docs) else (
            "-" if not live else "NO")
        print("%-10s %14s %14s %8s" % (
            f"{sym}/{tf}", ("$%.2f" % live) if live else "unfunded",
            "$%.2f" % docs, ok))
    _dump(args, "config", out)
    return out


# -------------------------------------------------------------- book-tape I/O

def load_book_tape(path=BOOK_TAPE):
    rows = []
    with open(path) as f:
        for line in f:
            try:
                d = json.loads(line)
            except Exception:
                continue
            if d.get("ev") != "book":
                continue
            if d.get("up_bid") is None or d.get("up_ask") is None:
                continue
            if d.get("dn_bid") is None or d.get("dn_ask") is None:
                continue
            rows.append(d)
    rows.sort(key=lambda r: r["t"])
    return rows


def parse_slug(slug):
    p = slug.split("-")
    return p[0], p[2], int(p[3])


def by_window(rows):
    w = defaultdict(list)
    for r in rows:
        w[r["slug"]].append(r)
    return w


# -------------------------------------------------------------- stage: depth

def stage_depth(args):
    """Qualifying-liquidity census from L1, and the resulting score share.

    At v=1.5c on a 1c grid only the touch can score, so the L1 tape carries the
    entire qualifying book (stage `live` proves this on full depth). Rivals are
    aggregated into a single competitor, which OVERSTATES their combined Q_min
    (min is superadditive) and therefore understates our share -- conservative.
    """
    rows = load_book_tape(args.book_tape)
    print("book-tape: %d two-sided samples, %.2fh, %d windows" % (
        len(rows), (rows[-1]["t"] - rows[0]["t"]) / 3600.0,
        len({r["slug"] for r in rows})))
    print("span %s -> %s UTC" % (
        time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(rows[0]["t"])),
        time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(rows[-1]["t"]))))
    print()

    v = args.max_spread
    ms = args.min_size
    agg = defaultdict(list)
    for r in rows:
        sym, tf, ep = parse_slug(r["slug"])
        mid_u = (r["up_bid"] + r["up_ask"]) / 2.0
        mid_d = (r["dn_bid"] + r["dn_ask"]) / 2.0
        q1, q2 = side_scores(
            [(r["up_bid"], r["up_bid_sz"])], [(r["up_ask"], r["up_ask_sz"])],
            [(r["dn_bid"], r["dn_bid_sz"])], [(r["dn_ask"], r["dn_ask_sz"])],
            mid_u, mid_d, v, ms)
        agg[(sym, tf)].append(dict(
            t=r["t"], slug=r["slug"], mid=mid_u, q1=q1, q2=q2,
            qmin=q_min(q1, q2, mid_u),
            spread_c=round((r["up_ask"] - r["up_bid"]) * 100),
            up_bid_sz=r["up_bid_sz"], up_ask_sz=r["up_ask_sz"]))

    print("qualifying rival liquidity at v=%.1fc, minSize=%g" % (v, ms))
    print("%-9s %7s %8s %8s %9s %9s %9s %8s" % (
        "sym/tf", "n", "Qmin=0", "medQmin", "p75Qmin", "p90Qmin", "medTouch", "1c-book"))
    census = {}
    for k in sorted(agg):
        rs = agg[k]
        n = len(rs)
        qs = sorted(x["qmin"] for x in rs)
        zero = sum(1 for x in qs if x <= 0) / n
        onec = sum(1 for x in rs if x["spread_c"] == 1) / n
        touch = statistics.median([x["up_bid_sz"] for x in rs])
        print("%-9s %7d %8.2f %8.1f %9.1f %9.1f %9.0f %8.2f" % (
            f"{k[0]}/{k[1]}", n, zero, qs[n // 2], qs[int(.75 * n)],
            qs[int(.90 * n)], touch, onec))
        census[f"{k[0]}/{k[1]}"] = dict(
            n=n, frac_qmin_zero=zero, qmin_med=qs[n // 2],
            qmin_p75=qs[int(.75 * n)], qmin_p90=qs[int(.90 * n)],
            touch_med=touch, frac_1c_book=onec)

    # score share: we join the touch two-sided with X shares a side. On a 1c book
    # our s is 0.5c on both, so S is identical for us and for every rival at the
    # touch and the share collapses to a size ratio -- but compute it properly.
    print()
    print("expected score share by our size (two-sided, joining the touch)")
    print("%-9s %s" % ("sym/tf", "".join("%9s" % f"X={x}" for x in args.sizes)))
    shares = {}
    for k in sorted(agg):
        rs = agg[k]
        line = []
        for X in args.sizes:
            acc = []
            for x in rs:
                s_us = S(v, 0.5)
                q1u = q2u = s_us * X
                ours = q_min(q1u, q2u, x["mid"])
                tot = q_min(x["q1"] + q1u, x["q2"] + q2u, x["mid"])
                acc.append(ours / tot if tot > 0 else 0.0)
            line.append(statistics.mean(acc))
        shares[f"{k[0]}/{k[1]}"] = dict(zip(map(str, args.sizes), line))
        print("%-9s %s" % (f"{k[0]}/{k[1]}", "".join("%9.3f" % y for y in line)))

    print()
    print("implied gross reward $/day per series (share x pot x windows/day)")
    print("%-9s %10s %s" % ("sym/tf", "pot/win", "".join("%11s" % f"X={x}" for x in args.sizes)))
    gross = {}
    for k in sorted(agg):
        sym, tf = k
        pot = pot_per_window(sym, tf)
        wpd = 86400.0 / TF_SECS[tf]
        line = [shares[f"{sym}/{tf}"][str(X)] * pot * wpd for X in args.sizes]
        gross[f"{sym}/{tf}"] = dict(zip(map(str, args.sizes), line))
        print("%-9s %10s %s" % (f"{sym}/{tf}", "$%.2f" % pot,
                                "".join("%11s" % ("$%.0f" % y) for y in line)))
    print()
    print("NOTE: gross reward only -- adverse selection is stage `adverse`. The pots")
    print("      are real: stage `config` confirms every series carries a live CLOB")
    print("      rate_per_day equal to the docs' August allocation / 30.")
    print("NOTE: this depth is measured on books that were only rewarded from")
    print("      2026-08-23. Rival depth can only rise from here, so every share")
    print("      above is an UPPER bound.")
    _dump(args, "depth", dict(census=census, share=shares, gross_per_day=gross))
    return census


# --------------------------------------------------------------- stage: live

def stage_live(args):
    """Full-depth sampler tape: does L1 carry the whole qualifying book, and how
    does the one FUNDED series compare with the unfunded ones?"""
    path = args.depth_tape
    if not path or not os.path.exists(path):
        print("no depth tape at %r -- run the collector first (see module docstring)"
              % path)
        return None
    rows = [json.loads(l) for l in open(path)]
    print("depth tape: %d full-depth samples over %d markets" % (
        len(rows), len({r["slug"] for r in rows})))
    print()

    levels = Counter()
    agg = defaultdict(list)
    for r in rows:
        # CLOB /book returns bids ascending and asks descending; sort so the
        # touch is index 0.
        ub = sorted(r["up_bids"], key=lambda x: -x[0])
        ua = sorted(r["up_asks"], key=lambda x: x[0])
        db = sorted(r["dn_bids"], key=lambda x: -x[0])
        da = sorted(r["dn_asks"], key=lambda x: x[0])
        if not (ub and ua and db and da):
            continue
        sym, tf, ep = parse_slug(r["slug"])
        v = r.get("max_spread") or args.max_spread
        ms = r.get("min_size") or args.min_size
        mid_u = (ub[0][0] + ua[0][0]) / 2.0
        mid_d = (db[0][0] + da[0][0]) / 2.0
        nq = sum(1 for p, sz in ub if sz >= ms and (mid_u - p) * 100.0 < v)
        levels[(v, nq)] += 1
        q1, q2 = side_scores(ub, ua, db, da, mid_u, mid_d, v, ms)
        agg[(sym, tf)].append(dict(v=v, mid=mid_u, q1=q1, q2=q2,
                                   qmin=q_min(q1, q2, mid_u),
                                   funded=r.get("rewarded"),
                                   spread_c=round((ua[0][0] - ub[0][0]) * 100)))

    print("qualifying levels on the UP-bid side, by max_spread:")
    for (v, nq) in sorted(levels):
        print("   v=%.1fc  %d level(s): %d samples" % (v, nq, levels[(v, nq)]))
    tot15 = sum(c for (v, nq), c in levels.items() if v == 1.5)
    le1 = sum(c for (v, nq), c in levels.items() if v == 1.5 and nq <= 1)
    if tot15:
        print("   -> at v=1.5c, %.1f%% of samples have <=1 qualifying bid level."
              % (100.0 * le1 / tot15))
        print("      L1 is therefore the whole qualifying book in that regime.")
    print()

    # "gammaRew" is gamma's clobRewards flag as the sampler saw it. It is NOT
    # the funding truth -- stage `config` shows the CLOB funds all 21 series
    # while gamma only admits to the 4h ones.
    print("%-9s %6s %8s %7s %9s %9s %8s" % (
        "sym/tf", "n", "gammaRew", "medV", "medQmin", "p90Qmin", "medSprd"))
    out = {}
    for k in sorted(agg):
        rs = agg[k]
        n = len(rs)
        qs = sorted(x["qmin"] for x in rs)
        print("%-9s %6d %8s %7.1f %9.1f %9.1f %8.0f" % (
            f"{k[0]}/{k[1]}", n, rs[0]["funded"],
            statistics.median([x["v"] for x in rs]),
            qs[n // 2], qs[int(.9 * n)],
            statistics.median([x["spread_c"] for x in rs])))
        out[f"{k[0]}/{k[1]}"] = dict(n=n, funded=rs[0]["funded"],
                                     qmin_med=qs[n // 2], qmin_p90=qs[int(.9 * n)])
    _dump(args, "live", dict(levels={f"v{v}_n{n}": c for (v, n), c in levels.items()},
                             series=out))
    return out


# ------------------------------------------------------ model p_up (the gate)

def load_p_up(path=UPDOWN_TAPE):
    """slug -> sorted [(t, p_up)] from the engine's own eval events."""
    per = defaultdict(list)
    with open(path) as f:
        for line in f:
            try:
                d = json.loads(line)
            except Exception:
                continue
            if d.get("ev") != "eval" or d.get("p_up") is None:
                continue
            per[d["slug"]].append((d["t"], d["p_up"]))
    for s in per:
        per[s].sort()
    return per


def p_up_at(series, t, max_age=20.0):
    """Most recent p_up at or before t, if it is fresh enough."""
    if not series:
        return None
    lo, hi = 0, len(series) - 1
    best = None
    while lo <= hi:
        mid = (lo + hi) // 2
        if series[mid][0] <= t:
            best = series[mid]
            lo = mid + 1
        else:
            hi = mid - 1
    if best is None or t - best[0] > max_age:
        return None
    return best[1]


# ------------------------------------------------------------ stage: adverse

def simulate_window(samples, X, v, min_size, pot, frac_lo, frac_hi,
                    requote_s, pup=None, gate=None, queue_front=False):
    """One window of two-sided touch quoting.

    We post a BID on UP and a BID on DOWN -- on a binary book that is a complete
    two-sided quote (a bid on DOWN is economically an ask on UP), and it needs no
    inventory to short. Worst case we end up holding complete sets.

    Fill rule: our bid at b is hit when the opposing best ask trades down to our
    price between samples. queue_front=False (the default) is the conservative
    queue-ahead convention of firsthalf_q3_maker.py -- we sat behind the
    displayed size, so only a sweep that carries the ask STRICTLY below our bid
    reaches us. queue_front=True gives us the level to ourselves.

    Fill size is capped at the displayed touch, because an aggressor that only
    cleared the visible level cannot have handed us more than that, and a side
    that just filled stays down until the next requote.

    Returns None if the window never resolves decisively.
    """
    if len(samples) < 4:
        return None
    tf = TF_SECS[parse_slug(samples[0]["slug"])[1]]
    ep = parse_slug(samples[0]["slug"])[2]

    last_mid = (samples[-1]["up_bid"] + samples[-1]["up_ask"]) / 2.0
    if not (last_mid > 0.85 or last_mid < 0.15):
        return None
    up_won = 1.0 if last_mid > 0.5 else 0.0

    sh_up = sh_dn = 0.0
    cost = 0.0
    fills = 0
    share_acc = []
    quoting_s = 0.0
    next_quote = -1e18
    b = d = None

    for i, r in enumerate(samples):
        frac = (r["t"] - ep) / tf
        if frac < frac_lo or frac > frac_hi:
            continue
        mid_u = (r["up_bid"] + r["up_ask"]) / 2.0
        mid_d = (r["dn_bid"] + r["dn_ask"]) / 2.0

        active = True
        if gate is not None:
            # Quote only while the model still calls it a coin flip; pull once
            # banked evidence pushes p_up away from 0.5. Fail CLOSED: no fresh
            # model signal means no quote, which is both the honest accounting
            # and what the engine would have to do live.
            p = p_up_at(pup, r["t"]) if pup else None
            active = p is not None and abs(p - 0.5) <= gate
        if not active:
            b = d = None
            continue

        # requote on cadence: join the touch on both books. A side that filled
        # is left down until the next requote rather than instantly reloaded.
        if r["t"] >= next_quote:
            b, d = r["up_bid"], r["dn_bid"]
            next_quote = r["t"] + requote_s

        dt = (samples[i + 1]["t"] - r["t"]) if i + 1 < len(samples) else 0.0
        dt = min(dt, 5.0)
        quoting_s += dt

        # reward accrual for this sample
        q1r, q2r = side_scores(
            [(r["up_bid"], r["up_bid_sz"])], [(r["up_ask"], r["up_ask_sz"])],
            [(r["dn_bid"], r["dn_bid_sz"])], [(r["dn_ask"], r["dn_ask_sz"])],
            mid_u, mid_d, v, min_size)
        s_us_up = S(v, (mid_u - b) * 100.0) if b is not None else 0.0
        s_us_dn = S(v, (mid_d - d) * 100.0) if d is not None else 0.0
        if X >= min_size:
            q1u, q2u = s_us_up * X, s_us_dn * X
        else:
            q1u = q2u = 0.0
        ours = q_min(q1u, q2u, mid_u)
        tot = q_min(q1r + q1u, q2r + q2u, mid_u)
        share_acc.append((ours / tot if tot > 0 else 0.0, dt))

        # fills between this sample and the next
        if i + 1 < len(samples):
            nx = samples[i + 1]
            eps = 1e-9
            hit_up = b is not None and (
                (nx["up_ask"] <= b + eps) if queue_front else (nx["up_ask"] < b - eps))
            hit_dn = d is not None and (
                (nx["dn_ask"] <= d + eps) if queue_front else (nx["dn_ask"] < d - eps))
            if hit_up:
                got = min(X, max(r["up_bid_sz"], min_size))
                sh_up += got
                cost += got * b
                fills += 1
                b = None
            if hit_dn:
                got = min(X, max(r["dn_bid_sz"], min_size))
                sh_dn += got
                cost += got * d
                fills += 1
                d = None

    if not share_acc or quoting_s <= 0:
        return None
    wsum = sum(w for _, w in share_acc)
    share = sum(s * w for s, w in share_acc) / wsum if wsum else 0.0
    # reward accrues only over the fraction of the window we were actually quoting
    reward = pot * share * (quoting_s / tf)

    payoff = sh_up * up_won + sh_dn * (1.0 - up_won)
    pnl = payoff - cost
    paired = min(sh_up, sh_dn)
    shares_traded = sh_up + sh_dn
    return dict(reward=reward, pnl=pnl, net=reward + pnl, fills=fills,
                shares=shares_traded, paired=paired, share=share,
                active_frac=quoting_s / tf, up_won=up_won)


def stage_adverse(args):
    rows = load_book_tape(args.book_tape)
    wins = by_window(rows)
    pup = load_p_up(args.updown_tape) if args.gate is not None or args.compare_gate else {}

    v, ms = args.max_spread, args.min_size
    print("maker simulation: two-sided touch quoting, maker fee = 0")
    print("v=%.1fc  minSize=%g  requote=%.0fs  window frac [%.2f, %.2f]"
          % (v, ms, args.requote_s, args.frac_lo, args.frac_hi))
    print()

    configs = [("always-on", None)]
    if args.compare_gate:
        for g in args.gates:
            configs.append((f"gated |p-.5|<={g}", g))
    elif args.gate is not None:
        configs.append((f"gated |p-.5|<={args.gate}", args.gate))

    results = {}
    for label, gate in configs:
        print("=== %s ===" % label)
        print("%-9s %6s %7s %8s %10s %10s %10s %9s %11s %8s" % (
            "sym/tf", "wins", "act.f", "fills/w", "reward/w", "adverse/w",
            "net/w", "c/share", "b/e pot/w", "pot mult"))
        for key in sorted({(parse_slug(s)[0], parse_slug(s)[1]) for s in wins}):
            sym, tf = key
            pot = pot_per_window(sym, tf)
            rs = []
            for slug, ss in wins.items():
                if parse_slug(slug)[:2] != key:
                    continue
                r = simulate_window(ss, args.size, v, ms, pot, args.frac_lo,
                                    args.frac_hi, args.requote_s,
                                    pup.get(slug), gate, args.queue_front)
                if r:
                    rs.append(r)
            if len(rs) < args.min_windows:
                continue
            n = len(rs)
            mean = lambda f: sum(f(x) for x in rs) / n
            tot_sh = sum(x["shares"] for x in rs)
            cps = 100.0 * sum(x["pnl"] for x in rs) / tot_sh if tot_sh else 0.0
            # fill-model-robust framing: the pot this series would need before
            # rewards cover the measured adverse selection, and how many times
            # bigger that is than the pot the docs advertise.
            eff_share = mean(lambda x: x["share"] * x["active_frac"])
            loss = -mean(lambda x: x["pnl"])
            be = loss / eff_share if eff_share > 0 else float("inf")
            mult = pot / be if be > 0 and be != float("inf") else 0.0
            print("%-9s %6d %7.2f %8.1f %10s %10s %10s %9.2f %11s %8.3f" % (
                f"{sym}/{tf}", n, mean(lambda x: x["active_frac"]),
                mean(lambda x: x["fills"]),
                "$%.3f" % mean(lambda x: x["reward"]),
                "$%.2f" % mean(lambda x: x["pnl"]),
                "$%.2f" % mean(lambda x: x["net"]), cps,
                "$%.0f" % be, mult))
            results[(label, f"{sym}/{tf}")] = dict(
                n=n, reward=mean(lambda x: x["reward"]),
                adverse=mean(lambda x: x["pnl"]), net=mean(lambda x: x["net"]),
                fills=mean(lambda x: x["fills"]),
                active_frac=mean(lambda x: x["active_frac"]),
                share=mean(lambda x: x["share"]), c_per_share=cps,
                breakeven_pot=be, pot_multiple_needed=(1.0 / mult if mult else None),
                paired_frac=(sum(2 * x["paired"] for x in rs) / tot_sh
                             if tot_sh else 0.0))
        print()
    _dump(args, "adverse", {f"{a}|{b}": v2 for (a, b), v2 in results.items()})
    return results


# ---------------------------------------------------------------- stage: net

def boot_ci(vals, n=2000, seed=7, lo=2.5, hi=97.5):
    if not vals:
        return (0.0, 0.0)
    rnd = random.Random(seed)
    k = len(vals)
    means = []
    for _ in range(n):
        means.append(sum(vals[rnd.randrange(k)] for _ in range(k)) / k)
    means.sort()
    return (means[int(lo / 100 * n)], means[min(n - 1, int(hi / 100 * n))])


def stage_net(args):
    rows = load_book_tape(args.book_tape)
    wins = by_window(rows)
    pup = load_p_up(args.updown_tape)
    v, ms = args.max_spread, args.min_size

    keys = sorted({(parse_slug(s)[0], parse_slug(s)[1]) for s in wins})
    print("config sweep: net $/window = reward - adverse selection (maker fee = 0)")
    print("bootstrap 2000x, seed 7, per-window resample")
    print()
    table = []
    for key in keys:
        sym, tf = key
        pot = pot_per_window(sym, tf)
        wpd = 86400.0 / TF_SECS[tf]
        slugs = [s for s in wins if parse_slug(s)[:2] == key]
        if len(slugs) < args.min_windows:
            continue
        print("--- %s/%s   pot/window $%.2f (%d windows)" % (sym, tf, pot, len(slugs)))
        print("%6s %8s %8s %10s %10s %10s %-24s %10s" % (
            "size", "gate", "act.f", "reward/w", "adv/w", "net/w", "95% CI net/w", "net/day"))
        for X in args.sizes:
            for gate in ([None] + args.gates):
                rs = []
                for s in slugs:
                    r = simulate_window(wins[s], X, v, ms, pot, args.frac_lo,
                                        args.frac_hi, args.requote_s,
                                        pup.get(s), gate, args.queue_front)
                    if r:
                        rs.append(r)
                if len(rs) < args.min_windows:
                    continue
                n = len(rs)
                nets = [x["net"] for x in rs]
                m = sum(nets) / n
                ci = boot_ci(nets)
                row = dict(sym=sym, tf=tf, size=X, gate=gate, n=n,
                           reward=sum(x["reward"] for x in rs) / n,
                           adverse=sum(x["pnl"] for x in rs) / n,
                           net=m, ci_lo=ci[0], ci_hi=ci[1],
                           active_frac=sum(x["active_frac"] for x in rs) / n,
                           net_per_day=m * wpd, pot=pot)
                table.append(row)
                print("%6d %8s %8.2f %10s %10s %10s %-24s %10s" % (
                    X, gate if gate is not None else "off", row["active_frac"],
                    "$%.3f" % row["reward"], "$%.2f" % row["adverse"],
                    "$%.2f" % m, "[%.2f, %.2f]" % ci, "$%.0f" % row["net_per_day"]))
        print()
    _dump(args, "net", table)
    return table


def _dump(args, stage, obj):
    if not args.json_out:
        return
    path = args.json_out.replace("STAGE", stage)
    with open(path, "w") as f:
        json.dump(obj, f, indent=1, default=str)
    print("wrote %s" % path)


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("stage", choices=["config", "depth", "live", "adverse", "net",
                                      "collect", "all"])
    ap.add_argument("--book-tape", default=BOOK_TAPE)
    ap.add_argument("--updown-tape", default=UPDOWN_TAPE)
    ap.add_argument("--depth-tape", default=None)
    ap.add_argument("--max-spread", type=float, default=1.5,
                    help="rewardsMaxSpread in cents (live value is 1.5)")
    ap.add_argument("--min-size", type=float, default=50.0)
    ap.add_argument("--size", type=int, default=200, help="shares a side")
    ap.add_argument("--sizes", type=int, nargs="+", default=[50, 100, 200, 500])
    ap.add_argument("--gate", type=float, default=None,
                    help="quote only while |p_up-0.5| <= gate")
    ap.add_argument("--gates", type=float, nargs="+", default=[0.20, 0.10, 0.05])
    ap.add_argument("--compare-gate", action="store_true")
    ap.add_argument("--frac-lo", type=float, default=0.20)
    ap.add_argument("--frac-hi", type=float, default=0.80)
    ap.add_argument("--requote-s", type=float, default=5.0)
    ap.add_argument("--queue-front", action="store_true",
                    help="assume front-of-queue instead of behind the displayed size")
    ap.add_argument("--min-windows", type=int, default=15)
    ap.add_argument("--collect-secs", type=float, default=2700.0)
    ap.add_argument("--collect-period", type=float, default=20.0)
    ap.add_argument("--json-out", default=None,
                    help="path with STAGE placeholder, e.g. out-STAGE.json")
    args = ap.parse_args()

    if args.stage == "collect":
        stage_collect(args)
        return
    if args.stage in ("config", "all"):
        stage_config(args)
        print()
    if args.stage in ("depth", "all"):
        stage_depth(args)
        print()
    if args.stage in ("live", "all"):
        stage_live(args)
        print()
    if args.stage in ("adverse", "all"):
        args.compare_gate = True
        stage_adverse(args)
        print()
    if args.stage in ("net", "all"):
        stage_net(args)


if __name__ == "__main__":
    main()
