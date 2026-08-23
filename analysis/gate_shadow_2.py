#!/usr/bin/env python3
"""Shadow accounting of the gate stack, re-run on the CURRENT regime.

The first shadow read (`pmt crypto shadow` / `stats --gates`,
polymarket/shadow.py) priced four coarse buckets — basis_guard, the named
brakes, one lumped `sub_threshold`, unfilled_fires — over a 6h window many
policy changes ago. This driver re-runs it on the stream era and on today's
posture, and fixes three things the coarse ledger gets wrong:

  1. **Terminal outcomes only.** shadow.py grades off `merge_outcomes()`,
     which happily hands back a `chainlink` or `book` label — OUR read of
     the settlement stream, which the corpus itself forbids for W-L
     (outcomes.py's TERMINAL_SOURCES). Here a window is priced only when
     the EXCHANGE said so: a wallet redeem or a gamma resolution. Every
     other window lands in the coverage note.

  2. **Mode-correct thresholds.** shadow.py tests every eval side against
     the constants 0.97/0.015 — the SAFE-mode bar. A speculative-mode side
     (before the budget unlocks) is actually judged at 0.55/0.08, so the
     coarse ledger mislabels early sides in both directions: it calls a
     fair-0.80 spec side "sub_threshold" when min_edge is what stopped it,
     and it DROPS a fair-0.98/net-0.05 spec side as mid-band noise when
     the 0.08 early bar is precisely what refused it.

  3. **The gates shadow.py cannot see at all.** An unbraked side that
     cleared fair and edge and still didn't fire is dropped as noise by
     `categorize_ticks`. That is where quiesce (rho < rho_block), the
     0.985 price ceiling, and the budget/room floor live — three real
     refusal classes with no line in the old ledger.

ATTRIBUTION is read straight off decide() in updown.rs, in ITS order, so
each refused side is charged to the FIRST gate that would have stopped it:

    brake (safety=theta / distrust / avg_down / latched / fleet)
      -> chop         !unlocked && rho < rho_block   (decide()'s chop_blocked)
      -> min_fair     fair < fair_req
      -> min_edge     net  < edge_req
      -> price_cap    ask  > max_price
      -> budget       sized(room) < 5 shares
      -> cooldown     residual: clip_cooldown_s / inflight (not on the tape)

Window-level gates short-circuit the eval loop entirely and arrive as their
own `gated` records: basis_guard, feed_stale, reference_wait.

TWO GATES ARE INVISIBLE TO THIS (and to any tape study), because decide()
returns from them BEFORE it writes anything to tape_out:

  * `quiesce_secs` — the final 20s of every window. Orders pulled, no new
    taker clips except the flip-proof carve-out. It leaves `last_eval`
    state=quiesce and pushes NO tape record, so the last 6.7% of a 5m
    window is a hole in every refusal ledger. Called out in the report as a
    coverage gap rather than scored as zero.
  * `min_elapsed_frac` — the retired clock gate. Same silent return; 0.0 on
    every live arm today, so it binds on nothing.

PRICING is shadow.py's, unchanged, so the two ledgers are comparable:
ticks sharing (slug, side, family) collapse into episodes on a 20s gap, each
episode prices as ONE clip at its best (lowest) recorded ask, a win pays
$1/share net of the taker fee and a loss forfeits the clip. Net shadow P&L
is missed wins MINUS avoided losses, always both halves.

CLIP SIZING is the window's own median real fire notional; a window that
fired nothing falls back to the median for its (symbol, duration, era)
and then to the arm's `clip_usdc` from arms-state. It never invents size.

Read-only over ~/.pmt: run against frozen copies (--tape/--outcomes/
--activity). No network.
"""
from __future__ import annotations

import argparse
import collections
import datetime
import json
import math
import statistics
import sys

