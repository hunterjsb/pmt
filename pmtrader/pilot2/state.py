"""Where the pilot keeps its own state, and the append-only writer.

The pilot writes ONLY under `~/.pmt/pilot2/` (override `PILOT2_HOME`). It reads
`~/.pmt/corpus` and the engine tapes; it never writes there. Two separate
services appending to one file is how a tape gets torn, and the corpora are the
one thing in this project that cannot be rebuilt.

Every path function takes an explicit `home`, so tests point at a tmp dir and
nothing in this package can reach the real state directory by accident.
"""

from __future__ import annotations

import json
import os
import time
from collections.abc import Iterator
from pathlib import Path

from polymarket import errlog

HOME_ENV = "PILOT2_HOME"

SHADOW_TAPE = "shadow-tape.jsonl"      # would-be trades + refusals + window summaries
LIVE_TAPE = "live-tape.jsonl"          # real order decisions, acks, errors
GRADED = "graded.jsonl"                # resolved shadow decisions with realized P&L
CALIB = "calib.jsonl"                  # one (model_p, book_p) row per window, for the blend fit
BLEND_WEIGHT = "blend-weight.json"     # the fitted w in force, written by the grader
REDEEM_QUEUE = "redeem-queue.jsonl"    # live positions awaiting the manual sweep
SERVICE_LOG = "pilot2.log"

# Tape event names. One vocabulary, used by the writer, the grader and status.
EV_SHADOW = "shadow"       # the EV gate passed: this is the would-be trade
EV_REFUSED = "refused"     # the EV gate passed but a risk law said no
EV_WINDOW = "window"       # per-window summary written at close
EV_CALIB = "calib"         # blend-fit sample, one per window, gate-independent
EV_ORDER = "order"         # live: an order was built and sent
EV_ACK = "ack"             # live: the exchange answered
# Redeem-queue rows. A CANDIDATE is written the moment a live clip is booked,
# before the order leaves the process: a position that filled and then lost its
# process was invisible to the sweep, because the only row was written at
# settlement by a `_retire` the dead process never ran. DUE is that settlement
# row, and it supersedes the candidate for the same (slug, side).
EV_REDEEM_CANDIDATE = "redeem_candidate"
EV_REDEEM_DUE = "redeem_due"
EV_REHYDRATE = "rehydrate"  # live: the risk book was rebuilt from this tape
EV_ERROR = "error"
EV_START = "start"
EV_STOP = "stop"
EV_HALT = "halt"


def pilot_home(home: str | Path | None = None) -> Path:
    """The pilot's state directory. Explicit argument > PILOT2_HOME > default."""
    if home is not None:
        return Path(home).expanduser()
    env = os.environ.get(HOME_ENV)
    if env:
        return Path(env).expanduser()
    return Path.home() / ".pmt" / "pilot2"


def ensure_home(home: str | Path | None = None) -> Path:
    p = pilot_home(home)
    p.mkdir(parents=True, exist_ok=True)
    return p


def path(name: str, home: str | Path | None = None) -> Path:
    return pilot_home(home) / name


def append(name: str, rec: dict, home: str | Path | None = None) -> None:
    """One JSONL line, line-buffered, never fsynced.

    A crash can cost the tail line. An fsync per record would cost an I/O
    stall inside the poll loop, and a stalled poll loses a whole book refresh
    rather than one line — the same trade the RTDS recorder makes.
    """
    d = ensure_home(home)
    rec = {"t": round(time.time(), 3), **rec} if "t" not in rec else rec
    with open(d / name, "a", buffering=1) as fh:
        fh.write(json.dumps(rec, separators=(",", ":")) + "\n")


def iter_records(name: str, home: str | Path | None = None,
                 evs: tuple[str, ...] | None = None) -> Iterator[dict]:
    """Parsed records from one of the pilot's tapes, oldest first.

    Skips blank and corrupt lines rather than raising: a line can be truncated
    mid-write by a service that is dying, and a bad record must never take the
    grader or the status view down with it (the watch-dashboard crash lesson).
    """
    try:
        fh = open(pilot_home(home) / name)
    except (FileNotFoundError, NotADirectoryError):
        return
    with fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                r = json.loads(line)
            except ValueError as e:
                # pilot2's shadow tape IS its ledger — `pilot2 grade` refits the
                # blend weight off these lines, so one lost silently moves a
                # live parameter.
                errlog.note("pilot2.state.iter_records.corrupt_line", e,
                            name=name, line=line[:200])
                continue
            if not isinstance(r, dict):
                continue
            if evs is not None and r.get("ev") not in evs:
                continue
            yield r


def read_json(name: str, home: str | Path | None = None, default: dict | None = None) -> dict:
    try:
        d = json.loads((pilot_home(home) / name).read_text())
    except (OSError, ValueError):
        return dict(default or {})
    return d if isinstance(d, dict) else dict(default or {})


def write_json(name: str, payload: dict, home: str | Path | None = None) -> None:
    """Atomic replace — status reads this file concurrently with the grader
    writing it, and a half-written blend weight would be read as a real one."""
    d = ensure_home(home)
    tmp = d / (name + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")
    tmp.replace(d / name)
