"""Shadow P&L ledger — the opportunity-cost complement of the loss audit.

Every gate refusal is recorded on the tape; this module turns that record
into a dollar figure, hindsight-honestly: net shadow P&L = missed wins
MINUS avoided losses. A gate that refuses 3 winners and 1 full loss can
still be net-positive (the loss it dodged outweighs the wins it missed), so
every rollup here carries both sides, never just the missed-wins half.

Pure (no network, no disk) so the episode/pricing logic is unit-testable
with inline fixtures — `pmt crypto stats --gates` does the I/O (tape
file, wallet activity, outcomes corpus refresh via polymarket.outcomes)
and calls in.

Method, per ROADMAP's shadow-ledger spec:
  1. Every tape tick that represents a refused side (a `gated` basis-guard
     line, or an `eval` side carrying a `brake`, or an unbraked eval side
     that failed the min_fair/min_edge threshold) becomes a "tick".
  2. Ticks sharing (window, side, category) collapse into EPISODES — the
     tape re-records every ~5s, so a >20s gap means the refusal cleared and
     came back, not one continuous stretch.
  3. Each episode prices as ONE hypothetical clip at its best (lowest)
     recorded ask, sized at that window's real clip notional (inferred from
     its actual fires) or the arm default. WIN pays $1/share net of fee;
     LOSS forfeits the whole clip.
  4. `unfilled_fires` is a separate, simpler category: the gap between a
     window's intended fire notional and what the wallet shows actually
     filled — no gap-collapsing needed, a window's fires already are the
     complete signal.
"""

from __future__ import annotations

import json
import re
import statistics
from collections.abc import Iterable, Iterator

from .constants import FEE_RATE, taker_fee
from .tape import EV_EVAL, EV_FIRE, EV_GATED
from .updown_slugs import is_updown

DEFAULT_CLIP_USDC = 25.0   # crypto arm's --clip default
DEFAULT_MIN_FAIR = 0.97    # crypto arm's --min-fair default (the safety gate)
DEFAULT_MIN_EDGE = 0.015   # crypto arm's --min-edge default
EPISODE_GAP_S = 20.0       # tape re-records ~every 5s; a bigger gap is a new episode

# Named because the journal calls this one brake out by itself — the latch is
# the only refusal whose SAVE is a story rather than a statistic.
BRAKE_LATCHED = "latched"
BRAKE_CATEGORIES = ("safety", BRAKE_LATCHED, "distrust", "avg_down")
CATEGORY_ORDER = ("basis_guard", *BRAKE_CATEGORIES, "sub_threshold", "unfilled_fires")

_MARGIN_RE = re.compile(r"projected margin ([+-]?\d+(?:\.\d+)?)bp")


def fee(ask: float) -> float:
    """Per-share taker fee at the live rate. This module never fetches a
    market's actual feeSchedule (pure/no-network by design), so it prices
    at the constant.

    Only ever used to price a HYPOTHETICAL clip. Where a wallet row exists
    the ledger reads the fee the wallet actually recorded — a formula never
    overrides a receipt.
    """
    return taker_fee(ask, FEE_RATE)


def basis_guard_side(reason: str, margin_bp: float | None = None) -> str | None:
    """Side implied by a basis-guard gate's signed projected margin
    (positive margin => up favored, negative => down). None if `reason`
    isn't a basis-guard line at all — 'feed stale', the pre-R9 clock-gated
    'window N% elapsed' lines, or anything else this ledger doesn't model —
    or a basis-guard line with no margin available.

    `margin_bp` is the engine's structured field and wins when present.
    The regex is the legacy path for tape lines written before that field
    shipped; it parses the very sentence the field is formatted from, so
    they cannot disagree — but a reword breaks the regex and not the field.
    """
    if not reason.startswith("basis guard"):
        return None
    if margin_bp is None:
        m = _MARGIN_RE.search(reason)
        if not m:
            return None
        margin_bp = float(m.group(1))
    return "up" if margin_bp >= 0 else "down"


def shadow_value(ask: float, clip_notional: float, won: bool) -> float:
    """Hindsight P&L of one hypothetical clip taken at `ask`.

    WIN: clip/ask shares pay $1 each, net of the per-share taker fee.
    LOSS: the whole clip is forfeit (fee is moot — it only ever applies to
    a paying settlement, same as a live fill).
    """
    if won:
        shares = clip_notional / ask
        return shares * (1.0 - ask - fee(ask))
    return -clip_notional


