"""`pilot2 status` — what the pilot has seen, what it would have done, what it holds.

Reads the tapes only. It never touches the network and never mutates state, so
it is safe to run against a live service from another terminal.
"""

from __future__ import annotations

import time

from . import policy, risk, state
from . import series as series_mod


def summarise(home, since_s: float | None = None, now: float | None = None) -> dict:
    """The whole status payload, as data. Rendering is a separate concern."""
    now = time.time() if now is None else now
    floor = 0.0 if since_s is None else now - since_s

    def _in(r: dict) -> bool:
        t = r.get("t")
        return isinstance(t, (int, float)) and t >= floor

    windows_seen = ev_pass = would_trade = priced = two_sided = polls = 0
    refused: dict[str, int] = {}
    unpriced: dict[str, int] = {}
    series_hits: dict[str, int] = {}
    for r in state.iter_records(state.SHADOW_TAPE, home,
                                evs=(state.EV_WINDOW, state.EV_SHADOW, state.EV_REFUSED)):
        if not _in(r):
            continue
        ev = r.get("ev")
        if ev == state.EV_WINDOW:
            windows_seen += 1
            polls += int(r.get("polls") or 0)
            priced += int(r.get("priced") or 0)
            two_sided += int(r.get("two_sided") or 0)
            ev_pass += int(r.get("ev_pass") or 0)
            for k, v in (r.get("unpriced") or {}).items():
                unpriced[k] = unpriced.get(k, 0) + int(v)
        elif ev == state.EV_SHADOW:
            would_trade += 1
            series_hits[r.get("series") or "?"] = series_hits.get(r.get("series") or "?", 0) + 1
        elif ev == state.EV_REFUSED:
            reason = r.get("refused") or "?"
            refused[reason] = refused.get(reason, 0) + 1

    graded = [r for r in state.iter_records(state.GRADED, home) if _in(r)]
    # Re-priced from each row's own (shares, ask, won) at today's fee schedule,
    # never summed off the stored `pnl` column — that column is stamped once at
    # grade time and a fee-model change leaves the file mixed-vintage. See
    # policy.reprice for the measured size of that drift.
    g_pnl = sum(policy.reprice(r) for r in graded)
    g_notional = sum(float(r.get("notional") or 0.0) for r in graded)
    g_wins = sum(1 for r in graded if r.get("won"))

    live_orders = [r for r in state.iter_records(state.LIVE_TAPE, home, evs=(state.EV_ORDER,))]
    acks = [r for r in state.iter_records(state.LIVE_TAPE, home, evs=(state.EV_ACK,))]
    filled = sum(float(r.get("filled") or 0.0) for r in acks)
    open_live = [r for r in state.iter_records(state.REDEEM_QUEUE, home)]

    weight = state.read_json(state.BLEND_WEIGHT, home,
                             {"w": policy.W_SEED, "source": policy.W_SOURCE_SEED, "rows": 0})

    return {
        "home": str(state.pilot_home(home)),
        "halted": risk.halted(state.pilot_home(home)),
        "halt_file": str(risk.halt_path(state.pilot_home(home))),
        "since_s": since_s,
        "blend": {"w": weight.get("w"), "source": weight.get("source"),
                  "rows": weight.get("rows"), "min_rows": policy.MIN_FIT_ROWS,
                  "seed": policy.W_SEED},
        "shadow": {
            "windows": windows_seen, "polls": polls, "priced": priced,
            "two_sided": two_sided, "ev_opportunities": ev_pass,
            "would_trade": would_trade, "refused": refused, "unpriced": unpriced,
            "by_series": series_hits,
        },
        "graded": {
            "n": len(graded), "wins": g_wins,
            "hit_pct": round(100.0 * g_wins / len(graded), 1) if graded else None,
            "pnl": round(g_pnl, 2), "notional": round(g_notional, 2),
            "c_per_dollar": round(100.0 * g_pnl / g_notional, 2) if g_notional > 0 else None,
        },
        "live": {
            "orders": len(live_orders), "acks": len(acks), "shares_filled": round(filled, 2),
            "redeem_queue": len(open_live),
            "redeem_notional": round(sum(float(r.get("notional") or 0.0) for r in open_live), 2),
        },
        "risk": {
            "max_total_exposure_usdc": risk.MAX_TOTAL_EXPOSURE_USDC,
            "max_clip_usdc": risk.MAX_CLIP_USDC,
            "max_shares_per_window": risk.MAX_SHARES_PER_WINDOW,
            "clips_per_window_side": risk.MAX_CLIPS_PER_WINDOW_SIDE,
            "no_entry_final_s": risk.NO_ENTRY_FINAL_S,
            "min_edge": policy.MIN_EDGE,
        },
    }


