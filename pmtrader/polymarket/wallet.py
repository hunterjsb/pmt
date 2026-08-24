"""Polymarket data-api wallet-activity client — shared by every updown
consumer that needs the trading wallet's trade/redeem history: the fleet
scoreboard, `pmt crypto activity`/`window`, and outcomes/shadow's
wallet-first resolver. One copy of the pagination and address resolution,
not the three that used to exist.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

import requests

from . import errlog, hosts

ACTIVITY_URL = f"{hosts.DATA}/activity"

# The on-disk dump the fixture freezer grades money against. Its only writer
# used to be `analysis/latency_report.py --refresh`, which left this repo on
# 2026-08-23 with the rest of the research record — taking the sole refresh
# path with it and freezing the dump at 08:41Z. Seven fixtures were frozen
# afterwards and every one recorded $0 buy/$0 redeem/$0 pnl on a window that
# really traded. The writer belongs next to the walk it depends on, in the
# repo that has pmtrader and the .env on hand.
ACTIVITY_DUMP = Path.home() / ".pmt" / "corpus" / "activity.jsonl"
PAGE_SIZE = 500
# Deliberately < PAGE_SIZE: the overlap re-reads the seam that offset
# pagination over a LIVE feed keeps shifting, absorbing up to 100 fresh
# inserts per fetch; row_key dedupe collapses the rest. docs/LESSONS.md#L25.
PAGE_STEP = 400

# Rows older than this are IMMUTABLE AS A CLASS: every measured in-place
# mutation is a REDEEM finalizing (docs/LESSONS.md#L25's autopsy), and
# finalization completes in minutes — 48h is ~50x padding. This is the
# boundary that keeps the all-time ledger reachable forever: data-api caps
# `offset` at 5000, so a full offset-walk dies for good near ~5,300 rows
# (~this week at current fill rates). The splice below re-walks only the
# mutable window and takes the immutable past from the on-disk dump — a
# clean TIMESTAMP partition, never an identity-keyed cache, which is the
# second-code-path trap the autopsy warns about (draft/final duplicates
# cannot arise when each row comes from exactly one side of a time cut).
IMMUTABLE_AFTER_S = 48 * 3600.0


def row_key(a: dict) -> tuple:
    """Stable identity for one activity row (no server-side id exists).
    Genuinely identical $0-redeem dust rows collapse too — zero-value, so
    the aggregate is unaffected."""
    return (
        a.get("transactionHash") or "",
        a.get("type") or "",
        a.get("asset") or a.get("slug") or "",
        a.get("side") or "",
        a.get("outcome") or "",
        a.get("size") or 0,
        a.get("usdcSize") or 0,
        a.get("timestamp") or 0,
    )


def funder_address() -> str:
    """PM_FUNDER_ADDRESS from the environment; raises if unset.

    An unset addr used to let pagination fall through silently and report
    a clean "0W-0L" — indistinguishable from a genuinely empty trading
    history. Every wallet-activity consumer must fail loud instead.
    """
    addr = os.environ.get("PM_FUNDER_ADDRESS", "")
    if not addr:
        raise ValueError("PM_FUNDER_ADDRESS not set")
    return addr


# Other engines' wallets, as `label=0xaddr` pairs. The fleet runs one engine
# per ACCOUNT, not one account with several series on it: the EU box
# collateralises through its own deposit wallet, so there is no view of its
# ledger from this box's rows and no way to reach it except by naming it.
# Read from the environment rather than a config file so the one place a
# private address is written stays the operator's own .env.
FLEET_WALLETS_ENV = "PMT_FLEET_WALLETS"
# The orchestrator's name for THIS box (pmtrader/orchestrator/heartbeat.py),
# reused so the dashboard's P&L rows and a fleet heartbeat call it one thing.
NODE_ENV = "PMT_FLEET_NODE"


def node_label(env: dict | None = None) -> str:
    """What to call this box on a fleet-wide view. Never raises: a dashboard
    row's label is not worth failing a render over."""
    env = os.environ if env is None else env
    return (env.get(NODE_ENV) or "").strip() or "local"


