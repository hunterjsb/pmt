"""Validated updown outcomes — ground truth the replay harness (pmengine R4) needs.

Priority is strict: wallet redemption beats Chainlink corpus inference,
because for a traded window Polymarket's own settlement already picked the
winner and paid (or didn't) — no inference needed. Chainlink corpus fills in
windows we never traded, but only when the corpus can prove it was fresh
enough at settlement time. That staleness guard exists because a stale
step-extension once mislabeled 9/37 windows and poisoned an A/B: the corpus's
last known round was well behind the window end, but a TWAP still happily
holds the last value flat out to `end` — silently wrong, not obviously wrong.
Windows the guard can't clear are dropped, never guessed.

All functions here are pure (no network, no disk) so the priority/staleness
logic is unit-testable with inline fixtures; `pmt crypto outcomes` in cli.py
does the I/O (tape files, wallet activity, corpus, output file) and calls in.
"""

from __future__ import annotations

import bisect
import json
from collections.abc import Iterable
from pathlib import Path

from .chainlink import twap_over_window
from .updown_slugs import parse_updown_slug  # re-exported: this module's original home

OUTCOMES_PATH = Path.home() / ".pmt" / "corpus" / "outcomes.jsonl"

STALE_S = 600  # no round within 10min before the query span -> corpus too stale to trust

# On-chain rounds land ~30s apart (deviation/heartbeat), so the corpus TWAP is a
# flat-hold interpolation. Measured against wallet + terminal-book witnesses
# (2026-08-23, 48h, n=344): labels under 1bp were WORSE than a coin flip (1/6 vs
# wallet), and one 15m window was wrong at 3.2bp. Below this floor the corpus
# refuses to grade — the terminal book or nothing takes over.
CK_NOISE_FLOOR_BP = 5.0


def extract_updown_slugs(lines: Iterable[str]) -> set[str]:
    """Distinct updown slugs referenced in a tape file's lines. Tolerant of blank/bad JSON."""
    slugs: set[str] = set()
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            r = json.loads(line)
        except ValueError:
            continue
        slug = r.get("slug") if isinstance(r, dict) else None
        if slug and parse_updown_slug(slug):
            slugs.add(slug)
    return slugs


def window_universe(slugs: Iterable[str], since: float, now: float) -> list[dict]:
    """Windows eligible for outcome resolution: fully closed and within [since, now).

    end <= now - 30s (settlement needs a moment to land on-chain/at data-api)
    and start >= since. Sorted ascending by start.
    """
    out = []
    for slug in slugs:
        w = parse_updown_slug(slug)
        if not w or w["end"] > now - 30 or w["start"] < since:
            continue
        out.append({"slug": slug, **w})
    out.sort(key=lambda w: w["start"])
    return out


def ck_settlement_width_s(dur_s: int) -> int:
    """Settlement TWAP width: 60s at every duration.

    The old R1 convention assumed 30s at 5m closes; the measured record says
    otherwise — book-graded 283/284 at 60s vs 277/284 at 30s, and all 6
    windows where the widths disagree resolve the 60s way (analysis/
    settle_width.md). One width, every duration.
    """
    return 60


# ---------- (a) wallet truth — strict priority source ----------

def wallet_outcomes(activity_rows: list[dict]) -> dict[str, str]:
    """{slug: 'up'|'down'} from REDEEM rows in wallet activity.

    A paying REDEEM's `outcome` field names the winner directly. A $0 REDEEM
    means the side we held lost (Polymarket only lets you redeem the outcome
    token you actually hold), so the winner is the other side.
    """
    by_slug: dict[str, list[dict]] = {}
    for a in activity_rows:
        if a.get("type") != "REDEEM":
            continue
        slug = a.get("slug") or ""
        if not parse_updown_slug(slug):
            continue
        by_slug.setdefault(slug, []).append(a)

    out: dict[str, str] = {}
    for slug, rows in by_slug.items():
        paying = next((r for r in rows if (r.get("usdcSize") or 0.0) > 0.5), None)
        if paying is not None:
            outcome = (paying.get("outcome") or "").lower()
        else:
            # Every redeem paid $0 -> we held the loser, but ONLY a row with
            # real size names the side we held: a size-0 dust row can carry the
            # WINNER's label (see docs/LESSONS.md#L22). No sized row -> no
            # guess; the chainlink/gamma fallback grades it.
            sized = next((r for r in rows if (r.get("size") or 0.0) > 0.0), None)
            if sized is None:
                continue
            held = (sized.get("outcome") or "").lower()
            outcome = {"up": "down", "down": "up"}.get(held, "")
        if outcome in ("up", "down"):
            out[slug] = outcome
    return out


# ---------- (b) Chainlink corpus inference, with the staleness guard ----------