def render(s: dict) -> str:
    """Plain text. No rich, no colour — this is read over ssh and in a unit log."""
    L = []
    L.append(f"pilot2  home={s['home']}")
    L.append(f"  HALT: {'PRESENT — pilot stopped' if s['halted'] else 'absent'}  ({s['halt_file']})")
    b = s["blend"]
    L.append(f"  blend weight: w={b['w']} ({b['source']}; {b['rows']}/{b['min_rows']} rows, "
             f"seed {b['seed']})")
    sh = s["shadow"]
    L.append("  shadow")
    L.append(f"    windows closed      {sh['windows']}")
    L.append(f"    polls / priced      {sh['polls']} / {sh['priced']}"
             f"  (two-sided book {sh['two_sided']})")
    L.append(f"    EV opportunities    {sh['ev_opportunities']}")
    L.append(f"    would-trade         {sh['would_trade']}")
    if sh["by_series"]:
        L.append("      " + "  ".join(f"{k}={v}" for k, v in sorted(sh["by_series"].items())))
    if sh["refused"]:
        L.append("    refused by law      "
                 + "  ".join(f"{k}={v}" for k, v in sorted(sh["refused"].items())))
    if sh["unpriced"]:
        # no_reference_print dominating is the COLD-START signature: the
        # reference is the TWAP print AT window start, so a pilot that came up
        # mid-window picks up at the next boundary. Not a fault.
        L.append("    unpriced polls      "
                 + "  ".join(f"{k}={v}" for k, v in sorted(sh["unpriced"].items())))
    g = s["graded"]
    L.append("  graded")
    L.append(f"    trades {g['n']}  wins {g['wins']}"
             + (f"  hit {g['hit_pct']}%" if g["hit_pct"] is not None else "")
             + f"  P&L ${g['pnl']:+.2f} on ${g['notional']:.2f}"
             + (f"  ({g['c_per_dollar']:+.2f}c/$)" if g["c_per_dollar"] is not None else ""))
    lv = s["live"]
    L.append("  live")
    L.append(f"    orders {lv['orders']}  acks {lv['acks']}  shares filled {lv['shares_filled']}")
    L.append(f"    redeem queue {lv['redeem_queue']} positions, ${lv['redeem_notional']:.2f} "
             "notional — MANUAL SWEEP")
    r = s["risk"]
    L.append("  risk law (hard-coded)")
    L.append(f"    total exposure <= ${r['max_total_exposure_usdc']:.0f}   "
             f"clip <= ${r['max_clip_usdc']:.0f}   shares/window <= {r['max_shares_per_window']:.0f}")
    L.append(f"    {r['clips_per_window_side']} clip per window-side EVER   "
             f"no entry in final {r['no_entry_final_s']:.0f}s   min_edge {r['min_edge']}")
    return "\n".join(L)


def series_view() -> str:
    """What this build would watch and trade, without starting anything."""
    lines = [f"  shadow: {', '.join(series_mod.shadow_series())}"]
    try:
        lines.append(f"  live:   {', '.join(series_mod.live_series())}")
    except series_mod.SeriesRefused as e:
        lines.append(f"  live:   REFUSED — {e}")
    return "\n".join(lines)
