"""Characterization fixtures — freeze one real window into one JSON file.

A fixture is everything `pmengine replay` needs to re-run ONE recorded
window offline, forever: the eval tape slice, the book tape slice, the 1m
klines the model reads, the params as they were armed, and the wallet's
verdict. `pmengine/src/replay/fixtures.rs` is the reader; this module is the
writer, and the two shapes are one contract.

Two rules this module enforces rather than documents:

* **Wallet-graded only.** `outcome.source` must be "wallet". A chainlink- or
  book-derived label is an inference, and L36 is the receipt for what
  happens when an inference is treated as ground truth. The Rust loader
  refuses a non-wallet fixture too — belt and braces, because a fixture is
  the thing every other measurement gets checked against.
* **No secrets, ever.** The tapes carry none today; `secret_scan` asserts it
  rather than trusting that, and the freezer fails instead of writing.
  Real CTF token ids are replaced by `{slug}-up` / `{slug}-down`: replay
  only uses them as dictionary keys, so a synthetic pair is both sufficient
  and one less on-chain identifier in the repo.

Everything here is pure (no network, no ambient disk) so the slicing,
param-reconstruction and redaction rules are unit-testable; `pmt crypto
fixture` in cli_crypto.py does the I/O and calls in.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from collections.abc import Iterable

from .updown_slugs import parse_updown_slug

FIXTURE_VERSION = 1

# How far before a window's start the model's kline feed reaches back. Must
# match updown_model.rs::KLINE_LOOKBACK_S — replay reconstructs rho from
# exactly this span, and a short slice silently reconstructs a different one.
KLINE_LOOKBACK_S = 2700

# The book-tape fields replay.rs::view_from_book_record + replay_full_window
# actually read. Everything else on a book record (print flow, per-side
# source/age diagnostics) is corpus for other studies and is dropped: a
# fixture carries the decision's inputs, not the night's whole telemetry.
BOOK_KEYS = (
    "t", "ev", "slug", "spot", "spot_age_s",
    "up_bid", "up_bid_sz", "up_ask", "up_ask_sz",
    "dn_bid", "dn_bid_sz", "dn_ask", "dn_ask_sz",
)

# Serde field order in fixtures.rs::Fixture. Matching it means the first
# `--bless` rewrites values, not the whole file.
FIXTURE_KEYS = (
    "fixture_version", "slug", "mode", "teaches", "lessons_ref", "era",
    "window_utc", "params", "params_provenance", "outcome", "evals", "book",
    "klines", "invariants", "expect", "provenance",
)
OUTCOME_KEYS = (
    "winner", "source", "buy", "buy_shares", "buy_side", "sell", "redeem",
    "redeem_seen", "redeem_outcome", "pnl",
)

SYMBOL = {"btc": "BTCUSDT", "eth": "ETHUSDT", "sol": "SOLUSDT",
          "xrp": "XRPUSDT", "bnb": "BNBUSDT", "doge": "DOGEUSDT"}

# Anything shaped like an EVM address or a 32-byte hex blob. The tapes carry
# neither; the point is to fail loudly the day one starts to.
_HEX_BLOB = re.compile(r"0x[0-9a-fA-F]{40,}")
# A bare CTF token id: 76-78 decimal digits. Not a secret, but it is an
# on-chain identifier with no job in an offline fixture.
_TOKEN_ID = re.compile(r"\b\d{70,}\b")


class FixtureError(Exception):
    """A window that cannot be frozen honestly. Never a warning — a fixture
    that quietly ships a guess is worse than no fixture."""


# ---------- slicing ----------

def slice_tape(records: Iterable[dict], slug: str) -> list[dict]:
    """This slug's records in time order. Exact slug match, never a prefix:
    a fixture is ONE window."""
    out = [r for r in records if r.get("slug") == slug]
    out.sort(key=lambda r: r.get("t", 0.0))
    return out


def trim_book_record(rec: dict) -> dict:
    """A book record reduced to the fields replay reads, key order preserved
    so a fixture diff is stable."""
    return {k: rec[k] for k in BOOK_KEYS if k in rec}


def kline_slice(rows: Iterable[dict], start: int, end: int) -> tuple[list[dict], list[int]]:
    """(rows, missing_minutes) covering [start - KLINE_LOOKBACK_S, end) at
    minute resolution, deduped by t and sorted.

    `missing` is what makes full mode refusable: replay must fail on a cache
    miss rather than fetch, so a fixture with a hole in its feed is not a
    full-mode fixture at all.
    """
    lo = (start - KLINE_LOOKBACK_S) // 60 * 60
    hi = end // 60 * 60
    have: dict[int, dict] = {}
    for r in rows:
        t = r.get("t")
        if not isinstance(t, int) or not (lo <= t < hi):
            continue
        o, c = r.get("o"), r.get("c")
        if not isinstance(o, (int, float)) or not isinstance(c, (int, float)):
            continue
        have[t] = {"t": t, "o": o, "c": c}
    missing = [m for m in range(lo, hi, 60) if m not in have]
    return [have[t] for t in sorted(have)], missing


# ---------- wallet truth ----------

def wallet_accounting(activity_rows: Iterable[dict], slug: str) -> dict:
    """What the money did on one window, from wallet activity rows.

    Mirrors cli_crypto.score_activity's aggregation over the same rows. The
    `winner` is NOT derived here — it comes from the graded outcomes corpus,
    which already applies L22/L23's dust-redeem rules; this only accounts.
    """
    acct = {"buy": 0.0, "buy_shares": 0.0, "buy_side": None, "sell": 0.0,
            "redeem": 0.0, "redeem_seen": False, "redeem_outcome": None, "pnl": 0.0}
    sides: dict[str, float] = {}
    for a in activity_rows:
        if a.get("slug") != slug:
            continue
        usd = a.get("usdcSize") or 0.0
        if a.get("type") == "TRADE":
            if a.get("side") == "BUY":
                acct["buy"] += usd
                acct["buy_shares"] += a.get("size") or 0.0
                side = (a.get("outcome") or "").lower()
                sides[side] = sides.get(side, 0.0) + usd
            else:
                acct["sell"] += usd
        elif a.get("type") == "REDEEM":
            acct["redeem"] += usd
            acct["redeem_seen"] = True
            if usd > 0.5:
                acct["redeem_outcome"] = (a.get("outcome") or "").lower()
    if sides:
        acct["buy_side"] = max(sides, key=lambda k: sides[k])
    acct["pnl"] = round(acct["redeem"] + acct["sell"] - acct["buy"], 6)
    for k in ("buy", "buy_shares", "sell", "redeem"):
        acct[k] = round(acct[k], 6)
    return acct


def build_outcome(graded: dict | None, acct: dict, slug: str) -> dict:
    """The fixture's `outcome` block. Refuses anything but a wallet grade."""
    if graded is None:
        raise FixtureError(
            f"{slug}: no graded outcome — run `pmt crypto outcomes` first, and note "
            f"that a window nobody traded can never be wallet-graded (no redeem, no truth)"
        )
    source = graded.get("source")
    if source != "wallet":
        raise FixtureError(
            f"{slug}: outcome is {source}-graded, not wallet-graded. Characterization "
            f"fixtures are ground truth and take the settlement Polymarket actually "
            f"paid — a derived label can lie (docs/LESSONS.md#L36)"
        )
    winner = graded.get("winner")
    if winner not in ("up", "down"):
        raise FixtureError(f"{slug}: graded winner {winner!r} is neither up nor down")
    return {"winner": winner, "source": "wallet", **{k: acct[k] for k in OUTCOME_KEYS[2:]}}


