"""Record the Polymarket public print tape for every window the engine trades.

WHY THIS EXISTS. The print corpus at `~/.pmt/corpus/prints.jsonl` was written
by `analysis/firsthalf_harvest_prints.py` (now in the private vault) — a
one-shot BACKFILL over whatever windows the book tape happened to hold when it
ran. It walked that list, exhausted it, and exited 0. Nothing killed it; it was
never continuous, and its coverage simply stops at the newest window that
existed when it ran:

    legacy prints.jsonl   167,941 prints   2026-08-22T02:59:36Z -> 08-23T07:39:00Z
    rtds corpus                            2026-08-23T08:28:55Z -> forward, forever

Fifty minutes of dead space between them and NOT ONE overlapping instant. That
makes print-vs-stream lead — does Polymarket print flow move before or after
the Chainlink stream the market settles on — unmeasurable. Not "noisy":
unmeasurable, because no moment is covered by both files. This module closes
the gap by running the print harvest CONTINUOUSLY alongside the RTDS recorder,
over the same windows, so overlap accrues for as long as both are up.

WHAT IT POLLS. `data-api /trades` serves the full public print history per
market long after a window resolves, so this does NOT need to poll during the
window. It waits `--settle-lag` past a window's close and harvests it once, in
full. Print timestamps come from the exchange, not from us, so a post-close
harvest preserves the lead signal exactly while costing a fraction of the
request budget a live poller would — and the account-wide data-api budget is
already the scarce resource (see analysis/watch_load.md: the engine's own
reconcile used to eat 8.7 req/s of it).

WHERE THE WINDOW LIST COMES FROM. The engine's own book tape,
`~/.pmt/engine/book-tape.jsonl` — the same source the original backfill used,
so the corpora stay comparable. Tailing it rather than reading it once is the
entire behavioural difference. It also means this recorder harvests exactly the
windows the engine actually watched, and cannot run away scanning all of
Polymarket.

ROTATION AND BOUNDS. `prints-YYYYMMDD.jsonl`, UTC-daily, same shape as the RTDS
corpus — but rotated by PRINT time, not harvest time, so "the prints for day X"
is one file and joins to `rtds-X.jsonl` without a filter. Unlike the RTDS
corpus this one is also BOUNDED: `--retention-days` (default 30) prunes old
daily files, because a print corpus grows with market volume rather than with
wall clock and the backfill alone was already 54 MB.

Run: `python -m polymarket.prints` (see deploy/systemd/pmt-print-recorder.service)
"""

from __future__ import annotations

import argparse
import errno
import json
import os
import signal
import sys
import threading
import time
from pathlib import Path
from typing import Iterable

import requests

from .hosts import DATA, GAMMA, UA
from .tape import BOOK_TAPE, iter_records
from .updown_slugs import parse_updown_slug

PRINTS_DIR = Path.home() / ".pmt" / "corpus" / "prints"
PIDFILE = PRINTS_DIR / "recorder.pid"
LOGFILE = PRINTS_DIR / "recorder.log"

# The pre-split one-shot backfill. Read at startup to seed the "already have
# it" set so we never re-fetch the 167k prints it already recorded, and never
# write a second copy of them into the daily corpus.
LEGACY_PRINTS = Path.home() / ".pmt" / "corpus" / "prints.jsonl"

TRADES_URL = f"{DATA}/trades"
EVENTS_URL = f"{GAMMA}/events"

PAGE = 500
MAX_OFFSET = 20_000  # pagination backstop, same bound the backfill used


def daily_path(t: float, directory: Path = PRINTS_DIR) -> Path:
    """`prints-YYYYMMDD.jsonl` for the **UTC** day containing epoch `t`."""
    return Path(directory) / f"prints-{time.strftime('%Y%m%d', time.gmtime(t))}.jsonl"


# ---------- the harvest ----------

def condition_id(slug: str, *, timeout: float = 30.0) -> str | None:
    """conditionId for a window slug, via gamma.

    Needed because data-api /trades SILENTLY IGNORES a `slug` filter — it
    answers with the global cross-market feed and a 200, so a slug-keyed
    harvest looks like it worked and is entirely wrong. Only `market=<cid>`
    actually filters; every row is re-checked against it in fetch_window.
    """
    r = requests.get(EVENTS_URL, params={"slug": slug}, headers=UA, timeout=timeout)
    if r.status_code != 200:
        return None
    evs = r.json()
    if not evs or not evs[0].get("markets"):
        return None
    return evs[0]["markets"][0].get("conditionId")


