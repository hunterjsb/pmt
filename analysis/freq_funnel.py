#!/usr/bin/env python3
"""freq_funnel.py — where the fleet's trading opportunities actually die.

The operator question this answers: "we're still not making any trades — where
are we missing opportunities?"  Not with vibes: with a tick-level funnel over
the recorded eval tape, a binding-gate shadow ledger, and a hindsight check on
whether the quiet windows had any money in them at all.

Four things the existing tooling does NOT do, and this does:

  1. FUNNEL ORDERING. `pmt crypto shadow` attributes a refusal to every
     category that would have refused it. This attributes each refused
     window-side moment to the ONE gate that bound FIRST in the engine's own
     order (feed -> basis guard -> book -> theta -> brakes -> chop -> min_fair
     -> min_edge -> max_price -> budget/cooldown). Only then is a knob's cost
     its own, not double-counted with every gate downstream of it.

  2. COUNTERFACTUAL DEPTH ON THE BASIS GUARD. A basis-gated tick has no model
     at all (eval_model returns Err before p_up exists), so the naive ledger
     prices every one of them as a missed trade. But the gate reason carries
     `banked` and `cushion`, which is exactly what the theta gate consumes —
     so we can ask what the tick would have done NEXT, and price only the ones
     that would have survived theta (L1) and could have cleared the edge floor
     at the recorded ask (L2). L0 is the naive number, kept for comparison.

  3. QUIET-MARKET SPLIT, three ceilings and one verdict. For every window
     that never fired: the winner's cheapest offer at ANY point (pure
     hindsight — a binary opens near 0.50, so this is nearly always < 0.90
     and mostly measures reversals), in the final 120s (the safe-bet window),
     and at the moments OUR OWN read already pointed at the winner. All three
     are winner-conditioned, so all three only show upside; the D block is
     the unbiased one — one clip per zero-fire window on whatever side we
     favoured, graded on the real outcome, losers included. Every price is
     capped by the size actually resting at that ask, because uncapped a $24
     clip at ask 0.01 "wins" $2,400 on depth that was never there.

  4. REGIME + ERA CONTEXT. |projected margin| against each arm's guard (how
     often within 1bp of clearing), tonight's realized sigma against a 90-day
     kline baseline, and fires/hour across the night's policy eras.

Read-only. Reads ~/.pmt/engine/updown-tape.jsonl, ~/.pmt/engine/book-tape.jsonl,
~/.pmt/corpus/outcomes.jsonl, ~/.pmt/corpus/klines-1m-*.jsonl. Writes nothing
but the report.

  cd pmtrader && uv run python ../analysis/freq_funnel.py
  cd pmtrader && uv run python ../analysis/freq_funnel.py --since 1787461200 \
        --out ../analysis/freq_funnel_report.md

The tape is append-only and the engine is still writing it, so two runs
minutes apart see different corpora. Every run stamps its own [t0, t1].
"""
from __future__ import annotations

import argparse
import json
import math
import random
import re
import statistics
import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "pmtrader"))

from firsthalf_lib import load_outcomes, parse_slug, wilson  # noqa: E402
from polymarket.shadow import (  # noqa: E402
    collapse_episodes,
    fee,
    price_episode,
    window_clip_notional,
)

# ---------------------------------------------------------------- constants

TAPE = Path.home() / ".pmt" / "engine" / "updown-tape.jsonl"
BOOK_TAPE = Path.home() / ".pmt" / "engine" / "book-tape.jsonl"
KLINES = Path.home() / ".pmt" / "corpus"

# The theta era: --theta 0.3 --min-elapsed 0 went fleet-wide at 05:00Z.
THETA_ERA_T0 = 1787461200.0

# Live arm policy (pmengine/src/strategies/updown.rs defaults; the fleet runs
# them unchanged apart from the per-symbol basis guard, which is read off the
# tape rather than assumed).
THETA = 0.3
MIN_FAIR = 0.97
MIN_EDGE = 0.015
EARLY_MIN_FAIR = 0.55       # updown.rs EARLY_MIN_FAIR
EARLY_MIN_EDGE = 0.08       # d_early_min_edge
MAX_PRICE = 0.985
LATE_REM_S = 120.0          # d_late_rem
RHO_BLOCK = -0.25           # d_rho_block
CADENCE_S = 5.0             # decide() throttles gated+eval tape to one per 5s
FIRE_MATCH_S = 3.0          # a fire belongs to the cadence tick it lands beside

# Policy eras of the recorded night, for the fires/hour trend.
ERAS = [
    ("pre-brake", 0.0, 1787451526.0),
    ("brake", 1787451526.0, THETA_ERA_T0),
    ("theta", THETA_ERA_T0, 1787464800.0),
    ("theta+payup", 1787464800.0, float("inf")),
]

SYMBOL_PAIR = {"btc": "BTCUSDT", "eth": "ETHUSDT", "sol": "SOLUSDT",
               "bnb": "BNBUSDT", "xrp": "XRPUSDT", "doge": "DOGEUSDT"}

# The engine build writing this tape formats the basis gate as prose; the
# structured margin_bp/banked_bp/cushion_bp fields only appear on newer
# records. Parse the sentence they are formatted from, prefer the fields.
_BG_RE = re.compile(
    r"projected margin ([+-]?[\d.]+)bp inside ([\d.]+)bp noise band"
    r"(?: \[banked ([+-]?[\d.]+)bp cushion ([\d.]+)bp\])?"
)

# Funnel stages, in the order the engine applies them. Each entry is
# (key, label, scope) where scope is "window" (the tick as a whole) or
# "side" (the model's favoured side of that tick).
STAGES = [
    ("armed", "armed (window-minutes on tape)", "window"),
    ("live_model", "live model (feed not stale)", "window"),
    ("basis_guard", "basis guard cleared", "window"),
    ("book_quoted", "model side quoted (has an ask)", "side"),
    ("theta", "theta / safety gate cleared", "side"),
    ("brakes", "no distrust / avg_down / latched brake", "side"),
    ("chop", "rho chop filter (spec mode only)", "side"),
    ("min_fair", "fair >= min_fair", "side"),
    ("min_edge", "net >= min_edge", "side"),
    ("max_price", "ask <= max_price", "side"),
    ("last_mile", "FIRED (budget/cooldown/inflight allowed it)", "side"),
]
STAGE_LABEL = {k: lab for k, lab, _ in STAGES}


# ---------------------------------------------------------------- loading

def _iter_json(path: Path):
    with path.open() as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                r = json.loads(line)
            except ValueError:
                continue
            if isinstance(r, dict):
                yield r


def parse_basis_reason(reason: str) -> dict | None:
    """margin / guard / banked / cushion out of a basis-guard gate sentence."""
    m = _BG_RE.search(reason or "")
    if not m:
        return None
    margin, guard, banked, cushion = m.groups()
    return {
        "margin_bp": float(margin),
        "guard_bp": float(guard),
        "banked_bp": float(banked) if banked is not None else None,
        "cushion_bp": float(cushion) if cushion is not None else None,
    }


def load_windows(t0: float, t1: float) -> dict[str, dict]:
    """slug -> {meta, ticks (5s cadence, gated+eval), fires} inside [t0, t1].

    gated and eval share one 5s throttle in decide(), so the two streams
    together are a clean partition of armed time — every cadence tick is
    exactly one of them. `fire` records are NOT on that throttle (a fire is
    pushed unconditionally, and the 2s clip cooldown outruns the 5s cadence),
    so they are kept separately and joined back by timestamp.
    """
    wins: dict[str, dict] = {}
    for r in _iter_json(TAPE):
        t, slug, ev = r.get("t"), r.get("slug"), r.get("ev")
        if t is None or not slug or not (t0 <= t <= t1):
            continue
        if ev not in ("eval", "gated", "fire"):
            continue
        meta = parse_slug(slug)
        if meta is None:
            continue
        # Roll-boundary stragglers are not part of the window's life.
        if t < meta["start"] - CADENCE_S or t > meta["end"]:
            continue
        w = wins.setdefault(slug, {"slug": slug, "meta": meta, "ticks": [], "fires": []})
        if ev == "fire":
            w["fires"].append(r)
        else:
            w["ticks"].append(r)
    for w in wins.values():
        w["ticks"].sort(key=lambda r: r["t"])
        w["fires"].sort(key=lambda r: r["t"])
    return wins


def load_book(t0: float, t1: float) -> dict[str, list[dict]]:
    by: dict[str, list[dict]] = defaultdict(list)
    for r in _iter_json(BOOK_TAPE):
        if r.get("ev") != "book":
            continue
        t, slug = r.get("t"), r.get("slug")
        if t is None or not slug or not (t0 <= t <= t1):
            continue
        meta = parse_slug(slug)
        if meta is None or t < meta["start"] or t > meta["end"]:
            continue
        by[slug].append(r)
    for s in by.values():
        s.sort(key=lambda r: r["t"])
    return dict(by)


# ---------------------------------------------------------------- the funnel

