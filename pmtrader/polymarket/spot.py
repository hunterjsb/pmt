"""Spot recorder — the exchange tape the Chainlink oracle is a lagging function of.

`analysis/opponent_model.md` §1d decomposes the makers' ~3s lead over our
settlement feed into ~1.7s of our own relay plumbing and **~1.3s of genuine
information advantage**: the makers price the underlying spot market, and
Chainlink follows it. No relay upgrade closes that second half. The only thing
that does is reading the same underlying they read.

This records it. `klines-1m-*.jsonl` cannot: a 1-minute bar has no opinion about
a 3-second lead. That needs ticks, live, with a clock you trust.

**Both clocks on every row, or the row is refused.** The entire §1 finding
turned on a clock choice — `book_lead.md` reached the opposite sign because it
compared a stream keyed on the exchange's stamp against a book keyed on our wall
clock, a ~1.7s lookahead larger than the effect. A tape that cannot separate
"the price moved" from "we heard about it late" can only reproduce that mistake,
so `t_exch` is mandatory and a message that lacks one is counted and dropped
rather than written with a null.

Venue and stream choices, all settled by live probe rather than documentation
(the probes are reproduced in `analysis/spot_lead.md`):

- **Binance via `data-stream.binance.vision`.** `stream.binance.com` answers
  **HTTP 451** from this box. `binance.vision` is Binance's market-data-only
  mirror of the *global* book and is not geo-blocked — it quoted BTC 77887 in the
  same second `stream.binance.us` quoted 77951, which is the point: Binance.US is
  a separate, thinner market and is *not* what the oracle follows.
- **`@trade`, not `@bookTicker`.** Verified live: a `@bookTicker` frame is
  `{u,s,b,B,a,A}` — it carries **no exchange timestamp of any kind**, so it
  cannot satisfy the both-clocks rule above. `@trade` carries `E` (event) and `T`
  (transaction). The stamped top-of-book alternative, `@depth@100ms`, is a
  *diff* stream needing a REST snapshot and sequence-gap resync inside the
  reader loop — the blocking-call-in-a-stream-loop shape that
  `analysis/watch_load.md` is about, and the reason the print recorder is a
  separate unit. Trades are also the closer match to the target: Chainlink's
  crypto feeds aggregate transaction prices, not quotes.
- **The quote arm is `@ticker` on Binance and `ticker` on Kraken** — both carry
  bid/ask *and* their own stamp, at ~1/s. That keeps "does the quote lead the
  trade" an answerable question rather than an assumption, on both venues, and
  1 Hz is ample against a lead measured in whole seconds.
- **Kraken as a second venue** disambiguates venue-specific noise from a real
  lead: a lead that appears on one venue and not the other is that venue's
  microstructure, not information about the oracle. Kraken v2 also lists all
  seven symbols including HYPE, so every symbol here has two independent
  readings.
- **HYPE is not on Binance** (`HYPEUSDT` → `-1121 Invalid symbol`; the
  `HYPER*` pairs are a different token and must not be substring-matched into
  the universe). It trades on Kraken as `HYPE/USD` and on **Hyperliquid**,
  whose public ws needs no key.

Two venue traps worth keeping in the file rather than in someone's memory:
Kraken **v2** wants `BTC/USD` and `DOGE/USD` — its own REST `AssetPairs` still
reports the legacy `XBT`/`XDG` `wsname`s, which v2 rejects — and Hyperliquid
spot coins are `@{index}` strings (HYPE/USDC is `@107`; plain `HYPE` is the
**perp**). Sending Hyperliquid a readable spot name like `HYPE/USDC` kills the
entire connection in ~0.2s with no close frame, taking every other subscription
on that socket with it.

Each venue is its own connection, its own thread and its own file. A stall on
one cannot silence another, and a venue's row rate is readable straight off its
file size.

Files are `~/.pmt/corpus/spot/spot-<venue>-YYYYMMDD.jsonl`, rotated on the
**UTC** day and with the path derived **per write** — the btc1h sampler wrote a
whole day into yesterday's file by resolving its path once at import.

Run it:  `uv run python -m polymarket.spot --minutes 60`
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
from pathlib import Path

SPOT_DIR = Path.home() / ".pmt" / "corpus" / "spot"
PIDFILE = SPOT_DIR / "recorder.pid"
LOGFILE = SPOT_DIR / "recorder.log"

EV_GAP = "gap"
EV_START = "start"
EV_STOP = "stop"

KIND_TRADE = "trade"
KIND_BOOK = "book"

# The six armed/candidate updown symbols plus hype (pilot2's series). Canonical
# short names, matching what `opponent_stream.py` splits out of the RTDS
# `btc/usd` form, so a join needs no translation layer on either side.
SYMBOLS: tuple[str, ...] = ("btc", "eth", "sol", "xrp", "bnb", "doge", "hype")

BINANCE_WS = "wss://data-stream.binance.vision/stream"
KRAKEN_WS = "wss://ws.kraken.com/v2"
HYPERLIQUID_WS = "wss://api.hyperliquid.xyz/ws"

# Per-venue symbol maps are explicit rather than derived. "DOGEUSDT"[:-4] works
# and "BTC/USD".split("/")[0].lower() works, but both silently invent a symbol
# for a pair we never subscribed to, and a mis-tagged row is worse in this
# corpus than a missing one.
BINANCE_SYMBOLS: dict[str, str] = {
    "btc": "btcusdt", "eth": "ethusdt", "sol": "solusdt",
    "xrp": "xrpusdt", "bnb": "bnbusdt", "doge": "dogeusdt",
}
# v2 spellings, NOT the legacy `XBT`/`XDG` that Kraken's own REST `AssetPairs`
# still reports as `wsname` — v2 answers those with "Currency pair not
# supported". USD and not USDT because HYPE/USDT does not exist.
KRAKEN_SYMBOLS: dict[str, str] = {
    "btc": "BTC/USD", "eth": "ETH/USD", "sol": "SOL/USD",
    "xrp": "XRP/USD", "bnb": "BNB/USD", "doge": "DOGE/USD",
    "hype": "HYPE/USD",
}
# `@107` is the HYPE/USDC **spot** pair; plain `HYPE` is the perp. Spot is what
# a settlement index tracks, so spot is what this records. Hyperliquid carries
# the majors too — a third reading of btc would be a nice control and costs a
# `--symbols` flag to turn on.
HYPERLIQUID_SYMBOLS: dict[str, str] = {"hype": "@107"}

VENUES: tuple[str, ...] = ("binance", "kraken", "hyperliquid")


# ---------- pure: the clock ----------

class Clock:
    """Wall-clock epoch seconds that cannot be stepped by NTP mid-run.

    `time.time()` is what everything else in the corpus is expressed in, but it
    is also what `ntpd` corrects — and a 200ms step landing in the middle of a
    recording is indistinguishable, downstream, from 200ms of lead appearing in
    the market. `time.monotonic()` never steps but has no epoch.

    So: anchor once, advance on the monotonic clock. The result is comparable to
    every other `t_recv` in `~/.pmt/corpus` while measuring *intervals* — which
    is the only thing a lead estimate reads — on a clock nothing can jog.

    The cost, stated plainly: over a long run this drifts from true UTC by
    whatever the host's oscillator error is, because it deliberately ignores the
    corrections. `skew()` reports the running disagreement so the heartbeat and
    the stop marker can both write it down.
    """

    def __init__(self) -> None:
        self.t0_wall = time.time()
        self.t0_mono = time.monotonic()

    def now(self) -> float:
        return self.t0_wall + (time.monotonic() - self.t0_mono)

    def skew(self) -> float:
        """anchored − true wall clock. Nonzero means NTP moved under us."""
        return self.now() - time.time()


# ---------- pure: timestamp parsing ----------

def parse_ms(value: object) -> float | None:
    """Exchange millisecond stamp → epoch seconds, or None."""
    if value is None or isinstance(value, bool):
        return None
    try:
        ms = float(value)
    except (TypeError, ValueError):
        return None
    if ms <= 0:
        return None
    return ms / 1000.0


def parse_rfc3339(value: object) -> float | None:
    """Kraken's `2026-08-24T11:32:19.505532Z` → epoch seconds, or None.

    Kept to the stdlib: `fromisoformat` handles the trailing `Z` from 3.11 on,
    and the microseconds are preserved rather than truncated to ms — that is the
    exchange's own ordering information and it is free to keep.
    """
    if not isinstance(value, str) or not value:
        return None
    import datetime as _dt
    try:
        return _dt.datetime.fromisoformat(value).timestamp()
    except ValueError:
        return None


def _f(value: object) -> float | None:
    """Venue price/size → float. Binance sends strings, Kraken sends numbers."""
    if value is None or isinstance(value, bool):
        return None
    try:
        x = float(value)
    except (TypeError, ValueError):
        return None
    return x if x == x and x not in (float("inf"), float("-inf")) else None


# ---------- pure: the record ----------

def make_row(t_recv: float, t_exch: float, venue: str, sym: str, kind: str,
             px: float, *, qty: float | None = None,
             bid: float | None = None, ask: float | None = None) -> dict:
    """One tape row. Both clocks are positional, which is the point.

    `px` is populated for every kind so a consumer has one price column: the
    trade price on a trade row, the **mid** on a book row. A book row keeps
    `bid`/`ask` alongside so the spread stays recoverable.
    """
    rec: dict = {
        "t_recv": round(float(t_recv), 6),
        "t_exch": round(float(t_exch), 6),
        "venue": venue,
        "sym": sym,
        "kind": kind,
        "px": px,
    }
    if qty is not None:
        rec["qty"] = qty
    if bid is not None:
        rec["bid"] = bid
    if ask is not None:
        rec["ask"] = ask
    return rec


def gap_record(t_recv: float, venue: str, down_s: float, *,
               t_last: float | None = None, reason: str = "") -> dict:
    """A hole in the tape, written into the tape.

    Spans last-frame-received → first-frame-after-reconnect, not close → open: a
    socket that is open but silent is just as much a hole, and on these feeds it
    is the more common one.
    """
    rec: dict = {"t_recv": round(float(t_recv), 6), "venue": venue,
                 "ev": EV_GAP, "down_s": round(float(down_s), 3)}
    if t_last is not None:
        rec["t_last"] = round(float(t_last), 6)
    if reason:
        rec["reason"] = reason
    return rec


def marker_record(t_recv: float, venue: str, ev: str, **extra) -> dict:
    """Lifecycle marker. Without one, a tape that ends because the box powered
    off is indistinguishable from one that ends because nobody traded."""
    return {"t_recv": round(float(t_recv), 6), "venue": venue, "ev": ev, **extra}


def daily_path(t: float, venue: str, directory: Path = SPOT_DIR) -> Path:
    """`spot-<venue>-YYYYMMDD.jsonl` for the **UTC** day containing epoch `t`.

    Called on every write, never cached: a path resolved once at import puts
    the whole of tomorrow into today's file, which is exactly what the btc1h
    sampler did.
    """
    return Path(directory) / f"spot-{venue}-{time.strftime('%Y%m%d', time.gmtime(t))}.jsonl"


# ---------- pure: per-venue parsers ----------
#
# Every parser returns a LIST: Kraken and Hyperliquid batch several events into
# one frame, and flattening that at the parser boundary keeps the reader loop
# from having to care which venue it is talking to.

def parse_binance(msg: object, t_recv: float) -> list[dict]:
    """Combined-stream envelope `{"stream": ..., "data": {...}}` → rows.

    Two event types, and `@bookTicker` is deliberately not one of them — see the
    module docstring. `@trade` is the fast transaction tape; `@ticker` is the
    1 Hz rolling-window statistic, which is only here because it happens to
    carry `b`/`a` **and** an `E`, making it the one stamped top-of-book Binance
    offers without maintaining an order book.

    On a trade, `T` (the transaction moment) is preferred over `E` (when the
    matching engine emitted the event): the former is when the price happened.
    """
    if not isinstance(msg, dict):
        return []
    data = msg.get("data")
    if not isinstance(data, dict):
        return []
    kind = data.get("e")
    if kind not in ("trade", "24hrTicker"):
        return []
    sym = _BINANCE_REV.get(str(data.get("s", "")).lower())
    if sym is None:
        return []
    if kind == "trade":
        t_exch = parse_ms(data.get("T"))
        if t_exch is None:
            t_exch = parse_ms(data.get("E"))
        px = _f(data.get("p"))
        if t_exch is None or px is None or px <= 0:
            return []
        return [make_row(t_recv, t_exch, "binance", sym, KIND_TRADE, px,
                         qty=_f(data.get("q")))]
    t_exch = parse_ms(data.get("E"))
    bid, ask = _f(data.get("b")), _f(data.get("a"))
    if t_exch is None or bid is None or ask is None or bid <= 0 or ask <= 0:
        return []
    return [make_row(t_recv, t_exch, "binance", sym, KIND_BOOK,
                     (bid + ask) / 2.0, bid=bid, ask=ask)]


def parse_kraken(msg: object, t_recv: float) -> list[dict]:
    """Kraken v2 `trade` and `ticker` channel frames → rows.

    `ticker` is the quote arm: it carries `bid`/`ask` AND its own `timestamp`,
    which `@bookTicker` on Binance does not, and is what makes the
    trades-vs-top-of-book comparison in §2 possible at all.
    """
    if not isinstance(msg, dict):
        return []
    channel = msg.get("channel")
    if channel not in ("trade", "ticker"):
        return []
    data = msg.get("data")
    if not isinstance(data, list):
        return []
    out: list[dict] = []
    for d in data:
        if not isinstance(d, dict):
            continue
        sym = _KRAKEN_REV.get(str(d.get("symbol", "")))
        if sym is None:
            continue
        t_exch = parse_rfc3339(d.get("timestamp"))
        if t_exch is None:
            continue
        if channel == "trade":
            px = _f(d.get("price"))
            if px is None or px <= 0:
                continue
            out.append(make_row(t_recv, t_exch, "kraken", sym, KIND_TRADE, px,
                                qty=_f(d.get("qty"))))
        else:
            bid, ask = _f(d.get("bid")), _f(d.get("ask"))
            if bid is None or ask is None or bid <= 0 or ask <= 0:
                continue
            out.append(make_row(t_recv, t_exch, "kraken", sym, KIND_BOOK,
                                (bid + ask) / 2.0, bid=bid, ask=ask))
    return out


def parse_hyperliquid(msg: object, t_recv: float) -> list[dict]:
    """Hyperliquid `trades` frames → rows. `time` is a millisecond stamp."""
    if not isinstance(msg, dict):
        return []
    if msg.get("channel") != "trades":
        return []
    data = msg.get("data")
    if not isinstance(data, list):
        return []
    out: list[dict] = []
    for d in data:
        if not isinstance(d, dict):
            continue
        sym = _HYPERLIQUID_REV.get(str(d.get("coin", "")))
        if sym is None:
            continue
        t_exch = parse_ms(d.get("time"))
        px = _f(d.get("px"))
        if t_exch is None or px is None or px <= 0:
            continue
        out.append(make_row(t_recv, t_exch, "hyperliquid", sym, KIND_TRADE, px,
                            qty=_f(d.get("sz"))))
    return out


_BINANCE_REV = {v: k for k, v in BINANCE_SYMBOLS.items()}
_KRAKEN_REV = {v: k for k, v in KRAKEN_SYMBOLS.items()}
_HYPERLIQUID_REV = {v: k for k, v in HYPERLIQUID_SYMBOLS.items()}


# ---------- pure: subscribe messages ----------

KINDS: tuple[str, ...] = (KIND_TRADE, KIND_BOOK)

# Which venue stream serves each record kind.
_BINANCE_STREAM = {KIND_TRADE: "trade", KIND_BOOK: "ticker"}


def binance_url(symbols: list[str], base: str = BINANCE_WS,
                kinds: tuple[str, ...] | list[str] = KINDS) -> str:
    """One combined connection carrying every symbol's requested streams.

    Well under the documented 1024-streams-per-connection ceiling at 2 per
    symbol, so the whole venue stays on one socket and one file.

    `kinds` exists because of a measured result, not for symmetry:
    `analysis/spot_lead.md` §S5 finds the 1 Hz `@ticker` quote captures the
    same lead as the full trade tape (btc r +0.934 at k=+2 against +0.919 at
    k=+3) at **0.8 rows/s instead of 106** — a ~130x disk saving for equal
    signal. A resident recorder should almost certainly run `--kinds book`.
    """
    streams = "/".join(f"{BINANCE_SYMBOLS[s]}@{_BINANCE_STREAM[k]}"
                       for s in symbols for k in kinds if k in _BINANCE_STREAM)
    return f"{base}?streams={streams}"


def kraken_subscribes(symbols: list[str],
                      kinds: tuple[str, ...] | list[str] = KINDS) -> list[str]:
    pairs = [KRAKEN_SYMBOLS[s] for s in symbols]
    channels = [c for k, c in ((KIND_TRADE, "trade"), (KIND_BOOK, "ticker"))
                if k in kinds]
    return [json.dumps({"method": "subscribe",
                        "params": {"channel": c, "symbol": pairs}})
            for c in channels]


def hyperliquid_subscribes(symbols: list[str]) -> list[str]:
    return [json.dumps({"method": "subscribe",
                        "subscription": {"type": "trades",
                                         "coin": HYPERLIQUID_SYMBOLS[s]}})
            for s in symbols]


def venue_symbols(venue: str, wanted: list[str]) -> list[str]:
    """The requested symbols this venue actually lists, in canonical order."""
    table = {"binance": BINANCE_SYMBOLS, "kraken": KRAKEN_SYMBOLS,
             "hyperliquid": HYPERLIQUID_SYMBOLS}[venue]
    return [s for s in wanted if s in table]


def venue_kinds(venue: str, wanted: tuple[str, ...] | list[str]) -> list[str]:
    """The requested record kinds this venue can actually serve.

    Hyperliquid is trades-only here: it does publish a stamped `bbo` channel,
    but nothing has needed it, and claiming a kind the recorder does not
    subscribe to would put trade rows in a book-only file.
    """
    served = {"binance": KINDS, "kraken": KINDS,
              "hyperliquid": (KIND_TRADE,)}[venue]
    return [k for k in wanted if k in served]


# ---------- pure: reconnect policy ----------

class Backoff:
    """Fast first retry, doubling, and a reset that requires proven health.

    The rtds recorder resets its backoff on every message received. That is
    almost right, and wrong in the one case that matters: a peer that accepts,
    sends a frame and drops resets the delay to 1s every time, so a flapping
    endpoint gets hammered at 1Hz forever. Health here means the connection
    *stayed up* for `healthy_after_s` — a duration a flap cannot fake.
    """

    def __init__(self, first: float = 0.5, cap: float = 30.0,
                 healthy_after_s: float = 30.0):
        self.first = first
        self.cap = cap
        self.healthy_after_s = healthy_after_s
        self.delay = first

    def next_delay(self) -> float:
        d = self.delay
        self.delay = min(self.delay * 2.0, self.cap)
        return d

    def note_uptime(self, uptime_s: float) -> bool:
        """Call on disconnect with how long the connection lasted.

        Returns True if that counted as healthy and the delay was reset.
        """
        if uptime_s >= self.healthy_after_s:
            self.delay = self.first
            return True
        return False


# ---------- writer ----------

class DailyWriter:
    """Append-only JSONL, UTC-daily rotation, one file per venue.

    Line-buffered, never fsynced per record: at Binance trade rates an fsync per
    row is an I/O stall on the hot path, and a stalled reader loses whole
    seconds rather than one line.
    """

    def __init__(self, venue: str, directory: Path = SPOT_DIR):
        self.venue = venue
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)
        self._path: Path | None = None
        self._fh = None

    def _handle(self, t: float):
        path = daily_path(t, self.venue, self.directory)
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

    def write_many(self, recs: list[dict]) -> None:
        """One `write()` per batch. Frames carrying several events are common on
        Kraken and Hyperliquid, and a single buffered write beats N of them."""
        if not recs:
            return
        fh = self._handle(recs[0].get("t_recv") or time.time())
        fh.write("".join(json.dumps(r) + "\n" for r in recs))

    def close(self) -> None:
        if self._fh is not None:
            try:
                self._fh.flush()
                os.fsync(self._fh.fileno())  # once, on the way out
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
    """Take the pidfile, or refuse if a live recorder holds it.

    A stale pidfile is taken over: the nightly poweroff guarantees them, and a
    recorder that refuses to start after every reboot is worse than useless.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    pid = read_pidfile(path)
    if pid is not None and pid != os.getpid() and pid_alive(pid):
        try:
            cmdline = Path(f"/proc/{pid}/cmdline").read_bytes().decode("utf8", "replace")
        except OSError:
            cmdline = ""
        if not cmdline or "spot" in cmdline:
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


