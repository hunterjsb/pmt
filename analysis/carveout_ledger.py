#!/usr/bin/env python3
"""Ledger of the `banked_decided` carve-out and the last-120s full-budget
unlock: every REAL fire either one of them let through, graded against
wallet truth.

This is the stake, not a counterfactual. It answers "how much money has
flowed through this waiver, in both directions" so that the A/B in
analysis/carveout_ab.md has a denominator.

Attribution is read straight off the decision code, not guessed:

  distrust carve-out  `distrust_blocks(net, 0.15, bd) = net > 0.15 && !bd`.
                      A clip only fires with `brake == None`, so a fire
                      whose recorded `net > 0.15` PROVES `banked_decided`
                      was true and PROVES the carve-out is the only reason
                      it fired. Exact, no inference.

  avg_down carve-out  `avg_down_blocks(ask, prev, 0.02, bd)`. Same
                      argument against the window's previous fire on that
                      same token. Exact.

  latch carve-out     `brake_latched && !bd` blocks. A fire in a window
                      that already raised a raw brake fired only because
                      bd was true. The latch itself is reconstructed from
                      the eval tape, which is throttled to 5s, so this
                      class is a LOWER bound and is reported apart.

  late unlock         `budget_unlocked(now, end, 120, bd)`. Fires with
                      <=120s left run on the full budget rather than
                      early_frac. Split by whether bd ALSO held (the
                      clock is then redundant) or the clock alone opened
                      the budget.

Per-fire P&L uses replay's own settlement convention (`settle_pnl`):
a winning share pays 1.0, cost is size*price, fee is
size * fee_rate * min(price, 1-price).

Read-only over ~/.pmt. Runs against FROZEN tape copies (L33) — see
--tape/--outcomes defaults.
"""
import argparse
import collections
import datetime
import json
import os
import sys

WORK = "/var/home/hunter/Desktop/code/pmt-carveout-work"
DISTRUST_NET = 0.15     # BOOK_DISTRUST_NET
AVG_DOWN_TOL = 0.02     # AVG_DOWN_TOL
LATE_REM_S = 120.0      # late_rem_s, every live arm
FEE_RATE = 0.07

# The three events the campaign named.
NAMED = {
    1787505300: "17:15Z five-arm correlated loss",
    1787462100: "05:15Z eth/sol escalating-clip loss",
}


def utc(t):
    return datetime.datetime.fromtimestamp(t, datetime.timezone.utc).strftime("%H:%M:%S")


def window_end(slug):
    """end epoch from the slug itself: <coin>-updown-<dur>m-<start>."""
    try:
        coin, _, dur, start = slug.split("-")
        return float(start) + int(dur.rstrip("m")) * 60, coin, int(start)
    except ValueError:
        return None, None, None


def load(tape_path, outcomes_path):
    fires, evals = collections.defaultdict(list), collections.defaultdict(list)
    for line in open(tape_path):
        try:
            r = json.loads(line)
        except ValueError:
            continue
        ev, slug = r.get("ev"), r.get("slug")
        if not slug:
            continue
        if ev == "fire":
            fires[slug].append(r)
        elif ev == "eval":
            evals[slug].append(r)
    outcomes = {}
    for line in open(outcomes_path):
        try:
            r = json.loads(line)
        except ValueError:
            continue
        outcomes[r["slug"]] = (r["winner"], r.get("source", "?"))
    for v in fires.values():
        v.sort(key=lambda r: r["t"])
    for v in evals.values():
        v.sort(key=lambda r: r["t"])
    return fires, evals, outcomes


def bd_at(evs, t, tol=6.0):
    """banked_decided from the nearest eval record within `tol` seconds."""
    best, bestd = None, tol
    for e in evs:
        d = abs(e["t"] - t)
        if d <= bestd and "banked_decided" in e:
            best, bestd = e["banked_decided"], d
    return best


def latch_before(evs, t):
    """Did a raw brake (distrust/avg_down) show on the eval tape before t?

    Reconstructed two ways, either sufficient: an explicit `brake` label,
    or the distrust predicate recomputed from a side's own net + the
    record's banked_decided.
    """
    for e in evs:
        if e["t"] >= t:
            break
        bd = e.get("banked_decided")
        for s in e.get("sides", []):
            if s.get("brake") in ("distrust", "avg_down"):
                return True
            net = s.get("net")
            if net is not None and bd is False and net > DISTRUST_NET:
                return True
    return False