# ---------- tick extraction from raw tape lines ----------

def _load(line: str) -> dict | None:
    line = line.strip()
    if not line:
        return None
    try:
        r = json.loads(line)
    except ValueError:
        return None
    return r if isinstance(r, dict) else None


def iter_ticks(lines: Iterable[str], since: float = 0.0) -> Iterator[dict]:
    """Refusal ticks from raw `updown-tape.jsonl` lines: {t, slug, side,
    category, ask, fair, net}.

    `category` is a brake name, "basis_guard", or None (an unbraked eval
    side — the caller runs `categorize_ticks` to decide sub_threshold vs
    drop, since that needs the min_fair/min_edge thresholds). `ask` is None
    when the source record didn't carry one — an old basis-guard `gated`
    line pre-dating up_ask/dn_ask, or an eval side with no live book —
    callers roll those into the 'unpriced' coverage bucket rather than
    dropping them silently.
    """
    for raw in lines:
        r = _load(raw)
        if r is None:
            continue
        t, slug = r.get("t"), r.get("slug")
        if t is None or not slug or t < since:
            continue
        ev = r.get("ev")
        if ev == EV_GATED:
            side = basis_guard_side(r.get("reason") or "", r.get("margin_bp"))
            if side is None:
                continue
            ask = r.get("up_ask") if side == "up" else r.get("dn_ask")
            yield {"t": t, "slug": slug, "side": side, "category": "basis_guard",
                   "ask": ask, "fair": None, "net": None}
        elif ev == EV_EVAL:
            for s in r.get("sides") or []:
                side = s.get("side")
                if side not in ("up", "down"):
                    continue
                brake = s.get("brake")
                category = brake if brake in BRAKE_CATEGORIES else None
                yield {"t": t, "slug": slug, "side": side, "category": category,
                       "ask": s.get("ask"), "fair": s.get("fair"), "net": s.get("net")}


def iter_fires(lines: Iterable[str], since: float = 0.0) -> Iterator[dict]:
    """Fire ticks from raw tape lines: {t, slug, side, ask, size}."""
    for raw in lines:
        r = _load(raw)
        if r is None or r.get("ev") != EV_FIRE:
            continue
        t, slug, side = r.get("t"), r.get("slug"), r.get("side")
        ask, size = r.get("ask"), r.get("size")
        if t is None or not slug or t < since:
            continue
        if side not in ("up", "down") or ask is None or size is None:
            continue
        yield {"t": t, "slug": slug, "side": side, "ask": ask, "size": size}


def classify_sub_threshold(tick: dict, min_fair: float, min_edge: float) -> bool:
    """True when an unbraked eval side failed purely on fair/edge — every
    other gate passed, this one just wasn't good enough to fire."""
    fair, net = tick.get("fair"), tick.get("net")
    if fair is None or net is None:
        return False
    return fair < min_fair or net < min_edge


def categorize_ticks(raw_ticks: Iterable[dict], min_fair: float = DEFAULT_MIN_FAIR,
                      min_edge: float = DEFAULT_MIN_EDGE) -> list[dict]:
    """Resolve every tick's final category, dropping unbraked eval sides
    that don't even clear the sub_threshold bar (mid-band noise — not a
    refusal we can attribute to any known gate)."""
    out = []
    for tick in raw_ticks:
        cat = tick["category"]
        if cat is None:
            if not classify_sub_threshold(tick, min_fair, min_edge):
                continue
            cat = "sub_threshold"
        out.append({**tick, "category": cat})
    return out


# ---------- episode collapsing ----------

def collapse_episodes(ticks: list[dict], gap_s: float = EPISODE_GAP_S) -> list[dict]:
    """Group ticks sharing (slug, side, category) into episodes: consecutive
    in time, split whenever the gap to the previous tick exceeds gap_s.

    Each episode: {slug, side, category, start, end, n_ticks, best_ask,
    last_fair, last_net}. best_ask is the lowest recorded ask seen in the
    episode (None if every tick in it lacked one).
    """
    by_key: dict[tuple[str, str, str], list[dict]] = {}
    for tk in ticks:
        by_key.setdefault((tk["slug"], tk["side"], tk["category"]), []).append(tk)

    episodes = []
    for (slug, side, category), tks in by_key.items():
        tks = sorted(tks, key=lambda x: x["t"])
        run = [tks[0]]
        for tk in tks[1:]:
            if tk["t"] - run[-1]["t"] > gap_s:
                episodes.append(_finish_episode(slug, side, category, run))
                run = [tk]
            else:
                run.append(tk)
        episodes.append(_finish_episode(slug, side, category, run))
    return episodes


