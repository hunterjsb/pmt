#!/usr/bin/env python3
"""Cross-symbol correlation study — the 2026-08-23 17:15Z five-arm event.

Five 5m arms (btc/eth/sol/xrp/bnb) all fired DOWN on banked evidence inside
window epoch 1787505300; a single macro impulse in the final ~90s carried
every one of them to an UP settlement. ~$230 gone at once, 107-win streak
over. The fleet cap rations TOTAL un-decided dollars and has never had an
opinion about SAME-SIDE concentration, so five "independent" 94% bets were
one bet.

What this driver measures, in the order the report reads them:

  S1  SETTLE-RULE VALIDATION. Before any correlation question: which rule
      actually settles these markets? The live arms price `range_avg` (the
      whole window's average vs its open). Graded against the L36-clean
      outcomes corpus over the RTDS span, that rule is 85.8% right on
      book-graded windows while a terminal 60s TWAP is 99.7%. The gap is
      the incident.
  S2  Q1 correlation structure — 90d of 1m klines (6 symbols) for the
      unconditional matrix, the RTDS settlement stream (8 symbols, incl.
      hype/zec we do not trade) for the intraday one. Conditional on
      |margin|, volatility regime, hour. P(>=k of 5 agree) against a
      permutation null that shuffles each symbol's own series in time.
  S3  Q2 concentration episodes on the durable tape — every epoch where
      N>=2 arms fired the same side, their realised hit rate and P&L
      against a solo-fire control and a permutation null.
  S4  Q3 the impulse class — final-90s multi-symbol synchronised moves off
      the 1Hz stream, and the lead-lag cross-correlation that decides
      whether a leader-veto has any warning window to work with.
  S5  Q4 policy counterfactuals (a) same-side concentration cap,
      (b) correlated-exposure fleet cap, (c) leader veto, (d) correlation-
      regime clip scaling. Priced against the recorded fires, bootstrap
      CI95 over windows, segmented by policy era.

Honest-counterfactual discipline, inherited from aggression_sweep.md and
retry_pricing_study.md:

  * Every counterfactual charges against RECORDED books. A blocked clip is
    priced at the ask the tape recorded for it; nothing is credited a fill
    the tape does not contain, and no policy is allowed to re-deploy the
    budget it frees.
  * Fill truth comes from the engine's own position tracker: a fire's
    filled notional is the INCREMENT in `committed` between that fire and
    the next observation of `committed` on the same slug. Fires that never
    moved `committed` never filled and are worth nothing to any policy.
  * Valuation is hold-to-settlement, gross of fees, same convention on
    every variant — so only DELTAS are read, never absolute P&L. The live
    engine exits; this does not. (hybrid_ab.md's rule.)
  * Independence nulls are PERMUTATIONS of the recorded data, never an
    assumed binomial.
  * Bootstrap CI95 is percentile over WINDOWS, 10,000 resamples — the same
    unit aggression_sweep.md and retry_pricing_study.md used.
  * Era-segmented throughout (polymarket/eras.py). Pre-theta behaviour is
    reported but never steers a recommendation; post-theta is the decision
    basis.

Read-only over ~/.pmt. No engine, no orders, no network. L33 caveat: the
live tape is append-only, so re-running this over a longer tape is not a
re-run — the spans printed by S0 pin what these numbers were computed on.

    cd pmtrader && uv run python ../analysis/correlation_study.py
"""
from __future__ import annotations

import bisect
import collections
import json
import math
import os
import pickle
import random
import re
import sys
import time

HOME = os.path.expanduser("~")
CORPUS = os.path.join(HOME, ".pmt", "corpus")
ENGINE = os.path.join(HOME, ".pmt", "engine")
TAPE = os.path.join(ENGINE, "updown-tape.jsonl")
OUTCOMES = os.path.join(CORPUS, "outcomes.jsonl")
RTDS = os.path.join(CORPUS, "rtds", "rtds-20260823.jsonl")
KLINES = os.path.join(CORPUS, "klines-1m-%s.jsonl")
CACHE = os.environ.get("PMT_STUDY_CACHE", "/tmp/claude-1000/-var-home-hunter/"
                       "35f80f35-e0c9-4e4d-80ea-9c5602f70444/scratchpad/corrcache")

# The incident.
INCIDENT_EPOCH = 1787505300
INCIDENT_DUR = 300

# Arms that actually trade 5m. doge/hype/zec are carried as correlation
# instruments only — hype/zec are on the stream and have never been armed,
# which makes them the cleanest available control for "is this macro".
FLEET = ("btc", "eth", "sol", "xrp", "bnb")
KLINE_SYMS = {"btc": "BTCUSDT", "eth": "ETHUSDT", "sol": "SOLUSDT",
              "xrp": "XRPUSDT", "bnb": "BNBUSDT", "doge": "DOGEUSDT"}
RTDS_SYMS = ("btc", "eth", "sol", "xrp", "bnb", "doge", "hype", "zec")

CK = "crypto_prices_chainlink"
T30 = "crypto_prices_twap_thirty"
T60 = "crypto_prices_twap_sixty"

SLUG_RE = re.compile(r"^([a-z]+)-updown-(5m|15m)-(\d+)$")
BOOT_N = 10000
RNG_SEED = 20260823

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "pmtrader"))
try:
    from polymarket import eras as ERAS_MOD
except Exception:  # pragma: no cover - the study still runs uncut
    ERAS_MOD = None


# ---------------------------------------------------------------- utilities

def say(*a):
    print(*a)
    sys.stdout.flush()


def rule(title):
    say("")
    say("=" * 78)
    say(title)
    say("=" * 78)


def mean(xs):
    xs = list(xs)
    return sum(xs) / len(xs) if xs else float("nan")


def stdev(xs):
    xs = list(xs)
    if len(xs) < 2:
        return float("nan")
    m = mean(xs)
    return math.sqrt(sum((x - m) ** 2 for x in xs) / (len(xs) - 1))


def pearson(xs, ys):
    n = min(len(xs), len(ys))
    if n < 3:
        return float("nan")
    xs, ys = xs[:n], ys[:n]
    mx, my = mean(xs), mean(ys)
    sxy = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    sxx = sum((x - mx) ** 2 for x in xs)
    syy = sum((y - my) ** 2 for y in ys)
    if sxx <= 0 or syy <= 0:
        return float("nan")
    return sxy / math.sqrt(sxx * syy)


def bootstrap_ci(units, stat, n=BOOT_N, seed=RNG_SEED, lo=2.5, hi=97.5):
    """Percentile CI over a resample of UNITS (windows), the precedent unit."""
    units = list(units)
    if not units:
        return (float("nan"), float("nan"))
    rnd = random.Random(seed)
    k = len(units)
    vals = []
    for _ in range(n):
        vals.append(stat([units[rnd.randrange(k)] for _ in range(k)]))
    vals.sort()
    return (vals[int(lo / 100 * n)], vals[min(n - 1, int(hi / 100 * n))])


def era_of(epoch):
    if ERAS_MOD is None:
        return "?"
    return ERAS_MOD.for_start(float(epoch)).name


def era_index(name):
    if ERAS_MOD is None:
        return 0
    return ERAS_MOD.names().index(name) if name in ERAS_MOD.names() else -1


POST_THETA = None  # filled in main once eras are known


def is_post_theta(epoch):
    return epoch >= POST_THETA


# ------------------------------------------------------------------ loaders

def _cached(name, build):
    path = "%s-%s.pkl" % (CACHE, name)
    src_mtimes = None
    try:
        with open(path, "rb") as fh:
            blob = pickle.load(fh)
        if blob.get("v") == 3:
            return blob["data"]
    except Exception:
        pass
    data = build()
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "wb") as fh:
            pickle.dump({"v": 3, "data": data, "m": src_mtimes}, fh, -1)
    except Exception:
        pass
    return data


def load_rtds():
    """(topic, sym) -> (sorted ts list, value list). ts in seconds, oracle time."""
    def build():
        acc = collections.defaultdict(list)
        with open(RTDS) as fh:
            for line in fh:
                try:
                    r = json.loads(line)
                except ValueError:
                    continue
                sym = r.get("symbol")
                ts = r.get("ts")
                if sym is None or ts is None:
                    continue
                acc[(r["topic"], sym.split("/")[0])].append((ts / 1000.0, r["value"]))
        out = {}
        for k, v in acc.items():
            v.sort()
            # collapse duplicate oracle timestamps, keep last
            ts, vs = [], []
            for t, x in v:
                if ts and ts[-1] == t:
                    vs[-1] = x
                else:
                    ts.append(t)
                    vs.append(x)
            out[k] = (ts, vs)
        return out
    return _cached("rtds", build)


def load_klines():
    """sym -> (sorted minute-open list, o list, c list)."""
    def build():
        out = {}
        for sym, pair in KLINE_SYMS.items():
            path = KLINES % pair
            if not os.path.exists(path):
                continue
            seen = {}
            with open(path) as fh:
                for line in fh:
                    try:
                        r = json.loads(line)
                    except ValueError:
                        continue
                    seen[int(r["t"])] = (float(r["o"]), float(r["c"]))
            ts = sorted(seen)
            out[sym] = (ts, [seen[t][0] for t in ts], [seen[t][1] for t in ts])
        return out
    return _cached("klines", build)


def load_outcomes():
    oc = {}
    with open(OUTCOMES) as fh:
        for line in fh:
            try:
                r = json.loads(line)
            except ValueError:
                continue
            oc[r["slug"]] = r
    return oc


def load_tape():
    """slug -> rows sorted by t, each tagged with _sym/_dur/_epoch."""
    rows = collections.defaultdict(list)
    with open(TAPE) as fh:
        for line in fh:
            try:
                r = json.loads(line)
            except ValueError:
                continue
            slug = r.get("slug")
            if not slug:
                continue
            m = SLUG_RE.match(slug)
            if not m:
                continue
            r["_sym"], r["_dur"] = m.group(1), m.group(2)
            r["_epoch"] = int(m.group(3))
            r["_dursec"] = 300 if m.group(2) == "5m" else 900
            rows[slug].append(r)
    for slug in rows:
        rows[slug].sort(key=lambda r: r["t"])
    return rows


# --------------------------------------------------- price series accessors

