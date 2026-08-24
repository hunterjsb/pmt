"""Chainlink Polygon oracle rounds — the actual resolution source for updown markets.

pmengine prices updown windows off Binance klines, but Polymarket resolves
them against Chainlink's on-chain price. That basis has already cost real
money (two thin-margin losses where Binance said one side, Chainlink said
the other). This module fetches the raw Chainlink round history for the
five updown pairs directly off Polygon mainnet via `eth_call` — no web3
dependency, just hand-encoded ABI calls over plain JSON-RPC — so
`pmt crypto basis` can measure the real basis distribution instead of
guessing at per-arm guards.

Proxy round IDs pack a phase: `roundId = (phaseId << 64) | aggregatorRoundId`.
Walking history means decrementing the low 64 bits within the current phase;
v1 never crosses into an earlier phase (an updated_at of 0 marks the phase
boundary and stops the walk there — see `fetch_rounds`).
"""

from __future__ import annotations

import bisect
import json
import math
import time
from pathlib import Path

import requests

from . import errlog, hosts
from .fit import fetch_klines

REQUEST_TIMEOUT = 10
BATCH_SIZE = 50
_MAX_RETRIES_PER_URL = 2
_BACKOFF_S = 0.6

# polygon-rpc.com is the canonical public gateway per its own docs, but it's
# currently 401ing ("API key disabled, tenant disabled") — kept in the list
# since that's the kind of outage this fallback chain exists for, just moved
# LAST so live traffic doesn't eat its retry+backoff on every batch.
_RPC_URLS = [
    "https://polygon-bor-rpc.publicnode.com",
    "https://polygon.gateway.tenderly.co",
    "https://1rpc.io/matic",
    hosts.POLYGON_RPC,
]

SEL_DESCRIPTION = "0x7284e416"
SEL_DECIMALS = "0x313ce567"
SEL_LATEST_ROUND_DATA = "0xfeaf968c"
SEL_GET_ROUND_DATA = "0x9a6fc8f5"

PHASE_SHIFT = 64
_AGG_MASK = (1 << PHASE_SHIFT) - 1

# Polygon mainnet Chainlink proxy addresses — verified live via
# description()/decimals() eth_call (each description() echoed the pair
# below exactly; decimals() returned 8 for all of them). btc/eth/sol/xrp/doge
# verified 2026-08-22, bnb 2026-08-23.
FEEDS: dict[str, dict] = {
    "btc": {"pair": "BTC / USD", "address": "0xc907E116054Ad103354f2D350FD2514433D57F6f", "decimals": 8},
    "eth": {"pair": "ETH / USD", "address": "0xF9680D99D6C9589e2a93a78A04A279e509205945", "decimals": 8},
    "sol": {"pair": "SOL / USD", "address": "0x10C8264C0935b3B9870013e057f330Ff3e9C56dC", "decimals": 8},
    "xrp": {"pair": "XRP / USD", "address": "0x785ba89291f676b5386652eB12b30cF361020694", "decimals": 8},
    "doge": {"pair": "DOGE / USD", "address": "0xbaf9327b6564454F4a3364C33eFeEf032b4b4444", "decimals": 8},
    "bnb": {"pair": "BNB / USD", "address": "0x82a6c4AF830caa6c97bb504425f6A66165C2c26e", "decimals": 8},
}
SYMBOLS = list(FEEDS)

BINANCE_SYMBOL = {"btc": "BTCUSDT", "eth": "ETHUSDT", "sol": "SOLUSDT", "xrp": "XRPUSDT",
                  "doge": "DOGEUSDT", "bnb": "BNBUSDT"}

# Deployed per-arm basis guards (R1 aligned measurement + replay A/B).
# None = unmeasured corpus, so untradeable: xrp/doge via the Binance proxy,
# bnb until its own corpus lands. THE single source — `pmt crypto arm` and
# analysis/ both read it rather than keeping copies (docs/LESSONS.md#L18,
# docs/LESSONS.md#L32).
GUARD_BP: dict[str, float | None] = {"btc": 6.0, "eth": 8.0, "sol": 10.0,
                                     "xrp": None, "doge": None, "bnb": None}

_CHAINLINK_KEY = {v: k for k, v in BINANCE_SYMBOL.items()}