# --- engine constants, cited to their definitions ------------------------
EARLY_MIN_FAIR = 0.55        # updown.rs EARLY_MIN_FAIR
LATE_REM_S = 120.0           # every live arm's late_rem_s
RHO_BLOCK = -0.25            # every live arm's rho_block
MAX_PRICE = 0.985            # every live arm's max_price
MIN_EDGE = 0.015             # every live arm's min_edge
EARLY_MIN_EDGE = 0.08        # every live arm's early_min_edge
EARLY_FRAC = 0.2             # every live arm's early_frac
MIN_SHARES = 5.0             # decide(): `sized(r)` needs r > 5.0 and size >= 5.0
FEE_RATE = 0.07              # constants.FEE_RATE
EPISODE_GAP_S = 20.0         # shadow.EPISODE_GAP_S

# Era boundaries this study cites (polymarket/eras.py).
STREAM_ERA = 1787484570.0    # e296336 11:29:30Z — arms can read the settlement stream
POSTURE = 1787517900.0       # 20:45Z — today's posture (guards 6/8/16, sol g10, 15m at $1)

# min_fair by (duration, era). The 5m fleet has run the CLI default 0.97 all
# day (arms-state.json, and no 5m fire on the tape prices below it — the
# --self-check assertion proves it). The 15m arms were re-armed at 17:00Z
# with min_fair 1.0 / theta 1.0 / size $1, which is a deliberate shut, not a
# gate reading: FIFTEEN_SHUT_AT is that moment.
FIFTEEN_SHUT_AT = 1787504400.0   # 17:00Z re-arm — btc/eth/sol-updown-15m-1787504400

WINDOW_GATES = ("basis_guard", "feed_stale", "reference_wait")
SIDE_GATES = ("theta", "distrust", "avg_down", "latch", "fleet",
              "chop", "min_fair", "min_edge", "price_cap", "budget", "cooldown")
FAMILY_ORDER = (*WINDOW_GATES, *SIDE_GATES, "unfilled_fires")

BRAKE_FAMILY = {"safety": "theta", "distrust": "distrust", "avg_down": "avg_down",
                "latched": "latch", "fleet": "fleet"}


def utc(t: float, fmt: str = "%H:%M:%S") -> str:
    return datetime.datetime.fromtimestamp(t, datetime.timezone.utc).strftime(fmt) + "Z"


def parse_slug(slug: str):
    """(symbol, duration_label, start, end) from `<coin>-updown-<N>m-<start>`."""
    try:
        coin, _updown, dur, start = slug.split("-")
        mins = int(dur.rstrip("m"))
        start = float(start)
        return coin, dur, start, start + mins * 60
    except (ValueError, AttributeError):
        return None


def taker_fee(ask: float) -> float:
    """Per-share fee at the live rate — constants.taker_fee."""
    return FEE_RATE * min(ask, 1.0 - ask)


def shadow_value(ask: float, clip: float, won: bool) -> float:
    """shadow.shadow_value: a win pays $1/share net of fee, a loss forfeits."""
    if won:
        return (clip / ask) * (1.0 - ask - taker_fee(ask))
    return -clip


# ---------------------------------------------------------------- loading

def load_tape(path: str) -> list[dict]:
    out = []
    with open(path) as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                r = json.loads(line)
            except ValueError:
                continue
            if isinstance(r, dict) and r.get("t") is not None and r.get("slug"):
                out.append(r)
    out.sort(key=lambda r: r["t"])
    return out


def load_winners(path: str) -> dict[str, str]:
    """{slug: winner} from TERMINAL sources only — the exchange's own answer.

    A chainlink/book row is our own read of settlement and never grades a
    counterfactual here (outcomes.py's never-grade-yourself rule); those
    windows surface as coverage, not as a silent zero.
    """
    winners = {}
    with open(path) as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            r = json.loads(line)
            if r.get("source") in ("wallet", "resolution") and r.get("winner"):
                winners[r["slug"]] = r["winner"]
    return winners


def load_arms(path: str) -> dict[tuple[str, str], dict]:
    """{(symbol, duration): params} from the live arm store — the authority on
    today's posture (clip, guard, theta, min_fair, size)."""
    try:
        blob = json.load(open(path))
    except (OSError, ValueError):
        return {}
    out = {}
    for a in blob.get("arms", []):
        p = parse_slug(a.get("slug", ""))
        if p:
            out[(p[0], p[1])] = a
    return out


# --------------------------------------------------------------- sizing

