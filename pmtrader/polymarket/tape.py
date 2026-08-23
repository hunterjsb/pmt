"""Decision-tape file access — the updown strategy's append-only eval/fire/gate log.

Every consumer (the fleet scoreboard, `stats --gates`, the `watch`
dashboard, `pmt crypto tape`) used to open the tape file and run its own
json.loads loop with "fire"/"eval"/"gated"/... literals sprinkled through
cli_crypto.py and polymarket/shadow.py. One place for the paths, the
event-name constants, and (for full-file scans) the parsing loop itself.

iter_records skips corrupt lines instead of raising — a line can be
truncated mid-write by a concurrently-crashing engine, and a bad record
must never take a consumer down with it (the `watch` dashboard crash
lesson).
"""

from __future__ import annotations

import json
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
