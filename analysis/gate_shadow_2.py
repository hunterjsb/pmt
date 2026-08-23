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
POSTURE = 1787517900.0       # 20:45Z — the mark the operator named
# The engine log dates the real boundaries: 17:31:11Z restart (15m re-armed at
# $1, sol/xrp maker bids live), and 20:25:50Z / 20:25:55Z when btc and eth
# joined xrp on rtds@60s. "20:45Z" sits INSIDE that last change, not at its
# start, and a 14-minute slice cannot carry a conclusion — hence three cuts.
REGIME_START = 1787506271.0  # 17:31:11Z engine restart
POSTURE_TRUE = 1787516750.0  # 20:25:50Z btc rtds@60 — first fully-rtds 5m fleet

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
    """One refusal run. Three entry prices, because the choice is load-bearing:

      first  — the ask at the moment the gate FIRST refused. This is the
               honest counterfactual for "the gate opened here and the clip
               went out", and it is the headline convention.
      median — the same run's typical price; the robustness check.
      best   — the lowest ask anywhere in the run. shadow.py's convention,
               kept for comparability, but it is a look-back-optimal entry:
               a basis-guard run spans a whole window (median 35s, p90 237s),
               and its cheapest tick is precisely the moment the book was
               most sure that side was wrong — the moment our model was most
               likely wrong with it. Treat `best` as the optimistic bound.
    """
    asks = [t["ask"] for t in tks if t.get("ask") is not None]
    dists = [t["dist_bp"] for t in tks if t.get("dist_bp") is not None]
    return {
        "slug": slug, "side": side, "family": fam,
        "start": tks[0]["t"], "end": tks[-1]["t"], "n_ticks": len(tks),
        "sym": tks[0]["sym"], "dur": tks[0]["dur"],
        "best_ask": min(asks) if asks else None,
        "first_ask": asks[0] if asks else None,
        "med_ask": statistics.median(asks) if asks else None,
        "phase": tks[0]["phase"],
        # The CLOSEST the window ever came to clearing its guard in this run —
        # the number a 1bp relaxation would have had to beat.
        "min_dist_bp": min(dists) if dists else None,
        "last": tks[-1],
    }


ASK_KEY = {"first": "first_ask", "median": "med_ask", "best": "best_ask"}


def price(ep: dict, winners: dict, clip: float, conv: str = "first") -> dict:
    ask = ep.get(ASK_KEY[conv])
    if ask is None:
        return {**ep, "status": "unpriced", "clip": clip, "won": None, "pnl": None}
    w = winners.get(ep["slug"])
    if w is None:
        return {**ep, "status": "unresolved", "clip": clip, "won": None, "pnl": None}
    won = ep["side"] == w
    out = {**ep, "status": "priced", "clip": clip, "won": won,
           "pnl": shadow_value(ask, clip, won), "entry_ask": ask}
    for c, k in ASK_KEY.items():
        out[f"pnl_{c}"] = (shadow_value(ep[k], clip, won)
                           if ep.get(k) is not None else None)
    return out


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
              "n_ticks": len(fs), "sym": p[0], "dur": p[1],
              # One price: the volume-weighted ask the fires actually chased.
              "best_ask": avg, "first_ask": avg, "med_ask": avg,
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
                                "ticks": 0, "net_best": 0.0, "net_median": 0.0})
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
            for c in ("best", "median"):
                v = e.get(f"pnl_{c}")
                if v is not None:
                    s[f"net_{c}"] += v
    for s in out.values():
        s["net"] = s["missed"] - s["avoided"]
        s["hit"] = s["wins"] / s["priced"] if s["priced"] else None
    return out


def fam_sort(name) -> int:
    return FAMILY_ORDER.index(name) if name in FAMILY_ORDER else len(FAMILY_ORDER)


def money(x: float) -> str:
    return f"${x:,.2f}" if x >= 0 else f"-${-x:,.2f}"