def roll_sizes(tape: list[dict]) -> dict[str, float]:
    """{slug: size_usdc} off the `roll` records — the arm's own budget for that
    window, recorded when the roll chain armed it."""
    return {r["slug"]: r["size"] for r in tape
            if r.get("ev") == "roll" and r.get("size") is not None}


def size_for(slug: str, rolls: dict[str, float], arms: dict, fallback: dict) -> float | None:
    if slug in rolls:
        return rolls[slug]
    p = parse_slug(slug)
    if not p:
        return None
    arm = arms.get((p[0], p[1]))
    if arm:
        return arm.get("size_usdc")
    return fallback.get((p[0], p[1]))


def clip_table(tape: list[dict]) -> dict[tuple[str, str], float]:
    """{(symbol, duration): median real single-fire notional} — the clip the
    arm actually used, measured rather than assumed."""
    by = collections.defaultdict(list)
    for r in tape:
        if r.get("ev") != "fire" or not r.get("size") or not r.get("ask"):
            continue
        p = parse_slug(r["slug"])
        if p:
            by[(p[0], p[1])].append(r["size"] * r["ask"])
    return {k: statistics.median(v) for k, v in by.items()}


# ------------------------------------------------------- gate attribution

def side_family(rec: dict, side: dict, size_usdc: float | None,
                min_fair: float, theta_hint: float | None) -> tuple[str, dict]:
    """The FIRST gate in decide()'s order that refused this eval side, plus the
    numbers behind the call. Returns ("fired", ...) when nothing refused it.

    Everything except `budget` and `cooldown` is exact: the brakes are on the
    record, and quiesce/min_fair/min_edge/price_cap are the same comparisons
    decide() makes against numbers the record carries. `budget` reconstructs
    room as `cap - committed` (it cannot see inflight or resting notional, so
    it UNDER-counts room and is a lower bound on how often budget binds);
    `cooldown` is the residual — clip_cooldown_s and the inflight set are the
    only two blockers the tape does not record at all.
    """
    t, slug = rec["t"], rec["slug"]
    p = parse_slug(slug)
    end = p[3] if p else t
    banked = bool(rec.get("banked_decided"))
    unlocked = (end - t) <= LATE_REM_S or banked
    fair_req = min_fair if unlocked else EARLY_MIN_FAIR
    edge_req = MIN_EDGE if unlocked else EARLY_MIN_EDGE
    ask, fair, net = side.get("ask"), side.get("fair"), side.get("net")
    ctx = {"unlocked": unlocked, "fair_req": fair_req, "edge_req": edge_req,
           "banked_decided": banked, "safety": side.get("safety"),
           "rho": rec.get("rho"), "theta_hint": theta_hint}

    brake = side.get("brake")
    if brake in BRAKE_FAMILY:
        return BRAKE_FAMILY[brake], ctx
    if ask is None or fair is None or net is None:
        # maker-slice record (no ask on this side at all) — no taker refusal
        # to price. Its own study is analysis/maker_grading.md.
        return "no_ask", ctx
    rho = rec.get("rho")
    if not unlocked and rho is not None and rho < RHO_BLOCK:
        return "chop", ctx
    if fair < fair_req:
        return "min_fair", ctx
    if net < edge_req:
        return "min_edge", ctx
    if ask > MAX_PRICE:
        return "price_cap", ctx
    if size_usdc is not None:
        cap = size_usdc * (1.0 if unlocked else EARLY_FRAC)
        room = cap - (rec.get("committed") or 0.0)
        if room <= MIN_SHARES or math.floor(room / ask) < MIN_SHARES:
            ctx["room"] = room
            return "budget", ctx
    return "cooldown", ctx


def window_family(reason: str) -> str | None:
    if reason.startswith("basis guard"):
        return "basis_guard"
    if reason.startswith("feed stale"):
        return "feed_stale"
    if reason.startswith("range-start reference"):
        return "reference_wait"
    return None


def guard_side(margin_bp: float | None) -> str | None:
    """shadow.basis_guard_side: the side the projected margin favours."""
    if margin_bp is None:
        return None
    return "up" if margin_bp >= 0 else "down"


