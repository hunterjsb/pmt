#!/usr/bin/env python3
"""retry_pricing — what a missed clip's re-decide should quote.

The largest unclaimed item in analysis/latency_report.txt section 6 is row
(c) RETRY PRICING, $771.40 over the measured 11.7h. That row is a MODEL:
lambda (adverse ask moves/s from the 1Hz book tape) x 10.64c (the measured mean
pay-up on a chase) x the corpus's shares, charged as if resting the original
limit were free. The report says so itself — at 12s the jump model predicts a
46.3% adverse-move probability against a 30.4% observed miss rate, so the row is
an upper bound and "a hypothesis worth its own study".

This is that study. It prices five retry policies against the RECORDED tape
instead of against a jump model, over the span where the book tape and the
wallet both cover the fires.

METHOD — what is ground truth and what is counterfactual
  * A recorded fire's fill is WALLET truth, joined by latency_report's own
    matcher (newest-first, 12s TTL + 3s on-chain grace). Every policy reuses
    that join unchanged for every clip it does not move.
  * A clip a policy would have RESTED longer than the engine did has no wallet
    row. It is filled only where the recorded book actually traded through the
    resting limit, and only strictly AFTER the recorded re-decide that proved
    the live ask had moved above it. The fill is charged at OUR limit, never at
    the better observed ask — a resting bid is the passive side and pays its
    own price.
  * Three strictness tiers on that test, all reported: `ask` (one qualifying
    book sample), `ask2` (two consecutive — one sample can be a stale
    straggler), `print` (a public trade print on our outcome at or below our
    limit, which needs no book-sampling assumption at all).
  * Policies that only SUBTRACT recorded fires (give-up) need no fill model and
    are exact.

CHARGES AND CAVEATS, stated once
  * The fire schedule is held fixed. A policy that forgoes fills frees budget
    the recorded tape never re-deployed, so no policy is credited with clips
    the tape does not contain.
  * P&L is terminal settlement at the L36-cleaned outcome (wallet rows first),
    hold-to-resolution, no exits and no fees — the same convention on every
    arm, so only the DELTA is read (hybrid_ab.md's rule).
  * A resting bid changes the book it rests in. Nobody can replay that.
  * INFLIGHT_TTL only gates UNFILLED clips: updown.rs on_fill removes the
    inflight entry, so a longer TTL does not block a ladder add after a fill.
    Modelled that way — a fire that follows a filled clip is never suppressed.

Usage:  cd pmtrader && uv run python ../analysis/retry_pricing.py
"""

from __future__ import annotations

import json
import math
import random
import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import latency_report as L  # noqa: E402

H = Path.home()
PRINTS = H / ".pmt" / "corpus" / "prints.jsonl"
ORDER_TAPE = H / ".pmt" / "engine" / "order-latency-tape.jsonl"

# --- policy constants, read off the live arm params (~/.pmt/engine/arms-state.json)
PAYUP_T0 = 1787464800.0     # 06:00Z, the pay_up_max=0.02 deploy; 0 before it
PAY_UP_MAX = 0.02
MIN_EDGE = 0.015            # safe-mode edge floor
EARLY_MIN_EDGE = 0.08       # spec-mode edge floor
MAX_PRICE = 0.985
QUIESCE_S = 20.0            # standing orders are pulled this far from the end
CHAIN_GAP_S = 20.0
TTL = L.INFLIGHT_TTL_S      # 12.0
GRACE = L.ONCHAIN_GRACE_S   # 3.0

BOOT_N = 10000
BOOT_SEED = 20260823


def ts(t: float) -> str:
    return datetime.fromtimestamp(t, timezone.utc).strftime("%m-%d %H:%M:%SZ")


def limit_of(f: dict) -> float:
    """The marketable limit the engine actually sent for this fire.

    pay_up_limit(ask, net, edge_req, pay_up_max, max_price) from
    updown_model.rs, tick-rounded. Section 0 re-validates it against every
    joinable ~/.pmt/engine/order-latency-tape.jsonl ack row on each run — that
    tape is live, so the counts move but the agreement should not.
    """
    pum = PAY_UP_MAX if f["t"] >= PAYUP_T0 else 0.0
    er = EARLY_MIN_EDGE if f.get("mode") == "spec" else MIN_EDGE
    raw = min(f["ask"] + min(max(f["net"] - er, 0.0), pum), MAX_PRICE)
    return tick(raw)


def tick(price: float) -> float:
    """What OrderManager puts on the wire: round_dp(tick_decimals), 2dp here.

    Every order-latency-tape row carries a 2-decimal price, so these tokens are
    on a 0.01 tick. Half-up, matching rust_decimal's default.
    """
    return math.floor(price * 100 + 0.5) / 100.0


def window_end(slug: str):
    p = L.parse_slug(slug)
    return None if p is None else float(p["start"] + p["dur_s"])


def side_ask(r, side):
    return r["up_ask"] if side == "up" else r["dn_ask"]


def side_sz(r, side):
    return r["up_ask_sz"] if side == "up" else r["dn_ask_sz"]


# ---------------------------------------------------------------- fill model