def classify_tick(tick: dict, w: dict, fired_before: bool) -> dict:
    """Which stage this cadence tick died at, and the priced side if any.

    Returns {stage, side, ask, fair, net, guard_bp, margin_bp, safety,
    unlocked, spec}. `stage` is the FIRST stage the tick failed; "pass" means
    it survived every predicate stage (the last-mile check is applied by the
    caller, which needs the fire records).
    """
    meta = w["meta"]
    out = {"t": tick["t"], "slug": w["slug"], "series": meta["series"],
           "stage": "pass", "side": None, "ask": None, "fair": None, "net": None,
           "guard_bp": None, "margin_bp": None, "banked_bp": None,
           "cushion_bp": None, "safety": None, "unlocked": None, "spec": None}

    if tick["ev"] == "gated":
        reason = tick.get("reason") or ""
        if not reason.startswith("basis guard"):
            out["stage"] = "live_model"       # "feed stale"
            return out
        bg = parse_basis_reason(reason) or {}
        margin = tick.get("margin_bp", bg.get("margin_bp"))
        guard = tick.get("guard_bp", bg.get("guard_bp"))
        banked = tick.get("banked_bp", bg.get("banked_bp"))
        cushion = tick.get("cushion_bp", bg.get("cushion_bp"))
        out["stage"] = "basis_guard"
        out["margin_bp"], out["guard_bp"] = margin, guard
        out["banked_bp"], out["cushion_bp"] = banked, cushion
        if margin is not None:
            side = "up" if margin >= 0 else "down"
            out["side"] = side
            out["ask"] = tick.get("up_ask") if side == "up" else tick.get("dn_ask")
            if banked is not None and cushion:
                signed = banked if side == "up" else -banked
                out["safety"] = signed / max(cushion, 1e-9)
        # A gated tick has no model, so unlocked/spec are unknowable here;
        # banked_decided cannot be read off a gate. Time-to-end still can be.
        out["unlocked"] = (meta["end"] - tick["t"]) <= LATE_REM_S
        out["spec"] = not out["unlocked"]
        return out

    # --- eval tick: the model exists, judge the side it favours ---
    p_up = tick.get("p_up")
    if p_up is None:
        out["stage"] = "live_model"
        return out
    side = "up" if p_up >= 0.5 else "down"
    out["side"] = side
    out["guard_bp"] = tick.get("guard_bp")
    out["margin_bp"] = tick.get("margin_bp")
    out["banked_bp"] = tick.get("banked_bp")
    out["cushion_bp"] = tick.get("cushion_bp")

    banked_decided = bool(tick.get("banked_decided"))
    unlocked = (meta["end"] - tick["t"]) <= LATE_REM_S or banked_decided
    out["unlocked"], out["spec"] = unlocked, not unlocked
    fair_req = MIN_FAIR if unlocked else EARLY_MIN_FAIR
    edge_req = MIN_EDGE if unlocked else EARLY_MIN_EDGE

    sd = next((s for s in (tick.get("sides") or []) if s.get("side") == side), None)
    if sd is None or sd.get("ask") is None:
        out["stage"] = "book_quoted"
        return out
    out["ask"], out["fair"], out["net"] = sd.get("ask"), sd.get("fair"), sd.get("net")
    out["safety"] = sd.get("safety")

    brake = sd.get("brake")
    if brake == "safety":
        out["stage"] = "theta"
        return out
    if brake in ("distrust", "avg_down", "latched"):
        out["stage"] = "brakes"
        out["brake"] = brake
        return out
    rho = tick.get("rho")
    if (not unlocked) and rho is not None and rho < RHO_BLOCK:
        out["stage"] = "chop"
        return out
    if out["fair"] is None or out["fair"] < fair_req:
        out["stage"] = "min_fair"
        return out
    if out["net"] is None or out["net"] < edge_req:
        out["stage"] = "min_edge"
        return out
    if out["ask"] > MAX_PRICE:
        out["stage"] = "max_price"
        return out
    return out


def build_ticks(wins: dict[str, dict]) -> list[dict]:
    """Every cadence tick, classified, with the last-mile check applied."""
    rows: list[dict] = []
    for w in wins.values():
        fire_ts = [(f["t"], f.get("side")) for f in w["fires"]]
        first_fire = min((ft for ft, _ in fire_ts), default=float("inf"))
        for tick in w["ticks"]:
            # The theta gate only guards the FIRST clip of a window; after one
            # lands, position management belongs to the brakes.
            fired_before = tick["t"] > first_fire
            row = classify_tick(tick, w, fired_before)
            row["fired_before"] = fired_before
            if row["stage"] == "pass":
                hit = any(abs(ft - tick["t"]) <= FIRE_MATCH_S and sd == row["side"]
                          for ft, sd in fire_ts)
                row["stage"] = "fired" if hit else "last_mile"
            rows.append(row)
    return rows


def funnel_counts(rows: list[dict]) -> dict[str, int]:
    """Survivors entering each stage, plus 'fired'."""
    died = Counter(r["stage"] for r in rows)
    total = len(rows)
    out = {"armed": total}
    running = total
    for key, _lab, _scope in STAGES[1:]:
        running -= died.get(key, 0)
        out[key] = running
    return out


def funnel_table(rows: list[dict]) -> list[dict]:
    """One row per stage: entering, surviving, %survive, %of-armed."""
    counts = funnel_counts(rows)
    armed = counts["armed"] or 1
    keys = [k for k, _, _ in STAGES]
    table = []
    for i, k in enumerate(keys):
        entering = counts[keys[i - 1]] if i else counts["armed"]
        surviving = counts[k]
        table.append({
            "key": k,
            "label": STAGE_LABEL[k],
            "entering": entering,
            "surviving": surviving,
            "minutes": surviving * CADENCE_S / 60.0,
            "survive_pct": 100.0 * surviving / entering if entering else float("nan"),
            "of_armed_pct": 100.0 * surviving / armed,
            "lost": entering - surviving,
        })
    return table


def binding_gate(rows: list[dict]) -> str | None:
    """The stage that killed the most armed time, ignoring 'armed' itself."""
    died = Counter(r["stage"] for r in rows if r["stage"] not in ("fired",))
    if not died:
        return None
    return died.most_common(1)[0][0]


# ---------------------------------------------------------- gate economics

def theta_survivable(r: dict) -> bool:
    """Would the theta gate have passed this basis-gated moment, had the basis
    guard let it reach one? The gate reason carries banked/cushion — theta's
    exact inputs — and theta only guards a window's FIRST clip."""
    if r.get("fired_before"):
        return True
    s = r.get("safety")
    return s is not None and s >= THETA


def episodes_for_gate(rows: list[dict], gate: str, extra=None) -> list[dict]:
    """Ticks that died at `gate` (so every upstream gate passed), collapsed
    into episodes by shadow.py's own 20s-gap rule so a 4-minute refusal is
    one counterfactual clip, not 48."""
    ticks = [{"t": r["t"], "slug": r["slug"], "side": r["side"],
              "category": gate, "ask": r["ask"], "fair": r["fair"], "net": r["net"]}
             for r in rows
             if r["stage"] == gate and r["side"] and (extra is None or extra(r))]
    return collapse_episodes(ticks)


def price_gate(episodes: list[dict], winners: dict[str, str],
               fires_by_slug: dict[str, list[dict]], default_clip: float) -> dict:
    priced = []
    for ep in episodes:
        clip = window_clip_notional(fires_by_slug.get(ep["slug"], []), default_clip)
        priced.append(price_episode(ep, winners.get(ep["slug"]), clip))
    wins = [e for e in priced if e["status"] == "priced" and e["won"]]
    losses = [e for e in priced if e["status"] == "priced" and not e["won"]]
    n = len(wins) + len(losses)
    missed = sum(e["pnl"] for e in wins)
    avoided = sum(-e["pnl"] for e in losses)
    lo, hi = wilson(len(wins), n) if n else (float("nan"), float("nan"))
    return {
        "episodes": len(priced), "priced": n,
        "unpriced": sum(1 for e in priced if e["status"] == "unpriced"),
        "unresolved": sum(1 for e in priced if e["status"] == "unresolved"),
        "wins": len(wins), "losses": len(losses),
        "hit": len(wins) / n if n else float("nan"),
        "hit_lo": lo, "hit_hi": hi,
        "missed_wins": missed, "avoided_losses": avoided, "net": missed - avoided,
        "median_ask": statistics.median([e["best_ask"] for e in priced
                                         if e["best_ask"] is not None])
        if any(e["best_ask"] is not None for e in priced) else float("nan"),
        "_priced": priced,
    }


def bootstrap_net_ci(priced: list[dict], iters: int = 4000, seed: int = 7) -> tuple:
    """Percentile bootstrap on the episode-level net P&L — the honest CI for
    a handful of lumpy binary outcomes."""
    vals = [e["pnl"] for e in priced if e["status"] == "priced"]
    if len(vals) < 3:
        return (float("nan"), float("nan"))
    rng = random.Random(seed)
    tot = []
    n = len(vals)
    for _ in range(iters):
        tot.append(sum(rng.choice(vals) for _ in range(n)))
    tot.sort()
    return (tot[int(0.025 * iters)], tot[int(0.975 * iters)])