def errlog(msg: str) -> None:
    """Failures go to stderr as well as the log. A recorder that dies quietly
    into a logfile nobody tails is the dead-sampler failure mode."""
    print(f"{_stamp()} {msg}", file=sys.stderr, flush=True)


# ---------- the per-venue recorder ----------

class VenueStats:
    def __init__(self, venue: str):
        self.venue = venue
        self.rows = 0
        self.frames = 0
        self.skipped = 0
        self.reconnects = 0
        self.connects = 0
        self.by_sym: Counter[str] = Counter()
        self.error: str = ""


def run_venue(venue: str, symbols: list[str], directory: Path, *,
              clock: Clock, stop: threading.Event, stats: VenueStats,
              kinds: tuple[str, ...] | list[str] = KINDS,
              deadline: float | None = None, once: bool = False,
              ping_s: float = 20.0, stall_s: float = 90.0,
              heartbeat_s: float = 60.0, max_backoff: float = 30.0,
              healthy_after_s: float = 30.0) -> None:
    """Record one venue until `stop`, `deadline`, or (with `once`) one drop.

    Runs in its own thread with its own socket and its own file, so a stall here
    cannot silence another venue.
    """
    from websockets.sync.client import connect

    kinds = tuple(kinds)
    if venue == "binance":
        url, subs, parse = binance_url(symbols, kinds=kinds), [], parse_binance
    elif venue == "kraken":
        url, subs, parse = KRAKEN_WS, kraken_subscribes(symbols, kinds), parse_kraken
    elif venue == "hyperliquid":
        url, subs, parse = HYPERLIQUID_WS, hyperliquid_subscribes(symbols), parse_hyperliquid
    else:
        stats.error = f"unknown venue {venue}"
        errlog(f"[{venue}] {stats.error}")
        return

    writer = DailyWriter(venue, directory)
    backoff = Backoff(cap=max_backoff, healthy_after_s=healthy_after_s)
    started = clock.now()
    hb_at = started
    window = Counter()
    t_last_frame: float | None = None
    pending_gap_reason = ""

    writer.write(marker_record(started, venue, EV_START, pid=os.getpid(),
                               symbols=symbols, url=url))
    log(f"[{venue}] start symbols={','.join(symbols)}")

    def heartbeat(now: float) -> None:
        nonlocal hb_at, window
        elapsed = now - hb_at
        if elapsed < 1.0:
            return
        top = " ".join(f"{s}={window[s] / elapsed:.1f}/s" for s in symbols if window[s])
        log(f"[{venue}] hb up={now - started:.0f}s "
            f"file={writer.path.name if writer.path else '-'} rows={stats.rows} "
            f"reconnects={stats.reconnects} skew={clock.skew() * 1000:+.0f}ms {top}")
        window = Counter()
        hb_at = now

    while not stop.is_set():
        if deadline and clock.now() >= deadline:
            break
        t_conn = clock.now()
        try:
            # ping_interval keeps the protocol-level keepalive going; the
            # app-level ping below is what Hyperliquid and Kraken actually count
            # as liveness.
            with connect(url, open_timeout=15, close_timeout=5,
                         ping_interval=20, ping_timeout=20,
                         max_queue=4096) as ws:
                stats.connects += 1
                t_conn = clock.now()
                for s in subs:
                    ws.send(s)
                log(f"[{venue}] connected ({len(subs)} subscribes)")
                last_ping = t_conn
                while not stop.is_set():
                    now = clock.now()
                    if deadline and now >= deadline:
                        break
                    if now - last_ping >= ping_s:
                        if venue in ("kraken", "hyperliquid"):
                            ws.send(json.dumps({"method": "ping"}))
                        last_ping = now
                    if now - hb_at >= heartbeat_s:
                        heartbeat(now)
                    try:
                        raw = ws.recv(timeout=1.0)
                    except TimeoutError:
                        # Silence is measured from the connect when nothing has
                        # ever arrived: a subscription the server quietly
                        # declines looks exactly like a healthy idle socket.
                        # Liveness is FRAMES, not rows — a market with no trades
                        # (hype at 04:00Z) is quiet, not broken, and the app
                        # ping above guarantees a pong to count.
                        quiet_since = t_last_frame if t_last_frame is not None else t_conn
                        if now - quiet_since > stall_s:
                            pending_gap_reason = "stall"
                            errlog(f"[{venue}] stalled {now - quiet_since:.0f}s "
                                   f"with the socket open — reconnecting")
                            break
                        continue
                    now = clock.now()
                    if isinstance(raw, bytes):
                        raw = raw.decode("utf8", "replace")
                    text = raw.strip()
                    if not text:
                        continue
                    stats.frames += 1
                    if t_last_frame is not None and pending_gap_reason:
                        down = now - t_last_frame
                        writer.write(gap_record(now, venue, down,
                                                t_last=t_last_frame,
                                                reason=pending_gap_reason))
                        log(f"[{venue}] gap {down:.1f}s ({pending_gap_reason}) recorded")
                    pending_gap_reason = ""
                    t_last_frame = now
                    try:
                        msg = json.loads(text)
                    except ValueError:
                        stats.skipped += 1
                        continue
                    rows = parse(msg, now)
                    if not rows:
                        stats.skipped += 1
                        continue
                    writer.write_many(rows)
                    stats.rows += len(rows)
                    for r in rows:
                        window[r["sym"]] += 1
                        stats.by_sym[r["sym"]] += 1
        except Exception as e:  # noqa: BLE001 — anything here means "reconnect"
            if stop.is_set():
                break
            stats.error = f"{type(e).__name__}: {e}"
            errlog(f"[{venue}] disconnected: {stats.error}")
            if not pending_gap_reason:
                pending_gap_reason = type(e).__name__
        else:
            if not stop.is_set() and not (deadline and clock.now() >= deadline):
                if not pending_gap_reason:
                    pending_gap_reason = "closed"
                    log(f"[{venue}] socket closed cleanly by peer")
        if stop.is_set() or (deadline and clock.now() >= deadline):
            break
        if once:
            log(f"[{venue}] --once: not reconnecting")
            break
        uptime = clock.now() - t_conn
        if backoff.note_uptime(uptime):
            log(f"[{venue}] connection was healthy for {uptime:.0f}s — backoff reset")
        stats.reconnects += 1
        d = backoff.next_delay()
        log(f"[{venue}] reconnecting in {d:.1f}s (attempt {stats.reconnects})")
        stop.wait(d)

    now = clock.now()
    heartbeat(now)
    writer.write(marker_record(now, venue, EV_STOP, pid=os.getpid(),
                               up_s=round(now - started, 1), rows=stats.rows,
                               reconnects=stats.reconnects,
                               clock_skew_s=round(clock.skew(), 6)))
    writer.close()
    log(f"[{venue}] stopped up={now - started:.0f}s rows={stats.rows} "
        f"reconnects={stats.reconnects}")


