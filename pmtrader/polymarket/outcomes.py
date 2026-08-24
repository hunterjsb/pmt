"""Validated updown outcomes — ground truth the replay harness (pmengine R4) needs.

Priority is strict, and the ranking is the whole design (SOURCE_RANK below):

  * **wallet** — Polymarket paid us (or paid us $0). The exchange's own money
    moving; nothing beats it.
  * **resolution** — the market's settled outcome off gamma. Also the
    EXCHANGE's answer, not ours: it is the number redemptions are paid on.
  * **chainlink** / **book** — OUR read of the settlement stream and of the
    market's terminal book. Inference, and only for windows we never traded.

The two bottom sources fill in windows we never traded, but only when they can
prove they were trustworthy at settlement time. That staleness guard exists
because a stale step-extension once mislabeled 9/37 windows and poisoned an
A/B: the corpus's last known round was well behind the window end, but a TWAP
still happily holds the last value flat out to `end` — silently wrong, not
obviously wrong. Windows the guard can't clear are dropped, never guessed.

WHY "resolution" IS ALLOWED TO GRADE THE W-L RECORD AND CHAINLINK IS NOT.
The standing rule is that the model never grades itself — a confidently wrong
final read (XRP basis, 2026-08-23) would otherwise book its own loss as a win,
so chainlink/book labels are refused for W-L no matter how sure they look.
Market resolution is not a read of ours at all: it is the exchange stating what
it pays redeems on, the same authority that produced the wallet rows. And it is
the ONLY evidence a LOSS can ever have — a losing position pays $0 and, when
nobody bothers to redeem worthless tokens, emits no wallet row of any kind. A
wallet-only W-L therefore books every win and can never book a silent loss,
which is not a conservative ledger, it is a rigged one (2026-08-23: three
resolved-lost windows sat "riding" for 13-25h hiding -$272.35).

All functions here are pure (no network, no disk) so the priority/staleness
logic is unit-testable with inline fixtures; `pmt crypto outcomes` in
cli_crypto_data.py does the I/O (tape files, wallet activity, gamma, corpus,
output file) and calls in.
"""

from __future__ import annotations

import bisect
import json
from collections.abc import Iterable
from pathlib import Path

from .chainlink import sorted_rounds, twap_over_window
from .updown_slugs import parse_updown_slug  # re-exported: this module's original home

OUTCOMES_PATH = Path.home() / ".pmt" / "corpus" / "outcomes.jsonl"

# Strongest first. See the module docstring for why the line between
# "resolution" and "chainlink" is where the W-L record is allowed to be graded.
SOURCE_RANK = {"wallet": 3, "resolution": 2, "chainlink": 1, "book": 0}

# Sources that may decide a WIN or a LOSS in the traded record: the exchange's
# payment, and the exchange's settlement. Our own stream/book reads never do.
TERMINAL_SOURCES = frozenset({"wallet", "resolution"})

STALE_S = 600  # no round within 10min before the query span -> corpus too stale to trust

# On-chain rounds land ~30s apart (deviation/heartbeat), so the corpus TWAP is a
# flat-hold interpolation. Measured against wallet + terminal-book witnesses
# (2026-08-23, 48h, n=344): labels under 1bp were WORSE than a coin flip (1/6 vs
# wallet), and one 15m window was wrong at 3.2bp. Below this floor the corpus
# refuses to grade — the terminal book or nothing takes over.
CK_NOISE_FLOOR_BP = 5.0


def source_rank(source: str | None) -> int:
    """Priority of an outcome source; an unknown source ranks below all of them
    so a corpus row written by some future path can never silently outrank one
    of these."""
    return SOURCE_RANK.get(source or "", -1)


def is_terminal_source(source: str | None) -> bool:
    """May a corpus row with this source decide a W or an L? Only the two the
    EXCHANGE authored (see module docstring) — never our own stream/book read."""
    return (source or "") in TERMINAL_SOURCES