# ---------- params as armed ----------

def _round_up_to(value: float, step: float) -> float:
    return math.ceil(value / step) * step


def build_params(slug: str, tape_recs: list[dict], live_arm: dict,
                 series_roll_size: float | None = None,
                 overrides: dict | None = None,
                 lifted_tunables: bool = False) -> tuple[dict, dict]:
    """(params, provenance) — the arm as it ran, as far as the tape proves it.

    What the tape KNOWS, and how:
      size_usdc        the window's own `roll` record (its real as-armed budget),
                       else the series' first roll
      basis_guard_bp   the lowest `guard_bp` this window recorded — the static
                       param the live dynamic guard raises FROM (L17)
      clip_usdc        the largest clip that actually fired, rounded up to $5.
                       The sizer caps at clip_usdc, so the biggest fire is a
                       tight lower bound; every window in the seed catalog
                       lands exactly on 25 or 50 this way. A window that never
                       fired has no evidence and inherits.
      theta            0 unless the tape shows a `safety` brake, which only
                       exists when theta > 0 (updown_model::safety_gate_blocks)
                       — that is how a pre-R9 window stays pre-R9.
      pay_up_max       0, always. The fire record carries `ask` but not the
                       submitted limit, so a chase is NOT recoverable from any
                       recorded window (issue #5, gap 2). 0 replays the clip at
                       the ask it recorded rather than inventing a chase.

    Everything else is inherited from the arm store at freeze time and is a
    RECONSTRUCTION, said so in the provenance. Those knobs are either
    unchanged since the recorded night (fee_rate, quiesce, cooldown) or read
    only as cold-start fallbacks that replay overwrites from the klines
    (sigma_bp_per_min).
    """
    w = parse_updown_slug(slug)
    if not w:
        raise FixtureError(f"{slug}: not a recognizable updown slug")
    coin = slug.split("-")[0]
    symbol = SYMBOL.get(coin)
    if not symbol:
        raise FixtureError(f"{slug}: no Binance symbol known for '{coin}'")

    prov: dict[str, str] = {}
    p = dict(live_arm)
    for k in ("token_up", "token_down"):
        p.pop(k, None)
    inherited = sorted(p)
    p.update({
        "slug": slug, "symbol": symbol,
        "token_up": f"{slug}-up", "token_down": f"{slug}-down",
        "start": float(w["start"]), "end": float(w["end"]),
        "roll": False,
    })
    for k in inherited:
        prov[k] = "inherited: arm store at freeze time"
    for k in ("slug", "symbol", "start", "end"):
        prov[k] = "slug"
    prov["token_up"] = prov["token_down"] = "synthesized (replay uses them as keys only)"
    prov["roll"] = "forced false — a fixture is one window, never a chain"

    roll_size = next((r.get("size") for r in reversed(tape_recs) if r.get("ev") == "roll"), None)
    if roll_size is not None:
        p["size_usdc"], prov["size_usdc"] = float(roll_size), "tape: this window's roll record"
    elif series_roll_size is not None:
        p["size_usdc"] = float(series_roll_size)
        prov["size_usdc"] = "tape: the series' first roll record"

    guards = [r["guard_bp"] for r in tape_recs
              if isinstance(r.get("guard_bp"), (int, float))]
    if guards:
        p["basis_guard_bp"] = float(min(guards))
        prov["basis_guard_bp"] = "tape: lowest recorded guard_bp (the static param)"

    fire_notionals = [(r.get("ask") or 0.0) * (r.get("size") or 0.0)
                      for r in tape_recs if r.get("ev") == "fire"]
    if fire_notionals:
        p["clip_usdc"] = _round_up_to(max(fire_notionals), 5.0)
        prov["clip_usdc"] = "tape: largest fired clip, rounded up to $5"

    safety_braked = any(
        s.get("brake") == "safety"
        for r in tape_recs if r.get("ev") == "eval"
        for s in (r.get("sides") or [])
    )
    if not safety_braked:
        p["theta"] = 0.0
        prov["theta"] = "tape: no `safety` brake recorded, so theta was 0 (pre-R9)"
    else:
        prov["theta"] = "tape: `safety` brake recorded, so theta was live at the freeze-time value"

    p["pay_up_max"] = 0.0
    prov["pay_up_max"] = "NOT RECOVERABLE — the fire record has no `limit` (issue #5 gap 2); 0 replays at the recorded ask"

    for k, v in (overrides or {}).items():
        p[k] = v
        prov[k] = "operator override"
    if lifted_tunables:
        # The pre-brake policy, expressible only through replay's Tunables —
        # live arms cannot reach these.
        p["tunables"] = {"distrust_net": 1e9, "avg_down_tol": 1e9}
        prov["tunables"] = "operator override: brakes lifted to reproduce the pre-brake engine"
    return p, prov