def chainlink_outcome(window: dict, rounds: list[dict]) -> tuple[str | None, str | None]:
    """(winner, drop_reason) for one window from the Chainlink corpus.

    winner is 'up'/'down' from the settlement-shaped TWAP (width per
    ck_settlement_width_s) at window end vs the same-width TWAP at window
    start. winner is None (with a reason) when the corpus can't be trusted
    for this window: no round within STALE_S before the query span, or the
    corpus's last known round predates the window end outright.
    """
    if not rounds:
        return None, "no corpus data"
    rounds = sorted(rounds, key=lambda r: r["updated_at"])
    ts_list = [r["updated_at"] for r in rounds]
    w = ck_settlement_width_s(window["dur_s"])
    span_start = window["start"] - w

    if rounds[-1]["updated_at"] < window["end"]:
        return None, "stale: corpus's last round predates window end"
    idx = bisect.bisect_right(ts_list, span_start) - 1
    if idx < 0 or span_start - ts_list[idx] > STALE_S:
        return None, "stale: no round within 10min before query span"

    settlement = twap_over_window(rounds, ts_list, window["end"] - w, window["end"])
    reference = twap_over_window(rounds, ts_list, window["start"] - w, window["start"])
    if settlement is None or reference is None:
        return None, "stale: twap unavailable"  # belt-and-suspenders; guards above should prevent this
    margin_bp = abs(settlement - reference) / reference * 1e4
    if margin_bp < CK_NOISE_FLOOR_BP:
        return None, f"margin {margin_bp:.1f}bp inside corpus noise floor"
    return ("up" if settlement > reference else "down"), None


# ---------- (c) gamma resolution — live tie-break when redemption is silent ----------

def gamma_resolution(markets: list[dict]) -> dict:
    """{'resolved': bool, 'winner': 'up'|'down'|None} from a gamma
    `/markets?slug=...` response (a list; empty when the slug is unknown).

    outcomePrices only pins to "1"/"0" once UMA has actually settled —
    `closed` alone isn't enough, a market can close for trading well before
    resolution proposes/finalizes. Tolerant of the malformed-JSON shapes
    gamma has emitted elsewhere (see scanner._parse_prices).
    """
    if not markets:
        return {"resolved": False, "winner": None}
    m = markets[0]
    try:
        outcomes = json.loads(m.get("outcomes") or "[]")
        prices = [float(p) for p in json.loads(m.get("outcomePrices") or "[]")]
    except (TypeError, ValueError):
        return {"resolved": False, "winner": None}
    if len(outcomes) != 2 or len(prices) != 2:
        return {"resolved": False, "winner": None}
    by_outcome = {str(o).lower(): p for o, p in zip(outcomes, prices)}
    up, down = by_outcome.get("up"), by_outcome.get("down")
    if up is None or down is None:
        return {"resolved": False, "winner": None}
    if up >= 0.99:
        return {"resolved": True, "winner": "up"}
    if down >= 0.99:
        return {"resolved": True, "winner": "down"}
    return {"resolved": False, "winner": None}


def grade_window(redeemed_usd: float, redeem_seen: bool, fired_side: str | None,
                  gamma: dict | None, now: float, end: float,
                  grace_s: float = 300) -> tuple[bool | None, bool]:
    """(won, estimated) for one window's wallet totals — worst case first.

    1. A paying redeem (>$0.5): WIN, ground truth from the wallet itself.
    2. A $0 redeem happened with no paying one: LOSS — still ground truth,
       Polymarket only lets you redeem the outcome token you actually hold
       (so a lone $0 redeem can't be dust beside a paying row elsewhere).
    3. No redeem event at all, still inside the settlement grace window:
       riding (None) — too soon to know either way.
    4. Past grace with no redeem: Polymarket doesn't reliably auto-redeem a
       slow-paying WIN (2026-08-23 audit), so a silent wallet no longer
       means LOSS. `gamma` — already fetched by the caller, or None on a
       skipped/failed lookup — breaks the tie: resolved -> WIN/LOSS by
       whether our side matches the winner; not yet resolved -> riding
       (None); unreachable -> fall back to the old assume-LOSS heuristic,
       flagged `estimated` so the UI can mark it as such instead of
       presenting it as confirmed.
    """
    if redeemed_usd > 0.5:
        return True, False
    if redeem_seen:
        return False, False
    if now < end + grace_s:
        return None, False
    if gamma is not None:
        if not gamma.get("resolved"):
            return None, False
        winner = gamma.get("winner")
        if winner and fired_side:
            return winner == fired_side, False
        return None, False  # resolved, but we can't tell which side we held
    return False, True


# ---------- (d) terminal book — last-resort source for UNFILLED windows ----------

BOOK_TERMINAL_S = 15   # only book samples this close to window end count as terminal
BOOK_PIN = 0.95        # winner bid must pin at least here, loser ask at most 1-here

