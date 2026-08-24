"""Swallowed-error surfacing — the counterpart to every `except` this codebase
keeps in order to stay alive.

WHY THIS EXISTS. Most of the broad handlers in pmtrader are correct: a torn
tape line must not take the dashboard down, a flaky balance call must not blank
a report, a dead fetch must not kill the worker thread that owns it. What was
NOT correct is that they were also SILENT. On 2026-08-23 the watch header said
`scoreboard: AttributeError` and that was the entire record of the failure —
no site, no message, no traceback, no count, and no way to know whether it had
happened once or ten thousand times. A loop that quietly stops producing is
indistinguishable from a market with nothing to say.

So the fix for a too-broad handler is NOT to let it crash the loop. It is to
keep the belt and make the failure LEAVE A MARK:

    try:
        ...
    except Exception as e:
        errlog.note("wallet.fetch_activity_page", e, addr=addr, offset=offset)
        <the same degradation as before>

WHAT IT COSTS THE OPERATOR'S TERMINAL. The FIRST occurrence of each
(site, exception type) prints a full traceback to stderr and writes one JSONL
line. Every occurrence after that is counted in memory and written only on a
power-of-two boundary (2nd, 4th, 8th, 1024th, ...) — so a storm escalates
visibly and bounds its own noise, and a site that failed 10,000 times can never
again look like a site that failed once. Nothing is ever printed twice.

THE FILE. `~/.pmt/engine/swallowed-errors.jsonl`, one JSON object per line,
read back by `pmt crypto errors`. It is a diagnostic log and nothing reads it
to make a decision, so it is size-capped and rotated one generation deep rather
than retained: a box that runs for months must not accumulate an unbounded file
because something upstream is broken.

THE ONE HARD RULE: note() NEVER RAISES. It is called from inside exception
handlers whose entire job is to keep something alive — a read-only `~/.pmt`, a
full disk or an object that explodes on repr() must all degrade to "the mark
was not written", never to a second exception thrown from the recovery path.
"""

from __future__ import annotations

import json
import os
import sys
import threading
import time
import traceback
from pathlib import Path

# Same derivation as tape.ENGINE_DIR, deliberately NOT imported from it: tape
# is one of the modules this instruments, and a cycle through the error path is
# the last place to discover an import problem.
ENGINE_DIR = Path.home() / ".pmt" / "engine"
ERRLOG_NAME = "swallowed-errors.jsonl"

# Bytes. Past this the file rotates to `<name>.1` (one generation, replaced) so
# a wedged site can't fill the disk it is trying to report from.
MAX_BYTES = 4 * 1024 * 1024

_LOCK = threading.Lock()
# (site, exc type) -> {"n", "first_t", "last_t", "last_msg"}. The whole rate
# limiter: it is per PROCESS, so a fresh `pmt crypto stats` always prints its
# first failure, and the long-lived watch worker prints each distinct one once.
_SEEN: dict[tuple[str, str], dict] = {}


def path() -> Path:
    """Where marks are written. `PMT_ERRLOG_PATH` overrides — read on every
    call rather than at import, so a test can redirect it without reloading the
    module and without ever touching the operator's real `~/.pmt`."""
    override = os.environ.get("PMT_ERRLOG_PATH")
    return Path(override) if override else ENGINE_DIR / ERRLOG_NAME


def _stderr_enabled() -> bool:
    """`PMT_ERRLOG_STDERR=0` mutes the traceback but never the file. For a
    caller that owns the whole terminal (a Rich Live alternate screen) and
    would rather read the marks afterwards than have them painted over."""
    return os.environ.get("PMT_ERRLOG_STDERR", "1") not in ("0", "false", "no")


def _is_power_of_two(n: int) -> bool:
    return n > 0 and (n & (n - 1)) == 0


def _safe(v):
    """Context values must survive json.dumps. Anything that doesn't gets its
    repr, and anything whose repr explodes gets its type name — a mark with a
    degraded field beats no mark at all."""
    if v is None or isinstance(v, (bool, int, float, str)):
        return v
    try:
        return repr(v)[:200]
    except Exception:
        return f"<unreprable {type(v).__name__}>"


def _rotate(p: Path) -> None:
    try:
        if p.stat().st_size > MAX_BYTES:
            os.replace(p, p.with_suffix(p.suffix + ".1"))
    except OSError:
        pass


def _write(rec: dict) -> None:
    p = path()
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        _rotate(p)
        with open(p, "a") as fh:
            fh.write(json.dumps(rec, sort_keys=True) + "\n")
    except (OSError, ValueError, TypeError):
        # A read-only or full ~/.pmt loses the mark. It does NOT lose the
        # traceback: stderr already carried the first occurrence.
        pass