def fetch_window(slug: str, cid: str, *, pause: float = 0.3,
                 timeout: float = 40.0) -> list[dict]:
    """Every public print for one window, paginated to exhaustion."""
    out: list[dict] = []
    seen: set[tuple] = set()
    offset = 0
    while True:
        r = requests.get(
            TRADES_URL,
            params={"market": cid, "limit": PAGE, "offset": offset},
            headers=UA,
            timeout=timeout,
        )
        if r.status_code != 200:
            log(f"  ! {slug} offset={offset} HTTP {r.status_code}")
            break
        page = r.json()
        if not page:
            break
        fresh = 0
        for t in page:
            if t.get("conditionId") != cid:  # the API ignores filters it dislikes
                continue
            key = (t.get("transactionHash"), t.get("asset"),
                   t.get("timestamp"), t.get("size"))
            if key in seen:
                continue
            seen.add(key)
            out.append(t)
            fresh += 1
        offset += PAGE
        if len(page) < PAGE or fresh == 0 or offset > MAX_OFFSET:
            break
        time.sleep(pause)
    return out


def slim(t: dict, slug: str) -> dict:
    """Only the fields any study needs — the profile blob is 80% of the payload.

    Field-for-field identical to the backfill's own `slim()`, so the daily
    corpus and the legacy `prints.jsonl` concatenate without a shim.
    """
    return {
        "slug": slug,
        "t": t.get("timestamp"),
        "asset": t.get("asset"),
        "outcome": (t.get("outcome") or "").lower(),
        "side": (t.get("side") or "").lower(),
        "size": t.get("size"),
        "price": t.get("price"),
        "wallet": t.get("proxyWallet"),
        "tx": t.get("transactionHash"),
    }


# ---------- window discovery ----------

def book_windows(path: str = BOOK_TAPE, floor: float | None = None) -> dict[str, dict]:
    """{slug: parsed-window} for every updown window in the engine's book tape.

    `floor` skips records older than that epoch — the recorder passes the
    oldest window it could still care about so a 50 MB tape is not re-parsed
    in full on every scan.
    """
    out: dict[str, dict] = {}
    for rec in iter_records(path, floor=floor):
        slug = rec.get("slug")
        if not slug or slug in out:
            continue
        w = parse_updown_slug(slug)
        if w is not None:
            out[slug] = w
    return out


def due_windows(windows: dict[str, dict], done: set[str], now: float,
                settle_lag: float) -> list[str]:
    """Slugs whose window closed at least `settle_lag` ago and aren't harvested.

    Oldest first, so an interrupted catch-up resumes in time order and the
    corpus stays roughly append-ordered.
    """
    due = [s for s, w in windows.items()
           if s not in done and now - w["end"] >= settle_lag]
    return sorted(due, key=lambda s: windows[s]["start"])


def harvested_slugs(directory: Path = PRINTS_DIR,
                    legacy: Path | None = LEGACY_PRINTS) -> set[str]:
    """Every slug already present in the corpus — daily files plus the backfill.

    Derived from the data rather than from a side-car index on purpose: an
    index can drift from the files it describes, and this is a startup-only
    cost. The backfill is 54 MB and takes ~1s to scan.
    """
    done: set[str] = set()
    paths: list[Path] = sorted(Path(directory).glob("prints-*.jsonl"))
    if legacy is not None and Path(legacy).exists():
        paths.append(Path(legacy))
    for p in paths:
        try:
            with p.open() as f:
                for line in f:
                    try:
                        done.add(json.loads(line)["slug"])
                    except (ValueError, KeyError):
                        continue
        except OSError as e:
            log(f"  ! reading {p.name}: {e}")
    return done


def prune(directory: Path = PRINTS_DIR, retention_days: int = 30,
          now: float | None = None) -> list[str]:
    """Delete daily files whose UTC day is older than `retention_days`.

    Bounds a corpus that otherwise grows with market volume. Returns the names
    removed. `retention_days <= 0` disables pruning entirely.
    """
    if retention_days <= 0:
        return []
    now = time.time() if now is None else now
    cutoff = time.strftime("%Y%m%d", time.gmtime(now - retention_days * 86400))
    removed = []
    for p in sorted(Path(directory).glob("prints-*.jsonl")):
        day = p.stem.split("-", 1)[1]
        if len(day) == 8 and day.isdigit() and day < cutoff:
            try:
                p.unlink()
                removed.append(p.name)
            except OSError as e:
                log(f"  ! pruning {p.name}: {e}")
    return removed