def peer_wallets(env: dict | None = None) -> list[tuple[str, str]]:
    """[(label, address)] for OTHER engines' wallets — never this box's own.

    Malformed entries are DROPPED rather than raised on: this feeds a display
    panel that must degrade to the wallets it can read, and a typo'd pair
    cannot be allowed to take the dashboard down. The local funder is filtered
    out by address, so listing it changes nothing — a peer row duplicating the
    local ledger would double the fleet line.
    """
    env = os.environ if env is None else env
    mine = (env.get("PM_FUNDER_ADDRESS") or "").strip().lower()
    out: list[tuple[str, str]] = []
    seen = {mine} if mine else set()
    for item in (env.get(FLEET_WALLETS_ENV) or "").split(","):
        label, _, addr = item.partition("=")
        label, addr = label.strip(), addr.strip()
        if not label or not addr.lower().startswith("0x") or addr.lower() in seen:
            continue
        seen.add(addr.lower())
        out.append((label, addr))
    return out


class ActivityPageError(RuntimeError):
    """data-api answered /activity with something that is not a page of rows.

    A distinct type because the caller's options differ from a transport
    failure's: this is the server declining, in JSON, with a reason worth
    printing verbatim.
    """


def fetch_activity_page(addr: str, offset: int, *, limit: int = PAGE_SIZE) -> list[dict]:
    """One page of /activity rows, newest first.

    RAISES ActivityPageError on any non-list body rather than handing it back.
    This used to be `.json() or []`, which is only correct while data-api is
    happy: it answers a refusal with HTTP 400 and a JSON OBJECT —

        {"error": "max historical activity offset of 5000 exceeded"}

    — and a dict is truthy, so the walk fell straight into `for a in page`,
    iterated the dict's KEYS, and handed row_key() the string "error". That is
    `AttributeError: 'str' object has no attribute 'get'`, thrown from the
    middle of the wallet walk, and every consumer of the walk turns it into
    four words: the watch header's `scoreboard: AttributeError`. Naming the
    status and the body is the entire difference between a mystery and a
    one-line diagnosis.

    Worth knowing about the specific refusal above: the walk pages with
    PAGE_STEP=400 and data-api caps `offset` at 5000, so a wallet with more
    than ~5300 activity rows can no longer be walked to genesis at all. It is
    a deadline, not a possibility — see fetch_wallet_activity.
    """
    r = requests.get(
        ACTIVITY_URL,
        params={"user": addr, "limit": limit, "offset": offset},
        headers=hosts.UA, timeout=8,
    )
    try:
        body = r.json()
    except ValueError:
        raise ActivityPageError(
            f"/activity offset={offset} answered HTTP {r.status_code} with a "
            f"non-JSON body: {r.text[:200]!r}") from None
    if body is None:
        return []
    if not isinstance(body, list):
        detail = body.get("error") if isinstance(body, dict) else None
        raise ActivityPageError(
            f"/activity offset={offset} answered HTTP {r.status_code} with "
            f"{type(body).__name__}, not a page: "
            f"{detail if detail else str(body)[:200]}")
    # A non-dict row can only be dropped or raised on, and dropping one is a
    # silent hole in the ledger — the class of bug this module's autopsy
    # comment is about. Raise, and let the operator see the shape.
    for a in body:
        if not isinstance(a, dict):
            raise ActivityPageError(
                f"/activity offset={offset} returned a {type(a).__name__} row, "
                f"not an object: {str(a)[:120]!r}")
    return body