# ---------- supervisor ----------

def run(directory: Path = SPOT_DIR, *, venues: list[str] | None = None,
        symbols: list[str] | None = None, kinds: list[str] | None = None,
        duration: float | None = None,
        once: bool = False, stop: threading.Event | None = None,
        **kw) -> int:
    """Record every venue in parallel. Returns a process exit code.

    Exit contract, written to be LOUD — the dead btc1h sampler produced a
    zero-byte file and a zero exit code for a day before anyone noticed:

      0  every venue connected, received frames, and at least one produced rows
      1  a venue never connected or never received a frame  (plumbing broken)
      2  every venue received frames but nobody parsed a row (parser broken)
    """
    venues = list(venues or VENUES)
    symbols = list(symbols or SYMBOLS)
    kinds = list(kinds or KINDS)
    stop = stop if stop is not None else threading.Event()
    clock = Clock()
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    deadline = clock.now() + duration if duration else None

    plan = {v: (venue_symbols(v, symbols), venue_kinds(v, kinds))
            for v in venues}
    plan = {v: (s, k) for v, (s, k) in plan.items() if s and k}
    if not plan:
        errlog(f"no venue serves {','.join(kinds)} for any of "
               f"{','.join(symbols)} — nothing to record")
        return 1

    dropped = sorted(set(symbols) - {s for ss, _ in plan.values() for s in ss})
    if dropped:
        log(f"note: no configured venue carries {','.join(dropped)}")
    for v in venues:
        if v not in plan:
            log(f"note: {v} serves none of kinds={','.join(kinds)} — skipped")

    stats = {v: VenueStats(v) for v in plan}
    threads = []
    for v, (syms, vkinds) in plan.items():
        t = threading.Thread(target=run_venue, name=f"spot-{v}",
                             args=(v, syms, directory),
                             kwargs=dict(clock=clock, stop=stop, stats=stats[v],
                                         kinds=vkinds, deadline=deadline,
                                         once=once, **kw),
                             daemon=True)
        t.start()
        threads.append(t)

    try:
        while any(t.is_alive() for t in threads):
            if stop.is_set():
                break
            if deadline and clock.now() >= deadline:
                break
            time.sleep(0.5)
    except KeyboardInterrupt:
        stop.set()
    stop.set()
    for t in threads:
        t.join(timeout=30)

    total = sum(s.rows for s in stats.values())
    log("=" * 62)
    for v, s in sorted(stats.items()):
        syms = " ".join(f"{k}={n}" for k, n in sorted(s.by_sym.items()))
        log(f"  {v:12s} rows={s.rows:8d} frames={s.frames:8d} "
            f"connects={s.connects} reconnects={s.reconnects}  {syms}")
    log(f"  {'TOTAL':12s} rows={total:8d}  clock_skew={clock.skew() * 1000:+.0f}ms")

    rc, messages = exit_code(stats)
    for m in messages:
        errlog(m)
    return rc


