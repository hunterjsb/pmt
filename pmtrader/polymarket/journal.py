"""Trade journal — the notable-window log, in a human voice.

The tapes and the scoreboard answer "what are the numbers". Nothing in the
toolkit answered "what happened today" in a form the operator reads back a
week later and remembers the session from. This is that file: one terse
timestamped line per notable event, appended to ~/.pmt/journal/journal.md — a
PRIVATE location, never the repo, because it is a running record of a real
book.

Everything here is PURE detection over inputs the caller already read (graded
windows from cli_crypto_stats.score_activity, tape records from
polymarket.tape, the shadow ledger's own episode pipeline, arms-state).
`pmt crypto journal` in cli_crypto_data.py does the I/O. Nothing below recomputes a grade, a streak, an episode or a clip size:
every class is built out of the module that already owns that question, so a
journal line cannot disagree with the report it was written beside.

Six classes of notable, and why each earns a line:

  day extremes    the biggest win and the biggest loss of a local day — the
                  two windows that actually moved the book.
  latch saves     a `latched` brake that refused a side which then LOST.
                  The only class here where the notable thing is a trade that
                  did not happen; priced through polymarket.shadow.
  firsts          the first time a flag put capital somewhere new — a new
                  symbol, the rtds feed, a resting maker bid, a maker fill.
  streaks         25 / 50 / then every 50 consecutive wins. With this payoff
                  shape (+2-8% up, -100% down) the run between losses is the
                  operator's real pulse.
  scale changes   size/clip moved since the last run. A sizing decision made
                  at 02:00 is one nobody remembers making by Thursday.

IDEMPOTENCY is a high-water mark PLUS a set of emitted event keys. The HWM
alone is not enough — a `--since` backfill deliberately walks back behind it,
and redeem rows post late — so the key set is what actually makes a re-run a
no-op. The HWM only bounds how much history a routine run re-offers.
"""

from __future__ import annotations

import json
import re
import time
from pathlib import Path

from . import effectiveness, shadow, tape, updown_slugs

JOURNAL_DIR = Path.home() / ".pmt" / "journal"
JOURNAL_PATH = JOURNAL_DIR / "journal.md"
STATE_PATH = JOURNAL_DIR / "state.json"

HEADER = "# pmt trade journal\n"

# Keys retained in the state file. At a handful of notable events a day this
# is years of history, and it bounds a file the operator never prunes.
SEEN_CAP = 2000

# Every run re-offers this much history behind the high-water mark. Redeem
# rows post minutes-to-hours after a window closes, so an event's true
# timestamp can land BEHIND a HWM that has already moved past it; the key set
# absorbs the repeats this causes.
LOOKBACK_SLACK_S = 6 * 3600

# First run with no HWM: journal the last day, not the whole book. `--since 0`
# is the deliberate way to ask for the full backfill.
FIRST_RUN_LOOKBACK_S = 24 * 3600

# A window whose P&L is smaller than this is not the story of anybody's day.
NOTABLE_PNL_USD = 0.50

# A latch save smaller than ONE default clip is sub-clip noise: the smallest
# stake a single fire could have put at risk. The gate refuses something on
# most windows it watches; a journal that logged every one of those would bury
# the day it actually mattered.
NOTABLE_SAVE_USD = shadow.DEFAULT_CLIP_USDC


# ---------- small shared formatting ----------

def _money(v: float) -> str:
    return f"{'+' if v >= 0 else '-'}${abs(v):,.2f}"


def _series(slug: str) -> str:
    """'btc 5m' for a window, the raw slug for anything unparseable."""
    parsed = updown_slugs.parse(slug)
    return parsed[4] if parsed else slug


def _stamp(t: float) -> str:
    return time.strftime("%H:%M", time.localtime(t))


def _day(t: float) -> str:
    return time.strftime("%Y-%m-%d", time.localtime(t))


def _ev(t: float, key: str, line: str, patch: dict | None = None) -> dict:
    """One journal event. `patch` folds into the state only if the event is
    actually written — a floor-filtered event must not move any memory."""
    return {"t": float(t), "key": key, "line": line, "state": patch or {}}


