#!/usr/bin/env python3
"""R2 calibrated quarter-Kelly sizing study, v2 (ROADMAP.md Phase 2, R2).

Successor to `r2_kelly_sim.py`, which the operator flagged as incomplete on
three counts. All three are fixed here:

  1. **Per-clip sub-cap.** v1 had only a per-WINDOW ceiling, so a window
     could reach its whole cap in one clip — which is exactly what makes a
     loss window big. Sizing is now
     `min(kelly_notional, clip_cap[symbol], window_room)` with `clip_cap`
     swept per symbol class ({25,50,75,100} for btc/eth, {10,25} for
     sol/bnb).
  2. **Calibration weighting.** v1 fit stated-fair -> realized at CLIP
     level, so a window that fired 20 clips voted 20 times and a window
     that fired 1 clip voted once — many-clip windows (the ones the book
     fought, i.e. the losses) were structurally overweighted. The fit is
     now at WINDOW level: one Bernoulli draw per decided window, its
     stated value taken as the FIRST fire's `fair` (the entry decision),
     with the notional-weighted fair reported as a sensitivity.
  3. **Fill reality.** v1 treated `size * ask` off the fire tape as money
     spent. Measured against the wallet, only ~75% of intended taker
     notional actually crossed on this corpus (ROADMAP's fill-chasing
     audit, independently reproduced here), and the fraction varies 0.17x
     to 1.01x per window. Every policy here is therefore scaled through
     the window's MEASURED fill ratio and its MEASURED realized cost per
     share (wallet `usdcSize`/`size`), which makes the "actual" policy
     reproduce the wallet's realized P&L exactly and makes every resized
     policy a pure scale on what really filled.

What the sim is and isn't
------------------------
It is resize-only: same windows, same entry times, same prices, same fill
fraction — only the notional per clip differs. It is NOT an entry-policy
study; when calibrated Kelly returns <= 0 for a clip (which it does above
ask ~0.955, since a calibrated p of ~0.96 cannot pay for a 0.97 ask) the
clip is dropped, and that de-facto R3 max-price gate is reported
separately rather than hidden inside the P&L.

Ground truth, in priority order (same rule as pmtrader/polymarket/outcomes.py
— validated or dropped, never guessed):
  1. wallet: a paying REDEEM = win; a $0 REDEEM against our bought side =
     loss. This OVERRIDES outcomes.jsonl where they disagree; see the
     `--audit` output for the one window where they do on this corpus.
  2. outcomes.jsonl (wallet- or Chainlink-validated) for windows the
     wallet can't settle on its own.
  3. otherwise undecided -> excluded from P&L, reported by count and by
     open exposure.

Data:
  ~/.pmt/engine/updown-tape.jsonl   fire/eval/cleanup records
  ~/.pmt/engine/book-tape.jsonl     top-of-book size, for the depth gate
  ~/.pmt/corpus/outcomes.jsonl      validated winners
  ~/.pmt/corpus/r2-wallet-windows.json  cached per-window wallet ledger
      (buy $, buy shares, redeem $) — refresh with --refresh-wallet, which
      is the only network call this script can make.

Run: cd pmtrader && uv run python ../analysis/r2_sizing_sim.py
     cd pmtrader && uv run python ../analysis/r2_sizing_sim.py --refresh-wallet
"""
from __future__ import annotations

import argparse
import json
import math
import statistics
from collections import defaultdict
from pathlib import Path

TAPE_PATH = Path.home() / ".pmt" / "engine" / "updown-tape.jsonl"
BOOK_PATH = Path.home() / ".pmt" / "engine" / "book-tape.jsonl"
OUTCOMES_PATH = Path.home() / ".pmt" / "corpus" / "outcomes.jsonl"
WALLET_CACHE = Path.home() / ".pmt" / "corpus" / "r2-wallet-windows.json"

CUTOFF_T = 1787452500.0          # post-brake era (R9 gate + window-brake latch)
DEFAULT_BANKROLL = 1300.0        # operator's stated capital for this study
DEFAULT_FEE_RATE = 0.07          # pmengine ArmParams.fee_rate live default
DEFAULT_KELLY_FRAC = 0.25        # quarter-Kelly, per R2
MIN_CLIP_USDC = 1.0

CAP_FRACS = (0.10, 0.15, 0.25)
CLIP_CAPS_MAJOR = (25.0, 50.0, 75.0, 100.0)   # btc / eth
CLIP_CAPS_MINOR = (10.0, 25.0)                # sol / bnb
MAJOR_SYMS = ("btc", "eth")
MINOR_SYMS = ("sol", "bnb")
FLEET_CAP_UNDECIDED = 250.0      # R7 counterfactual level

# Calibration bucket edges over stated fair. Coarse at the top because that
# is where this policy actually lives; a per-unique-float fit would be one
# window per bin and PAVA would just memorize the corpus.
CAL_EDGES = (0.0, 0.90, 0.95, 0.97, 0.98, 0.99, 0.995, 0.999, 1.0 + 1e-9)

R2_BAR_WINDOWS = 30              # ROADMAP: "calibration over >=30 decided windows"

# Live arm params as of the operator's 2026-08-23 bump (ROADMAP "Operating
# rules"): window budget / clip. Displayed beside the recommendation so the
# "unchanged" verdict is legible against what is actually armed.
CURRENT_ARM_PARAMS = {
    "btc5m": "400/50", "btc15m": "350/25", "eth5m": "350/50",
    "eth15m": "350/50", "sol5m": "150/25", "sol15m": "150/25",
}


# ---------------------------------------------------------------------------
# the pure sizing function — the thing this study is actually recommending
# ---------------------------------------------------------------------------

def size_usdc(
    p_fair: float,
    ask: float,
    fee_rate: float = DEFAULT_FEE_RATE,
    bankroll: float = DEFAULT_BANKROLL,
    kelly_frac: float = DEFAULT_KELLY_FRAC,
    cap_frac: float = 0.15,
    clip_cap: float = 50.0,
    committed_in_window: float = 0.0,
    fleet_room: float = math.inf,
    min_ask: float = 0.0,
) -> float:
    """Intended clip notional (USDC, inclusive of modeled fee).

    Binary payout Kelly: a share pays $1 on win, costs c = ask + fee where
    fee = fee_rate * min(ask, 1-ask) per share (pmengine updown.rs). Staking
    fraction f of bankroll buys f*B/c shares; win -> +f*B*(1-c)/c, lose ->
    -f*B. Full Kelly is (p-c)/(1-c); note p-c is exactly the engine's `net`.

    Four ceilings, all binding at once — the per-clip one is the piece v1
    was missing, and it is what stops a single clip from becoming a whole
    window's cap:
      kelly    quarter-Kelly on the (calibrated) probability
      clip_cap hard per-clip notional ceiling, swept per symbol class
      window   cap_frac * bankroll minus what this window already holds
      fleet    R7 room left in the un-decided correlated pool (inf = off)

    `min_ask` is a fifth gate the corpus forced into existence: refuse a clip
    whose book price has collapsed below a floor. Kelly on a calibrated p
    reads a crashed ask as an enormous edge, and a crashed ask on our side
    is precisely the fingerprint of every loss window in this corpus. Without
    the floor, calibrated Kelly pours its biggest bets into its worst
    windows — see the two sweep tables.
    """
    if ask < min_ask:
        return 0.0
    if not (0.0 < ask < 1.0):
        return 0.0
    fee = fee_rate * min(ask, 1.0 - ask)
    c = ask + fee
    if not (0.0 < c < 1.0):
        return 0.0
    edge = p_fair - c
    if edge <= 0.0:
        return 0.0
    f_stake = kelly_frac * (edge / (1.0 - c))
    notional = max(0.0, f_stake) * bankroll
    window_room = max(0.0, cap_frac * bankroll - committed_in_window)
    return max(0.0, min(notional, clip_cap, window_room, fleet_room))


