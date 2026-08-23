"""Resolution-farmer study, stage 6: portfolio simulation and capital efficiency.

Runs the farmer over the recorded history under an explicit filter/cap set and reports the
only numbers that decide a go/no-go: $/day per $1,000 of capital ACTUALLY EMPLOYED (not
per $1,000 of notional traded), max drawdown, worst day, and the correlated-failure tail.

Capital employed is integrated properly: every clip locks its cost basis from entry until
the market's closedTime, so slow UMA resolution shows up as a cost instead of being waved
away by an annualisation.
"""

from __future__ import annotations

import datetime as dt
import json
import math
import os
import random
import sys
from collections import Counter, defaultdict
from pathlib import Path

OUT = Path(os.path.expanduser("~/.pmt/resfarm"))


def econ(r, slip):
    entry = min(r["p_fav"] + slip, 1.0 - r["tick"])
    fee = r["fee_rate"] * entry * (1 - entry)
    cap = entry + fee
    payoff = 0.5 if r["split"] else (1.0 if r["won"] else 0.0)
    return payoff - cap, cap


def select(rows, f):
    out = []
    for r in rows:
        if r["delta_h"] != f["delta_h"]:
            continue
        if f["tradable_only"] and not r["tradable"]:
            continue
        if r["cat"] in f["exclude_cats"]:
            continue
        if f["cats"] and r["cat"] not in f["cats"]:
            continue
        if not (f["px_lo"] <= r["p_fav"] < f["px_hi"]):
            continue
        if r["vol"] < f["min_vol"]:
            continue
        if r["stale_s"] > f["max_stale_s"]:
            continue
        if r["distinct_late"] < f["min_distinct_late"]:
            continue
        if r["won"] is None and not r["split"]:
            continue
        out.append(r)
    return out


def apply_caps(rows, f):
    """Caps are applied in chronological order, the way a live scanner would hit them."""
    rows = sorted(rows, key=lambda r: r["end_ts"] - int(r["delta_h"] * 3600))
    per_day, per_event, per_cat_day = Counter(), Counter(), Counter()
    kept = []
    for r in rows:
        ts = r["end_ts"] - int(r["delta_h"] * 3600)
        day = dt.datetime.fromtimestamp(ts, dt.timezone.utc).date().isoformat()
        if per_day[day] >= f["max_per_day"]:
            continue
        if per_event[r["event_slug"]] >= f["max_per_event"]:
            continue
        if per_cat_day[(r["cat"], day)] >= f["max_per_cat_day"]:
            continue
        per_day[day] += 1
        per_event[r["event_slug"]] += 1
        per_cat_day[(r["cat"], day)] += 1
        r = dict(r)
        r["_ts"] = ts
        r["_day"] = day
        kept.append(r)
    return kept