# ------------------------------------------------------------ tick -> episode

def build_ticks(tape: list[dict], since: float, rolls, arms, size_fallback,
                 fired_index: set) -> list[dict]:
    """One tick per refused side (or refused window) at or after `since`."""
    ticks = []
    for r in tape:
        t = r["t"]
        if t < since:
            continue
        slug = r["slug"]
        p = parse_slug(slug)
        if not p:
            continue
        sym, dur, start, end = p
        phase = (t - start) / max(end - start, 1e-9)
        if r.get("ev") == "gated":
            fam = window_family(r.get("reason") or "")
            if fam is None:
                continue
            if fam == "basis_guard":
                side = guard_side(r.get("margin_bp"))
                if side is None:
                    continue
                ask = r.get("up_ask") if side == "up" else r.get("dn_ask")
                dist = None
                if r.get("guard_bp") is not None and r.get("margin_bp") is not None:
                    dist = r["guard_bp"] - abs(r["margin_bp"])
                ticks.append({"t": t, "slug": slug, "sym": sym, "dur": dur, "phase": phase,
                              "side": side, "family": fam, "ask": ask,
                              "margin_bp": r.get("margin_bp"), "guard_bp": r.get("guard_bp"),
                              "banked_bp": r.get("banked_bp"), "cushion_bp": r.get("cushion_bp"),
                              "dist_bp": dist, "spot_age_s": r.get("spot_age_s")})
            else:
                # A feed/reference gate refuses BOTH sides — the arm is blind,
                # it has no direction. Price the cheaper side: the best the
                # window could have done had the gate not held.
                for side, ask in (("up", r.get("up_ask")), ("down", r.get("dn_ask"))):
                    ticks.append({"t": t, "slug": slug, "sym": sym, "dur": dur, "phase": phase,
                                  "side": side, "family": fam, "ask": ask,
                                  "spot_age_s": r.get("spot_age_s")})
        elif r.get("ev") == "eval":
            size_usdc = size_for(slug, rolls, arms, size_fallback)
            arm = arms.get((sym, dur)) or {}
            min_fair = arm.get("min_fair", 0.97)
            if dur == "15m" and start < FIFTEEN_SHUT_AT:
                min_fair = 0.97   # before the 17:00Z shut, 15m ran the fleet default
            for s in r.get("sides") or []:
                if s.get("side") not in ("up", "down"):
                    continue
                fam, ctx = side_family(r, s, size_usdc, min_fair, arm.get("theta"))
                if fam in ("fired", "no_ask"):
                    continue
                side = "down" if s["side"] == "down" else "up"
                if fam == "cooldown" and (round(t, 3), slug, side) in fired_index:
                    continue      # it DID fire on this tick — not a refusal
                ticks.append({"t": t, "slug": slug, "sym": sym, "dur": dur, "phase": phase,
                              "side": side, "family": fam, "ask": s.get("ask"),
                              "fair": s.get("fair"), "net": s.get("net"),
                              "safety": s.get("safety"), "rho": r.get("rho"),
                              "margin_bp": r.get("margin_bp"), "guard_bp": r.get("guard_bp"),
                              "cushion_bp": r.get("cushion_bp"), "banked_bp": r.get("banked_bp"),
                              "size_usdc": size_usdc, **{"edge_req": ctx["edge_req"],
                                                          "fair_req": ctx["fair_req"],
                                                          "unlocked": ctx["unlocked"]}})
    return ticks


def collapse(ticks: list[dict], gap_s: float = EPISODE_GAP_S) -> list[dict]:
    """shadow.collapse_episodes, keyed on (slug, side, family)."""
    by = collections.defaultdict(list)
    for tk in ticks:
        by[(tk["slug"], tk["side"], tk["family"])].append(tk)
    eps = []
    for (slug, side, fam), tks in by.items():
        tks.sort(key=lambda x: x["t"])
        run = [tks[0]]
        for tk in tks[1:]:
            if tk["t"] - run[-1]["t"] > gap_s:
                eps.append(_finish(slug, side, fam, run))
                run = [tk]
            else:
                run.append(tk)
        eps.append(_finish(slug, side, fam, run))
    return eps