def guard_bp_for(binance_symbol: str) -> float | None:
    """Measured basis guard (bp) for a Binance pair like 'ETHUSDT'.

    None when the symbol has no measured corpus yet (xrp/doge/bnb) or isn't
    an updown pair at all — the caller decides what to do without one.
    `pmt crypto arm` is the reason this exists: a bare arm used to hand the
    engine a flat 3bp for every symbol, which under-guards the alts by 2-3x
    and structurally reproduces an incident we already paid for.
    """
    key = _CHAINLINK_KEY.get(binance_symbol.upper())
    return GUARD_BP.get(key) if key else None


CORPUS_DIR = Path.home() / ".pmt" / "corpus"


def corpus_path(symbol: str) -> Path:
    return CORPUS_DIR / f"chainlink-{symbol}.jsonl"


# ---------- roundId phase math ----------

def split_round_id(round_id: int) -> tuple[int, int]:
    """(phase_id, aggregator_round_id) from a proxy roundId."""
    return round_id >> PHASE_SHIFT, round_id & _AGG_MASK


def join_round_id(phase_id: int, aggregator_round_id: int) -> int:
    return (phase_id << PHASE_SHIFT) | aggregator_round_id


# ---------- ABI encode/decode (no web3 — the repo already avoids heavy deps) ----------

def _encode_round_id_call(round_id: int) -> str:
    return SEL_GET_ROUND_DATA + format(round_id, "064x")


def _decode_string(hexdata: str) -> str:
    b = bytes.fromhex(hexdata[2:])
    length = int.from_bytes(b[32:64], "big")
    return b[64:64 + length].decode("utf-8", errors="replace")


def _decode_uint(hexdata: str) -> int:
    return int(hexdata, 16)


def _decode_round(hexdata: str) -> dict:
    b = bytes.fromhex(hexdata[2:])
    return {
        "round_id": int.from_bytes(b[0:32], "big"),
        "answer": int.from_bytes(b[32:64], "big", signed=True),
        "started_at": int.from_bytes(b[64:96], "big"),
        "updated_at": int.from_bytes(b[96:128], "big"),
        "answered_in_round": int.from_bytes(b[128:160], "big"),
    }


# ---------- JSON-RPC transport ----------

def _post_batch(batch: list[dict]) -> list[dict]:
    """POST one JSON-RPC batch, trying each public endpoint with backoff."""
    last_err: Exception | None = None
    for url in _RPC_URLS:
        for attempt in range(_MAX_RETRIES_PER_URL):
            try:
                r = requests.post(url, json=batch, headers=hosts.UA, timeout=REQUEST_TIMEOUT)
                r.raise_for_status()
                data = r.json()
                if isinstance(data, dict) and "error" in data:
                    raise RuntimeError(f"{url}: {data['error']}")
                return data
            except Exception as e:
                last_err = e
                time.sleep(_BACKOFF_S * (attempt + 1))
    raise RuntimeError(f"all Polygon RPC endpoints failed: {last_err}")


def _eth_call_batch(calls: list[tuple[str, str]]) -> list[str | None]:
    """Batched eth_call over (to, data) pairs; None per-item on a per-call RPC error."""
    out: list[str | None] = []
    for start in range(0, len(calls), BATCH_SIZE):
        chunk = calls[start:start + BATCH_SIZE]
        batch = [{"jsonrpc": "2.0", "id": i, "method": "eth_call",
                  "params": [{"to": to, "data": data}, "latest"]}
                 for i, (to, data) in enumerate(chunk)]
        results = _post_batch(batch)
        by_id = {item.get("id"): item for item in results}
        out.extend(by_id.get(i, {}).get("result") for i in range(len(chunk)))
    return out


def _eth_call(to: str, data: str) -> str:
    res = _eth_call_batch([(to, data)])[0]
    if res is None:
        raise RuntimeError(f"eth_call to {to} {data} returned no result")
    return res


# ---------- feed reads ----------

def description(address: str) -> str:
    return _decode_string(_eth_call(address, SEL_DESCRIPTION))


def decimals(address: str) -> int:
    return _decode_uint(_eth_call(address, SEL_DECIMALS))


def verify_feeds() -> dict[str, dict]:
    """Live re-check of every hardcoded FEEDS entry against description()/decimals()."""
    out = {}
    for sym, feed in FEEDS.items():
        desc = description(feed["address"])
        dec = decimals(feed["address"])
        out[sym] = {"description": desc, "decimals": dec,
                    "ok": desc.strip() == feed["pair"] and dec == feed["decimals"]}
    return out


