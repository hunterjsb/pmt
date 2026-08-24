"""Live settlement stream — an in-memory consumer built on `polymarket.rtds`.

The recorder in `polymarket.rtds` writes the tape; this reads the same socket
into RAM so the pricer has the four inputs it needs at decision time. It does
NOT fork the recorder: the subscribe frame, the envelope normaliser, the E18
parser and the topic names all come from that module, so the protocol lessons
it paid for (one entry per TOPIC not per symbol, the mandatory 4s PING, "open
but dead" detection) apply here for free and cannot drift.

Two topics, not three: the 60s TWAP (the reference the market settles against,
stamped at window start) and Chainlink spot (the walk the model projects). The
30s TWAP is what the width bug priced and nothing here reads it.

The socket is free and unauthenticated, so being a third subscriber alongside
the recorder and the engine costs nobody anything.
"""

from __future__ import annotations

import threading
import time
from collections import deque

from polymarket import rtds

from .predict import MAX_STALE_S, SETTLE_WIDTH_S, sigma_per_second

TOPICS = (rtds.TOPIC_TWAP60, rtds.TOPIC_SPOT)

# 1 Hz feed; the pricer's longest lookback is the 300s sigma window, so ten
# minutes of spot is generous headroom and costs ~10k floats for all 8 symbols.
RING_S = 600
PING_S = 4.0        # not optional — the flow dies silently without it
STALL_S = 30.0      # "open but dead" is this feed's signature failure
MAX_BACKOFF_S = 30.0


class SymbolSeries:
    """Per-symbol rings of (ts_seconds, price). Both are strictly the
    CHAINLINK observation clock, never our receive clock: the settlement
    average is defined on Chainlink's timestamps and comparing our arrival
    times to a window boundary would be measuring relay lag, not price."""

    __slots__ = ("last_recv", "spot_px", "spot_ts", "twap_px", "twap_ts")

    def __init__(self) -> None:
        self.spot_ts: deque[float] = deque(maxlen=RING_S)
        self.spot_px: deque[float] = deque(maxlen=RING_S)
        # The reference is a print at a window START, and a 15m window's start
        # is 900s back — the twap ring must outlive the longest window we
        # price, so it is deliberately deeper than the spot ring.
        self.twap_ts: deque[float] = deque(maxlen=4 * RING_S)
        self.twap_px: deque[float] = deque(maxlen=4 * RING_S)
        self.last_recv: float = 0.0


