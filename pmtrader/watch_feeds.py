"""Incremental tails of the recorder corpora, for the watch dashboard's feed
charts. Runs on the fetch worker; the render loop never touches this file.

The corpora are the two the fleet already writes and nothing here starts a
socket of its own: `~/.pmt/corpus/rtds/` is the Chainlink stream the markets
SETTLE on, `~/.pmt/corpus/spot/` is the exchange tape that stream is a lagging
function of (analysis lives in the vault's `spot_lead.md`). Both are appended
to continuously and rtds alone is ~240MB/day, so the ONE rule this module
exists to hold is:

  * a poll reads only the bytes appended since the last poll, from a
    remembered offset, capped. Re-reading a corpus per frame — or even per
    minute — is the blocking-call-in-the-render-loop shape the dashboard was
    split in two to avoid.

Everything else is degradation. No corpus, no recorder, a symbol the stream
does not carry, a day rollover, a torn last line, a file truncated under us:
each costs the panel what it costs and never the dashboard.
"""

from __future__ import annotations

import json
import time
from collections import deque
from pathlib import Path

from polymarket import errlog, rtds, rtds_read, spot

# Seeds: enough tail to fill the charts' own axes on the first frame, and no
# more. rtds appends ~6KB/s and the per-symbol rows plot 120s; the spot tape
# runs ~3x that and only the 30s lead block reads it.
RTDS_SEED_BYTES = 2 << 20
SPOT_SEED_BYTES = 1 << 20
# Per-poll ceiling. A dashboard whose worker stalled (a slow wallet walk, a
# suspended laptop) must not answer by parsing the whole backlog on one tick —
# it skips forward to the recent end, which is the only part any chart plots.
MAX_CHUNK_BYTES = 4 << 20

# How much history the ring buffers hold. The widest axis any panel draws.
KEEP_S = 150.0
# Spot trade tapes run ~17 prints/s/symbol; a chart cell is worth far less
# resolution than that, and the deques are read on the render thread.
SPOT_MIN_GAP_S = 0.2
# A venue is this symbol's spot source while its newest print is this fresh.
VENUE_FRESH_S = 10.0
# Backfill re-attempt floor for a window whose opening TWAP print predates the
# dashboard. A miss is usually "the recorder was down then" and re-reading the
# corpus every tick to rediscover that is exactly what this module refuses.
BACKFILL_RETRY_S = 60.0
# Minute marks kept per symbol: enough for the longest window the fleet trades.
MARKS_KEEP = 300


class CorpusTail:
    """Follow the newest daily file matching `pattern`, from a byte offset.

    Three transitions the recorders actually make, and what each means here:
    a NEW day's file appears (read it from byte 0 — it starts empty), the file
    SHRINKS (a rotation or a truncated write: start over rather than seek past
    the end), and the first sight of a file the dashboard did not watch being
    written (seed from its tail, never its head — the head is hours of history
    no chart on screen plots).
    """

    def __init__(self, directory, pattern: str, *,
                 seed_bytes: int = RTDS_SEED_BYTES,
                 max_chunk: int = MAX_CHUNK_BYTES) -> None:
        self._dir = Path(directory)
        self._pattern = pattern
        self._seed_bytes = seed_bytes
        self._max_chunk = max_chunk
        self._path: Path | None = None
        self._offset = 0
        self._skip_first = False

    @property
    def path(self) -> Path | None:
        return self._path

    def _newest(self) -> Path | None:
        # Names carry a UTC date, so lexical order is chronological order.
        try:
            files = sorted(p for p in self._dir.glob(self._pattern) if p.is_file())
        except OSError:
            return None
        return files[-1] if files else None

    def read_new(self) -> list[str]:
        """Complete lines appended since the last call. Never raises."""
        try:
            return self._read_new()
        except OSError as e:
            # A corpus that vanished mid-run (a backup sweep, an unmounted
            # home) leaves the panel with the samples it has. Marked because a
            # silently-frozen chart is indistinguishable from a quiet market.
            errlog.note("watch_feeds.CorpusTail.read_new", e, pattern=self._pattern)
            return []

    def _read_new(self) -> list[str]:
        path = self._newest()
        if path is None:
            return []
        size = path.stat().st_size
        if path != self._path:
            first_sight = self._path is None
            self._path = path
            # First sight seeds from the tail; a day rollover starts at 0
            # because the new file opens empty and every byte of it is new.
            self._offset = max(0, size - self._seed_bytes) if first_sight else 0
            self._skip_first = self._offset > 0
        if size < self._offset:
            self._offset, self._skip_first = 0, False
        if size - self._offset > self._max_chunk:
            self._offset = size - self._max_chunk
            self._skip_first = True
        if size <= self._offset:
            return []
        with path.open("rb") as fh:
            fh.seek(self._offset)
            data = fh.read(size - self._offset)
        cut = data.rfind(b"\n") + 1
        if cut == 0:
            return []  # a record still being written: leave the offset alone
        self._offset += cut
        lines = data[:cut].decode("utf-8", "replace").split("\n")[:-1]
        if self._skip_first:
            # The seek landed inside a record; its first fragment is not one.
            self._skip_first = False
            lines = lines[1:]
        return lines