# ---------- redaction ----------

def secret_scan(text: str, needles: Iterable[str] = ()) -> list[str]:
    """Everything in `text` that must not be committed. Empty list = clean.

    `needles` is for values only the caller knows (the funder address, a
    configured key) — passing them makes the check specific instead of
    merely shaped.
    """
    hits = sorted(set(_HEX_BLOB.findall(text)) | set(_TOKEN_ID.findall(text)))
    for n in needles:
        if n and len(n) >= 8 and n in text:
            hits.append(n[:10] + "...")
    return hits


# ---------- assembly / rendering ----------

def sha256_records(records: Iterable[dict]) -> str:
    """Hash of the records as this fixture stores them — the provenance
    check that a slice was not edited after it was cut."""
    h = hashlib.sha256()
    for r in records:
        h.update(json.dumps(r, sort_keys=True, separators=(",", ":")).encode())
        h.update(b"\n")
    return h.hexdigest()


def build_fixture(slug: str, mode: str, params: dict, params_prov: dict,
                  outcome: dict, evals: list[dict], book: list[dict],
                  klines: list[dict], teaches: str, lessons_ref: str | None,
                  eras: list[str], invariants: list[str], provenance: dict,
                  expect: dict | None = None) -> dict:
    w = parse_updown_slug(slug)
    return {
        "fixture_version": FIXTURE_VERSION,
        "slug": slug,
        "mode": mode,
        "teaches": teaches,
        "lessons_ref": lessons_ref,
        "era": list(eras),
        "window_utc": [_utc(w["start"]), _utc(w["end"])],
        "params": params,
        "params_provenance": params_prov,
        "outcome": outcome,
        "evals": evals,
        "book": book,
        "klines": klines,
        "invariants": list(invariants),
        "expect": expect,
        "provenance": provenance,
    }


def _utc(epoch: float) -> str:
    import datetime as _dt
    return _dt.datetime.fromtimestamp(epoch, _dt.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def render_fixture(obj: dict) -> str:
    """Mirror of fixtures.rs::render_fixture — top-level keys pretty, record
    arrays one per line.

    Same renderer on both sides so `--bless` (which rewrites the file from
    Rust) produces a diff of the numbers that moved, not of the whole file.
    """
    keys = [k for k in FIXTURE_KEYS if k in obj] + [k for k in obj if k not in FIXTURE_KEYS]
    lines = ["{"]
    for i, k in enumerate(keys):
        val = obj[k]
        comma = "" if i + 1 == len(keys) else ","
        if isinstance(val, list) and val and all(isinstance(e, (dict, int, float)) for e in val):
            lines.append(f" {json.dumps(k)}: [")
            for j, e in enumerate(val):
                c = "" if j + 1 == len(val) else ","
                lines.append(f"  {json.dumps(e, separators=(',', ':'))}{c}")
            lines.append(f" ]{comma}")
        else:
            body = json.dumps(val, indent=2).replace("\n", "\n ")
            lines.append(f" {json.dumps(k)}: {body}{comma}")
    lines.append("}")
    return "\n".join(lines) + "\n"