def extract_updown_slugs(lines: Iterable[str]) -> set[str]:
    """Distinct updown slugs referenced in a tape file's lines. Tolerant of blank/bad JSON."""
    slugs: set[str] = set()
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            r = json.loads(line)
        except ValueError:
            continue
        slug = r.get("slug") if isinstance(r, dict) else None
        if slug and parse_updown_slug(slug):
            slugs.add(slug)
    return slugs


def window_universe(slugs: Iterable[str], since: float, now: float) -> list[dict]:
    """Windows eligible for outcome resolution: fully closed and within [since, now).

    end <= now - 30s (settlement needs a moment to land on-chain/at data-api)
    and start >= since. Sorted ascending by start.
    """
    out = []
    for slug in slugs:
        w = parse_updown_slug(slug)
        if not w or w["end"] > now - 30 or w["start"] < since:
            continue
        out.append({"slug": slug, **w})
    out.sort(key=lambda w: w["start"])
    return out


def ck_settlement_width_s(dur_s: int) -> int:
    """Settlement TWAP width: 60s at every duration.

    The old R1 convention assumed 30s at 5m closes; the measured record says
    otherwise — book-graded 283/284 at 60s vs 277/284 at 30s, and all 6
    windows where the widths disagree resolve the 60s way (analysis/
    settle_width.md). One width, every duration.
    """
    return 60


# ---------- (a) wallet truth — strict priority source ----------

def wallet_outcomes(activity_rows: list[dict]) -> dict[str, str]:
    """{slug: 'up'|'down'} from REDEEM rows in wallet activity.

    A paying REDEEM's `outcome` field names the winner directly. A $0 REDEEM
    means the side we held lost (Polymarket only lets you redeem the outcome
    token you actually hold), so the winner is the other side.
    """
    by_slug: dict[str, list[dict]] = {}
    for a in activity_rows:
        if a.get("type") != "REDEEM":
            continue
        slug = a.get("slug") or ""
        if not parse_updown_slug(slug):
            continue
        by_slug.setdefault(slug, []).append(a)

    out: dict[str, str] = {}
    for slug, rows in by_slug.items():
        paying = next((r for r in rows if (r.get("usdcSize") or 0.0) > 0.5), None)
        if paying is not None:
            outcome = (paying.get("outcome") or "").lower()
        else:
            # Every redeem paid $0 -> we held the loser, but ONLY a row with
            # real size names the side we held: a size-0 dust row can carry the
            # WINNER's label (see docs/LESSONS.md#L22). No sized row -> no
            # guess; the chainlink/gamma fallback grades it.
            sized = next((r for r in rows if (r.get("size") or 0.0) > 0.0), None)
            if sized is None:
                continue
            held = (sized.get("outcome") or "").lower()
            outcome = {"up": "down", "down": "up"}.get(held, "")
        if outcome in ("up", "down"):
            out[slug] = outcome
    return out


# ---------- (b) Chainlink corpus inference, with the staleness guard ----------