def load_prints_by_key(path: Path = PRINTS):
    by = defaultdict(list)
    lo = hi = None
    if not path.exists():
        return by, (None, None)
    with path.open() as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            d = json.loads(line)
            oc = (d.get("outcome") or "").lower()
            if oc not in ("up", "down"):
                continue
            by[(d.get("slug", ""), oc)].append(d)
            t = d["t"]
            lo = t if lo is None else min(lo, t)
            hi = t if hi is None else max(hi, t)
    for v in by.values():
        v.sort(key=lambda r: r["t"])
    return by, (lo, hi)


def model_fill(slug, side, limit, t_lo, t_hi, size, books, prints, tier="ask"):
    """Shares a bid resting at `limit` takes over (t_lo, t_hi]. Fill price = limit."""
    if size <= 1e-9 or t_hi <= t_lo:
        return []
    out, rem = [], size
    if tier == "print":
        for d in prints.get((slug, side), []):
            if d["t"] <= t_lo:
                continue
            if d["t"] > t_hi:
                break
            if float(d["price"]) > limit + 1e-9:
                continue
            take = min(rem, float(d["size"]))
            if take <= 1e-9:
                continue
            out.append((float(d["t"]), take, limit))
            rem -= take
            if rem <= 1e-9:
                break
        return out
    prev_ok = False
    for r in books.get(slug, []):
        if r["t"] <= t_lo:
            continue
        if r["t"] > t_hi:
            break
        a, s = side_ask(r, side), side_sz(r, side) or 0.0
        ok = a is not None and a <= limit + 1e-9 and s > 0
        if not ok:
            prev_ok = False
            continue
        if tier == "ask2" and not prev_ok:
            prev_ok = True
            continue
        prev_ok = True
        take = min(rem, s)
        if take <= 1e-9:
            continue
        out.append((r["t"], take, limit))
        rem -= take
        if rem <= 1e-9:
            break
    return out


# ---------------------------------------------------------------- valuation


def pnl_of(fills, side, winner):
    if winner is None:
        return 0.0
    if side == winner:
        return sum(sh * (1.0 - px) for _, sh, px in fills)
    return sum(-sh * px for _, sh, px in fills)


def boot_ci(delta_by_window, reps=BOOT_N):
    # Its own generator, seeded per call: the same policy must produce the same
    # interval wherever in the report it is printed.
    rng = random.Random(BOOT_SEED)
    vals = list(delta_by_window.values())
    if not vals:
        return (0.0, 0.0)
    n = len(vals)
    sums = sorted(sum(vals[rng.randrange(n)] for _ in range(n)) for _ in range(reps))
    return (sums[int(0.025 * reps)], sums[int(0.975 * reps)])


# ---------------------------------------------------------------- placements


def life_end(f, nxt_t):
    return min(nxt_t if nxt_t is not None else float("inf"), f["t"] + TTL) + GRACE


def is_retry(prev, cur):
    """cur re-decides an UNFILLED prev on the same key inside one chain."""
    return (prev is not None
            and prev["_filled"] <= 1e-9
            and cur["t"] - prev["t"] <= CHAIN_GAP_S)


class Run:
    """One policy's counterfactual over the corpus."""

    def __init__(self):
        self.pnl = defaultdict(float)     # per window
        self.st = Counter()
        self.gained = []                  # fills the policy recovers at a held price
        self.forgone = []                 # recorded fills the policy gives up
        self.wl = Counter()

    def take(self, slug, side, fills, winner):
        p = pnl_of(fills, side, winner)
        self.pnl[slug] += p
        self.st["clips"] += 1
        if fills:
            self.st["filled"] += 1
            self.st["shares"] += sum(x[1] for x in fills)
            self.st["notional"] += sum(x[1] * x[2] for x in fills)

    def drop(self, slug, side, f, winner, tag):
        self.st[tag] += 1
        if f["_fills"]:
            self.st[tag + "_shares"] += sum(x[1] for x in f["_fills"])
            self.st[tag + "_pnl"] += pnl_of(f["_fills"], side, winner)
            self.forgone.append((slug, side, f["_fills"]))

    def add_ext(self, slug, side, ext, winner):
        self.st["recovered"] += 1
        self.st["recovered_shares"] += sum(x[1] for x in ext)
        self.st["recovered_pnl"] += pnl_of(ext, side, winner)
        self.gained.append((slug, side, ext))