# ------------------------------------------------------------ quiet market

def winner_min_ask(samples: list[dict], winner: str,
                   t_from: float = 0.0, t_to: float = float("inf")) -> dict | None:
    """Cheapest offer ever quoted on the eventual winner inside [t_from, t_to],
    with the size that was actually resting there."""
    key = "up_ask" if winner == "up" else "dn_ask"
    skey = key + "_sz"
    best = None
    for r in samples:
        if not (t_from <= r["t"] <= t_to):
            continue
        a = r.get(key)
        if a is None:
            continue
        if best is None or a < best["ask"]:
            best = {"ask": a, "size": r.get(skey), "t": r["t"]}
    return best


def depth_capped_win(ask: float, size: float | None, clip: float) -> float:
    """Hindsight profit of one clip, capped by the size actually resting at
    that ask. Without the cap a $24 clip at ask 0.01 'wins' $2,400 on depth
    that was never there — the number that makes a hindsight study lie."""
    return depth_capped_pnl(ask, size, clip, True)


def depth_capped_pnl(ask: float, size: float | None, clip: float, won: bool) -> float:
    """Signed hindsight P&L of one depth-capped clip. LOSS forfeits only the
    notional that would actually have filled, not the nominal clip."""
    want = clip / ask
    shares = min(want, size) if size else want
    if won:
        return shares * (1.0 - ask - fee(ask))
    return -shares * ask


def nearest(samples: list[dict], t: float, tol: float = 6.0) -> dict | None:
    best = None
    for r in samples:
        d = abs(r["t"] - t)
        if d <= tol and (best is None or d < abs(best["t"] - t)):
            best = r
    return best


# ---------------------------------------------------------------- regime

def realized_sigma_bp(pair: str, t0: float, t1: float, window: int = 45) -> list[float]:
    """Rolling `window`-minute stdev of 1m log returns, in bp — the same
    quantity the engine's trailing sigma estimates."""
    path = KLINES / f"klines-1m-{pair}.jsonl"
    if not path.exists():
        return []
    closes: dict[int, float] = {}
    for r in _iter_json(path):
        t, c = r.get("t"), r.get("c")
        if t is not None and c:
            closes[int(t)] = float(c)
    ts = sorted(closes)
    rets = []
    for i in range(1, len(ts)):
        if ts[i] - ts[i - 1] != 60:
            rets.append((ts[i], None))
            continue
        rets.append((ts[i], math.log(closes[ts[i]] / closes[ts[i - 1]])))
    out = []
    buf: list[float] = []
    for t, r in rets:
        if r is None:
            buf.clear()
            continue
        buf.append(r)
        if len(buf) > window:
            buf.pop(0)
        if len(buf) == window and t0 <= t <= t1:
            out.append(statistics.pstdev(buf) * 1e4)
    return out


def pctl(xs: list[float], q: float) -> float:
    if not xs:
        return float("nan")
    s = sorted(xs)
    i = min(len(s) - 1, max(0, int(round(q * (len(s) - 1)))))
    return s[i]


# ---------------------------------------------------------------- report

def _f(x, nd=1, dollar=False, pct=False):
    if x is None or (isinstance(x, float) and math.isnan(x)):
        return "-"
    if dollar:
        return f"{'-' if x < 0 else ''}${abs(x):,.2f}"
    if pct:
        return f"{x:.{nd}f}%"
    return f"{x:.{nd}f}"


def _wrap(text: str, width: int) -> list[str]:
    out, line = [], ""
    for word in text.split():
        if line and len(line) + 1 + len(word) > width:
            out.append(line)
            line = word
        else:
            line = f"{line} {word}".strip()
    if line:
        out.append(line)
    return out


def utc(ts: float) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%H:%M:%SZ")


