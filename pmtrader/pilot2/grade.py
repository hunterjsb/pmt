"""`pilot2 grade` — score the shadow tape against the exchange's own answer.

Nothing the model believes ever grades a window. The verdict comes from gamma's
resolution, which is the number redemptions are PAID on — the same authority
that produces wallet rows — and `windows.resolution` pins `closed=true` so a
settled window cannot answer `[]` and be mistaken for one still riding.

Two outputs, and they are different jobs:

  * `graded.jsonl` — realized P&L per would-be trade, on `ev_policy.replay`'s
    accounting: a winner pays $1/share, the loss leg is exactly -100% of
    notional, fees are charged either way.
  * `blend-weight.json` — the walk-forward weight refit on the `calib` rows of
    windows that have now resolved. Walk-forward BY CONSTRUCTION: a row cannot
    exist until its window has settled, so no fit sees a row it is later
    scored on.

Idempotent by (slug, side) for grades and by slug for calibration rows, so a
re-run adds nothing it already said.
"""

from __future__ import annotations

import time

from . import policy, state, windows

# A window is not graded until settlement has had time to land on-chain and at
# gamma. The outcomes corpus uses the same 30s floor before it will even
# consider a window; 300s is the resolution grace the fleet's grader waits.
GRADE_AFTER_S = 300.0


def _graded_keys(home) -> set[tuple[str, str]]:
    return {(r.get("slug", ""), r.get("side", ""))
            for r in state.iter_records(state.GRADED, home)}


def pending(home, now: float | None = None) -> list[dict]:
    """Would-be trades whose window has settled and that are not graded yet."""
    now = time.time() if now is None else now
    done = _graded_keys(home)
    out = []
    for r in state.iter_records(state.SHADOW_TAPE, home, evs=(state.EV_SHADOW,)):
        slug, side = r.get("slug", ""), r.get("side", "")
        end = r.get("end")
        if not slug or not side or not isinstance(end, (int, float)):
            continue
        if now < end + GRADE_AFTER_S or (slug, side) in done:
            continue
        out.append(r)
    return out


def grade_record(rec: dict, winner: str) -> dict:
    """One would-be trade + the exchange's winner -> a graded row."""
    won = rec.get("side") == winner
    shares = float(rec.get("shares") or 0.0)
    ask = float(rec.get("ask") or 0.0)
    return {
        "ev": "graded", "slug": rec.get("slug"), "series": rec.get("series"),
        "side": rec.get("side"), "mode": rec.get("mode"), "end": rec.get("end"),
        "ask": ask, "shares": round(shares, 4),
        "notional": round(float(rec.get("notional") or shares * ask), 4),
        "edge": rec.get("edge"), "p_side": rec.get("p_side"),
        "model_p_up": rec.get("model_p_up"), "book_p_up": rec.get("book_p_up"),
        "blend_p_up": rec.get("blend_p_up"), "w": rec.get("w"),
        "winner": winner, "won": won,
        "pnl": round(policy.realized_pnl(shares, ask, won), 4),
    }


def calibration_rows(home, now: float | None = None,
                     resolve=None) -> list[tuple[float, float, int]]:
    """(model_p_up, book_p_up, y) for every calib sample whose window resolved.

    y is the WINDOW's outcome (1 = up), not the side we took: the weight is
    fitted on P(up), the quantity the blend estimates.
    """
    now = time.time() if now is None else now
    resolve = resolve or (lambda slug: windows.resolution(slug))
    rows: list[tuple[float, float, int]] = []
    seen: set[str] = set()
    cache = {r.get("slug"): r for r in state.iter_records(state.GRADED, home)
             if r.get("winner")}
    for r in state.iter_records(state.CALIB, home, evs=(state.EV_CALIB,)):
        slug = r.get("slug", "")
        end = r.get("end")
        if not slug or slug in seen or not isinstance(end, (int, float)):
            continue
        if now < end + GRADE_AFTER_S:
            continue
        seen.add(slug)
        winner = (cache.get(slug) or {}).get("winner") or (resolve(slug) or {}).get("winner")
        if winner not in ("up", "down"):
            continue
        m, b = r.get("model_p_up"), r.get("book_p_up")
        if not isinstance(m, (int, float)) or not isinstance(b, (int, float)):
            continue
        rows.append((float(m), float(b), 1 if winner == "up" else 0))
    return rows


def run(home, now: float | None = None, resolve=None, log=print) -> dict:
    """Grade everything gradeable, refit the weight, return a summary."""
    now = time.time() if now is None else now
    resolve = resolve or (lambda slug: windows.resolution(slug))
    home = state.ensure_home(home)

    verdicts: dict[str, dict] = {}
    graded = unresolved = 0
    pnl = notional = 0.0
    wins = 0
    for rec in pending(home, now):
        slug = rec["slug"]
        if slug not in verdicts:
            verdicts[slug] = resolve(slug) or {}
        v = verdicts[slug]
        if not v.get("resolved") or v.get("winner") not in ("up", "down"):
            unresolved += 1
            continue
        row = grade_record(rec, v["winner"])
        state.append(state.GRADED, row, home)
        graded += 1
        wins += int(row["won"])
        pnl += row["pnl"]
        notional += row["notional"]

    rows = calibration_rows(home, now, resolve)
    w, source, n = policy.fit_blend_weight(rows)
    state.write_json(state.BLEND_WEIGHT,
                     {"w": w, "source": source, "rows": n,
                      "min_rows": policy.MIN_FIT_ROWS, "seed": policy.W_SEED,
                      "fitted_at": round(now, 3)}, home)

    summary = {
        "graded": graded, "unresolved": unresolved, "wins": wins,
        "pnl": round(pnl, 4), "notional": round(notional, 4),
        "c_per_dollar": round(100.0 * pnl / notional, 2) if notional > 0 else None,
        "w": w, "w_source": source, "w_rows": n,
    }
    log(f"graded {graded} would-be trades ({wins} won), P&L ${pnl:+.2f} "
        f"on ${notional:.2f} notional; {unresolved} still unresolved")
    log(f"blend weight w={w:.2f} ({source}, {n} calibration rows, "
        f"need {policy.MIN_FIT_ROWS} to fit)")
    return summary