def table(rows: dict, hours: float, title: str, keyname: str = "gate",
           order=None, note: str | None = None) -> str:
    """missed / avoided / net per key, at the `first` convention, with the
    `best` (shadow.py) and `median` nets beside it so the entry-price choice
    is visible rather than assumed."""
    keys = order or sorted(rows, key=(fam_sort if keyname == "gate" else str))
    lines = [f"**{title}**", ""]
    if note:
        lines += [note, ""]
    lines += [f"| {keyname} | eps | priced | refused-side hit | missed wins | "
              f"avoided losses | **net** | net/h | net@median | net@best |",
              "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|"]
    tot = collections.Counter()
    fl = collections.defaultdict(float)
    for k in keys:
        s = rows.get(k)
        if not s:
            continue
        for f in ("episodes", "priced", "wins"):
            tot[f] += s[f]
        for f in ("missed", "avoided", "net_best", "net_median"):
            fl[f] += s[f]
        hit = f"{s['hit']*100:.0f}%" if s["hit"] is not None else "—"
        lines.append(f"| {k} | {s['episodes']} | {s['priced']} | {hit} | "
                     f"{money(s['missed'])} | {money(s['avoided'])} | "
                     f"**{money(s['net'])}** | {money(s['net']/hours)} | "
                     f"{money(s['net_median'])} | {money(s['net_best'])} |")
    net = fl["missed"] - fl["avoided"]
    hit = f"{tot['wins']/tot['priced']*100:.0f}%" if tot["priced"] else "—"
    lines.append(f"| **TOTAL** | {tot['episodes']} | {tot['priced']} | {hit} | "
                 f"{money(fl['missed'])} | {money(fl['avoided'])} | "
                 f"**{money(net)}** | {money(net/hours)} | "
                 f"{money(fl['net_median'])} | {money(fl['net_best'])} |")
    if keyname == "gate" and any(k in rows for k in BLIND_GATES):
        d = collections.Counter()
        df = collections.defaultdict(float)
        for k in keys:
            if k in BLIND_GATES or k not in rows:
                continue
            for f in ("episodes", "priced", "wins"):
                d[f] += rows[k][f]
            for f in ("missed", "avoided", "net_best", "net_median"):
                df[f] += rows[k][f]
        dnet = df["missed"] - df["avoided"]
        dhit = f"{d['wins']/d['priced']*100:.0f}%" if d["priced"] else "—"
        lines.append(f"| **DIRECTIONAL ONLY** | {d['episodes']} | {d['priced']} | {dhit} | "
                     f"{money(df['missed'])} | {money(df['avoided'])} | "
                     f"**{money(dnet)}** | {money(dnet/hours)} | "
                     f"{money(df['net_median'])} | {money(df['net_best'])} |")
        lines += ["", "_`feed_stale` and `reference_wait` are directionless — the arm was "
                  "blind, so the counterfactual has to price BOTH sides and its ~50% hit "
                  "rate is an artifact of that, not a reading. Their real unit is the "
                  "exposure table below; DIRECTIONAL ONLY is the line to read._"]
    lines.append("")
    return "\n".join(lines)


# ------------------------------------------------------- relaxation ladders

def guard_ladder(tape, winners, clip_of, since: float, arms,
                  steps=(0.5, 1.0, 1.5, 2.0, 3.0, 4.0, 6.0)) -> list[dict]:
    """What a basis-guard relaxation of X bp would actually have bought.

    NOT a re-pricing of the refusal run: the counterfactual fires at the FIRST
    tick whose |margin| clears the RELAXED guard (dist_bp = guard - |margin|
    < X), at that tick's own ask on that tick's own side. One clip per window
    per side, the same unit the live arm would have committed.

    The theta gate is applied on top, exactly: a gated record carries
    banked_bp and cushion_bp, so `side_safety` is computable and the R9 entry
    gate (safety < theta refuses the first clip) is reproduced rather than
    assumed away. min_fair/min_edge are NOT reproducible from a gated record
    (no p_up), so every figure here is an UPPER bound on what the relaxation
    buys — the fair/edge bar would refuse some of these clips downstream.
    """
    out = []
    for step in steps:
        # first qualifying tick per (slug, side)
        opened: dict[tuple[str, str], dict] = {}
        for r in tape:
            if r.get("ev") != "gated" or r["t"] < since:
                continue
            if not (r.get("reason") or "").startswith("basis guard"):
                continue
            g, m = r.get("guard_bp"), r.get("margin_bp")
            if g is None or m is None:
                continue
            if g - abs(m) >= step:
                continue
            side = guard_side(m)
            ask = r.get("up_ask") if side == "up" else r.get("dn_ask")
            if ask is None:
                continue
            p = parse_slug(r["slug"])
            if not p:
                continue
            arm = arms.get((p[0], p[1])) or {}
            theta = arm.get("theta", 0.3)
            safety = None
            if r.get("banked_bp") is not None and r.get("cushion_bp"):
                signed = r["banked_bp"] if side == "up" else -r["banked_bp"]
                safety = signed / max(r["cushion_bp"], 1e-9)
            key = (r["slug"], side)
            if key in opened:
                continue
            opened[key] = {"slug": r["slug"], "side": side, "ask": ask, "t": r["t"],
                           "safety": safety, "theta": theta, "sym": p[0], "dur": p[1],
                           "dist": g - abs(m), "margin_bp": m, "guard_bp": g}
        row = {"step": step, "n": 0, "priced": 0, "wins": 0, "missed": 0.0, "avoided": 0.0,
               "theta_n": 0, "theta_priced": 0, "theta_wins": 0,
               "theta_missed": 0.0, "theta_avoided": 0.0}
        for k, o in opened.items():
            row["n"] += 1
            w = winners.get(o["slug"])
            clip = clip_of(o["slug"], o["sym"], o["dur"])
            if w is None:
                continue
            won = o["side"] == w
            pnl = shadow_value(o["ask"], clip, won)
            row["priced"] += 1
            row["wins"] += won
            row["missed" if won else "avoided"] += pnl if won else -pnl
            if o["safety"] is not None and o["safety"] >= o["theta"]:
                row["theta_n"] += 1
                row["theta_priced"] += 1
                row["theta_wins"] += won
                row["theta_missed" if won else "theta_avoided"] += pnl if won else -pnl
        row["net"] = row["missed"] - row["avoided"]
        row["theta_net"] = row["theta_missed"] - row["theta_avoided"]
        out.append(row)
    return out