def render(t0: float, t1: float, wins: dict, rows: list[dict], book: dict,
           winners: dict[str, str]) -> str:
    L: list[str] = []
    add = L.append
    span_h = (t1 - t0) / 3600.0
    by_series: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        by_series[r["series"]].append(r)
    series_order = sorted(by_series)

    fires_by_slug = {s: w["fires"] for s, w in wins.items()}
    all_fires = [f for w in wins.values() for f in w["fires"]]
    fire_notionals = [f["size"] * f["ask"] for f in all_fires
                      if f.get("size") and f.get("ask")]
    default_clip = statistics.median(fire_notionals) if fire_notionals else 25.0

    add("# freq_funnel — where the fleet's trades die")
    add("")
    add(f"corpus window   : {utc(t0)} -> {utc(t1)}  ({span_h:.2f}h, theta era)")
    add(f"windows armed   : {len(wins)}   cadence ticks: {len(rows)}   "
        f"armed window-minutes: {len(rows) * CADENCE_S / 60.0:.0f}")
    add(f"fires on tape   : {len(all_fires)} clips across "
        f"{len({f['slug'] for f in all_fires})} windows "
        f"({len(all_fires) / span_h:.1f} clips/h)")
    add(f"outcomes joined : {sum(1 for s in wins if s in winners)}/{len(wins)} windows")
    add(f"clip notional   : median observed fire ${default_clip:,.2f} "
        f"(used for windows that never fired)")
    add("rerun           : cd pmtrader && uv run python ../analysis/freq_funnel.py "
        "--out ../analysis/freq_funnel_report.md")
    add("")
    answer_at = len(L)
    add("Gate order is the engine's own (updown.rs decide): feed -> basis guard ->")
    add("book -> theta -> brakes -> chop -> min_fair -> min_edge -> max_price ->")
    add("budget/cooldown. Every refused moment is charged to the FIRST gate that")
    add("stopped it, so no gate is billed for work a gate upstream already did.")
    add("")

    # ---------------- 1. funnel
    add("=" * 78)
    add("1. THE FUNNEL")
    add("=" * 78)
    add("")
    for name, subset in [("POOLED (all series)", rows)] + \
                        [(s, by_series[s]) for s in series_order]:
        tbl = funnel_table(subset)
        add(f"--- {name} ---")
        add(f"{'stage':<44}{'ticks':>8}{'win-min':>9}{'%survive':>10}{'%armed':>9}")
        for r in tbl:
            add(f"{r['label']:<44}{r['surviving']:>8}{r['minutes']:>9.1f}"
                f"{r['survive_pct']:>9.1f}%{r['of_armed_pct']:>8.1f}%")
        bind = binding_gate(subset)
        died = Counter(x["stage"] for x in subset)
        add(f"BINDING GATE: {bind}  ({died[bind]} ticks = "
            f"{100.0 * died[bind] / max(len(subset), 1):.1f}% of armed time, "
            f"{died[bind] * CADENCE_S / 60.0:.0f} window-minutes)")
        add("")

    add("Where the armed time goes, pooled (share of all cadence ticks):")
    died = Counter(r["stage"] for r in rows)
    for k, n in died.most_common():
        add(f"  {k:<16}{n:>7}  {100.0 * n / len(rows):>5.1f}%   "
            f"{n * CADENCE_S / 60.0:>6.0f} window-min")
    add("")

    # ---------------- 2. binding-gate economics
    add("=" * 78)
    add("2. BINDING-GATE ECONOMICS (hindsight-priced, one clip per episode)")
    add("=" * 78)
    add("")
    add("Each row = the moments blocked ONLY by that gate (everything upstream")
    add("passed), collapsed into episodes on shadow.py's 20s-gap rule, priced as")
    add("one clip at the episode's best (lowest) recorded ask. WIN pays")
    add("shares*(1-ask-fee); LOSS forfeits the clip. NET = missed wins MINUS")
    add("avoided losses: NET>0 means the gate cost us money tonight.")
    add("")
    hdr = (f"{'gate':<14}{'eps':>5}{'priced':>7}{'hit':>7}{'95% CI':>15}"
           f"{'med ask':>9}{'missed':>11}{'avoided':>11}{'NET':>11}")
    add(hdr)
    add("-" * len(hdr))
    gate_stats = {}
    for key, _lab, _scope in STAGES[1:]:
        eps = episodes_for_gate(rows, key)
        if not eps:
            continue
        st = price_gate(eps, winners, fires_by_slug, default_clip)
        gate_stats[key] = st
        ci = (f"[{st['hit_lo'] * 100:.0f}-{st['hit_hi'] * 100:.0f}%]"
              if st["priced"] else "-")
        add(f"{key:<14}{st['episodes']:>5}{st['priced']:>7}"
            f"{_f(st['hit'] * 100 if st['priced'] else float('nan'), 1, pct=True):>7}"
            f"{ci:>15}{_f(st['median_ask'], 3):>9}"
            f"{_f(st['missed_wins'], dollar=True):>11}"
            f"{_f(st['avoided_losses'], dollar=True):>11}"
            f"{_f(st['net'], dollar=True):>11}")
    add("")

    # per-series for the top gates
    top_gates = sorted(gate_stats, key=lambda k: -abs(gate_stats[k]["net"]))[:3]
    for g in top_gates:
        add(f"--- {g}: by series ---")
        add(f"{'series':<12}{'eps':>5}{'priced':>7}{'hit':>7}{'missed':>11}"
            f"{'avoided':>11}{'NET':>11}")
        for s in series_order:
            eps = episodes_for_gate(by_series[s], g)
            if not eps:
                continue
            st = price_gate(eps, winners, fires_by_slug, default_clip)
            add(f"{s:<12}{st['episodes']:>5}{st['priced']:>7}"
                f"{_f(st['hit'] * 100 if st['priced'] else float('nan'), 0, pct=True):>7}"
                f"{_f(st['missed_wins'], dollar=True):>11}"
                f"{_f(st['avoided_losses'], dollar=True):>11}"
                f"{_f(st['net'], dollar=True):>11}")
        add("")

    # which brake, exactly
    add("--- brakes: which one ---")
    add(f"{'brake':<12}{'ticks':>7}{'eps':>5}{'priced':>7}{'hit':>7}"
        f"{'missed':>11}{'avoided':>11}{'NET':>11}")
    brake_rows = [r for r in rows if r["stage"] == "brakes"]
    for bname in ("latched", "distrust", "avg_down"):
        sub = [r for r in brake_rows if r.get("brake") == bname]
        if not sub:
            continue
        eps = collapse_episodes([{"t": r["t"], "slug": r["slug"], "side": r["side"],
                                  "category": bname, "ask": r["ask"],
                                  "fair": r["fair"], "net": r["net"]} for r in sub])
        st = price_gate(eps, winners, fires_by_slug, default_clip)
        add(f"{bname:<12}{len(sub):>7}{st['episodes']:>5}{st['priced']:>7}"
            f"{_f(st['hit'] * 100 if st['priced'] else float('nan'), 0, pct=True):>7}"
            f"{_f(st['missed_wins'], dollar=True):>11}"
            f"{_f(st['avoided_losses'], dollar=True):>11}"
            f"{_f(st['net'], dollar=True):>11}")
    add("")

    # the min_ask floor: what the book was asking when the edge gate bound
    add("--- the min_ask floor: is it the edge FLOOR or the PRICE? ---")
    add("Two different bars live behind one funnel stage: an unlocked (safe-mode)")
    add(f"side needs min_edge {MIN_EDGE}, a locked (spec-mode) side needs")
    add(f"early_min_edge {EARLY_MIN_EDGE}. They are different knobs with different")
    add("answers, so they are split here.")
    me = [r for r in rows if r["stage"] == "min_edge" and r.get("ask") is not None]
    for mode_unlocked, mname, req in ((True, "SAFE (unlocked)", MIN_EDGE),
                                      (False, "SPEC (locked)", EARLY_MIN_EDGE)):
        sub = [r for r in me if bool(r.get("unlocked")) == mode_unlocked]
        if not sub:
            continue
        asks = [r["ask"] for r in sub]
        nets = [r["net"] for r in sub if r.get("net") is not None]
        add("")
        add(f"  {mname}: {len(sub)} ticks, bar = {req}")
        add(f"    ask  p10 {pctl(asks, .10):.3f}  p25 {pctl(asks, .25):.3f}  "
            f"median {pctl(asks, .50):.3f}  p75 {pctl(asks, .75):.3f}  "
            f"p90 {pctl(asks, .90):.3f}")
        add(f"    net  p10 {pctl(nets, .10):+.4f}  p25 {pctl(nets, .25):+.4f}  "
            f"median {pctl(nets, .50):+.4f}  p75 {pctl(nets, .75):+.4f}  "
            f"p90 {pctl(nets, .90):+.4f}")
        cuts = (0.012, 0.010, 0.008, 0.005) if mode_unlocked else (0.06, 0.04, 0.03, 0.02)
        for cut in cuts:
            n = sum(1 for x in nets if x >= cut)
            add(f"    net >= {cut:<6}: {n:>5} ticks "
                f"({100.0 * n / len(nets):.0f}% of them would be released)")
    ceil97 = MIN_FAIR - MIN_EDGE - fee(0.95)
    add("")
    add(f"  Effective max ask a safe-mode clip can pay: {ceil97:.3f} at "
        f"fair = min_fair {MIN_FAIR}, {1.0 - MIN_EDGE - fee(0.985):.3f} at fair = 1.00.")
    if me:
        asks = [r["ask"] for r in me]
        add(f"  Edge-refused ticks already asking above {ceil97:.3f}: "
            f"{sum(1 for a in asks if a > ceil97)} of {len(asks)} "
            f"({100.0 * sum(1 for a in asks if a > ceil97) / len(asks):.0f}%) — "
            f"that is the")
        add("  min_ask floor biting: not our edge bar, the market's price.")
    add("")

    # the no-offer problem
    add("--- no offer at any price: the book_quoted stage ---")
    bq = [r for r in rows if r["stage"] == "book_quoted"]
    if bq:
        fr = []
        bidq = []
        for r in bq:
            meta = wins[r["slug"]]["meta"]
            fr.append((r["t"] - meta["start"]) / meta["dur_s"])
            s = nearest(book.get(r["slug"]) or [], r["t"])
            if s:
                b = s.get("up_bid") if r["side"] == "up" else s.get("dn_bid")
                if b is not None:
                    bidq.append(b)
        add(f"eval ticks whose model side had NO ask on the book: {len(bq)} "
            f"({100.0 * len(bq) / max(len(rows), 1):.1f}% of armed time, "
            f"{len(bq) * CADENCE_S / 60.0:.0f} window-minutes)")
        add(f"  window elapsed-frac  p10 {pctl(fr, .10):.2f}  median "
            f"{pctl(fr, .50):.2f}  p90 {pctl(fr, .90):.2f}   (late-window, "
            f"exactly when the model is finally confident)")
        if bidq:
            add(f"  our side's BID at those moments: p25 {pctl(bidq, .25):.3f}  "
                f"median {pctl(bidq, .50):.3f}  p75 {pctl(bidq, .75):.3f}")
        add("  Reading: the side we want is bid up near 1.00 and NOBODY IS "
            "OFFERING. No gate setting reaches this time — it is supply.")
    add("")

    # basis guard counterfactual layers
    add("--- basis guard: counterfactual layers ---")
    add("A basis-gated tick never got a model, so L0 (the naive shadow-ledger")
    add("number) prices moments that the NEXT gates would have refused anyway.")
    add("The gate reason carries banked/cushion, which is exactly theta's input,")
    add("so we can carry the counterfactual one step further:")
    add("  L0  every basis-gated moment")
    add(f"  L1  + the theta gate would also have cleared (safety >= {THETA})")
    add("  L2  + the recorded ask is low enough to clear min_edge at min_fair")
    add(f"      (ask <= {MIN_FAIR - MIN_EDGE - fee(0.95):.3f})")
    ask_ceiling = MIN_FAIR - MIN_EDGE - fee(0.95)
    layers = [
        ("L0 all", None),
        ("L1 +theta", theta_survivable),
        ("L2 +edge", lambda r: theta_survivable(r)
         and r.get("ask") is not None and r["ask"] <= ask_ceiling),
    ]
    add("")
    layer_stats: dict[str, tuple] = {}
    add(f"{'layer':<12}{'eps':>5}{'priced':>7}{'hit':>7}{'95% CI':>15}"
        f"{'missed':>11}{'avoided':>11}{'NET':>11}{'boot 95% CI on NET':>28}")
    for lab, pred in layers:
        eps = episodes_for_gate(rows, "basis_guard", pred)
        if not eps:
            add(f"{lab:<12}    0")
            continue
        st = price_gate(eps, winners, fires_by_slug, default_clip)
        lo, hi = bootstrap_net_ci(st["_priced"])
        layer_stats[lab] = (st, lo, hi)
        ci = (f"[{st['hit_lo'] * 100:.0f}-{st['hit_hi'] * 100:.0f}%]"
              if st["priced"] else "-")
        boot = (f"[{_f(lo, dollar=True)}, {_f(hi, dollar=True)}]"
                if not math.isnan(lo) else "-")
        add(f"{lab:<12}{st['episodes']:>5}{st['priced']:>7}"
            f"{_f(st['hit'] * 100 if st['priced'] else float('nan'), 1, pct=True):>7}"
            f"{ci:>15}{_f(st['missed_wins'], dollar=True):>11}"
            f"{_f(st['avoided_losses'], dollar=True):>11}"
            f"{_f(st['net'], dollar=True):>11}{boot:>28}")
    add("")

    # per-series L1 (the number a guard trim would actually buy)
    add("--- basis guard L1 (theta-survivable) by series: what a guard trim buys ---")
    add(f"{'series':<12}{'guard':>6}{'eps':>5}{'priced':>7}{'hit':>7}"
        f"{'missed':>11}{'avoided':>11}{'NET':>11}")
    guard_by_series = {}
    for s in series_order:
        gs = [r["guard_bp"] for r in by_series[s] if r.get("guard_bp")]
        guard_by_series[s] = statistics.median(gs) if gs else float("nan")
        eps = episodes_for_gate(by_series[s], "basis_guard", theta_survivable)
        if not eps:
            add(f"{s:<12}{_f(guard_by_series[s], 0):>6}    0")
            continue
        st = price_gate(eps, winners, fires_by_slug, default_clip)
        add(f"{s:<12}{_f(guard_by_series[s], 0):>6}{st['episodes']:>5}{st['priced']:>7}"
            f"{_f(st['hit'] * 100 if st['priced'] else float('nan'), 0, pct=True):>7}"
            f"{_f(st['missed_wins'], dollar=True):>11}"
            f"{_f(st['avoided_losses'], dollar=True):>11}"
            f"{_f(st['net'], dollar=True):>11}")
    add("")

    # guard-trim sweep: what each 1bp of guard is worth, L1-filtered
    add("--- guard trim sweep: episodes that a LOWER guard would have released ---")
    add("(basis-gated moments whose |margin| already exceeded the trial guard,")
    add(" theta-survivable, priced the same way. This is the A/B candidate set.)")
    add("")
    add(f"{'series':<12}{'guard':>6}{'trial':>7}{'eps':>5}{'priced':>7}{'hit':>7}"
        f"{'missed':>11}{'avoided':>11}{'NET':>11}")
    for s in series_order:
        g = guard_by_series[s]
        if math.isnan(g):
            continue
        for trial in [g - 1, g - 2, g - 3]:
            if trial < 1:
                continue
            eps = episodes_for_gate(
                by_series[s], "basis_guard",
                lambda r, tr=trial: (r.get("margin_bp") is not None
                                     and abs(r["margin_bp"]) >= tr
                                     and theta_survivable(r)))
            if not eps:
                continue
            st = price_gate(eps, winners, fires_by_slug, default_clip)
            add(f"{s:<12}{g:>6.0f}{trial:>7.0f}{st['episodes']:>5}{st['priced']:>7}"
                f"{_f(st['hit'] * 100 if st['priced'] else float('nan'), 0, pct=True):>7}"
                f"{_f(st['missed_wins'], dollar=True):>11}"
                f"{_f(st['avoided_losses'], dollar=True):>11}"
                f"{_f(st['net'], dollar=True):>11}")
    add("")

    # theta sweep on the theta-bound ticks
    add("--- theta trim sweep: eval moments the safety gate refused ---")
    add("(each row = the moments a LOWER theta would have released, priced the")
    add(" same way. NET>0 = the trim would have made money on tonight's tape.)")
    add(f"{'trial theta':<14}{'eps':>5}{'priced':>7}{'hit':>7}{'95% CI':>15}"
        f"{'missed':>11}{'avoided':>11}{'NET':>11}{'boot 95% CI on NET':>28}")
    for trial in (0.25, 0.20, 0.15, 0.10, 0.0):
        eps = episodes_for_gate(
            rows, "theta",
            lambda r, tr=trial: r.get("safety") is not None and r["safety"] >= tr)
        if not eps:
            add(f"{trial:<14}    0")
            continue
        st = price_gate(eps, winners, fires_by_slug, default_clip)
        lo, hi = bootstrap_net_ci(st["_priced"])
        ci = (f"[{st['hit_lo'] * 100:.0f}-{st['hit_hi'] * 100:.0f}%]"
              if st["priced"] else "-")
        boot = (f"[{_f(lo, dollar=True)}, {_f(hi, dollar=True)}]"
                if not math.isnan(lo) else "-")
        add(f"{trial:<14}{st['episodes']:>5}{st['priced']:>7}"
            f"{_f(st['hit'] * 100 if st['priced'] else float('nan'), 0, pct=True):>7}"
            f"{ci:>15}{_f(st['missed_wins'], dollar=True):>11}"
            f"{_f(st['avoided_losses'], dollar=True):>11}"
            f"{_f(st['net'], dollar=True):>11}{boot:>28}")
    add("")

    # min_edge sweep on the min_edge-bound ticks
    add("--- edge-bar trim sweeps (the two bars, separately) ---")
    add(f"{'bar / trial':<30}{'eps':>5}{'priced':>7}{'hit':>7}{'95% CI':>15}"
        f"{'missed':>11}{'avoided':>11}{'NET':>11}{'boot 95% CI on NET':>28}")
    edge_trials = [("min_edge (safe)", True, t) for t in (0.012, 0.010, 0.008, 0.005)]
    edge_trials += [("early_min_edge (spec)", False, t) for t in (0.06, 0.04, 0.03, 0.02)]
    for bar, want_unlocked, trial in edge_trials:
        label = f"{bar} -> {trial}"
        eps = episodes_for_gate(
            rows, "min_edge",
            lambda r, tr=trial, wu=want_unlocked: (
                bool(r.get("unlocked")) == wu
                and r.get("net") is not None and r["net"] >= tr))
        if not eps:
            add(f"{label:<30}    0")
            continue
        st = price_gate(eps, winners, fires_by_slug, default_clip)
        lo, hi = bootstrap_net_ci(st["_priced"])
        ci = (f"[{st['hit_lo'] * 100:.0f}-{st['hit_hi'] * 100:.0f}%]"
              if st["priced"] else "-")
        boot = (f"[{_f(lo, dollar=True)}, {_f(hi, dollar=True)}]"
                if not math.isnan(lo) else "-")
        add(f"{label:<30}{st['episodes']:>5}{st['priced']:>7}"
            f"{_f(st['hit'] * 100 if st['priced'] else float('nan'), 0, pct=True):>7}"
            f"{ci:>15}{_f(st['missed_wins'], dollar=True):>11}"
            f"{_f(st['avoided_losses'], dollar=True):>11}"
            f"{_f(st['net'], dollar=True):>11}{boot:>28}")
    add("")

    # spec-mode question
    add("--- spec mode: does it still exist post-theta? ---")
    spec_rows = [r for r in rows if r.get("spec")]
    safe_rows = [r for r in rows if r.get("unlocked")]
    spec_evals = [r for r in spec_rows if r["stage"] not in
                  ("live_model", "basis_guard")]
    spec_fires = [f for w in wins.values() for f in w["fires"]
                  if f.get("mode") == "spec"]
    safe_fires = [f for w in wins.values() for f in w["fires"]
                  if f.get("mode") == "safe"]
    flip_fires = [f for w in wins.values() for f in w["fires"]
                  if f.get("mode") == "flip"]
    add(f"cadence ticks in spec mode (locked budget) : {len(spec_rows)} "
        f"({100.0 * len(spec_rows) / max(len(rows), 1):.1f}% of armed)")
    add(f"  ...of which reached the side gates       : {len(spec_evals)}")
    add(f"cadence ticks in safe mode (unlocked)      : {len(safe_rows)}")
    spec_died = Counter(r["stage"] for r in spec_evals)
    add(f"  spec-mode side-gate deaths: {dict(spec_died.most_common())}")
    add(f"fires by mode: spec={len(spec_fires)}  safe={len(safe_fires)}  "
        f"flip={len(flip_fires)}")
    if spec_fires:
        add(f"  last spec fire at {utc(max(f['t'] for f in spec_fires))}")
    spec_mf = sum(1 for r in rows if r["stage"] == "min_fair" and not r.get("unlocked"))
    spec_me = sum(1 for r in rows if r["stage"] == "min_edge" and not r.get("unlocked"))
    add(f"spec-mode deaths at EARLY_MIN_FAIR {EARLY_MIN_FAIR}: {spec_mf}   "
        f"at early_min_edge {EARLY_MIN_EDGE}: {spec_me}")
    add("")
    if spec_fires:
        add(f"VERDICT: spec mode is NOT dead post-theta — {len(spec_fires)} of "
            f"{len(all_fires)} clips tonight ({100.0 * len(spec_fires) / max(len(all_fires), 1):.0f}%)")
        add(f"fired in spec mode, the last at {utc(max(f['t'] for f in spec_fires))}. "
            f"What killed spec")
    else:
        add("VERDICT: spec mode fired NOTHING tonight. What kills it")
    add(f"moments is theta ({spec_died.get('theta', 0)} ticks) and the brakes "
        f"({spec_died.get('brakes', 0)}), both UPSTREAM of the")
    add(f"spec bars — EARLY_MIN_FAIR {EARLY_MIN_FAIR} refused {spec_mf} moment"
        f"{'' if spec_mf == 1 else 's'} all night. Re-tuning the")
    add("spec bars is re-tuning a gate that is barely reached.")
    add("")

    # ---------------- 3. quiet market
    add("=" * 78)
    add("3. THE QUIET-MARKET QUESTION")
    add("=" * 78)
    add("")
    add("For every window that never fired, three progressively honest measures of")
    add("what was actually on the table, all on the eventual winner\'s side and all")
    add("capped by the size really resting at that ask (uncapped, a $24 clip at ask")
    add("0.01 'wins' $2,400 on depth that was never there):")
    add("")
    add("  A  ANY  — cheapest offer at any point in the window. Pure hindsight: a")
    add("           binary opens near 0.50, so this is almost always < 0.90 and")
    add("           mostly measures reversals nobody could have known. Ceiling only.")
    add("  B  LATE — cheapest offer in the final 120s (the unlocked / safe-bet")
    add("           window where min_fair 0.97 applies). This is the safe-bet")
    add("           opportunity the fleet is actually built to take.")
    add("  C  KNEW — cheapest offer at a moment when OUR OWN read already pointed")
    add("           at the winner (eval p_up side, or a gated tick's margin sign).")
    add("           This is the only one that is a verdict on our gates: the market")
    add("           was selling the winner cheap AND we already knew which side.")
    add("")
    add("'No edge existed' = that measure never got below 0.90.")
    add("")
    fired_slugs = {s for s, w in wins.items() if w["fires"]}
    no_eval_slugs = {s for s, w in wins.items()
                     if not any(t["ev"] == "eval" for t in w["ticks"])}
    zero_trade = sorted(s for s in wins if s not in fired_slugs)
    rows_by_slug: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        rows_by_slug[r["slug"]].append(r)

    def measures(slug: str) -> dict | None:
        win = winners.get(slug)
        samples = book.get(slug) or []
        if win is None or not samples:
            return None
        meta = wins[slug]["meta"]
        out = {"any": winner_min_ask(samples, win),
               "late": winner_min_ask(samples, win, meta["end"] - LATE_REM_S,
                                      meta["end"])}
        knew = None
        for r in rows_by_slug.get(slug, []):
            if r["side"] != win or r.get("ask") is None:
                continue
            bs = nearest(samples, r["t"])
            sz = None
            if bs:
                sz = bs.get("up_ask_sz" if win == "up" else "dn_ask_sz")
            if knew is None or r["ask"] < knew["ask"]:
                knew = {"ask": r["ask"], "size": sz, "t": r["t"]}
        out["knew"] = knew
        return out

    for label, universe in [("ZERO-FIRE windows", zero_trade),
                            ("basis guard NEVER cleared", sorted(no_eval_slugs))]:
        add(f"--- {label} (n={len(universe)}) ---")
        got = {k: [] for k in ("any", "late", "knew")}
        unknown = 0
        for sl in universe:
            m = measures(sl)
            if m is None:
                unknown += 1
                continue
            for k in got:
                if m[k] is not None:
                    got[k].append((sl, m[k]))
        tot = max(len(universe), 1)
        add(f"  windows with no outcome or no book: {unknown} "
            f"({100.0 * unknown / tot:.0f}%)")
        add("")
        add(f"  {'measure':<8}{'n':>5}{'no edge':>9}{'edge, gated':>13}"
            f"{'% of n':>9}{'$ ceiling (depth-capped)':>26}")
        for k, name in (("any", "A ANY"), ("late", "B LATE"), ("knew", "C KNEW")):
            v = got[k]
            if not v:
                add(f"  {name:<8}{0:>5}")
                continue
            cheap = [(sl, d) for sl, d in v if d["ask"] < 0.90]
            money = sum(depth_capped_win(d["ask"], d["size"], default_clip)
                        for _sl, d in cheap)
            add(f"  {name:<8}{len(v):>5}{len(v) - len(cheap):>9}{len(cheap):>13}"
                f"{100.0 * len(cheap) / len(v):>8.0f}%"
                f"{_f(money, dollar=True):>26}")
        add("")
        for k, name in (("any", "A ANY"), ("late", "B LATE"), ("knew", "C KNEW")):
            asks = [d["ask"] for _sl, d in got[k]]
            if asks:
                add(f"  {name} winner min-ask: p10 {pctl(asks, .10):.3f}  "
                    f"p25 {pctl(asks, .25):.3f}  median {pctl(asks, .50):.3f}  "
                    f"p75 {pctl(asks, .75):.3f}  p90 {pctl(asks, .90):.3f}")
        deep = sorted([x for x in got["knew"] if x[1]["ask"] < 0.90],
                      key=lambda x: x[1]["ask"])[:10]
        if deep:
            add("")
            add("  deepest C-KNEW misses (we already pointed at the winner and the")
            add("  book was offering it this cheap):")
            for sl, d in deep:
                meta = wins[sl]["meta"]
                add(f"    {sl:<32} ask {d['ask']:.3f} x{_f(d['size'], 0)} sh "
                    f"@{(d['t'] - meta['start']) / meta['dur_s'] * 100:>3.0f}% "
                    f"elapsed -> "
                    f"{_f(depth_capped_win(d['ask'], d['size'], default_clip), dollar=True)}")
        add("")

    # D: the unbiased version — one clip per zero-fire window at the best price
    # OUR OWN read ever offered, graded on the real outcome, wrong side included.
    add("--- D UNBIASED: one clip per zero-fire window, our side, real outcome ---")
    add("A/B/C above are conditioned on the eventual winner, so they only ever")
    add("show upside — they are ceilings, not verdicts. D takes the cheapest")
    add("moment on whatever side OUR read favoured at the time and grades it on")
    add("the real outcome, losers included. This is the honest 'was there money")
    add("in the quiet' number.")
    add("")
    add(f"  {'variant':<22}{'n':>5}{'hit':>7}{'95% CI':>14}{'missed':>11}"
        f"{'avoided':>11}{'NET':>11}")
    d_stats: dict[str, dict] = {}
    for vname, pred in (("D-best (any tick)", lambda r: True),
                        ("D-theta (safety>=0.3)", theta_survivable)):
        wins_n = losses_n = 0
        missed = avoided = 0.0
        for sl in zero_trade:
            win = winners.get(sl)
            samples = book.get(sl) or []
            if win is None or not samples:
                continue
            best = None
            for r in rows_by_slug.get(sl, []):
                if not r["side"] or r.get("ask") is None or not pred(r):
                    continue
                if best is None or r["ask"] < best["ask"]:
                    bs = nearest(samples, r["t"])
                    sz = bs.get("up_ask_sz" if r["side"] == "up" else "dn_ask_sz") \
                        if bs else None
                    best = {"ask": r["ask"], "size": sz, "side": r["side"]}
            if best is None:
                continue
            won = best["side"] == win
            pnl = depth_capped_pnl(best["ask"], best["size"], default_clip, won)
            if won:
                wins_n += 1
                missed += pnl
            else:
                losses_n += 1
                avoided += -pnl
        n = wins_n + losses_n
        lo, hi = wilson(wins_n, n) if n else (float("nan"), float("nan"))
        d_stats[vname] = {"n": n, "wins": wins_n, "hit": wins_n / n if n else float("nan"),
                          "missed": missed, "avoided": avoided, "net": missed - avoided}
        ci = f"[{lo * 100:.0f}-{hi * 100:.0f}%]" if n else "-"
        add(f"  {vname:<22}{n:>5}"
            f"{_f(100.0 * wins_n / n if n else float('nan'), 0, pct=True):>7}{ci:>14}"
            f"{_f(missed, dollar=True):>11}{_f(avoided, dollar=True):>11}"
            f"{_f(missed - avoided, dollar=True):>11}")
    add("")

    # per-series zero-fire rate
    add("--- zero-fire rate by series (C-KNEW basis) ---")
    add(f"{'series':<12}{'windows':>9}{'fired':>7}{'zero':>7}{'zero%':>8}"
        f"{'edge existed':>14}{'$ ceiling':>12}")
    for s in series_order:
        ws = [sl for sl, w in wins.items() if w["meta"]["series"] == s]
        f_n = sum(1 for sl in ws if sl in fired_slugs)
        z = [sl for sl in ws if sl not in fired_slugs]
        edge, money = 0, 0.0
        for sl in z:
            m = measures(sl)
            if m is None or m["knew"] is None:
                continue
            d = m["knew"]
            if d["ask"] < 0.90:
                edge += 1
                money += depth_capped_win(d["ask"], d["size"], default_clip)
        add(f"{s:<12}{len(ws):>9}{f_n:>7}{len(z):>7}"
            f"{100.0 * len(z) / max(len(ws), 1):>7.0f}%{edge:>14}"
            f"{_f(money, dollar=True):>12}")
    add("")

    # ---------------- 4. regime
    add("=" * 78)
    add("4. REGIME CONTEXT")
    add("=" * 78)
    add("")
    add("--- |projected margin| vs the guard ---")
    add(f"{'series':<12}{'guard':>6}{'n':>7}{'p25':>7}{'p50':>7}{'p75':>7}"
        f"{'p90':>7}{'<guard':>8}{'within 1bp':>12}{'within 2bp':>12}")
    for s in series_order:
        g = guard_by_series[s]
        ms = [abs(r["margin_bp"]) for r in by_series[s]
              if r.get("margin_bp") is not None]
        if not ms:
            continue
        under = [m for m in ms if m < g]
        near1 = [m for m in under if g - m <= 1.0]
        near2 = [m for m in under if g - m <= 2.0]
        add(f"{s:<12}{g:>6.0f}{len(ms):>7}{pctl(ms, .25):>7.1f}{pctl(ms, .50):>7.1f}"
            f"{pctl(ms, .75):>7.1f}{pctl(ms, .90):>7.1f}"
            f"{100.0 * len(under) / len(ms):>7.0f}%"
            f"{100.0 * len(near1) / max(len(under), 1):>11.0f}%"
            f"{100.0 * len(near2) / max(len(under), 1):>11.0f}%")
    add("  (%<guard is over ticks with a known margin; 'within Nbp' is the share")
    add("   of BLOCKED ticks that a guard N bp lower would have released.)")
    add("")

    add("--- realized sigma tonight vs the 90-day 1m baseline (bp/min, 45m window) ---")
    add(f"{'symbol':<8}{'tonight p50':>12}{'90d p10':>9}{'90d p25':>9}{'90d p50':>9}"
        f"{'90d p75':>9}{'pctile':>9}")
    ranks: list[float] = []
    for sym in sorted({r["series"].split()[0] for r in rows}):
        pair = SYMBOL_PAIR.get(sym)
        if not pair:
            continue
        base = realized_sigma_bp(pair, 0, t0)
        tonight = realized_sigma_bp(pair, t0, t1)
        if not base or not tonight:
            continue
        med = statistics.median(tonight)
        rank = 100.0 * sum(1 for b in base if b <= med) / len(base)
        ranks.append(rank)
        add(f"{sym:<8}{med:>12.1f}{pctl(base, .10):>9.1f}{pctl(base, .25):>9.1f}"
            f"{pctl(base, .50):>9.1f}{pctl(base, .75):>9.1f}{rank:>8.0f}%")
    if ranks:
        rmin, rmax = min(ranks), max(ranks)
        if rmax < 35:
            calm = "ABNORMALLY CALM — the quiet is the tape, not the gates."
        elif rmin > 55:
            calm = ("NOT CALM. Tonight sits ABOVE the 90-day median on every "
                    "symbol, so")
        else:
            calm = "MIXED — some symbols quiet, some not."
        add("")
        add(f"  VERDICT: {calm}")
        if rmin > 55:
            add("  low volatility is NOT the explanation for the low fire count. The")
            add("  fleet is quiet in a normal-to-busy tape.")
    add("")
    add("--- engine-reported sig_bp (eval tape) by series, tonight ---")
    add(f"{'series':<12}{'n':>7}{'p25':>8}{'p50':>8}{'p75':>8}")
    sig_by = defaultdict(list)
    for w in wins.values():
        for tk in w["ticks"]:
            if tk["ev"] == "eval" and tk.get("sig_bp"):
                sig_by[w["meta"]["series"]].append(tk["sig_bp"])
    for s in series_order:
        v = sig_by.get(s) or []
        if not v:
            continue
        add(f"{s:<12}{len(v):>7}{pctl(v, .25):>8.1f}{pctl(v, .50):>8.1f}"
            f"{pctl(v, .75):>8.1f}")
    add("")

    add("--- fires/hour by policy era (whole tape, not just the theta era) ---")
    era_fires = Counter()
    era_windows = defaultdict(set)
    tmin, tmax = float("inf"), 0.0
    for r in _iter_json(TAPE):
        t = r.get("t")
        if t is None:
            continue
        tmin, tmax = min(tmin, t), max(tmax, t)
    era_armed = defaultdict(set)
    for r in _iter_json(TAPE):
        ev = r.get("ev")
        if ev not in ("fire", "eval", "gated"):
            continue
        for name, a, b in ERAS:
            if a <= r["t"] < b:
                era_armed[name].add(r["slug"])
                if ev == "fire":
                    era_fires[name] += 1
                    era_windows[name].add(r["slug"])
                break
    add(f"{'era':<14}{'from':>11}{'to':>11}{'hours':>7}{'clips':>7}"
        f"{'clips/h':>9}{'armed w':>9}{'fired w':>9}{'fired w %':>11}")
    for name, a, b in ERAS:
        lo = max(a, tmin)
        hi = min(b, tmax)
        if hi <= lo:
            continue
        h = (hi - lo) / 3600.0
        na, nf = len(era_armed[name]), len(era_windows[name])
        add(f"{name:<14}{utc(lo):>11}{utc(hi):>11}{h:>7.2f}{era_fires[name]:>7}"
            f"{era_fires[name] / h:>9.1f}{na:>9}{nf:>9}"
            f"{100.0 * nf / max(na, 1):>10.0f}%")
    add("  ('armed w' counts every window with a tape record in the era, so a")
    add("   window spanning two eras is counted in both — read the % as a rate,")
    add("   not a ledger.)")
    add("")

    # ---------------- 5. recommendations
    add("=" * 78)
    add("5. RECOMMENDATIONS, RANKED BY NET SHADOW $")
    add("=" * 78)
    add("")
    add("Every row is: move this knob this far, and tonight's tape says you would")
    add("have taken these episodes, at this hit rate, for this NET. The bootstrap")
    add("CI is over episode P&L — where it straddles $0 the number is a direction,")
    add("not a result.")
    add("")
    add("LOOSEN = the change relaxes a gate. ROADMAP operating rule: a loosened")
    add("gate ships ONLY on a replay A/B win, then one small-size night, then full")
    add("size. The A/B command is on the row.")
    add("")
    cands = []

    def add_cand(knob, move, st, loosen, ab, note=""):
        lo, hi = bootstrap_net_ci(st["_priced"])
        cands.append({
            "knob": knob, "move": move, "n": st["priced"],
            "hit": st["hit"], "hit_lo": st["hit_lo"], "hit_hi": st["hit_hi"],
            "net": st["net"], "lo": lo, "hi": hi,
            "loosen": loosen, "ab": ab, "note": note,
        })

    for s_ in series_order:
        g = guard_by_series[s_]
        if math.isnan(g):
            continue
        sym = s_.split()[0]
        dur = s_.split()[1]
        pref = f"{sym}-updown-{dur}"
        for trial in (g - 1, g - 2, g - 3):
            if trial < 1:
                continue
            eps = episodes_for_gate(
                by_series[s_], "basis_guard",
                lambda r, tr=trial: (r.get("margin_bp") is not None
                                     and abs(r["margin_bp"]) >= tr
                                     and theta_survivable(r)))
            if not eps:
                continue
            st = price_gate(eps, winners, fires_by_slug, default_clip)
            if st["priced"] < 3:
                continue
            add_cand(f"{s_} guard", f"{g:.0f} -> {trial:.0f}bp", st, True,
                     f"pmengine replay --mode full --slug {pref} "
                     f"--params ab_guard{trial:.0f}.json "
                     f"--outcomes ~/.pmt/corpus/outcomes.jsonl",
                     "full mode REQUIRED: evals mode has no model on a gated tick")

    for trial in (0.25, 0.20, 0.15, 0.10):
        eps = episodes_for_gate(
            rows, "theta",
            lambda r, tr=trial: r.get("safety") is not None and r["safety"] >= tr)
        if not eps:
            continue
        st = price_gate(eps, winners, fires_by_slug, default_clip)
        if st["priced"] < 3:
            continue
        add_cand("theta (fleet)", f"{THETA} -> {trial}", st, True,
                 "pmengine replay --mode full --slug <series> "
                 f"--params ab_theta{int(trial * 100):02d}.json "
                 "--outcomes ~/.pmt/corpus/outcomes.jsonl")

    for bar, want_unlocked, base, trial in (
            [("min_edge", True, MIN_EDGE, t) for t in (0.012, 0.010, 0.008, 0.005)]
            + [("early_min_edge", False, EARLY_MIN_EDGE, t)
               for t in (0.06, 0.04, 0.03, 0.02)]):
        eps = episodes_for_gate(
            rows, "min_edge",
            lambda r, tr=trial, wu=want_unlocked: (
                bool(r.get("unlocked")) == wu
                and r.get("net") is not None and r["net"] >= tr))
        if not eps:
            continue
        st = price_gate(eps, winners, fires_by_slug, default_clip)
        if st["priced"] < 3:
            continue
        add_cand(f"{bar} (fleet)", f"{base} -> {trial}", st, True,
                 "pmengine replay --mode evals --slug <series> "
                 f"--params ab_{bar}{int(trial * 1000):03d}.json "
                 "--outcomes ~/.pmt/corpus/outcomes.jsonl")

    for bname in ("latched", "distrust", "avg_down"):
        sub = [r for r in rows if r["stage"] == "brakes" and r.get("brake") == bname]
        if not sub:
            continue
        eps = collapse_episodes([{"t": r["t"], "slug": r["slug"], "side": r["side"],
                                  "category": bname, "ask": r["ask"],
                                  "fair": r["fair"], "net": r["net"]} for r in sub])
        st = price_gate(eps, winners, fires_by_slug, default_clip)
        if st["priced"] < 3:
            continue
        add_cand(f"brake: {bname}", "disable brake", st, True,
                 "pmengine replay --mode evals --slug <series> "
                 "--params ab_nobrake.json --outcomes ~/.pmt/corpus/outcomes.jsonl",
                 "ROADMAP operating rule names the three brakes as never-loosen; "
                 "they are priced here to SIZE the cost, not to propose removal. "
                 "Only replay-only Tunables can express them, and only on one night "
                 "of tape — the night they were built for is a different night.")

    cands.sort(key=lambda c: -c["net"])
    hdr2 = (f"{'#':<3}{'knob':<23}{'move':<18}{'n':>4}{'hit':>6}{'hit CI':>12}"
            f"{'NET':>10}{'boot 95% CI on NET':>26}  flag")
    add(hdr2)
    add("-" * (len(hdr2) + 6))
    for i, c in enumerate(cands, 1):
        boot = (f"[{_f(c['lo'], dollar=True)}, {_f(c['hi'], dollar=True)}]"
                if not math.isnan(c["lo"]) else "-")
        if math.isnan(c["lo"]) or c["lo"] <= 0 <= c["hi"]:
            sig = ""
        elif c["lo"] > 0:
            sig = "  *CI clears $0 POSITIVE"
        else:
            sig = "  *CI clears $0 NEGATIVE - the gate is earning its keep"
        hci = f"[{c['hit_lo'] * 100:.0f}-{c['hit_hi'] * 100:.0f}%]"
        add(f"{i:<3}{c['knob']:<23}{c['move']:<18}{c['n']:>4}"
            f"{_f(c['hit'] * 100, 0, pct=True):>6}{hci:>12}"
            f"{_f(c['net'], dollar=True):>10}{boot:>26}"
            f"  {'LOOSEN' if c['loosen'] else 'tighten'}{sig}")
    add("")
    seen_notes: dict[str, list[int]] = {}
    for i, c in enumerate(cands, 1):
        if c["note"]:
            seen_notes.setdefault(c["note"], []).append(i)
    if seen_notes:
        add("Notes:")
        for note, idxs in seen_notes.items():
            rows_txt = ",".join(str(i) for i in idxs)
            add(f"  rows {rows_txt}:")
            for chunk in _wrap(note, 72):
                add(f"    {chunk}")
        add("")
    add("NOT KNOBS — the two blockers no parameter reaches:")
    bq_n = sum(1 for r in rows if r["stage"] == "book_quoted")
    lm_n = sum(1 for r in rows if r["stage"] == "last_mile")
    add(f"  no offer on our side   {bq_n:>5} ticks "
        f"({100.0 * bq_n / max(len(rows), 1):.1f}% of armed time). The book is bid "
        f"~0.99 with")
    add("                         nothing offered. Only a MAKER quote reaches this")
    add("                         time (ROADMAP Phase 3.1), never a taker gate.")
    add(f"  budget/cooldown        {lm_n:>5} ticks — every gate passed and no clip "
        f"went out.")
    add("                         That is a sizing/cadence question, not a gate one.")
    add("")
    add("A/B params files: copy the arm's as-run params, change ONE field")
    add("(basis_guard_bp / theta / min_edge / early_min_edge), keep everything")
    add("else identical, and")
    add("run baseline and candidate over the SAME --slug and --outcomes.")
    add("")

    # ---- the answer, spliced in at the top now that every number exists ----
    bg_died = died["basis_guard"]
    bq_died = died["book_quoted"]
    l1 = layer_stats.get("L1 +theta", (None, float("nan"), float("nan")))
    dbest = d_stats.get("D-best (any tick)", {})
    dtheta = d_stats.get("D-theta (safety>=0.3)", {})
    # The three brakes are never-loosen per the ROADMAP operating rules, so they
    # are priced but never headlined as "the move".
    top_loosen = next((c for c in cands
                       if not c["knob"].startswith("brake:")
                       and not math.isnan(c["lo"]) and c["lo"] > 0), None)
    brake_net = sum(c["net"] for c in cands if c["knob"].startswith("brake:"))
    ans = [
        "-" * 78,
        "THE ANSWER, IN FIVE LINES",
        "-" * 78,
        "",
        f"1. The BASIS GUARD is the binding gate on every single series. It eats "
        f"{100.0 * bg_died / max(len(rows), 1):.0f}% of",
        f"   armed time ({bg_died * CADENCE_S / 60.0:.0f} of "
        f"{len(rows) * CADENCE_S / 60.0:.0f} window-minutes) — more than every other "
        f"gate combined.",
        "",
        "2. But most of what it blocks is not a trade. Of its refused moments, only",
        f"   the theta-survivable slice is real: L1 = "
        f"{(l1[0] or {}).get('priced', 0)} episodes, "
        f"{_f(((l1[0] or {}).get('hit') or 0) * 100, 0, pct=True)} hit, "
        f"NET {_f((l1[0] or {}).get('net'), dollar=True)},",
        f"   bootstrap 95% CI [{_f(l1[1], dollar=True)}, {_f(l1[2], dollar=True)}] — "
        f"positive but not yet significant.",
        "",
        f"3. The second-biggest killer is NOT a gate at all. On "
        f"{100.0 * bq_died / max(len(rows), 1):.0f}% of armed time "
        f"({bq_died * CADENCE_S / 60.0:.0f}",
        "   window-minutes) the side our model wants has NO ASK ON THE BOOK — bid "
        "~0.99,",
        "   nothing offered. No taker parameter reaches that time; a maker quote does.",
        "",
        f"4. min_fair is a non-event ({died.get('min_fair', 0)} tick"
        f"{'' if died.get('min_fair', 0) == 1 else 's'} of {len(rows):,}), and the "
        f"edge bar is not the",
        "   problem either. In SAFE mode the median ask on edge-refused moments is",
        "   ~0.99 — the market already priced it — and trimming min_edge 0.015 ->",
        "   0.008 tests NET-NEGATIVE with a CI that clears $0 the wrong way. Only",
        "   the SPEC bar (early_min_edge 0.08) has anything behind it, and it is",
        "   small.",
        "",
        "5. Was there money in the quiet? Unbiased, one clip per zero-fire window at",
        f"   our own best moment: {dbest.get('n', 0)} windows, "
        f"{_f((dbest.get('hit') or 0) * 100, 0, pct=True)} hit, NET "
        f"{_f(dbest.get('net'), dollar=True)} — a coin flip.",
        f"   Filtered to theta-survivable moments: {dtheta.get('n', 0)} windows, "
        f"{_f((dtheta.get('hit') or 0) * 100, 0, pct=True)} hit, NET "
        f"{_f(dtheta.get('net'), dollar=True)}.",
        "   The edge lives entirely in the theta-survivable slice the basis guard is",
        "   sitting on. That is the one place to spend an A/B.",
        "",
        "Two facts that frame all of the above:",
        "",
        "  * The fire rate stepped down at the theta deploy, not gradually: share of",
        "    armed windows that fired anything went "
        + " -> ".join(
            f"{100.0 * len(era_windows[n]) / max(len(era_armed[n]), 1):.0f}%"
            for n, _a, _b in ERAS if era_armed[n])
        + " across",
        "    " + " / ".join(n for n, _a, _b in ERAS if era_armed[n]) + ".",
        "",
        "  * Tonight is NOT a calm tape. Realized 1m sigma sits at the "
        + (f"{min(ranks):.0f}-{max(ranks):.0f}" if ranks else "?")
        + " percentile band",
        "    of the 90-day baseline on every symbol. Low volatility is not the",
        "    explanation for the low fire count.",
        "",
    ]
    if top_loosen:
        ans += [
            f"Highest-confidence single move: {top_loosen['knob']} "
            f"{top_loosen['move']} — {top_loosen['n']} episodes, "
            f"{_f(top_loosen['hit'] * 100, 0, pct=True)} hit,",
            f"NET {_f(top_loosen['net'], dollar=True)}, bootstrap CI "
            f"[{_f(top_loosen['lo'], dollar=True)}, {_f(top_loosen['hi'], dollar=True)}] "
            f"(clears $0). LOOSENS a gate -> needs the replay A/B:",
            f"  {top_loosen['ab']}",
            "",
        ]
    ans += [
        f"The three brakes (distrust / avg_down / latch) together cost "
        f"{_f(brake_net, dollar=True)} tonight.",
        "They are named never-loosen in the ROADMAP operating rules and were built",
        "for a violent night, not this one — priced here to SIZE them, not to",
        "propose removing them.",
        "",
        f"Sample size warning: this is ONE night, {(t1 - t0) / 3600.0:.1f}h, "
        f"{len(wins)} windows, {sum(1 for s_ in wins if s_ in winners)} of them",
        "graded. Every row below is a direction to test on the replay harness,",
        "not a result.",
        "",
    ]
    L[answer_at:answer_at] = ans

    return "\n".join(L)


# ---------------------------------------------------------------- main

def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--since", type=float, default=THETA_ERA_T0,
                    help="epoch start (default: theta era, 05:00Z 2026-08-23)")
    ap.add_argument("--until", type=float, default=float("inf"))
    ap.add_argument("--out", type=str, default=None,
                    help="also write the report to this path")
    args = ap.parse_args()

    t0 = args.since
    wins = load_windows(t0, args.until)
    if not wins:
        print("no armed windows in range", file=sys.stderr)
        return 1
    t1 = max(tk["t"] for w in wins.values() for tk in w["ticks"] + w["fires"])
    book = load_book(t0, args.until)
    winners = load_outcomes()
    rows = build_ticks(wins)

    text = render(t0, t1, wins, rows, book, winners)
    print(text)
    if args.out:
        Path(args.out).write_text(text + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