# ---------- detectors (pure) ----------

def day_extremes(windows: list[dict], state: dict) -> list[dict]:
    """Biggest win and biggest loss of each local day.

    Keyed by (day, slug) rather than day alone so a day whose best is later
    beaten can say so — but only when it IS beaten: state remembers the figure
    already journaled, so a steady day writes exactly one line and a re-run
    mid-day writes none.
    """
    best_seen = state.get("day_best") or {}
    worst_seen = state.get("day_worst") or {}
    best: dict[str, dict] = {}
    worst: dict[str, dict] = {}
    for w in windows:
        pnl = float(w.get("pnl") or 0.0)
        if abs(pnl) < NOTABLE_PNL_USD:
            continue
        day = _day(float(w.get("end_ts") or 0.0))
        if pnl > 0 and pnl > float(best.get(day, {}).get("pnl", 0.0)):
            best[day] = w
        if pnl < 0 and pnl < float(worst.get(day, {}).get("pnl", 0.0)):
            worst[day] = w

    out = []
    for day, w in best.items():
        pnl, slug = float(w["pnl"]), w["slug"]
        if pnl <= float(best_seen.get(day, 0.0)):
            continue
        est = " (est)" if w.get("est") else ""
        out.append(_ev(w["end_ts"], f"best:{day}:{slug}",
                        f"{_series(slug)} best of the day {_money(pnl)} — "
                        f"${float(w.get('notional') or 0.0):,.0f} committed{est}",
                        {"day_best": {day: pnl}}))
    for day, w in worst.items():
        pnl, slug = float(w["pnl"]), w["slug"]
        if pnl >= float(worst_seen.get(day, 0.0)):
            continue
        notional = float(w.get("notional") or 0.0)
        est = " (est)" if w.get("est") else ""
        # A full forfeit reads differently from a scratch — this payoff shape
        # loses the whole stake, and the line should say so in words.
        tail = (f"the whole ${notional:,.0f} went" if notional and pnl <= -notional * 0.99
                else f"${notional:,.0f} committed")
        out.append(_ev(w["end_ts"], f"worst:{day}:{slug}",
                        f"{_series(slug)} worst of the day {_money(pnl)} — {tail}{est}",
                        {"day_worst": {day: pnl}}))
    return out


def latch_saves(tape_lines: list[str], winners: dict[str, str],
                fires: list[dict], graded: dict[str, dict]) -> list[dict]:
    """`latched` brakes that refused a side which then LOST — money not spent.

    Runs the shadow ledger's own pipeline over just the latched ticks rather
    than a private copy of it: same episode collapsing, same clip inference
    from the window's real fires, same hindsight pricing. A save here and a
    `latched` row in `pmt crypto stats --gates` are the same arithmetic.

    `graded` is {slug: window} for windows the wallet DID grade, so a latch
    that held while a position was already riding can report what the window
    finally closed at.
    """
    ticks = [tk for tk in shadow.iter_ticks(tape_lines)
             if tk["category"] == shadow.BRAKE_LATCHED]
    if not ticks:
        return []
    fires_by_slug: dict[str, list[dict]] = {}
    for f in fires:
        fires_by_slug.setdefault(f["slug"], []).append(f)

    per_slug: dict[str, dict] = {}
    for ep in shadow.collapse_episodes(ticks):
        slug = ep["slug"]
        clip = shadow.window_clip_notional(fires_by_slug.get(slug, []))
        priced = shadow.price_episode(ep, winners.get(slug), clip)
        if priced["status"] != "priced" or priced["won"]:
            continue  # unresolved, unpriceable, or the latch cost us a winner
        agg = per_slug.setdefault(slug, {"avoided": 0.0, "t": priced["end"],
                                          "sides": set()})
        agg["avoided"] += -float(priced["pnl"])
        agg["t"] = max(agg["t"], priced["end"])
        agg["sides"].add(priced["side"])

    out = []
    for slug, agg in per_slug.items():
        if agg["avoided"] < NOTABLE_SAVE_USD:
            continue
        sides = "/".join(sorted(agg["sides"]))
        line = (f"{_series(slug)} latch held {sides} — that side lost, "
                f"${agg['avoided']:,.0f} not spent")
        w = graded.get(slug)
        if w is not None:
            line += f"; window closed {_money(float(w.get('pnl') or 0.0))}"
        out.append(_ev(agg["t"], f"latch:{slug}", line))
    return out