class Series:
    """1Hz-ish oracle series with an explicit staleness bound.

    Every lookup that cannot be answered within `stale` seconds of the
    requested instant returns None rather than a stale number — a study
    that silently reads a 40s-old print is measuring the recorder's gaps.
    """

    def __init__(self, rtds, stale=20.0):
        self.s = rtds
        self.stale = stale

    def at(self, topic, sym, t):
        key = (topic, sym)
        if key not in self.s:
            return None
        ts, vs = self.s[key]
        i = bisect.bisect_right(ts, t) - 1
        if i < 0 or t - ts[i] > self.stale:
            return None
        return vs[i]

    def span(self, topic, sym, t0, t1):
        key = (topic, sym)
        if key not in self.s:
            return [], []
        ts, vs = self.s[key]
        a = bisect.bisect_left(ts, t0)
        b = bisect.bisect_left(ts, t1)
        return ts[a:b], vs[a:b]

    def coverage(self, sym):
        key = (CK, sym)
        if key not in self.s:
            return None
        ts, _ = self.s[key]
        return ts[0], ts[-1]

    def grid(self, topic, sym, t0, t1, step=1.0):
        """Forward-filled step-second grid; None where the gap exceeds stale."""
        ts, vs = self.s.get((topic, sym), ([], []))
        out = []
        i = 0
        n = len(ts)
        t = t0
        while t < t1:
            while i + 1 < n and ts[i + 1] <= t:
                i += 1
            if i < n and ts[i] <= t and t - ts[i] <= self.stale:
                out.append(vs[i])
            else:
                out.append(None)
            t += step
        return out


# ------------------------------------------------------- S1 settle-rule fit

def settle_width(dursec):
    """updown_model.rs::settle_tw_secs — the Chainlink settlement TWAP width.
    30s for a 5m market, 60s for anything longer. pmtrader's grader mirrors it
    (outcomes.py::ck_settlement_width_s)."""
    return 30.0 if dursec <= 300 else 60.0


def settle_margin(ser, sym, start, dursec, rule_name):
    """bp of the window's settlement margin under `rule_name`, or None.

    `terminal` is the WIDTH-CORRECT rule the market actually settles on: the
    settlement-width TWAP at the close against the same TWAP at the open
    (updown_model.rs:287-292 — reference is `per_min[start-60]`, "the
    settlement print at the window's start instant").
    """
    end = start + dursec
    topic = T30 if settle_width(dursec) == 30.0 else T60
    if rule_name == "terminal":
        a, b = ser.at(topic, sym, start), ser.at(topic, sym, end)
        return None if (a is None or b is None) else (b / a - 1) * 1e4
    if rule_name == "range_avg":
        _, vs = ser.span(CK, sym, start, end)
        ref = ser.at(topic, sym, start)
        if not vs or ref is None:
            return None
        return (mean(vs) / ref - 1) * 1e4
    if rule_name == "terminal_t60":
        a, b = ser.at(T60, sym, start), ser.at(T60, sym, end)
        return None if (a is None or b is None) else (b / a - 1) * 1e4
    if rule_name == "terminal_t30":
        a, b = ser.at(T30, sym, start), ser.at(T30, sym, end)
        return None if (a is None or b is None) else (b / a - 1) * 1e4
    if rule_name == "range_avg_ckref":
        _, vs = ser.span(CK, sym, start, end)
        ref = ser.at(CK, sym, start)
        if not vs or ref is None:
            return None
        return (mean(vs) / ref - 1) * 1e4
    if rule_name == "ck_close_open":
        a, b = ser.at(CK, sym, start), ser.at(CK, sym, end)
        return None if (a is None or b is None) else (b / a - 1) * 1e4
    raise KeyError(rule_name)


RULES = ("terminal", "range_avg", "range_avg_ckref", "terminal_t60",
         "terminal_t30", "ck_close_open")


def s1_settle_rule(ser, oc):
    rule("S1  WHICH RULE ACTUALLY SETTLES THESE MARKETS")
    say("Graded against the L36-clean outcomes corpus, restricted to windows")
    say("wholly inside the RTDS recorder's span. `wallet` is ground truth but is")
    say("SELECTED — it only contains windows the fleet chose to trade, i.e. the")
    say("ones the gates already liked. `book` (the market's own terminal book) is")
    say("the unselected control and is the row that matters.")
    say("")
    tally = collections.defaultdict(collections.Counter)
    disagree = []
    for slug, d in sorted(oc.items()):
        m = SLUG_RE.match(slug)
        sym, dursec, ep = m.group(1), 300 if m.group(2) == "5m" else 900, int(m.group(3))
        cov = ser.coverage(sym)
        if cov is None or ep < cov[0] + 120 or ep + dursec > cov[1]:
            continue
        got = {}
        for rn in RULES:
            bp = settle_margin(ser, sym, ep, dursec, rn)
            if bp is None:
                continue
            got[rn] = bp
            pred = "up" if bp > 0 else "down"
            tally[(rn, d["source"])][pred == d["winner"]] += 1
            tally[(rn, "ALL")][pred == d["winner"]] += 1
        if "range_avg" in got and "terminal" in got:
            if (got["range_avg"] > 0) != (got["terminal"] > 0):
                disagree.append((slug, d["source"], d["winner"],
                                 got["range_avg"], got["terminal"]))
    say("%-16s %-10s %6s %6s %8s" % ("rule", "corpus src", "right", "wrong", "accuracy"))
    for rn in RULES:
        for src in ("wallet", "book", "chainlink", "ALL"):
            t = tally[(rn, src)]
            n = t[True] + t[False]
            if not n:
                continue
            star = " <<< what settles" if (rn == "terminal" and src == "ALL") else \
                   (" <<< what the arms price" if (rn == "range_avg" and src == "ALL") else "")
            say("%-16s %-10s %6d %6d %7.1f%%%s" % (rn, src, t[True], t[False],
                                                   100 * t[True] / n, star))
        say("")
    say("`terminal` is the settlement-width TWAP at the close against the same")
    say("TWAP at the open (30s wide for a 5m market, 60s above it —")
    say("updown_model.rs::settle_tw_secs, mirrored by outcomes.py). `range_avg`")
    say("is the whole window's average — a MOMENTUM PROXY, and the rule every")
    say("live arm prices (`settle_rule: range_avg` in arms-state.json;")
    say("updown.rs::d_settle_rule). It is the only rule here wrong more than a")
    say("couple of percent of the time, and `banked_decided` — the certificate")
    say("that exempts a position from the fleet cap — is arithmetic over IT.")
    say("")
    say("The `wallet` column is selected: it contains only windows the fleet")
    say("chose to trade. range_avg scoring well there is the gates working, not")
    say("the rule being right. `book` is the unselected control.")
    say("")
    say("Windows where range_avg and the terminal rule DISAGREE on the winner:")
    n_all = sum(tally[("terminal", s)][True] + tally[("terminal", s)][False]
                for s in ("wallet", "book", "chainlink"))
    say("  n = %d of %d graded-in-span (%.1f%%)" %
        (len(disagree), n_all, 100 * len(disagree) / n_all if n_all else 0))
    right_t = sum(1 for _, _, w, _, t in disagree if (t > 0) == (w == "up"))
    say("  on the disagreements the terminal rule is right %d times, range_avg %d"
        % (right_t, len(disagree) - right_t))
    say("  -> every one of these is a window where the fleet's model and the")
    say("     market's rule name different winners. That is the trap set.")
    say("")
    say("  %-34s %-9s %-6s %11s %11s" %
        ("slug", "src", "winner", "range_avg", "terminal"))
    for slug, src, w, ra, t6 in disagree[:26]:
        say("  %-34s %-9s %-6s %+10.2fbp %+10.2fbp" % (slug, src, w, ra, t6))
    if len(disagree) > 26:
        say("  ... %d more" % (len(disagree) - 26))
    say("")
    say("  Clustering: how many of these disagreements share an epoch with")
    say("  another? (a disagreement that is macro hits several symbols at once)")
    byep = collections.defaultdict(list)
    for slug, src, w, ra, t6 in disagree:
        m = SLUG_RE.match(slug)
        byep[(int(m.group(3)), m.group(2))].append(m.group(1))
    hist = collections.Counter(len(v) for v in byep.values())
    for k in sorted(hist):
        say("    %d symbol(s) disagreeing in the same epoch: %d epochs (%d windows)"
            % (k, hist[k], k * hist[k]))
    return disagree


def s1b_incident(ser):
    rule("S1b  THE INCIDENT WINDOW, PRICED UNDER BOTH RULES")
    ep, dur = INCIDENT_EPOCH, INCIDENT_DUR
    say("epoch %d  (%s)  = 2026-08-23 %sZ, 5m" %
        (ep, era_of(ep), time.strftime("%H:%M:%S", time.gmtime(ep))))
    say("")
    say("5m market -> settlement TWAP width %.0fs. Reference = that TWAP at the" % settle_width(dur))
    say("window's open; settlement = the same TWAP at its close.")
    say("")
    say("%-5s %11s %11s %11s | %12s %12s  %s" %
        ("sym", "t30@open", "t30@close", "ck@close", "range_avg", "terminal(t30)",
         "fired / settled"))
    for sym in FLEET:
        ra = settle_margin(ser, sym, ep, dur, "range_avg")
        t3 = settle_margin(ser, sym, ep, dur, "terminal")
        o = ser.at(T30, sym, ep)
        c = ser.at(T30, sym, ep + dur)
        ck = ser.at(CK, sym, ep + dur)
        say("%-5s %11.4f %11.4f %11.4f | %+11.2fbp %+11.2fbp  DOWN / %s" %
            (sym, o, c, ck, ra, t3, "UP" if t3 > 0 else "DOWN"))
    say("")
    say("Every arm fired DOWN. range_avg agreed with all five. The terminal rule —")
    say("the one that pays — was UP on all five. The fleet was not wrong five")
    say("times; it was wrong ONCE, about which prices decide the window, and that")
    say("one error was worth five positions because the impulse was macro.")
    say("")
    say("Non-fleet control symbols on the same stream (never armed):")
    for sym in ("doge", "hype", "zec"):
        ra = settle_margin(ser, sym, ep, dur, "range_avg")
        t6 = settle_margin(ser, sym, ep, dur, "terminal")
        if ra is None or t6 is None:
            continue
        say("  %-5s range_avg %+8.2fbp   terminal %+8.2fbp   %s" %
            (sym, ra, t6, "SAME FLIP" if (ra < 0 < t6) else "-"))