def side_ladder(ticks: list[dict], family: str, field: str, steps,
                 winners, clip_of, extra_ok=None) -> list[dict]:
    """First-crossing ladder for a side-level threshold gate.

    Same shape as `guard_ladder`, and the same reason: a relaxed threshold
    does not re-price the refusal run, it FIRES at the first tick the looser
    bar admits, at that tick's own ask. Taking the run's last (or best) tick
    instead would credit the relaxation with a price it could never have got.

    `extra_ok(tick)` is the downstream gate the relaxation does not lift —
    for min_fair that is min_edge, which still has to clear.
    """
    out = []
    for step in steps:
        opened: dict[tuple[str, str], dict] = {}
        for tk in ticks:
            if tk["family"] != family or tk.get(field) is None or tk.get("ask") is None:
                continue
            if tk[field] < step:
                continue
            if extra_ok is not None and not extra_ok(tk):
                continue
            opened.setdefault((tk["slug"], tk["side"]), tk)
        row = {"step": step, "n": 0, "priced": 0, "wins": 0, "missed": 0.0, "avoided": 0.0}
        for (slug, side), tk in opened.items():
            row["n"] += 1
            w = winners.get(slug)
            if w is None:
                continue
            won = side == w
            pnl = shadow_value(tk["ask"], clip_of(slug, tk["sym"], tk["dur"]), won)
            row["priced"] += 1
            row["wins"] += won
            row["missed" if won else "avoided"] += pnl if won else -pnl
        row["net"] = row["missed"] - row["avoided"]
        out.append(row)
    return out


def ladder_table(rows, title, label, extra=False) -> str:
    lines = [f"**{title}**", "",
             f"| {label} | opens | priced | hit | missed wins | avoided losses | net |"
             + (" | net after theta |" if extra else ""),
             "|---|---:|---:|---:|---:|---:|---:|" + ("---:|" if extra else "")]
    for r in rows:
        hit = f"{r['wins']/r['priced']*100:.0f}%" if r["priced"] else "—"
        line = (f"| {r['step']} | {r['n']} | {r['priced']} | {hit} | "
                f"{money(r['missed'])} | {money(r['avoided'])} | **{money(r['net'])}** |")
        if extra:
            line += f" {money(r['theta_net'])} ({r['theta_priced']} clips) |"
        lines.append(line)
    lines.append("")
    return "\n".join(lines)


# --------------------------------------------------------------- blind gates

BLIND_GATES = ("feed_stale", "reference_wait")


def blind_exposure(tape, since: float) -> str:
    """feed_stale and reference_wait are DIRECTIONLESS: the arm was blind, so
    it had no side to want. Pricing them as a counterfactual clip means
    pricing both sides, and both sides at these books is a coin flip by
    construction — which is exactly what the ledger shows (49-50% hit). The
    signed net there measures the book's two-sided cost, not a missed edge,
    so the honest unit for these two is EXPOSURE: windows touched, ticks
    held, and how deep into a window the gate was still holding.
    """
    rows = collections.defaultdict(lambda: collections.defaultdict(list))
    windows = collections.defaultdict(set)
    for r in tape:
        if r["t"] < since:
            continue
        p = parse_slug(r["slug"])
        if not p:
            continue
        windows[p[0]].add(r["slug"])
        if r.get("ev") != "gated":
            continue
        fam = window_family(r.get("reason") or "")
        if fam in BLIND_GATES:
            rows[fam][r["slug"]].append(r["t"])

    out = ["**blind gates — exposure, not P&L**", "",
           "| gate | symbol | windows touched | of armed | ticks | median last-tick "
           "(% elapsed) | windows held past 90% |", "|---|---|---:|---:|---:|---:|---:|"]
    writeoffs = []
    for fam in BLIND_GATES:
        per_sym = collections.defaultdict(list)
        for slug, ts in rows[fam].items():
            per_sym[parse_slug(slug)[0]].append((slug, ts))
        for sym in sorted(per_sym):
            hits = per_sym[sym]
            fracs = []
            deep = 0
            ticks = 0
            for slug, ts in hits:
                _s, _d, start, end = parse_slug(slug)
                frac = (max(ts) - start) / max(end - start, 1e-9)
                fracs.append(frac)
                ticks += len(ts)
                if frac > 0.90:
                    deep += 1
                    writeoffs.append((fam, slug, len(ts), frac))
            out.append(f"| {fam} | {sym} | {len(hits)} | {len(windows[sym])} | {ticks} | "
                       f"{statistics.median(fracs)*100:.0f}% | {deep} |")
    out.append("")
    if writeoffs:
        out += ["Windows a blind gate held past 90% elapsed — the quiesce boundary is "
                "93.3% on a 5m window, so these were shut for their whole tradeable "
                "life:", ""]
        for fam, slug, n, frac in sorted(writeoffs, key=lambda x: -x[3])[:12]:
            out.append(f"- `{slug}` — {fam}, {n} ticks, still gated at "
                       f"{frac*100:.0f}% elapsed")
        out.append("")
    return "\n".join(out)