def firsts(windows: list[dict], orders: list[dict], maker: dict,
           arms: list[dict], state: dict, now: float) -> list[dict]:
    """First time a flag put capital somewhere new.

    New symbols come off the graded windows (the wallet actually filled one).
    rtds comes off arms-state, which is the only durable record of an arm's
    feed — the decision tape does not carry it, so an rtds arm that rolled off
    before the first journal run is invisible here. Maker rests come off the
    order tape's post_only acks, which updown_stats calls the hard number.
    """
    out = []

    seen_symbols = state.get("symbols") or {}
    firsts_by_symbol: dict[str, dict] = {}
    for w in sorted(windows, key=lambda x: float(x.get("end_ts") or 0.0)):
        parsed = updown_slugs.parse(w.get("slug") or "")
        if parsed is None or parsed[0] in seen_symbols:
            continue
        firsts_by_symbol.setdefault(parsed[0], w)
    for sym, w in firsts_by_symbol.items():
        verdict = "won" if w.get("won") else "lost"
        out.append(_ev(w["end_ts"], f"first:symbol:{sym}",
                        f"first {sym} window filled — "
                        f"${float(w.get('notional') or 0.0):,.0f} in, {verdict}",
                        {"symbols": {sym: w["slug"]}}))

    if not state.get("rtds"):
        rtds = sorted((a for a in arms if a.get("feed") == "rtds"),
                       key=lambda a: float(a.get("start") or 0.0))
        if rtds:
            a = rtds[0]
            out.append(_ev(a.get("start") or now, "first:rtds",
                            f"{_series(a.get('slug') or '')} first window off the "
                            f"rtds feed — pricing the stream it settles on, not the proxy",
                            {"rtds": a.get("slug")}))

    if not state.get("maker_rest"):
        # Same predicate updown_stats.maker_summary calls the hard number: an
        # ACKED post-only order, not a decision the delta matcher suppressed.
        rests = sorted((o for o in orders
                        if o.get("stage") == tape.STAGE_ACK and o.get("post_only")),
                        key=lambda o: float(o.get("t") or 0.0))
        if rests:
            o = rests[0]
            out.append(_ev(o.get("t") or now, "first:maker-rest",
                            f"first post-only bid rested at {o.get('price')} — "
                            f"maker step 0 has capital on the book",
                            {"maker_rest": o.get("order_id")}))

    if not state.get("maker_fill") and (maker.get("fills") or 0) > 0:
        # Stamped at RUN time, not fill time: maker_summary's join is a
        # price/side/window match against the wallet, and a post-only order id
        # never reaches the wallet feed — there is no fill timestamp to claim.
        out.append(_ev(now, "first:maker-fill",
                        f"first maker fill on the books — "
                        f"${float(maker.get('fill_usd') or 0.0):,.2f} across "
                        f"{maker['fills']} bid(s); the join is circumstantial, "
                        f"a cheap taker clip satisfies it too",
                        {"maker_fill": True}))
    return out


def is_milestone(n: int) -> bool:
    """25, 50, then every 50."""
    return n == 25 or (n >= 50 and n % 50 == 0)


def streak_milestones(windows: list[dict]) -> list[dict]:
    """Crossings of the consecutive-win milestones.

    Asks effectiveness.streak() for the run at each settled window in turn
    rather than re-deriving it here, so the milestone and the number
    `pmt crypto stats` prints can never mean two different things.
    """
    ordered = sorted(windows, key=lambda w: float(w.get("end_ts") or 0.0))
    out = []
    for i, w in enumerate(ordered, start=1):
        n = effectiveness.streak(ordered[:i])["current"]
        if is_milestone(n):
            out.append(_ev(w.get("end_ts") or 0.0, f"streak:{n}:{w['slug']}",
                            f"{n} in a row — {_series(w['slug'])} kept it alive"))
    return out