def chainlink_outcome(window: dict, rounds: list[dict],
                       ts_list: list[int] | None = None) -> tuple[str | None, str | None]:
    """(winner, drop_reason) for one window from the Chainlink corpus.

    winner is 'up'/'down' from the settlement-shaped TWAP (width per
    ck_settlement_width_s) at window end vs the same-width TWAP at window
    start. winner is None (with a reason) when the corpus can't be trusted
    for this window: no round within STALE_S before the query span, or the
    corpus's last known round predates the window end outright.

    `ts_list` is chainlink.sorted_rounds()'s second half, for a caller grading
    many windows off one symbol's corpus: passing it says `rounds` is ALREADY
    oldest-first and skips re-sorting it per window. Omit it and this sorts
    its own copy, which is what every one-window caller wants.
    """
    if not rounds:
        return None, "no corpus data"
    if ts_list is None:
        rounds, ts_list = sorted_rounds(rounds)
    w = ck_settlement_width_s(window["dur_s"])
    span_start = window["start"] - w

    if rounds[-1]["updated_at"] < window["end"]:
        return None, "stale: corpus's last round predates window end"
    idx = bisect.bisect_right(ts_list, span_start) - 1
    if idx < 0 or span_start - ts_list[idx] > STALE_S:
        return None, "stale: no round within 10min before query span"

    settlement = twap_over_window(rounds, ts_list, window["end"] - w, window["end"])
    reference = twap_over_window(rounds, ts_list, window["start"] - w, window["start"])
    if settlement is None or reference is None:
        return None, "stale: twap unavailable"  # belt-and-suspenders; guards above should prevent this
    margin_bp = abs(settlement - reference) / reference * 1e4
    if margin_bp < CK_NOISE_FLOOR_BP:
        return None, f"margin {margin_bp:.1f}bp inside corpus noise floor"
    return ("up" if settlement > reference else "down"), None


# ---------- (c) market resolution — the exchange's own settled answer ----------

# Settlement lands on gamma about a minute after a window ends (observed
# closedTime - endDate: 54-150s across the 2026-08-23 audit set). Past this
# a silent wallet is evidence about the REDEEM, never about the outcome.
RESOLUTION_GRACE_S = 300


def gamma_resolution(markets: list[dict]) -> dict:
    """{'resolved': bool, 'winner': 'up'|'down'|None} from a gamma
    `/markets?slug=...` response (a list; empty when the slug is unknown).

    outcomePrices only pins to "1"/"0" once UMA has actually settled —
    `closed` alone isn't enough, a market can close for trading well before
    resolution proposes/finalizes. Tolerant of the malformed-JSON shapes
    gamma has emitted elsewhere (see scanner._parse_prices).

    CALLERS MUST ASK GAMMA FOR CLOSED MARKETS. `/markets?slug=X` defaults to
    closed=false and answers `[]` for every settled window — which lands here
    as "not resolved" and is indistinguishable from a window still trading.
    That default is the whole 2026-08-23 hidden-loss bug; the one fetcher is
    cli_crypto_stats._gamma_resolution_cached and it pins the flag.
    """
    if not markets:
        return {"resolved": False, "winner": None}
    m = markets[0]
    try:
        outcomes = json.loads(m.get("outcomes") or "[]")
        prices = [float(p) for p in json.loads(m.get("outcomePrices") or "[]")]
    except (TypeError, ValueError):
        return {"resolved": False, "winner": None}
    if len(outcomes) != 2 or len(prices) != 2:
        return {"resolved": False, "winner": None}
    by_outcome = {str(o).lower(): p for o, p in zip(outcomes, prices)}
    up, down = by_outcome.get("up"), by_outcome.get("down")
    if up is None or down is None:
        return {"resolved": False, "winner": None}
    if up >= 0.99:
        return {"resolved": True, "winner": "up"}
    if down >= 0.99:
        return {"resolved": True, "winner": "down"}
    return {"resolved": False, "winner": None}