def _sym(rtds_symbol: object) -> str | None:
    """`btc/usd` -> `btc`, the symbol an updown slug names."""
    if not isinstance(rtds_symbol, str) or "/" not in rtds_symbol:
        return None
    return rtds_symbol.split("/", 1)[0]


class FeedTail:
    """Live per-symbol price paths off the two recorder corpora.

    Every method here runs on the fetch worker. poll() returns a whole
    snapshot dict which the caller publishes into WatchState, so the render
    thread reads an already-built mapping and never walks a deque this class
    is appending to — the same hand-off every other watch source uses.
    """

    def __init__(self, rtds_dir=None, spot_dir=None, *,
                 keep_s: float = KEEP_S, venues=None) -> None:
        self._keep_s = keep_s
        self._rtds = CorpusTail(rtds.RTDS_DIR if rtds_dir is None else rtds_dir,
                                "rtds-*.jsonl", seed_bytes=RTDS_SEED_BYTES)
        venues = spot.VENUES if venues is None else venues
        self._venues = tuple(venues)
        self._spot_tails = {
            v: CorpusTail(spot.SPOT_DIR if spot_dir is None else spot_dir,
                          f"spot-{v}-*.jsonl", seed_bytes=SPOT_SEED_BYTES)
            for v in self._venues
        }
        self._chain: dict[str, deque] = {}
        self._spot: dict[tuple[str, str], deque] = {}
        self._marks: dict[str, dict[float, float]] = {}
        self._backfilled: dict[str, float] = {}

    # -- ingest --

    def _add_chain(self, sym: str, t: float, px: float) -> None:
        self._chain.setdefault(sym, deque()).append((t, px))

    def _add_spot(self, venue: str, sym: str, t: float, px: float) -> None:
        d = self._spot.setdefault((venue, sym), deque())
        if d and t - d[-1][0] < SPOT_MIN_GAP_S:
            return
        d.append((t, px))

    def _ingest_rtds(self, lines: list[str]) -> None:
        for raw in lines:
            try:
                rec = json.loads(raw)
            except ValueError as e:
                # Not the trailing partial (read_new never yields one), so this
                # write finished badly and the sample is gone for good.
                errlog.note("watch_feeds.FeedTail.rtds_line", e, line=raw[:200])
                continue
            if not isinstance(rec, dict):
                continue
            sym = _sym(rec.get("symbol"))
            ts = rec.get("ts")
            if sym is None or not isinstance(ts, (int, float)) or isinstance(ts, bool):
                continue
            px = rtds_read.price_of(rec)
            if px is None:
                continue
            topic = rec.get("topic")
            if topic == rtds.TOPIC_SPOT:
                # The chainlink clock, never our receive clock: every boundary
                # these markets settle on is defined there.
                self._add_chain(sym, ts / 1000.0, px)
            elif topic == rtds.TOPIC_TWAP60:
                self._bank_mark(sym, ts / 1000.0, px)

    def _bank_mark(self, sym: str, ts: float, px: float) -> None:
        """One settlement TWAP print, keyed as rtds_read.twap_marks keys it —
        the print at `m+60` averages minute `m`, so a window opening at `start`
        reads its strike at `start - 60`."""
        sec = int(ts)
        mark = sec // 60 * 60
        if sec - mark > rtds_read.MARK_TOL_S:
            return
        marks = self._marks.setdefault(sym, {})
        marks.setdefault(float(mark - 60), px)
        if len(marks) > MARKS_KEEP:
            for k in sorted(marks)[:len(marks) - MARKS_KEEP]:
                marks.pop(k, None)

    def _ingest_spot(self, venue: str, lines: list[str]) -> None:
        for raw in lines:
            try:
                rec = json.loads(raw)
            except ValueError as e:
                errlog.note("watch_feeds.FeedTail.spot_line", e, line=raw[:200])
                continue
            if not isinstance(rec, dict) or rec.get("ev"):
                continue  # start/stop/gap markers carry no price
            sym, px = rec.get("sym"), rec.get("px")
            # t_exch, not t_recv: the whole point of this row is WHEN the venue
            # printed, and our receive clock is the lookahead the corpus format
            # exists to keep out (polymarket/spot.py).
            t = rec.get("t_exch")
            if not isinstance(sym, str) or not isinstance(t, (int, float)):
                continue
            if not isinstance(px, (int, float)) or isinstance(px, bool) or px <= 0:
                continue
            self._add_spot(venue, sym, float(t), float(px))

    # -- the settlement strike for a window the dashboard opened mid-flight --

    def _backfill_marks(self, slugs, now: float) -> None:
        """One bounded reverse read per armed window whose opening TWAP print
        landed before this dashboard started tailing.

        `rtds_read.twap_marks` walks the corpus BACKWARD with a byte budget and
        stops at the horizon, which is the same read `pmt crypto arm`'s
        pre-flight makes — reused rather than reimplemented so the strike on
        this chart and the strike the arm was priced against cannot differ.
        Attempted once per window, then throttled: a miss almost always means
        the recorder was down for that minute, and rediscovering that every
        tick is the corpus re-read this module refuses.
        """
        from polymarket import updown_slugs

        for slug in slugs:
            # /status is a parsed JSON body, so an arm key is only a slug by
            # convention — the same isinstance discipline the header applies.
            parsed = updown_slugs.parse_updown_slug(slug if isinstance(slug, str) else "")
            if parsed is None:
                continue
            sym, start = parsed["symbol"], float(parsed["start"])
            if float(start - 60.0) in self._marks.get(sym, {}):
                continue
            if now - self._backfilled.get(slug, 0.0) < BACKFILL_RETRY_S:
                continue
            self._backfilled[slug] = now
            try:
                found = rtds_read.twap_marks(f"{sym}/usd", start - 180.0)
            except Exception as e:
                # A strike we cannot read makes one chart plot raw price with
                # no zero line. Marked, because "the corpus is unreadable" and
                # "this window opened before the recorder" look identical on
                # the panel and want opposite fixes.
                errlog.note("watch_feeds.FeedTail.backfill", e, slug=slug)
                continue
            for k, v in found.items():
                self._marks.setdefault(sym, {}).setdefault(float(k), float(v))

    # -- one worker tick --

    def poll(self, arm_slugs=(), now: float | None = None) -> dict:
        """Read what the recorders appended, then publish a fresh snapshot."""
        now = time.time() if now is None else now
        self._ingest_rtds(self._rtds.read_new())
        for venue, tail in self._spot_tails.items():
            self._ingest_spot(venue, tail.read_new())
        self._backfill_marks(list(arm_slugs), now)
        self._trim(now)
        return self.snapshot(now)

    def _trim(self, now: float) -> None:
        floor = now - self._keep_s
        for d in list(self._chain.values()) + list(self._spot.values()):
            while d and d[0][0] < floor:
                d.popleft()

    def _venue_for(self, sym: str, now: float) -> str | None:
        """The freshest venue carrying this symbol, in preference order — one
        line per symbol, so a thin venue never displaces a liquid one."""
        for v in self._venues:
            d = self._spot.get((v, sym))
            if d and now - d[-1][0] <= VENUE_FRESH_S:
                return v
        return None

    def snapshot(self, now: float | None = None) -> dict:
        """A whole result object for WatchState: lists, not the live deques."""
        now = time.time() if now is None else now
        chain = {s: list(d) for s, d in self._chain.items() if d}
        syms = {s for _v, s in self._spot}
        venue = {s: v for s in sorted(syms) if (v := self._venue_for(s, now))}
        spot_px = {s: list(self._spot[(v, s)]) for s, v in venue.items()}
        marks = {s: dict(m) for s, m in self._marks.items() if m}
        return {"chain": chain, "spot": spot_px, "venue": venue, "marks": marks,
                "at": now}