def exit_code(stats: dict) -> tuple[int, list[str]]:
    """The loud-failure contract, as a pure function so it can be tested.

      0  every venue got frames, and something parsed
      1  a venue never received a frame          (plumbing / subscription)
      2  frames everywhere but nothing parsed    (a payload shape moved)

    A venue with frames but no rows is a WARNING, not a failure: a market can
    legitimately be silent for an hour (hype at 04:00Z), and turning that into
    a nonzero exit would train everyone to ignore the exit code — which is the
    actual lesson of the dead btc1h sampler, not the zero bytes.
    """
    msgs: list[str] = []
    rc = 0
    for v, s in sorted(stats.items()):
        if s.frames == 0:
            msgs.append(f"FAIL [{v}] received ZERO frames "
                        f"(connects={s.connects}, last error: {s.error or 'none'})")
            rc = 1
    total = sum(s.rows for s in stats.values())
    if rc == 0 and total == 0:
        msgs.append("FAIL every venue received frames but NOTHING parsed — "
                    "a payload shape changed under the parsers")
        rc = 2
    if rc == 0:
        for v, s in sorted(stats.items()):
            if s.frames and s.rows == 0:
                msgs.append(f"WARN [{v}] {s.frames} frames but zero rows parsed")
    return rc, msgs


