"""Decision-tape file access — the updown strategy's append-only eval/fire/gate log.

Every consumer (the fleet scoreboard, `pmt crypto shadow`, the `watch`
dashboard, `pmt crypto tape`) used to open the tape file and run its own
json.loads loop with "fire"/"eval"/"gated"/... literals sprinkled through
cli.py and polymarket/shadow.py. One place for the paths, the event-name
constants, and (for full-file scans) the parsing loop itself.

iter_records skips corrupt lines instead of raising — a line can be
truncated mid-write by a concurrently-crashing engine, and a bad record
must never take a consumer down with it (the `watch` dashboard crash
lesson).
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Iterator

UPDOWN_TAPE = "/var/home/hunter/.pmt/engine/updown-tape.jsonl"
BOOK_TAPE = "/var/home/hunter/.pmt/engine/book-tape.jsonl"

EV_FIRE = "fire"
EV_EXIT = "exit"
EV_EVAL = "eval"
EV_GATED = "gated"
EV_ROLL = "roll"
EV_CLEANUP = "cleanup"


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