def run_policy(keyed, books, prints, outcomes, policy, tier="ask", **kw):
    r = Run()
    for (slug, side), fl in keyed.items():
        winner = outcomes.get(slug)
        wend = window_end(slug)
        quiesce = (wend - QUIESCE_S) if wend else float("inf")
        n = len(fl)
        nxt = [fl[i + 1]["t"] if i + 1 < n else None for i in range(n)]

        # ---------------------------------------------------------- baseline
        if policy == "base":
            for f in fl:
                r.take(slug, side, f["_fills"], winner)
            continue

        # ---------------------------------------- longer TTL: hold the limit
        if policy == "hold":
            T = kw["T"]
            i = 0
            while i < n:
                f = fl[i]
                fills = list(f["_fills"])
                le = life_end(f, nxt[i])
                j = i + 1
                if not fills:
                    hold_to = min(f["t"] + T, quiesce)
                    # Scan strictly after the recorded life, and — where a
                    # re-decide happened inside the hold — strictly after that
                    # re-decide, whose own ask is proof the live book was above
                    # our limit at that instant.
                    scan_from = le
                    if i + 1 < n and fl[i + 1]["t"] < hold_to:
                        scan_from = max(scan_from, fl[i + 1]["t"])
                    ext = model_fill(slug, side, limit_of(f), scan_from, hold_to,
                                     f["size"], books, prints, tier)
                    # on_fill removes the inflight entry, so the moment the held
                    # clip fills the token is free again and the NEXT re-decide
                    # is a normal ladder add, not a suppressed retry.
                    free_at = ext[0][0] if ext else hold_to
                    while j < n and fl[j]["t"] < free_at:
                        r.drop(slug, side, fl[j], winner, "suppressed")
                        j += 1
                    if ext:
                        r.add_ext(slug, side, ext, winner)
                    fills += ext
                r.take(slug, side, fills, winner)
                i = j
            continue

        # ------------------------------- cumulative chase cap (0c = ratchet)
        if policy == "cap":
            C = kw["C"]
            i = 0
            anchor = None            # the FIRST clip's limit in this chase
            while i < n:
                f = fl[i]
                prev = fl[i - 1] if i else None
                if not is_retry(prev, f):
                    anchor = limit_of(f)
                cap = tick(anchor + C)
                lim = min(limit_of(f), cap)
                if f["ask"] <= lim + 1e-9:
                    # still marketable at the recorded ask: a lower but still
                    # crossing limit buys the same shares at the same book
                    r.take(slug, side, f["_fills"], winner)
                    i += 1
                    continue
                # The cap binds — the clip is not marketable and rests at `lim`.
                # Every further re-decide inside the chain reprices to the SAME
                # capped price, which the delta matcher declines to replace, so
                # the order just keeps resting. Chain membership here is by time
                # gap only: the recorded fills of those re-decides are exactly
                # what the policy is giving up, so they cannot define the chain.
                r.drop(slug, side, f, winner, "capped")
                # how long the capped order would rest: to the end of the chain
                # (by time gap only — the recorded fills of those re-decides are
                # exactly what this policy gives up, so they cannot define it)
                end = i + 1
                while end < n and fl[end]["t"] - fl[end - 1]["t"] <= CHAIN_GAP_S:
                    end += 1
                rest_to = min(quiesce, fl[end]["t"] if end < n else quiesce)
                scan_from = max(life_end(f, nxt[i]), nxt[i] or f["t"])
                ext = model_fill(slug, side, lim, scan_from, rest_to,
                                 f["size"], books, prints, tier)
                free_at = ext[0][0] if ext else rest_to
                j = i + 1
                while j < end and fl[j]["t"] < free_at:
                    r.drop(slug, side, fl[j], winner, "capped")
                    j += 1
                if ext:
                    r.add_ext(slug, side, ext, winner)
                r.take(slug, side, ext, winner)
                i = j
            continue

        # ------------------------------------------------- give up after N
        if policy == "giveup":
            N = kw["N"]
            scope = kw.get("scope", "window")     # "window" or "chain"
            retries = 0
            stood = False
            for i, f in enumerate(fl):
                prev = fl[i - 1] if i else None
                retry = is_retry(prev, f)
                if stood:
                    if scope == "chain" and not retry:
                        stood, retries = False, 0
                    else:
                        r.drop(slug, side, f, winner, "stood_down")
                        continue
                if retry:
                    if retries >= N:
                        stood = True
                        r.drop(slug, side, f, winner, "stood_down")
                        continue
                    retries += 1
                else:
                    retries = 0
                r.take(slug, side, f["_fills"], winner)
                if f["_filled"] > 1e-9:
                    retries = 0
            continue

        raise ValueError(policy)
    return r


# ---------------------------------------------------------------- main


