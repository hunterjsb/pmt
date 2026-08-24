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
fixture` in cli_crypto_fixture.py does the I/O and calls in.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from collections.abc import Iterable

from . import errlog
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

# --- RTDS recorder slice (feed="rtds" windows) ---------------------------
# The klines' counterpart for a stream-fed arm. Unlike klines this can NEVER
# be re-fetched: the settlement stream serves no history, so the slice in the
# fixture is the only surviving record of that window's market data.

# Must match updown_rtds.rs::HISTORY_WARMUP_S (CLOSES_CAP minutes). Replay
# warms the hub from exactly this far back, and a short slice silently
# reconstructs a different rho and slow sigma than the one that traded.
RTDS_LOOKBACK_S = 7200
# Keep every 1 Hz spot print from this far before the window. Inside it the
# arm is evaluating, and `spot_ts` is receive-time freshness: thin the prints
# here and the replay gates on a staleness that never happened.
RTDS_DENSE_LEAD_S = 300
# Past the close: the final settlement mark prints AT the close, and its
# in-tolerance substitute up to MARK_TOL_S after.
RTDS_TAIL_S = 120
# How late a TWAP print may be and still bank as that minute's mark
# (updown_rtds.rs::MARK_TOL_S). Prints outside it can never become a mark,
# so they are weight with no effect on any number replay reads.
RTDS_MARK_TOL_S = 2

RTDS_TOPIC_SPOT = "crypto_prices_chainlink"
# The recorder row minus `window_s`, which nothing in the shaping path reads
# (the topic already names the width). Same principle as BOOK_KEYS: a fixture
# carries the decision's inputs, not the recorder's whole row.
RTDS_KEYS = ("t_recv", "topic", "symbol", "ts", "value", "full_accuracy_value")

# Serde field order in fixtures.rs::Fixture. Matching it means the first
# `--bless` rewrites values, not the whole file.
FIXTURE_KEYS = (
    "fixture_version", "slug", "mode", "teaches", "lessons_ref", "era",
    "window_utc", "params", "params_provenance", "outcome", "evals", "book",
    "klines", "rtds", "invariants", "expect", "provenance",
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


def rtds_symbol(binance_symbol: str) -> str | None:
    """`"XRPUSDT"` -> `"xrp/usd"`. Mirror of updown_rtds.rs::rtds_symbol."""
    s = binance_symbol.lower()
    for quote in ("usdt", "usdc", "busd", "usd"):
        if s.endswith(quote):
            base = s[: -len(quote)]
            return f"{base}/usd" if base else None
    return None


def rtds_slice(rows: Iterable[dict], symbol: str, start: int,
               end: int) -> tuple[list[dict], tuple[float, float] | None]:
    """(rows, coverage) — the recorder slice a stream-fed window replays on.

    `coverage` is the (first, last) receive time actually present, or None
    when the symbol has no rows at all. The caller refuses on it; this
    function only reports, so the rule stays testable without a corpus.

    Thinning is the whole design problem: a day's stream for one symbol is
    ~65k rows at 1 Hz across three topics, and a fixture is a file a human
    reads a diff of. What survives is exactly what changes a number replay
    reads, decided by the live router's own rules:

      * **spot, inside the window** — every print. `spot_ts` is receive-time
        freshness and the arm evaluates on it continuously.
      * **spot, before the window** — one print a minute, chosen by the
        router's OWN banking rule (first arrival whose minute is new), so
        the reconstructed `closes` vector is identical to the live one.
      * **TWAP marks** — only prints within MARK_TOL_S of a minute
        boundary. Nothing else can ever become a mark; the other 57 prints
        a minute are read by nobody.
      * **both widths** — a 5m arm reads the 30s TWAP and a 15m arm the
        60s one, and keeping both means the slice does not depend on having
        got the arm's settlement width right when it was cut.
    """
    lo, hi = start - RTDS_LOOKBACK_S, end + RTDS_TAIL_S
    dense_from = start - RTDS_DENSE_LEAD_S

    mine = [r for r in rows if r.get("symbol") == symbol
            and isinstance(r.get("ts"), (int, float))
            and isinstance(r.get("t_recv"), (int, float))]
    if not mine:
        return [], None
    coverage = (min(r["t_recv"] for r in mine), max(r["t_recv"] for r in mine))

    # Receive order, because that is the order the router sees.
    mine.sort(key=lambda r: r["t_recv"])
    kept: list[dict] = []
    last_close_min = -1
    for r in mine:
        ts_s = int(r["ts"]) // 1000
        if not (lo <= ts_s < hi):
            continue
        if r.get("topic") == RTDS_TOPIC_SPOT:
            if ts_s >= dense_from:
                kept.append(r)
                continue
            minute = ts_s // 60 * 60
            if minute <= last_close_min:
                continue
            last_close_min = minute
            kept.append(r)
        elif ts_s - ts_s // 60 * 60 <= RTDS_MARK_TOL_S:
            kept.append(r)
    return [{k: r[k] for k in RTDS_KEYS if k in r} for r in kept], coverage


# ---------- wallet truth ----------

def wallet_accounting(activity_rows: Iterable[dict], slug: str,
                      window_end: float | None = None) -> dict:
    """What the money did on one window, from wallet activity rows.

    Mirrors cli_crypto_stats.score_activity's aggregation over the same rows. The
    `winner` is NOT derived here — it comes from the graded outcomes corpus,
    which already applies L22/L23's dust-redeem rules; this only accounts.

    Pass `window_end` to make a STALE DUMP fatal instead of silent. Zero matching
    rows has two causes that look identical in the output — "this window was
    never traded" and "the dump stops before this window ever happened" — and
    for months the second one was written into fixtures as $0 buy / $0 redeem /
    $0 pnl beside `source: "wallet"`, a provably impossible pair (a wallet grade
    requires a redeem). With `window_end` set, a dump whose newest row predates
    the window's close is refused by name; without it the old permissive
    behaviour is unchanged, because `pmt crypto window` and the scoreboard
    legitimately ask about windows a deliberately-floored walk does not reach.
    """
    acct = {"buy": 0.0, "buy_shares": 0.0, "buy_side": None, "sell": 0.0,
            "redeem": 0.0, "redeem_seen": False, "redeem_outcome": None, "pnl": 0.0}
    sides: dict[str, float] = {}
    matched = 0
    newest = 0.0
    for a in activity_rows:
        try:
            newest = max(newest, float(a.get("timestamp") or 0))
        except (TypeError, ValueError) as e:
            # `newest` is the dump-coverage check that decides whether this
            # fixture may be frozen at all. A row whose timestamp won't parse
            # silently lowers it, and a too-low coverage number is what stamps
            # $0 buy/redeem/pnl onto a window that really traded.
            errlog.note("fixtures.wallet_accounting.timestamp", e,
                        slug=slug, ts=a.get("timestamp"))
        if a.get("slug") != slug:
            continue
        matched += 1
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
    if window_end is not None and matched == 0 and newest < window_end:
        raise FixtureError(
            f"{slug}: the wallet activity dump stops at "
            f"{_utc(newest)} but this window closes at {_utc(window_end)}, so it "
            f"cannot say what the money did — every number would be a zero that "
            f"means 'not recorded', not 'not traded'. Refresh it first:\n"
            f"    pmt crypto activity --refresh"
        )
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

    # The chase, if this window's tape is new enough to have recorded it.
    # `limit - ask` is what a clip actually spent above the book, so the
    # largest one is a tight lower bound on the budget — the same reasoning
    # clip_usdc uses, and the same caveat: a window where no clip ever hit
    # the cap reads back a smaller budget than was armed. Windows cut before
    # the `limit` field shipped have no evidence at all and stay at 0, which
    # replays every clip at the ask it recorded.
    chases = [r["limit"] - r["ask"] for r in tape_recs
              if r.get("ev") == "fire"
              and isinstance(r.get("limit"), (int, float))
              and isinstance(r.get("ask"), (int, float))]
    if any(c > 1e-9 for c in chases):
        p["pay_up_max"] = round(max(chases), 6)
        prov["pay_up_max"] = "tape: largest recorded limit-over-ask chase (a lower bound on the armed budget)"
    else:
        p["pay_up_max"] = 0.0
        prov["pay_up_max"] = (
            "tape: no clip chased above its ask"
            if chases
            else "NOT RECOVERABLE — this slice predates the fire record's `limit` field; 0 replays at the recorded ask"
        )

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
                  expect: dict | None = None,
                  rtds: list[dict] | None = None) -> dict:
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
        "rtds": list(rtds or []),
        "invariants": list(invariants),
        "expect": expect,
        "provenance": provenance,
    }


def _utc(epoch: float) -> str:
    import datetime as _dt
    return _dt.datetime.fromtimestamp(epoch, _dt.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def replace_top_level_block(text: str, key: str, value: object) -> str:
    """Swap one top-level key's value in rendered fixture text, byte-for-byte
    everywhere else. Raises KeyError if the key is not present.

    Re-rendering a whole fixture from Python cannot be byte-stable against a
    file the Rust `--bless` wrote: the two disagree on key order AND on float
    formatting (Rust `0.00001807603856551765` vs Python `1.807603856551765e-05`
    — same double, different text). Both are value-preserving and both are pure
    noise in a data repo's diff. An edit that only needs to move one block
    should move one block.
    """
    lines = text.split("\n")
    # Two renderers are in the wild: render_fixture's one-space top level, and
    # a plain json.dumps(indent=2) two-space one (the 17:15Z cohort). Take the
    # indent from the file instead of assuming either.
    indent = ""
    for ln in lines[1:]:
        if ln.lstrip().startswith('"'):
            indent = ln[:len(ln) - len(ln.lstrip())]
            break
    open_line = f'{indent}{json.dumps(key)}: '
    start = next((i for i, ln in enumerate(lines) if ln.startswith(open_line)), None)
    if start is None:
        raise KeyError(key)
    # Nested content is indented deeper, so the block ends at the next line
    # that opens another top-level key or closes the object.
    end = start + 1
    while end < len(lines) and not (
            lines[end].startswith(f'{indent}"') or lines[end] == "}"):
        end += 1
    trailing = "," if lines[end - 1].rstrip().endswith(",") else ""
    body = json.dumps(value, indent=len(indent) or 2).replace("\n", "\n" + indent)
    return "\n".join(lines[:start] + [f"{open_line}{body}{trailing}"] + lines[end:])


def render_fixture(obj: dict, order: Iterable[str] | None = None) -> str:
    """Mirror of fixtures.rs::render_fixture — top-level keys pretty, record
    arrays one per line.

    Same renderer on both sides so `--bless` (which rewrites the file from
    Rust) produces a diff of the numbers that moved, not of the whole file.

    KEY ORDER IS NOT ACTUALLY SHARED, though. This renderer emits FIXTURE_KEYS
    order; the Rust bless emits its serde struct order, which lands
    alphabetical. A fresh freeze is written here and then immediately rewritten
    by `--bless`, so every COMMITTED fixture is in the Rust order and rendering
    one back through this function reorders the whole file — 222 lines of churn
    on a one-field edit. Pass `order` (e.g. `list(loaded)`) to keep the order
    the file already had, which is what any edit-in-place path wants.
    """
    if order is not None:
        order = list(order)
        keys = [k for k in order if k in obj] + [k for k in obj if k not in order]
    else:
        keys = ([k for k in FIXTURE_KEYS if k in obj]
                + [k for k in obj if k not in FIXTURE_KEYS])
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