def _walk_phase_back(addr: str, phase: int, agg: int, cutoff: float) -> list[dict]:
    """Decoded rounds walking aggregator ids DOWN from `agg`, newest first.

    Stops at the phase start (updated_at 0) or the first round older than
    `cutoff`. Shared by the live walk (fetch_rounds, starting at the latest
    round) and the backfill (extend_corpus_backward, starting at the corpus's
    oldest) so their stop conditions and batch cadence can't drift apart.
    """
    collected: list[dict] = []
    while agg >= 0:
        calls = [(addr, _encode_round_id_call(join_round_id(phase, i)))
                 for i in range(agg, max(agg - BATCH_SIZE, -1), -1)]
        done = False
        for raw in _eth_call_batch(calls):
            if raw is None:
                continue
            rd = _decode_round(raw)
            if rd["updated_at"] <= 0 or rd["updated_at"] < cutoff:
                # zero updated_at = walked past the phase start; below cutoff = out of window
                done = True
                break
            collected.append(rd)
        if done:
            break
        agg -= BATCH_SIZE
        time.sleep(0.05)  # be polite to the public endpoint
    return collected


def _corpus_rows(rounds: list[dict], scale: int) -> list[dict]:
    """Decoded rounds -> corpus row shape, price decimals-adjusted."""
    return [{"round_id": r["round_id"], "price": r["answer"] / scale,
             "updated_at": r["updated_at"]} for r in rounds]


def fetch_rounds(symbol: str, hours: float = 24.0) -> list[dict]:
    """Walk one feed's round history back `hours` (or to the phase start, whichever first).

    Returns oldest-first: [{"round_id", "price", "updated_at"}, ...]. `price`
    is decimals-adjusted; `updated_at` is unix seconds.
    """
    feed = FEEDS[symbol]
    addr, scale = feed["address"], 10 ** feed["decimals"]
    cutoff = time.time() - hours * 3600

    latest = _decode_round(_eth_call(addr, SEL_LATEST_ROUND_DATA))
    if latest["updated_at"] <= 0:
        return []
    phase, agg = split_round_id(latest["round_id"])
    collected = [latest] + _walk_phase_back(addr, phase, agg - 1, cutoff)
    collected.reverse()
    return _corpus_rows(collected, scale)


# ---------- corpus (append-only ground truth, never rewritten) ----------

def load_corpus(symbol: str, since: float | None = None) -> list[dict]:
    """Rounds already on disk, optionally filtered to updated_at >= since (unix s)."""
    path = corpus_path(symbol)
    rows: list[dict] = []
    if not path.exists():
        return rows
    with open(path) as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                r = json.loads(line)
            except ValueError as e:
                errlog.note("chainlink.load_corpus.corrupt_line", e,
                            path=str(path), line=line[:200])
                continue
            if since is None or r.get("updated_at", 0) >= since:
                rows.append(r)
    return rows


def append_corpus(symbol: str, rounds: list[dict]) -> int:
    """Append rounds not already on disk (deduped by round_id). Returns count appended."""
    path = corpus_path(symbol)
    path.parent.mkdir(parents=True, exist_ok=True)
    existing = {r["round_id"] for r in load_corpus(symbol)}
    new_rows = [r for r in rounds if r["round_id"] not in existing]
    if new_rows:
        with open(path, "a") as fh:
            for r in new_rows:
                fh.write(json.dumps(r) + "\n")
    return len(new_rows)


REFRESH_MIN_H = 3.0   # always reach past the last fetch, even if it was minutes ago
REFRESH_MAX_H = 48.0  # ceiling on one walk (~48h is ~5.7k rounds/symbol at the 30s cadence)