def _finish_episode(slug: str, side: str, category: str, tks: list[dict]) -> dict:
    asks = [t["ask"] for t in tks if t.get("ask") is not None]
    return {
        "slug": slug, "side": side, "category": category,
        "start": tks[0]["t"], "end": tks[-1]["t"], "n_ticks": len(tks),
        "best_ask": min(asks) if asks else None,
        "last_fair": tks[-1].get("fair"), "last_net": tks[-1].get("net"),
    }


# ---------- clip sizing ----------

def window_clip_notional(fires: list[dict], default: float = DEFAULT_CLIP_USDC) -> float:
    """Clip notional a window's real fires reveal: the median single-fire
    notional (robust to an outsized opening fire vs a run's steady-state
    per-clip size — a min or mean would be skewed by that outlier). Falls
    back to the arm CLI's --clip default when the window fired nothing.
    """
    notionals = [f["size"] * f["ask"] for f in fires if f.get("size") and f.get("ask")]
    if not notionals:
        return default
    return statistics.median(notionals)


# ---------- pricing ----------

def price_episode(ep: dict, winner: str | None, clip_notional: float) -> dict:
    """Attach hindsight status/P&L to one episode.

    status: "unpriced" (no recorded ask — can't price regardless of outcome),
    "unresolved" (priceable but the window's winner isn't known — never
    guessed), or "priced" (both known; `won`/`pnl` are set).
    """
    if ep["best_ask"] is None:
        return {**ep, "status": "unpriced", "clip_notional": clip_notional,
                "won": None, "pnl": None}
    if winner is None:
        return {**ep, "status": "unresolved", "clip_notional": clip_notional,
                "won": None, "pnl": None}
    won = ep["side"] == winner
    pnl = shadow_value(ep["best_ask"], clip_notional, won)
    return {**ep, "status": "priced", "clip_notional": clip_notional, "won": won, "pnl": pnl}


# ---------- unfilled fires ----------

def wallet_fill_notional(activity_rows: list[dict]) -> dict[tuple[str, str], float]:
    """{(slug, side): filled BUY notional} from wallet TRADE rows — the same
    ground truth crypto_window's per-window P&L already reads."""
    out: dict[tuple[str, str], float] = {}
    for a in activity_rows:
        if a.get("type") != "TRADE" or a.get("side") != "BUY":
            continue
        slug = a.get("slug") or ""
        if not is_updown(slug):
            continue
        side = (a.get("outcome") or "").lower()
        if side not in ("up", "down"):
            continue
        key = (slug, side)
        out[key] = out.get(key, 0.0) + (a.get("usdcSize") or 0.0)
    return out


def unfilled_episodes(fires: list[dict], fills: dict[tuple[str, str], float],
                       winners: dict[str, str]) -> list[dict]:
    """One synthetic episode per (slug, side) with a positive unfilled
    remainder between intended fire notional and what the wallet shows
    actually filled. An unfilled remainder of a WINNING side is missed
    profit at the fires' volume-weighted ask; of a LOSING side, avoided
    loss. No gap-collapsing here — a window's fires for one side already
    are the complete real signal.
    """
    by_key: dict[tuple[str, str], list[dict]] = {}
    for f in fires:
        by_key.setdefault((f["slug"], f["side"]), []).append(f)

    episodes = []
    for (slug, side), fs in by_key.items():
        intended_shares = sum(f["size"] for f in fs)
        intended_notional = sum(f["size"] * f["ask"] for f in fs)
        if intended_shares <= 0:
            continue
        avg_ask = intended_notional / intended_shares
        filled_notional = fills.get((slug, side), 0.0)
        filled_shares = filled_notional / avg_ask if avg_ask else 0.0
        unfilled_shares = max(0.0, intended_shares - filled_shares)
        if unfilled_shares <= 1e-9:
            continue
        ep = {
            "slug": slug, "side": side, "category": "unfilled_fires",
            "start": min(f["t"] for f in fs), "end": max(f["t"] for f in fs),
            "n_ticks": len(fs), "best_ask": avg_ask,
            "last_fair": None, "last_net": None,
        }
        clip_notional = unfilled_shares * avg_ask
        episodes.append(price_episode(ep, winners.get(slug), clip_notional))
    return episodes