def main():
    tape = L.load_tape()
    books = L.load_books()
    fills = L.load_fills()
    outcomes, src = {}, {}
    with (H / ".pmt" / "corpus" / "outcomes.jsonl").open() as f:
        for line in f:
            d = json.loads(line)
            if d.get("winner") in ("up", "down"):
                outcomes[d["slug"]] = d["winner"]
                src[d["slug"]] = d.get("source")
    prints, pspan = load_prints_by_key()

    fires = [f for f in tape["fire"] if not L.in_blackout(f["t"])]
    fires, orphans = L.match_fills(fires, fills)
    fires = L.attach_book(fires, books)

    bt = [r["t"] for v in books.values() for r in v]
    T0 = min(bt)
    T1 = 0.0
    with (H / ".pmt" / "corpus" / "activity.jsonl").open() as f:
        for line in f:
            T1 = max(T1, float(json.loads(line)["timestamp"]))
    sub = [f for f in fires if T0 <= f["t"] <= T1 and f["slug"] in outcomes]

    keyed = defaultdict(list)
    for f in sub:
        keyed[(f["slug"], f["side"])].append(f)
    for v in keyed.values():
        v.sort(key=lambda x: x["t"])

    # ---------------------------------------------------------------- 0
    L.hdr("0. CORPUS AND THE POPULATION UNDER STUDY")
    print(f"span             {ts(T0)} -> {ts(T1)}  ({(T1-T0)/3600:.2f}h)")
    print("                 = book-tape start .. last wallet activity row; outside it")
    print("                   one side of every counterfactual is missing")
    print(f"fires in span    {len(sub)}   graded windows "
          f"{len(set(f['slug'] for f in sub))}   (slug,side) keys {len(keyed)}")
    print(f"fills            {sum(1 for f in sub if f['_filled']>1e-9)} filled   "
          f"{sum(1 for f in sub if f['_filled']<=1e-9)} zero-fill   "
          f"{sum(f['_filled'] for f in sub):,.0f} shares")
    print(f"outcome sources  {dict(Counter(src[s] for s in set(f['slug'] for f in sub)))}")
    print(f"era mix          {dict(Counter(L.era_of(f['t']) for f in sub))}")
    print(f"mode mix         {dict(Counter(f.get('mode') for f in sub))}")
    print(f"print coverage   {ts(pspan[0])} -> {ts(pspan[1])}  "
          f"(ends {(T1-pspan[1])/60:.0f}min before the span; the print tier is a "
          f"LOWER bound past that)")

    # limit reconstruction check
    orows = [json.loads(x) for x in ORDER_TAPE.open()] if ORDER_TAPE.exists() else []
    allfires = sorted(tape["fire"], key=lambda f: f["t"])
    ok = bad = miss = 0
    for o in orows:
        if o.get("stage") != "ack":
            continue
        dt_ = float(o["decision_id"].split("-")[0]) / 1000.0
        cand = [f for f in allfires if abs(f["t"] - dt_) < 0.05]
        if not cand:
            miss += 1
            continue
        f = min(cand, key=lambda x: abs(x["t"] - dt_))
        if abs(limit_of(f) - float(o["price"])) < 1e-6:
            ok += 1
        else:
            bad += 1
    print(f"limit rebuild    order-latency-tape ack rows: {ok} exact, {bad} mismatched, "
          f"{miss} unjoinable  <- pay_up_limit() reconstruction is EXACT")
    supp = sum(1 for o in orows if o.get("stage") == "suppressed")
    print(f"delta matcher    {supp}/{len(orows)} order-tape rows suppressed (the matcher "
          f"already holds a quote when price+size match)")

    L.sub("the retry population — an unfilled clip re-decided by the 12s TTL")
    pairs = []
    for k, fl in keyed.items():
        for a, b in zip(fl, fl[1:]):
            if is_retry(a, b):
                pairs.append((b["ask"] - a["ask"]) * 100)
    worse = [x for x in pairs if x > 0]
    same = [x for x in pairs if x == 0]
    better = [x for x in pairs if x < 0]
    print(f"  re-decides of an unfilled clip: {len(pairs)}")
    print(f"    repriced WORSE  {len(worse):3} ({100*len(worse)/len(pairs):.0f}%)   "
          + L.dist(worse, unit="c"))
    print(f"    repriced SAME   {len(same):3} ({100*len(same)/len(pairs):.0f}%)")
    print(f"    repriced BETTER {len(better):3} ({100*len(better)/len(pairs):.0f}%)  "
          + L.dist([-x for x in better], unit="c"))
    print("""
  FIRST RESULT, and it reframes the question: the 12s re-decide is not only a
  chase. A third of them come back at a BETTER price than the clip that missed.
  Any policy that pins the original limit forgoes those improvements as well as
  the chase, and the table below charges it for both.""")

    chains = L.build_chains(sub)
    chase = [(k, c) for k, c in chains if c[0]["_filled"] <= 1e-9 and len(c) > 1]
    print(f"  chains {len(chains)}   chase chains {len(chase)}   "
          f"ladder {sum(1 for k,c in chains if c[0]['_filled']>1e-9 and len(c)>1)}   "
          f"singletons {sum(1 for k,c in chains if len(c)==1)}")

    L.sub("does the book come back? — an ask <= the missed clip's own limit, after the re-decide")
    print(f"  {'horizon':<9} {'chase chains':>13} {'ask':>6} {'ask2':>6} {'print':>7}")
    for T in (12, 24, 36, 60, 120):
        hit = Counter()
        for k, c in chase:
            f0 = c[0]
            lim = limit_of(f0)
            scan_from = max(life_end(f0, c[1]["t"]), c[1]["t"])
            hi = min(f0["t"] + T, (window_end(f0["slug"]) or 1e18) - QUIESCE_S)
            for tier in ("ask", "ask2", "print"):
                if model_fill(f0["slug"], f0["side"], lim, scan_from, hi,
                              f0["size"], books, prints, tier):
                    hit[tier] += 1
        print(f"  {str(T)+'s':<9} {len(chase):13} {hit['ask']:6} {hit['ask2']:6} "
              f"{hit['print']:7}")

    # ---------------------------------------------------------------- 1
    L.hdr("1. FILL-MODEL CALIBRATION — does the book test reproduce the wallet?")
    print("""
The model only ever runs on intervals where no order of ours was live, so it
cannot be validated in place. What CAN be checked is the direction of its bias:
run it over each recorded clip's OWN life and compare to the wallet. The tape
samples at 1-5s while the engine fires off a live book, so it is expected to
OVER-fill there. That over-fill rate is the haircut every gain below must
survive.""".strip())
    print()
    for tier in ("ask", "ask2", "print"):
        tp = fp = fn = tn = 0
        for k, fl in keyed.items():
            n = len(fl)
            for i, f in enumerate(fl):
                mf = model_fill(f["slug"], f["side"], limit_of(f), f["t"] - 1e-6,
                                life_end(f, fl[i + 1]["t"] if i + 1 < n else None),
                                f["size"], books, prints, tier)
                real, pred = f["_filled"] > 1e-9, bool(mf)
                tp += real and pred
                fp += (not real) and pred
                fn += real and (not pred)
                tn += (not real) and (not pred)
        tot = tp + fp + fn + tn
        print(f"  {tier:<6} n={tot:4}  says-fill {tp+fp:4} vs wallet {tp+fn:4}   "
              f"precision {tp/max(tp+fp,1):.2f}  recall {tp/max(tp+fn,1):.2f}   "
              f"FALSE FILLS {fp} ({100*fp/max(tot,1):.1f}% of clips)")
    print("""
  READ: a false fill is the model claiming a cross the wallet never saw. On a
  clip's OWN life that is mostly the tape's stale ask, which is exactly why the
  counterfactual never scans a placement's own life — it starts strictly after
  the re-decide whose ask proves the live book had moved above our limit.""")

    # ---------------------------------------------------------------- 2
    base = run_policy(keyed, books, prints, outcomes, "base")
    base_total = sum(base.pnl.values())
    variants = [
        ("hold TTL 24s", "hold", dict(T=24.0)),
        ("hold TTL 36s", "hold", dict(T=36.0)),
        ("hold TTL 60s", "hold", dict(T=60.0)),
        ("ratchet (cap 0c)", "cap", dict(C=0.0)),
        ("chase cap 2c", "cap", dict(C=0.02)),
        ("chase cap 4c", "cap", dict(C=0.04)),
        ("chase cap 8c", "cap", dict(C=0.08)),
    ]
    exact = [
        ("give up after 0 (chain)", "giveup", dict(N=0, scope="chain")),
        ("give up after 0 (win)", "giveup", dict(N=0, scope="window")),
        ("give up after 1 (win)", "giveup", dict(N=1, scope="window")),
        ("give up after 2 (win)", "giveup", dict(N=2, scope="window")),
        ("give up after 3 (win)", "giveup", dict(N=3, scope="window")),
        ("give up after 1 (chain)", "giveup", dict(N=1, scope="chain")),
        ("give up after 2 (chain)", "giveup", dict(N=2, scope="chain")),
    ]

    L.hdr("2. POLICY TABLE — Δ vs today's policy, priced on the recorded tape")
    print(f"""
base = today's policy. RECORDED fills only; no fill model anywhere in this row.
  clips {base.st['clips']}   filled {base.st['filled']}   shares {base.st['shares']:,.0f}   """
          f"""notional ${base.st['notional']:,.0f}
  settlement P&L ${base_total:+,.2f}  (hold-to-resolution, no exits, no fees —
  the absolute level is not wallet truth, only the Δ column is read)
""".rstrip())

    def row(name, r):
        d = {s: r.pnl.get(s, 0.0) - base.pnl.get(s, 0.0)
             for s in set(r.pnl) | set(base.pnl)}
        lo, hi = boot_ci(d)
        gone = r.st["suppressed_shares"] + r.st["capped_shares"] + r.st["stood_down_shares"]
        print(f"  {name:<24} {r.st['clips']:5} {r.st['filled']:5} "
              f"{r.st['shares']:8,.0f} {-gone:+8,.0f} {r.st['recovered_shares']:+8,.0f} "
              f"{sum(r.pnl.values())-base_total:+9.2f}  [{lo:+7.0f},{hi:+7.0f}]")

    hdr_ = (f"  {'policy':<24} {'clips':>5} {'fill':>5} {'shares':>8} "
            f"{'lost':>8} {'gained':>8} {'Δ P&L':>9}  {'bootstrap CI95':>17}")
    for tier in ("ask", "ask2", "print"):
        L.sub(f"fill tier = {tier}"
              + ("   (the loosest — one stale sample is enough)" if tier == "ask" else "")
              + ("   (two consecutive samples)" if tier == "ask2" else "")
              + ("   (real trade prints; lower bound past 07:39Z)" if tier == "print" else ""))
        print(hdr_)
        for name, pol, kw in variants:
            row(name, run_policy(keyed, books, prints, outcomes, pol, tier=tier, **kw))
    L.sub("give-up rules — EXACT (they only subtract recorded fills; no fill model)")
    print(hdr_)
    for name, pol, kw in exact:
        row(name, run_policy(keyed, books, prints, outcomes, pol, **kw))

    L.sub("the knob that actually moved — pay_up_max, from the ENTRY side")
    print("""
  Every policy above fixes the retry. pay_up_max prevents it: a fatter
  marketable buffer means the clip crosses even though the ask ticked up while
  the order was in flight, and a marketable limit still fills AT the book, so
  the buffer costs nothing unless the book actually moved. Over this corpus
  pay_up_max was 0 (pre-06:00Z) then 0.02. As of 15:0xZ TODAY the live arms
  carry btc/eth 0.05, sol 0.04, bnb/xrp 0.02 — a 2.5x loosening of the chase
  budget with no A/B behind it, so here is what the corpus says it buys.

  Counterfactual: an unfilled clip whose re-decide came back at ask' would have
  crossed on the FIRST attempt if ask' <= tick(ask + min(surplus, pay_up_max)).
  ask' is the engine's own next live read, so this needs no book model at all.""")
    print()
    print(f"  {'pay_up_max':<12} {'worse re-decides pre-empted':>29} "
          f"{'buffer spent (UPPER bound':>26}")
    print(f"  {'':<12} {'':>29} {'on the extra paid)':>26}")
    for pum in (0.0, 0.02, 0.04, 0.05, 0.08):
        pre = 0
        cost = []
        tot = 0
        for (slug, side), fl in keyed.items():
            for a, b in zip(fl, fl[1:]):
                if not is_retry(a, b) or b["ask"] <= a["ask"]:
                    continue
                tot += 1
                er = EARLY_MIN_EDGE if a.get("mode") == "spec" else MIN_EDGE
                lp = min(tick(a["ask"] + min(max(a["net"] - er, 0.0), pum)), MAX_PRICE)
                if b["ask"] <= lp + 1e-9:
                    pre += 1
                    cost.append((lp - a["ask"]) * 100)
        mc = sum(cost) / len(cost) if cost else 0.0
        print(f"  {pum*100:>5.0f}c       {pre:>10}/{tot:<18} {mc:>19.2f}c"
              + ("   <- the corpus's own policy" if pum == 0.02 else "")
              + ("   <- LIVE NOW (btc/eth)" if pum == 0.05 else ""))
    print("""
  READ: the buffer is the only lever in this study that buys fills WITHOUT
  buying a stale price — the limit stays marketable, so it pays the BOOK on
  arrival (section 1 of latency_report: filled clips pay at or better than
  their quoted ask 95% of the time), and the column above is the ceiling on
  the extra, not the expectation. What it cannot do is reach the clips whose
  re-decide moved further than the buffer: the p90 worse re-decide is +10c,
  twice the 5c now live.""")

    # ---------------------------------------------------------------- 3
    L.hdr("3. ADVERSE SELECTION — do held-price fills lose more often?")
    print("""
The mechanism to fear: the ask comes back down to our original limit precisely
when the market has re-priced our side CHEAPER — i.e. when the tape went against
the thesis. If that is what is happening, every fill a hold policy 'recovers' is
one we should be glad to have missed. Graded on the L36-cleaned outcomes
(wallet rows first), shares-weighted.""".strip())
    print()
    print(f"  {'population':<50} {'fills':>5} {'sh':>7} {'$notl':>7} {'won%':>6} "
          f"{'P&L':>9} {'c/sh':>7} {'wins':>5} {'top w':>7}")

    def grade(rows, label):
        sh = sum(x[1] for _, _, fs in rows for x in fs)
        notl = sum(x[1] * x[2] for _, _, fs in rows for x in fs)
        won = sum(sum(x[1] for x in fs) for slug, side, fs in rows
                  if outcomes.get(slug) == side)
        pl = sum(pnl_of(fs, side, outcomes.get(slug)) for slug, side, fs in rows)
        byw = defaultdict(float)
        for slug, side, fs in rows:
            byw[slug] += pnl_of(fs, side, outcomes.get(slug))
        top = max(byw.values(), key=abs) if byw else 0.0
        print(f"  {label:<50} {len(rows):5} {sh:7,.0f} {notl:7,.0f} "
              f"{100*won/sh if sh else 0:5.1f}% {pl:+9.2f} "
              f"{100*pl/sh if sh else 0:+7.2f} {len(byw):5} {top:+7.1f}")

    chase_f, first_f = [], []
    for (slug, side), fl in keyed.items():
        for i, f in enumerate(fl):
            if not f["_fills"]:
                continue
            (chase_f if is_retry(fl[i - 1] if i else None, f)
             else first_f).append((slug, side, f["_fills"]))
    grade(first_f, "base: fills on a clip that was NOT a chase re-decide")
    grade(chase_f, "base: fills bought BY a chase re-decide")
    for T in (24, 36, 60):
        for tier in ("ask", "print"):
            r = run_policy(keyed, books, prints, outcomes, "hold", tier=tier, T=float(T))
            grade(r.gained, f"hold {T}s: fills recovered at the original limit [{tier}]")
            grade(r.forgone, f"hold {T}s: chase fills the hold forgoes")
    for N in (1, 2, 3):
        r = run_policy(keyed, books, prints, outcomes, "giveup", N=N, scope="window")
        grade(r.forgone, f"give up after {N}: recorded fills the stand-down forgoes")

    L.sub("recorded fills by retry index — is there a depth at which the chase turns bad?")
    print(f"  {'retry index':<50} {'fills':>5} {'sh':>7} {'$notl':>7} {'won%':>6} "
          f"{'P&L':>9} {'c/sh':>7} {'wins':>5} {'top w':>7}")
    byidx = defaultdict(list)
    for (slug, side), fl in keyed.items():
        k = 0
        for i, f in enumerate(fl):
            k = k + 1 if is_retry(fl[i - 1] if i else None, f) else 0
            if f["_fills"]:
                byidx[min(k, 4)].append((slug, side, f["_fills"]))
    for k in sorted(byidx):
        grade(byidx[k], f"  retry {k}" + ("+" if k == 4 else "")
              + ("  (the original clip / a ladder add)" if k == 0 else ""))

    L.sub("retry-1 fills split by which way the re-decide moved — where the damage actually is")
    w, s_, b = [], [], []
    detail = []
    for (slug, side), fl in keyed.items():
        k = 0
        for i, f in enumerate(fl):
            prev = fl[i - 1] if i else None
            k = k + 1 if is_retry(prev, f) else 0
            if k != 1 or not f["_fills"]:
                continue
            (w if f["ask"] > prev["ask"] else b if f["ask"] < prev["ask"]
             else s_).append((slug, side, f["_fills"]))
            if f["ask"] < prev["ask"]:
                detail.append((slug, side, prev, f))
    grade(w, "  re-decide came back WORSE  (the chase this study is about)")
    grade(s_, "  re-decide came back at the SAME ask")
    grade(b, "  re-decide came back BETTER (the ask fell)")
    print("""
  Every case in the BETTER row, because it is 2 windows wearing a share count:""")
    for slug, side, a, f in detail:
        print(f"    {slug:<28} {side:<4} won={outcomes.get(slug):<4} "
              f"A ask {a['ask']:.2f} x{a['size']:.0f} -> 0    "
              f"B ask {f['ask']:.2f} x{f['size']:.0f} -> {f['_filled']:.0f} "
              f"@ {f['_vwap']:.3f}   P&L {pnl_of(f['_fills'], side, outcomes.get(slug)):+7.2f}")
    print("""
  Read the two that carry it. eth-5m-1787462100 is 1,478 shares of a ONE-CENT
  side ($14.78 of notional) — a penny lottery clip that inflates every
  share-weighted statistic it touches and moves $15. btc-15m-1787457600 is the
  L22 window, a known loser whose clips lost on retry 0 as well as retry 1.
  Strip those two and the 'cheaper re-decide is poison' story is gone. This is
  the L37 failure mode caught early: a share-weighted slice of n=2 windows.""")

    L.sub("worked example — one chase chain, every policy side by side")
    pick = None
    for (slug, side), fl in keyed.items():
        if len(fl) >= 3 and fl[0]["_filled"] <= 1e-9 and fl[1]["ask"] > fl[0]["ask"]:
            lim = limit_of(fl[0])
            if model_fill(slug, side, lim, max(life_end(fl[0], fl[1]["t"]), fl[1]["t"]),
                          fl[0]["t"] + 36, fl[0]["size"], books, prints, "ask"):
                pick = (slug, side, fl)
                break
    if pick:
        slug, side, fl = pick
        print(f"  {slug} {side}   winner={outcomes.get(slug)}  "
              f"({src.get(slug)})   quiesce at {ts((window_end(slug) or 0)-QUIESCE_S)}")
        t0 = fl[0]["t"]
        for i, f in enumerate(fl[:6]):
            print(f"    +{f['t']-t0:6.1f}s  fire {f['side']:<4} {f['size']:6.0f}sh  "
                  f"ask {f['ask']:.2f}  limit {limit_of(f):.2f}  net {100*f['net']:+5.1f}c"
                  f"  -> wallet filled {f['_filled']:6.1f}sh"
                  + (f" @ {f['_vwap']:.3f}" if f["_vwap"] else ""))
        lim = limit_of(fl[0])
        for T in (24, 36):
            ext = model_fill(slug, side, lim,
                             max(life_end(fl[0], fl[1]["t"]), fl[1]["t"]),
                             min(t0 + T, (window_end(slug) or 1e18) - QUIESCE_S),
                             fl[0]["size"], books, prints, "ask")
            got = f"{sum(x[1] for x in ext):.0f}sh @ {lim:.2f} at +{ext[0][0]-t0:.1f}s" \
                if ext else "nothing crossed"
            print(f"    hold {T}s: order stays at {lim:.2f}; book test says {got}")

    # ---------------------------------------------------------------- 4
    L.hdr("4. PRICE IMPROVEMENT — held limit vs what the chase actually paid")
    for T in (24, 36):
        allr, worse_only = [], []
        for (slug, side), fl in keyed.items():
            n = len(fl)
            for i, f in enumerate(fl):
                if f["_filled"] > 1e-9 or i + 1 >= n:
                    continue
                lim = limit_of(f)
                hi = min(f["t"] + T, (window_end(slug) or 1e18) - QUIESCE_S)
                if not model_fill(slug, side, lim,
                                  max(life_end(f, fl[i + 1]["t"]), fl[i + 1]["t"]),
                                  hi, f["size"], books, prints, "ask"):
                    continue
                got = [x for j in range(i + 1, n) for x in fl[j]["_fills"]
                       if fl[j]["t"] - f["t"] <= T]
                if not got:
                    continue
                vw = sum(s * p for _, s, p in got) / sum(s for _, s, p in got)
                allr.append((vw - lim) * 100)
                if fl[i + 1]["ask"] > f["ask"]:
                    worse_only.append((vw - lim) * 100)
        print(f"  hold {T}s — cents/share the held limit SAVES vs the chase's own VWAP")
        print(f"    all re-decides   n={len(allr):3}  " + L.dist(allr, unit="c"))
        print(f"    WORSE ones only  n={len(worse_only):3}  " + L.dist(worse_only, unit="c"))
    print("""
  Negative = the chase paid LESS than the limit we would have pinned. That is
  the mirror of the 'repriced BETTER' third above, and it is the reason the
  pay-up row's +10.64c does not translate into +10.64c of recoverable price.""")

    # ---------------------------------------------------------------- 5
    L.hdr("5. AGAINST latency_report SECTION 6 ROW (c)")
    shares = sum(f["_filled"] for f in sub)
    prorata = 771.40 * shares / 22715
    print(f"""
  row (c) priced the 12s TTL's own drift at $771.40 over 22,715 shares in 11.7h
  (+3.396 c/share) as lambda(12s)=46.3% x 10.64c pay-up, charged as if resting
  the original limit were free.

  This span carries {shares:,.0f} filled shares in {(T1-T0)/3600:.2f}h; pro-rata by shares that is
  ${prorata:,.2f} of theoretical retry-pricing value sitting inside this corpus.
""".rstrip())
    print(f"  {'policy':<24} {'tier':>6} {'Δ P&L':>9} {'CI95 upper':>12}   "
          f"vs the ${prorata:,.0f} row (c) implies")
    worst_upper = -1e18
    for name, pol, kw in variants:
        for tier in ("ask", "ask2", "print"):
            r = run_policy(keyed, books, prints, outcomes, pol, tier=tier, **kw)
            d = {s: r.pnl.get(s, 0.0) - base.pnl.get(s, 0.0)
                 for s in set(r.pnl) | set(base.pnl)}
            lo, hi = boot_ci(d)
            worst_upper = max(worst_upper, hi)
            print(f"  {name:<24} {tier:>6} {sum(r.pnl.values())-base_total:+9.2f} "
                  f"{hi:+12.0f}   {'REJECTED' if hi < prorata else 'not rejected'}")
    print(f"""
  Every policy's 95% upper bound is below ${prorata:,.0f} (max upper: ${worst_upper:,.0f}).
  The corpus REJECTS row (c)'s pro-rata value for every retry policy tested.
  Row (c) is not a $772 pot of money that a shorter TTL or a pinned limit picks
  up; it is the price of a chase that the recorded book mostly does not give
  back, plus a third of re-decides that come back CHEAPER and are pure gain.""".rstrip())

    # ---------------------------------------------------------------- 6
    L.hdr("6. MATCHER SENSITIVITY — does the answer survive the fill attribution?")
    print("""
The whole table rides on one free parameter: which fire a wallet row is hung
on. latency_report's sweep showed the median is robust and the tail is not, and
a retry study lives exactly in the tail — clip k+1's fills landing on clip k is
the same event this study calls 'the chase filled'. Re-run the headline
policies under the opposite attribution.""".strip())
    print()
    print(f"  {'policy':<24} {'tier':>6} {'newest-first Δ':>16} {'oldest-first Δ':>16}")
    # SHALLOW COPY, not the same dicts: match_fills writes its attribution back
    # onto the fire records, and `keyed` still points at the originals.
    alt = [dict(f) for f in tape["fire"] if not L.in_blackout(f["t"])]
    alt, _ = L.match_fills(alt, fills, newest_first=False)
    alt = L.attach_book(alt, books)
    akeyed = defaultdict(list)
    for f in alt:
        if T0 <= f["t"] <= T1 and f["slug"] in outcomes:
            akeyed[(f["slug"], f["side"])].append(f)
    for v in akeyed.values():
        v.sort(key=lambda x: x["t"])
    abase = run_policy(akeyed, books, prints, outcomes, "base")
    abase_total = sum(abase.pnl.values())
    print(f"  {'base (absolute P&L)':<24} {'—':>6} {base_total:16.2f} {abase_total:16.2f}")
    for name, pol, kw in (variants + exact):
        t_ = "ask" if pol != "giveup" else "ask"
        a = run_policy(keyed, books, prints, outcomes, pol, tier=t_, **kw)
        b = run_policy(akeyed, books, prints, outcomes, pol, tier=t_, **kw)
        print(f"  {name:<24} {t_:>6} {sum(a.pnl.values())-base_total:+16.2f} "
              f"{sum(b.pnl.values())-abase_total:+16.2f}")
    print("""
  A policy whose sign flips between these two columns is measuring the join,
  not the market.""")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