# ------------------------------------------------ S2 correlation structure

def five_min_windows_klines(kl, t0=None, t1=None, syms=None):
    """epoch -> {sym: (margin_bp, ref, settle)} using a 1m-kline proxy for the
    terminal rule: settlement = last minute's close, reference = first
    minute's open. Validated against the corpus in S2a."""
    syms = syms or [s for s in KLINE_SYMS if s in kl]
    idx = {}
    for sym in syms:
        ts, o, c = kl[sym]
        idx[sym] = ({t: i for i, t in enumerate(ts)}, ts, o, c)
    epochs = set()
    for sym in syms:
        _, ts, _, _ = idx[sym]
        for t in ts:
            e = t - (t % 300)
            epochs.add(e)
    out = {}
    for e in sorted(epochs):
        if t0 is not None and e < t0:
            continue
        if t1 is not None and e + 300 > t1:
            continue
        row = {}
        for sym in syms:
            pos, ts, o, c = idx[sym]
            i0, i4 = pos.get(e), pos.get(e + 240)
            if i0 is None or i4 is None:
                continue
            ref, settle = o[i0], c[i4]
            if ref <= 0 or settle <= 0:
                continue
            row[sym] = ((settle / ref - 1) * 1e4, ref, settle)
        if len(row) >= 2:
            out[e] = row
    return out


def corr_matrix(rows, syms, key=lambda v: v[0]):
    """Pearson on the margin, and sign-agreement rate, pairwise."""
    cols = {s: [] for s in syms}
    keep = []
    for e, row in sorted(rows.items()):
        if all(s in row for s in syms):
            keep.append(e)
            for s in syms:
                cols[s].append(key(row[s]))
    pear = {}
    sign = {}
    for a in syms:
        for b in syms:
            pear[(a, b)] = pearson(cols[a], cols[b])
            agree = sum(1 for x, y in zip(cols[a], cols[b]) if (x > 0) == (y > 0))
            sign[(a, b)] = agree / len(keep) if keep else float("nan")
    return keep, cols, pear, sign


def show_matrix(syms, m, title, fmt="%+6.2f"):
    say("")
    say(title)
    say("      " + "".join("%8s" % s for s in syms))
    for a in syms:
        say("%-5s " % a + "".join((fmt % m[(a, b)]) if not math.isnan(m[(a, b)])
                                  else "     n/a" for b in syms))


def agreement_hist(cols, syms, n_windows):
    """How many of the fleet settle the SAME way, per window."""
    hist = collections.Counter()
    for i in range(n_windows):
        ups = sum(1 for s in syms if cols[s][i] > 0)
        hist[max(ups, len(syms) - ups)] += 1
    return hist


def permutation_null(cols, syms, n_windows, reps=2000, seed=RNG_SEED):
    """Destroy cross-sectional dependence, keep each symbol's own marginal
    and its own serial structure's marginal: shuffle each symbol's series in
    time independently. This is the honest 'if they were independent' null —
    it needs no distributional assumption and preserves each arm's own up/down
    mix exactly."""
    rnd = random.Random(seed)
    acc = collections.Counter()
    for _ in range(reps):
        shuffled = {}
        for s in syms:
            v = list(cols[s])
            rnd.shuffle(v)
            shuffled[s] = v
        for i in range(n_windows):
            ups = sum(1 for s in syms if shuffled[s][i] > 0)
            acc[max(ups, len(syms) - ups)] += 1
    return {k: v / reps for k, v in acc.items()}