def grade_window(redeemed_usd: float, redeem_seen: bool, fired_side: str | None,
                  gamma: dict | None, now: float, end: float,
                  grace_s: float = RESOLUTION_GRACE_S) -> tuple[bool | None, bool]:
    """(won, estimated) for one window's wallet totals — worst case first.

    1. A paying redeem (>$0.5): WIN, ground truth from the wallet itself.
    2. A $0 redeem happened with no paying one: LOSS — still ground truth,
       Polymarket only lets you redeem the outcome token you actually hold
       (so a lone $0 redeem can't be dust beside a paying row elsewhere).
    3. No redeem event at all, still inside the settlement grace window:
       riding (None) — too soon to know either way.
    4. Past grace with no redeem: Polymarket doesn't reliably auto-redeem a
       slow-paying WIN, and a LOST position pays $0 and posts NO ROW AT ALL
       (2026-08-23 audit), so a silent wallet says nothing either way. The
       market's own resolution — `gamma`, already fetched by the caller, or
       None on a skipped/failed lookup — is what breaks the tie: resolved
       -> WIN/LOSS by whether our side matches the winner; not yet resolved
       -> riding (None); unreachable -> fall back to the old assume-LOSS
       heuristic, flagged `estimated` so the UI can mark it as such instead
       of presenting it as confirmed.

    Step 4 is the ONE place the W-L record is decided by something other than
    a wallet row, and it does not break the never-grade-yourself rule: the
    resolution is the exchange's, not the model's — it is the number redeems
    are paid on — and a loss can produce no other evidence. A chainlink or
    terminal-book label never reaches here (see module docstring).

    `fired_side` is the side we HELD. The tape's fire record names it, but the
    tape rotates and the wallet's own BUY rows outlive it — callers should
    fall back to those rather than lose a window to a missing fire.
    """
    if redeemed_usd > 0.5:
        return True, False
    if redeem_seen:
        return False, False
    if now < end + grace_s:
        return None, False
    if gamma is not None:
        if not gamma.get("resolved"):
            return None, False
        winner = gamma.get("winner")
        if winner and fired_side:
            return winner == fired_side, False
        return None, False  # resolved, but we can't tell which side we held
    return False, True


# Share counts come back from data-api with sub-share dust (a 100-share exit
# fills as 99.998625). Anything under this is not a position.
DUST_SHARES = 0.01


def exited_flat(buy_shares: float, sell_shares: float) -> bool:
    """Did the wallet sell (essentially) everything it bought?

    A window sold flat before settlement holds no outcome tokens, so it can
    never produce a redeem row and no resolution can pay it: its P&L is
    already fully realized as sold minus bought. Grading it off redeem
    silence rides it forever — btc-updown-5m-1787419200 sat "riding $44.72"
    for 26h having been closed out at $0.80 with +$35.28 booked.
    """
    return buy_shares > 0 and (buy_shares - sell_shares) <= DUST_SHARES


# ---------- (d) terminal book — last-resort source for UNFILLED windows ----------

BOOK_TERMINAL_S = 15   # only book samples this close to window end count as terminal
BOOK_PIN = 0.95        # winner bid must pin at least here, loser ask at most 1-here

def book_outcome(window: dict, book_records: list[dict]) -> tuple[str | None, str | None]:
    """(winner, drop_reason) from the market's own terminal book. Last-resort
    source: wallet-first grading leaves every window we never FILLED
    ungraded — which is exactly the missed-opportunity population the
    latency/miss studies need (2026-08-23).

    Only samples inside the final BOOK_TERMINAL_S of the window count — a
    tape that stopped mid-window (restart, blackout) must not grade, because
    a 0.96 book with minutes left is a forecast, not a settlement
    (docs/LESSONS.md: drop, never guess). Needs >= 2 agreeing pinned samples
    (winner bid >= BOOK_PIN, its opponent's ask <= 1-BOOK_PIN where quoted)
    and zero samples pinned the other way.
    """
    end = window["end"]
    term = [r for r in book_records
            if (r.get("t") or 0) >= end - BOOK_TERMINAL_S and (r.get("t") or 0) <= end + 120]
    if not term:
        return None, "no terminal book samples"
    up_w = dn_w = 0
    for r in term:
        ub, da = r.get("up_bid"), r.get("dn_ask")
        db, ua = r.get("dn_bid"), r.get("up_ask")
        if ub is not None and ub >= BOOK_PIN and (da is None or da <= 1 - BOOK_PIN):
            up_w += 1
        if db is not None and db >= BOOK_PIN and (ua is None or ua <= 1 - BOOK_PIN):
            dn_w += 1
    if up_w >= 2 and dn_w == 0:
        return "up", None
    if dn_w >= 2 and up_w == 0:
        return "down", None
    return None, "terminal book ambiguous"