class StreamState:
    """Thread-safe read model over the stream. The feed thread writes; the
    poll loop reads. Every reader method copies what it needs under the lock
    and does its arithmetic outside, so a slow pricer never stalls the socket."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._sym: dict[str, SymbolSeries] = {}
        self.connected_at: float = 0.0
        self.last_msg_at: float = 0.0
        self.reconnects: int = 0
        self.messages: int = 0

    # ---- writer side (feed thread) ----

    def ingest(self, rec: dict, t_recv: float) -> None:
        """One normalised RTDS record. Ignores anything that isn't a price we
        price with — the E18 string is the value of record, never the float."""
        px = rtds.e18_decimal(rec.get("full_accuracy_value"))
        if px is None:
            v = rec.get("value")
            if not isinstance(v, (int, float)) or isinstance(v, bool):
                return
            px = float(v)
        px = float(px)
        if not (px > 0.0):
            return
        ts = float(rec["ts"]) / 1000.0
        sym = rec["symbol"]
        with self._lock:
            s = self._sym.get(sym)
            if s is None:
                s = self._sym[sym] = SymbolSeries()
            if rec["topic"] == rtds.TOPIC_SPOT:
                # The feed can repeat a second on reconnect; a duplicate ts
                # would read as a 0s gap and poison sigma's consecutive test.
                if s.spot_ts and ts <= s.spot_ts[-1]:
                    return
                s.spot_ts.append(ts)
                s.spot_px.append(px)
            elif rec["topic"] == rtds.TOPIC_TWAP60:
                if s.twap_ts and ts <= s.twap_ts[-1]:
                    return
                s.twap_ts.append(ts)
                s.twap_px.append(px)
            else:
                return
            s.last_recv = t_recv
            self.last_msg_at = t_recv
            self.messages += 1

    # ---- reader side (poll loop) ----

    def _snapshot(self, symbol: str) -> tuple[list[float], list[float], list[float], list[float]] | None:
        with self._lock:
            s = self._sym.get(symbol)
            if s is None:
                return None
            return list(s.spot_ts), list(s.spot_px), list(s.twap_ts), list(s.twap_px)

    def reference(self, symbol: str, start: float) -> float | None:
        """The 60s-TWAP print stamped AT `start`, or None.

        EXACT match, and that is the spec: "the reference is the TWAP print AT
        start, not the minute mark before it". A dropped second at the window
        boundary means this window has no reference and cannot be priced —
        the same way a stream-fed arm gates on a missing reference print
        rather than answering off a neighbouring one.
        """
        snap = self._snapshot(symbol)
        if snap is None:
            return None
        _, _, tts, tpx = snap
        target = float(int(start))
        for ts, px in zip(reversed(tts), reversed(tpx)):
            if ts == target:
                return px
            if ts < target:
                break
        return None

    def spot(self, symbol: str, now: float, max_stale_s: float = MAX_STALE_S) -> tuple[float, float] | None:
        """(price, age_s) of the newest spot print, or None if it is stale.

        The 5s staleness bound is the predictor spec's own input contract, and
        the same bound every rtds-fed arm in the engine gates on.
        """
        snap = self._snapshot(symbol)
        if snap is None:
            return None
        sts, spx, _, _ = snap
        if not sts:
            return None
        age = now - sts[-1]
        if age > max_stale_s or age < -max_stale_s:
            return None
        return spx[-1], age

    def banked(self, symbol: str, end: float, now: float) -> list[float]:
        """Spot prints stamped in [end-60, now] — the settlement seconds that
        have already printed. Width is 60s at EVERY duration."""
        snap = self._snapshot(symbol)
        if snap is None:
            return []
        sts, spx, _, _ = snap
        lo = end - SETTLE_WIDTH_S
        return [px for ts, px in zip(sts, spx) if lo <= ts <= now]

    def sigma(self, symbol: str, start: float, now: float) -> float:
        """Per-second log-return stdev over [max(now-300, start-60), now].

        The `start-60` floor is a real part of the spec, not an accident: early
        in a window the lookback is only ~70s and the reported results were
        produced that way. A full trailing 300s is a DIFFERENT model and would
        have to be re-validated before it shipped.
        """
        snap = self._snapshot(symbol)
        if snap is None:
            return float("nan")
        sts, spx, _, _ = snap
        lo = max(now - 300.0, start - 60.0)
        ts = [t for t in sts if lo <= t <= now]
        px = [p for t, p in zip(sts, spx) if lo <= t <= now]
        return sigma_per_second(ts, px)

    def health(self, now: float | None = None) -> dict:
        now = time.time() if now is None else now
        with self._lock:
            return {
                "connected": bool(self.connected_at),
                "silent_s": round(now - self.last_msg_at, 1) if self.last_msg_at else None,
                "reconnects": self.reconnects,
                "messages": self.messages,
                "symbols": len(self._sym),
            }


def run_feed(state: StreamState, stop: threading.Event, *, url: str = rtds.RTDS_URL,
             topics: tuple[str, ...] = TOPICS, log=print) -> None:
    """Forever-retry subscriber. Runs on its own thread; never raises out.

    Deliberately a thread rather than folded into the poll loop: the poll loop
    makes blocking REST calls to the CLOB, and a multi-second HTTP call inside
    a stream reader's loop is the exact bug `analysis/watch_load.md` is about.
    """
    from websockets.sync.client import connect

    backoff = 1.0
    while not stop.is_set():
        try:
            with connect(url, open_timeout=15, close_timeout=5) as ws:
                ws.send(rtds.subscribe_message(topics))
                t_conn = time.time()
                with state._lock:
                    state.connected_at = t_conn
                log(f"stream connected ({len(topics)} topics)")
                last_ping = t_conn
                while not stop.is_set():
                    now = time.time()
                    if now - last_ping >= PING_S:
                        ws.send("PING")
                        last_ping = now
                    try:
                        raw = ws.recv(timeout=1.0)
                    except TimeoutError:
                        # Measure silence from the connect when nothing has
                        # ever arrived: a subscription the server quietly
                        # declined looks exactly like a healthy idle socket.
                        quiet_since = state.last_msg_at or t_conn
                        if now - quiet_since > STALL_S:
                            log(f"stream stalled {now - quiet_since:.0f}s with the socket open")
                            break
                        continue
                    if isinstance(raw, bytes):
                        raw = raw.decode("utf8", "replace")
                    text = raw.strip()
                    if not text or text.upper() == "PONG":
                        continue
                    import json
                    try:
                        msg = json.loads(text)
                    except ValueError:
                        continue
                    rec = rtds.normalize(msg, time.time())
                    if rec is not None:
                        state.ingest(rec, time.time())
                        backoff = 1.0
        except Exception as e:  # noqa: BLE001 — any failure here means "reconnect"
            if stop.is_set():
                break
            log(f"stream disconnected: {type(e).__name__}: {e}")
        if stop.is_set():
            break
        with state._lock:
            state.reconnects += 1
        stop.wait(backoff)
        backoff = min(backoff * 2, MAX_BACKOFF_S)
