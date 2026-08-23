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
import re
from collections.abc import Iterable
from pathlib import Path

from .chainlink import twap_over_window

SLUG_RE = re.compile(r"^([a-z]+)-updown-(\d+)m-(\d+)$")

OUTCOMES_PATH = Path.home() / ".pmt" / "corpus" / "outcomes.jsonl"

STALE_S = 600  # no round within 10min before the query span -> corpus too stale to trust


def parse_updown_slug(slug: str) -> dict | None:
    """{'symbol','dur_s','start','end'} from an updown slug, or None if it doesn't match."""
    m = SLUG_RE.match(slug)
    if not m:
        return None
    sym, dur_m, start_s = m.groups()
    start = int(start_s)
    dur_s = int(dur_m) * 60
    return {"symbol": sym, "dur_s": dur_s, "start": start, "end": start + dur_s}


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
    """Settlement TWAP width, R1 convention: 30s at 5m closes, 60s at everything wider."""
    return 30 if dur_s <= 300 else 60


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
            # every redeem on this slug paid $0 -> we held the loser; flip its outcome field.
            held = (rows[0].get("outcome") or "").lower()
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
    return ("up" if settlement > reference else "down"), None


# ---------- priority merge ----------

def build_outcomes(windows: list[dict], wallet: dict[str, str],
                    rounds_by_symbol: dict[str, list[dict]]) -> tuple[list[dict], list[dict]]:
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
        if winner is None:
            dropped.append({"slug": slug, "reason": reason})
        else:
            rows.append({"slug": slug, "winner": winner, "source": "chainlink"})
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