def refresh_corpus(symbols: list[str], since: float, now: float | None = None) -> dict[str, dict]:
    """Top each symbol's corpus up to the live round, for callers about to grade off it.

    Reaches back to the newest round already on disk (the gap since the last
    run) or to `since` — the oldest window being graded — when the corpus is
    empty, capped at REFRESH_MAX_H so a months-old tape can't turn a grading
    run into an unbounded RPC walk. Filling a gap OLDER than the corpus is
    `extend_corpus_backward`'s job, not this one.

    Never raises: a per-symbol failure comes back as `error` so the caller can
    warn and fall back to whatever corpus is already on disk. Unknown symbols
    (a slug for a pair we have no feed for) are skipped with an error, never a
    KeyError.

    Returns {sym: {"new", "hours", "error"}}.
    """
    now = time.time() if now is None else now
    out: dict[str, dict] = {}
    for sym in symbols:
        row: dict = {"new": 0, "hours": 0.0, "error": None}
        out[sym] = row
        if sym not in FEEDS:
            row["error"] = "no Chainlink feed for this symbol"
            continue
        try:
            newest = max((r["updated_at"] for r in load_corpus(sym)), default=0.0)
            reach = now - (newest if newest > 0 else since)
            row["hours"] = min(max(reach / 3600.0, REFRESH_MIN_H), REFRESH_MAX_H)
            row["new"] = append_corpus(sym, fetch_rounds(sym, hours=row["hours"]))
        except Exception as e:
            row["error"] = str(e)
    return out


# ---------- basis: Chainlink vs Binance ----------

def _fetch_minute_closes(binance_symbol: str, start_ms: int, end_ms: int) -> dict[int, float]:
    """Binance 1m close per minute-open ms, paginated like fit.extreme_since."""
    out: dict[int, float] = {}
    cursor = start_ms
    while cursor <= end_ms:
        kl = fetch_klines(binance_symbol, "1m", start_ms=cursor)
        if not kl:
            break
        for k in kl:
            t = int(k[0])
            if t > end_ms:
                break
            out[t] = float(k[4])
        if len(kl) < 1000:
            break
        cursor = int(kl[-1][0]) + 60_000
    return out