# ---------- rollups ----------

def summarize_categories(priced_episodes: list[dict]) -> dict[str, dict]:
    """Per-category rollup: episodes, priced n, missed_wins $, avoided_losses
    $, net $ (missed MINUS avoided), hit rate of the refused side."""
    out: dict[str, dict] = {}
    for ep in priced_episodes:
        s = out.setdefault(ep["category"], {
            "episodes": 0, "priced": 0, "unresolved": 0, "unpriced": 0,
            "wins": 0, "losses": 0, "missed_wins": 0.0, "avoided_losses": 0.0,
        })
        s["episodes"] += 1
        if ep["status"] == "unpriced":
            s["unpriced"] += 1
        elif ep["status"] == "unresolved":
            s["unresolved"] += 1
        else:
            s["priced"] += 1
            if ep["won"]:
                s["wins"] += 1
                s["missed_wins"] += ep["pnl"]
            else:
                s["losses"] += 1
                s["avoided_losses"] += -ep["pnl"]
    for s in out.values():
        s["net"] = s["missed_wins"] - s["avoided_losses"]
        s["hit_rate"] = s["wins"] / s["priced"] if s["priced"] else None
    return out


def verdict(cat_stats: dict) -> str:
    """'paying for itself' when the gate avoided more than it missed (net <=
    0), 'over-tight' when it missed more than it avoided (net > 0)."""
    if cat_stats["priced"] == 0:
        return "no priced episodes"
    return "paying for itself" if cat_stats["net"] <= 0 else "over-tight"


def build_report(tape_lines: Iterable[str], winners: dict[str, str], activity_rows: list[dict],
                  since: float = 0.0, min_fair: float = DEFAULT_MIN_FAIR,
                  min_edge: float = DEFAULT_MIN_EDGE, default_clip: float = DEFAULT_CLIP_USDC,
                  gap_s: float = EPISODE_GAP_S) -> dict:
    """Full shadow ledger: categorized episodes -> per-category summary,
    grand totals, and a coverage note. Pure given the tape lines already
    read and `winners` already resolved — all I/O lives in cli.py.
    """
    lines = list(tape_lines)

    raw_ticks = list(iter_ticks(lines, since))
    ticks = categorize_ticks(raw_ticks, min_fair, min_edge)
    episodes = collapse_episodes(ticks, gap_s)

    fires = list(iter_fires(lines, since))
    fires_by_slug: dict[str, list[dict]] = {}
    for f in fires:
        fires_by_slug.setdefault(f["slug"], []).append(f)

    priced: list[dict] = []
    windows: set[str] = set()
    for ep in episodes:
        windows.add(ep["slug"])
        clip = window_clip_notional(fires_by_slug.get(ep["slug"], []), default_clip)
        priced.append(price_episode(ep, winners.get(ep["slug"]), clip))

    fills = wallet_fill_notional(activity_rows)
    unfilled = unfilled_episodes(fires, fills, winners)
    for ep in unfilled:
        windows.add(ep["slug"])
    priced.extend(unfilled)

    categories = summarize_categories(priced)
    totals = {
        "episodes": sum(c["episodes"] for c in categories.values()),
        "priced": sum(c["priced"] for c in categories.values()),
        "unresolved": sum(c["unresolved"] for c in categories.values()),
        "unpriced": sum(c["unpriced"] for c in categories.values()),
        "missed_wins": sum(c["missed_wins"] for c in categories.values()),
        "avoided_losses": sum(c["avoided_losses"] for c in categories.values()),
    }
    totals["net"] = totals["missed_wins"] - totals["avoided_losses"]

    return {
        "categories": categories,
        "totals": totals,
        "coverage": {
            "windows": len(windows),
            "unpriced_episodes": totals["unpriced"],
            "skipped_unresolved": totals["unresolved"],
        },
        "episodes": priced,
    }