def cold_feed_defect(tape, since: float) -> str:
    """`spot_age_s` is `now - f.spot_ts` (updown_model.rs eval_model). A feed
    that has NEVER printed leaves spot_ts at 0.0, so the field carries an
    absolute epoch instead of an age and the gate's prose collapses two
    different failures into one sentence: a feed that stopped, and a feed
    that never started. Counting the epoch-valued records isolates the
    second class."""
    cold = [r for r in tape if r.get("ev") == "gated" and r["t"] >= since
            and (r.get("spot_age_s") or 0) > 1e6]
    sane = [r.get("spot_age_s") for r in tape if r.get("ev") == "gated"
            and r["t"] >= since and 0 < (r.get("spot_age_s") or 0) <= 1e6]
    if not cold:
        return ""
    at_roll = sum(1 for r in cold if (r["t"] - parse_slug(r["slug"])[2]) < 30.0)
    syms = collections.Counter(parse_slug(r["slug"])[0] for r in cold)
    lines = ["**telemetry defect: `spot_age_s` carries an epoch on a never-printed feed**", "",
             f"- {len(cold)} gated ticks report `spot_age_s` > 1e6 (an absolute epoch, "
             f"not an age) — `now - spot_ts` with `spot_ts` still 0.0.",
             f"- {at_roll} of {len(cold)} land in the first 30s of a window: the roll "
             f"chain arms the next window before its feed thread's first print.",
             f"- symbols: {', '.join(f'{k} {v}' for k, v in syms.most_common())} "
             f"— every one of them binance-fed.",
             f"- the {len(sane)} well-formed readings median "
             f"{statistics.median(sane):.1f}s, p90 "
             f"{sorted(sane)[int(0.9*len(sane))]:.1f}s.", ""]
    return "\n".join(lines)


def guard_band(tape, since: float) -> str:
    """How much armed time sits in the last bp under each arm's guard — the
    denominator behind "what would 1bp buy". Bands are measured against the
    guard THAT TICK enforced (`guard_bp`), so a live oracle raise is respected
    rather than averaged away."""
    bands = collections.defaultdict(collections.Counter)
    guards = collections.defaultdict(collections.Counter)
    for r in tape:
        if r.get("ev") != "gated" or r["t"] < since:
            continue
        if not (r.get("reason") or "").startswith("basis guard"):
            continue
        g, m = r.get("guard_bp"), r.get("margin_bp")
        if g is None or m is None:
            continue
        sym = parse_slug(r["slug"])[0]
        guards[sym][g] += 1
        d = g - abs(m)
        b = ("0-1bp under" if d < 1 else "1-2bp under" if d < 2 else
             "2-3bp under" if d < 3 else "3-6bp under" if d < 6 else "6bp+ under")
        bands[sym][b] += 1
    order = ["0-1bp under", "1-2bp under", "2-3bp under", "3-6bp under", "6bp+ under"]
    out = ["**basis-guard margin distance — where the gated ticks actually sit**", "",
           "| symbol | guard (bp) | " + " | ".join(order) + " | total |",
           "|---|---|" + "---:|" * (len(order) + 1)]
    for sym in sorted(bands):
        tot = sum(bands[sym].values())
        g = "/".join(f"{k:.0f}" for k, _ in guards[sym].most_common(2))
        cells = " | ".join(f"{bands[sym][b]} ({bands[sym][b]/tot*100:.0f}%)" for b in order)
        out.append(f"| {sym} | {g} | {cells} | {tot} |")
    out.append("")
    return "\n".join(out)


# ------------------------------------------------------------------ clusters

def phase_bucket(p: float) -> str:
    if p < 1 / 3:
        return "early (0-33%)"
    if p < 2 / 3:
        return "mid (33-66%)"
    return "late (66-100%)"


def cluster(eps, keyfn, title, hours, keyname, order=None) -> str:
    return table(rollup(eps, keyfn), hours, title, keyname, order=order)


def ask_bucket(a: float) -> str:
    for hi, name in ((0.20, "0.00-0.20"), (0.50, "0.20-0.50"), (0.80, "0.50-0.80"),
                     (0.95, "0.80-0.95")):
        if a < hi:
            return name
    return "0.95-1.00"


ASK_BUCKETS = ("0.00-0.20", "0.20-0.50", "0.50-0.80", "0.80-0.95", "0.95-1.00")


def by_entry_price(eps, hours, fams=("basis_guard", "theta", "min_fair", "latch")) -> str:
    """The stratification the aggregate hides: a gate refusing a $0.10 side and
    a gate refusing a $0.90 side are two different policies wearing one name.
    A cheap refusal risks one clip to win nine; a dear one risks nine to win
    one, and the same hit rate means opposite things at the two ends."""
    out = ["**refusals by entry price — where each gate is actually right**", "",
           "| gate | entry ask | eps | hit | missed wins | avoided losses | net |",
           "|---|---|---:|---:|---:|---:|---:|"]
    for fam in fams:
        pool = [e for e in eps if e["family"] == fam and e["status"] == "priced"]
        if not pool:
            continue
        r = rollup(pool, lambda e: ask_bucket(e["entry_ask"]))
        for b in ASK_BUCKETS:
            if b not in r:
                continue
            v = r[b]
            out.append(f"| {fam} | {b} | {v['episodes']} | {v['hit']*100:.0f}% | "
                       f"{money(v['missed'])} | {money(v['avoided'])} | "
                       f"**{money(v['net'])}** |")
    out.append("")
    return "\n".join(out)