# ---------- writer ----------

class DailyWriter:
    """Append-only JSONL rotated by the UTC day of each PRINT's own timestamp.

    Rotating on print time rather than harvest time is what makes a day's
    prints one file that lines up with `rtds-YYYYMMDD.jsonl`. A window that
    straddles midnight reopens once mid-write; records arrive in time order so
    it cannot thrash.
    """

    def __init__(self, directory: Path = PRINTS_DIR):
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)
        self._path: Path | None = None
        self._fh = None

    def _handle(self, t: float):
        path = daily_path(t, self.directory)
        if path != self._path:
            if self._fh is not None:
                self._fh.close()
            self._fh = open(path, "a", buffering=1)
            self._path = path
        return self._fh

    @property
    def path(self) -> Path | None:
        return self._path

    def write(self, rec: dict) -> None:
        try:
            t = float(rec.get("t") or 0.0)
        except (TypeError, ValueError):
            t = 0.0
        # A print with no usable timestamp is useless for a lead study but is
        # still evidence; file it under today rather than dropping it.
        self._handle(t or time.time()).write(json.dumps(rec, separators=(",", ":")) + "\n")

    def close(self) -> None:
        if self._fh is not None:
            try:
                self._fh.flush()
                os.fsync(self._fh.fileno())
            except OSError:
                pass
            self._fh.close()
            self._fh = None
            self._path = None


# ---------- pidfile ----------

def pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except OSError as e:
        return e.errno == errno.EPERM
    return True


def read_pidfile(path: Path = PIDFILE) -> int | None:
    try:
        return int(Path(path).read_text().strip())
    except (OSError, ValueError):
        return None