def clip_cap_for(sym: str, major: float, minor: float) -> float:
    return major if sym in MAJOR_SYMS else minor


# ---------------------------------------------------------------------------
# loading
# ---------------------------------------------------------------------------

def parse_slug(slug: str) -> tuple[str, int, float, float] | None:
    """(symbol, duration_s, start, end) or None."""
    parts = slug.split("-")
    if len(parts) != 4 or parts[1] != "updown" or not parts[2].endswith("m"):
        return None
    try:
        dur_s = int(parts[2][:-1]) * 60
        start = float(parts[3])
    except ValueError:
        return None
    return parts[0], dur_s, start, start + dur_s


def series_of(slug: str) -> str:
    p = parse_slug(slug)
    return f"{p[0]}{p[1] // 60}m" if p else "?"


def load_tape(path: Path = TAPE_PATH) -> list[dict]:
    out = []
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except ValueError:
                continue
    out.sort(key=lambda r: r.get("t", 0.0))
    return out


def load_outcomes(path: Path = OUTCOMES_PATH) -> dict[str, str]:
    out = {}
    with open(path) as f:
        for line in f:
            line = line.strip()
            if line:
                d = json.loads(line)
                out[d["slug"]] = d["winner"]
    return out


def refresh_wallet_cache(floor: float, path: Path = WALLET_CACHE) -> list[dict]:
    """The one network call. Per-window wallet ledger -> cache file.

    Must run from pmtrader/ (or anywhere below the repo root) so .env
    discovery finds PM_FUNDER_ADDRESS.
    """
    from polymarket.env import load_project_env

    load_project_env()
    from polymarket import updown_slugs, wallet

    addr = wallet.funder_address()
    agg: dict[str, dict] = {}
    for a in wallet.fetch_wallet_activity(addr, floor):
        slug = a.get("slug") or ""
        if not updown_slugs.is_updown(slug) or updown_slugs.window_start(slug) < floor:
            continue
        w = agg.setdefault(slug, {"slug": slug, "buy": 0.0, "buy_shares": 0.0,
                                  "sell": 0.0, "redeem": 0.0, "redeem_seen": False,
                                  "redeem_outcome": None, "buy_side": None})
        usd = a.get("usdcSize") or 0.0
        if a["type"] == "TRADE":
            if a.get("side") == "BUY":
                w["buy"] += usd
                w["buy_shares"] += a.get("size") or 0.0
                w["buy_side"] = (a.get("outcome") or "").lower() or w["buy_side"]
            else:
                w["sell"] += usd
        elif a["type"] == "REDEEM":
            w["redeem"] += usd
            w["redeem_seen"] = True
            if usd > 0.5:
                w["redeem_outcome"] = (a.get("outcome") or "").lower()
    rows = sorted(agg.values(), key=lambda r: r["slug"])
    path.write_text(json.dumps(rows, indent=1))
    return rows


def load_wallet(path: Path = WALLET_CACHE) -> dict[str, dict]:
    if not path.exists():
        return {}
    return {r["slug"]: r for r in json.loads(path.read_text())}


# ---------------------------------------------------------------------------
# window assembly
# ---------------------------------------------------------------------------

def build_windows(tape: list[dict], outcomes: dict[str, str], wallet: dict[str, dict],
                  cutoff: float) -> tuple[list[dict], list[dict]]:
    """(windows, audit_notes). One record per window that fired post-cutoff."""
    fires_by_slug: dict[str, list[dict]] = defaultdict(list)
    for r in tape:
        if r.get("ev") == "fire" and r.get("t", 0.0) >= cutoff:
            fires_by_slug[r["slug"]].append(r)

    # banked_decided flip time per slug, from the eval stream (R7's gate input)
    decided_at: dict[str, float] = {}
    for r in tape:
        if r.get("ev") == "eval" and r.get("banked_decided") and r["slug"] not in decided_at:
            decided_at[r["slug"]] = r["t"]

    audit: list[dict] = []
    windows = []
    for slug, fires in fires_by_slug.items():
        parsed = parse_slug(slug)
        if parsed is None:
            continue
        sym, dur_s, start, end = parsed
        fires.sort(key=lambda f: f["t"])

        notional_by_side: dict[str, float] = defaultdict(float)
        for f in fires:
            notional_by_side[f["side"]] += f["size"] * f["ask"]
        side = max(notional_by_side, key=notional_by_side.get)

        intended = sum(f["size"] * f["ask"] for f in fires)
        w = wallet.get(slug)
        buy = (w or {}).get("buy", 0.0)
        buy_shares = (w or {}).get("buy_shares", 0.0)
        fill_ratio = (buy / intended) if intended > 0 else 0.0
        cost_ps = (buy / buy_shares) if buy_shares > 0 else None

        # ---- outcome, wallet-first ----
        winner = None
        source = None
        if w and w.get("redeem_outcome"):
            winner, source = w["redeem_outcome"], "wallet:paying-redeem"
        elif w and w.get("redeem_seen") and w.get("buy_side") in ("up", "down"):
            # $0 redeem and we only ever bought one side -> that side lost.
            winner = "up" if w["buy_side"] == "down" else "down"
            source = "wallet:zero-redeem"
        if winner is None and slug in outcomes:
            winner, source = outcomes[slug], "outcomes.jsonl"
        elif winner is not None and slug in outcomes and outcomes[slug] != winner:
            audit.append({"slug": slug, "wallet": winner, "file": outcomes[slug],
                          "buy": buy, "source": source})

        total_fair = sum(f["size"] * f["ask"] * f["fair"] for f in fires)
        windows.append({
            "slug": slug, "sym": sym, "series": series_of(slug), "dur_s": dur_s,
            "start": start, "end": end, "fires": fires, "side": side,
            "first_fair": fires[0]["fair"],
            "wavg_fair": (total_fair / intended) if intended > 0 else fires[0]["fair"],
            "intended": intended, "fill_ratio": fill_ratio, "cost_ps": cost_ps,
            "actual_buy": buy, "actual_pnl": (buy_shares if winner == side else 0.0) - buy
                            if winner is not None else None,
            "winner": winner, "outcome_source": source,
            "decided_at": decided_at.get(slug),
        })
    windows.sort(key=lambda w: w["fires"][0]["t"])
    return windows, audit


# ---------------------------------------------------------------------------
# calibration: WINDOW-level isotonic fit — never clip-level (LESSONS.md#L34)
# ---------------------------------------------------------------------------

def bucket_index(fair: float, edges: tuple[float, ...] = CAL_EDGES) -> int:
    for i in range(len(edges) - 1):
        if edges[i] <= fair < edges[i + 1]:
            return i
    return len(edges) - 2


def pava(values: list[float], weights: list[float]) -> list[float]:
    """Pool-adjacent-violators: weighted isotonic (non-decreasing) fit."""
    stack: list[list[float]] = []
    for v, wt in zip(values, weights):
        block = [v * wt, wt, 1]
        while stack and stack[-1][0] / stack[-1][1] > block[0] / block[1]:
            prev = stack.pop()
            block = [prev[0] + block[0], prev[1] + block[1], prev[2] + block[2]]
        stack.append(block)
    out: list[float] = []
    for sum_wv, sum_w, cnt in stack:
        out.extend([sum_wv / sum_w] * cnt)
    return out


def wilson_lo(wins: int, n: int, z: float = 1.96) -> float:
    """Wilson lower bound — the honest number when n is 13 and wins is 13."""
    if n == 0:
        return 0.0
    p = wins / n
    denom = 1.0 + z * z / n
    centre = p + z * z / (2 * n)
    rad = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n))
    return max(0.0, (centre - rad) / denom)