def arm_scale(arms: list[dict]) -> dict[str, dict]:
    """{series: {"size", "clip"}} as arms-state has it right now. Keyed by
    series, not slug: every arm in a series carries the same params, and a
    roll would otherwise read as a change every window."""
    out: dict[str, dict] = {}
    for a in arms:
        parsed = updown_slugs.parse(a.get("slug") or "")
        if parsed is None:
            continue
        out[parsed[4]] = {"size": float(a.get("size_usdc") or 0.0),
                          "clip": float(a.get("clip_usdc") or 0.0)}
    return out


def scale_changes(arms: list[dict], state: dict, now: float) -> list[dict]:
    """size/clip moves per series, against what the last run saw.

    First sight of a series is silent — a journal of changes should not open
    with a wall of things that did not change. A sizing decision made at 02:00
    is one nobody remembers making by Thursday, which is the whole reason this
    class exists.
    """
    seen = state.get("scale") or {}
    out = []
    for series, cur in sorted(arm_scale(arms).items()):
        prev = seen.get(series)
        if prev is None or cur == prev:
            continue
        bits = []
        if cur["size"] != prev.get("size"):
            verb = "sized up" if cur["size"] > prev.get("size", 0) else "sized down"
            bits.append(f"{verb} ${prev.get('size', 0):,.0f}→${cur['size']:,.0f}")
        if cur["clip"] != prev.get("clip"):
            bits.append(f"clip ${prev.get('clip', 0):,.0f}→${cur['clip']:,.0f}")
        out.append(_ev(now, f"scale:{series}:{cur['size']:.0f}:{cur['clip']:.0f}",
                        f"{series} {', '.join(bits)}"))
    return out


def note_scale(state: dict, arms: list[dict]) -> dict:
    """Record the CURRENT scale as the baseline for the next run.

    Unconditional, and separate from `commit`: a change whose line was already
    written (a second run inside the same arm configuration) must still move
    the baseline, or the state never converges on what the arms actually are.
    """
    scale = dict(state.get("scale") or {})
    scale.update(arm_scale(arms))
    state["scale"] = scale
    return state


def detect(*, windows: list[dict], tape_lines: list[str], orders: list[dict],
           fires: list[dict], winners: dict[str, str], maker: dict,
           arms: list[dict], state: dict, now: float) -> list[dict]:
    """Every candidate event, unfiltered and unsorted. `select` decides which
    ones are actually new."""
    graded = {w["slug"]: w for w in windows}
    return [
        *day_extremes(windows, state),
        *latch_saves(tape_lines, winners, fires, graded),
        *firsts(windows, orders, maker, arms, state, now),
        *streak_milestones(windows),
        *scale_changes(arms, state, now),
    ]


# ---------- selection, state, output ----------

def select(events: list[dict], state: dict, floor: float) -> list[dict]:
    """Events to actually write: at or after `floor`, never emitted before,
    oldest-first — the file is append-only and reads as a timeline."""
    seen = set(state.get("seen") or [])
    deduped: dict[str, dict] = {}
    for e in sorted(events, key=lambda e: e["t"]):
        if e["t"] >= floor and e["key"] not in seen:
            deduped.setdefault(e["key"], e)
    return list(deduped.values())


def floor_for(state: dict, since: float | None, now: float) -> float:
    """Where this run starts reading. An explicit --since wins outright;
    otherwise the high-water mark minus the late-redeem slack, or a one-day
    look-back on a state file that has never run."""
    if since is not None:
        return since
    hwm = float(state.get("hwm") or 0.0)
    if hwm <= 0:
        return now - FIRST_RUN_LOOKBACK_S
    return hwm - LOOKBACK_SLACK_S


def _merge(dst: dict, patch: dict) -> None:
    for k, v in patch.items():
        if isinstance(v, dict) and isinstance(dst.get(k), dict):
            _merge(dst[k], v)
        else:
            dst[k] = v