def _finish(slug, side, fam, tks) -> dict:
    asks = [t["ask"] for t in tks if t.get("ask") is not None]
    dists = [t["dist_bp"] for t in tks if t.get("dist_bp") is not None]
    return {
        "slug": slug, "side": side, "family": fam,
        "start": tks[0]["t"], "end": tks[-1]["t"], "n_ticks": len(tks),
        "sym": tks[0]["sym"], "dur": tks[0]["dur"],
        "best_ask": min(asks) if asks else None,
        "phase": tks[0]["phase"],
        # The CLOSEST the window ever came to clearing its guard in this run —
        # the number a 1bp relaxation would have had to beat.
        "min_dist_bp": min(dists) if dists else None,
        "last": tks[-1],
    }


def price(ep: dict, winners: dict, clip: float) -> dict:
    if ep["best_ask"] is None:
        return {**ep, "status": "unpriced", "clip": clip, "won": None, "pnl": None}
    w = winners.get(ep["slug"])
    if w is None:
        return {**ep, "status": "unresolved", "clip": clip, "won": None, "pnl": None}
    won = ep["side"] == w
    return {**ep, "status": "priced", "clip": clip, "won": won,
            "pnl": shadow_value(ep["best_ask"], clip, won)}


# ------------------------------------------------------------- unfilled fires

def unfilled(tape: list[dict], activity: list[dict], winners: dict,
              since: float) -> list[dict]:
    """shadow.unfilled_episodes: the gap between intended fire notional and
    what the wallet shows actually filled, per (slug, side)."""
    fills = collections.defaultdict(float)
    for a in activity:
        if a.get("type") != "TRADE" or a.get("side") != "BUY":
            continue
        side = (a.get("outcome") or "").lower()
        if side not in ("up", "down") or not a.get("slug"):
            continue
        fills[(a["slug"], side)] += a.get("usdcSize") or 0.0

    by = collections.defaultdict(list)
    for r in tape:
        if r.get("ev") != "fire" or r["t"] < since:
            continue
        if r.get("side") in ("up", "down") and r.get("ask") and r.get("size"):
            by[(r["slug"], r["side"])].append(r)

    eps = []
    for (slug, side), fs in by.items():
        shares = sum(f["size"] for f in fs)
        notional = sum(f["size"] * f["ask"] for f in fs)
        if shares <= 0:
            continue
        avg = notional / shares
        filled_sh = fills[(slug, side)] / avg if avg else 0.0
        gap_sh = max(0.0, shares - filled_sh)
        p = parse_slug(slug)
        ep = {"slug": slug, "side": side, "family": "unfilled_fires",
              "start": min(f["t"] for f in fs), "end": max(f["t"] for f in fs),
              "n_ticks": len(fs), "sym": p[0], "dur": p[1], "best_ask": avg,
              "phase": (min(f["t"] for f in fs) - p[2]) / max(p[3] - p[2], 1e-9),
              "min_dist_bp": None, "last": fs[-1],
              "intended": notional, "filled": fills[(slug, side)],
              "hit_rate": min(1.0, fills[(slug, side)] / notional) if notional else None,
              "paid_up": any((f.get("limit") or f["ask"]) > f["ask"] + 1e-9 for f in fs)}
        if gap_sh <= 1e-9:
            eps.append({**ep, "status": "filled", "clip": 0.0, "won": None, "pnl": 0.0})
            continue
        eps.append(price(ep, winners, gap_sh * avg))
    return eps


# ------------------------------------------------------------------ rollups

def rollup(eps: list[dict], key=lambda e: e["family"]) -> dict:
    out = {}
    for e in eps:
        k = key(e)
        s = out.setdefault(k, {"episodes": 0, "priced": 0, "unresolved": 0, "unpriced": 0,
                                "wins": 0, "losses": 0, "missed": 0.0, "avoided": 0.0,
                                "ticks": 0})
        s["episodes"] += 1
        s["ticks"] += e.get("n_ticks", 0)
        st = e["status"]
        if st == "unpriced":
            s["unpriced"] += 1
        elif st == "unresolved":
            s["unresolved"] += 1
        elif st == "priced":
            s["priced"] += 1
            if e["won"]:
                s["wins"] += 1
                s["missed"] += e["pnl"]
            else:
                s["losses"] += 1
                s["avoided"] += -e["pnl"]
    for s in out.values():
        s["net"] = s["missed"] - s["avoided"]
        s["hit"] = s["wins"] / s["priced"] if s["priced"] else None
    return out