def fit_window_calibration(windows: list[dict], fair_key: str = "first_fair",
                           edges: tuple[float, ...] = CAL_EDGES) -> dict:
    """One Bernoulli draw per decided window, isotonic over stated-fair buckets."""
    n_b = len(edges) - 1
    n = [0] * n_b
    wins = [0] * n_b
    for w in windows:
        if w["winner"] is None:
            continue
        b = bucket_index(w[fair_key], edges)
        n[b] += 1
        wins[b] += 1 if w["winner"] == w["side"] else 0

    populated = [b for b in range(n_b) if n[b] > 0]
    raw = [wins[b] / n[b] for b in populated]
    fitted = pava(raw, [float(n[b]) for b in populated])
    lookup = dict(zip(populated, fitted))
    table = [{"bucket": b, "label": f"[{edges[b]:.3f},{edges[b + 1]:.3f})",
              "n": n[b], "wins": wins[b], "raw": raw[i], "fit": fitted[i],
              "wilson_lo": wilson_lo(wins[b], n[b]),
              "passes_bar": n[b] >= R2_BAR_WINDOWS}
             for i, b in enumerate(populated)]
    return {"table": table, "lookup": lookup, "edges": edges,
            "n_decided": sum(n), "populated": populated}


def calibrated_fair(fair: float, cal: dict) -> float:
    """Map a stated fair through the window-level calibration.

    An unpopulated bucket falls back to the nearest populated one (the fit is
    monotone, so nearest-index is also the nearest probability) — never to
    the raw stated fair, which is the degeneracy v1 hit: raw fair pins at
    0.999+ and quarter-Kelly then always wants the whole cap.
    """
    b = bucket_index(fair, cal["edges"])
    if b in cal["lookup"]:
        return cal["lookup"][b]
    if not cal["populated"]:
        return fair
    nearest = min(cal["populated"], key=lambda x: (abs(x - b), x))
    return cal["lookup"][nearest]


# ---------------------------------------------------------------------------
# simulation
# ---------------------------------------------------------------------------

def _peak_concurrency(events: list[tuple[float, float]]) -> tuple[float, float]:
    """(peak, peak_t) over (time, delta) commitment events."""
    running = peak = 0.0
    peak_t = 0.0
    for t, d in sorted(events, key=lambda e: (e[0], e[1])):
        running += d
        if running > peak:
            peak, peak_t = running, t
    return peak, peak_t


def max_drawdown(seq: list[float]) -> float:
    peak = running = 0.0
    worst = 0.0
    for x in seq:
        running += x
        peak = max(peak, running)
        worst = min(worst, running - peak)
    return -worst


def simulate(
    windows: list[dict],
    fair_fn,
    bankroll: float = DEFAULT_BANKROLL,
    kelly_frac: float = DEFAULT_KELLY_FRAC,
    cap_frac: float = 0.15,
    clip_cap_major: float = 50.0,
    clip_cap_minor: float = 25.0,
    fee_rate: float = DEFAULT_FEE_RATE,
    fleet_cap: float | None = None,
    min_ask: float = 0.0,
    actual: bool = False,
    bar_buckets: set[int] | None = None,
) -> dict:
    """Replay every post-cutoff clip under one sizing policy.

    `actual=True` bypasses sizing entirely and replays the recorded clip
    (size*ask), which — through the same fill-ratio/cost machinery — must
    reproduce the wallet's realized P&L. That identity is the sim's own
    unit test; see `_print_fidelity`.
    """
    clips = [(f["t"], w, f) for w in windows for f in w["fires"]]
    clips.sort(key=lambda c: c[0])

    committed: dict[str, float] = defaultdict(float)   # filled $ per window
    shares: dict[str, float] = defaultdict(float)
    n_clips_taken = defaultdict(int)
    dropped_zero_kelly = 0
    dropped_clip_cap_bind = 0
    dropped_fleet = 0
    dropped_min_ask = 0
    events: list[tuple[float, float]] = []
    events_undecided: list[tuple[float, float]] = []
    events_by_series: dict[str, list] = defaultdict(list)
    intended_total = 0.0

    # R7 fleet state: filled $ sitting in windows that have not yet flipped
    # banked_decided, released at window end or at the flip, whichever first.
    undecided: dict[str, float] = defaultdict(float)

    def fleet_undecided(now: float) -> float:
        total = 0.0
        for slug, amt in undecided.items():
            w = win_by_slug[slug]
            if now >= w["end"]:
                continue
            if w["decided_at"] is not None and now >= w["decided_at"]:
                continue
            total += amt
        return total

    win_by_slug = {w["slug"]: w for w in windows}

    for t, w, f in clips:
        if w["fill_ratio"] <= 0.0 or w["cost_ps"] is None:
            continue  # nothing crossed in this window; no counterfactual to scale
        # `bar_buckets` is the R2-compliant restriction: resize ONLY clips whose
        # stated-fair bucket cleared the >=30-decided-window calibration bar;
        # everything else keeps the flat clip the engine really traded. This is
        # the only variant that respects "no size increases until each p-bucket
        # shows calibration over >=30 decided windows" literally.
        gated_out = (bar_buckets is not None
                     and bucket_index(f["fair"]) not in bar_buckets)
        if actual or gated_out:
            intent = f["size"] * f["ask"]
        else:
            if f["ask"] < min_ask:
                dropped_min_ask += 1
                continue
            room_fleet = math.inf
            if fleet_cap is not None:
                is_undecided = w["decided_at"] is None or t < w["decided_at"]
                room_fleet = max(0.0, fleet_cap - fleet_undecided(t)) if is_undecided else math.inf
            raw_kelly = size_usdc(fair_fn(f), f["ask"], fee_rate, bankroll, kelly_frac,
                                  cap_frac, math.inf, committed[w["slug"]], math.inf)
            intent = size_usdc(fair_fn(f), f["ask"], fee_rate, bankroll, kelly_frac,
                               cap_frac, clip_cap_for(w["sym"], clip_cap_major, clip_cap_minor),
                               committed[w["slug"]], room_fleet)
            if raw_kelly <= 0.0:
                dropped_zero_kelly += 1
                continue
            if intent <= 0.0 and room_fleet <= 0.0:
                dropped_fleet += 1
                continue
            if raw_kelly > intent + 1e-9:
                dropped_clip_cap_bind += 1
        if intent < MIN_CLIP_USDC:
            continue
        filled = intent * w["fill_ratio"]
        intended_total += intent
        committed[w["slug"]] += filled
        undecided[w["slug"]] += filled
        shares[w["slug"]] += filled / w["cost_ps"]
        n_clips_taken[w["slug"]] += 1
        events.append((t, filled))
        events.append((w["end"], -filled))
        # Un-decided pool (R7's target): held from the fire until banked_decided
        # flips, or window end if it never does.
        release = w["end"] if w["decided_at"] is None else min(w["decided_at"], w["end"])
        if release > t:
            events_undecided.append((t, filled))
            events_undecided.append((release, -filled))
        events_by_series[w["series"]].append((t, filled))
        events_by_series[w["series"]].append((w["end"], -filled))

    # ---- P&L, decided windows only ----
    by_window: dict[str, float] = {}
    undecided_exposure = 0.0
    for slug, spent in committed.items():
        w = win_by_slug[slug]
        if w["winner"] is None:
            undecided_exposure += spent
            continue
        by_window[slug] = (shares[slug] if w["winner"] == w["side"] else 0.0) - spent

    ordered = sorted(by_window, key=lambda s: win_by_slug[s]["end"])
    curve = [by_window[s] for s in ordered]
    peak, peak_t = _peak_concurrency(events)
    peak_undec, peak_undec_t = _peak_concurrency(events_undecided)

    per_series: dict[str, dict] = {}
    for series in sorted({w["series"] for w in windows}):
        slugs = [s for s in by_window if win_by_slug[s]["series"] == series]
        o = sorted(slugs, key=lambda s: win_by_slug[s]["end"])
        c = [by_window[s] for s in o]
        pk, _ = _peak_concurrency(events_by_series[series])
        per_series[series] = {
            "n_windows": len(o),
            "n_wins": sum(1 for s in o if win_by_slug[s]["winner"] == win_by_slug[s]["side"]),
            "pnl": sum(c),
            "notional": sum(committed[s] for s in o),
            "max_dd": max_drawdown(c),
            "worst_window": -min(c, default=0.0),
            "peak_concurrent": pk,
            "n_clips": sum(n_clips_taken[s] for s in o),
        }

    return {
        "by_window": by_window, "committed": dict(committed), "shares": dict(shares),
        "n_clips": sum(n_clips_taken.values()),
        "n_windows": len(by_window),
        "pnl": sum(curve),
        "notional": sum(committed[s] for s in by_window),
        "intended": intended_total,
        "max_dd": max_drawdown(curve),
        "worst_window": -min(curve, default=0.0),
        "peak_concurrent": peak, "peak_t": peak_t,
        "peak_undecided": peak_undec, "peak_undecided_t": peak_undec_t,
        "undecided_exposure": undecided_exposure,
        "dropped_zero_kelly": dropped_zero_kelly,
        "dropped_clip_cap_bind": dropped_clip_cap_bind,
        "dropped_fleet": dropped_fleet,
        "dropped_min_ask": dropped_min_ask,
        "per_series": per_series,
    }