def specimen(eps: list[dict], fam: str, n: int = 3, worst=True) -> list[dict]:
    """The n episodes of a family with the largest |net| — the windows a
    finding has to be able to point at."""
    pool = [e for e in eps if e["family"] == fam and e["status"] == "priced"]
    pool.sort(key=lambda e: e["pnl"], reverse=worst)
    return pool[:n]


def deployment(tape, activity, arms, since: float) -> str:
    """The denominator every shadow number needs: what the fleet ACTUALLY did.

    An opportunity-cost ledger prices one clip per refused side, so it will
    always dwarf the book — the useful question is by how much, and whether
    the thing standing between the arm and the money is a gate at all. The
    second table answers that: `sized(r)` is
    `min(clip_usdc/ask, ask_size, room/ask)`, and on a window's FIRST clip
    the early room (0.2 x size_usdc) exceeds clip_usdc on every live arm, so
    a first clip that lands under its clip_usdc was truncated by `ask_size`
    — the book's displayed depth — and by nothing else.
    """
    per = collections.defaultdict(lambda: {"buy": 0.0, "sell": 0.0, "redeem": 0.0})
    for a in activity:
        slug = a.get("slug") or ""
        p_ = parse_slug(slug)
        if not p_ or p_[2] < since:
            continue
        u = a.get("usdcSize") or 0.0
        if a.get("type") == "TRADE" and a.get("side") == "BUY":
            per[slug]["buy"] += u
        elif a.get("type") == "TRADE" and a.get("side") == "SELL":
            per[slug]["sell"] += u
        elif a.get("type") == "REDEEM":
            per[slug]["redeem"] += u
    traded = {k: v for k, v in per.items() if v["buy"] > 0}
    pnl = {k: v["redeem"] + v["sell"] - v["buy"] for k, v in traded.items()}
    notional = sum(v["buy"] for v in traded.values())
    armed = {r["slug"] for r in tape if r["t"] >= since}
    fired = {r["slug"] for r in tape if r["t"] >= since and r.get("ev") == "fire"}
    tot = sum(pnl.values())
    out = ["**what the fleet actually deployed (the shadow ledger's denominator)**", "",
           f"- armed windows: **{len(armed)}** · fired at least one clip: **{len(fired)}** "
           f"({len(fired)/max(len(armed),1)*100:.0f}%) · wallet-traded: **{len(traded)}**",
           f"- deployed notional **${notional:,.0f}**, realized **{money(tot)}** "
           f"({tot/max(notional,1)*100:+.2f}%), "
           f"{sum(1 for v in pnl.values() if v > 0)}/{len(pnl)} windows up",
           f"- mean **${notional/max(len(traded),1):,.0f}** of notional per traded window", ""]

    first = {}
    for r in sorted((r for r in tape if r.get("ev") == "fire" and r["t"] >= since),
                    key=lambda x: x["t"]):
        first.setdefault((r["slug"], r["side"]), r)
    by = collections.defaultdict(list)
    for (slug, _side), r in first.items():
        p_ = parse_slug(slug)
        by[(p_[0], p_[1])].append(r["size"] * r["ask"])
    out += ["**first clips: what the BOOK let through, not what the gates did**", "",
            "| arm | first clips | clip_usdc | early room (0.2 x size) | median first clip | "
            "% of clip |", "|---|---:|---:|---:|---:|---:|"]
    for k in sorted(by):
        arm = arms.get(k)
        if not arm:
            continue
        med = statistics.median(by[k])
        out.append(f"| {k[0]} {k[1]} | {len(by[k])} | ${arm['clip_usdc']:.0f} | "
                   f"${arm['size_usdc']*EARLY_FRAC:.0f} | ${med:.1f} | "
                   f"{med/arm['clip_usdc']*100:.0f}% |")
    out.append("")
    return "\n".join(out)


# ------------------------------------------------------------------- fifteen

def fifteen(tape, winners, clip_of, arms, since: float) -> dict:
    """Why 15m has not fired, ordered by bind frequency, and what its refused
    sides did — both at the arm's REAL $1 clip and at the clip a normally
    sized 15m arm would have used."""
    rolls = roll_sizes(tape)
    fired = {(round(r["t"], 3), r["slug"], r["side"]) for r in tape if r.get("ev") == "fire"}
    binds = collections.Counter()
    win_gates = collections.Counter()
    evals = 0
    ticks = []
    for r in tape:
        if r["t"] < since or "-15m-" not in r["slug"]:
            continue
        p = parse_slug(r["slug"])
        sym, dur, start, end = p
        if r.get("ev") == "gated":
            fam = window_family(r.get("reason") or "")
            if fam:
                win_gates[fam] += 1
            continue
        if r.get("ev") != "eval":
            continue
        evals += 1
        arm = arms.get((sym, dur)) or {}
        min_fair = arm.get("min_fair", 0.97)
        if start < FIFTEEN_SHUT_AT:
            min_fair = 0.97
        size_usdc = rolls.get(r["slug"], arm.get("size_usdc"))
        for s in r.get("sides") or []:
            if s.get("side") not in ("up", "down"):
                continue
            fam, ctx = side_family(r, s, size_usdc, min_fair, arm.get("theta"))
            if fam == "cooldown" and (round(r["t"], 3), r["slug"], s["side"]) in fired:
                fam = "fired"
            binds[fam] += 1
            if fam not in ("fired", "no_ask"):
                ticks.append({"t": r["t"], "slug": r["slug"], "sym": sym, "dur": dur,
                              "phase": (r["t"] - start) / max(end - start, 1e-9),
                              "side": s["side"], "family": fam, "ask": s.get("ask"),
                              "fair": s.get("fair"), "net": s.get("net"),
                              "safety": s.get("safety")})
    eps = collapse(ticks)
    real, norm = [], []
    for ep in eps:
        real.append(price(ep, winners, (arms.get((ep["sym"], ep["dur"])) or {})
                          .get("clip_usdc", 1.0)))
        norm.append(price(ep, winners, clip_of(ep["slug"], ep["sym"], "15m_norm")))
    return {"evals": evals, "binds": binds, "window_gates": win_gates,
            "episodes_real": real, "episodes_norm": norm}


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