def fam_sort(name: str) -> int:
    return FAMILY_ORDER.index(name) if name in FAMILY_ORDER else len(FAMILY_ORDER)


def table(rows: dict, hours: float, title: str, keyname: str = "gate") -> str:
    """missed / avoided / net per family, with net-per-hour."""
    lines = [f"### {title}", "",
             f"| {keyname} | episodes | priced | refused-side hit | missed wins | "
             f"avoided losses | net | net/h |",
             "|---|---:|---:|---:|---:|---:|---:|---:|"]
    tot = {"episodes": 0, "priced": 0, "missed": 0.0, "avoided": 0.0, "wins": 0}
    for k in sorted(rows, key=lambda x: fam_sort(x) if keyname == "gate" else str(x)):
        s = rows[k]
        tot["episodes"] += s["episodes"]; tot["priced"] += s["priced"]
        tot["missed"] += s["missed"]; tot["avoided"] += s["avoided"]; tot["wins"] += s["wins"]
        hit = f"{s['hit']*100:.0f}%" if s["hit"] is not None else "—"
        lines.append(f"| {k} | {s['episodes']} | {s['priced']} | {hit} | "
                     f"${s['missed']:,.2f} | ${s['avoided']:,.2f} | "
                     f"${s['net']:+,.2f} | ${s['net']/hours:+,.2f} |")
    net = tot["missed"] - tot["avoided"]
    hit = f"{tot['wins']/tot['priced']*100:.0f}%" if tot["priced"] else "—"
    lines.append(f"| **TOTAL** | {tot['episodes']} | {tot['priced']} | {hit} | "
                 f"${tot['missed']:,.2f} | ${tot['avoided']:,.2f} | "
                 f"${net:+,.2f} | ${net/hours:+,.2f} |")
    lines.append("")
    return "\n".join(lines)


# --------------------------------------------------------------------- main

def analyse(tape, winners, activity, arms, since: float, label: str) -> dict:
    rolls = roll_sizes(tape)
    clips = clip_table(tape)
    size_fallback = {k: (arms.get(k) or {}).get("size_usdc") for k in clips}
    fired_index = {(round(r["t"], 3), r["slug"], r["side"]) for r in tape
                   if r.get("ev") == "fire"}

    ticks = build_ticks(tape, since, rolls, arms, size_fallback, fired_index)
    eps = collapse(ticks)

    # per-window real clip, else the (symbol,duration) measured clip, else the
    # live arm's clip_usdc. Never a guess.
    win_clip = collections.defaultdict(list)
    for r in tape:
        if r.get("ev") == "fire" and r.get("size") and r.get("ask"):
            win_clip[r["slug"]].append(r["size"] * r["ask"])
    priced = []
    for ep in eps:
        c = win_clip.get(ep["slug"])
        if c:
            clip, src = statistics.median(c), "window"
        elif (ep["sym"], ep["dur"]) in clips and not (
                ep["dur"] == "15m" and parse_slug(ep["slug"])[2] >= FIFTEEN_SHUT_AT):
            clip, src = clips[(ep["sym"], ep["dur"])], "symbol"
        else:
            arm = arms.get((ep["sym"], ep["dur"])) or {}
            clip, src = arm.get("clip_usdc", 25.0), "arm"
        p = price(ep, winners, clip)
        p["clip_src"] = src
        priced.append(p)

    priced.extend(unfilled(tape, activity, winners, since))
    last_t = max(r["t"] for r in tape)
    hours = (last_t - since) / 3600.0
    return {"label": label, "since": since, "hours": hours, "episodes": priced,
            "families": rollup([e for e in priced if e["family"] != "unfilled_fires"]),
            "unfilled": [e for e in priced if e["family"] == "unfilled_fires"],
            "all": rollup(priced)}