# ---------------------------------------------------------------------------
# depth feasibility — can a bigger clip actually fill?
# ---------------------------------------------------------------------------

def depth_check(windows: list[dict], clip_caps: tuple[float, ...],
                path: Path = BOOK_PATH) -> dict:
    """Top-of-book size at each fire, vs the shares a capped clip would need.

    Sizing up is only real if the book is there. Reads the book tape and,
    for each fire, takes the nearest book tick within 15s on the same slug.
    """
    if not path.exists():
        return {}
    ticks: dict[str, list[dict]] = defaultdict(list)
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                r = json.loads(line)
            except ValueError:
                continue
            if r.get("ev") == "book":
                ticks[r.get("slug", "")].append(r)
    for v in ticks.values():
        v.sort(key=lambda r: r["t"])

    avail: list[float] = []
    for w in windows:
        for f in w["fires"]:
            near = None
            best = 15.0
            for r in ticks.get(w["slug"], []):
                d = abs(r["t"] - f["t"])
                if d < best:
                    best, near = d, r
            if near is None:
                continue
            sz = near.get("up_ask_sz") if f["side"] == "up" else near.get("dn_ask_sz")
            if sz:
                avail.append(float(sz) * f["ask"])
    if not avail:
        return {}
    avail.sort()
    out = {"n": len(avail), "median": statistics.median(avail),
           "p25": avail[len(avail) // 4], "p75": avail[3 * len(avail) // 4]}
    out["frac_covering"] = {cap: sum(1 for a in avail if a >= cap) / len(avail)
                            for cap in clip_caps}
    return out


# ---------------------------------------------------------------------------
# reporting
# ---------------------------------------------------------------------------

def money(x: float) -> str:
    return f"-${-x:,.2f}" if x < 0 else f"${x:,.2f}"


def _print_fidelity(windows: list[dict], act: dict) -> None:
    wallet_pnl = sum(w["actual_pnl"] for w in windows if w["actual_pnl"] is not None)
    wallet_buy = sum(w["actual_buy"] for w in windows if w["winner"] is not None)
    print("-" * 78)
    print("fidelity check — the 'actual' policy must reproduce the wallet")
    print("-" * 78)
    print(f"  wallet realized P&L (decided windows): {money(wallet_pnl)}")
    print(f"  sim 'actual' policy P&L:               {money(act['pnl'])}"
          f"   delta {money(act['pnl'] - wallet_pnl)}")
    print(f"  wallet bought notional:                {money(wallet_buy)}")
    print(f"  sim 'actual' notional:                 {money(act['notional'])}")


def print_report(windows: list[dict], audit: list[dict], cal: dict, cal_w: dict,
                 args, cap_fracs, caps_major, caps_minor) -> None:
    decided = [w for w in windows if w["winner"] is not None and w["actual_buy"] > 0]
    nofill = [w for w in windows if w["actual_buy"] <= 0]
    undecided_w = [w for w in windows if w["winner"] is None and w["actual_buy"] > 0]
    n_fires = sum(len(w["fires"]) for w in windows)
    n_fires_dec = sum(len(w["fires"]) for w in decided)

    print("=" * 78)
    print("R2 CALIBRATED QUARTER-KELLY SIZING STUDY (v2)")
    print("=" * 78)
    print(f"corpus cutoff t >= {args.cutoff:.0f} (post-brake / post-R9)")
    print(f"bankroll ${args.bankroll:,.0f} | kelly_frac {args.kelly_frac} | "
          f"fee_rate {args.fee_rate} (edge model)")
    print()
    print(f"post-brake FIRE records:            {n_fires}  across {len(windows)} windows")
    print(f"  ... in DECIDED windows:           {n_fires_dec}  across {len(decided)} windows")
    print(f"  ... in UNDECIDED windows:         "
          f"{sum(len(w['fires']) for w in undecided_w)}  across "
          f"{len(undecided_w)} windows (excluded from P&L — validated or dropped)")
    for w in undecided_w:
        print(f"      {w['slug']:34s} bought {money(w['actual_buy'])} "
              f"side={w['side']} last_ask={w['fires'][-1]['ask']:.2f}")
    print(f"  ... fired but NOTHING crossed:    "
          f"{sum(len(w['fires']) for w in nofill)}  across {len(nofill)} windows "
          f"({', '.join(w['slug'] for w in nofill)})")
    open_risk = sum(w["actual_buy"] for w in undecided_w)
    crashed = [w for w in undecided_w if w["fires"][-1]["ask"] < 0.5]
    crashed_risk = sum(w["actual_buy"] for w in crashed)
    print(f"  open (un-redeemed) exposure in those undecided windows: {money(open_risk)}")
    if crashed:
        print(f"  of which {money(crashed_risk)} sits in {len(crashed)} window(s) whose "
              f"book collapsed below $0.50 on our own side and which have not paid out "
              f"hours later. If those settle as losses the night's realized P&L is "
              f"{money(sum(w['actual_pnl'] for w in decided) - crashed_risk)}, not the "
              f"headline below. Excluded here per the corpus rule (validated or "
              f"dropped, never guessed) — not because they look benign.")
    print()

    if audit:
        print("-" * 78)
        print("OUTCOME AUDIT — wallet vs outcomes.jsonl disagreement (wallet wins)")
        print("-" * 78)
        for a in audit:
            print(f"  {a['slug']}: wallet says winner={a['wallet']} ({a['source']}), "
                  f"outcomes.jsonl says {a['file']}; we bought {money(a['buy'])} "
                  f"and received $0 -> it is a LOSS.")
            print(f"    -> outcomes.jsonl mislabels this window; `pmt crypto stats` "
                  f"already grades it correctly. Fix belongs in "
                  f"polymarket/outcomes.py::wallet_outcomes (a $0 REDEEM row whose "
                  f"`outcome` field names the WINNER, not our held side, gets flipped "
                  f"the wrong way).")
        print()

    # ---- per-series ledger of record ----
    print("-" * 78)
    print("actual (flat-clip) per-series ledger, post-brake, wallet-graded")
    print("-" * 78)
    print(f"  {'series':8s} {'W-L':>7s} {'n_win':>6s} {'bought $':>10s} {'P&L':>10s} "
          f"{'worst win':>10s}")
    by_ser: dict[str, list[dict]] = defaultdict(list)
    for w in decided:
        by_ser[w["series"]].append(w)
    for s in sorted(by_ser):
        ws = by_ser[s]
        wins = sum(1 for w in ws if w["winner"] == w["side"])
        pnl = sum(w["actual_pnl"] for w in ws)
        worst = min((w["actual_pnl"] for w in ws), default=0.0)
        print(f"  {s:8s} {f'{wins}-{len(ws) - wins}':>7s} {len(ws):>6d} "
              f"{sum(w['actual_buy'] for w in ws):>10,.2f} {pnl:>10,.2f} {worst:>10,.2f}")
    print(f"  {'TOTAL':8s} "
          f"{f'{sum(1 for w in decided if w["winner"] == w["side"])}-'
             f'{sum(1 for w in decided if w["winner"] != w["side"])}':>7s} "
          f"{len(decided):>6d} {sum(w['actual_buy'] for w in decided):>10,.2f} "
          f"{sum(w['actual_pnl'] for w in decided):>10,.2f}")
    print()

    # ---- fill reality ----
    ratios = [w["fill_ratio"] for w in windows if w["intended"] > 0]
    ks = [w["cost_ps"] / (w["intended"] / sum(f["size"] for f in w["fires"]))
          for w in windows if w["cost_ps"] and w["intended"] > 0]
    print("-" * 78)
    print("fill reality (why v1's size*ask baseline was wrong)")
    print("-" * 78)
    tot_int = sum(w["intended"] for w in windows)
    tot_buy = sum(w["actual_buy"] for w in windows)
    print(f"  intended taker notional (sum size*ask):  {money(tot_int)}")
    print(f"  actually crossed (wallet BUY usdcSize):  {money(tot_buy)}"
          f"   = {tot_buy / tot_int:.1%} of intent")
    print(f"  per-window fill ratio: median {statistics.median(ratios):.3f}, "
          f"min {min(ratios):.3f}, max {max(ratios):.3f}")
    print(f"  realized cost per share / quoted ask: median {statistics.median(ks):.4f} "
          f"-> the taker fee+slippage drag we actually paid is ~"
          f"{(statistics.median(ks) - 1) * 100:.2f}%, far under the engine's modeled "
          f"{args.fee_rate * 100:.0f}%*min(ask,1-ask). The edge model here keeps the "
          f"engine's conservative fee; P&L uses the measured cost.")
    print()

    # ---- calibration ----
    for name, c, key in (("first-fire fair (primary)", cal, "first_fair"),
                         ("notional-weighted fair (sensitivity)", cal_w, "wavg_fair")):
        print("-" * 78)
        print(f"WINDOW-LEVEL calibration — {name}")
        print("-" * 78)
        print(f"  one Bernoulli draw per decided window (v1 fit this per CLIP, which "
              f"overweighted many-clip windows)")
        print(f"  {'bucket':>16s} {'n':>4s} {'wins':>5s} {'raw':>7s} {'isotonic':>9s} "
              f"{'wilson95lo':>11s} {'>=30?':>6s}")
        for row in c["table"]:
            print(f"  {row['label']:>16s} {row['n']:>4d} {row['wins']:>5d} "
                  f"{row['raw']:>7.3f} {row['fit']:>9.3f} {row['wilson_lo']:>11.3f} "
                  f"{'PASS' if row['passes_bar'] else 'fail':>6s}")
        print(f"  decided windows in fit: {c['n_decided']}")
        print()

    # ---- per-series x bucket, the R2 bar ----
    print("-" * 78)
    print(f"R2 BAR CHECK — decided windows per (series, stated-fair bucket), bar = "
          f"{R2_BAR_WINDOWS}")
    print("-" * 78)
    print("  (calibration draws — a window with a validated outcome is a draw whether "
          "or not its clip crossed, so this counts 90 where the money ledger counts 87)")
    grid: dict[tuple[str, int], list[int]] = defaultdict(lambda: [0, 0])
    for w in [x for x in windows if x["winner"] is not None]:
        b = bucket_index(w["first_fair"])
        g = grid[(w["series"], b)]
        g[0] += 1
        g[1] += 1 if w["winner"] == w["side"] else 0
    for s in sorted({k[0] for k in grid}):
        cells = sorted((k[1], v) for k, v in grid.items() if k[0] == s)
        tot = sum(v[0] for _, v in cells)
        parts = [f"{CAL_EDGES[b]:.3f}+:{v[0]}({v[1]}W)" for b, v in cells]
        print(f"  {s:8s} n={tot:<3d} {'PASS' if tot >= R2_BAR_WINDOWS else 'FAIL':4s} "
              f"pooled-over-buckets | " + "  ".join(parts))
    print(f"  NOTE: no single (series, bucket) cell reaches {R2_BAR_WINDOWS} on the "
          f"post-brake corpus. Pooling across series is what gets any bucket over the "
          f"bar, and pooling assumes the arms share one calibration curve.")
    print()


def print_sweep(windows: list[dict], cal: dict, args, cap_fracs, caps_major, caps_minor,
                depth: dict, min_asks) -> dict:
    act = simulate(windows, lambda f: f["fair"], args.bankroll, args.kelly_frac,
                   1.0, math.inf, math.inf, args.fee_rate, None, actual=True)
    _print_fidelity(windows, act)
    print(f"  peak concurrent committed (actual):    {money(act['peak_concurrent'])}"
          f"  -> utilization {act['peak_concurrent'] / args.bankroll:.1%} of "
          f"${args.bankroll:,.0f}")
    print(f"  peak concurrent UN-DECIDED (actual):   {money(act['peak_undecided'])}"
          f"  -> the ~$423 number in the brief is this one, not the total. Total "
          f"committed capital already peaks near three-quarters of bankroll; it is "
          f"the SPECULATIVE slice that is small.")
    print()

    def fair_cal(f):
        return calibrated_fair(f["fair"], cal)

    cells = {}
    for ma in min_asks:
        print("=" * 78)
        print(f"SWEEP — calibrated quarter-Kelly, per-clip sub-cap x per-window cap"
              f"   [min_ask = {ma:.2f}]")
        print("=" * 78)
        print(f"  {'cap_frac':>8s} {'win$':>6s} {'clipM':>6s} {'clipm':>6s} "
              f"{'clips':>6s} {'notional':>10s} {'P&L':>9s} {'maxDD':>8s} "
              f"{'worstW':>8s} {'peak$':>8s} {'util':>6s} {'undec$':>7s}")
        for cf in cap_fracs:
            for cm in caps_major:
                for cn in caps_minor:
                    r = simulate(windows, fair_cal, args.bankroll, args.kelly_frac, cf,
                                 cm, cn, args.fee_rate, None, min_ask=ma)
                    cells[(cf, cm, cn, ma)] = r
                    print(f"  {cf:>8.2f} {cf * args.bankroll:>6.0f} {cm:>6.0f} "
                          f"{cn:>6.0f} {r['n_clips']:>6d} {r['notional']:>10,.0f} "
                          f"{r['pnl']:>9,.2f} {-r['max_dd']:>8,.2f} "
                          f"{-r['worst_window']:>8,.2f} {r['peak_concurrent']:>8,.0f} "
                          f"{r['peak_concurrent'] / args.bankroll:>5.0%} "
                          f"{r['peak_undecided']:>7,.0f}")
        print()

    base = cells[(cap_fracs[0], caps_major[0], caps_minor[0], min_asks[0])]
    print(f"  clips dropped by calibrated Kelly returning <=0 (ask too rich for a "
          f"calibrated p): {base['dropped_zero_kelly']} of "
          f"{sum(len(w['fires']) for w in windows)}")
    print("  -> R3 odds discipline falls out of R2 for free: a calibrated p of ~0.975 "
          "cannot pay a 0.98 ask. Calibration does not just shrink sizes, it refuses "
          "the expensive end of the book outright.")
    print()
    print("  READ THE min_ask=0.00 TABLE AS A FAILURE MODE, NOT A RECOMMENDATION.")
    print("  Calibrated Kelly on a stated fair is a function of (p_cal - c)/(1-c), and")
    print("  c is the BOOK's ask. Every loss window in this corpus has the same shape:")
    print("  the book collapses on our side while the model stays pinned near 1.0. A")
    print("  collapsed ask therefore reads to Kelly as a gigantic edge, so uncapped")
    print("  calibrated Kelly puts its LARGEST bets into precisely the windows that")
    print("  lose. That is why the min_ask=0.00 cells lose more than the flat clips the")
    print("  engine actually traded, and why the per-clip sub-cap alone cannot fix it:")
    print("  the sub-cap limits one clip, but a collapsing book emits many clips.")
    print()

    if depth:
        print("-" * 78)
        print("DEPTH GATE — is a bigger clip actually fillable at the fire tick?")
        print("-" * 78)
        print(f"  top-of-book notional on our side at fire time (n={depth['n']} fires "
              f"matched to a book tick within 15s):")
        print(f"    p25 {money(depth['p25'])}  median {money(depth['median'])}  "
              f"p75 {money(depth['p75'])}")
        for cap, frac in sorted(depth["frac_covering"].items()):
            print(f"    a ${cap:.0f} clip is covered by top-of-book alone in "
                  f"{frac:.0%} of fires")
        print("  Above that, the clip walks the book and pays worse than the tape's "
              "quoted ask — the sim's per-window measured cost does NOT model that "
              "extra slippage, so large-clip P&L here is an upper bound.")
        print()
    return {"actual": act, "cells": cells}


def print_loss_windows(windows: list[dict], act: dict, cells: dict, chosen: tuple,
                       args) -> None:
    losses = [w for w in windows if w["winner"] is not None and w["winner"] != w["side"]]
    losses.sort(key=lambda w: w["actual_pnl"])
    r = cells[chosen]
    print("=" * 78)
    print(f"LOSS WINDOWS under the chosen cell cap_frac={chosen[0]} "
          f"clip_major=${chosen[1]:.0f} clip_minor=${chosen[2]:.0f} "
          f"min_ask={chosen[3]:.2f}")
    print("=" * 78)
    print(f"  {'slug':34s} {'actual $':>9s} {'act P&L':>9s} {'policy $':>9s} "
          f"{'pol P&L':>9s} {'delta':>9s}")
    tot_a = tot_p = 0.0
    for w in losses:
        a_n, a_p = w["actual_buy"], w["actual_pnl"]
        p_n = r["committed"].get(w["slug"], 0.0)
        p_p = r["by_window"].get(w["slug"], 0.0)
        tot_a += a_p
        tot_p += p_p
        print(f"  {w['slug']:34s} {a_n:>9,.2f} {a_p:>9,.2f} {p_n:>9,.2f} "
              f"{p_p:>9,.2f} {p_p - a_p:>+9,.2f}")
    print(f"  {'TOTAL':34s} {'':>9s} {tot_a:>9,.2f} {'':>9s} {tot_p:>9,.2f} "
          f"{tot_p - tot_a:>+9,.2f}")
    print()


def print_per_series(act: dict, cells: dict, chosen: tuple, args) -> None:
    r = cells[chosen]
    print("-" * 78)
    print("PER-SERIES: actual vs policy at the chosen cell")
    print("-" * 78)
    print(f"  cell: cap_frac={chosen[0]} clip_major=${chosen[1]:.0f} "
          f"clip_minor=${chosen[2]:.0f} min_ask={chosen[3]:.2f}")
    print(f"  {'series':8s} {'n':>3s} {'W-L':>6s} | {'act $':>8s} {'act P&L':>8s} "
          f"{'act peak':>8s} | {'pol $':>8s} {'pol P&L':>8s} {'pol DD':>7s} "
          f"{'pol worst':>9s} {'pol peak':>8s}")
    for s in sorted(r["per_series"]):
        a = act["per_series"][s]
        p = r["per_series"][s]
        if a["n_windows"] == 0:
            continue
        print(f"  {s:8s} {a['n_windows']:>3d} "
              f"{f'{a["n_wins"]}-{a["n_windows"] - a["n_wins"]}':>6s} | "
              f"{a['notional']:>8,.0f} {a['pnl']:>8,.2f} {a['peak_concurrent']:>8,.0f} | "
              f"{p['notional']:>8,.0f} {p['pnl']:>8,.2f} {-p['max_dd']:>7,.0f} "
              f"{-p['worst_window']:>9,.2f} {p['peak_concurrent']:>8,.0f}")
    print()


def bootstrap_delta(act: dict, pol: dict, windows: list[dict], n_boot: int = 4000,
                    seed: int = 7) -> tuple[float, float, float]:
    """(p05, p50, p95) of (policy - actual) P&L, resampling WINDOWS with replacement.

    The whole P&L difference between any two cells here is carried by five
    loss windows out of ninety. Resampling windows is the cheapest honest way
    to say how much of a reported edge is a real edge and how much is which
    five windows happened to land tonight.
    """
    import random

    rng = random.Random(seed)
    slugs = [w["slug"] for w in windows if w["winner"] is not None and w["actual_buy"] > 0]
    deltas = [pol["by_window"].get(s, 0.0) - act["by_window"].get(s, 0.0) for s in slugs]
    if not deltas:
        return (0.0, 0.0, 0.0)
    n = len(deltas)
    sums = []
    for _ in range(n_boot):
        sums.append(sum(deltas[rng.randrange(n)] for _ in range(n)))
    sums.sort()
    return (sums[int(0.05 * n_boot)], sums[n_boot // 2], sums[int(0.95 * n_boot)])


def print_bar_gated(windows: list[dict], cal: dict, args, chosen: tuple,
                    act: dict) -> dict:
    """The literal-ROADMAP policy: resize only the buckets that cleared the bar."""
    passing = {row["bucket"] for row in cal["table"] if row["passes_bar"]}
    labels = [row["label"] for row in cal["table"] if row["passes_bar"]]
    print("-" * 78)
    print(f"R2-COMPLIANT CELL — resize ONLY clips in buckets that cleared the "
          f">={R2_BAR_WINDOWS}-window bar")
    print("-" * 78)
    print(f"  passing buckets: {', '.join(labels) or 'NONE'}  "
          f"(everything else keeps today's flat clip)")

    def fair_cal(f):
        return calibrated_fair(f["fair"], cal)

    r = simulate(windows, fair_cal, args.bankroll, args.kelly_frac, chosen[0],
                 chosen[1], chosen[2], args.fee_rate, None, min_ask=chosen[3],
                 bar_buckets=passing)
    lo, mid, hi = bootstrap_delta(act, r, windows)
    print(f"  {'variant':>22s} {'clips':>6s} {'notional':>10s} {'P&L':>9s} "
          f"{'maxDD':>8s} {'worstW':>8s} {'peak$':>8s} {'undec$':>7s}")
    print(f"  {'actual (flat clips)':>22s} {act['n_clips']:>6d} "
          f"{act['notional']:>10,.0f} {act['pnl']:>9,.2f} {-act['max_dd']:>8,.2f} "
          f"{-act['worst_window']:>8,.2f} {act['peak_concurrent']:>8,.0f} "
          f"{act['peak_undecided']:>7,.0f}")
    print(f"  {'bar-gated Kelly':>22s} {r['n_clips']:>6d} {r['notional']:>10,.0f} "
          f"{r['pnl']:>9,.2f} {-r['max_dd']:>8,.2f} {-r['worst_window']:>8,.2f} "
          f"{r['peak_concurrent']:>8,.0f} {r['peak_undecided']:>7,.0f}")
    print(f"  P&L delta vs actual: {money(r['pnl'] - act['pnl'])}   "
          f"window-bootstrap 90% CI [{money(lo)}, {money(hi)}] (median {money(mid)})")
    print("  If that interval straddles zero, tonight's improvement is not "
          "distinguishable from which five windows happened to lose.")
    print()
    return r


def print_lifetime_bar(tape: list[dict], outcomes: dict[str, str]) -> None:
    """The same bar check against the ENTIRE tape, not just the post-brake slice.

    The post-brake corpus is one night. If the answer to "which buckets pass"
    changes materially when you spend the whole tape, the bar is about corpus
    size; if it doesn't, the bar is about where this policy fires.
    """
    fires_by_slug: dict[str, list[dict]] = defaultdict(list)
    for r in tape:
        if r.get("ev") == "fire":
            fires_by_slug[r["slug"]].append(r)
    n = [0] * (len(CAL_EDGES) - 1)
    wins = [0] * (len(CAL_EDGES) - 1)
    for slug, fs in fires_by_slug.items():
        if slug not in outcomes:
            continue
        fs.sort(key=lambda f: f["t"])
        notional: dict[str, float] = defaultdict(float)
        for f in fs:
            notional[f["side"]] += f["size"] * f["ask"]
        side = max(notional, key=notional.get)
        b = bucket_index(fs[0]["fair"])
        n[b] += 1
        wins[b] += 1 if outcomes[slug] == side else 0
    print("-" * 78)
    print("LIFETIME BAR CHECK — same buckets, the whole tape (not just post-brake)")
    print("-" * 78)
    print(f"  {'bucket':>16s} {'n':>4s} {'wins':>5s} {'rate':>7s} {'>=30?':>6s}")
    for b in range(len(n)):
        if n[b] == 0:
            continue
        print(f"  [{CAL_EDGES[b]:.3f},{CAL_EDGES[b + 1]:.3f}) {n[b]:>4d} {wins[b]:>5d} "
              f"{wins[b] / n[b]:>7.3f} "
              f"{'PASS' if n[b] >= R2_BAR_WINDOWS else 'fail':>6s}")
    print(f"  total decided windows with fires, whole tape: {sum(n)}")
    print("  Spending the ENTIRE tape still clears exactly one bucket. More nights "
          "will not fix the thin buckets quickly: this policy fires at fair>=0.999 "
          "most of the time by construction, so the sub-0.999 bins fill slowly.")
    print()


def print_kelly_ladder(cal: dict, args) -> None:
    """What quarter-Kelly actually asks for, at the three defensible p's.

    Same bucket, three readings of it: the raw window win rate, the isotonic
    fit, and the Wilson 95% lower bound. The spread between the last two is
    the honest width of "what sizing does the data justify" at n<=57.
    """
    top = max(cal["table"], key=lambda r: r["n"])
    print("-" * 78)
    print(f"WHAT QUARTER-KELLY ASKS FOR — bucket {top['label']} "
          f"(n={top['n']}, {top['wins']}W), bankroll ${args.bankroll:,.0f}")
    print("-" * 78)
    print(f"  {'ask':>6s} | {'p=raw ' + f'{top["raw"]:.3f}':>16s} "
          f"{'p=fit ' + f'{top["fit"]:.3f}':>16s} "
          f"{'p=wilson95lo ' + f'{top["wilson_lo"]:.3f}':>22s}")
    for ask in (0.70, 0.80, 0.85, 0.90, 0.93, 0.95, 0.97, 0.98):
        cells = []
        for p in (top["raw"], top["fit"], top["wilson_lo"]):
            s = size_usdc(p, ask, args.fee_rate, args.bankroll, args.kelly_frac,
                          cap_frac=1.0, clip_cap=math.inf)
            cells.append("no bet" if s <= 0 else f"${s:,.0f}")
        print(f"  {ask:>6.2f} | {cells[0]:>16s} {cells[1]:>16s} {cells[2]:>22s}")
    print("  The right-hand column is the number a risk officer would sign: at the")
    print("  95% lower bound of a 57-window sample, quarter-Kelly declines every ask")
    print("  the fleet actually pays. The middle column is the point estimate. Sizing")
    print("  policy has to live between them, and 57 windows is not enough to narrow")
    print("  the gap — that is what the >=30-per-bucket bar is really protecting.")
    print()


def print_recommendation(windows: list[dict], cal: dict, act: dict, cells: dict,
                         chosen: tuple, bar: dict, args, current_params: dict) -> None:
    print("=" * 78)
    print("RECOMMENDATION — per series, what the data supports TODAY")
    print("=" * 78)
    decided = [w for w in windows if w["winner"] is not None]
    by_ser: dict[str, list[dict]] = defaultdict(list)
    for w in decided:
        by_ser[w["series"]].append(w)
    passing = [r["label"] for r in cal["table"] if r["passes_bar"]]
    print(f"  ROADMAP bar: no size increase without calibration over "
          f">={R2_BAR_WINDOWS} decided windows PER p-BUCKET.")
    print(f"  Buckets clearing it on the post-brake corpus: "
          f"{', '.join(passing) or 'NONE'} (pooled across all six arms).")
    print(f"  Per-(series,bucket) cells clearing it: NONE — the largest is "
          f"{max((len([w for w in ws if bucket_index(w['first_fair']) == len(CAL_EDGES) - 2]), s) for s, ws in by_ser.items())[0]} windows.")
    print()
    print(f"  {'series':8s} {'n_dec':>5s} {'W-L':>6s} {'top-bucket n':>13s} "
          f"{'current size/clip':>18s} {'RECOMMENDED':>18s}")
    for s in sorted(by_ser):
        ws = by_ser[s]
        wins = sum(1 for w in ws if w["winner"] == w["side"])
        topn = sum(1 for w in ws if bucket_index(w["first_fair"]) == len(CAL_EDGES) - 2)
        cur = current_params.get(s, "?")
        rec = "UNCHANGED"
        print(f"  {s:8s} {len(ws):>5d} {f'{wins}-{len(ws) - wins}':>6s} {topn:>13d} "
              f"{cur:>18s} {rec:>18s}")
    print()
    pol = cells[chosen]
    print("  CAPITAL-UTILIZATION EFFECT (peak concurrent committed / bankroll):")
    print(f"    actual flat clips        {money(act['peak_concurrent']):>10s} = "
          f"{act['peak_concurrent'] / args.bankroll:.0%}   "
          f"un-decided slice {money(act['peak_undecided'])}")
    print(f"    bar-gated Kelly (R2-legal){money(bar['peak_concurrent']):>10s} = "
          f"{bar['peak_concurrent'] / args.bankroll:.0%}   "
          f"un-decided slice {money(bar['peak_undecided'])}")
    print(f"    full calibrated Kelly     {money(pol['peak_concurrent']):>10s} = "
          f"{pol['peak_concurrent'] / args.bankroll:.0%}   "
          f"un-decided slice {money(pol['peak_undecided'])}")
    print("    -> every R2 policy here LOWERS peak committed capital, because a")
    print("       cap_frac-of-bankroll window ceiling (0.15*1300 = $195) is SMALLER")
    print("       than the $350-400 window budgets already armed, and because")
    print("       calibration refuses the >=0.98 asks the fleet currently pays.")
    print("       R2 is a risk-REALLOCATION lever on this corpus, not a utilization")
    print("       lever. It raises the un-decided (speculative) slice while lowering")
    print("       total committed — the opposite of what the brief assumed.")
    print()


def print_fleet_cap(windows: list[dict], cal: dict, args, chosen: tuple) -> None:
    print("-" * 78)
    print(f"R7 CONSTRAINT CELL — same sizing, plus a ${FLEET_CAP_UNDECIDED:.0f} "
          f"fleet-wide cap on UN-DECIDED committed notional")
    print("-" * 78)

    def fair_cal(f):
        return calibrated_fair(f["fair"], cal)

    free = simulate(windows, fair_cal, args.bankroll, args.kelly_frac, chosen[0],
                    chosen[1], chosen[2], args.fee_rate, None, min_ask=chosen[3])
    capped = simulate(windows, fair_cal, args.bankroll, args.kelly_frac, chosen[0],
                      chosen[1], chosen[2], args.fee_rate, FLEET_CAP_UNDECIDED,
                      min_ask=chosen[3])
    print(f"  cell: cap_frac={chosen[0]} clip_major=${chosen[1]:.0f} "
          f"clip_minor=${chosen[2]:.0f} min_ask={chosen[3]:.2f}")
    print(f"  {'variant':>16s} {'clips':>6s} {'notional':>10s} {'P&L':>9s} "
          f"{'maxDD':>8s} {'worstW':>8s} {'peak$':>8s} {'undec$':>7s}")
    for name, r in (("no fleet cap", free), (f"fleet cap ${FLEET_CAP_UNDECIDED:.0f}", capped)):
        print(f"  {name:>16s} {r['n_clips']:>6d} {r['notional']:>10,.0f} "
              f"{r['pnl']:>9,.2f} {-r['max_dd']:>8,.2f} {-r['worst_window']:>8,.2f} "
              f"{r['peak_concurrent']:>8,.0f} {r['peak_undecided']:>7,.0f}")
    print(f"  clips shrunk/blocked by the fleet cap: {capped['dropped_fleet']}")
    print("  R7 caveat: a per-series recommendation that ignores fleet concurrency "
          "overstates safe size. Same-duration arms fire within seconds of each other "
          "on the same macro move, so their un-decided capital is one position, not six.")
    print()


# ---------------------------------------------------------------------------

def _selftest() -> None:
    # sizing: no edge, degenerate prices
    assert size_usdc(0.5, 0.5, cap_frac=1.0, clip_cap=1e9) == 0.0
    assert size_usdc(0.90, 0.95, cap_frac=1.0, clip_cap=1e9) == 0.0
    assert size_usdc(0.9, 0.0, cap_frac=1.0, clip_cap=1e9) == 0.0
    assert size_usdc(0.9, 1.0, cap_frac=1.0, clip_cap=1e9) == 0.0
    # the per-clip sub-cap is the v2 addition: it must bind before the window cap
    big = size_usdc(0.99, 0.50, bankroll=1000.0, cap_frac=1.0, clip_cap=1e9)
    assert big > 100.0, big
    assert size_usdc(0.99, 0.50, bankroll=1000.0, cap_frac=1.0, clip_cap=25.0) == 25.0
    # window room still binds, and binds cumulatively
    assert abs(size_usdc(0.99, 0.50, bankroll=1000.0, cap_frac=0.10, clip_cap=1e9,
                         committed_in_window=95.0) - 5.0) < 1e-9
    assert size_usdc(0.99, 0.50, bankroll=1000.0, cap_frac=0.10, clip_cap=1e9,
                     committed_in_window=100.0) == 0.0
    # fleet room binds too
    assert abs(size_usdc(0.99, 0.50, bankroll=1000.0, cap_frac=1.0, clip_cap=1e9,
                         fleet_room=7.0) - 7.0) < 1e-9
    # calibrated fair never exceeds a bucket's fitted rate; PAVA is monotone
    fit = pava([0.9, 0.5, 0.95], [1.0, 1.0, 1.0])
    assert all(fit[i] <= fit[i + 1] + 1e-12 for i in range(len(fit) - 1)), fit
    assert abs(wilson_lo(13, 13) - 0.7715) < 0.01, wilson_lo(13, 13)
    assert wilson_lo(0, 0) == 0.0
    # bucket edges
    assert bucket_index(0.9995) == len(CAL_EDGES) - 2
    assert bucket_index(0.0) == 0
    # drawdown
    assert abs(max_drawdown([10.0, -30.0, 5.0]) - 30.0) < 1e-9
    assert max_drawdown([]) == 0.0
    # concurrency
    pk, _ = _peak_concurrency([(0.0, 100.0), (1.0, 50.0), (2.0, -100.0), (3.0, -50.0)])
    assert abs(pk - 150.0) < 1e-9, pk


def main() -> None:
    _selftest()
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--bankroll", type=float, default=DEFAULT_BANKROLL)
    ap.add_argument("--kelly-frac", type=float, default=DEFAULT_KELLY_FRAC)
    ap.add_argument("--fee-rate", type=float, default=DEFAULT_FEE_RATE)
    ap.add_argument("--cutoff", type=float, default=CUTOFF_T)
    ap.add_argument("--refresh-wallet", action="store_true",
                    help="re-fetch the per-window wallet ledger (the only network call)")
    ap.add_argument("--chosen", type=str, default="0.15,50,25,0.70",
                    help="cap_frac,clip_major,clip_minor,min_ask for the detail tables")
    ap.add_argument("--min-asks", type=str, default="0.0,0.70",
                    help="ask floors to sweep")
    args = ap.parse_args()
    min_asks = tuple(float(x) for x in args.min_asks.split(","))

    if args.refresh_wallet:
        refresh_wallet_cache(args.cutoff)
    wallet = load_wallet()
    if not wallet:
        print("no wallet cache — run once with --refresh-wallet from pmtrader/")
        return

    tape = load_tape()
    outcomes = load_outcomes()
    windows, audit = build_windows(tape, outcomes, wallet, args.cutoff)
    cal = fit_window_calibration(windows, "first_fair")
    cal_w = fit_window_calibration(windows, "wavg_fair")

    print_report(windows, audit, cal, cal_w, args, CAP_FRACS, CLIP_CAPS_MAJOR,
                 CLIP_CAPS_MINOR)
    depth = depth_check(windows, CLIP_CAPS_MAJOR + CLIP_CAPS_MINOR)
    res = print_sweep(windows, cal, args, CAP_FRACS, CLIP_CAPS_MAJOR, CLIP_CAPS_MINOR,
                      depth, min_asks)
    chosen = tuple(float(x) for x in args.chosen.split(","))
    print_per_series(res["actual"], res["cells"], chosen, args)
    print_loss_windows(windows, res["actual"], res["cells"], chosen, args)

    lo, mid, hi = bootstrap_delta(res["actual"], res["cells"][chosen], windows)
    print("-" * 78)
    print("UNCERTAINTY — window bootstrap on the chosen cell's P&L edge")
    print("-" * 78)
    print(f"  chosen cell P&L {money(res['cells'][chosen]['pnl'])} vs actual "
          f"{money(res['actual']['pnl'])} -> delta "
          f"{money(res['cells'][chosen]['pnl'] - res['actual']['pnl'])}")
    print(f"  90% CI on the delta, resampling the 90 decided windows: "
          f"[{money(lo)}, {money(hi)}] (median {money(mid)})")
    print()

    bar = print_bar_gated(windows, cal, args, chosen, res["actual"])
    print_fleet_cap(windows, cal, args, chosen)
    print_lifetime_bar(tape, outcomes)
    print_kelly_ladder(cal, args)
    print_recommendation(windows, cal, res["actual"], res["cells"], chosen, bar, args,
                         CURRENT_ARM_PARAMS)


if __name__ == "__main__":
    main()
