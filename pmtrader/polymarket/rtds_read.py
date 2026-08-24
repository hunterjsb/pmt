"""Read side of the RTDS settlement stream — spot, per-minute marks and vol
for a symbol that has no Binance listing.

`rtds.py` writes the tape; this reads it back for the ONE-SHOT callers (the
`pmt crypto updown` pricer and the `pmt crypto arm` pre-flight), which need a
handful of numbers once and then exit. That is a different job from the
engine's resident router and from `pilot2.stream`'s live consumer, both of
which hold a socket open for hours.

Why this exists: `hype` (and anything else Polymarket lists off a Chainlink
feed before an exchange lists a USDT pair) has no `HYPEUSDT` on Binance, so
the venue-proxy path the arm CLI was built on 400s in pre-flight and the
window cannot be priced at all. The settlement stream itself has every number
those markets resolve on, and the recorder has been writing it down since
2026-08-23.

**The banking rules below are mirrors of `updown_rtds.rs::route_sample`, not
new inventions.** A pre-flight that derived vol a different way from the
engine that inherits the arm would hand it a floor computed off a series the
engine never sees. Specifically:

  - `closes[m]` — the FIRST chainlink spot print of each new minute. Not the
    last, not an average: that is the sample the router pushes onto its
    `closes` vec, and sigma is the estimator's floor.
  - `per_min[m]` — the settlement-width TWAP printed AT wall time `m+60`,
    accepted only within `MARK_TOL_S` of the boundary. The print at `m+60`
    averages the minute that just ended, which is what Binance's
    `(open+close)/2` for minute `m` was always proxying — and it makes
    `per_min[start-60]` literally the settlement print at the window's start
    instant, rather than a venue's guess at it.

Reads walk the daily files BACKWARD from the newest byte and stop as soon as
they have reached far enough back, because the corpus is ~240MB/day and a 5m
window's pre-flight needs the last ~7 minutes of it.
"""

from __future__ import annotations

import json
import os
import time
from collections.abc import Iterator
from pathlib import Path

from . import rtds
from .fit import realized_sigma

# How stale the freshest corpus print may be and still stand in for spot. The
# engine's own gate is 5s, but this is a pre-flight on a human-typed command:
# the number seeds a model the engine immediately re-derives from its live
# socket, so the bar is "the recorder is alive", not "tick-fresh". Past this
# we go ask the socket ourselves rather than quietly pricing off a dead tape.
MAX_SPOT_AGE_S = 90.0

# Re-exported so an error message can name the corpus and the socket without
# every caller reaching through to the recorder module.
RTDS_DIR = rtds.RTDS_DIR
RTDS_URL = rtds.RTDS_URL

# Mirror of updown_rtds.rs::MARK_TOL_S (and fixtures.RTDS_MARK_TOL_S).
MARK_TOL_S = 2

# Mirror of updown_rtds.rs::CLOSES_CAP — the depth of the deepest estimator,
# and the same lookback crypto._sigma_1m asks Binance for.
CLOSES_CAP = 120

_CHUNK = 1 << 20        # 1MiB reverse-read step
_TAIL_BUDGET = 1 << 26  # 64MiB ceiling per read, ~6h of corpus


# ---------- reverse file reads ----------

def corpus_files(directory: Path = rtds.RTDS_DIR) -> list[Path]:
    """Daily recorder files, oldest first. Names sort chronologically (UTC)."""
    d = Path(directory)
    return sorted(d.glob("rtds-*.jsonl")) if d.is_dir() else []


def reverse_lines(path: Path, budget_bytes: int = _TAIL_BUDGET) -> Iterator[bytes]:
    """Complete lines from the end of `path`, newest first.

    A partial line left dangling at the budget edge is dropped rather than
    yielded half-parsed — the caller is reading a tail, not the whole file,
    and a truncated JSON object is not a record.
    """
    with open(path, "rb") as fh:
        fh.seek(0, os.SEEK_END)
        pos = fh.tell()
        carry = b""
        read = 0
        while pos > 0 and read < budget_bytes:
            step = min(_CHUNK, pos)
            pos -= step
            read += step
            fh.seek(pos)
            parts = (fh.read(step) + carry).split(b"\n")
            carry = parts[0]
            for line in reversed(parts[1:]):
                if line:
                    yield line
        if pos == 0 and carry:
            yield carry