# ---------------------------------------------------------------------------
# READ THIS BEFORE YOU CACHE THE WALLET FEED. Someone has already tried.
#
# The full re-walk below looks wasteful and IS the cheap option. An
# accumulating in-memory ledger with an incremental head-refresh lived here
# until 2026-08-23; it cost five patches and was deleted. What it ran into,
# in the order it found them:
#
#   * data-api rows MUTATE IN PLACE. A REDEEM indexes with draft amounts and
#     the payout finalizes in stages; a partial-fill aggregate grows; a
#     reindex rewrites a row outright. Any identity that includes the amounts
#     makes the finalized row look NEW, so a cache ends up holding the draft
#     AND the final. Measured live: the first 30 drift purges were every one
#     of them a REDEEM, $1,227.83 double-counted, showing up as an optimistic
#     `watch` P&L that a restart mysteriously "fixed".
#   * PAGES SEAM-SHIFT. This is offset pagination over a LIVE feed: inserts
#     at the head push rows down between requests, so page N's tail reappears
#     at the top of page N+1. A page can therefore open with rows you already
#     hold and still carry new ones below them — "stop at the first known
#     row" is wrong, and even "stop at the first fully-known page" only
#     bounds the damage.
#   * MUTATION HAPPENS MID-WALK. A redeem can finalize between two of your
#     own page fetches, putting its draft on one page and its final on the
#     next one's seam overlap. There is no walk short enough to be atomic.
#
# The invariant that survives all three: this feed is authoritative and
# mutable, so the only correct read is a fresh one. The history is a handful
# of pages and every caller runs it off a worker thread or a one-shot report.
# If pagination cost ever genuinely hurts, the fix is a FLOOR at strategy
# genesis — a smaller honest walk, never a second code path holding old rows.
# ---------------------------------------------------------------------------


def _oldest_ts(page: list[dict]) -> float:
    """The last row's timestamp for the walk's early stop, or +inf when it
    cannot be read.

    +inf, never 0: this number decides whether to STOP paginating, so an
    unreadable timestamp must mean "keep walking" — one extra request — and
    never "we're done", which would silently shorten the ledger. It used to be
    a bare `page[-1]["timestamp"]`, the one unguarded index in a module where
    every other field read is `.get(...) or 0`: a row missing the key raised
    KeyError and a `"timestamp": null` raised TypeError, both from inside the
    wallet walk and both reaching the operator as four words in a header cell.
    """
    ts = page[-1].get("timestamp")
    if isinstance(ts, bool) or not isinstance(ts, (int, float, str)):
        errlog.note("wallet.fetch_wallet_activity.oldest_ts",
                    TypeError(f"page tail timestamp is {type(ts).__name__}"))
        return float("inf")
    try:
        return float(ts)
    except ValueError as e:
        errlog.note("wallet.fetch_wallet_activity.oldest_ts", e, ts=ts)
        return float("inf")


def fetch_wallet_activity(addr: str, floor: float = 0.0) -> list[dict]:
    """Every activity row back to `floor` (paginate until a page runs short
    of PAGE_SIZE or its oldest row predates floor).

    floor=0 walks the full history — the early-stop condition below only
    ever fires on a real (positive) timestamp, so it never short-circuits.

    A DEADLINE lives in that sentence. data-api caps `offset` at 5000 and
    refuses past it (see fetch_activity_page), so "the full history" stops
    being reachable once the wallet holds more than ~5300 activity rows: the
    walk raises ActivityPageError instead of quietly returning a short ledger.
    Raising is the right failure — a truncated all-time walk would print a
    confident wrong P&L — but it is a failure, and the fix when it arrives is
    a FLOOR at strategy genesis (the option the autopsy above already names),
    not a wider offset.
    """
    rows: list[dict] = []
    seen: set = set()
    offset = 0
    while True:
        page = fetch_activity_page(addr, offset)
        for a in page:
            k = row_key(a)
            if k not in seen:
                seen.add(k)
                rows.append(a)
        if len(page) < PAGE_SIZE or (page and _oldest_ts(page) < floor):
            break
        offset += PAGE_STEP
        if offset >= 4000:
            # The 5000 cap is a deadline, not a possibility. This fires while
            # the walk still WORKS, so the splice migration happens on a
            # warning instead of an outage.
            errlog.note("wallet.fetch_wallet_activity.offset_cap",
                        RuntimeError(f"offset {offset} nearing data-api cap 5000"))
    return rows