def join_basis(rounds: list[dict], minute_closes: dict[int, float]) -> list[dict]:
    """Per-round basis_bp = (chainlink/binance_1m_close - 1) * 1e4. Rounds with no matching minute are dropped."""
    rows = []
    for r in rounds:
        minute_ms = (int(r["updated_at"]) // 60) * 60_000
        close = minute_closes.get(minute_ms)
        if not close:
            continue
        basis_bp = (r["price"] / close - 1) * 1e4
        rows.append({"round_id": r["round_id"], "updated_at": r["updated_at"],
                     "chainlink_price": r["price"], "binance_price": close, "basis_bp": basis_bp})
    return rows


def _percentile(sorted_vals: list[float], p: float) -> float:
    """Linear-interpolation percentile, p in [0, 100]. sorted_vals sorted ascending, non-empty."""
    n = len(sorted_vals)
    if n == 1:
        return sorted_vals[0]
    k = (p / 100) * (n - 1)
    f, c = math.floor(k), math.ceil(k)
    if f == c:
        return sorted_vals[int(k)]
    return sorted_vals[f] * (c - k) + sorted_vals[c] * (k - f)


def basis_stats(rows: list[dict]) -> dict | None:
    vals = [r["basis_bp"] for r in rows]
    n = len(vals)
    if n == 0:
        return None
    mean = sum(vals) / n
    std = math.sqrt(sum((v - mean) ** 2 for v in vals) / (n - 1)) if n > 1 else 0.0
    svals = sorted(vals)
    abs_sorted = sorted(abs(v) for v in vals)
    return {
        "n": n, "mean": mean, "std": std,
        "p5": _percentile(svals, 5), "p50": _percentile(svals, 50), "p95": _percentile(svals, 95),
        "p95_abs": _percentile(abs_sorted, 95), "max_abs": abs_sorted[-1],
    }


def basis_report(symbol: str, hours: float = 24.0) -> dict:
    """Join the stored corpus for one symbol against Binance 1m closes; compute the basis distribution."""
    cutoff = time.time() - hours * 3600
    rounds = load_corpus(symbol, since=cutoff)
    if not rounds:
        return {"symbol": symbol, "n_rounds": 0, "basis": [], "stats": None}
    rounds.sort(key=lambda r: r["updated_at"])
    start_ms = (rounds[0]["updated_at"] // 60) * 60_000
    end_ms = (rounds[-1]["updated_at"] // 60) * 60_000
    closes = _fetch_minute_closes(BINANCE_SYMBOL[symbol], start_ms, end_ms)
    basis = join_basis(rounds, closes)
    return {"symbol": symbol, "n_rounds": len(rounds), "basis": basis, "stats": basis_stats(basis)}


# ---------- aligned basis: TWAP-vs-TWAP Chainlink-vs-Binance (ROADMAP.md R1) ----------
#
# The point-in-time join above compares a single Chainlink round against a 1m
# Binance close — that bakes up to 60s of pure timing noise on top of real
# basis. This measures what actually matters instead: a time-weighted
# Chainlink average against the Binance mark shape the engine actually banks,
# over the same window. Two variants:
#   per-minute:  Chainlink TWAP over [m, m+60) vs the engine's Binance mark
#                ((open+close)/2 of the 1m kline) for the same minute.
#   settlement:  at real settlement boundaries, Chainlink's actual settlement
#                TWAP (30s @5m close, 60s @15m close) vs the last Binance
#                mark the model banks — the error that decides wins/losses.

ALIGNED_STALE_S = 600      # no round in the prior 10 min -> feed too stale, no carry-in value
ALIGNED_FETCH_BUFFER_H = 2.0  # backfill past the requested window so edge minutes aren't starved


def extend_corpus_backward(symbol: str, target_hours: float) -> int:
    """Walk further back from the corpus's oldest known round to cover `target_hours`.

    Mirrors fetch_rounds' walk, but starts at the existing corpus's oldest
    round instead of the live latest — re-fetching hours we already have
    would double the RPC load on the public endpoints for no reason.
    """
    existing = load_corpus(symbol)
    if not existing:
        return append_corpus(symbol, fetch_rounds(symbol, hours=target_hours))
    existing.sort(key=lambda r: r["round_id"])
    oldest = existing[0]
    cutoff = time.time() - target_hours * 3600
    if oldest["updated_at"] <= cutoff:
        return 0
    feed = FEEDS[symbol]
    phase, agg = split_round_id(oldest["round_id"])
    rounds = _walk_phase_back(feed["address"], phase, agg - 1, cutoff)
    return append_corpus(symbol, _corpus_rows(rounds, 10 ** feed["decimals"]))


def extend_all(target_hours: float, symbols: list[str]) -> dict[str, dict]:
    """Top up (last 3h) + backfill each symbol's corpus to cover `target_hours`.

    Returns {sym: {"topped", "backfilled", "top_up_error", "backfill_error"}}
    so callers (CLI, the standalone R1 script) can render their own summary.
    """
    out: dict[str, dict] = {}
    for sym in symbols:
        row = {"topped": 0, "backfilled": 0, "top_up_error": None, "backfill_error": None}
        try:
            row["topped"] = append_corpus(sym, fetch_rounds(sym, hours=3.0))  # close the gap since last fetch
        except Exception as e:
            row["top_up_error"] = str(e)
        try:
            row["backfilled"] = extend_corpus_backward(sym, target_hours)
        except Exception as e:
            row["backfill_error"] = str(e)
        out[sym] = row
    return out


def sorted_rounds(rounds: list[dict]) -> tuple[list[dict], list[int]]:
    """Rounds oldest-first, plus the parallel updated_at list twap_over_window
    bisects — the pair every windowed read below needs.

    Its own function so a caller grading N windows off ONE symbol's corpus
    prepares it once. Doing it inside the per-window call re-sorts the same
    ~6k rounds N times, which is the same per-window-instead-of-per-symbol
    cost as re-reading the file.
    """
    rs = sorted(rounds, key=lambda r: r["updated_at"])
    return rs, [r["updated_at"] for r in rs]


def twap_over_window(rounds: list[dict], ts_list: list[int], start: int, end: int) -> float | None:
    """Time-weighted average of the step function (answer holds until the next round).

    `rounds`/`ts_list` must be sorted ascending by updated_at (ts_list is the
    parallel updated_at list — passed separately so callers bisect once and
    reuse it across many windows). None if there's no round at/before `start`
    (no carry-in value) or the feed was already stale entering the window
    (no update in the prior ALIGNED_STALE_S).
    """
    idx = bisect.bisect_right(ts_list, start) - 1
    if idx < 0 or start - ts_list[idx] > ALIGNED_STALE_S:
        return None
    total, cur_t, j, n = 0.0, start, idx, len(ts_list)
    while cur_t < end:
        nxt_t = min(ts_list[j + 1], end) if j + 1 < n else end
        total += rounds[j]["price"] * (nxt_t - cur_t)
        cur_t, j = nxt_t, j + 1
    return total / (end - start)


def fetch_minute_marks(binance_symbol: str, start_s: int, end_s: int) -> dict[int, float]:
    """Binance (open+close)/2 per minute — the mark shape the engine actually prices with."""
    out: dict[int, float] = {}
    cursor_ms = (start_s // 60) * 60_000
    end_ms = (end_s // 60) * 60_000
    while cursor_ms <= end_ms:
        kl = fetch_klines(binance_symbol, "1m", start_ms=cursor_ms)
        if not kl:
            break
        for k in kl:
            t_ms = int(k[0])
            if t_ms > end_ms:
                break
            out[t_ms // 1000] = (float(k[1]) + float(k[4])) / 2
        if len(kl) < 1000:
            break
        cursor_ms = int(kl[-1][0]) + 60_000
    return out


def per_minute_basis(rounds: list[dict], marks: dict[int, float]) -> list[dict]:
    """aligned_basis_bp(m) = (chainlink_twap(m)/binance_mark(m) - 1) * 1e4 for each minute [m, m+60)."""
    if not rounds:
        return []
    ts_list = [r["updated_at"] for r in rounds]
    first_m = ((rounds[0]["updated_at"] // 60) + 1) * 60
    last_m = ((rounds[-1]["updated_at"] // 60) + 1) * 60
    out = []
    for m in range(first_m, last_m, 60):
        twap = twap_over_window(rounds, ts_list, m, m + 60)
        mark = marks.get(m)
        if twap is None or not mark:
            continue
        out.append({"t": m, "basis_bp": (twap / mark - 1) * 1e4})
    return out


def settlement_basis(rounds: list[dict], marks: dict[int, float], period_s: int, ck_window_s: int) -> list[dict]:
    """The error that actually decides wins/losses: Chainlink's real settlement TWAP
    vs the last Binance mark the model banks (the minute ending at the boundary)."""
    if not rounds:
        return []
    ts_list = [r["updated_at"] for r in rounds]
    first_end = ((rounds[0]["updated_at"] // period_s) + 1) * period_s
    last_end = ((rounds[-1]["updated_at"] // period_s) + 1) * period_s
    out = []
    for end in range(first_end, last_end, period_s):
        twap = twap_over_window(rounds, ts_list, end - ck_window_s, end)
        mark = marks.get(end - 60)
        if twap is None or not mark:
            continue
        out.append({"t": end, "basis_bp": (twap / mark - 1) * 1e4})
    return out


def aligned_stats(rows: list[dict]) -> dict | None:
    """Stats over |basis_bp| — n/mean/std/p50/p90/p95/p99/max, the R1 report shape."""
    vals = sorted(abs(r["basis_bp"]) for r in rows)
    n = len(vals)
    if n == 0:
        return None
    mean = sum(vals) / n
    std = (sum((v - mean) ** 2 for v in vals) / (n - 1)) ** 0.5 if n > 1 else 0.0
    return {
        "n": n, "mean": mean, "std": std,
        "p50": _percentile(vals, 50), "p90": _percentile(vals, 90),
        "p95": _percentile(vals, 95), "p99": _percentile(vals, 99),
        "max": vals[-1],
    }


def signed_bias(rows: list[dict]) -> float:
    vals = [r["basis_bp"] for r in rows]
    return sum(vals) / len(vals) if vals else 0.0


def aligned_basis_report(symbol: str, hours: float = 24.0) -> dict:
    """Both aligned variants for one symbol, joined from the on-disk corpus (no fetch).

    {"symbol", "n_rounds", "span_h", "per_minute", "settlement_5m", "settlement_15m"}
    — the last three are `aligned_stats()` dicts (or None with no corpus data).
    """
    cutoff = time.time() - hours * 3600
    rounds = sorted(load_corpus(symbol, since=cutoff), key=lambda r: r["updated_at"])
    if not rounds:
        return {"symbol": symbol, "n_rounds": 0, "span_h": 0.0,
                "per_minute": None, "settlement_5m": None, "settlement_15m": None}
    span_h = (rounds[-1]["updated_at"] - rounds[0]["updated_at"]) / 3600
    marks = fetch_minute_marks(BINANCE_SYMBOL[symbol], rounds[0]["updated_at"], rounds[-1]["updated_at"] + 60)
    pm = per_minute_basis(rounds, marks)
    s5 = settlement_basis(rounds, marks, period_s=300, ck_window_s=30)
    s15 = settlement_basis(rounds, marks, period_s=900, ck_window_s=60)
    return {
        "symbol": symbol, "n_rounds": len(rounds), "span_h": span_h,
        "per_minute": aligned_stats(pm), "settlement_5m": aligned_stats(s5), "settlement_15m": aligned_stats(s15),
    }