def self_check(tape, arms) -> tuple[list[str], int]:
    """Every real fire must satisfy the thresholds this study assumes. A
    violation means the params table is wrong, and the run says so instead of
    quietly mis-attributing thousands of sides.

    Fires with no `mode` field predate that field (the first hour of the tape,
    2026-08-22 22:0xZ) and are skipped rather than guessed at — they are
    outside every range this study reports on. `mode: "flip"` is the quiesce
    carve-out, which by construction bypasses min_fair and the early edge bar
    and is judged at `min_edge` alone (updown.rs's flip_live block).
    """
    bad, skipped = [], 0
    for r in tape:
        if r.get("ev") != "fire":
            continue
        p = parse_slug(r["slug"])
        if not p:
            continue
        sym, dur, start, _end = p
        mode = r.get("mode")
        if mode is None:
            skipped += 1
            continue
        arm = arms.get((sym, dur)) or {}
        min_fair = arm.get("min_fair", 0.97)
        if dur == "15m" and start < FIFTEEN_SHUT_AT:
            min_fair = 0.97
        if mode == "flip":
            fair_req, edge_req = 0.0, MIN_EDGE
        elif mode == "safe":
            fair_req, edge_req = min_fair, MIN_EDGE
        else:
            fair_req, edge_req = EARLY_MIN_FAIR, EARLY_MIN_EDGE
        if r["fair"] < fair_req - 1e-9:
            bad.append(f"{r['slug']} {utc(r['t'])} [{mode}] fair {r['fair']:.4f} < {fair_req}")
        if r["net"] < edge_req - 1e-9:
            bad.append(f"{r['slug']} {utc(r['t'])} [{mode}] net {r['net']:.4f} < {edge_req}")
        if r["ask"] > MAX_PRICE + 1e-9:
            bad.append(f"{r['slug']} {utc(r['t'])} [{mode}] ask {r['ask']} > {MAX_PRICE}")
    return bad, skipped


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--tape", default=".work/updown-tape-frozen.jsonl")
    ap.add_argument("--outcomes", default=".work/outcomes-frozen.jsonl")
    ap.add_argument("--activity", default=".work/activity.json")
    ap.add_argument("--arms", default=".work/arms-state.json")
    ap.add_argument("--json", dest="json_out", default=None,
                    help="dump the full per-episode ledger here")
    ap.add_argument("--self-check", action="store_true",
                    help="prove the assumed thresholds against every real fire, then stop")
    args = ap.parse_args()

    tape = load_tape(args.tape)
    winners = load_winners(args.outcomes)
    activity = json.load(open(args.activity))
    arms = load_arms(args.arms)

    bad, skipped = self_check(tape, arms)
    if args.self_check:
        print(f"fires checked: {sum(1 for r in tape if r.get('ev') == 'fire') - skipped}"
              f"  (skipped {skipped} pre-`mode` records)")
        print(f"threshold violations: {len(bad)}")
        for b in bad[:20]:
            print("  " + b)
        return 1 if bad else 0
    if bad:
        print(f"[warn] {len(bad)} fires violate the assumed thresholds "
              f"— run --self-check", file=sys.stderr)

    out = {"stream": analyse(tape, winners, activity, arms, STREAM_ERA, "stream era (11:29Z)"),
           "posture": analyse(tape, winners, activity, arms, POSTURE, "today's posture (20:45Z)")}
    if args.json_out:
        dump = {k: {"label": v["label"], "since": v["since"], "hours": v["hours"],
                    "families": v["families"],
                    "episodes": [{kk: vv for kk, vv in e.items() if kk != "last"}
                                 for e in v["episodes"]]}
                for k, v in out.items()}
        json.dump(dump, open(args.json_out, "w"), indent=1, default=str)

    for key in ("stream", "posture"):
        r = out[key]
        print(table(r["families"], r["hours"],
                    f"{r['label']} — {r['hours']:.1f}h, gate families"))
        print(table(rollup(r["unfilled"]), r["hours"],
                    f"{r['label']} — unfilled fires"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