def grade(rec, winner):
    """Per-fire wallet-graded P&L, replay's settle_pnl convention."""
    px = rec.get("limit", rec["ask"])
    size = rec["size"]
    fee = size * FEE_RATE * min(px, 1.0 - px)
    payoff = size if rec["side"] == winner else 0.0
    return payoff - size * px - fee


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--tape", default=os.path.join(WORK, "updown-tape-frozen.jsonl"))
    ap.add_argument("--outcomes", default=os.path.join(WORK, "outcomes-frozen.jsonl"))
    ap.add_argument("--json-out", default=os.path.join(WORK, "carveout-ledger.json"))
    a = ap.parse_args()

    fires, evals, outcomes = load(a.tape, a.outcomes)

    rows = []
    for slug, fs in fires.items():
        end, coin, start = window_end(slug)
        if end is None:
            continue
        got = outcomes.get(slug)
        last_ask = {}
        latched_seen = latch_before(evals[slug], float("inf"))
        for r in fs:
            t, side = r["t"], r["side"]
            token = f"{slug}-{'u' if side == 'up' else 'd'}"
            rem = end - t
            bd = bd_at(evals[slug], t)
            # rem > 120 and the arm is in safe mode => the unlock came from
            # decidedness, which is itself proof of bd.
            if r.get("mode") == "safe" and rem > LATE_REM_S:
                bd = True
            prev = last_ask.get(token)
            tags = []
            if r["net"] > DISTRUST_NET:
                tags.append("distrust")
            if prev is not None and r["ask"] < prev - AVG_DOWN_TOL:
                tags.append("avg_down")
            if not tags and latch_before(evals[slug], t):
                tags.append("latched")
            # A distrust/avg_down carve-out fire PROVES banked_decided —
            # the predicates return false only when bd is true. Trust the
            # proof over the 5s-throttled eval tape, which misses exactly
            # the late flips this study is about (1787508300 flipped 3s
            # before its 0.54 fire, and the nearest eval still reads
            # undecided).
            if set(tags) & {"distrust", "avg_down"}:
                bd = True
            late = 0.0 <= rem <= LATE_REM_S
            rows.append({
                "slug": slug, "coin": coin, "start": start, "t": t, "rem": rem,
                "side": side, "ask": r["ask"], "px": r.get("limit", r["ask"]),
                "size": r["size"], "net": r["net"], "fair": r["fair"],
                "mode": r.get("mode"), "bd": bd,
                "notional": r["size"] * r.get("limit", r["ask"]),
                "carve": tags, "late": late,
                "late_clock_only": late and bd is False,
                "winner": got[0] if got else None,
                "src": got[1] if got else None,
                "pnl": grade(r, got[0]) if got else None,
            })
            last_ask[token] = r["ask"]

    graded = [r for r in rows if r["pnl"] is not None]
    print(f"tape: {len(rows)} fires across {len(fires)} windows; "
          f"{len(graded)} graded by wallet/resolution truth "
          f"({len(rows) - len(graded)} ungraded, excluded)\n")

    def tally(name, sel):
        sub = [r for r in graded if sel(r)]
        if not sub:
            print(f"{name:34} (none)")
            return None
        wins = [r for r in sub if r["pnl"] > 0]
        loss = [r for r in sub if r["pnl"] <= 0]
        net = sum(r["pnl"] for r in sub)
        print(f"{name:34} {len(sub):>5} fires  ${sum(r['notional'] for r in sub):>9,.0f} notional  "
              f"W {len(wins):>4} +${sum(r['pnl'] for r in wins):>8,.2f}   "
              f"L {len(loss):>4} -${-sum(r['pnl'] for r in loss):>8,.2f}   "
              f"NET ${net:>+9,.2f}")
        return {"fires": len(sub), "notional": sum(r["notional"] for r in sub),
                "wins": len(wins), "won": sum(r["pnl"] for r in wins),
                "losses": len(loss), "lost": sum(r["pnl"] for r in loss),
                "net": net}

    print("=" * 118)
    print("CARVE-OUT LEDGER — real fires, wallet-graded")
    print("=" * 118)
    out = {}
    out["all"] = tally("ALL fires (denominator)", lambda r: True)
    print("-" * 118)
    out["distrust"] = tally("carve-out: distrust waived", lambda r: "distrust" in r["carve"])
    out["avg_down"] = tally("carve-out: avg_down waived", lambda r: "avg_down" in r["carve"])
    out["latched"] = tally("carve-out: latch waived (lower bd)", lambda r: "latched" in r["carve"])
    out["carve_any"] = tally("carve-out: ANY of the three", lambda r: bool(r["carve"]))
    out["carve_hard"] = tally("carve-out: distrust|avg_down only", lambda r: bool(
        set(r["carve"]) & {"distrust", "avg_down"}))
    print("-" * 118)
    out["late"] = tally("late unlock: <=120s left", lambda r: r["late"])
    out["late_clock"] = tally("late unlock: clock alone (bd false)", lambda r: r["late_clock_only"])
    out["late_bd"] = tally("late unlock: bd also true", lambda r: r["late"] and r["bd"] is True)
    print("-" * 118)
    out["family"] = tally("FAMILY = carve-out OR late unlock",
                          lambda r: bool(r["carve"]) or r["late"])
    out["neither"] = tally("neither (plain early/spec fires)",
                           lambda r: not r["carve"] and not r["late"])

    # --- the split that decides the verdict --------------------------------
    # docs/LESSONS.md L39 / analysis/fourh_fit.md: the range_avg "banked
    # mass" is settlement arithmetic only under a range-average rule. Under
    # the true terminal rule it is a MOMENTUM PROXY that works at 5m and
    # lies with duration. If that is right, the carve-out should pay at 5m
    # and be the whole hole at 15m. It is, and it is.
    print("\n" + "=" * 118)
    print("BY DURATION — the axis the waiver actually splits on")
    print("=" * 118)
    print(f"{'dur':>5} {'class':<22} {'fires':>6} {'notional':>11} {'W':>5} {'L':>5} {'NET':>11}")
    print("-" * 74)
    for dur in ("5m", "15m"):
        for label, sel in (
            ("carve-out waived", lambda r: bool(r["carve"])),
            ("late unlock only", lambda r: r["late"] and not r["carve"]),
            ("neither", lambda r: not r["late"] and not r["carve"]),
            ("ALL", lambda r: True),
        ):
            sub = [r for r in graded if r["slug"].split("-")[2] == dur and sel(r)]
            if not sub:
                continue
            net = sum(r["pnl"] for r in sub)
            w = sum(1 for r in sub if r["pnl"] > 0)
            print(f"{dur:>5} {label:<22} {len(sub):>6} "
                  f"${sum(r['notional'] for r in sub):>10,.0f} {w:>5} {len(sub) - w:>5} "
                  f"${net:>+10,.2f}")
        print("-" * 74)

    # --- the three named events ------------------------------------------
    print("\n" + "=" * 118)
    print("NAMED EVENTS")
    print("=" * 118)
    for start, label in sorted(NAMED.items()):
        sub = [r for r in graded if r["start"] == start]
        if not sub:
            continue
        print(f"\n{start} — {label}   net ${sum(r['pnl'] for r in sub):+,.2f} "
              f"over {len(sub)} fires / {len({r['slug'] for r in sub})} arms")
        hdr = (f"  {'time':>8} {'arm':>4} {'side':>5} {'rem':>6} {'ask':>6} {'size':>7} "
               f"{'net':>7} {'notional':>9} {'pnl':>9}  carve")
        print(hdr)
        for r in sorted(sub, key=lambda r: r["t"]):
            print(f"  {utc(r['t']):>8} {r['coin']:>4} {r['side']:>5} {r['rem']:>6.0f} "
                  f"{r['ask']:>6.3f} {r['size']:>7.0f} {r['net']:>7.3f} "
                  f"{r['notional']:>9.2f} {r['pnl']:>+9.2f}  "
                  f"{','.join(r['carve']) or '-'}{' LATE' if r['late'] else ''}"
                  f"{' bd' if r['bd'] else ''}")

    # --- worst windows in the family -------------------------------------
    print("\n" + "=" * 118)
    print("WORST FAMILY WINDOWS (carve-out or late-unlock notional present)")
    print("=" * 118)
    per = collections.defaultdict(lambda: [0.0, 0.0, 0])
    for r in graded:
        if r["carve"] or r["late"]:
            per[r["slug"]][0] += r["pnl"]
            per[r["slug"]][1] += r["notional"]
            per[r["slug"]][2] += 1
    for slug, (pnl, notl, n) in sorted(per.items(), key=lambda kv: kv[1][0])[:15]:
        print(f"  {slug:34} {n:>3} fires  ${notl:>8,.0f} notional   ${pnl:>+9,.2f}")

    with open(a.json_out, "w") as fh:
        json.dump({"summary": out, "rows": rows}, fh)
    print(f"\n[wrote] {a.json_out}", file=sys.stderr)


if __name__ == "__main__":
    main()