def note(site: str, exc: BaseException, **ctx) -> dict | None:
    """Record one swallowed exception. Returns the record written, or None when
    this occurrence was only counted.

    `site` is a stable dotted name for the handler — `module.function`, not the
    message — because it is half the rate-limit key and the whole grouping key
    in `pmt crypto errors`. `ctx` is whatever the handler knows that the
    traceback doesn't: the slug, the offset, the path it was reading.

    NEVER RAISES. See the module docstring.
    """
    try:
        return _note(site, exc, ctx)
    except BaseException:  # noqa: BLE001 - the recovery path may not have its own bug
        return None


def _note(site: str, exc: BaseException, ctx: dict) -> dict | None:
    key = (str(site), type(exc).__name__)
    now = time.time()
    with _LOCK:
        st = _SEEN.setdefault(key, {"n": 0, "first_t": now})
        st["n"] += 1
        st["last_t"] = now
        n = st["n"]
        first_t = st["first_t"]
        try:
            st["last_msg"] = str(exc)[:300]
        except Exception:
            st["last_msg"] = ""
    if n != 1 and not _is_power_of_two(n):
        return None

    try:
        msg = str(exc)[:300]
    except Exception:
        msg = ""
    rec = {
        "t": now, "site": key[0], "exc": key[1], "msg": msg,
        "n": n, "first_t": first_t,
        "kind": "first" if n == 1 else "repeat",
        "pid": os.getpid(),
    }
    if ctx:
        rec["ctx"] = {str(k): _safe(v) for k, v in ctx.items()}
    if n == 1:
        # The traceback is the whole point of the first one, and it is the ONLY
        # place the frames survive: by the time `pmt crypto errors` reads the
        # file the stack is long gone.
        tb = "".join(traceback.format_exception(type(exc), exc,
                                                exc.__traceback__)).strip()
        rec["traceback"] = tb[-4000:]
        if _stderr_enabled():
            try:
                print(f"\n[pmt] swallowed error at {key[0]} — surfaced, not fatal:\n{tb}\n",
                      file=sys.stderr, flush=True)
            except Exception:
                pass
    _write(rec)
    return rec


def counts() -> dict[tuple[str, str], dict]:
    """This process's live tally, newest state per (site, exc type). The file is
    the cross-process record; this is what the current run has seen."""
    with _LOCK:
        return {k: dict(v) for k, v in _SEEN.items()}


def reset() -> None:
    """Forget the rate-limit state. Tests only — a caller that resets this in
    production turns the limiter off."""
    with _LOCK:
        _SEEN.clear()


# ---------- reading the marks back ----------

def load(p: Path | str | None = None, since: float = 0.0) -> list[dict]:
    """Every mark on file at or after `since`, oldest first.

    Skips unparseable lines for the same reason tape.iter_records does: this
    file is appended to from several processes and a torn line must not take
    the reader down. A diagnostic log that can't be read during an incident is
    worth nothing.
    """
    target = path() if p is None else Path(p)
    out: list[dict] = []
    try:
        fh = open(target)
    except OSError:
        return out
    with fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                r = json.loads(line)
            except ValueError:
                continue
            if isinstance(r, dict) and float(r.get("t") or 0) >= since:
                out.append(r)
    return out


def aggregate(records: list[dict]) -> list[dict]:
    """Marks folded to one row per (site, exc type), worst first.

    `n` is the HIGH-WATER count, not the number of marks: the limiter writes
    log2 lines for a site that failed thousands of times, so summing rows would
    understate a storm by orders of magnitude. Sorted by that count and then by
    recency, because the two questions an operator has during an incident are
    "what is failing most" and "what broke just now".
    """
    by: dict[tuple[str, str], dict] = {}
    for r in records:
        key = (str(r.get("site") or "?"), str(r.get("exc") or "?"))
        row = by.setdefault(key, {"site": key[0], "exc": key[1], "n": 0,
                                  "first_t": None, "last_t": 0.0, "msg": "",
                                  "marks": 0, "traceback": None})
        row["marks"] += 1
        row["n"] = max(row["n"], int(r.get("n") or 1))
        t = float(r.get("t") or 0)
        ft = float(r.get("first_t") or t)
        row["first_t"] = ft if row["first_t"] is None else min(row["first_t"], ft)
        if t >= row["last_t"]:
            row["last_t"] = t
            row["msg"] = str(r.get("msg") or "")
            row["ctx"] = r.get("ctx")
        if r.get("traceback") and not row["traceback"]:
            row["traceback"] = r["traceback"]
    return sorted(by.values(), key=lambda x: (-x["n"], -x["last_t"]))
