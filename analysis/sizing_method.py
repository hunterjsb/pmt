"""Derive every arm's size and clip from the wallet-graded record.

WHY THIS EXISTS
---------------
Live sizes today are hand-scaled history: btc5 $1000/150, eth5 $900/110,
sol5 $400/50, xrp5 $100/10, bnb5 $50/10 (EU box), the 15m arms parked at $1.
No line of that ladder is derived from a measurement — it is the shape of the
fleet's conviction, not of its edge. This script replaces the conviction with
arithmetic, and every constant it uses is either measured here or cited to the
study that measured it.

THE PAYOFF, WHICH DECIDES EVERYTHING ELSE
-----------------------------------------
A window that wins pays $1/share on shares bought at `c`; a window that loses
pays $0 and the whole committed notional is gone. Measured over 280
wallet-graded windows the loss leg is EXACTLY -100.00% of notional in every era
and every arm — there is no partial loss in this book. Per dollar of notional:

    win  (prob p):  +g,  g = (1 - c)/c
    lose (prob 1-p): -1

which collapses to one identity worth tattooing on the fleet:

    BREAK-EVEN WIN RATE == THE PRICE WE PAY ON THE WINDOWS THAT PAY.

Buy at 0.948 and you must win 94.8% of the time to tread water. Every sizing
question downstream is "does this arm's win rate beat its own entry price, by
how much, and how sure are we". Note what this makes of a size increase: it
CANNOT create edge. Only entering cheaper (a lower c) or picking better (a
higher p) can. Size only scales whatever sign the edge already has.

WHY THE PAYOFF IS MEASURED ON WINNING WINDOWS ONLY
--------------------------------------------------
`g` is the notional-weighted return of the windows that WON. That is not a
convenience, it is the only unbiased estimator available here, and two wrong
ones are easy to reach for first:

  * sum($)/sum(shares) over all windows is arithmetically correct about the past
    and useless about the future. One eth window bought into a book that had
    collapsed to 8.5c contributed 1,740 shares and drags eth's apparent cost
    from 0.95 to 0.40. Those shares paid $0.
  * The notional-weighted price over ALL windows is better but still biased
    upward in our favour, because a cheap entry price is *correlated with
    losing*: buying at 0.45 lowers the average price (raising apparent payoff)
    while its loss is counted only once in `p`. Pairing an all-window price with
    an all-window win rate quietly books the discount and ignores that we only
    got the discount on trades that went to zero.

Only winning windows ever pay (1-c)/c, so only winning windows can measure it.
The loss leg needs no estimator at all: it is -100.00% of notional, exactly, in
every era and every arm across 280 graded windows.

THE FORMULAS
------------
1. Kelly on a two-outcome bet, gain `g` and loss `l` as fractions of the stake:

       f_full = (p*g - (1-p)*l) / (g*l)          # fraction of bankroll as NOTIONAL
       f_qtr  = 0.25 * f_full                    # ROADMAP R2's standing intent

   With l = 1 and g = (1-c)/c this is exactly `(p - c)/(1 - c)`, i.e. the same
   closed form as analysis/r2_kelly_sim.py::size_usdc, which derives it from the
   ask side. Here `c` is the wallet's REALIZED cost per share, so the fee is
   already inside it rather than modelled.

2. Small-sample honesty — a Beta prior centred on the arm's OWN break-even:

       p_shrunk = (wins_eff + K*c) / (n_eff + K),   K = PRIOR_WINDOWS = 30

   The prior mean is `c`, i.e. ZERO EDGE, so an arm with no record sizes to zero
   and has to earn its way up. K is 30 because that is the ROADMAP's own
   calibration bar ("no size increases until each p-bucket shows calibration
   over >=30 decided windows"): an arm's record only outvotes the null once it
   clears that bar. Wilson's one-sided 90% lower bound is printed beside it as
   the honesty column, the way r2_sizing_report.md did.

3. Era + feed weighting — `n_eff` is not a headcount. Windows are weighted by
   how much their policy regime still predicts the present (ERA_WEIGHT, keyed to
   polymarket.eras) times, for an arm that has since migrated to the RTDS
   settlement stream, FEED_DISCOUNT: a binance-fed window measured a different
   market-data path than the arm runs now.

4. THE FLEET BUDGET, derived twice and taken at the minimum. This is the part
   that makes the per-symbol caps outputs instead of inputs.

   (a) BOTTOM-UP. Summing per-arm quarter-Kelly assumes independence. The arms
       are not independent: mean pairwise correlation of 5m settlement margin
       over 90 days is 0.767 (analysis/correlation_study.md Result 1, n=25,927),
       and all five symbols settle the same direction 53.4% of the time against
       an independence null of 6.3%. For an equal-weight book of N assets with
       mean pairwise rho, portfolio variance is sigma^2 (1+(N-1)rho)/N, so the
       log-optimal TOTAL is

           N_eff = N / (1 + (N-1)*rho)
           bottom_up = N_eff/N * sum_i(f_qtr_i) * bankroll

       With N=5 and rho=0.767, N_eff = 1.21 — the fleet is one and a fifth bets
       wearing five hats, and every arm's standalone Kelly gets the same 0.24x
       haircut.

   (b) TOP-DOWN, and this one needs no correlation assumption at all because the
       correlation is already inside the data. Group every graded window into
       FLEET SLOTS (maximal clusters of windows that overlap in time), and treat
       each slot as ONE bet with its own notional and its own P&L. Then measure
       (p, g, l) across slots and quarter-Kelly that. A slot in which four arms
       lost together shows up as one bad bet with a near -100% return, which is
       exactly the event the bottom-up view cannot see.

   The fleet budget is min(a, b). Allocation within it is proportional to each
   arm's own f_qtr, so the evidence decides the ranking and the fleet decides
   the scale.

5. Three states, not one dial. An arm whose measured edge is positive gets a
   Kelly slice. An arm whose edge is negative but inside the noise gets a
   MEASUREMENT size — small enough that being wrong costs little, large enough
   that the record keeps growing, which is the ROADMAP's own "one small-size
   live night" doctrine. An arm whose win rate is clearly below its own entry
   price gets zero, because no size makes a negative edge positive.

Everything above is reproducible from a frozen snapshot of the graded record
(analysis/sizing_windows.json), so the table in analysis/sizing_method.md can be
checked without a wallet walk. `--refresh` re-walks.

    uv run --project pmtrader python analysis/sizing_method.py            # frozen
    uv run --project pmtrader python analysis/sizing_method.py --refresh  # re-walk
    uv run --project pmtrader python analysis/sizing_method.py --json

READ-ONLY. Never posts to the engine, never writes ~/.pmt.
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from collections import defaultdict
from datetime import UTC, datetime
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "pmtrader"))

from polymarket import env, eras, updown_slugs  # noqa: E402

FREEZE = Path(__file__).resolve().parent / "sizing_windows.json"

# ---------------------------------------------------------------------------
# Inputs the operator may move. Everything else is derived.

# Free USDC on the desktop trading wallet, 2026-08-23 17:00Z (`pmt balance`).
BANKROLL = 2283.31
# The EU L0 box runs bnb5 off its own wallet and its own engine, so it gets its
# own bankroll rather than a slice of the desktop's — a wipeout there cannot be
# funded from here without a bridge.
BANKROLL_EU = 182.0

KELLY_FRAC = 0.25          # ROADMAP R2: quarter-Kelly, not full
PRIOR_WINDOWS = 30.0       # ROADMAP R2's calibration bar, used as prior strength
WILSON_Z = 1.2816          # one-sided 90%

# analysis/correlation_study.md Result 1, 25,927 complete 5m windows over 90d:
# mean pairwise |r| of settlement margin across the tradeable fleet. That is the
# horizon the bet actually settles on, which is why it and not the 1Hz matrix
# (mean ~0.60) sets the haircut. The intraday terminal-margin cut says 0.810 and
# the trailing-15m distribution over fired clips is p10 0.60 / p50 0.70 / p90
# 0.81, so 0.767 sits mid-range and is the least cherry-picked of the three.
FLEET_RHO = 0.767

# An arm this far below its own break-even is not "unproven", it is losing.
# Inside the band it is a measurement problem; below it, it is a verdict.
NOISE_BAND_PP = 2.0
# Measurement size as a fraction of bankroll — the cost of continuing to learn.
MEASURE_FRAC = 0.010

# How much a window's policy era still predicts the present. Keyed to
# polymarket.eras; each weight cites that registry's own stated reason.
ERA_WEIGHT = {
    # No brakes, no theta. ROADMAP.md forbids ever restoring this policy, so its
    # windows measure a strategy that cannot legally be run again.
    "pre-brake": 0.0,
    # Brakes exist, but entry is the 50% clock gate R9 retired and the book is
    # the 2s REST poller.
    "brakes": 0.25,
    # Theta entry + window brake latch — the current decision core, still on the
    # REST book.
    "theta": 0.5,
    # WS-authoritative book + today's size ladder.
    "ws+scale": 1.0,
    # Current regime.
    "stream": 1.0,
}

# An arm that reads the Chainlink settlement stream is not the arm that read a
# Binance proxy plus a basis guess. Half weight, not zero, because the DECISION
# core (theta, brakes, cushion) is unchanged across the migration — except for
# xrp, whose binance record is affirmatively not evidence (the basis losses that
# struck it off the tradeable list), and which happens to have no non-pre-brake
# binance windows anyway, so the era weights already zero it.
FEED_DISCOUNT = 0.5

# Which feed each arm runs today (~/.pmt/engine/arms-state.json, 2026-08-23
# 17:00Z). Arms on "rtds" get FEED_DISCOUNT on every pre-`stream`-era window.
ARM_FEED = {
    "btc 5m": "rtds", "eth 5m": "rtds", "xrp 5m": "rtds",
    "sol 5m": "binance", "bnb 5m": "binance",
    "btc 15m": "binance", "eth 15m": "binance", "sol 15m": "binance",
}

# Live as of 2026-08-23 17:00Z. The 15m arms are PARKED (size/clip $1, min_fair
# 1.0, theta 1.0 — they cannot fire); bnb5 lives on the EU L0 box and is not in
# the desktop arms-state at all.
LIVE = {
    "btc 5m": (1000.0, 150.0), "eth 5m": (900.0, 110.0),
    "sol 5m": (400.0, 50.0), "xrp 5m": (100.0, 10.0),
    "bnb 5m": (50.0, 10.0),
    "btc 15m": (1.0, 1.0), "eth 15m": (1.0, 1.0), "sol 15m": (1.0, 1.0),
}
EU_ARMS = {"bnb 5m"}
LIVE_FLEET_CAP = 500.0     # arms-state.json fleet_undecided_cap, un-derived

# Clip rule constants — see clip_for().
MIN_CLIPS_PER_WINDOW = 4   # the brakes only bite BETWEEN clips
CLIP_DEPTH_CAP = 25.0      # top-of-book median $49.48 / p25 $18.80 (r2 depth scan)
CLIP_FLOOR = 5.0


# ---------------------------------------------------------------------------
# Acquisition — THE stats path, never a second grading convention.

def walk() -> list[dict]:
    """Grade the whole wallet through cli_crypto_stats.score_activity and reduce
    it to the per-window fields sizing needs.

    score_activity is the single acquisition path this repo allows (see that
    module's docstring); this adds no verdict of its own, it projects and stamps
    each window with the era whose policy priced it.
    """
    env.load_project_env()
    import cli_crypto_stats as stats  # noqa: PLC0415 - needs sys.path first
    from polymarket import wallet  # noqa: PLC0415

    rows = wallet.fetch_wallet_activity(wallet.funder_address(), 0.0)
    sb = stats.score_activity(rows, 0.0, tape_records=stats._fire_roll_records())

    out = []
    for w in sb["eff_windows"]:
        parsed = updown_slugs.parse(w["slug"])
        if parsed is None or not w["notional"] or not w["shares"]:
            continue
        _sym, _dur, start, end, series = parsed
        out.append({
            "slug": w["slug"], "series": series, "start": start, "end": end,
            "era": eras.for_start(start).name,
            "won": bool(w["won"]), "pnl": w["pnl"],
            "notional": w["notional"], "shares": w["shares"],
            "entry_px": w["entry_px"], "side": w["side"],
        })
    out.sort(key=lambda r: r["start"])
    return out


def load(refresh: bool) -> list[dict]:
    if refresh or not FREEZE.exists():
        windows = walk()
        FREEZE.write_text(json.dumps(
            {"frozen_at": datetime.now(UTC).isoformat(timespec="seconds"),
             "n": len(windows), "windows": windows}, indent=1) + "\n")
        return windows
    return json.loads(FREEZE.read_text())["windows"]


# ---------------------------------------------------------------------------
# Math

def weight_for(series: str, era: str) -> float:
    """This window's evidential weight: era relevance x feed relevance."""
    w = ERA_WEIGHT.get(era, 0.0)
    if ARM_FEED.get(series) == "rtds" and era != "stream":
        w *= FEED_DISCOUNT
    return w


def wilson_lo(wins: float, n: float, z: float = WILSON_Z) -> float:
    """One-sided lower confidence bound on a win rate. Accepts FRACTIONAL counts
    — n here is evidential weight, not a headcount, and a bound that refused
    fractions would force us to pretend a quarter-weighted window is a whole
    one."""
    if n <= 0:
        return 0.0
    p = wins / n
    d = 1.0 + z * z / n
    centre = p + z * z / (2 * n)
    margin = z * math.sqrt(max(p * (1 - p), 0.0) / n + z * z / (4 * n * n))
    return max(0.0, (centre - margin) / d)


def kelly(p: float, g: float, loss: float = 1.0) -> float:
    """Full-Kelly stake as a fraction of bankroll for a two-outcome bet paying
    +g of the stake with probability p and -loss with probability 1-p.

    Never negative: on a long-only strategy a negative Kelly means "do not bet",
    not "bet the other way" — we cannot short the losing side of a window.
    """
    if g <= 0 or loss <= 0:
        return 0.0
    return max(0.0, (p * g - (1.0 - p) * loss) / (g * loss))


def arm_evidence(windows: list[dict]) -> dict[str, dict]:
    """Per-arm weighted record and the Kelly inputs it implies."""
    by: dict[str, list[dict]] = defaultdict(list)
    for w in windows:
        by[w["series"]].append(w)

    out: dict[str, dict] = {}
    for series, ws in by.items():
        n_eff = w_eff = usd = pnl = 0.0
        win_usd = win_pnl = 0.0
        raw_n = raw_w = 0
        per_era: dict[str, list[int]] = {}
        for w in ws:
            raw_n += 1
            raw_w += w["won"]
            e = per_era.setdefault(w["era"], [0, 0])
            e[0] += 1
            e[1] += w["won"]
            wt = weight_for(series, w["era"])
            if wt <= 0:
                continue
            n_eff += wt
            w_eff += wt * w["won"]
            usd += wt * w["notional"]
            pnl += wt * w["pnl"]
            if w["won"]:
                # Only winners ever collect the spread, so only winners can
                # measure it. Module docstring has the bias argument.
                win_usd += wt * w["notional"]
                win_pnl += wt * w["pnl"]

        g = (win_pnl / win_usd) if win_usd > 0 else None
        # Break-even win rate, which for a -100% loss leg is exactly the price
        # the winners were bought at.
        c = (1.0 / (1.0 + g)) if g else None
        p_raw = (w_eff / n_eff) if n_eff else None
        p_s = ((w_eff + PRIOR_WINDOWS * c) / (n_eff + PRIOR_WINDOWS)
               if c is not None else None)
        f_full = kelly(p_s, g) if (p_s is not None and g) else 0.0
        raw_edge = (None if (p_raw is None or c is None) else (p_raw - c) * 100)
        if raw_edge is None:
            state = "no data"
        elif f_full > 0:
            state = "kelly"
        elif raw_edge >= -NOISE_BAND_PP:
            state = "measure"
        else:
            state = "off"
        out[series] = {
            "series": series, "raw_n": raw_n, "raw_w": raw_w, "per_era": per_era,
            "n_eff": n_eff, "w_eff": w_eff, "usd_eff": usd,
            "c": c, "g": g, "p_raw": p_raw, "p_shrunk": p_s,
            "p_wilson": wilson_lo(w_eff, n_eff) if n_eff else 0.0,
            "raw_edge_pp": raw_edge,
            "edge_pp": (None if (p_s is None or c is None) else (p_s - c) * 100),
            "ron_w": (pnl / usd) if usd else None,
            "f_full": f_full, "f_qtr": KELLY_FRAC * f_full,
            "feed": ARM_FEED.get(series, "?"), "state": state,
        }
    return out


def fleet_slots(windows: list[dict]) -> list[dict]:
    """Maximal clusters of windows that overlap in time — the unit a correlated
    event actually hits.

    Overlap, not equal start: a 15m window shares its clock with three 5m
    windows, and the 2026-08-23 04:00Z btc15+sol15 event is invisible to a
    same-start grouping the moment durations differ.
    """
    live = sorted((w for w in windows if weight_for(w["series"], w["era"]) > 0),
                  key=lambda r: r["start"])
    slots, cur = [], []
    for w in live:
        if cur and w["start"] < max(x["end"] for x in cur):
            cur.append(w)
        else:
            if cur:
                slots.append(cur)
            cur = [w]
    if cur:
        slots.append(cur)

    out = []
    for s in slots:
        notional = sum(x["notional"] for x in s)
        pnl = sum(x["pnl"] for x in s)
        wt = sum(weight_for(x["series"], x["era"]) for x in s) / len(s)
        out.append({"start": s[0]["start"], "n_arms": len(s), "weight": wt,
                    "notional": notional, "pnl": pnl,
                    "ret": pnl / notional if notional else 0.0,
                    "era": s[0]["era"],
                    "legs": [(x["series"], round(x["pnl"], 2)) for x in s],
                    "losers": sum(1 for x in s if not x["won"])})
    return out


def empirical_kelly(returns: list[float], weights: list[float],
                    step: float = 0.0005) -> float:
    """argmax_f of the weighted E[log(1 + f*R)] over an EMPIRICAL return
    distribution.

    A two-outcome Kelly on a mean win and a mean loss is the wrong tool for the
    fleet: averaging the loss leg turns "one slot in a hundred goes to -100%"
    into "losing slots return -50%", and Kelly then happily recommends betting
    the entire bankroll. The empirical form cannot make that mistake — a single
    -100% observation drives log(1 - f) and pins f strictly below 1 no matter
    how good the other ninety-nine slots look. Grid search, because the search
    space is one bounded dimension and a closed form would need the very
    parametric assumption this function exists to avoid.
    """
    if not returns:
        return 0.0
    best_f, best_v = 0.0, None
    f = 0.0
    while f <= 0.995:
        tot = 0.0
        ok = True
        for r, w in zip(returns, weights, strict=True):
            x = 1.0 + f * r
            if x <= 1e-12:
                ok = False
                break
            tot += w * math.log(x)
        if ok and (best_v is None or tot > best_v):
            best_f, best_v = f, tot
        f += step
    return best_f


def bootstrap_kelly(returns: list[float], weights: list[float],
                    n_boot: int = 1000, pct: float = 0.10,
                    seed: int = 7) -> float:
    """Lower percentile of the empirical Kelly under a resample of the slots.

    The point estimate is fit to the 102 slots we happen to have had, one of
    which is the 17:15Z five-arm wipeout. Resampling asks the only question that
    matters for a small corpus: how much of this number survives a different
    draw of the same process? The 10th percentile is the number a size increase
    has to clear, which is the same discipline the ROADMAP's calibration gate
    applies to p-buckets. Coarse grid on purpose — n_boot x a fine grid buys
    precision the corpus cannot support.
    """
    import random  # noqa: PLC0415 - only this function needs it

    if len(returns) < 5:
        return 0.0
    rng = random.Random(seed)
    n = len(returns)
    fs = []
    for _ in range(n_boot):
        idx = [rng.randrange(n) for _ in range(n)]
        fs.append(empirical_kelly([returns[i] for i in idx],
                                  [weights[i] for i in idx], step=0.005))
    fs.sort()
    return fs[max(0, min(n_boot - 1, int(pct * n_boot)))]


def fleet_bet(slots: list[dict]) -> dict:
    """The fleet treated as ONE bet, measured from its own slots.

    This is the top-down half of the budget and it needs no correlation
    assumption at all: a slot where five arms lost together already appears as
    one bad bet with a -100% return. The two-outcome summary (p, g, l) is
    printed as a diagnostic, but the sizing number is the EMPIRICAL Kelly over
    the whole slot-return distribution — see empirical_kelly for why the summary
    must not be sized on.
    """
    wsum = sum(s["weight"] for s in slots)
    if not slots or not wsum:
        return {"n": 0, "p": None, "g": None, "l": None, "f_qtr": 0.0}
    wins = [s for s in slots if s["ret"] > 0]
    losses = [s for s in slots if s["ret"] <= 0]
    p = (sum(s["weight"] for s in wins) / wsum) if wins else 0.0
    g = ((sum(s["weight"] * s["ret"] for s in wins)
          / sum(s["weight"] for s in wins)) if wins else None)
    loss = (-(sum(s["weight"] * s["ret"] for s in losses)
              / sum(s["weight"] for s in losses)) if losses else None)
    rets = [s["ret"] for s in slots]
    wts = [s["weight"] for s in slots]
    f = empirical_kelly(rets, wts)
    f_lo = bootstrap_kelly(rets, wts)
    return {"n": len(slots), "n_eff": wsum, "p": p, "g": g, "l": loss,
            "f_full": f, "f_qtr": KELLY_FRAC * f,
            "f_full_lo": f_lo, "f_qtr_lo": KELLY_FRAC * f_lo,
            "worst": min(s["ret"] for s in slots),
            "worst_usd": min(s["pnl"] for s in slots)}


def n_eff_bets(n_arms: int, rho: float) -> float:
    """Effective independent bets in an equal-weight book of `n_arms` with mean
    pairwise correlation `rho`. Portfolio variance is sigma^2(1+(N-1)rho)/N, so
    this is the factor by which the log-optimal TOTAL differs from one arm's
    standalone Kelly. rho=0 gives N, rho=1 gives 1."""
    if n_arms <= 0:
        return 0.0
    return n_arms / (1.0 + (n_arms - 1) * rho)


def clip_for(size: float) -> float:
    """Per-fire clip. Two binding reasons, neither of them Kelly:

    * The three brakes (15c distrust, 2c no-averaging-down, the window latch)
      can only refuse the NEXT clip. A window that spends its budget in two
      fires cannot be braked, so a window must be at least
      MIN_CLIPS_PER_WINDOW clips wide or the brake system is decorative.
    * Book depth. r2's depth scan (n=480 fires matched within 15s) puts
      top-of-book on our side at p25 $18.80 / median $49.48; a $25 clip is
      covered 70% of the time, a $100 clip 31%. Above the cap we are paying for
      our own impact, and every backtest that says otherwise is an upper bound.
    """
    if size <= 0:
        return 0.0
    return max(CLIP_FLOOR, min(size / MIN_CLIPS_PER_WINDOW, CLIP_DEPTH_CAP))


def growth_at(slots: list[dict], f: float) -> float | None:
    """Weighted E[log(1 + f*R)] over the slot returns — expected log growth per
    fleet slot at a stake of `f` x bankroll.

    None means RUIN: some observed slot return makes 1 + f*R non-positive, i.e.
    an event this book has actually produced would take the account past zero at
    that stake. That is not a modelling artifact and not a tail assumption — the
    17:15Z five-arm slot returned -100%, so any f >= 1 is undefined here by
    measurement.
    """
    tot = wsum = 0.0
    for s in slots:
        x = 1.0 + f * s["ret"]
        if x <= 1e-12:
            return None
        tot += s["weight"] * math.log(x)
        wsum += s["weight"]
    return tot / wsum if wsum else None


def concurrency_peaks(windows: list[dict], k: int = 5) -> list[dict]:
    """Peak simultaneous notional across overlapping windows — what one
    correlated event could actually have cost on the days we ran."""
    pts = []
    for w in windows:
        pts.append((w["start"], w["notional"]))
        pts.append((w["end"], -w["notional"]))
    pts.sort()
    live, peaks = 0.0, []
    for t, d in pts:
        live += d
        peaks.append((live, t))
    peaks.sort(reverse=True)
    return [{"usd": u, "t": t} for u, t in peaks[:k]]


# ---------------------------------------------------------------------------
# Allocation

def allocate(ev: dict[str, dict], slots: list[dict], bankroll: float,
             rho: float, arms: list[str], parked: set[str]) -> dict:
    """Fleet budget first, arms second.

    N in the correlation haircut is the number of arms that will actually be
    CONCURRENTLY EXPOSED — not the number in the arms table. An arm sized to
    zero, or one the operator has parked, contributes no correlated exposure,
    and counting it would shrink the haircut for the arms that do: punishing the
    survivors for the sins of the retired.

    The budget covers EVERYTHING armed, measurement included. Splitting it the
    other way — a Kelly budget plus a research line bolted on outside it —
    would let the fleet cap understate the exposure a correlated event reaches,
    which is the exact failure the cap exists to prevent. So the measurement
    line is paid first (it is a fixed research cost, not a bet whose size
    responds to edge) and the Kelly arms share what is left.
    """
    active = [a for a in arms if a not in parked and a in ev
              and ev[a]["state"] in ("kelly", "measure")]
    n = len(active)
    ne = n_eff_bets(n, rho)
    haircut = ne / n if n else 0.0

    sum_f = sum(ev[a]["f_qtr"] for a in active)
    bottom_up = sum_f * bankroll * haircut
    fb = fleet_bet(slots)
    # Quarter-Kelly on the POINT estimate. The bootstrap p10 is reported beside
    # it as the uncertainty band, deliberately NOT multiplied in: fractional
    # Kelly is already the uncertainty discount the ROADMAP mandates, and
    # stacking a confidence haircut on top of it discounts the same doubt twice.
    top_down = fb["f_qtr"] * bankroll
    budget = min(bottom_up, top_down)
    binding = ("bottom-up (per-arm Kelly x correlation haircut)"
               if bottom_up <= top_down else "top-down (fleet-as-one-bet Kelly)")

    measure_arms = [a for a in active if ev[a]["state"] == "measure"]
    kelly_arms = [a for a in active if ev[a]["state"] == "kelly"]
    measure_each = MEASURE_FRAC * bankroll
    measure_total = measure_each * len(measure_arms)
    kelly_budget = max(0.0, budget - measure_total)
    kelly_f = sum(ev[a]["f_qtr"] for a in kelly_arms)

    rows = {}
    for a in arms:
        e = ev.get(a)
        if a in parked:
            hypo = (measure_each if (e and e["state"] in ("kelly", "measure"))
                    else 0.0)
            rows[a] = {"size": 0.0, "clip": 0.0, "state": "parked",
                       "if_restarted": hypo}
            continue
        if e is None or e["state"] in ("off", "no data"):
            rows[a] = {"size": 0.0, "clip": 0.0, "state": "off"}
            continue
        size = (measure_each if e["state"] == "measure"
                else (kelly_budget * e["f_qtr"] / kelly_f if kelly_f else 0.0))
        rows[a] = {"size": size, "clip": clip_for(size), "state": e["state"]}
    return {
        "n_arms": n, "active": active, "rho": rho, "n_eff": ne,
        "haircut": haircut, "sum_f_qtr": sum_f,
        # The correlation-adjusted FULL-Kelly total: the stake above which
        # expected log growth starts falling, and above 2x of which it goes
        # negative however real the edge is. This is the yardstick live sizing
        # gets measured against.
        "full_kelly_total": (sum_f / KELLY_FRAC) * bankroll * haircut,
        "bottom_up": bottom_up,
        "top_down": top_down, "fleet_bet": fb, "budget": budget,
        "binding": binding, "measure_each": measure_each,
        "measure_total": measure_total, "kelly_budget": kelly_budget,
        "arms": rows, "committed_total": sum(r["size"] for r in rows.values()),
    }


# ---------------------------------------------------------------------------
# Report

def _ts(t: float) -> str:
    return datetime.fromtimestamp(t, UTC).strftime("%m-%d %H:%MZ")


def _f(v, spec: str, dash: str = "—") -> str:
    return dash if v is None else format(v, spec)


def report(windows: list[dict], bankroll: float, bankroll_eu: float,
           rho: float) -> dict:
    ev = arm_evidence(windows)
    slots = fleet_slots(windows)
    desktop = sorted(a for a in LIVE if a not in EU_ARMS)
    # An arm the operator has parked stays parked: this study sizes arms, it
    # does not decide which experiments are running.
    parked = {a for a in LIVE if LIVE[a][0] <= 1.0}
    eu_slots = [s for s in slots if any(k in EU_ARMS for k, _ in s["legs"])]

    alloc = allocate(ev, slots, bankroll, rho, desktop, parked)
    eu = allocate(ev, eu_slots or slots, bankroll_eu, rho, sorted(EU_ARMS), parked)

    print("=" * 78)
    print("SIZING METHOD — quarter-Kelly per arm inside a correlation-derived fleet budget")
    print("=" * 78)
    print(f"windows graded {len(windows)}   fleet slots {len(slots)}   "
          f"bankroll ${bankroll:,.2f} desktop + ${bankroll_eu:,.2f} EU   rho {rho:.3f}")
    print()

    print("--- 1. Evidence per arm (era- and feed-weighted) ---------------------")
    print(f"{'arm':9s} {'raw':>9s} {'n_eff':>6s} {'feed':>8s} {'c=BE':>7s} "
          f"{'p_raw':>7s} {'p_shr':>7s} {'p_w90':>7s} {'edge':>9s} {'f_qtr':>7s} state")
    for a in sorted(ev, key=lambda k: -ev[k]["n_eff"]):
        e = ev[a]
        print(f"{a:9s} {e['raw_w']:>3d}W-{e['raw_n'] - e['raw_w']:<4d} "
              f"{e['n_eff']:>6.1f} {e['feed']:>8s} {_f(e['c'], '.4f'):>7s} "
              f"{_f(e['p_raw'], '.4f'):>7s} {_f(e['p_shrunk'], '.4f'):>7s} "
              f"{e['p_wilson']:>7.4f} {_f(e['raw_edge_pp'], '+.2f'):>6s}pp "
              f"{e['f_qtr'] * 100:>6.2f}%  {e['state']}")
    print()
    print("  c = notional-weighted entry price = THE BREAK-EVEN WIN RATE.")
    print("  edge = p_raw - c in percentage points (the shrunk edge drives f_qtr).")
    print("  p_w90 = one-sided 90% Wilson lower bound on the weighted record.")
    print(f"  state: kelly = shrunk edge > 0 | measure = raw edge within "
          f"{NOISE_BAND_PP:.0f}pp of break-even | off = clearly below")
    print()
    print("  per-era window counts (W-L), unweighted:")
    for a in sorted(ev):
        parts = [f"{k} {v[1]}-{v[0] - v[1]}" for k, v in ev[a]["per_era"].items()]
        print(f"    {a:9s} " + "  ".join(parts))
    print()

    print("--- 1b. Sensitivity: how much the era/feed weighting decides --------")
    print("  Same arithmetic on three different slices of the same record.")
    stream_only = arm_evidence([w for w in windows if w["era"] == "stream"])
    # Relabelling every window into the running era is how you get weight 1.0
    # everywhere without a second code path through arm_evidence.
    flat = arm_evidence([dict(w, era="stream") for w in windows])
    print(f"{'arm':9s} | {'weighted (used)':>24s} | {'stream era only':>24s} "
          f"| {'all-time flat':>24s}")
    print(f"{'':9s} | {'n':>5s} {'c':>6s} {'p':>6s} {'edge':>5s} "
          f"| {'n':>5s} {'c':>6s} {'p':>6s} {'edge':>5s} "
          f"| {'n':>5s} {'c':>6s} {'p':>6s} {'edge':>5s}")
    for a in sorted(ev, key=lambda k: -ev[k]["n_eff"]):
        cells = []
        for src, is_flat in ((ev, False), (stream_only, True), (flat, True)):
            e = src.get(a)
            if e is None or e["c"] is None:
                cells.append(f"{'—':>5s} {'—':>6s} {'—':>6s} {'—':>5s}")
                continue
            n = e["raw_n"] if is_flat else e["n_eff"]
            p = (e["raw_w"] / e["raw_n"]) if is_flat else e["p_raw"]
            cells.append(f"{n:>5.1f} {e['c']:>6.3f} {p:>6.3f} "
                         f"{(p - e['c']) * 100:>+5.1f}")
        print(f"{a:9s} | " + " | ".join(cells))
    print("  'all-time flat' ignores eras entirely — it is what the fleet would")
    print("  size on if pre-brake windows counted the same as tonight's.")
    print()

    print("--- 2. Fleet risk budget --------------------------------------------")
    fb = alloc["fleet_bet"]
    print(f"  (a) bottom-up: sum of standalone quarter-Kelly = "
          f"{alloc['sum_f_qtr'] * 100:.2f}% = ${alloc['sum_f_qtr'] * bankroll:,.0f} "
          f"IF the arms were independent")
    print(f"      N={alloc['n_arms']}  rho={rho:.3f}  "
          f"N_eff = N/(1+(N-1)rho) = {alloc['n_eff']:.2f}  "
          f"haircut = {alloc['haircut']:.3f}")
    print(f"      -> ${alloc['bottom_up']:,.0f}")
    print(f"  (b) top-down: the fleet as ONE bet over {fb['n']} overlapping slots "
          f"(n_eff {fb.get('n_eff', 0):.1f})")
    print(f"      p={_f(fb['p'], '.4f')}  g={_f(fb['g'], '+.4f')}  "
          f"l={_f(fb['l'], '.4f')}  worst slot {_f(fb.get('worst'), '+.1%')} "
          f"(${_f(fb.get('worst_usd'), ',.0f')})")
    print(f"      full Kelly {fb.get('f_full', 0) * 100:.2f}%  ->  quarter "
          f"{fb['f_qtr'] * 100:.2f}% = ${fb['f_qtr'] * bankroll:,.0f} "
          f"(point estimate)")
    print(f"      bootstrap p10 (1000 slot resamples): full "
          f"{fb.get('f_full_lo', 0) * 100:.2f}%  ->  quarter "
          f"{fb.get('f_qtr_lo', 0) * 100:.2f}% = "
          f"${fb.get('f_qtr_lo', 0) * bankroll:,.0f}   [uncertainty band, not "
          f"a second multiplier]")
    print()
    print(f"  FLEET BUDGET = min(a, b) = ${alloc['budget']:,.0f}  "
          f"= {alloc['budget'] / bankroll:.1%} of bankroll")
    print(f"  binding constraint: {alloc['binding']}")
    print(f"    of which measurement line  ${alloc['measure_total']:,.0f} "
          f"({len([a for a in alloc['active'] if ev[a]['state'] == 'measure'])} "
          f"arms x {MEASURE_FRAC:.1%} of bankroll)")
    print(f"    of which Kelly line        ${alloc['kelly_budget']:,.0f}")
    print(f"  live un-derived cap for comparison: ${LIVE_FLEET_CAP:,.0f} "
          f"({LIVE_FLEET_CAP / bankroll:.1%})")
    print()
    live_total = sum(LIVE[a][0] for a in desktop if a not in parked)
    fk = alloc["full_kelly_total"]
    print("  WHERE LIVE SIZING SITS ON THAT SCALE")
    print(f"    correlation-adjusted FULL Kelly total   ${fk:,.0f}")
    print(f"    live per-window ceiling                 ${live_total:,.0f} "
          f"= {live_total / fk:.1f}x FULL Kelly")
    slot_opt = alloc["fleet_bet"].get("f_full", 0.0) * bankroll
    print(f"    slot-distribution full Kelly (no shrinkage, no haircut) "
          f"${slot_opt:,.0f}")
    print("    The two optima disagree by ~3.6x, and that gap IS the estimation")
    print("    error: the per-arm view pays for shrinkage and a theoretical rho,")
    print("    the slot view pays for neither. What they do not disagree about is")
    print("    where live sizing sits. Growth per fleet slot, measured on the")
    print("    slot-return distribution itself:")
    for label, stake in (("recommended", alloc["budget"]),
                         ("2x recommended", 2 * alloc["budget"]),
                         ("bottom-up Kelly", fk),
                         ("live fleet cap", LIVE_FLEET_CAP),
                         ("slot-view Kelly", slot_opt),
                         ("live arm sizes", live_total)):
        gr = growth_at(slots, stake / bankroll)
        shown = "RUIN (an observed slot exceeds the account)" if gr is None \
            else f"{gr * 100:+.4f}% per slot"
        print(f"      {label:16s} ${stake:>7,.0f}  ({stake / bankroll:5.1%} of "
              f"bankroll)  {shown}")
    print()

    print("--- 3. Allocation table ----------------------------------------------")
    print(f"{'arm':9s} {'live size':>10s} {'live clip':>10s} {'rec size':>9s} "
          f"{'rec clip':>9s} {'x':>7s}  basis")
    total_live = total_rec = 0.0
    for a in desktop:
        r = alloc["arms"][a]
        ls, lc = LIVE[a]
        if r["state"] == "parked":
            hypo = (f"${r['if_restarted']:.0f} measurement" if r["if_restarted"]
                    else "OFF — edge clearly negative")
            print(f"{a:9s} {'PARKED':>10s} {'—':>10s} {'hold':>9s} {'—':>9s} "
                  f"{'—':>7s}  parked by the operator; if restarted: {hypo} "
                  f"(n_eff {ev.get(a, {}).get('n_eff', 0):.1f})")
            continue
        total_live += ls
        total_rec += r["size"]
        mult = (r["size"] / ls) if ls else 0.0
        print(f"{a:9s} {ls:>10,.0f} {lc:>10,.0f} {r['size']:>9,.0f} "
              f"{r['clip']:>9,.0f} {mult:>6.2f}x  {r['state']}"
              + (f" (n_eff {ev[a]['n_eff']:.1f})" if a in ev else ""))
    for a in sorted(EU_ARMS):
        r = eu["arms"][a]
        ls, lc = LIVE[a]
        print(f"{a:9s} {ls:>10,.0f} {lc:>10,.0f} {r['size']:>9,.0f} "
              f"{r['clip']:>9,.0f} {(r['size'] / ls if ls else 0):>6.2f}x  "
              f"{r['state']}  [EU wallet ${bankroll_eu:,.0f}]")
    print(f"{'TOTAL':9s} {total_live:>10,.0f} {'':>10s} {total_rec:>9,.0f}"
          "   (desktop, un-parked)")
    print(f"  live per-window ceiling  ${total_live:,.0f} = "
          f"{total_live / bankroll:6.1%} of bankroll")
    print(f"  recommended ceiling      ${total_rec:,.0f} = "
          f"{total_rec / bankroll:6.1%} of bankroll")
    print()

    print("--- 4. Correlated-loss evidence (the premise, measured) --------------")
    multi = [s for s in slots if s["losers"] >= 2]
    allw = fleet_slots([dict(w, era="stream") for w in windows])  # unweighted view
    multi_all = [s for s in allw if s["losers"] >= 2]
    for s in multi_all:
        legs = ", ".join(f"{k}:{p:+.0f}" for k, p in s["legs"] if p < 0)
        print(f"  {_ts(s['start'])}  {s['losers']}/{s['n_arms']} arms lost  "
              f"${s['pnl']:>9,.2f} on ${s['notional']:>7,.0f}  {legs}")
    print(f"  {len(multi_all)} multi-arm loss events all-time "
          f"({len(multi)} inside the weighted corpus)")
    hist: dict[int, int] = defaultdict(int)
    for s in allw:
        if s["n_arms"] >= 2:
            hist[s["losers"]] += 1
    print("  slots with >=2 arms, by number of losing arms: "
          + ", ".join(f"{k}->{v}" for k, v in sorted(hist.items())))
    peaks = concurrency_peaks(windows)
    print("  historical peak simultaneous notional: "
          + ", ".join(f"${p['usd']:,.0f}" for p in peaks))
    wipeout = alloc["committed_total"]
    print(f"  at the recommended sizes a 100% correlated wipeout costs "
          f"${wipeout:,.0f} = {wipeout / bankroll:.1%} of bankroll; "
          f"four in a row = {1 - (1 - wipeout / bankroll) ** 4:.1%}")
    print(f"  at LIVE sizes the same event costs ${total_live:,.0f} = "
          f"{total_live / bankroll:.1%} — i.e. more than the account holds")
    print()

    print("--- 5. `pmt crypto arm` commands -------------------------------------")
    for a in desktop:
        r = alloc["arms"][a]
        sym, dur = a.split()
        if r["state"] == "parked":
            print(f"  # {a}: leave parked (operator's A/B), no command")
            continue
        if r["size"] <= 0:
            print(f"  # {a}: OFF — no size makes a negative measured edge positive")
            continue
        print(f"  pmt crypto arm <{sym}-updown-{dur} url> --size {r['size']:.0f} "
              f"--clip {r['clip']:.0f} --feed {ARM_FEED[a]} --theta 0.3")
    print(f"  pmt crypto fleet --cap {alloc['budget']:.0f}")
    for a in sorted(EU_ARMS):
        r = eu["arms"][a]
        sym, dur = a.split()
        if r["size"] <= 0:
            print(f"  # {a} (EU box): OFF")
        else:
            print(f"  # on the EU box: pmt crypto arm <{sym}-updown-{dur} url> "
                  f"--size {r['size']:.0f} --clip {r['clip']:.0f}")
    print()

    return {"evidence": ev, "alloc": alloc, "eu": eu, "slots": len(slots),
            "multi_arm_events": multi_all, "peaks": peaks,
            "bankroll": bankroll, "bankroll_eu": bankroll_eu, "rho": rho,
            "n_windows": len(windows), "live_total": total_live,
            "rec_total": total_rec}


def _selftest() -> None:
    """The identities the report is allowed to rely on."""
    # Break-even win rate IS the entry price.
    for c in (0.80, 0.90, 0.95, 0.99):
        assert abs(kelly(c, (1 - c) / c)) < 1e-12, c
    # l=1 form matches (p-c)/(1-c).
    for c, p in ((0.95, 0.97), (0.90, 0.95), (0.80, 0.90)):
        assert abs(kelly(p, (1 - c) / c) - (p - c) / (1 - c)) < 1e-9
    # Long-only: no negative stakes.
    assert kelly(0.5, 0.05) == 0.0
    # Correlation haircut endpoints.
    assert abs(n_eff_bets(5, 0.0) - 5.0) < 1e-9
    assert abs(n_eff_bets(5, 1.0) - 1.0) < 1e-9
    # Clip rule: brake-ability first, depth cap second, floor last.
    assert clip_for(40) == 10.0
    assert clip_for(400) == CLIP_DEPTH_CAP
    assert clip_for(4) == CLIP_FLOOR
    # Wilson tolerates fractional weight and stays below the point estimate.
    assert 0.0 < wilson_lo(9.5, 10.0) < 0.95


def main() -> None:
    ap = argparse.ArgumentParser(description="Derive arm sizes from the graded record")
    ap.add_argument("--refresh", action="store_true",
                    help="re-walk the wallet and rewrite the frozen snapshot")
    ap.add_argument("--bankroll", type=float, default=BANKROLL)
    ap.add_argument("--bankroll-eu", type=float, default=BANKROLL_EU)
    ap.add_argument("--rho", type=float, default=FLEET_RHO)
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    _selftest()
    windows = load(args.refresh)
    if args.json:
        import contextlib
        import io

        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            out = report(windows, args.bankroll, args.bankroll_eu, args.rho)
        print(json.dumps(out, indent=1, default=str))
    else:
        report(windows, args.bankroll, args.bankroll_eu, args.rho)


if __name__ == "__main__":
    main()
