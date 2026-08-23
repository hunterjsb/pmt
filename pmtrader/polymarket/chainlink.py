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

import json
import math
import time
from pathlib import Path

import requests

from . import hosts
from .fit import fetch_klines

REQUEST_TIMEOUT = 10
BATCH_SIZE = 50
_MAX_RETRIES_PER_URL = 2
_BACKOFF_S = 0.6

# polygon-rpc.com first (canonical public gateway, per its own docs) with
# public fallbacks — it was returning "API key disabled, tenant disabled"
# during development, which is exactly the kind of outage this list exists for.
_RPC_URLS = [
    hosts.POLYGON_RPC,
    "https://polygon-bor-rpc.publicnode.com",
    "https://polygon.gateway.tenderly.co",
    "https://1rpc.io/matic",
]

SEL_DESCRIPTION = "0x7284e416"
SEL_DECIMALS = "0x313ce567"
SEL_LATEST_ROUND_DATA = "0xfeaf968c"
SEL_GET_ROUND_DATA = "0x9a6fc8f5"

PHASE_SHIFT = 64
_AGG_MASK = (1 << PHASE_SHIFT) - 1

# Polygon mainnet Chainlink proxy addresses — verified live 2026-08-22 via
# description()/decimals() eth_call (each description() echoed the pair
# below exactly; decimals() returned 8 for all five).
FEEDS: dict[str, dict] = {
    "btc": {"pair": "BTC / USD", "address": "0xc907E116054Ad103354f2D350FD2514433D57F6f", "decimals": 8},
    "eth": {"pair": "ETH / USD", "address": "0xF9680D99D6C9589e2a93a78A04A279e509205945", "decimals": 8},
    "sol": {"pair": "SOL / USD", "address": "0x10C8264C0935b3B9870013e057f330Ff3e9C56dC", "decimals": 8},
    "xrp": {"pair": "XRP / USD", "address": "0x785ba89291f676b5386652eB12b30cF361020694", "decimals": 8},
    "doge": {"pair": "DOGE / USD", "address": "0xbaf9327b6564454F4a3364C33eFeEf032b4b4444", "decimals": 8},
}
SYMBOLS = list(FEEDS)

BINANCE_SYMBOL = {"btc": "BTCUSDT", "eth": "ETHUSDT", "sol": "SOLUSDT", "xrp": "XRPUSDT", "doge": "DOGEUSDT"}

# Live per-arm basis guards (ROADMAP.md 2026-08-23): BTC 3bp, alts 6bp, XRP
# off pending this exact data. DOGE isn't in the live fleet yet — no guard set.
GUARD_BP: dict[str, float | None] = {"btc": 3.0, "eth": 6.0, "sol": 6.0, "xrp": None, "doge": None}

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


def latest_round_data(address: str) -> dict:
    return _decode_round(_eth_call(address, SEL_LATEST_ROUND_DATA))


def get_round_data(address: str, round_id: int) -> dict:
    return _decode_round(_eth_call(address, _encode_round_id_call(round_id)))


def verify_feeds() -> dict[str, dict]:
    """Live re-check of every hardcoded FEEDS entry against description()/decimals()."""
    out = {}
    for sym, feed in FEEDS.items():
        desc = description(feed["address"])
        dec = decimals(feed["address"])
        out[sym] = {"description": desc, "decimals": dec,
                    "ok": desc.strip() == feed["pair"] and dec == feed["decimals"]}
    return out


def fetch_rounds(symbol: str, hours: float = 24.0) -> list[dict]:
    """Walk one feed's round history back `hours` (or to the phase start, whichever first).

    Returns oldest-first: [{"round_id", "price", "updated_at"}, ...]. `price`
    is decimals-adjusted; `updated_at` is unix seconds.
    """
    feed = FEEDS[symbol]
    addr, dec = feed["address"], feed["decimals"]
    scale = 10 ** dec
    cutoff = time.time() - hours * 3600

    latest = _decode_round(_eth_call(addr, SEL_LATEST_ROUND_DATA))
    if latest["updated_at"] <= 0:
        return []
    phase, agg = split_round_id(latest["round_id"])

    collected = [latest]
    agg -= 1
    while agg >= 0:
        ids = list(range(agg, max(agg - BATCH_SIZE, -1), -1))
        calls = [(addr, _encode_round_id_call(join_round_id(phase, i))) for i in ids]
        raws = _eth_call_batch(calls)
        done = False
        for i, raw in zip(ids, raws):
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

    collected.reverse()
    return [{"round_id": r["round_id"], "price": r["answer"] / scale, "updated_at": r["updated_at"]}
            for r in collected]


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
            except ValueError:
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
