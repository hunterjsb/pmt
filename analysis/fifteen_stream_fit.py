#!/usr/bin/env python3
"""15m reopen fit on the RTDS settlement stream — the parked-arm re-add gate.

15m is PARKED: the range-avg momentum proxy lost -$654/night on it, and the
hybrid settle rule that skipped every one of those catastrophes banked almost
nothing (2 fires / $100 over 47 windows — analysis/hybrid_ab.md). The reason
hybrid is volumeless is mechanical, not statistical: on the minute-grain
Binance feed the forming 60s settlement TWAP is ONE sample, so the lock the
hybrid brake prices is invisible until the wire. The RTDS recorder captures
that stream at 1Hz, which is the first tape on which the lock is measurable.

This extends analysis/xrp_fit.py's decidedness method from 5m to 15m and adds
the quantity the hybrid brake actually prices:

  §1  decidedness — P(final winner == sign of live stream margin) at elapsed
      {40,60,80}% for |margin| >= {6,10,15}bp, per symbol, 15m windows.
  §2  control — the same at 5m on the SAME tape (5m is the proved arm; a 15m
      number only means something next to it).
  §3  terminal lock — inside the last 60s (the forming sixty-TWAP),
      P(final winner == locked side) by locked_frac bucket, measured two ways:
        spot  — sign(spot/ref-1), what updown_model::terminal_lock uses today
        ptwap — sign(mean(chainlink over [end-60, now])/ref-1), the partial
                settlement TWAP itself, which only a sub-minute feed can form
  §4  bankability — how often the hybrid chain (range-avg guard -> terminal
      banked_decided -> theta safety gate) would open at all before the wire,
      and whether the side it opens on wins. This is the volume question that
      parked 15m; §1-§3 are the accuracy question.

Stream quantities only — no Binance, no gamma resolutions. Settlement is the
stream's own terminal rule: sixty-TWAP at range end vs sixty-TWAP at range
start (thirty-TWAP for 5m; updown_model::settle_tw_secs owns the width rule).
Reference is keyed exactly as updown_rtds does it: per_min[start-60] is the
mark printed AT `start`.

Usage:  cd pmtrader && uv run python ../analysis/fifteen_stream_fit.py
        [--corpus PATH] [--syms btc,eth,sol,xrp]
"""
import argparse
import datetime
import json
import math
import os
from bisect import bisect_right
from collections import defaultdict

DEFAULT_CORPUS = os.path.expanduser("~/.pmt/corpus/rtds/rtds-20260823.jsonl")

TOPIC_SPOT = "crypto_prices_chainlink"
TOPIC_TWAP30 = "crypto_prices_twap_thirty"
TOPIC_TWAP60 = "crypto_prices_twap_sixty"

# Engine constants, mirrored so the fit prices what the arm would price.
MAX_SPOT_AGE_S = 5.0      # updown_model
MARK_TOL_S = 2            # updown_rtds
VOL_FAST_WINDOW = 12
SIGMA_SLOW_WINDOW = 45
SIGMA_SLOW_MIN = 30
MANIP_PUSH_BP = 25.0      # arms-state default
THETA = 0.3               # live policy

# Per-symbol static guards: the live fleet's (btc/eth 6, sol 10, bnb 8) plus
# xrp's proposed 12 (analysis/xrp_fit.md).
GUARD_BP = {"btc/usd": 6.0, "eth/usd": 6.0, "sol/usd": 10.0,
            "xrp/usd": 12.0, "bnb/usd": 8.0, "doge/usd": 20.0}

MARGIN_THRESHOLDS = (6.0, 10.0, 15.0)
ELAPSED_FRACS = (0.4, 0.6, 0.8)
LOCK_BUCKETS = ((0.0, 0.25), (0.25, 0.5), (0.5, 0.75), (0.75, 1.0))


def utc(t):
    return datetime.datetime.fromtimestamp(t, datetime.timezone.utc).strftime("%H:%M:%SZ")


def norm_cdf(x):
    return 0.5 * math.erfc(-x / math.sqrt(2.0))