def _dump_rows(path: Path | str = ACTIVITY_DUMP) -> list[dict]:
    """Every parseable row in the on-disk dump, oldest-first. [] if absent."""
    p = Path(path)
    if not p.exists():
        return []
    rows: list[dict] = []
    with p.open() as f:
        for line in f:
            try:
                r = json.loads(line)
            except ValueError as e:
                errlog.note("wallet._dump_rows", e)
                continue
            if isinstance(r, dict):
                rows.append(r)
    return rows


def activity_since(addr: str, floor: float = 0.0, *, now: float | None = None,
                   path: Path | str = ACTIVITY_DUMP) -> list[dict]:
    """Every activity row back to `floor` — the splice: a fresh walk of the
    MUTABLE window (now − IMMUTABLE_AFTER_S onward) plus the immutable past
    read from the dump. THE accessor for deep floors; a shallow floor inside
    the mutable window degenerates to a plain fresh walk, unchanged.

    The partition is by timestamp, exclusive on the dump side: a row belongs
    to the walk iff ts >= cut, to the dump iff floor <= ts < cut. No row can
    appear on both sides, so no identity key is ever compared — the trap the
    module autopsy documents cannot arise.

    Falls back to the full offset-walk (today's behavior, with today's
    deadline) when the dump is missing or does not COVER the cut — a splice
    against a stale dump would silently drop the gap between its coverage
    and the cut, which is exactly the confident-wrong-ledger failure this
    module refuses everywhere else.
    """
    t = time.time() if now is None else now
    cut = t - IMMUTABLE_AFTER_S
    if floor >= cut:
        return fetch_wallet_activity(addr, floor)
    dump = _dump_rows(path)
    coverage = max((float(r.get("timestamp") or 0) for r in dump), default=0.0)
    if coverage < cut:
        errlog.note("wallet.activity_since",
                    RuntimeError(f"dump coverage {coverage:.0f} < cut {cut:.0f} "
                                 "— full walk fallback (refresh the dump)"))
        return fetch_wallet_activity(addr, floor)
    fresh = [r for r in fetch_wallet_activity(addr, cut)
             if float(r.get("timestamp") or 0) >= cut]
    old = [r for r in dump
           if floor <= float(r.get("timestamp") or 0) < cut]
    merged = old + fresh
    merged.sort(key=lambda r: r.get("timestamp") or 0)
    return merged


def refresh_activity_dump(path: Path | str = ACTIVITY_DUMP,
                          addr: str | None = None) -> int:
    """Re-walk the wallet activity feed into the on-disk dump. Network read only.

    A full rewrite, never an append: the feed MUTATES IN PLACE (see the comment
    above `fetch_wallet_activity`), so the only correct dump is a whole fresh
    one. Written to a temp file and renamed, so a reader mid-refresh sees the
    old dump rather than a truncated one. Returns the row count written.
    """
    rows = activity_since(addr or funder_address(), 0.0, path=path)
    rows.sort(key=lambda r: r.get("timestamp") or 0)
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    with tmp.open("w") as f:
        for r in rows:
            f.write(json.dumps(r, sort_keys=True) + "\n")
    tmp.replace(path)
    return len(rows)


def activity_dump_coverage(path: Path | str = ACTIVITY_DUMP) -> float:
    """Newest row timestamp in the dump; 0.0 if it is missing or empty.

    How far forward the dump can answer at all. Anything asking it about a
    window that closed after this is asking a question the file cannot hold.
    """
    newest = 0.0
    try:
        with Path(path).open() as f:
            for line in f:
                try:
                    ts = float(json.loads(line).get("timestamp") or 0)
                except (ValueError, TypeError) as e:
                    # Same stake as fixtures.account_window: this watermark
                    # gates whether a fixture may be frozen. Undercounting it
                    # is silent and expensive.
                    errlog.note("wallet.activity_dump_coverage.bad_row", e,
                                path=str(path), line=line[:200])
                    continue
                newest = max(newest, ts)
    except OSError:
        return 0.0
    return newest