def claim_pidfile(path: Path = PIDFILE) -> bool:
    """Take the pidfile, or return False if a live recorder already holds it.

    Stale pidfiles are taken over rather than treated as fatal — the nightly
    poweroff guarantees them, and a recorder that refuses to start after every
    reboot is worse than useless. Same policy as the RTDS recorder.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    pid = read_pidfile(path)
    if pid is not None and pid != os.getpid() and pid_alive(pid):
        try:
            cmdline = Path(f"/proc/{pid}/cmdline").read_bytes().decode("utf8", "replace")
        except OSError:
            cmdline = ""
        if not cmdline or "prints" in cmdline:
            return False
    path.write_text(f"{os.getpid()}\n")
    return True


def release_pidfile(path: Path = PIDFILE) -> None:
    if read_pidfile(path) == os.getpid():
        try:
            Path(path).unlink()
        except OSError:
            pass


# ---------- logging ----------

def _stamp(t: float | None = None) -> str:
    t = time.time() if t is None else t
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(t))


def log(msg: str) -> None:
    print(f"{_stamp()} {msg}", flush=True)


# ---------- the recorder ----------

def run(directory: Path = PRINTS_DIR, *, book_tape: str = BOOK_TAPE,
        scan_every_s: float = 60.0, settle_lag_s: float = 180.0,
        pause_s: float = 0.35, max_per_scan: int = 40,
        retention_days: int = 30, lookback_s: float = 86400.0,
        duration: float | None = None, once: bool = False,
        stop: threading.Event | None = None) -> int:
    """Harvest forever. Returns an exit code (0 on clean stop).

    One pass per `scan_every_s`: re-read the recent tail of the book tape, take
    every window that closed at least `settle_lag_s` ago and has no prints yet,
    and fetch each in full. `max_per_scan` caps a catch-up burst so a recorder
    that has been down for a day does not open the throttle on data-api all at
    once — the leftovers are simply picked up by the next pass.
    """
    stop = stop or threading.Event()
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)

    writer = DailyWriter(directory)
    started = time.time()
    log(f"print recorder starting — dir={directory} book_tape={book_tape} "
        f"scan={scan_every_s}s settle_lag={settle_lag_s}s retention={retention_days}d")

    done = harvested_slugs(directory)
    log(f"seeded {len(done)} already-harvested windows (daily corpus + legacy backfill)")

    windows_total = 0
    prints_total = 0
    last_prune_day = ""

    try:
        while not stop.is_set():
            now = time.time()
            day = time.strftime("%Y%m%d", time.gmtime(now))
            if day != last_prune_day:
                removed = prune(directory, retention_days, now)
                if removed:
                    log(f"pruned {len(removed)} file(s) past {retention_days}d: "
                        f"{', '.join(removed)}")
                last_prune_day = day

            try:
                windows = book_windows(book_tape, floor=now - lookback_s)
            except OSError as e:
                log(f"  ! book tape unreadable ({e}); retrying next scan")
                windows = {}

            due = due_windows(windows, done, now, settle_lag_s)
            if due:
                log(f"{len(due)} window(s) due; taking up to {max_per_scan}")

            for slug in due[:max_per_scan]:
                if stop.is_set():
                    break
                try:
                    cid = condition_id(slug)
                    if not cid:
                        # No conditionId today can mean gamma is slow, not that
                        # the window has none — leave it undone and retry.
                        log(f"  ! {slug}: no conditionId (will retry)")
                        continue
                    trades = fetch_window(slug, cid, pause=pause_s)
                except requests.RequestException as e:
                    log(f"  ! {slug}: {e} (will retry)")
                    continue

                for t in trades:
                    writer.write(slim(t, slug))
                # Mark done even at zero prints: a settled window with no
                # trades is a real, permanent answer, and retrying it forever
                # would be a slow leak against the request budget.
                done.add(slug)
                windows_total += 1
                prints_total += len(trades)
                log(f"  {slug:<32} {len(trades):>5} prints -> "
                    f"{writer.path.name if writer.path else '?'}")
                time.sleep(pause_s)

            # Checked AFTER the sweep, so --once means one pass and not zero.
            if once or (duration is not None
                        and time.time() - started >= duration):
                break
            stop.wait(scan_every_s)
    finally:
        writer.close()
        log(f"stopped — {windows_total} windows, {prints_total} prints this run")
    return 0


# ---------- entrypoint ----------

def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="python -m polymarket.prints",
        description="Continuously record the Polymarket print tape for the engine's "
                    "windows into ~/.pmt/corpus/prints/.",
    )
    ap.add_argument("--dir", default=str(PRINTS_DIR), help="corpus directory")
    ap.add_argument("--book-tape", default=BOOK_TAPE,
                    help="engine book tape to take the window list from")
    ap.add_argument("--scan-every", type=float, default=60.0,
                    help="seconds between passes (default 60)")
    ap.add_argument("--settle-lag", type=float, default=180.0,
                    help="seconds to wait past a window's close before harvesting "
                         "it (default 180)")
    ap.add_argument("--max-per-scan", type=int, default=40,
                    help="cap on windows harvested in one pass (default 40)")
    ap.add_argument("--retention-days", type=int, default=30,
                    help="prune daily files older than this; 0 disables (default 30)")
    ap.add_argument("--lookback", type=float, default=86400.0,
                    help="how far back in the book tape to consider (default 24h)")
    ap.add_argument("--duration", type=float, default=None,
                    help="stop after N seconds (testing; default: forever)")
    ap.add_argument("--stdout", action="store_true",
                    help="log to stdout instead of recorder.log")
    ap.add_argument("--no-pidfile", action="store_true",
                    help="skip the single-instance pidfile (testing)")
    ap.add_argument("--once", action="store_true",
                    help="run a single pass and exit (backfill/catch-up)")
    args = ap.parse_args(argv)

    directory = Path(args.dir).expanduser()
    directory.mkdir(parents=True, exist_ok=True)

    if not args.stdout:
        stream = open(directory / LOGFILE.name, "a", buffering=1)
        sys.stdout = sys.stderr = stream

    if not args.no_pidfile:
        pidfile = directory / PIDFILE.name
        if not claim_pidfile(pidfile):
            log(f"refusing to start: recorder already running (pid {read_pidfile(pidfile)})")
            return 1
    else:
        pidfile = None

    stop = threading.Event()

    def _handle(signum, _frame):
        log(f"signal {signal.Signals(signum).name} — closing")
        stop.set()

    signal.signal(signal.SIGTERM, _handle)
    signal.signal(signal.SIGINT, _handle)

    try:
        return run(
            directory,
            book_tape=args.book_tape,
            scan_every_s=args.scan_every,
            settle_lag_s=args.settle_lag,
            max_per_scan=args.max_per_scan,
            retention_days=args.retention_days,
            lookback_s=args.lookback,
            duration=args.duration,
            once=args.once,
            stop=stop,
        )
    finally:
        if pidfile is not None:
            release_pidfile(pidfile)


if __name__ == "__main__":
    raise SystemExit(main())