def commit(state: dict, written: list[dict], now: float) -> dict:
    """Fold written events back into the state: their keys, their per-detector
    memory, and the high-water mark. Nothing that was not written moves — a
    floor-filtered event must not claim to have been journaled."""
    seen = list(state.get("seen") or [])
    for e in written:
        _merge(state, e["state"])
        seen.append(e["key"])
    state["seen"] = seen[-SEEN_CAP:]
    if written:
        state["hwm"] = max(float(state.get("hwm") or 0.0),
                            max(e["t"] for e in written))
    state["last_run"] = now
    return state


def load_state(path: Path = STATE_PATH) -> dict:
    """State from disk; an absent or corrupt file is a fresh start, never an
    error — a journal that refuses to run because its bookkeeping is unreadable
    is worse than one that re-offers a day."""
    try:
        with open(path) as fh:
            s = json.load(fh)
    except (OSError, ValueError):
        return {}
    return s if isinstance(s, dict) else {}


def save_state(state: dict, path: Path = STATE_PATH) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w") as fh:
        json.dump(state, fh, indent=1, sort_keys=True)
    tmp.replace(path)  # a torn state file would re-journal a day


_HEADING_RE = re.compile(r"^## (\d{4}-\d{2}-\d{2})\s*$")


def last_day(text: str) -> str:
    """The day heading the file currently ends under, "" for an empty file."""
    day = ""
    for ln in text.splitlines():
        m = _HEADING_RE.match(ln)
        if m:
            day = m.group(1)
    return day


def render_lines(events: list[dict], open_day: str) -> list[str]:
    """The exact lines to append, day headings included.

    A heading is emitted whenever the day CHANGES, not when it is first seen:
    the file is append-only, so a `--since` backfill genuinely does put an
    older day at the bottom, and a repeated heading is how that stays legible
    instead of the old lines silently joining the wrong day.
    """
    out: list[str] = []
    day_open = open_day
    for e in events:
        day = _day(e["t"])
        if day != day_open:
            day_open = day
            out.append("")
            out.append(f"## {day}")
        out.append(f"{_stamp(e['t'])}  {e['line']}")
    return out


def append(events: list[dict], path: Path = JOURNAL_PATH) -> int:
    """Append events under their local-day headings. Returns lines written."""
    if not events:
        return 0
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        text = path.read_text()
    except OSError:
        text = ""
    lines = render_lines(events, last_day(text))
    with open(path, "a") as fh:
        if not text:
            fh.write(HEADER)
        fh.write("\n".join(lines) + "\n")
    return len(events)


# ---------- --show ----------

_MONEY_RE = re.compile(r"([+-]\$[\d,]+\.\d{2})")


def styled(line: str) -> str:
    """One stored line as Rich markup: heading bold, signed money coloured,
    the clause after the em dash dim. Escapes first — a journal line is data."""
    from rich.markup import escape

    text = escape(line)
    m = _HEADING_RE.match(line)
    if m:
        return f"[bold]{text}[/bold]"
    text = _MONEY_RE.sub(
        lambda mm: f"[{'green' if mm.group(1)[0] == '+' else 'red'}]{mm.group(1)}[/]",
        text)
    head, sep, tail = text.partition(" — ")
    if sep:
        text = f"{head} — [dim]{tail}[/dim]"
    if text.startswith("#"):
        return f"[dim]{text}[/dim]"
    # The HH:MM stamp is scaffolding; the sentence is the content.
    stamp, sp, rest = text.partition("  ")
    if sp and re.fullmatch(r"\d{2}:\d{2}", stamp):
        return f"[dim]{stamp}[/dim]  {rest}"
    return text


def tail(path: Path = JOURNAL_PATH, n: int = 20) -> list[str]:
    """The last `n` entry lines, each under the day heading it belongs to —
    a tail that drops the headings is a tail with no dates in it."""
    try:
        raw = path.read_text().splitlines()
    except OSError:
        return []
    day = ""
    entries: list[tuple[str, str]] = []
    for ln in raw:
        if _HEADING_RE.match(ln):
            day = ln
        elif ln.strip() and not ln.startswith("#"):
            entries.append((day, ln))

    out: list[str] = []
    shown = ""
    for day, ln in entries[-n:]:
        if day and day != shown:
            shown = day
            if out:
                out.append("")
            out.append(day)
        out.append(ln)
    return out
