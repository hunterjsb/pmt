"""Tape-derived aggregates for `pmt crypto stats` — the blocks that read the
engine's own telemetry rather than the wallet.

Everything here is pure: the caller opens the files (polymarket.tape) and
hands in already-parsed record lists, these functions fold them into small
dicts, and stats_render turns those into pixels. Same split effectiveness.py
already uses, and the reason each block below is unit-testable with three
inline records instead of a live engine.

Three questions, three folds:

  arm_flags      — which feed each series is armed on, and whether it is
                   resting maker bids. Straight off the /status payload.
  maker_summary  — the resting-bid experiment (maker step 0): how often the
                   slice was REACHABLE, how often a bid actually rested, and
                   what landed in it. The fill attribution is deliberately
                   weak evidence and labelled as such — see below.
  chase_summary  — the order path: what got submitted, what the delta matcher
                   suppressed, how long an ack took, and how much of the
                   pay-up buffer the engine is actually spending.
  fleet_summary  — the R7 un-decided cap and how close the fleet came to it.
"""

from __future__ import annotations

from collections.abc import Sequence

from . import tape, updown_slugs

# A limit this far above the ask is float noise on a cent-quoted book, not a
# pay-up. Half a tick.
_CHASE_EPS = 0.0005


def _pctl(values: Sequence[float], q: float) -> float | None:
    """Nearest-rank percentile. Deliberately not interpolated: these samples
    are latency milliseconds where the observed value is more useful than a
    number that never occurred."""
    vals = sorted(v for v in values if v is not None)
    if not vals:
        return None
    idx = min(int(q * len(vals)), len(vals) - 1)
    return vals[idx]


def arm_flags(arms: dict | None) -> dict:
    """series_key -> {"feed", "maker_bid"} from a /status reply's arms.

    Keyed by series ("btc 5m"), not slug: the stats table has one row per
    series and every arm in a series carries the same params. Last arm wins
    on the (transient) disagreement a mid-roll param change can produce.
    """
    out: dict[str, dict] = {}
    for slug, a in (arms or {}).items():
        parsed = updown_slugs.parse(slug)
        if parsed is None or not isinstance(a, dict):
            continue
        out[parsed[4]] = {"feed": a.get("feed") or "binance",
                          "maker_bid": bool(a.get("maker_bid"))}
    return out


def maker_summary(evals: Sequence[dict], orders: Sequence[dict],
                  activity: Sequence[dict], graded: Sequence[dict]) -> dict:
    """The resting-bid experiment, from the eval tape + order tape + wallet.

    Maker step 0 rests ONE post-only bid on a side the book is not offering
    (analysis/freq_funnel_report.md §3: ~10% of armed time has no ask at
    all). The eval tape marks both halves of the experiment:

      side.maker_candidate  the knob is OFF and the slice would have quoted —
                            the shadow measurement that prices the class
                            before any capital rides on it.
      side.maker_rest = px  the knob is ON and a bid is resting at px.

    Attribution of a FILL is experiment-grade, not proof, and the renderer
    must say so. A post-only order id never reaches the wallet feed, so the
    only join available is: a BUY on the same slug and side, at or below a
    price we were resting at, after we started resting there. A taker clip
    that happened to cross cheap satisfies that too. The hard number beside
    it is `placed` — post_only acks on the order tape, which is the count of
    bids the engine really did submit.
    """
    cands: set = set()
    cand_n = 0
    rests: dict[tuple[str, str], float] = {}   # (slug, side) -> best px rested
    rest_from: dict[tuple[str, str], float] = {}
    rest_n = 0
    for r in evals:
        slug = r.get("slug") or ""
        t = float(r.get("t") or 0.0)
        for s in r.get("sides") or []:
            if not isinstance(s, dict):
                continue
            side = s.get("side") or ""
            if s.get("maker_candidate"):
                cand_n += 1
                cands.add(slug)
            px = s.get("maker_rest")
            if px is None:
                continue
            rest_n += 1
            key = (slug, side)
            rests[key] = max(rests.get(key, 0.0), float(px))
            rest_from[key] = min(rest_from.get(key, t), t)

    placed = sum(1 for o in orders
                 if o.get("stage") == tape.STAGE_ACK and o.get("post_only"))

    fills = 0
    fill_usd = 0.0
    fill_slugs: set = set()
    for a in activity:
        if a.get("type") != "TRADE" or a.get("side") != "BUY":
            continue
        key = (a.get("slug") or "", (a.get("outcome") or "").lower())
        bar = rests.get(key)
        if bar is None:
            continue
        if float(a.get("price") or 0.0) <= bar + _CHASE_EPS \
                and float(a.get("timestamp") or 0.0) >= rest_from[key]:
            fills += 1
            fill_usd += float(a.get("usdcSize") or 0.0)
            fill_slugs.add(key[0])

    wins = losses = 0
    pnl = 0.0
    for w in graded:
        if w.get("slug") not in fill_slugs:
            continue
        wins += bool(w.get("won"))
        losses += not w.get("won")
        pnl += float(w.get("pnl") or 0.0)
    return {"candidates": cand_n, "candidate_windows": len(cands),
            "rested": rest_n, "rested_windows": len({k[0] for k in rests}),
            "placed": placed, "fills": fills, "fill_usd": fill_usd,
            "fill_windows": len(fill_slugs), "wins": wins, "losses": losses,
            "pnl": pnl if (wins or losses) else None}