def read_back(symbol: str, since_ts: float, *, directory: Path = rtds.RTDS_DIR,
              budget_bytes: int = _TAIL_BUDGET) -> list[dict]:
    """Every record for `symbol` with observation ts >= `since_ts`, oldest first.

    `since_ts` is on the CHAINLINK clock (record `ts`, ms), never our receive
    clock — every boundary these markets settle on is defined there.

    The symbol is matched as a raw substring before any JSON is parsed: the
    stream carries eight symbols across three topics, so this skips ~7/8 of
    the bytes without constructing a dict for them.
    """
    needle = f'"{symbol}"'.encode()
    out: list[dict] = []
    for path in reversed(corpus_files(directory)):
        done = False
        for line in reverse_lines(path, budget_bytes):
            if needle not in line:
                continue
            try:
                rec = json.loads(line)
            except ValueError:
                continue
            if rec.get("symbol") != symbol:
                continue
            ts = rec.get("ts")
            if not isinstance(ts, (int, float)):
                continue
            if ts / 1000.0 < since_ts:
                # Records are appended in receive order, so once this
                # symbol has taken us past the horizon there is nothing
                # older worth reading in any earlier file either.
                done = True
                break
            out.append(rec)
        if done:
            break
    out.sort(key=lambda r: (r["ts"], r.get("t_recv", 0.0)))
    return out


# ---------- record → price ----------

def price_of(rec: dict) -> float | None:
    """Exact E18 price, falling back to Polymarket's lossy display float.

    Same precedence as `pilot2.stream.ingest` and the Rust router: the
    `full_accuracy_value` string is the reason the corpus stores strings.
    """
    px = rtds.e18_decimal(rec.get("full_accuracy_value"))
    if px is None:
        v = rec.get("value")
        if not isinstance(v, (int, float)) or isinstance(v, bool):
            return None
        px = v
    px = float(px)
    return px if px > 0.0 else None


# ---------- the three numbers a pre-flight needs ----------

def corpus_spot(symbol: str, *, now: float | None = None,
                max_age_s: float = MAX_SPOT_AGE_S,
                directory: Path = rtds.RTDS_DIR) -> tuple[float, float] | None:
    """(price, observation ts) of the freshest chainlink spot print, or None.

    None means "the recorder is not keeping this symbol current", which is a
    different fact from "the symbol does not exist" — the caller says which.
    """
    now = time.time() if now is None else now
    # A minute of tail is thousands of prints; ask for a few so a brief
    # recorder gap still answers rather than falling through to the socket.
    rows = read_back(symbol, now - max_age_s, directory=directory,
                     budget_bytes=8 << 20)
    for rec in reversed(rows):
        if rec.get("topic") != rtds.TOPIC_SPOT:
            continue
        px = price_of(rec)
        if px is not None:
            return px, rec["ts"] / 1000.0
    return None


def minute_closes(symbol: str, since_ts: float, *,
                  directory: Path = rtds.RTDS_DIR,
                  rows: list[dict] | None = None) -> list[tuple[int, float]]:
    """(minute, price) closes, oldest first — the router's own `closes` vec.

    The FIRST spot print of each new minute, exactly as
    `updown_rtds.rs::route_sample` banks it.
    """
    rows = read_back(symbol, since_ts, directory=directory) if rows is None else rows
    out: list[tuple[int, float]] = []
    last_min = -1
    for rec in rows:
        if rec.get("topic") != rtds.TOPIC_SPOT:
            continue
        minute = int(rec["ts"]) // 1000 // 60 * 60
        if minute <= last_min:
            continue
        px = price_of(rec)
        if px is None:
            continue
        last_min = minute
        out.append((minute, px))
    return out