# --- corpus ------------------------------------------------------------

def load(path, syms):
    """Stream quantities per symbol: spot series, minute closes, TWAP marks.

    `twap_at[T]` is the mark PRINTED AT wall time T — which is what
    updown_rtds stores as per_min[T-60], and therefore literally the
    settlement print at a window boundary of T.
    """
    spot = {s: ([], []) for s in syms}
    twap30 = {s: {} for s in syms}
    twap60 = {s: {} for s in syms}
    needles = tuple(f'"symbol": "{s}"' for s in syms)
    lines = kept = 0
    for raw in open(path):
        lines += 1
        if not any(n in raw for n in needles):
            continue
        try:
            r = json.loads(raw)
        except ValueError:
            continue
        sym, topic = r.get("symbol"), r.get("topic")
        if sym not in spot:
            continue
        ts = r["ts"] / 1000.0
        kept += 1
        if topic == TOPIC_SPOT:
            spot[sym][0].append(ts)
            spot[sym][1].append(r["value"])
        elif topic in (TOPIC_TWAP30, TOPIC_TWAP60):
            mark = int(ts // 60) * 60
            if ts - mark > MARK_TOL_S:
                continue  # a mid-minute print is not a boundary mark
            book = twap30 if topic == TOPIC_TWAP30 else twap60
            book[sym].setdefault(mark, r["value"])

    for s in syms:                                     # recorder order is not wire order
        ts, vs = spot[s]
        order = sorted(range(len(ts)), key=ts.__getitem__)
        spot[s] = ([ts[i] for i in order], [vs[i] for i in order])
    return spot, twap30, twap60, lines, kept


def minute_closes(spot_series):
    """One chainlink sample per minute, oldest first — the FIRST print of each
    new minute, exactly as updown_rtds::route_sample banks it."""
    ts, vs = spot_series
    out, last_min = [], -1
    for t, v in zip(ts, vs):
        m = int(t // 60) * 60
        if m > last_min:
            last_min = m
            out.append((m, v))
    return out


def gaps(spot_series, thresh=5.0):
    ts, _ = spot_series
    return [(a, b - a) for a, b in zip(ts, ts[1:]) if b - a > thresh]


def spot_at(series, t, tol=MAX_SPOT_AGE_S):
    ts, vs = series
    i = bisect_right(ts, t) - 1
    if i < 0 or t - ts[i] > tol:
        return None
    return vs[i]


def mean_spot_over(series, lo, hi):
    """Partial settlement TWAP: the mean chainlink print over [lo, hi]."""
    ts, vs = series
    i, j = bisect_right(ts, lo - 1e-9), bisect_right(ts, hi)
    if j <= i:
        return None
    return sum(vs[i:j]) / (j - i)


# --- engine arithmetic, mirrored ---------------------------------------

def trailing_sigma_bp(vals, window):
    n = min(len(vals), window + 1)
    if n < 4:
        return 0.0
    seg = vals[len(vals) - n:]
    rets = [math.log(b / a) for a, b in zip(seg, seg[1:])]
    mu = sum(rets) / len(rets)
    var = sum((r - mu) ** 2 for r in rets) / (len(rets) - 1)
    return math.sqrt(var) * 1e4


class Sigma:
    """sig_bp as eval_model computes it, at any wall time, off the closes."""

    def __init__(self, closes):
        self.mins = [m for m, _ in closes]
        self.vals = [v for _, v in closes]

    def at(self, t):
        k = bisect_right(self.mins, t)
        if k < 4:
            return None
        vals = self.vals[:k]
        fast = trailing_sigma_bp(vals, VOL_FAST_WINDOW)
        slow = trailing_sigma_bp(vals, SIGMA_SLOW_WINDOW)
        base = slow if (len(vals) >= SIGMA_SLOW_MIN and slow > 0.0) else 0.0
        return max(base, fast) or None


def terminal_lock(rem, tw, margin_bp, sig_frac, guard_bp):
    """updown_model::terminal_lock, verbatim."""
    if rem > tw:
        h = max((rem - tw / 2.0) / 60.0, 0.02)
        return 0.0, guard_bp + sig_frac * math.sqrt(h) * 1e4, h
    locked_frac = min(max((tw - rem) / tw, 0.0), 1.0)
    h = max(rem / 60.0, 0.02)
    banked = margin_bp * locked_frac
    cushion = guard_bp + sig_frac * math.sqrt(h / 3.0) * 1e4 * (1.0 - locked_frac)
    return banked, cushion, h


def eval_range_avg(per_min, start, end, now, spot, ref, sig_frac, guard_bp):
    """updown_model::eval_range_avg on stream marks. `per_min` is keyed the
    engine's way — per_min[k] is the mark PRINTED AT k+60 — because the
    banked-mass filter reads those keys directly. `gated` is the guard the
    live arm would have returned as a GateReason."""
    banked = [v for k, v in per_min.items() if start <= k < min(now - 30.0, end)]
    banked_s = len(banked) * 60.0
    banked_avg = (sum(banked) / len(banked)) if banked else spot
    rem = max(end - now, 0.0)
    window = banked_s + rem
    if window <= 0.0 or rem <= 0.0:
        m = (banked_avg / ref - 1.0) * 1e4
        return dict(p_up=1.0 if banked_avg >= ref else 0.0, margin_bp=m,
                    banked_margin_bp=m, cushion_bp=guard_bp, gated=False)
    proj = (banked_avg * banked_s + spot * rem) / window
    margin_bp = (proj / ref - 1.0) * 1e4
    banked_margin_bp = (banked_avg / ref - 1.0) * 1e4 * (banked_s / window)
    cushion_bp = guard_bp + sig_frac * 1e4 * math.sqrt(max(rem / 60.0, 0.02) / 3.0) * (rem / window)
    gated = abs(margin_bp) < guard_bp
    breakeven = (ref * window - banked_avg * banked_s) / rem
    sig_avg = sig_frac * math.sqrt(max(rem / 60.0, 0.02) / 3.0)
    p_up = 1.0 if breakeven <= 0.0 else 1.0 - norm_cdf(math.log(breakeven / spot) / sig_avg)
    return dict(p_up=p_up, margin_bp=margin_bp, banked_margin_bp=banked_margin_bp,
                cushion_bp=cushion_bp, gated=gated)


# --- windows -----------------------------------------------------------

def windows(twap_at, dur):
    """Every fully-covered window of `dur` seconds: (start, ref, settle, winner).

    ref and settle are the settlement-width TWAP marks printed AT the window
    bounds — the terminal rule, verified against 3,193 resolutions
    (updown_model::eval_terminal docstring)."""
    if not twap_at:
        return []
    t0, t1 = min(twap_at), max(twap_at)
    out = []
    start = (t0 // dur) * dur
    while start + dur <= t1:
        ref, settle = twap_at.get(start), twap_at.get(start + dur)
        if ref and settle:
            out.append((start, ref, settle, "up" if settle >= ref else "down"))
        start += dur
    return out


# --- studies -----------------------------------------------------------

def decidedness(ws, spot_series, dur):
    """P(final winner == sign of live stream margin) at each elapsed frac,
    conditioned on |margin| >= threshold."""
    cells = {}
    for frac in ELAPSED_FRACS:
        for m_th in MARGIN_THRESHOLDS:
            n = right = 0
            for start, ref, _settle, winner in ws:
                sp = spot_at(spot_series, start + dur * frac)
                if sp is None:
                    continue
                m = (sp / ref - 1.0) * 1e4
                if abs(m) >= m_th:
                    n += 1
                    right += (m > 0) == (winner == "up")
            cells[(frac, m_th)] = (right, n)
    return cells


def terminal_lock_view(ws, spot_series, tw, dur, max_margin_bp=None):
    """Inside the forming settlement TWAP: P(final winner == locked side),
    bucketed by locked_frac, for both lock estimators.

    `max_margin_bp` restricts to CONTESTED windows — the ones whose settlement
    margin is small enough that the lock estimator is what decides the read.
    On a wide-margin window every estimator is right and the average hides
    the only cases the brake exists for."""
    acc = {("spot", b): [0, 0] for b in LOCK_BUCKETS}
    acc.update({("ptwap", b): [0, 0] for b in LOCK_BUCKETS})
    nwin = 0
    for start, ref, settle, winner in ws:
        if max_margin_bp is not None and abs(settle / ref - 1) * 1e4 > max_margin_bp:
            continue
        nwin += 1
        end = start + dur
        for rem in range(int(tw) - 1, 0, -1):
            now = end - rem
            locked_frac = (tw - rem) / tw
            bucket = next((b for b in LOCK_BUCKETS if b[0] < locked_frac <= b[1]), None)
            if bucket is None:
                continue
            sp = spot_at(spot_series, now)
            if sp is not None:
                a = acc[("spot", bucket)]
                a[1] += 1
                a[0] += ((sp / ref - 1.0) > 0) == (winner == "up")
            pt = mean_spot_over(spot_series, end - tw, now)
            if pt is not None:
                a = acc[("ptwap", bucket)]
                a[1] += 1
                a[0] += ((pt / ref - 1.0) > 0) == (winner == "up")
    return acc, nwin


def bankability(ws, spot_series, per_min, sigma, tw, dur, guard_bp):
    """Would the hybrid chain ever open before the wire, and does it win?

    Chain, in the order updown.rs applies it:
      1. eval_range_avg's guard on the PROJECTED margin (hybrid inherits the
         range-avg evidence half wholesale, gate included),
      2. terminal banked_decided: |margin*locked_frac| > residual cushion AND
         the lock agrees with the momentum proxy's side,
      3. the R9 theta gate on the first clip: safety = banked_margin_bp (range)
         / cushion_bp (terminal) >= theta on the fired side.
    Book depth, ask price and edge are NOT modelled here — this is a ceiling
    on model-side opportunity, never a fill count.
    """
    res = dict(win=len(ws), guard_open=0, banked=0, entry=0, banked_right=0,
               entry_right=0, first_rem=[], ra_banked=0, ra_banked_right=0)
    for start, ref, _settle, winner in ws:
        end = start + dur
        guard_open = ra_banked_side = None
        banked_side = entry_side = None
        first_rem = None
        # 5s steps until the settlement window opens, 1s inside it — the lock
        # only moves at 1Hz there and that is the whole point of the tape.
        t = start + 60.0
        while t < end:
            rem = end - t
            step = 1.0 if rem <= tw + 5 else 5.0
            sp = spot_at(spot_series, t)
            sig_bp = sigma.at(t)
            if sp is None or sig_bp is None:
                t += step
                continue
            sig_frac = sig_bp / 1e4
            r = eval_range_avg(per_min, start, end, t, sp, ref, sig_frac, guard_bp)
            if not r["gated"] and guard_open is None:
                guard_open = True
            term_margin_bp = (sp / ref - 1.0) * 1e4
            t_banked, t_cushion, _ = terminal_lock(rem, tw, term_margin_bp, sig_frac, guard_bp)
            dec = abs(t_banked) > t_cushion and (t_banked > 0.0) == (r["p_up"] > 0.5)
            if dec and banked_side is None:
                banked_side = "up" if t_banked > 0 else "down"
                first_rem = rem
            # range_avg's own banked_decided — the 13.2% false-decided rate the
            # hybrid spec exists to kill (updown_model::eval_hybrid docstring).
            if (abs(r["banked_margin_bp"]) > r["cushion_bp"]
                    and (r["banked_margin_bp"] > 0.0) == (r["p_up"] > 0.5)
                    and ra_banked_side is None):
                ra_banked_side = "up" if r["banked_margin_bp"] > 0 else "down"
            if dec and not r["gated"] and entry_side is None:
                is_up = r["p_up"] > 0.5
                signed = r["banked_margin_bp"] if is_up else -r["banked_margin_bp"]
                if signed / max(t_cushion, 1e-9) >= THETA:
                    entry_side = "up" if is_up else "down"
            t += step
        res["guard_open"] += bool(guard_open)
        if banked_side:
            res["banked"] += 1
            res["banked_right"] += banked_side == winner
            res["first_rem"].append(first_rem)
        if entry_side:
            res["entry"] += 1
            res["entry_right"] += entry_side == winner
        if ra_banked_side:
            res["ra_banked"] += 1
            res["ra_banked_right"] += ra_banked_side == winner
    return res


# --- report ------------------------------------------------------------

def pct(right, n):
    return f"{right / n * 100:3.0f}% (n={n:>3})" if n else "   — (n=  0)"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--corpus", default=DEFAULT_CORPUS)
    ap.add_argument("--syms", default="btc,eth,sol,xrp")
    a = ap.parse_args()
    syms = [f"{s}/usd" for s in a.syms.split(",")]

    spot, twap30, twap60, lines, kept = load(a.corpus, syms)
    t0 = min(min(s[0]) for s in spot.values() if s[0])
    t1 = max(max(s[0]) for s in spot.values() if s[0])
    print(f"corpus  {a.corpus}")
    print(f"        {lines:,} lines, {kept:,} on {len(syms)} symbols; "
          f"{utc(t0)} -> {utc(t1)} = {(t1 - t0) / 3600:.2f}h")
    for s in syms:
        g = gaps(spot[s])
        if g:
            worst = sorted(g, key=lambda x: -x[1])[:3]
            print(f"        gap {s:9s} {len(g):>2} x >5s, {sum(d for _, d in g):5.0f}s total "
                  f"({sum(d for _, d in g) / (t1 - t0) * 100:.1f}% of span); worst: "
                  + ", ".join(f"{d:.0f}s @{utc(a)}" for a, d in worst))

    sigma = {s: Sigma(minute_closes(spot[s])) for s in syms}
    w15 = {s: windows(twap60[s], 900) for s in syms}
    w5 = {s: windows(twap30[s], 300) for s in syms}
    # Engine keying: per_min[k] is the mark printed AT k+60.
    pm60 = {s: {t - 60: v for t, v in twap60[s].items()} for s in syms}
    pm30 = {s: {t - 60: v for t, v in twap30[s].items()} for s in syms}
    for s in syms:
        for dur, ws in ((900, w15[s]), (300, w5[s])):
            possible = int((t1 - t0) // dur)
            print(f"        {s:9s} {dur // 60:>2}m windows: {len(ws):>3} formed of ~{possible} "
                  f"in span ({possible - len(ws)} lost to a missing boundary mark)")

    for tag, ws_by_sym, dur, tw in (("15m  (sixty-TWAP settle)", w15, 900, 60),
                                    ("5m CONTROL (thirty-TWAP settle)", w5, 300, 30)):
        print(f"\n{'=' * 96}\n§ decidedness — {tag}\n{'=' * 96}")
        print(f"{'sym':>9} {'win':>4} {'|settle m|bp p50/p90':>21} {'sig1m':>6} {'elapsed':>8}  "
              f"{'>=6bp':>13} {'>=10bp':>13} {'>=15bp':>13}")
        for s in syms:
            ws = ws_by_sym[s]
            if not ws:
                continue
            margins = sorted(abs(st / rf - 1) * 1e4 for _, rf, st, _ in ws)
            p50 = margins[len(margins) // 2]
            p90 = margins[min(int(len(margins) * 0.9), len(margins) - 1)]
            sigs = sorted(x for x in (sigma[s].at(st) for st, *_ in ws) if x)
            sig = sigs[len(sigs) // 2] if sigs else 0.0   # median over the run
            cells = decidedness(ws, spot[s], dur)
            for i, frac in enumerate(ELAPSED_FRACS):
                head = (f"{s:>9} {len(ws):>4} {p50:>9.1f}/{p90:<11.1f} {sig:>6.1f}"
                        if i == 0 else f"{'':>9} {'':>4} {'':>21} {'':>6}")
                row = "  ".join(pct(*cells[(frac, m)]) for m in MARGIN_THRESHOLDS)
                print(f"{head} {int(frac * 100):>7}%  {row}")

    print(f"\n{'=' * 96}\n§ terminal lock — P(final winner == locked side) inside the forming "
          f"settlement TWAP\n{'=' * 96}")
    print("  locked_frac = (tw - rem)/tw, the share of the settlement TWAP already formed.")
    print("  spot  = sign(spot/ref-1)                     <- what terminal_lock() banks today")
    print("  ptwap = sign(mean(chainlink over [end-tw,now])/ref-1)  <- the partial TWAP itself")
    print("  n counts 1Hz TICKS; ticks inside one window are ~perfectly correlated, so the")
    print("  effective sample is the WINDOW count in the row label, not n.")
    for tag, ws_by_sym, dur, tw in (("15m", w15, 900, 60), ("5m", w5, 300, 30)):
        for cut, cutlab in ((None, "all windows"), (15.0, "CONTESTED: |settle margin| <= 15bp")):
            print(f"\n  -- {tag} (tw={tw}s) — {cutlab} --")
            print(f"{'sym':>9} {'win':>4} {'est':>6}  " + "  ".join(
                f"{f'{lo:.2f}-{hi:.2f}':>15}" for lo, hi in LOCK_BUCKETS))
            for s in syms:
                ws = ws_by_sym[s]
                if not ws:
                    continue
                acc, nwin = terminal_lock_view(ws, spot[s], tw, dur, cut)
                for est in ("spot", "ptwap"):
                    cells = "  ".join(f"{pct(*acc[(est, b)]):>15}" for b in LOCK_BUCKETS)
                    print(f"{s if est == 'spot' else '':>9} {nwin if est == 'spot' else '':>4} "
                          f"{est:>6}  {cells}")

    print(f"\n{'=' * 96}\n§ bankability — would the hybrid chain open before the wire?\n{'=' * 96}")
    print("  guard    windows where the range-avg PROJECTED margin ever clears the static guard")
    print("  banked   windows where terminal banked_decided ever fires (the brake waiver)")
    print("  entry    windows where guard AND banked AND the theta=0.3 safety gate clear on ONE")
    print("           tick — the first clip's full model-side chain")
    print("  ra-bank  range_avg's OWN banked_decided, for contrast (the parked model's waiver)")
    print("  CEILING, not a fill count: no book, no ask, no min_edge/min_fair/max_price, no")
    print("  quiesce, no clip cooldown, no early_frac. Depth is what actually binds at 15m.")
    print(f"\n{'set':>4} {'sym':>9} {'guard':>7} {'win':>5} {'guard':>9} {'banked':>9} "
          f"{'P(win|bk)':>11} {'entry':>9} {'P(win|en)':>11} {'med rem@bk':>11} "
          f"{'ra-bank':>9} {'P(win|ra)':>11}")
    for tag, ws_by_sym, dur, tw in (("15m", w15, 900, 60), ("5m", w5, 300, 30)):
        for s in syms:
            ws = ws_by_sym[s]
            if not ws:
                continue
            g = GUARD_BP.get(s, 10.0)
            b = bankability(ws, spot[s], pm60[s] if tw == 60 else pm30[s],
                            sigma[s], tw, dur, g)
            rems = sorted(b["first_rem"])
            med = f"{rems[len(rems) // 2]:.0f}s" if rems else "—"
            print(f"{tag:>4} {s:>9} {g:>6.0f}bp {b['win']:>5} {b['guard_open']:>9} "
                  f"{b['banked']:>9} {pct(b['banked_right'], b['banked']):>11} "
                  f"{b['entry']:>9} {pct(b['entry_right'], b['entry']):>11} {med:>11} "
                  f"{b['ra_banked']:>9} {pct(b['ra_banked_right'], b['ra_banked']):>11}")


if __name__ == "__main__":
    main()