def book_outcome(window: dict, book_records: list[dict]) -> tuple[str | None, str | None]:
    """(winner, drop_reason) from the market's own terminal book. Last-resort
    source: wallet-first grading leaves every window we never FILLED
    ungraded — which is exactly the missed-opportunity population the
    latency/miss studies need (2026-08-23).

    Only samples inside the final BOOK_TERMINAL_S of the window count — a
    tape that stopped mid-window (restart, blackout) must not grade, because
    a 0.96 book with minutes left is a forecast, not a settlement
    (docs/LESSONS.md: drop, never guess). Needs >= 2 agreeing pinned samples
    (winner bid >= BOOK_PIN, its opponent's ask <= 1-BOOK_PIN where quoted)
    and zero samples pinned the other way.
    """
    end = window["end"]
    term = [r for r in book_records
            if (r.get("t") or 0) >= end - BOOK_TERMINAL_S and (r.get("t") or 0) <= end + 120]
    if not term:
        return None, "no terminal book samples"
    up_w = dn_w = 0
    for r in term:
        ub, da = r.get("up_bid"), r.get("dn_ask")
        db, ua = r.get("dn_bid"), r.get("up_ask")
        if ub is not None and ub >= BOOK_PIN and (da is None or da <= 1 - BOOK_PIN):
            up_w += 1
        if db is not None and db >= BOOK_PIN and (ua is None or ua <= 1 - BOOK_PIN):
            dn_w += 1
    if up_w >= 2 and dn_w == 0:
        return "up", None
    if dn_w >= 2 and up_w == 0:
        return "down", None
    return None, "terminal book ambiguous"


# ---------- priority merge ----------

def build_outcomes(windows: list[dict], wallet: dict[str, str],
                    rounds_by_symbol: dict[str, list[dict]],
                    book_by_slug: dict[str, list[dict]] | None = None) -> tuple[list[dict], list[dict]]:
    """Resolve every window by strict priority: wallet, then Chainlink.

    Returns (rows, dropped) — rows are {"slug","winner","source"} ready to
    merge into the outcomes corpus; dropped are {"slug","reason"} for
    windows neither source could validate.
    """
    rows, dropped = [], []
    for w in windows:
        slug = w["slug"]
        if slug in wallet:
            rows.append({"slug": slug, "winner": wallet[slug], "source": "wallet"})
            continue
        winner, reason = chainlink_outcome(w, rounds_by_symbol.get(w["symbol"]) or [])
        if winner is not None:
            rows.append({"slug": slug, "winner": winner, "source": "chainlink"})
            continue
        bwinner, breason = book_outcome(w, (book_by_slug or {}).get(slug) or [])
        if bwinner is not None:
            rows.append({"slug": slug, "winner": bwinner, "source": "book"})
        else:
            dropped.append({"slug": slug, "reason": f"{reason}; {breason}"})
    return rows, dropped


# ---------- outcomes corpus (append + dedupe by slug) ----------

def load_outcomes(path: Path = OUTCOMES_PATH) -> dict[str, dict]:
    """{slug: row} currently on disk."""
    out: dict[str, dict] = {}
    if not path.exists():
        return out
    with open(path) as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                r = json.loads(line)
            except ValueError:
                continue
            if r.get("slug"):
                out[r["slug"]] = r
    return out


def merge_outcomes(existing: dict[str, dict], new_rows: list[dict]) -> tuple[dict[str, dict], int, int]:
    """Merge new_rows into existing by slug. Wallet source upgrades a prior
    chainlink row; nothing ever downgrades wallet, and chainlink never
    overwrites chainlink (first write wins — the walk order doesn't matter).

    Returns (merged, n_added, n_upgraded).
    """
    merged = dict(existing)
    added = upgraded = 0
    for r in new_rows:
        prev = merged.get(r["slug"])
        if prev is None:
            merged[r["slug"]] = r
            added += 1
        elif prev["source"] == "chainlink" and r["source"] == "wallet":
            merged[r["slug"]] = r
            upgraded += 1
        elif prev["source"] == "book" and r["source"] in ("wallet", "chainlink"):
            merged[r["slug"]] = r
            upgraded += 1
    return merged, added, upgraded


def write_outcomes(merged: dict[str, dict], path: Path = OUTCOMES_PATH) -> None:
    """Full rewrite, chronological by window start — the only way to dedupe a JSONL file by key."""
    path.parent.mkdir(parents=True, exist_ok=True)

    def sort_key(slug: str) -> tuple:
        w = parse_updown_slug(slug)
        return (0, w["start"]) if w else (1, slug)

    with open(path, "w") as fh:
        for slug in sorted(merged, key=sort_key):
            fh.write(json.dumps(merged[slug]) + "\n")