# ---------- entrypoint ----------

def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="python -m polymarket.spot",
        description="Record live exchange spot ticks to ~/.pmt/corpus/spot/ — "
                    "the underlying the Chainlink settlement feed follows.",
    )
    ap.add_argument("--dir", default=str(SPOT_DIR), help="corpus directory")
    ap.add_argument("--venues", default=",".join(VENUES),
                    help=f"comma-separated subset of {','.join(VENUES)}")
    ap.add_argument("--symbols", default=",".join(SYMBOLS),
                    help=f"comma-separated subset of {','.join(SYMBOLS)}")
    ap.add_argument("--kinds", default=",".join(KINDS),
                    help=f"comma-separated subset of {','.join(KINDS)}. "
                         "`book` alone is ~1%% of the rows for the same "
                         "measured lead (analysis/spot_lead.md §S5) and is "
                         "the sane choice for a resident recorder; "
                         "hyperliquid serves `trade` only.")
    ap.add_argument("--minutes", type=float, default=None,
                    help="stop after N minutes (bounded run)")
    ap.add_argument("--duration", type=float, default=None,
                    help="stop after N seconds (bounded run)")
    ap.add_argument("--once", action="store_true",
                    help="one connection attempt per venue, no reconnect — "
                         "a smoke test that fails loudly rather than retrying")
    ap.add_argument("--stdout", action="store_true",
                    help="log to stdout instead of recorder.log")
    ap.add_argument("--no-pidfile", action="store_true",
                    help="skip the single-instance pidfile (testing)")
    args = ap.parse_args(argv)

    directory = Path(args.dir).expanduser()
    directory.mkdir(parents=True, exist_ok=True)

    duration = args.duration
    if args.minutes is not None:
        duration = args.minutes * 60.0

    if not args.stdout:
        stream = open(directory / LOGFILE.name, "a", buffering=1)
        sys.stdout = stream  # stderr stays attached: failures must be visible

    venues = [v.strip() for v in args.venues.split(",") if v.strip()]
    symbols = [s.strip().lower() for s in args.symbols.split(",") if s.strip()]
    kinds = [k.strip().lower() for k in args.kinds.split(",") if k.strip()]
    bad = [v for v in venues if v not in VENUES]
    if bad:
        errlog(f"unknown venue(s): {','.join(bad)}")
        return 1
    bad = [k for k in kinds if k not in KINDS]
    if bad:
        errlog(f"unknown kind(s): {','.join(bad)} (known: {','.join(KINDS)})")
        return 1

    pidfile = None
    if not args.no_pidfile:
        pidfile = directory / PIDFILE.name
        if not claim_pidfile(pidfile):
            errlog(f"refusing to start: recorder already running "
                   f"(pid {read_pidfile(pidfile)})")
            return 1

    stop = threading.Event()

    def _handle(signum, _frame):
        log(f"signal {signal.Signals(signum).name} — closing")
        stop.set()

    signal.signal(signal.SIGTERM, _handle)
    signal.signal(signal.SIGINT, _handle)

    try:
        return run(directory, venues=venues, symbols=symbols, kinds=kinds,
                   duration=duration, once=args.once, stop=stop)
    finally:
        if pidfile is not None:
            release_pidfile(pidfile)


if __name__ == "__main__":
    raise SystemExit(main())