# --------------------------------------------------------------------- main

SLICES = (
    # name, epoch, what changed there and how it is dated
    ("stream", STREAM_ERA,
     "stream era — e296336 11:29:30Z, arms can read the settlement stream (eras.py)"),
    ("regime", REGIME_START,
     "today's regime — 17:31Z engine restart: 15m shut to $1, sol/xrp maker bids on"),
    ("posture", POSTURE_TRUE,
     "today's posture — 20:25:50Z, btc+eth join xrp on rtds@60s (engine log)"),
    ("posture_asked", POSTURE,
     "the operator's 20:45Z mark, inside the posture slice above"),
)


def make_clip_of(tape, arms):
    """clip_of(slug, sym, dur) -> the notional ONE hypothetical clip commits.

    Window's own median real fire notional, else the (symbol,duration) median
    across the tape, else the live arm's clip_usdc. `dur == "15m_norm"` asks
    for the 15m clip a NORMALLY sized 15m arm used (the $300-500 era), which
    is the only honest way to price a counterfactual for arms currently
    sized at $1.
    """
    win = collections.defaultdict(list)
    sym_dur = collections.defaultdict(list)
    for r in tape:
        if r.get("ev") != "fire" or not r.get("size") or not r.get("ask"):
            continue
        p = parse_slug(r["slug"])
        if not p:
            continue
        win[r["slug"]].append(r["size"] * r["ask"])
        sym_dur[(p[0], p[1])].append(r["size"] * r["ask"])
    fifteen_norm = statistics.median(
        [v for (s, d), vs in sym_dur.items() if d == "15m" for v in vs] or [25.0])

    def clip_of(slug, sym, dur):
        if dur == "15m_norm":
            return fifteen_norm
        c = win.get(slug)
        if c:
            return statistics.median(c)
        if (sym, dur) in sym_dur and not (
                dur == "15m" and (parse_slug(slug) or [0, 0, 0])[2] >= FIFTEEN_SHUT_AT):
            return statistics.median(sym_dur[(sym, dur)])
        return (arms.get((sym, dur)) or {}).get("clip_usdc", 25.0)
    return clip_of


def analyse(tape, winners, activity, arms, since: float, label: str, clip_of) -> dict:
    rolls = roll_sizes(tape)
    size_fallback = {k: (arms.get(k) or {}).get("size_usdc") for k in arms}
    fired_index = {(round(r["t"], 3), r["slug"], r["side"]) for r in tape
                   if r.get("ev") == "fire"}
    ticks = build_ticks(tape, since, rolls, arms, size_fallback, fired_index)
    eps = [price(ep, winners, clip_of(ep["slug"], ep["sym"], ep["dur"]))
           for ep in collapse(ticks)]
    unf = unfilled(tape, activity, winners, since)
    last_t = max(r["t"] for r in tape)
    return {"label": label, "since": since, "hours": (last_t - since) / 3600.0,
            "episodes": eps, "unfilled": unf,
            "families": rollup(eps), "ticks": ticks}


def fill_report(unf: list[dict], hours: float) -> str:
    """The old 93%-hit unfilled leak, re-measured either side of pay-up.

    `paid_up` is exact: a fire record carries `limit` beside `ask`, and a
    limit above the ask is the chase actually submitted. Pre-pay-up fires
    have limit == ask (or no limit field at all).
    """
    groups = {
        "chased (limit > ask)": [e for e in unf if e["last"].get("limit")
                                 and e["last"]["limit"] > e["last"]["ask"] + 1e-9],
        "not chased": [e for e in unf if not e["last"].get("limit")
                       or e["last"]["limit"] <= e["last"]["ask"] + 1e-9],
    }
    lines = ["**unfilled fires — the pay-up test**", "",
             "| cohort | (slug,side) fires | fully filled | intended $ | filled $ | "
             "fill rate | unfilled $ | net of the gap |",
             "|---|---:|---:|---:|---:|---:|---:|---:|"]
    for name, es in groups.items():
        if not es:
            continue
        intended = sum(e["intended"] for e in es)
        filled = sum(min(e["filled"], e["intended"]) for e in es)
        full = sum(1 for e in es if e["status"] == "filled")
        net = sum(e["pnl"] for e in es if e["status"] == "priced")
        lines.append(f"| {name} | {len(es)} | {full} | ${intended:,.0f} | ${filled:,.0f} | "
                     f"{filled/intended*100:.1f}% | ${intended-filled:,.0f} | {money(net)} |")
    lines.append("")
    return "\n".join(lines)