def twap_marks(symbol: str, since_ts: float, *, window_s: int = 60,
               directory: Path = rtds.RTDS_DIR,
               rows: list[dict] | None = None) -> dict[float, float]:
    """`{minute_open: settlement TWAP}` — the model's `per_min`, keyed the way
    `crypto._model_twap` looks it up (so `per_min[start - 60]` is the
    range-start reference).

    Only prints within MARK_TOL_S of a minute boundary are marks; the other
    ~57 prints a minute are read by nobody, here or in the engine.
    """
    topic = rtds.TOPIC_TWAP30 if window_s == 30 else rtds.TOPIC_TWAP60
    rows = read_back(symbol, since_ts, directory=directory) if rows is None else rows
    out: dict[float, float] = {}
    for rec in rows:
        if rec.get("topic") != topic:
            continue
        ts = int(rec["ts"]) // 1000
        mark = ts // 60 * 60
        if ts - mark > MARK_TOL_S:
            continue
        px = price_of(rec)
        if px is None:
            continue
        # The print AT `mark` averages the minute that just ended.
        out.setdefault(float(mark - 60), px)
    return out


def corpus_sigma(symbol: str, *, now: float | None = None,
                 lookback_min: int = CLOSES_CAP,
                 directory: Path = rtds.RTDS_DIR,
                 closes: list[tuple[int, float]] | None = None) -> tuple[float, int] | None:
    """(sigma per minute, n closes) off the stream's own per-minute closes.

    The same estimator `crypto._sigma_1m` runs on Binance klines, fed the
    series the market actually settles on. None when the corpus holds too
    little history to estimate anything (`realized_sigma` needs 3 closes);
    a caller with no history must be told to pass `--sigma-bp`, not handed
    a zero that reads as "no vol".

    Gaps in the stream are left as gaps rather than interpolated: a missing
    minute makes one log return span two minutes, which biases sigma UP.
    That is the safe direction for a floor.
    """
    now = time.time() if now is None else now
    if closes is None:
        closes = minute_closes(symbol, now - (lookback_min + 2) * 60, directory=directory)
    px = [p for _, p in closes]
    if len(px) < 3:
        return None
    return realized_sigma(px, min(lookback_min, len(px) - 1)), len(px)


# ---------- last resort: ask the socket ourselves ----------

def live_spot(symbol: str, *, timeout_s: float = 8.0, url: str = rtds.RTDS_URL,
              topic: str = rtds.TOPIC_SPOT) -> tuple[float, float] | None:
    """One-shot socket read: connect, subscribe, take the first print for
    `symbol`, hang up.

    For when the recorder is down. Uses `rtds.subscribe_message` and
    `rtds.normalize` rather than its own frames, so the protocol lessons that
    module paid for (one entry per TOPIC, never per symbol) hold here too.
    The feed is free and unauthenticated, so a few seconds as a fourth
    subscriber costs nobody anything.
    """
    from websockets.sync.client import connect

    deadline = time.time() + timeout_s
    try:
        with connect(url, open_timeout=min(timeout_s, 10), close_timeout=2) as ws:
            ws.send(rtds.subscribe_message((topic,)))
            last_ping = time.time()
            while True:
                now = time.time()
                if now >= deadline:
                    return None
                if now - last_ping >= 4.0:
                    ws.send("PING")  # not optional: the flow dies without it
                    last_ping = now
                try:
                    raw = ws.recv(timeout=min(1.0, max(deadline - now, 0.05)))
                except TimeoutError:
                    continue
                if isinstance(raw, bytes):
                    raw = raw.decode("utf8", "replace")
                text = raw.strip()
                if not text or text.upper() == "PONG":
                    continue
                try:
                    msg = json.loads(text)
                except ValueError:
                    continue
                rec = rtds.normalize(msg, time.time())
                if rec is None or rec["symbol"] != symbol or rec["topic"] != topic:
                    continue
                px = price_of(rec)
                if px is not None:
                    return px, rec["ts"] / 1000.0
    except Exception:  # noqa: BLE001 — any failure means "no live spot", and
        return None    # the caller's error names the whole fallback chain