def simulate(rows, f, clip=75.0, slip=0.002):
    sel = apply_caps(select(rows, f), f)
    if not sel:
        return None
    events = []          # (t, delta_capital, delta_cash)
    daily = defaultdict(float)
    per_trade = []
    for r in sel:
        pnl_share, cap_share = econ(r, slip)
        shares = clip / cap_share
        pnl = pnl_share * shares
        cost = cap_share * shares
        close_ts = r["closed_ts"] or (r["end_ts"] + 2 * 3600)
        if close_ts <= r["_ts"]:
            close_ts = r["_ts"] + 600
        events.append((r["_ts"], cost, close_ts))
        daily[dt.datetime.fromtimestamp(close_ts, dt.timezone.utc).date().isoformat()] += pnl
        per_trade.append({"pnl": pnl, "cost": cost, "hold_h": (close_ts - r["_ts"]) / 3600.0,
                          "won": r["won"], "split": r["split"], "cat": r["cat"],
                          "event": r["event_slug"], "day": r["_day"], "p": r["p_fav"],
                          "q": r["question"], "id": r["id"]})

    # integrate capital employed over wall-clock time
    marks = []
    for t0, cost, t1 in events:
        marks.append((t0, cost))
        marks.append((t1, -cost))
    marks.sort()
    cur = 0.0
    area = 0.0
    peak = 0.0
    prev_t = marks[0][0]
    for t, d in marks:
        area += cur * (t - prev_t)
        peak = max(peak, cur)
        prev_t = t
        cur += d
    span_s = marks[-1][0] - marks[0][0]
    avg_cap = area / span_s if span_s else 0.0
    span_days = span_s / 86400.0

    total_pnl = sum(p["pnl"] for p in per_trade)
    total_cost = sum(p["cost"] for p in per_trade)
    wins = sum(1 for p in per_trade if p["won"])

    # equity curve by settlement day -> drawdown
    days = sorted(daily)
    eq, cum, mx, dd = [], 0.0, 0.0, 0.0
    for d in days:
        cum += daily[d]
        mx = max(mx, cum)
        dd = min(dd, cum - mx)
        eq.append((d, daily[d], cum))

    worst_day = min(daily.items(), key=lambda kv: kv[1]) if daily else (None, 0.0)
    losses = [p for p in per_trade if not p["won"] and not p["split"]]
    loss_by_day = Counter()
    for p in losses:
        loss_by_day[p["day"]] += 1

    return {
        "n": len(per_trade), "wins": wins, "wr": wins / len(per_trade),
        "pnl": total_pnl, "notional": total_cost,
        "roi_notional": total_pnl / total_cost,
        "avg_capital": avg_cap, "peak_capital": peak,
        "span_days": span_days,
        "per_day_pnl": total_pnl / span_days,
        "per_1k_per_day": (total_pnl / span_days) / max(avg_cap, 1e-9) * 1000.0,
        "trades_per_day": len(per_trade) / span_days,
        "med_hold_h": sorted(p["hold_h"] for p in per_trade)[len(per_trade) // 2],
        "p90_hold_h": sorted(p["hold_h"] for p in per_trade)[int(len(per_trade) * 0.9)],
        "max_dd": dd, "worst_day": worst_day,
        "max_losses_one_day": loss_by_day.most_common(1)[0] if loss_by_day else (None, 0),
        "equity": eq, "trades": per_trade, "losses": losses,
    }


BASE = {
    "delta_h": 3.0, "exclude_cats": {"crypto-updown"}, "cats": None, "tradable_only": True,
    "px_lo": 0.93, "px_hi": 1.001, "min_vol": 10000.0,
    "max_stale_s": 20 * 60, "min_distinct_late": 0,
    "max_per_day": 10_000, "max_per_event": 10_000, "max_per_cat_day": 10_000,
}


def show(name, s):
    if not s:
        print(f"{name:<44} (no trades)")
        return
    print(f"{name:<44} n={s['n']:>5} wr={s['wr']*100:>6.2f}% "
          f"pnl=${s['pnl']:>9,.0f} roi={s['roi_notional']*100:>6.2f}% "
          f"avgcap=${s['avg_capital']:>8,.0f} $/day=${s['per_day_pnl']:>7,.1f} "
          f"$/1k/day=${s['per_1k_per_day']:>7.2f} dd=${s['max_dd']:>8,.0f} "
          f"worst=${s['worst_day'][1]:>8,.0f} hold={s['med_hold_h']:.1f}h")


def main():
    rows = json.load((OUT / "entries.json").open())
    slip = float(os.environ.get("RF_SLIP", "0.002"))
    clip = float(os.environ.get("RF_CLIP", "75"))
    print(f"entries={len(rows)}  clip=${clip:g}  slip={slip}\n")

    print("=== price band sweep (Δ=3h, all non-updown cats, vol>=$10k) ===")
    for lo, hi in ((0.93, 1.001), (0.95, 1.001), (0.97, 1.001), (0.985, 1.001),
                   (0.995, 1.001), (0.999, 1.001), (0.93, 0.97), (0.97, 0.995)):
        f = dict(BASE, px_lo=lo, px_hi=hi)
        show(f"px [{lo:.3f},{hi:.3f})", simulate(rows, f, clip, slip))

    print("\n=== per-category (Δ=3h, px>=0.93) ===")
    cats = sorted({r["cat"] for r in rows if r["cat"] != "crypto-updown"})
    for c in cats:
        f = dict(BASE, cats={c})
        s = simulate(rows, f, clip, slip)
        if s and s["n"] >= 40:
            show(c, s)

    print("\n=== Δ sweep (px>=0.93) ===")
    for d in (6.0, 4.0, 3.0, 2.0, 1.0):
        show(f"Δ={d:g}h", simulate(rows, dict(BASE, delta_h=d), clip, slip))

    print("\n=== volume floor sweep (Δ=3h, px>=0.93) ===")
    for v in (10_000, 25_000, 50_000, 100_000, 250_000):
        show(f"vol>=${v:,}", simulate(rows, dict(BASE, min_vol=float(v)), clip, slip))

    print("\n=== slip sensitivity (Δ=3h, px>=0.93, vol>=$25k) ===")
    for s in (0.0, 0.0005, 0.001, 0.002, 0.005, 0.01):
        show(f"slip={s:.4f}", simulate(rows, dict(BASE, min_vol=25_000.0), clip, s))

    print("\n=== caps (Δ=3h, px>=0.93, vol>=$25k, slip=0.002) ===")
    for cap_ev, cap_day in ((1, 10_000), (1, 20), (1, 10), (2, 20)):
        f = dict(BASE, min_vol=25_000.0, max_per_event=cap_ev, max_per_day=cap_day)
        show(f"max/event={cap_ev} max/day={cap_day}", simulate(rows, f, clip, slip))


if __name__ == "__main__":
    main()