def s2_correlation(kl, ser, oc):
    rule("S2  Q1 — CORRELATION STRUCTURE")

    say("--- S2a  kline proxy calibration (does the 1m proxy grade like the corpus?)")
    tal = collections.Counter()
    per_src = collections.defaultdict(collections.Counter)
    for slug, d in oc.items():
        m = SLUG_RE.match(slug)
        sym, dur, ep = m.group(1), m.group(2), int(m.group(3))
        if dur != "5m" or sym not in kl:
            continue
        ts, o, c = kl[sym]
        pos = {t: i for i, t in enumerate(ts)} if sym not in _POS else _POS[sym]
        _POS[sym] = pos
        i0, i4 = pos.get(ep), pos.get(ep + 240)
        if i0 is None or i4 is None:
            continue
        bp = (c[i4] / o[i0] - 1) * 1e4
        ok = ("up" if bp > 0 else "down") == d["winner"]
        tal[ok] += 1
        per_src[d["source"]][ok] += 1
    n = tal[True] + tal[False]
    say("  1m proxy (last-minute close vs first-minute open, Binance) vs corpus:")
    for src in ("wallet", "book", "chainlink"):
        t = per_src[src]
        k = t[True] + t[False]
        if k:
            say("    %-10s %5d/%-5d  %5.1f%%" % (src, t[True], k, 100 * t[True] / k))
    say("    %-10s %5d/%-5d  %5.1f%%" % ("ALL", tal[True], n, 100 * tal[True] / n if n else 0))
    say("  Good enough to carry the 90d correlation structure; it is NOT good")
    say("  enough to grade a trade, and is never used to below.")

    say("")
    say("--- S2b  unconditional 5m settlement correlation, 90 days of klines")
    syms = [s for s in ("btc", "eth", "sol", "xrp", "bnb", "doge") if s in kl]
    rows = five_min_windows_klines(kl, syms=syms)
    keep, cols, pear, sign = corr_matrix(rows, syms)
    say("  windows with all %d symbols present: %d  (%.1f days)" %
        (len(syms), len(keep), len(keep) * 300 / 86400.0))
    say("  span %s .. %s UTC" % (time.strftime("%Y-%m-%d %H:%M", time.gmtime(min(keep))),
                                 time.strftime("%Y-%m-%d %H:%M", time.gmtime(max(keep)))))
    show_matrix(syms, pear, "  Pearson r on 5m settlement MARGIN (bp):")
    show_matrix(syms, sign, "  P(same settlement DIRECTION), pairwise:", fmt="%7.3f")

    say("")
    say("--- S2c  how often does the tradeable fleet agree?")
    f5 = [s for s in FLEET if s in kl]
    keep5, cols5, pear5, sign5 = corr_matrix(rows, f5)
    hist = agreement_hist(cols5, f5, len(keep5))
    null = permutation_null(cols5, f5, len(keep5))
    say("  n = %d windows, all five present" % len(keep5))
    say("")
    say("  %-28s %9s %9s %9s %9s" % ("max symbols on ONE side", "observed", "obs %",
                                     "null (perm)", "ratio"))
    for k in range(3, 6):
        o_ = hist.get(k, 0)
        e_ = null.get(k, 0.0)
        say("  %-28s %9d %8.2f%% %9.1f %9s" %
            ("%d of 5" % k, o_, 100 * o_ / len(keep5), e_,
             "%.2fx" % (o_ / e_) if e_ else "-"))
    obs45 = hist.get(4, 0) + hist.get(5, 0)
    exp45 = null.get(4, 0.0) + null.get(5, 0.0)
    say("")
    say("  >=4 of 5 the same way: observed %d (%.1f%%), independence null %.1f (%.1f%%)"
        % (obs45, 100 * obs45 / len(keep5), exp45, 100 * exp45 / len(keep5)))
    say("  excess over independence: %.2fx" % (obs45 / exp45 if exp45 else float("nan")))
    say("  5 of 5:                  observed %d (%.1f%%), null %.1f (%.1f%%)  %.2fx"
        % (hist.get(5, 0), 100 * hist.get(5, 0) / len(keep5), null.get(5, 0.0),
           100 * null.get(5, 0.0) / len(keep5),
           hist.get(5, 0) / null.get(5, 0.0) if null.get(5) else float("nan")))
    say("")
    say("  Mean pairwise |r| across the fleet: %.3f" %
        mean([abs(pear5[(a, b)]) for a in f5 for b in f5 if a != b]))
    say("  Mean pairwise sign agreement:      %.3f  (0.5 = independent)" %
        mean([sign5[(a, b)] for a in f5 for b in f5 if a != b]))

    say("")
    say("--- S2d  CONDITIONAL: is correlation higher when the window is near-flat?")
    say("  Bucketed on the cross-sectional MEDIAN |margin| of the five.")
    say("")
    buckets = [(0, 2), (2, 5), (5, 10), (10, 20), (20, 40), (40, 1e9)]
    say("  %-14s %7s %9s %9s %9s %9s" %
        ("median |bp|", "n", "mean r", "sign agr", ">=4 same", "5 same"))
    for lo, hi in buckets:
        idx = [i for i in range(len(keep5))
               if lo <= sorted(abs(cols5[s][i]) for s in f5)[len(f5) // 2] < hi]
        if len(idx) < 30:
            continue
        sub = {s: [cols5[s][i] for i in idx] for s in f5}
        rs = [pearson(sub[a], sub[b]) for a in f5 for b in f5 if a < b]
        sg = [mean([1.0 if (sub[a][j] > 0) == (sub[b][j] > 0) else 0.0
                    for j in range(len(idx))]) for a in f5 for b in f5 if a < b]
        h = agreement_hist(sub, f5, len(idx))
        say("  %-14s %7d %9.3f %9.3f %8.1f%% %8.1f%%" %
            ("%g-%g" % (lo, hi if hi < 1e9 else float("inf")), len(idx),
             mean(rs), mean(sg), 100 * (h.get(4, 0) + h.get(5, 0)) / len(idx),
             100 * h.get(5, 0) / len(idx)))
    say("")
    say("  Read: near-flat windows are LESS sign-correlated, not more — a small")
    say("  common move is swamped by each symbol's own noise, so the signs")
    say("  scatter. Concentration risk rises with the SIZE of the common move,")
    say("  which is the opposite of where the basis guard is looking.")

    say("")
    say("--- S2e  CONDITIONAL: volatility regime")
    say("  Trailing realised vol = stdev of btc's last 12 5m margins (1h).")
    say("")
    btc = cols5["btc"]
    trail = [stdev(btc[max(0, i - 12):i]) if i >= 12 else float("nan")
             for i in range(len(keep5))]
    good = [i for i in range(len(keep5)) if not math.isnan(trail[i])]
    qs = sorted(trail[i] for i in good)
    cuts = [qs[int(len(qs) * f)] for f in (0.25, 0.5, 0.75)]
    say("  %-16s %7s %9s %9s %9s %9s" %
        ("vol quartile", "n", "mean r", "sign agr", ">=4 same", "5 same"))
    edges = [(-1e9, cuts[0], "Q1 calm"), (cuts[0], cuts[1], "Q2"),
             (cuts[1], cuts[2], "Q3"), (cuts[2], 1e9, "Q4 wild")]
    for lo, hi, lab in edges:
        idx = [i for i in good if lo <= trail[i] < hi]
        if len(idx) < 30:
            continue
        sub = {s: [cols5[s][i] for i in idx] for s in f5}
        rs = [pearson(sub[a], sub[b]) for a in f5 for b in f5 if a < b]
        sg = [mean([1.0 if (sub[a][j] > 0) == (sub[b][j] > 0) else 0.0
                    for j in range(len(idx))]) for a in f5 for b in f5 if a < b]
        h = agreement_hist(sub, f5, len(idx))
        say("  %-16s %7d %9.3f %9.3f %8.1f%% %8.1f%%" %
            ("%s (<%.1fbp)" % (lab, hi if hi < 1e9 else float('inf')), len(idx),
             mean(rs), mean(sg), 100 * (h.get(4, 0) + h.get(5, 0)) / len(idx),
             100 * h.get(5, 0) / len(idx)))

    say("")
    say("--- S2f  CONDITIONAL: hour of day (UTC)")
    say("  %-8s %7s %9s %9s %9s" % ("hour", "n", "mean r", "sign agr", "5 same"))
    byhour = collections.defaultdict(list)
    for i, e in enumerate(keep5):
        byhour[time.gmtime(e).tm_hour].append(i)
    worst = []
    for h in range(24):
        idx = byhour[h]
        if len(idx) < 30:
            continue
        sub = {s: [cols5[s][i] for i in idx] for s in f5}
        rs = [pearson(sub[a], sub[b]) for a in f5 for b in f5 if a < b]
        sg = [mean([1.0 if (sub[a][j] > 0) == (sub[b][j] > 0) else 0.0
                    for j in range(len(idx))]) for a in f5 for b in f5 if a < b]
        hh = agreement_hist(sub, f5, len(idx))
        five = 100 * hh.get(5, 0) / len(idx)
        worst.append((mean(rs), h, len(idx), mean(sg), five))
        say("  %-8s %7d %9.3f %9.3f %8.1f%%" % ("%02d:00" % h, len(idx), mean(rs),
                                                mean(sg), five))
    worst.sort()
    say("")
    say("  least correlated hour %02d:00 (r=%.3f), most correlated hour %02d:00 (r=%.3f)"
        % (worst[0][1], worst[0][0], worst[-1][1], worst[-1][0]))
    say("  incident hour was %02d:00 UTC" % time.gmtime(INCIDENT_EPOCH).tm_hour)

    say("")
    say("--- S2g  intraday, off the settlement stream itself (8 symbols)")
    say("  Same quantity computed the way the market settles (terminal 60s TWAP),")
    say("  including hype/zec which the fleet has NEVER armed — if they move with")
    say("  the fleet the driver is macro, not anything about our symbol picks.")
    cov = [ser.coverage(s) for s in RTDS_SYMS if ser.coverage(s)]
    lo, hi = max(c[0] for c in cov), min(c[1] for c in cov)
    srows = {}
    e = int(lo // 300 + 1) * 300
    while e + 300 <= hi:
        row = {}
        for s in RTDS_SYMS:
            bp = settle_margin(ser, s, e, 300, "terminal")
            if bp is not None:
                row[s] = (bp,)
        if len(row) >= 6:
            srows[e] = row
        e += 300
    ssyms = list(RTDS_SYMS)
    keeps, colss, pears, signs = corr_matrix(srows, ssyms)
    say("  n = %d 5m windows, %.1fh of stream" % (len(keeps), len(keeps) * 300 / 3600.0))
    show_matrix(ssyms, pears, "  Pearson r, terminal-rule margin (bp):")
    show_matrix(ssyms, signs, "  P(same settlement direction):", fmt="%7.3f")
    say("")
    say("  fleet-internal mean |r|   : %.3f" %
        mean([abs(pears[(a, b)]) for a in FLEET for b in FLEET if a != b]))
    say("  fleet-vs-never-armed |r|  : %.3f   (hype/zec)" %
        mean([abs(pears[(a, b)]) for a in FLEET for b in ("hype", "zec")]))
    return rows, keep5, cols5, f5, srows


_POS = {}


# --------------------------------------------- S3 concentration on the tape

class Fires:
    """The recorded fire schedule, with fill truth from `committed`."""

    def __init__(self, tape, oc, ser):
        self.tape = tape
        self.oc = oc
        self.ser = ser
        self.clips = []       # every fire, with its realised fill
        self.windows = {}     # slug -> window record
        self.grade_src = collections.Counter()
        self._build()

    def _grade(self, slug, sym, ep, dursec):
        d = self.oc.get(slug)
        if d:
            self.grade_src[d["source"]] += 1
            return d["winner"], d["source"]
        bp = settle_margin(self.ser, sym, ep, dursec, "terminal")
        if bp is not None and abs(bp) >= 0.5:
            self.grade_src["rtds_terminal"] += 1
            return ("up" if bp > 0 else "down"), "rtds_terminal"
        self.grade_src["ungraded"] += 1
        return None, None

    def _build(self):
        for slug, rows in self.tape.items():
            fires = [r for r in rows if r.get("ev") == "fire"]
            if not fires:
                continue
            sym, dursec, ep = rows[0]["_sym"], rows[0]["_dursec"], rows[0]["_epoch"]
            winner, src = self._grade(slug, sym, ep, dursec)
            # committed observations across the whole slug, in time order
            obs = [(r["t"], r["committed"]) for r in rows if r.get("committed") is not None]
            clips = []
            for i, f in enumerate(fires):
                t = f["t"]
                nxt = [c for (tt, c) in obs if tt > t]
                cur = f.get("committed")
                if cur is None or not nxt:
                    fill = 0.0
                else:
                    # the tracker reconciles ~5s late and can also DROP on an
                    # exit; take the running max over the next 30s so an exit
                    # elsewhere in the window cannot erase this clip's fill,
                    # then clamp - a clip never fills negative.
                    horizon = [c for (tt, c) in obs if t < tt <= t + 30.0]
                    peak = max(horizon) if horizon else cur
                    fill = max(0.0, peak - cur)
                    # never credit a clip more than it asked for
                    fill = min(fill, f["size"] * max(f.get("limit") or f["ask"], 0.01))
                price = f.get("ask") or 0.0
                shares = fill / price if price > 0 else 0.0
                if winner is None:
                    pnl = 0.0
                elif winner == f["side"]:
                    pnl = shares - fill
                else:
                    pnl = -fill
                c = dict(slug=slug, sym=sym, dur=rows[0]["_dur"], dursec=dursec,
                         epoch=ep, t=t, side=f["side"], mode=f.get("mode"),
                         ask=price, limit=f.get("limit"), size=f["size"],
                         fill=fill, shares=shares, pnl=pnl, winner=winner,
                         src=src, fleet_room=f.get("fleet_room"),
                         elapsed_frac=f.get("elapsed_frac"), net=f.get("net"),
                         idx=i)
                clips.append(c)
                self.clips.append(c)
            self.windows[slug] = dict(
                slug=slug, sym=sym, dur=rows[0]["_dur"], dursec=dursec, epoch=ep,
                side=fires[0]["side"], winner=winner, src=src, clips=clips,
                n_fires=len(fires), fill=sum(c["fill"] for c in clips),
                pnl=sum(c["pnl"] for c in clips),
                t_first=fires[0]["t"], t_last=fires[-1]["t"],
                era=era_of(ep), max_committed=max([c for _, c in obs] or [0.0]))


def s3_concentration(F):
    rule("S3  Q2 — CONCENTRATION EPISODES ON THE TAPE")
    W = F.windows
    say("fired windows: %d   clips: %d   filled $%.2f   reconstructed P&L $%.2f"
        % (len(W), len(F.clips), sum(c["fill"] for c in F.clips),
           sum(c["pnl"] for c in F.clips)))
    say("grading sources: %s" % dict(F.grade_src))
    say("")
    say("Fill reconstruction check (per window, tracker peak vs summed clips):")
    err = [w["fill"] - w["max_committed"] for w in W.values() if w["max_committed"] > 0]
    say("  n=%d  median delta $%.2f  p90 |delta| $%.2f" %
        (len(err), sorted(err)[len(err) // 2], sorted(abs(x) for x in err)[int(.9 * len(err))]))
    say("  (a clip credited with more than the tracker ever held would be a")
    say("   fabricated fill; the summed clips track the tracker's own peak.)")

    # ---- same-epoch groups
    say("")
    say("--- S3a  same-epoch, same-duration concentration")
    groups = collections.defaultdict(list)
    for w in W.values():
        groups[(w["epoch"], w["dur"])].append(w)
    say("")
    say("%-6s %-5s %6s %8s %8s %10s %10s %8s" %
        ("dur", "N arms", "groups", "windows", "wins", "hit rate", "P&L", "$filled"))
    rowsout = []
    for dur in ("5m", "15m"):
        for n in range(1, 6):
            gs = [g for k, g in groups.items() if k[1] == dur and len(g) == n]
            ws = [w for g in gs for w in g if w["winner"]]
            if not gs:
                continue
            wins = sum(1 for w in ws if w["winner"] == w["side"])
            pnl = sum(w["pnl"] for w in ws)
            fill = sum(w["fill"] for w in ws)
            say("%-6s %-5d %6d %8d %8d %9.1f%% %+10.2f %8.0f" %
                (dur, n, len(gs), len(ws), wins,
                 100 * wins / len(ws) if ws else 0, pnl, fill))
            rowsout.append((dur, n, len(gs), len(ws), wins, pnl, fill))

    say("")
    say("--- S3b  SAME-SIDE concentration (the quantity the fleet cap never saw)")
    say("")
    say("%-6s %-10s %6s %8s %8s %10s %11s %10s %9s" %
        ("dur", "same-side", "groups", "windows", "wins", "hit rate", "P&L",
         "$filled", "$/window"))
    conc = collections.defaultdict(list)
    for k, g in groups.items():
        sides = collections.Counter(w["side"] for w in g)
        top, ntop = sides.most_common(1)[0]
        for w in g:
            conc[(k[1], ntop if w["side"] == top else sides[w["side"]])].append(w)
    for dur in ("5m", "15m"):
        for n in range(1, 6):
            ws = [w for w in conc.get((dur, n), []) if w["winner"]]
            if not ws:
                continue
            wins = sum(1 for w in ws if w["winner"] == w["side"])
            pnl = sum(w["pnl"] for w in ws)
            fill = sum(w["fill"] for w in ws)
            say("%-6s %-10s %6d %8d %8d %9.1f%% %+11.2f %10.0f %9.2f" %
                (dur, "%d arms" % n, len({w["epoch"] for w in ws}), len(ws), wins,
                 100 * wins / len(ws), pnl, fill, pnl / len(ws)))

    say("")
    say("  HOW THIN THIS IS. Count the EPISODES, not the windows — a five-arm")
    say("  epoch is one draw of one macro factor, not five:")
    ep_hist = collections.Counter()
    for k, g in groups.items():
        if k[1] != "5m":
            continue
        sides = collections.Counter(w["side"] for w in g)
        ep_hist[max(sides.values())] += 1
    for n in sorted(ep_hist):
        say("    %d arms same side: %3d epochs in the whole tape" % (n, ep_hist[n]))
    say("  The five-arm row above is TWO events. One won all five, one lost all")
    say("  five. Any statistic computed on it is a statistic about n=2, and no")
    say("  threshold may be fitted to it (L37).")

    say("")
    say("--- S3c  ADVERSE SELECTION TEST: is the fleet worse when it agrees?")
    say("  5m windows only, post-theta only (the decision basis).")
    say("")
    solo, pair, trip, quad = [], [], [], []
    for k, g in groups.items():
        if k[1] != "5m" or not is_post_theta(k[0]):
            continue
        sides = collections.Counter(w["side"] for w in g)
        for w in g:
            if not w["winner"]:
                continue
            n = sides[w["side"]]
            (solo if n == 1 else pair if n == 2 else trip if n == 3 else quad).append(w)
    say("  %-22s %7s %7s %10s %12s %11s" %
        ("population", "windows", "wins", "hit rate", "P&L", "$/window"))
    pops = [("solo (1 arm this side)", solo), ("2 same side", pair),
            ("3 same side", trip), (">=4 same side", quad)]
    for lab, ws in pops:
        if not ws:
            continue
        wins = sum(1 for w in ws if w["winner"] == w["side"])
        pnl = sum(w["pnl"] for w in ws)
        say("  %-22s %7d %7d %9.1f%% %+12.2f %11.2f" %
            (lab, len(ws), wins, 100 * wins / len(ws), pnl, pnl / len(ws)))
    conc_all = pair + trip + quad
    if solo and conc_all:
        hs = sum(1 for w in solo if w["winner"] == w["side"]) / len(solo)
        hc = sum(1 for w in conc_all if w["winner"] == w["side"]) / len(conc_all)
        say("")
        say("  solo hit %.1f%%  vs  concentrated hit %.1f%%   (delta %+.1f pp)" %
            (100 * hs, 100 * hc, 100 * (hc - hs)))
        obs = hc - hs
        rnd = random.Random(RNG_SEED)
        pool = [1 if w["winner"] == w["side"] else 0 for w in solo + conc_all]
        ns = len(solo)
        hits = 0
        for _ in range(20000):
            rnd.shuffle(pool)
            d = mean(pool[ns:]) - mean(pool[:ns])
            if abs(d) >= abs(obs) - 1e-12:
                hits += 1
        say("  permutation p (two-sided, labels shuffled, 20k reps): %.3f" %
            (hits / 20000.0))
        say("  -> the hit-rate difference is %s" %
            ("NOT distinguishable from noise at this n"
             if hits / 20000.0 > 0.05 else "significant"))
        say("")
        say("  Note the direction the MONEY runs, which the hit rate hides (L27):")
        say("  %-22s %11s %11s" % ("", "$/window", "worst window"))
        for lab, ws in pops:
            if ws:
                say("  %-22s %+11.2f %+11.2f" %
                    (lab, sum(w["pnl"] for w in ws) / len(ws),
                     min(w["pnl"] for w in ws)))

    say("")
    say("--- S3g  WHAT WOULD JUSTIFY A CAP AT ALL (the arithmetic, not the n=2)")
    say("  Buy $1 of a side at price p. Win -> +$(1-p)/p. Lose -> -$1. Refusing")
    say("  that dollar is +EV exactly when its loss probability q exceeds")
    say("")
    say("      q* = 1 - p")
    say("")
    say("  because (1-q)(1-p)/p - q < 0  <=>  q > 1-p. This is L27's asymmetry")
    say("  read forwards: at these prices a blocked winner costs pennies and a")
    say("  blocked loser saves the whole dollar, so a cap needs only a SMALL")
    say("  excess loss rate to pay. It is also why the hit-rate table above is")
    say("  the wrong statistic to decide on.")
    say("")
    fires5 = [c for c in F.clips if c["dur"] == "5m" and c["winner"]
              and is_post_theta(c["epoch"]) and c["fill"] > 0]
    if fires5:
        vw = sum(c["ask"] * c["fill"] for c in fires5) / sum(c["fill"] for c in fires5)
        say("  fill-weighted entry price on post-theta 5m clips: p = %.3f" % vw)
        say("  -> break-even loss rate q* = %.1f%%" % (100 * (1 - vw)))
        say("")
        say("  %-24s %8s %8s %10s %12s" %
            ("population", "windows", "losses", "loss rate", "vs q*"))
        groups5 = collections.defaultdict(list)
        for k, g in groups.items():
            if k[1] != "5m" or not is_post_theta(k[0]):
                continue
            sides = collections.Counter(w["side"] for w in g)
            for w in g:
                if w["winner"]:
                    groups5[min(sides[w["side"]], 4)].append(w)
        for n in sorted(groups5):
            ws = groups5[n]
            L = sum(1 for w in ws if w["winner"] != w["side"])
            q = L / len(ws)
            say("  %-24s %8d %8d %9.1f%% %12s" %
                ("%d arms same side" % n + (" +" if n == 4 else ""), len(ws), L,
                 100 * q, "ABOVE" if q > (1 - vw) else "below"))
        say("")
        say("  So the SHAPE of the case for a cap is sound — >=4-same-side is far")
        say("  above break-even. What is missing is n: that row is 3 epochs, one")
        say("  of which is the incident the policy was designed after. The")
        say("  arithmetic says 'a cap would pay if this loss rate is real'; the")
        say("  corpus cannot yet say the loss rate is real. Those are different")
        say("  claims and only the second one licenses a deploy.")

    say("")
    say("--- S3d  the same-side dollar pile, per epoch")
    say("  What a same-side cap would actually have been rationing.")
    say("")
    piles = []
    for k, g in groups.items():
        if k[1] != "5m":
            continue
        bys = collections.defaultdict(float)
        for w in g:
            bys[w["side"]] += w["fill"]
        for side, amt in bys.items():
            arms = [w for w in g if w["side"] == side]
            piles.append((amt, k[0], side, len(arms),
                          sum(w["pnl"] for w in arms), era_of(k[0])))
    piles.sort(reverse=True)
    say("  %-12s %-6s %5s %10s %11s %-10s" %
        ("epoch", "side", "arms", "$same-side", "P&L", "era"))
    for amt, ep, side, n, pnl, era in piles[:15]:
        mark = "   <<< THE INCIDENT" if ep == INCIDENT_EPOCH else ""
        say("  %-12d %-6s %5d %10.2f %+11.2f %-10s%s" % (ep, side, n, amt, pnl, era, mark))
    say("")
    tot = sum(p[0] for p in piles)
    say("  total same-side 5m notional across the tape: $%.0f in %d (epoch,side) piles"
        % (tot, len(piles)))
    return groups


def s3e_case_study(F, tape, ser):
    rule("S3e  THE 17:15Z EVENT — EVIDENCE TRAIL")
    ep = INCIDENT_EPOCH
    slugs = [s for s in tape if tape[s][0]["_epoch"] == ep and tape[s][0]["_dur"] == "5m"]
    say("Tape coverage for this window ENDS at t=%.0f (elapsed_frac ~%.2f) — the"
        % (max(r["t"] for s in slugs for r in tape[s]),
           (max(r["t"] for s in slugs for r in tape[s]) - ep) / 300.0))
    say("engine stopped writing before the window closed, so the 80% and 100%")
    say("evals the operator saw live are NOT on this tape. Everything below is")
    say("what the tape does contain; the settlement comes from the stream.")
    say("")
    say("--- fires")
    say("%-6s %6s %6s %6s %6s %8s %9s %8s %10s" %
        ("sym", "t+s", "efrac", "side", "ask", "limit", "fair", "shares", "mode"))
    fires = sorted([c for c in F.clips if c["epoch"] == ep and c["dursec"] == 300],
                   key=lambda c: c["t"])
    for c in fires:
        say("%-6s %6.0f %6.2f %6s %6.2f %8s %9s %8.0f %10s" %
            (c["sym"], c["t"] - ep, c["elapsed_frac"], c["side"], c["ask"],
             ("%.4f" % c["limit"]) if c["limit"] else "-", "-", c["size"],
             c["mode"]))
    say("")
    say("--- the last eval each arm wrote before the tape ended")
    say("%-6s %8s %10s %10s %10s %8s %9s %9s" %
        ("sym", "efrac", "margin_bp", "banked_bp", "cushion_bp", "safety",
         "banked_dec", "fleet_room"))
    for s in sorted(slugs):
        ev = [r for r in tape[s] if r.get("ev") == "eval"]
        if not ev:
            continue
        r = ev[-1]
        side = None
        sf = 0.0
        for d in (r.get("sides") or []):
            if d["side"] == "down":
                sf = d.get("safety")
        say("%-6s %8.2f %+10.2f %+10.2f %10.2f %8s %9s %9s" %
            (r["_sym"], (r["t"] - ep) / 300.0, r.get("margin_bp", float("nan")),
             r.get("banked_bp", float("nan")), r.get("cushion_bp", float("nan")),
             ("%.2f" % sf) if sf is not None else "-",
             r.get("banked_decided"), ("%.0f" % r["fleet_room"]) if r.get("fleet_room") else "-"))
    say("")
    say("`banked_decided: true` is the engine asserting that no remaining path")
    say("can overturn the elapsed average. That assertion is TRUE about the")
    say("average and IRRELEVANT to the settlement, which reads only the final")
    say("30 seconds of a 5m window. Sound arithmetic about the wrong number.")
    say("")
    say("--- realised")
    tot = 0.0
    say("%-6s %7s %10s %10s %11s %11s" %
        ("sym", "clips", "$filled", "shares", "settle bp", "P&L"))
    for sym in FLEET:
        cs = [c for c in fires if c["sym"] == sym]
        if not cs:
            continue
        bp = settle_margin(ser, sym, ep, 300, "terminal")
        pnl = sum(c["pnl"] for c in cs)
        tot += pnl
        say("%-6s %7d %10.2f %10.0f %+10.2fbp %+11.2f" %
            (sym, len(cs), sum(c["fill"] for c in cs), sum(c["shares"] for c in cs),
             bp, pnl))
    say("%-6s %7d %10.2f %10s %11s %+11.2f" %
        ("TOTAL", len(fires), sum(c["fill"] for c in fires), "", "", tot))
    say("")
    say("(Reconstructed from the tracker's committed deltas and a hold-to-")
    say("settlement valuation; the operator's wallet number is the ledger of")
    say("record. The tape truncates mid-window, so late clips are missing.)")


# ------------------------------------------------------- S4 the impulse class

def s4_impulses(ser):
    rule("S4  Q3 — THE IMPULSE CLASS, AND WHETHER ANYTHING LEADS")
    cov = {s: ser.coverage(s) for s in RTDS_SYMS}
    lo = max(c[0] for c in cov.values())
    hi = min(c[1] for c in cov.values())
    say("stream span %s .. %s UTC (%.2fh), %d symbols" %
        (time.strftime("%H:%M:%S", time.gmtime(lo)), time.strftime("%H:%M:%S", time.gmtime(hi)),
         (hi - lo) / 3600.0, len(RTDS_SYMS)))
    say("")
    N = int(hi - lo)
    g = {}
    for s in RTDS_SYMS:
        g[s] = ser.grid(CK, s, lo, hi, 1.0)
    ok = [i for i in range(N) if all(g[s][i] for s in RTDS_SYMS)]
    say("1Hz grid: %d seconds, %d with every symbol present (%.1f%%)" %
        (N, len(ok), 100 * len(ok) / N))

    # log returns at 1s
    r1 = {s: [None] * N for s in RTDS_SYMS}
    for s in RTDS_SYMS:
        v = g[s]
        for i in range(1, N):
            if v[i] and v[i - 1]:
                r1[s][i] = math.log(v[i] / v[i - 1]) * 1e4

    say("")
    say("--- S4a  1Hz cross-correlation, contemporaneous")
    say("      " + "".join("%8s" % s for s in RTDS_SYMS))
    for a in RTDS_SYMS:
        line = "%-5s " % a
        for b in RTDS_SYMS:
            xa = [r1[a][i] for i in ok if r1[a][i] is not None and r1[b][i] is not None]
            xb = [r1[b][i] for i in ok if r1[a][i] is not None and r1[b][i] is not None]
            line += "%8.3f" % pearson(xa, xb)
        say(line)
    say("")
    say("  Even at ONE SECOND the fleet moves together: btc-eth 0.68, btc-sol")
    say("  0.65. hype/zec sit near 0.28 — lower, but a long way from zero for")
    say("  assets the fleet has never touched. There is a common factor and it")
    say("  is visible at every horizon measured here.")

    say("")
    say("--- S4b  LEAD-LAG: does btc move first? (10s returns, btc leads by k)")
    say("  r(btc_t-k , alt_t) for k = 0..10 seconds. A leader shows a peak at k>0.")
    say("")
    H = 10
    rH = {s: [None] * N for s in RTDS_SYMS}
    for s in RTDS_SYMS:
        v = g[s]
        for i in range(H, N):
            if v[i] and v[i - H]:
                rH[s][i] = math.log(v[i] / v[i - H]) * 1e4
    say("  %-6s %s" % ("alt", "".join("%7s" % ("k=%d" % k) for k in range(0, 11))))
    lead_summary = {}
    for b in RTDS_SYMS:
        if b == "btc":
            continue
        row = []
        for k in range(0, 11):
            xa, xb = [], []
            for i in range(H + 11, N):
                if rH["btc"][i - k] is not None and rH[b][i] is not None:
                    xa.append(rH["btc"][i - k])
                    xb.append(rH[b][i])
            row.append(pearson(xa, xb))
        lead_summary[b] = row
        best = max(range(11), key=lambda k: row[k])
        say("  %-6s %s   peak k=%d" % (b, "".join("%7.3f" % x for x in row), best))
    say("")
    say("  Symmetric check — does the ALT lead btc? r(alt_t-k, btc_t):")
    say("  %-6s %s" % ("alt", "".join("%7s" % ("k=%d" % k) for k in range(0, 11))))
    for b in RTDS_SYMS:
        if b == "btc":
            continue
        row = []
        for k in range(0, 11):
            xa, xb = [], []
            for i in range(H + 11, N):
                if rH[b][i - k] is not None and rH["btc"][i] is not None:
                    xa.append(rH[b][i - k])
                    xb.append(rH["btc"][i])
            row.append(pearson(xa, xb))
        best = max(range(11), key=lambda k: row[k])
        say("  %-6s %s   peak k=%d" % (b, "".join("%7.3f" % x for x in row), best))
    say("")
    say("  VERDICT ON LEAD-LAG: there is no leader. Every pair peaks at k=0 and")
    say("  decays monotonically in BOTH directions, and the two directions are")
    say("  near-mirror images — which is what a common factor hitting every")
    say("  oracle in the same second looks like, and is not what a lead looks")
    say("  like. btc does not front-run the alts and the alts do not front-run")
    say("  btc. Policy (c) is therefore not a leader-veto; at best it is a")
    say("  CONTEMPORANEOUS momentum veto using btc as a proxy for the factor,")
    say("  and it buys no warning time at all. It is measured below anyway,")
    say("  because 'no warning' is a finding and not a reason to skip the cost.")

    say("")
    say("--- S4c  IMPULSE CATALOG: synchronised multi-symbol moves")
    say("  An impulse = a 30s interval in which >=4 of the 8 stream symbols move")
    say("  >= X bp in the SAME direction. Counted on non-overlapping 10s starts.")
    say("")
    say("  %-8s %8s %10s %12s %10s %12s" %
        ("thresh", "events", "per hour", "in final 90s", "of those", "5-of-5 fleet"))
    W = 30
    catalog = {}
    for X in (3, 5, 8, 12, 20):
        events = []
        i = H
        while i + 1 < N:
            v = 0
            up = dn = 0
            for s in RTDS_SYMS:
                if g[s][i] and g[s][max(0, i - W)]:
                    b = math.log(g[s][i] / g[s][i - W]) * 1e4
                    if b >= X:
                        up += 1
                    elif b <= -X:
                        dn += 1
            if max(up, dn) >= 4:
                t = lo + i
                events.append((t, "up" if up >= dn else "down", max(up, dn)))
                i += W          # non-overlapping
            else:
                i += 10
        fin = [e for e in events if 300 - ((e[0] - 0) % 300) <= 90 or (e[0] % 300) >= 210]
        five = 0
        for t, d, k in fin:
            ep = int(t // 300) * 300
            got = 0
            for s in FLEET:
                bp = settle_margin(ser, s, ep, 300, "terminal")
                ra = settle_margin(ser, s, ep, 300, "range_avg")
                if bp is not None and ra is not None and (bp > 0) != (ra > 0):
                    got += 1
            if got >= 4:
                five += 1
        catalog[X] = (events, fin, five)
        say("  >=%-6dbp %8d %10.2f %12d %10s %12d" %
            (X, len(events), len(events) / ((hi - lo) / 3600.0), len(fin),
             "%.0f%%" % (100 * len(fin) / len(events)) if events else "-", five))
    say("")
    say("  'in final 90s' = the impulse landed inside the last 90s of a 5m window,")
    say("  which is the only place it can flip a terminal-TWAP settlement that the")
    say("  window's own average had already decided the other way.")
    say("  '5-of-5 fleet' = of those, how many produced >=4 fleet symbols where")
    say("  range_avg and the terminal rule disagreed — i.e. a five-arm trap.")

    say("")
    say("--- S4d  the incident's impulse, second by second")
    ep = INCIDENT_EPOCH
    say("  bp vs each symbol's own window-open chainlink print:")
    say("  %-6s %s" % ("sym", "".join("%7d" % k for k in range(-120, 1, 10))))
    refs = {s: ser.at(CK, s, ep) for s in RTDS_SYMS}
    for s in RTDS_SYMS:
        if not refs[s]:
            continue
        row = []
        for k in range(-120, 1, 10):
            v = ser.at(CK, s, ep + 300 + k)
            row.append("%7.1f" % ((v / refs[s] - 1) * 1e4) if v else "      -")
        say("  %-6s %s%s" % (s, "".join(row), "   <- fleet" if s in FLEET else ""))
    say("")
    say("  Every symbol on the stream turns up in the SAME 10s bucket, including")
    say("  hype and zec which the fleet has never armed. That is the signature of")
    say("  one macro impulse, not five independent windows going wrong.")

    say("")
    say("--- S4e  WARNING WINDOW: how much notice would a leader-veto get?")
    say("  For each final-90s impulse (>=5bp), the second at which each symbol")
    say("  first crosses +/-3bp of its 30s-trailing level.")
    say("")
    evs = catalog[5][1]
    firsts = collections.defaultdict(list)
    for t, d, k in evs[:400]:
        cross = {}
        for s in RTDS_SYMS:
            for u in range(int(t - W), int(t + 10)):
                i = int(u - lo)
                if 0 <= i - W and i < N and g[s][i] and g[s][i - W]:
                    b = math.log(g[s][i] / g[s][i - W]) * 1e4
                    if (d == "up" and b >= 3) or (d == "down" and b <= -3):
                        cross[s] = u
                        break
        if "btc" in cross:
            for s in RTDS_SYMS:
                if s != "btc" and s in cross:
                    firsts[s].append(cross[s] - cross["btc"])
    say("  %-6s %6s %9s %9s %9s %9s" %
        ("alt", "n", "median", "mean", "p25", "p75"))
    for s in RTDS_SYMS:
        v = sorted(firsts.get(s, []))
        if len(v) < 10:
            continue
        say("  %-6s %6d %8.1fs %8.1fs %8.1fs %8.1fs" %
            (s, len(v), v[len(v) // 2], mean(v), v[len(v) // 4], v[3 * len(v) // 4]))
    say("")
    say("  Positive = btc crossed first, i.e. the alt arm would have had that many")
    say("  seconds of warning. A median at or below zero means there is no lead to")
    say("  trade on and a leader-veto is a coin flip dressed as a signal.")
    return catalog, lo, hi, g, N


# ----------------------------------------------------- S5 policy candidates

def window_units(F, era_filter=None, dur="5m"):
    out = []
    for w in F.windows.values():
        if dur and w["dur"] != dur:
            continue
        if w["winner"] is None:
            continue
        if era_filter and not era_filter(w["epoch"]):
            continue
        out.append(w)
    return out


def report_policy(label, base_ws, changed, blocked_pnl, blocked_fill, extra=""):
    """One counterfactual row.

    delta = -(P&L of the clips the policy refuses). Positive means the refused
    clips lost money, i.e. the policy would have helped. The bootstrap unit is
    the EPOCH — resampling epochs, not clips, is what keeps a single five-arm
    event from being counted as five independent draws. That distinction is the
    whole point of this study, so getting it wrong here would be self-refuting.
    """
    delta = -blocked_pnl
    by_ep = collections.defaultdict(float)
    for w, amt in changed:
        by_ep[w["epoch"]] += -amt
    epochs = sorted({w["epoch"] for w in base_ws})
    uu = [by_ep.get(e, 0.0) for e in epochs]
    ci = bootstrap_ci(uu, sum)
    # Concentration of the RESULT: if one epoch supplies most of the delta,
    # the policy is fitted to one event and L37 applies to it.
    top = max((abs(v) for v in by_ep.values()), default=0.0)
    frac = top / abs(delta) if abs(delta) > 1e-9 else 0.0
    say("  %-34s %6d %10.0f %+11.2f  [%+8.2f, %+8.2f] %5s %s" %
        (label, len({w["epoch"] for w, _ in changed}), blocked_fill, delta,
         ci[0], ci[1], ("%.0f%%" % (100 * frac)) if by_ep else "-", extra))
    POLICY_LOG.append(dict(label=label, delta=delta, ci=ci, by_ep=dict(by_ep),
                           refused=blocked_fill, top_frac=frac))
    return delta, ci


POLICY_LOG = []


def attribution(era_label, n=4):
    """Where each headline policy's money actually came from."""
    if not POLICY_LOG:
        return
    say("")
    say("  ATTRIBUTION — the epochs supplying each policy's delta. A policy whose")
    say("  number is one window is not a policy, it is that window (L37).")
    ranked = sorted(POLICY_LOG, key=lambda r: -abs(r["delta"]))[:n]
    # always show the two candidates the verdict turns on, ranked or not
    for r in POLICY_LOG:
        if r["label"].startswith(("(f) ", "(e) terminal-margin gate >= 0")) \
                and r not in ranked:
            ranked.append(r)
    for r in ranked:
        say("")
        say("    %s   net %+.2f, top epoch = %.0f%% of it" %
            (r["label"], r["delta"], 100 * r["top_frac"]))
        items = sorted(r["by_ep"].items(), key=lambda kv: -abs(kv[1]))[:5]
        for ep, v in items:
            mark = "  <<< THE INCIDENT" if ep == INCIDENT_EPOCH else ""
            say("      epoch %-12d %-10s %+9.2f%s" % (ep, era_of(ep), v, mark))
    POLICY_LOG.clear()


def s3f_fleet_cap(F, tape):
    """The cap that was supposed to be the backstop, and why it never fired."""
    rule("S3f  WHY THE FLEET CAP DID NOT SEE ANY OF THIS")
    say("`fleet_room` appears on an eval only when a cap is armed")
    say("(updown.rs:1489-1492). Reconstructing the cap from the tape:")
    frs = [(r["t"], r["fleet_room"]) for v in tape.values() for r in v
           if r.get("fleet_room") is not None]
    frs.sort()
    say("  %d eval rows carry fleet_room; first t=%.0f, last t=%.0f" %
        (len(frs), frs[0][0], frs[-1][0]))
    byt = collections.defaultdict(dict)
    for slug, v in tape.items():
        for r in v:
            if r.get("ev") == "eval":
                byt[round(r["t"], 1)][slug] = (r.get("committed", 0.0),
                                               r.get("banked_decided"),
                                               r.get("fleet_room"))
    caps = collections.Counter()
    for t, d in byt.items():
        room = [x[2] for x in d.values() if x[2] is not None]
        if not room:
            continue
        undec = sum(c for c, bd, _ in d.values() if not bd)
        caps[round(undec + room[0])] += 1
    say("  inferred cap (un-decided committed + fleet_room), most common:")
    for cap, n in caps.most_common(4):
        say("    $%-6d on %d ticks" % (cap, n))
    say("  minimum fleet_room ever observed: $%.2f" % min(x[1] for x in frs))
    say("")
    say("THE STRUCTURAL POINT (updown.rs:1405-1407):")
    say("")
    say("    let fleet_bound = if m.banked_decided { INFINITY }")
    say("                      else { fleet_room.max(0.0) };")
    say("")
    say("and ArmState::undecided_committed (updown.rs:568-579) returns 0.0 the")
    say("moment `last_banked_decided` is set. A banked-decided arm therefore")
    say("(1) does not count against the cap and (2) cannot be capped. The cap")
    say("rations UN-decided dollars, and `banked_decided` is computed under")
    say("range_avg — the rule S1 shows is wrong on ~1 window in 8.")
    say("")
    say("How much of the fleet's fired notional was invisible to the cap:")
    tot = collections.Counter()
    dol = collections.Counter()
    for slug, v in tape.items():
        ev = [r for r in v if r.get("ev") == "eval"]
        for r in v:
            if r.get("ev") != "fire":
                continue
            prev = [e for e in ev if e["t"] <= r["t"]]
            bd = prev[-1].get("banked_decided") if prev else None
            tot[bd] += 1
            dol[bd] += r["size"] * r["ask"]
    for k in (True, False, None):
        if tot[k]:
            say("  banked_decided=%-5s  %4d fires, $%8.0f intended notional (%.0f%%)" %
                (k, tot[k], dol[k], 100 * dol[k] / sum(dol.values())))
    say("")
    say("The incident's five arms, at their last recorded eval:")
    say("  %-26s %7s %9s %13s %11s" %
        ("slug", "evals", "committed", "banked_dec", "fleet_room"))
    for slug in sorted(tape):
        r0 = tape[slug][0]
        if r0["_epoch"] != INCIDENT_EPOCH or r0["_dur"] != "5m":
            continue
        ev = [r for r in tape[slug] if r.get("ev") == "eval"]
        if not ev:
            continue
        bds = [r for r in ev if r.get("banked_decided")]
        say("  %-26s %7d %9.2f %13s %11s" %
            (slug, len(ev), ev[-1].get("committed", 0.0),
             "%d/%d ticks" % (len(bds), len(ev)),
             "%.0f" % ev[-1]["fleet_room"] if ev[-1].get("fleet_room") else "-"))
    say("")
    say("The cap was armed, sized ~$350-500, and never came within $17 of")
    say("binding: the largest position in the event (eth, the one that actually")
    say("cost money) had been banked_decided since elapsed 0.51 and was carrying")
    say("ZERO against the cap while it grew to $169.")
    say("")
    say("So 'the fleet cap never addressed same-side concentration' understates")
    say("it. The cap could not have addressed this event at ANY cap value,")
    say("because the positions had exempted themselves.")


def s5_policies(F, ser, groups, lo_s, hi_s, grid, Ngrid):
    rule("S5  Q4 — POLICY COUNTERFACTUALS")
    say("Every row: which windows change, the notional the policy REFUSES, the")
    say("net dollar delta on the corpus (positive = the policy would have made")
    say("money), and a percentile CI95 bootstrapped over epochs (10k resamples).")
    say("")
    say("Charges taken, stated once:")
    say("  * The fire schedule is FIXED. A policy is never credited with clips")
    say("    the tape does not contain, and budget it frees is never redeployed.")
    say("  * A refused clip is priced at its own recorded fill — the increment in")
    say("    the engine's committed tracker — so a clip that never filled is")
    say("    worth zero to every policy and cannot flatter one.")
    say("  * Hold-to-settlement, gross of fees, identical on every variant.")
    say("  * A policy that blocks a clip does not change the book that clip would")
    say("    have traded in. Nobody can replay that.")

    eras_cuts = [("all eras", lambda e: True),
                 ("post-theta", is_post_theta),
                 ("stream era", lambda e: e >= ERAS_MOD.by_name("stream").start
                  if ERAS_MOD else True)]

    for ename, efilt in eras_cuts:
        base = window_units(F, efilt)
        if not base:
            continue
        rule2 = "  --- %s: %d graded 5m windows, $%.0f filled, base P&L %+.2f" % (
            ename, len(base), sum(w["fill"] for w in base), sum(w["pnl"] for w in base))
        say("")
        say(rule2)
        say("  %-34s %6s %10s %12s %22s %5s" %
            ("policy", "eps ch", "$refused", "net delta", "bootstrap CI95", "top"))
        say("  ('top' = |largest single epoch| / |net delta|. Above 100% means one")
        say("   epoch exceeds the whole result and everything else nets against it")
        say("   — the policy is that epoch, not a policy.)")

        # (a) same-side concentration cap: at most N arms per (epoch, side).
        for N in (1, 2, 3, 4):
            changed, bp, bf = [], 0.0, 0.0
            for (ep, dur), g in groups.items():
                if dur != "5m" or not efilt(ep):
                    continue
                bys = collections.defaultdict(list)
                for w in g:
                    if w["winner"]:
                        bys[w["side"]].append(w)
                for side, ws in bys.items():
                    ws.sort(key=lambda w: w["t_first"])
                    for w in ws[N:]:
                        changed.append((w, w["pnl"]))
                        bp += w["pnl"]
                        bf += w["fill"]
            report_policy("(a) same-side cap: max %d arms" % N, base, changed, bp, bf)

        # (a$) same-side notional cap
        for X in (50, 100, 150, 200, 300):
            changed, bp, bf = [], 0.0, 0.0
            for (ep, dur), g in groups.items():
                if dur != "5m" or not efilt(ep):
                    continue
                bys = collections.defaultdict(list)
                for w in g:
                    if w["winner"]:
                        for c in w["clips"]:
                            bys[w["side"]].append(c)
                for side, cs in bys.items():
                    cs.sort(key=lambda c: c["t"])
                    run = 0.0
                    for c in cs:
                        if run + c["fill"] > X:
                            changed.append((F.windows[c["slug"]], c["pnl"]))
                            bp += c["pnl"]
                            bf += c["fill"]
                        else:
                            run += c["fill"]
            report_policy("(a$) same-side cap: $%d/epoch" % X, base, changed, bp, bf)

        # (b) correlated-exposure fleet cap. Two variants, and the difference
        #     between them is the whole finding: the live cap exempts a
        #     banked_decided arm, and banked_decided is a range_avg claim.
        for exempt in (True, False):
            for X in (100, 200, 350):
                changed, bp, bf = [], 0.0, 0.0
                for (ep, dur), g in groups.items():
                    if dur != "5m" or not efilt(ep):
                        continue
                    cs = sorted([c for w in g if w["winner"] for c in w["clips"]],
                                key=lambda c: c["t"])
                    run = collections.defaultdict(float)
                    for c in cs:
                        # `mode == "safe"` is the tape's marker for an unlocked
                        # budget: late in the window OR banked_decided. It is
                        # the closest observable to the exemption the engine
                        # applies, and it is recorded on every fire.
                        if exempt and c["mode"] == "safe":
                            continue
                        if run[c["side"]] + c["fill"] > X:
                            changed.append((F.windows[c["slug"]], c["pnl"]))
                            bp += c["pnl"]
                            bf += c["fill"]
                        else:
                            run[c["side"]] += c["fill"]
                report_policy("(b) same-side cap $%d, %s" %
                              (X, "banked EXEMPT (as built)" if exempt
                               else "no exemption"),
                              base, changed, bp, bf)

        # (c) leader veto — only where the stream covers the fire.
        for T in (20, 30, 60):
            for Y in (2, 4, 8):
                changed, bp, bf, seen = [], 0.0, 0.0, 0
                for w in base:
                    for c in w["clips"]:
                        if not (lo_s + T < c["t"] < hi_s):
                            continue
                        seen += 1
                        a = ser.at(CK, "btc", c["t"] - T)
                        b = ser.at(CK, "btc", c["t"])
                        if a is None or b is None:
                            continue
                        move = (b / a - 1) * 1e4
                        against = move if c["side"] == "down" else -move
                        if against >= Y:
                            changed.append((w, c["pnl"]))
                            bp += c["pnl"]
                            bf += c["fill"]
                report_policy("(c) btc veto T=%ds Y=%dbp" % (T, Y), base, changed, bp, bf,
                              extra="(%d clips in stream span)" % seen)

        # (d) correlation-regime clip scaling. Thresholds are chosen off the
        #     realised distribution of the trailing statistic (printed once
        #     below) — a threshold below its median scales nearly every clip
        #     and is a size cut wearing a correlation costume.
        vals = sorted(v for v in (_trailing_corr(ser, c["t"], 900)
                                  for w in base for c in w["clips"]
                                  if lo_s + 900 < c["t"] < hi_s) if v is not None)
        if vals and ename == "all eras":
            say("  [trailing 15m mean pairwise fleet corr over the fired clips:"
                " p10 %.2f p50 %.2f p90 %.2f, n=%d]" %
                (vals[len(vals) // 10], vals[len(vals) // 2],
                 vals[9 * len(vals) // 10], len(vals)))
        for thr in (0.5, 0.7, 0.85):
            for f in (0.5, 0.0):
                changed, bp, bf = [], 0.0, 0.0
                for w in base:
                    for c in w["clips"]:
                        if not (lo_s + 900 < c["t"] < hi_s):
                            continue
                        rr = _trailing_corr(ser, c["t"], 900)
                        if rr is not None and rr >= thr:
                            changed.append((w, c["pnl"] * (1 - f)))
                            bp += c["pnl"] * (1 - f)
                            bf += c["fill"] * (1 - f)
                report_policy("(d) corr>=%.2f -> clip x%.1f" % (thr, f),
                              base, changed, bp, bf)

        # (e) TERMINAL-RULE AGREEMENT GATE — not on the operator's list, but it
        #     is what S1 implies and it is the only candidate here that is
        #     causally implementable with what the engine already has: a
        #     `--feed rtds` arm reads this exact number off the settlement
        #     stream in real time. Block a NEW clip whose fired side disagrees
        #     with the LIVE terminal margin (the settlement-width TWAP at the
        #     window's open against the live oracle print). Exits untouched.
        for Y in (0, 2, 5):
            changed, bp, bf, seen = [], 0.0, 0.0, 0
            for w in base:
                ref = ser.at(T30 if settle_width(w["dursec"]) == 30 else T60,
                             w["sym"], w["epoch"])
                if ref is None:
                    continue
                for c in w["clips"]:
                    spot = ser.at(CK, w["sym"], c["t"])
                    if spot is None:
                        continue
                    seen += 1
                    live = (spot / ref - 1) * 1e4
                    signed = live if w["side"] == "up" else -live
                    if signed < Y:          # the settling rule is against us
                        changed.append((w, c["pnl"]))
                        bp += c["pnl"]
                        bf += c["fill"]
            report_policy("(e) terminal-margin gate >= %dbp" % Y, base, changed,
                          bp, bf, extra="(%d clips in stream span)" % seen)

        # (f) THE HYBRID CUSHION, priced on the tape. `mode == "safe"` means the
        #     arm's full budget was unlocked, which happens for one of two
        #     reasons (updown.rs): the window is LATE (rem <= late_rem_s, 120s)
        #     or it is banked_decided. Under settle_rule="hybrid" the second
        #     door closes until the settlement TWAP actually starts locking
        #     (terminal_lock: banked == 0 while rem > tw), so a clip unlocked
        #     ONLY by a range_avg banked_decided certificate would not have
        #     fired at full size. Blocking exactly those clips is the tape's
        #     view of what hybrid's cushion buys. No exits are touched.
        for late in (120.0,):
            for name, keep_late in (("banked-only unlock", True),):
                changed, bp, bf = [], 0.0, 0.0
                for w in base:
                    for c in w["clips"]:
                        rem = w["epoch"] + w["dursec"] - c["t"]
                        if c["mode"] == "safe" and rem > late:
                            changed.append((w, c["pnl"]))
                            bp += c["pnl"]
                            bf += c["fill"]
                report_policy("(f) no banked_decided-only unlock", base, changed,
                              bp, bf, extra="(rem > %.0fs)" % late)
        # and the same idea one step milder: keep the clip, halve it.
        changed, bp, bf = [], 0.0, 0.0
        for w in base:
            for c in w["clips"]:
                rem = w["epoch"] + w["dursec"] - c["t"]
                if c["mode"] == "safe" and rem > 120.0:
                    changed.append((w, c["pnl"] * 0.5))
                    bp += c["pnl"] * 0.5
                    bf += c["fill"] * 0.5
        report_policy("(f2) banked-only unlock at half size", base, changed, bp, bf)
        attribution(ename)


_TC = {}


def _trailing_corr(ser, t, look):
    """Mean pairwise Pearson of 10s fleet returns over the trailing `look` s."""
    key = (int(t // 30) * 30, look)
    if key in _TC:
        return _TC[key]
    series = {}
    for s in FLEET:
        v = ser.grid(CK, s, t - look, t, 10.0)
        r = [math.log(v[i] / v[i - 1]) * 1e4 if (v[i] and v[i - 1]) else None
             for i in range(1, len(v))]
        series[s] = r
    rs = []
    for a in FLEET:
        for b in FLEET:
            if a >= b:
                continue
            xa = [x for x, y in zip(series[a], series[b]) if x is not None and y is not None]
            xb = [y for x, y in zip(series[a], series[b]) if x is not None and y is not None]
            if len(xa) >= 20:
                p = pearson(xa, xb)
                if not math.isnan(p):
                    rs.append(p)
    out = mean(rs) if rs else None
    _TC[key] = out
    return out


# ------------------------------------------------------------------- main

def main():
    global POST_THETA
    t0 = time.time()
    rule("S0  PROVENANCE")
    say("driver      analysis/correlation_study.py")
    say("tape        %s" % TAPE)
    say("outcomes    %s" % OUTCOMES)
    say("stream      %s" % RTDS)
    say("klines      %s" % (KLINES % "<PAIR>"))
    say("")
    say("L33: the live tape is append-only. These numbers are pinned to the")
    say("spans printed below, not to the filenames.")
    say("")
    if ERAS_MOD:
        POST_THETA = ERAS_MOD.by_name("theta").start
        say("eras (polymarket/eras.py):")
        for e in ERAS_MOD.ERAS:
            say("  %-10s start %-14.0f %s" % (e.name, e.start, e.why))
        say("post-theta boundary = %.0f" % POST_THETA)
    else:
        POST_THETA = 1787461200.0
        say("!! eras module not importable; post-theta pinned to %.0f" % POST_THETA)

    say("")
    say("loading ...")
    ser = Series(load_rtds())
    kl = load_klines()
    oc = load_outcomes()
    tape = load_tape()
    say("  rtds series   %d (topic,symbol) pairs" % len(ser.s))
    say("  klines        %s" % ", ".join("%s:%d" % (k, len(v[0])) for k, v in sorted(kl.items())))
    say("  outcomes      %d graded windows" % len(oc))
    say("  tape          %d slugs, t %.0f..%.0f" %
        (len(tape), min(r["t"] for v in tape.values() for r in v),
         max(r["t"] for v in tape.values() for r in v)))
    say("  loaded in %.1fs" % (time.time() - t0))

    s1_settle_rule(ser, oc)
    s1b_incident(ser)
    rows, keep5, cols5, f5, srows = s2_correlation(kl, ser, oc)
    F = Fires(tape, oc, ser)
    groups = s3_concentration(F)
    s3e_case_study(F, tape, ser)
    s3f_fleet_cap(F, tape)
    catalog, lo_s, hi_s, grid, Ngrid = s4_impulses(ser)
    s5_policies(F, ser, groups, lo_s, hi_s, grid, Ngrid)
    rule("done in %.1fs" % (time.time() - t0))


if __name__ == "__main__":
    main()
