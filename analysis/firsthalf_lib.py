"""Shared loader for the first-half (idle-window) research pass.

The R9 entry gate needs banked TWAP evidence that accrues ~1 mark/minute, so
directional entries land 50-60% through a window and the first half is
structurally idle. Everything here reads the recorded corpus only — book tape,
updown eval tape, validated outcomes — and answers what that idle half is worth.

Pure functions + a couple of loaders; the study scripts do the reporting.
"""

from __future__ import annotations

import json
import os
import re
from collections import defaultdict
from pathlib import Path

# Point PMT_BOOK_TAPE at a FROZEN copy for any reproducible study — the live
# tape is append-only and still being written (docs/LESSONS.md#L33).
BOOK_TAPE = Path(
    os.environ.get("PMT_BOOK_TAPE", Path.home() / ".pmt" / "engine" / "book-tape.jsonl")
)
UPDOWN_TAPE = Path.home() / ".pmt" / "engine" / "updown-tape.jsonl"
OUTCOMES = Path.home() / ".pmt" / "corpus" / "outcomes.jsonl"

TAKER_FEE_RATE = 0.07  # crypto category; fee = shares * rate * p * (1-p)
SLUG_RE = re.compile(r"^([a-z]+)-updown-(\d+)m-(\d+)$")


def parse_slug(slug: str) -> dict | None:
    m = SLUG_RE.match(slug)
    if not m:
        return None
    sym, dur_m, start = m.groups()
    dur = int(dur_m) * 60
    return {
        "symbol": sym,
        "dur_s": dur,
        "start": int(start),
        "end": int(start) + dur,
        "series": f"{sym} {dur_m}m",
    }


def taker_fee(price: float, shares: float = 1.0) -> float:
    """Polymarket crypto taker fee — same shape updown.rs already uses."""
    return shares * TAKER_FEE_RATE * price * (1.0 - price)


def load_book_windows(path: Path = BOOK_TAPE) -> dict[str, list[dict]]:
    """slug -> samples sorted by t, each annotated with window meta + elapsed_frac.

    Samples outside [start, end] (the roll-boundary stragglers) are dropped:
    a quote recorded after settlement isn't part of the window's life.
    """
    by_slug: dict[str, list[dict]] = defaultdict(list)
    with path.open() as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                d = json.loads(line)
            except ValueError:
                continue
            if d.get("ev") != "book":
                continue
            w = parse_slug(d.get("slug", ""))
            if not w:
                continue
            t = d["t"]
            if t < w["start"] or t > w["end"]:
                continue
            d["_w"] = w
            d["frac"] = (t - w["start"]) / w["dur_s"]
            by_slug[d["slug"]].append(d)
    for s in by_slug.values():
        s.sort(key=lambda r: r["t"])
    return dict(by_slug)


def load_outcomes(path: Path = OUTCOMES) -> dict[str, str]:
    """slug -> 'up'|'down' from the wallet-first validated corpus."""
    out: dict[str, str] = {}
    if not path.exists():
        return out
    with path.open() as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                d = json.loads(line)
            except ValueError:
                continue
            if d.get("winner") in ("up", "down"):
                out[d["slug"]] = d["winner"]
    return out


def terminal_winner(samples: list[dict], decisive: float = 0.9) -> str | None:
    """Winner inferred from the window's own last book, or None if undecided.

    The validated outcomes corpus only reaches part of the book tape (it is
    rebuilt on a slower cadence than the tape is written). For windows it
    doesn't cover, the last recorded book is the market's own verdict: a
    settled up/down pair quotes 0.999/0.001. Only used where the terminal
    book is that decisive, and cross-checked against the validated corpus
    on every window where both exist (see the study's agreement number).
    """
    for r in reversed(samples):
        up = _mid(r.get("up_bid"), r.get("up_ask"))
        dn = _mid(r.get("dn_bid"), r.get("dn_ask"))
        p = None
        if up is not None and dn is not None:
            p = (up + (1.0 - dn)) / 2.0
        elif up is not None:
            p = up
        elif dn is not None:
            p = 1.0 - dn
        if p is None:
            continue
        if p >= decisive:
            return "up"
        if p <= 1.0 - decisive:
            return "down"
        return None
    return None


def _mid(bid, ask):
    if bid is not None and ask is not None:
        return (bid + ask) / 2.0
    if bid is not None:
        return bid
    if ask is not None:
        return ask
    return None


def mid_up(r: dict) -> float | None:
    """Best estimate of P(up) from a book sample, merging both tokens.

    Both legs price the same event, so the down book is a second opinion on
    p_up (1 - dn). Averaging them is the pair-merged mid; falling back to a
    single leg keeps one-sided samples usable.
    """
    up = _mid(r.get("up_bid"), r.get("up_ask"))
    dn = _mid(r.get("dn_bid"), r.get("dn_ask"))
    if up is not None and dn is not None:
        return (up + (1.0 - dn)) / 2.0
    if up is not None:
        return up
    if dn is not None:
        return 1.0 - dn
    return None


def sample_at(samples: list[dict], frac: float) -> dict | None:
    """Last sample at or before `frac` of the window (no look-ahead)."""
    hit = None
    for r in samples:
        if r["frac"] <= frac:
            hit = r
        else:
            break
    return hit


def pct(xs: list[float], q: float) -> float:
    if not xs:
        return float("nan")
    s = sorted(xs)
    i = min(len(s) - 1, max(0, int(round(q * (len(s) - 1)))))
    return s[i]


def wilson(k: int, n: int, z: float = 1.96) -> tuple[float, float]:
    """Wilson score interval — the honest CI for the tiny n this corpus has."""
    if n == 0:
        return (float("nan"), float("nan"))
    p = k / n
    d = 1 + z * z / n
    c = p + z * z / (2 * n)
    h = z * ((p * (1 - p) / n + z * z / (4 * n * n)) ** 0.5)
    return ((c - h) / d, (c + h) / d)
