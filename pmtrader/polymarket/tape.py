"""Decision-tape file access — the updown strategy's append-only eval/fire/gate log.

Every consumer (the fleet scoreboard, `stats --gates`, the `watch`
dashboard, `pmt crypto tape`) used to open the tape file and run its own
json.loads loop with "fire"/"eval"/"gated"/... literals sprinkled through
the cli_crypto_* modules and polymarket/shadow.py. One place for the paths, the
event-name constants, and (for full-file scans) the parsing loop itself.

iter_records skips corrupt lines instead of raising — a line can be
truncated mid-write by a concurrently-crashing engine, and a bad record
must never take a consumer down with it (the `watch` dashboard crash
lesson).
"""

from __future__ import annotations

import json
import os
from collections.abc import Iterable, Iterator
from pathlib import Path

# Where the engine appends (updown_model.rs::append_jsonl writes under
# $HOME/.pmt/engine). Derived, not hardcoded — same shape as
# outcomes.OUTCOMES_PATH. str, not Path: every consumer passes these
# straight to open() and some log them.
ENGINE_DIR = Path.home() / ".pmt" / "engine"
UPDOWN_TAPE = str(ENGINE_DIR / "updown-tape.jsonl")
BOOK_TAPE = str(ENGINE_DIR / "book-tape.jsonl")
# Phase 7 order-path telemetry (pmengine/src/order_tape.rs) — one record per
# order decision. Its own file because the order path samples on a completely
# different clock from the 5s eval throttle; joins to UPDOWN_TAPE on t+token.
ORDER_TAPE = str(ENGINE_DIR / "order-latency-tape.jsonl")
# Not a tape — the engine's live arm store, rewritten on every mutation. It
# lives here anyway because it is the same directory under the same rule:
# one place for the paths, so a consumer can't drift onto a stale copy.
ARMS_STATE = str(ENGINE_DIR / "arms-state.json")

EV_FIRE = "fire"
EV_EXIT = "exit"
EV_EVAL = "eval"
EV_GATED = "gated"
EV_ROLL = "roll"
EV_CLEANUP = "cleanup"

# ORDER_TAPE "stage" values — the leg of the order path a record describes.
STAGE_ACK = "ack"
STAGE_SUPPRESSED = "suppressed"
STAGE_FILL = "fill"


def iter_records(path: str, floor: float | None = None,
                  evs: Iterable[str] | None = None) -> Iterator[dict]:
    """Yield parsed dict records from a tape file, oldest to newest.

    Skips blank and corrupt (unparseable / non-dict) lines. `floor` drops
    records whose "t" is below it (missing "t" treated as 0). `evs`, if
    given, restricts to records whose "ev" is in it. Yields nothing (no
    error) when the file doesn't exist yet.
    """
    evs_set = set(evs) if evs is not None else None
    try:
        fh = open(path)
    except FileNotFoundError:
        return
    with fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                r = json.loads(line)
            except ValueError:
                continue
            if not isinstance(r, dict):
                continue
            if floor is not None and r.get("t", 0) < floor:
                continue
            if evs_set is not None and r.get("ev") not in evs_set:
                continue
            yield r


# The tail read a caller needs to answer "how fresh is this file" — a few
# hundred records' worth, never the whole tape (updown-tape.jsonl passed 24MB
# in August 2026 and only grows).
_TAIL_BYTES = 64 * 1024


def record_t(raw: str | bytes) -> float | None:
    """One raw tape line's `t`, or None if it isn't a record with a numeric
    one — a torn mid-write append, a blank line, the partial line a seek into
    the middle of the file landed inside.

    ONE parse rule for "does this line carry a timestamp", so the freshness
    check below and the watch dashboard's dedupe cursor can't disagree about
    which lines count.
    """
    try:
        r = json.loads(raw)
    except (ValueError, TypeError):
        return None
    if isinstance(r, dict) and isinstance(r.get("t"), (int, float)) \
            and not isinstance(r.get("t"), bool):
        return float(r["t"])
    return None


def record_key(raw: str | bytes) -> str | None:
    """A raw tape line's IDENTITY — canonical JSON, or None if it isn't a
    record at all.

    `t` alone is not an identity: the engine writes one record per armed token
    per fleet tick and they all carry that tick's `t` (up to 14 of them). A
    consumer deduping on `t` keeps the lexically-first slug and throws the rest
    of the fleet away. Canonical because the same record reaches the watch
    panel two ways — Rust's compact `serde_json` off the file, Python's
    spaced `json.dumps` off the control plane — and byte compare would call
    those two different records.
    """
    try:
        r = json.loads(raw)
    except (ValueError, TypeError):
        return None
    if not isinstance(r, dict):
        return None
    return json.dumps(r, sort_keys=True, separators=(",", ":"))


def newest_t(path: str, tail_bytes: int = _TAIL_BYTES) -> float | None:
    """The `t` of the newest parseable record in a tape, or None if there
    isn't one (no file, empty file, nothing parseable in the tail).

    Reads backwards from the last `tail_bytes` and stops at the first record
    it can read, so the cost is flat in file size. Used to decide whether a
    LOCAL tape is still being written — a laptop pointed at a remote engine
    has either no file at all or one frozen hours ago.
    """
    try:
        with open(path, "rb") as fh:
            fh.seek(0, os.SEEK_END)
            size = fh.tell()
            fh.seek(max(0, size - tail_bytes))
            chunk = fh.read()
    except OSError:
        return None
    for raw in reversed(chunk.split(b"\n")):
        t = record_t(raw)
        if t is not None:
            return t
    return None
