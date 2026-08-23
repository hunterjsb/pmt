"""RTDS recorder — the Chainlink settlement stream, written down forever.

Polymarket's real-time data socket (`wss://ws-live-data.polymarket.com/`) relays
the object the crypto up/down markets actually settle against: Chainlink's 60s
TWAP, at 1 Hz, free and unauthenticated. See `analysis/chainlink_stream_scout.md`.

The one thing that matters about this feed: **it has no history.** No snapshot,
no replay, no backfill after a disconnect, and no on-chain shadow to reconstruct
from. A 4h window's opening reference exists only if something was connected
four hours earlier. Every minute nobody is recording is calibration data that is
permanently gone. That is the entire reason this is a resident daemon with a
pidfile and forever-retry, rather than a script someone remembers to run.

Three protocol facts, all learned the hard way:

- **The `PING` keepalive is not optional.** A plain-text `PING` frame every ~5 s;
  without it the socket stays *open* while the flow silently stops (~228 s in the
  scout capture). We send every 4 s and also force a reconnect after
  `stall_s` of silence, because "open but dead" is the failure mode this feed has.
- **Subscribe per topic, not per symbol.** One connection carries all three
  topics if you send one entry per topic with `filters` omitted (all 8 symbols
  come free). Sending 24 per-symbol entries made the server quietly downgrade the
  whole subscription to `crypto_prices` snapshot payloads — wrong data, no error.
- **`full_accuracy_value` is an E18 fixed-point string** (Data Streams scale, not
  the 8-decimal Polygon aggregator). It is stored verbatim as a string and never
  round-tripped through a float; `value` is Polymarket's lossy display float.

Records are append-only JSONL under `~/.pmt/corpus/rtds/`, one file per **UTC**
day. UTC because a corpus indexed by local time has a duplicated hour every
autumn, and every timestamp we compare it against is already UTC.

Gaps are corpus facts and are written down as records, not just logged: a
consumer reading the tape must be able to tell "the price did not move" from
"we were not listening".

Run it:  `uv run python -m polymarket.rtds`
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
from collections import Counter
from collections.abc import Iterable
from decimal import Context, Decimal
from pathlib import Path

RTDS_URL = "wss://ws-live-data.polymarket.com/"

# The three Chainlink-sourced topics. `crypto_prices` (Binance spot) is
# deliberately excluded: pmengine already consumes Binance directly at lower
# latency, and it would double the tape for data we are not short of.
TOPIC_TWAP60 = "crypto_prices_twap_sixty"
TOPIC_TWAP30 = "crypto_prices_twap_thirty"
TOPIC_SPOT = "crypto_prices_chainlink"
TOPICS: tuple[str, ...] = (TOPIC_TWAP60, TOPIC_TWAP30, TOPIC_SPOT)

# What a no-filter subscription actually delivers (verified live 2026-08-23):
# the six armed/candidate updown symbols plus hype and zec, which cost nothing
# extra and are the next symbols anyone would ask about.
SYMBOLS: tuple[str, ...] = (
    "btc/usd", "eth/usd", "sol/usd", "xrp/usd",
    "doge/usd", "bnb/usd", "hype/usd", "zec/usd",
)

# window_s is absent on the spot topic; for the TWAP topics the payload carries
# it, but the topic name is the authority when a capture predates the field.
TOPIC_WINDOW_S: dict[str, int | None] = {
    TOPIC_TWAP60: 60,
    TOPIC_TWAP30: 30,
    TOPIC_SPOT: None,
}

RTDS_DIR = Path.home() / ".pmt" / "corpus" / "rtds"
PIDFILE = RTDS_DIR / "recorder.pid"
LOGFILE = RTDS_DIR / "recorder.log"
LEGACY_MARKER = RTDS_DIR / ".legacy-converted.json"

EV_GAP = "gap"
EV_START = "start"
EV_STOP = "stop"

E18 = 10 ** 18
_E18_CTX = Context(prec=60)  # wide enough that scaleb never rounds a real price


# ---------- pure: value parsing ----------

def parse_e18(value: object) -> int | None:
    """Exact integer behind an E18 fixed-point string, or None if unparseable.

    Deliberately string-only arithmetic. `full_accuracy_value` exists precisely
    because `value` already lost bits to float64; parsing it via float would
    throw away the thing we recorded it for.
    """
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    s = str(value).strip()
    if not s:
        return None
    sign = 1
    if s[0] in "+-":
        sign = -1 if s[0] == "-" else 1
        s = s[1:]
    if not s.isdigit():
        return None
    return sign * int(s)


def e18_decimal(value: object) -> Decimal | None:
    """E18 fixed-point string → exact Decimal price (no float anywhere)."""
    n = parse_e18(value)
    if n is None:
        return None
    return _E18_CTX.scaleb(Decimal(n), -18)


# ---------- pure: record shapes ----------

def normalize(msg: object, t_recv: float) -> dict | None:
    """RTDS envelope → corpus record, or None if it isn't a price update.

    Envelope shape:
      {"connection_id": ..., "topic": ..., "type": "update", "timestamp": <emit ms>,
       "payload": {"symbol", "timestamp" <observation ms>, "value", "full_accuracy_value", "window_s"}}

    We keep the payload's observation timestamp as `ts` (that is the Chainlink
    clock the settlement compares on); the envelope's emit time is dropped —
    `t_recv` already bounds the relay lag from our side, and more honestly, since
    it includes our own scheduling.
    """
    if not isinstance(msg, dict):
        return None
    topic = msg.get("topic")
    if topic not in TOPIC_WINDOW_S:
        return None
    payload = msg.get("payload")
    if not isinstance(payload, dict):
        return None
    symbol = payload.get("symbol")
    ts = payload.get("timestamp")
    if not isinstance(symbol, str) or not symbol or ts is None:
        return None
    try:
        ts_i = int(ts)
    except (TypeError, ValueError):
        return None
    fav = payload.get("full_accuracy_value")
    value = payload.get("value")
    if fav is None and value is None:
        return None
    window_s = payload.get("window_s")
    if window_s is None:
        window_s = TOPIC_WINDOW_S[topic]
    else:
        try:
            window_s = int(window_s)
        except (TypeError, ValueError):
            window_s = TOPIC_WINDOW_S[topic]
    return {
        "t_recv": round(float(t_recv), 3),
        "topic": topic,
        "symbol": symbol,
        "ts": ts_i,
        "value": value,
        "full_accuracy_value": None if fav is None else str(fav),
        "window_s": window_s,
    }


def gap_record(t_recv: float, down_s: float, *,
               t_last: float | None = None, reason: str = "") -> dict:
    """A hole in the tape, written into the tape.

    `down_s` spans last-message-received → first-message-after-reconnect, not
    socket-close → socket-open: a socket that is open but silent (this feed's
    signature failure) is just as much a hole.
    """
    rec: dict = {"t_recv": round(float(t_recv), 3), "ev": EV_GAP,
                 "down_s": round(float(down_s), 3)}
    if t_last is not None:
        rec["t_last"] = round(float(t_last), 3)
    if reason:
        rec["reason"] = reason
    return rec


def marker_record(t_recv: float, ev: str, **extra) -> dict:
    """Lifecycle marker (`start`/`stop`). Bounds coverage at the file edges:
    without it, a tape that ends because the box powered off is
    indistinguishable from one that ends because the price stopped moving."""
    return {"t_recv": round(float(t_recv), 3), "ev": ev, **extra}


def daily_path(t: float, directory: Path = RTDS_DIR) -> Path:
    """`rtds-YYYYMMDD.jsonl` for the **UTC** day containing epoch `t`."""
    return Path(directory) / f"rtds-{time.strftime('%Y%m%d', time.gmtime(t))}.jsonl"


# ---------- pure: legacy capture conversion ----------

def convert_legacy_record(d: object) -> dict | None:
    """One `.rtds4`/`.rtds5` raw-capture line → the recorder's record shape.

    Those two captures (the scout's, and the only recording of the settlement
    object that existed before this daemon) used a terser ad-hoc shape:
    `{topic, sym, ts, v, fav, w?, recv_ms}`. rtds4 predates `w` entirely, so the
    window is recovered from the topic name.
    """
    if not isinstance(d, dict):
        return None
    topic = d.get("topic")
    if topic not in TOPIC_WINDOW_S:
        return None
    symbol = d.get("sym") or d.get("symbol")
    ts = d.get("ts")
    if not isinstance(symbol, str) or not symbol or ts is None:
        return None
    try:
        ts_i = int(ts)
    except (TypeError, ValueError):
        return None
    fav = d.get("fav", d.get("full_accuracy_value"))
    value = d.get("v", d.get("value"))
    if fav is None and value is None:
        return None
    window_s = d.get("w", d.get("window_s"))
    if window_s is None:
        window_s = TOPIC_WINDOW_S[topic]
    recv_ms = d.get("recv_ms")
    t_recv = float(recv_ms) / 1000.0 if recv_ms is not None else float(ts_i) / 1000.0
    return {
        "t_recv": round(t_recv, 3),
        "topic": topic,
        "symbol": symbol,
        "ts": ts_i,
        "value": value,
        "full_accuracy_value": None if fav is None else str(fav),
        "window_s": window_s,
    }


def convert_legacy_file(src: Path, directory: Path = RTDS_DIR) -> dict:
    """Append a raw capture into the daily files.

    Returns `{"converted", "off_topic", "bad"}`. `off_topic` is counted apart
    from `bad` on purpose: the scout's captures also carry the Binance
    `crypto_prices` topic, which this recorder deliberately does not keep, and
    filing thousands of intentional skips as "bad" would make a real parse
    failure invisible in the same number.

    Records are routed by their own `t_recv`, so a capture that straddles UTC
    midnight lands in the right two files.
    """
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    handles: dict[Path, object] = {}
    counts = {"converted": 0, "off_topic": 0, "bad": 0}
    try:
        with open(src) as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    d = json.loads(line)
                except ValueError:
                    counts["bad"] += 1
                    continue
                if isinstance(d, dict) and d.get("topic") not in TOPIC_WINDOW_S:
                    counts["off_topic"] += 1
                    continue
                rec = convert_legacy_record(d)
                if rec is None:
                    counts["bad"] += 1
                    continue
                path = daily_path(rec["t_recv"], directory)
                out = handles.get(path)
                if out is None:
                    out = handles[path] = open(path, "a", buffering=1)
                out.write(json.dumps(rec) + "\n")
                counts["converted"] += 1
    finally:
        for out in handles.values():
            out.close()
    return counts


def convert_legacy(sources: Iterable[Path], directory: Path = RTDS_DIR,
                   marker: Path | None = None, force: bool = False) -> dict:
    """One-shot import of the salvaged captures, idempotent via a marker file.

    Idempotency matters more than it looks: these are append-only corpus files
    and a second run would silently double every print in them.
    """
    directory = Path(directory)
    marker = Path(marker) if marker is not None else directory / LEGACY_MARKER.name
    try:
        done = json.loads(marker.read_text())
    except (OSError, ValueError):
        done = {}
    if not isinstance(done, dict):
        done = {}
    result: dict = {}
    for src in sources:
        src = Path(src)
        key = src.name
        if not src.exists():
            result[key] = {"skipped": "missing"}
            continue
        if key in done and not force:
            result[key] = {"skipped": "already-converted", "at": done[key].get("at")}
            continue
        counts = convert_legacy_file(src, directory)
        done[key] = {**counts, "at": time.time(), "src": str(src)}
        result[key] = dict(counts)
    directory.mkdir(parents=True, exist_ok=True)
    marker.write_text(json.dumps(done, indent=2) + "\n")
    return result


# ---------- writer ----------

class DailyWriter:
    """Append-only JSONL with UTC-daily rotation.

    Line-buffered, never fsynced. A crash can cost the tail line; an fsync per
    record would cost an I/O stall on every one of ~24 messages/second, and a
    stalled recorder loses whole seconds rather than one line.
    """

    def __init__(self, directory: Path = RTDS_DIR):
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
        self._handle(rec.get("t_recv") or time.time()).write(json.dumps(rec) + "\n")

    def close(self) -> None:
        if self._fh is not None:
            try:
                self._fh.flush()
                os.fsync(self._fh.fileno())  # only on the way out — cost is once
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

    A stale pidfile (process gone, or pid recycled by something that is not this
    module) is taken over rather than treated as fatal — the nightly poweroff
    guarantees stale pidfiles, and a recorder that refuses to start after every
    reboot is worse than useless.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    pid = read_pidfile(path)
    if pid is not None and pid != os.getpid() and pid_alive(pid):
        try:
            cmdline = Path(f"/proc/{pid}/cmdline").read_bytes().decode("utf8", "replace")
        except OSError:
            cmdline = ""
        if not cmdline or "rtds" in cmdline:
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

def subscribe_message(topics: Iterable[str] = TOPICS) -> str:
    """One entry per topic, `filters` omitted so every symbol is included.

    Do not "improve" this into per-symbol entries — 24 of them made the server
    downgrade the subscription to `crypto_prices` snapshots with no error frame.
    """
    return json.dumps({
        "action": "subscribe",
        "subscriptions": [{"topic": t, "type": "update"} for t in topics],
    })


def run(directory: Path = RTDS_DIR, *, url: str = RTDS_URL,
        topics: Iterable[str] = TOPICS, ping_s: float = 4.0,
        heartbeat_s: float = 60.0, stall_s: float = 30.0,
        max_backoff: float = 30.0, duration: float | None = None,
        stop: threading.Event | None = None) -> int:
    """Record forever. Returns an exit code (0 on clean stop)."""
    from websockets.sync.client import connect

    topics = tuple(topics)
    stop = stop if stop is not None else threading.Event()
    writer = DailyWriter(directory)
    started = time.time()
    deadline = started + duration if duration else None

    totals: Counter[str] = Counter()
    window: Counter[str] = Counter()
    hb_at = started
    backoff = 1.0
    reconnects = 0
    t_last_msg: float | None = None
    pending_gap_reason = ""

    writer.write(marker_record(started, EV_START, pid=os.getpid(),
                               topics=list(topics), url=url))
    log(f"start pid={os.getpid()} dir={writer.directory} topics={','.join(topics)}")

    def heartbeat(now: float) -> None:
        nonlocal hb_at, window
        elapsed = now - hb_at
        if elapsed < 1.0:  # a final heartbeat right after a scheduled one would
            return         # divide by ~0 and print absurd rates

        rates = " ".join(f"{t.replace('crypto_prices', 'cp')}={window[t] / elapsed:.2f}/s"
                         for t in topics)
        log(f"hb up={now - started:.0f}s file={writer.path.name if writer.path else '-'} "
            f"msgs={sum(totals.values())} reconnects={reconnects} {rates} "
            f"skipped={window['_skipped']}")
        window = Counter()
        hb_at = now

    while not stop.is_set():
        if deadline and time.time() >= deadline:
            break
        try:
            with connect(url, open_timeout=15, close_timeout=5) as ws:
                ws.send(subscribe_message(topics))
                t_conn = time.time()
                log(f"connected {url} (subscribed {len(topics)} topics)")
                last_ping = t_conn
                while not stop.is_set():
                    now = time.time()
                    if deadline and now >= deadline:
                        break
                    if now - last_ping >= ping_s:
                        ws.send("PING")  # not optional: the flow dies without it
                        last_ping = now
                    if now - hb_at >= heartbeat_s:
                        heartbeat(now)
                    try:
                        raw = ws.recv(timeout=1.0)
                    except TimeoutError:
                        # Measure silence from the connect when nothing has ever
                        # arrived: a subscription the server quietly declines
                        # looks exactly like a healthy idle socket otherwise.
                        quiet_since = t_last_msg if t_last_msg is not None else t_conn
                        if now - quiet_since > stall_s:
                            pending_gap_reason = "stall"
                            log(f"stalled {now - quiet_since:.0f}s with the socket open — reconnecting")
                            break
                        continue
                    now = time.time()
                    if isinstance(raw, bytes):
                        raw = raw.decode("utf8", "replace")
                    text = raw.strip()
                    if not text or text.upper() == "PONG":
                        continue
                    try:
                        msg = json.loads(text)
                    except ValueError:
                        window["_skipped"] += 1
                        totals["_skipped"] += 1
                        continue
                    rec = normalize(msg, now)
                    if rec is None:
                        window["_skipped"] += 1
                        totals["_skipped"] += 1
                        continue
                    if t_last_msg is not None and pending_gap_reason:
                        down = now - t_last_msg
                        writer.write(gap_record(now, down, t_last=t_last_msg,
                                                reason=pending_gap_reason))
                        log(f"gap {down:.1f}s ({pending_gap_reason}) recorded")
                    pending_gap_reason = ""
                    t_last_msg = now
                    backoff = 1.0
                    writer.write(rec)
                    window[rec["topic"]] += 1
                    totals[rec["topic"]] += 1
        except Exception as e:  # noqa: BLE001 — any failure here means "reconnect"
            if stop.is_set():
                break
            log(f"disconnected: {type(e).__name__}: {e}")
            if not pending_gap_reason:
                pending_gap_reason = type(e).__name__
        else:
            # No exception: either we broke out on a stall (reason already set)
            # or the peer closed on us.
            if not stop.is_set() and not (deadline and time.time() >= deadline):
                if not pending_gap_reason:
                    pending_gap_reason = "closed"
                    log("socket closed cleanly by peer")
        if stop.is_set() or (deadline and time.time() >= deadline):
            break
        reconnects += 1
        log(f"reconnecting in {backoff:.0f}s (attempt {reconnects})")
        stop.wait(backoff)
        backoff = min(backoff * 2, max_backoff)

    now = time.time()
    heartbeat(now)
    writer.write(marker_record(now, EV_STOP, pid=os.getpid(),
                               up_s=round(now - started, 1),
                               msgs=sum(v for k, v in totals.items() if not k.startswith("_")),
                               reconnects=reconnects))
    writer.close()
    log(f"stopped up={now - started:.0f}s msgs={sum(totals.values())} reconnects={reconnects}")
    return 0


# ---------- entrypoint ----------

def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="python -m polymarket.rtds",
        description="Record Polymarket's RTDS Chainlink TWAP stream to ~/.pmt/corpus/rtds/.",
    )
    ap.add_argument("--dir", default=str(RTDS_DIR), help="corpus directory")
    ap.add_argument("--duration", type=float, default=None,
                    help="stop after N seconds (testing; default: forever)")
    ap.add_argument("--stdout", action="store_true",
                    help="log to stdout instead of recorder.log")
    ap.add_argument("--no-pidfile", action="store_true",
                    help="skip the single-instance pidfile (testing)")
    ap.add_argument("--convert-legacy", nargs="*", metavar="FILE", default=None,
                    help="one-shot: fold raw .rtds*.jsonl captures into the corpus, then exit "
                         "(default: .rtds4.jsonl .rtds5.jsonl in --dir)")
    ap.add_argument("--force", action="store_true",
                    help="with --convert-legacy: re-convert an already-imported capture")
    args = ap.parse_args(argv)

    directory = Path(args.dir).expanduser()
    directory.mkdir(parents=True, exist_ok=True)

    if not args.stdout:
        stream = open(directory / LOGFILE.name, "a", buffering=1)
        sys.stdout = sys.stderr = stream

    if args.convert_legacy is not None:
        srcs = [Path(p) for p in args.convert_legacy] or \
               [directory / ".rtds4.jsonl", directory / ".rtds5.jsonl"]
        result = convert_legacy(srcs, directory, force=args.force)
        for name, info in result.items():
            log(f"legacy {name}: {info}")
        return 0

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
        return run(directory, duration=args.duration, stop=stop)
    finally:
        if pidfile is not None:
            release_pidfile(pidfile)


if __name__ == "__main__":
    raise SystemExit(main())