def trace(tape, winners, arms, slug: str) -> str:
    """Every tape record for one window, with the gate each eval side was
    charged to — a finding has to survive being pointed at a window."""
    rolls = roll_sizes(tape)
    p = parse_slug(slug)
    if not p:
        return f"not an updown slug: {slug}"
    sym, dur, start, end = p
    arm = arms.get((sym, dur)) or {}
    min_fair = arm.get("min_fair", 0.97)
    if dur == "15m" and start < FIFTEEN_SHUT_AT:
        min_fair = 0.97
    size_usdc = size_for(slug, rolls, arms, {})
    out = [f"### {slug}",
           f"window {utc(start)} - {utc(end)} · size ${size_usdc} · "
           f"clip ${arm.get('clip_usdc', '?')} · guard {arm.get('basis_guard_bp', '?')}bp · "
           f"theta {arm.get('theta', '?')} · min_fair {min_fair} · "
           f"feed {arm.get('feed', '?')} · winner **{winners.get(slug, 'ungraded')}**", "",
           "```"]
    for r in tape:
        if r["slug"] != slug:
            continue
        el = (r["t"] - start) / max(end - start, 1e-9) * 100
        ev = r.get("ev")
        if ev == "gated":
            out.append(f"{utc(r['t'])} {el:5.1f}%  GATED  {r.get('reason')}"
                       + (f"  up_ask={r.get('up_ask')} dn_ask={r.get('dn_ask')}"
                          if r.get("up_ask") or r.get("dn_ask") else ""))
        elif ev == "eval":
            bits = []
            for sd in r.get("sides") or []:
                if sd.get("side") not in ("up", "down"):
                    continue
                fam, _ctx = side_family(r, sd, size_usdc, min_fair, arm.get("theta"))
                bits.append(f"{sd['side']}: ask={sd.get('ask')} fair="
                            f"{(sd.get('fair') or 0):.3f} net={(sd.get('net') or 0):+.3f} "
                            f"safety={sd.get('safety')} -> {fam}")
            out.append(f"{utc(r['t'])} {el:5.1f}%  EVAL   rho={r.get('rho')} "
                       f"bd={r.get('banked_decided')} margin={r.get('margin_bp')} | "
                       + " | ".join(bits))
        elif ev == "fire":
            out.append(f"{utc(r['t'])} {el:5.1f}%  FIRE   {r.get('side')} ask={r.get('ask')} "
                       f"limit={r.get('limit')} size={r.get('size')} "
                       f"(${r.get('size', 0)*r.get('ask', 0):.0f}) mode={r.get('mode')}")
        elif ev in ("roll", "cleanup", "exit"):
            out.append(f"{utc(r['t'])} {el:5.1f}%  {ev.upper()}  {json.dumps(r)}")
    out += ["```", ""]
    return "\n".join(out)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--tape", default=".work/updown-tape-frozen.jsonl")
    ap.add_argument("--outcomes", default=".work/outcomes-frozen.jsonl")
    ap.add_argument("--activity", default=".work/activity.json")
    ap.add_argument("--arms", default=".work/arms-state.json")
    ap.add_argument("--json", dest="json_out", default=None,
                    help="dump the full per-episode ledger here")
    ap.add_argument("--trace", action="append", default=None,
                    help="dump every record for these slugs, gate-attributed, then stop")
    ap.add_argument("--self-check", action="store_true",
                    help="prove the assumed thresholds against every real fire, then stop")
    args = ap.parse_args()

    tape = load_tape(args.tape)
    winners = load_winners(args.outcomes)
    activity = json.load(open(args.activity))
    arms = load_arms(args.arms)

    if args.trace:
        for slug in args.trace:
            print(trace(tape, winners, arms, slug))
        return 0

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

    clip_of = make_clip_of(tape, arms)
    runs = {name: analyse(tape, winners, activity, arms, since,
                          f"{name} · since {utc(since)}", clip_of)
            for name, since, _why in SLICES}

    print(f"# gate shadow 2 — tape through {utc(max(r['t'] for r in tape))}, "
          f"{len(winners)} terminal-graded windows\n")
    for name, since, why in SLICES:
        r = runs[name]
        print(f"## {name} — {why}")
        print(f"_{r['hours']:.2f}h · episode = one refusal run, priced as ONE clip "
              f"at the ask when the gate FIRST refused_\n")
        print(table(r["families"], r["hours"], f"gate families · {name}"))
        print(fill_report(r["unfilled"], r["hours"]))

    st = runs["stream"]
    eps = st["episodes"]
    hours = st["hours"]
    print("## clusters (stream era)\n")
    print(cluster(eps, lambda e: e["sym"], "by symbol", hours, "symbol"))
    print(cluster(eps, lambda e: f"{e['sym']} · {e['family']}",
                  "by symbol x gate (top movers)", hours, "symbol · gate",
                  order=[k for k, v in sorted(
                      rollup(eps, lambda e: f"{e['sym']} · {e['family']}").items(),
                      key=lambda kv: -abs(kv[1]["net"]))][:18]))
    print(cluster(eps, lambda e: utc(e["start"], "%H") + ":00",
                  "by hour", hours, "hour (UTC)",
                  order=sorted({utc(e["start"], "%H") + ":00" for e in eps})))
    print(cluster(eps, lambda e: phase_bucket(e["phase"]),
                  "by window phase", hours, "phase",
                  order=["early (0-33%)", "mid (33-66%)", "late (66-100%)"]))
    print(cluster([e for e in eps if e["family"] == "basis_guard"],
                  lambda e: phase_bucket(e["phase"]),
                  "basis guard by window phase", hours, "phase",
                  order=["early (0-33%)", "mid (33-66%)", "late (66-100%)"]))
    print(cluster(eps, lambda e: e["dur"], "by duration", hours, "duration"))
    print(by_entry_price(eps, hours))

    print("## deployment (stream era)\n")
    print(deployment(tape, activity, arms, STREAM_ERA))

    print("## data-quality gates (stream era)\n")
    print(blind_exposure(tape, STREAM_ERA))
    print(cold_feed_defect(tape, STREAM_ERA))
    print(guard_band(tape, STREAM_ERA))

    print("## relaxation ladders (stream era — where an A/B should aim)\n")
    print(ladder_table(guard_ladder(tape, winners, clip_of, STREAM_ERA, arms),
                       "basis guard: relax by X bp — fires at the first tick that clears "
                       "the looser guard, at that tick's ask", "relax (bp)", extra=True))
    tk = st["ticks"]
    print(ladder_table(side_ladder(tk, "theta", "safety",
                                   (0.28, 0.25, 0.20, 0.15, 0.10, 0.05),
                                   winners, clip_of),
                       "theta: drop the entry bar to X — fires at the first tick whose "
                       "per-side safety clears it, at that tick's ask (theta 0.30 live)",
                       "theta -> X"))
    print(ladder_table(side_ladder(tk, "min_fair", "fair",
                                   (0.96, 0.95, 0.94, 0.92, 0.90, 0.85),
                                   winners, clip_of,
                                   extra_ok=lambda t: (t.get("net") is not None
                                                       and t["net"] >= t["edge_req"])),
                       "min_fair: drop the safe-mode fair bar to X — first tick that "
                       "clears it AND still clears min_edge (min_fair 0.97 live)",
                       "min_fair -> X"))
    print(ladder_table(side_ladder(tk, "min_edge", "net",
                                   (0.012, 0.010, 0.008, 0.005),
                                   winners, clip_of,
                                   extra_ok=lambda t: t.get("unlocked")),
                       "min_edge: drop the safe-mode edge bar to X (safe-mode sides only; "
                       "min_edge 0.015 live)", "min_edge -> X"))

    print("## the 15m question\n")
    f = fifteen(tape, winners, clip_of, arms, REGIME_START)
    print(f"15m eval SIDES since {utc(REGIME_START)}: {sum(f['binds'].values())} "
          f"across {f['evals']} eval records\n")
    print("| gate holding 15m shut | side-evals | share |")
    print("|---|---:|---:|")
    tot = sum(f["binds"].values())
    for k, v in f["binds"].most_common():
        print(f"| {k} | {v} | {v/tot*100:.1f}% |")
    print()
    print("| window-level gate (15m) | ticks |")
    print("|---|---:|")
    for k, v in f["window_gates"].most_common():
        print(f"| {k} | {v} |")
    print()
    fh = (max(r["t"] for r in tape) - REGIME_START) / 3600.0
    print(table(rollup(f["episodes_real"]), fh,
                "15m refused sides priced at the arm's REAL $1 clip", "gate"))
    print(table(rollup(f["episodes_norm"]), fh,
                "15m refused sides priced at a normally-sized 15m clip "
                f"(${clip_of('x', 'x', '15m_norm'):.0f}) — the counterfactual", "gate"))

    print("## specimens\n")
    for fam in ("basis_guard", "theta", "min_fair", "latch", "feed_stale"):
        for e in specimen(eps, fam, 2):
            print(f"- **{fam}** {e['slug']} {e['side']} · {utc(e['start'])}-{utc(e['end'])} "
                  f"· {e['n_ticks']} ticks · entry {e['entry_ask']} "
                  f"(best {e['best_ask']}) · clip ${e['clip']:.0f} · "
                  f"{'WON' if e['won'] else 'lost'} · {money(e['pnl'])}")
    print()

    if args.json_out:
        dump = {k: {"label": v["label"], "since": v["since"], "hours": v["hours"],
                    "families": v["families"],
                    "episodes": [{kk: vv for kk, vv in e.items() if kk != "last"}
                                 for e in v["episodes"] + v["unfilled"]]}
                for k, v in runs.items()}
        dump["fifteen"] = {"binds": dict(f["binds"]),
                           "window_gates": dict(f["window_gates"])}
        json.dump(dump, open(args.json_out, "w"), indent=1, default=str)
    return 0


if __name__ == "__main__":
    sys.exit(main())