# ---------- priority merge ----------

def build_outcomes(windows: list[dict], wallet: dict[str, str],
                    rounds_by_symbol: dict[str, list[dict]],
                    book_by_slug: dict[str, list[dict]] | None = None,
                    resolution_by_slug: dict[str, str] | None = None) -> tuple[list[dict], list[dict]]:
    """Resolve every window by strict priority: wallet, resolution, chainlink, book.

    `resolution_by_slug` is {slug: 'up'|'down'} the caller already read off
    gamma (empty/None when it skipped that lookup — this module stays pure).

    Returns (rows, dropped) — rows are {"slug","winner","source"} ready to
    merge into the outcomes corpus; dropped are {"slug","reason"} for
    windows no source could validate.
    """
    rows, dropped = [], []
    # Sorted ONCE per symbol. Every window of a symbol grades off that symbol's
    # whole corpus, so sorting inside the per-window call re-sorted the same
    # ~6k rounds once per window (0.9s over 1422 windows, and rising with both
    # the window count and the corpus).
    prepared = {sym: sorted_rounds(rs) for sym, rs in rounds_by_symbol.items()}
    for w in windows:
        slug = w["slug"]
        if slug in wallet:
            rows.append({"slug": slug, "winner": wallet[slug], "source": "wallet"})
            continue
        res = (resolution_by_slug or {}).get(slug)
        if res:
            rows.append({"slug": slug, "winner": res, "source": "resolution"})
            continue
        ck_rounds, ck_ts = prepared.get(w["symbol"]) or ([], [])
        winner, reason = chainlink_outcome(w, ck_rounds, ck_ts)
        if winner is not None:
            rows.append({"slug": slug, "winner": winner, "source": "chainlink"})
            continue
        bwinner, breason = book_outcome(w, (book_by_slug or {}).get(slug) or [])
        if bwinner is not None:
            rows.append({"slug": slug, "winner": bwinner, "source": "book"})
        else:
            dropped.append({"slug": slug, "reason": f"{reason}; {breason}"})
    return rows, dropped


# ---------- outcomes corpus (append + dedupe by slug) ----------

def load_outcomes(path: Path = OUTCOMES_PATH) -> dict[str, dict]:
    """{slug: row} currently on disk."""
    out: dict[str, dict] = {}
    if not path.exists():
        return out
    with open(path) as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                r = json.loads(line)
            except ValueError:
                continue
            if r.get("slug"):
                out[r["slug"]] = r
    return out


def merge_outcomes(existing: dict[str, dict], new_rows: list[dict]) -> tuple[dict[str, dict], int, int]:
    """Merge new_rows into existing by slug, strictly by SOURCE_RANK: a row
    only overwrites one it OUTRANKS. So wallet upgrades everything and is
    never downgraded, resolution upgrades a chainlink/book guess, and a
    same-source rewrite is refused (first write wins — the walk order can't
    change the corpus).

    Returns (merged, n_added, n_upgraded).
    """
    merged = dict(existing)
    added = upgraded = 0
    for r in new_rows:
        prev = merged.get(r["slug"])
        if prev is None:
            merged[r["slug"]] = r
            added += 1
        elif source_rank(r["source"]) > source_rank(prev.get("source")):
            merged[r["slug"]] = r
            upgraded += 1
    return merged, added, upgraded


def write_outcomes(merged: dict[str, dict], path: Path = OUTCOMES_PATH) -> None:
    """Full rewrite, chronological by window start — the only way to dedupe a JSONL file by key."""
    path.parent.mkdir(parents=True, exist_ok=True)

    def sort_key(slug: str) -> tuple:
        w = parse_updown_slug(slug)
        return (0, w["start"]) if w else (1, slug)

    with open(path, "w") as fh:
        for slug in sorted(merged, key=sort_key):
            fh.write(json.dumps(merged[slug]) + "\n")