def chase_summary(orders: Sequence[dict], fires: Sequence[dict]) -> dict:
    """Order path + pay-up, from the order tape and the fire tape.

    The order tape answers what happened to a decision (acked, or suppressed
    by the delta matcher because an equivalent order was already resting) and
    how long the wire took. The fire tape answers what price we asked for:
    `limit` is the marketable limit `pay_up_limit` produced, recorded beside
    the `ask` it chased from, so limit-minus-ask is the pay-up buffer the
    engine actually spent out of its `--pay-up` allowance.

    `chase_n` counts only fire records that CARRY a limit — the field
    postdates most of the tape, and a pre-limit fire is unknown, not zero.
    """
    acks = [o for o in orders if o.get("stage") == tape.STAGE_ACK]
    suppressed = sum(1 for o in orders if o.get("stage") == tape.STAGE_SUPPRESSED)
    decided = len(acks) + suppressed

    buffers = []
    chased = 0
    for f in fires:
        limit, ask = f.get("limit"), f.get("ask")
        if limit is None or ask is None:
            continue
        gap = (float(limit) - float(ask)) * 100.0  # cents
        buffers.append(gap)
        chased += gap > _CHASE_EPS * 100.0
    return {
        "acks": len(acks), "suppressed": suppressed,
        "suppressed_share": (suppressed / decided) if decided else None,
        "ack_p50": _pctl([o.get("ack_ms") for o in acks], 0.50),
        "ack_p90": _pctl([o.get("ack_ms") for o in acks], 0.90),
        "sign_p50": _pctl([o.get("sign_done_ms") for o in acks], 0.50),
        "chase_n": len(buffers), "chased": chased,
        "buffer_med_c": _pctl(buffers, 0.50),
        "buffer_max_c": max(buffers) if buffers else None,
    }


def fleet_summary(evals: Sequence[dict], cap: float | None) -> dict:
    """R7 fleet un-decided cap: the ration, and how close the fleet got.

    The engine only writes `fleet_room` (remaining headroom) when a cap is
    actually set — an uncapped fleet has infinite room, which is not a number
    any consumer should see — so a tape with no such records means the cap
    was off for the whole range, not that exposure was zero.

    Peak un-decided is `cap - min(fleet_room)`, measured against the cap in
    force NOW. A cap changed mid-range would misprice the older ticks; that
    is why `ticks` is reported beside it rather than the number standing
    alone.
    """
    rooms = [float(r["fleet_room"]) for r in evals if r.get("fleet_room") is not None]
    blocked = 0.0
    for r in evals:
        for s in r.get("sides") or []:
            if isinstance(s, dict) and s.get("brake") == "fleet":
                blocked += float(s.get("fleet_blocked") or 0.0)
    cap = float(cap or 0.0)
    return {"cap": cap, "ticks": len(rooms), "blocked_usd": blocked,
            "peak_undecided": (cap - min(rooms)) if (rooms and cap > 0) else None}
