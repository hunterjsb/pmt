"""Render layer for `pmt crypto watch` (and the tape/stats renderers it shares).

Every function here turns already-fetched data into text, Rich markup, or a
Rich renderable. No network, no engine calls, no wallet walks — that all lives
on cli_crypto_watch's WatchFetcher thread (docs/LESSONS.md#L28), which is why
this file is unit-testable without a single mock: hand it a dict, read the
string back.

The two hard rules the dashboard is built on:
  * every cell tolerates a missing or half-built eval — an engine restart
    mid-watch leaves `last_eval` None or partial, and the dashboard must keep
    painting rather than raise;
  * column geometry is fixed (see _HEAD_*, _WINDOWS_COLUMNS,
    _TAPE_TAG_WIDTH, _TAPE_AGG_WIDTH), so the layout never jitters as
    state/reason text changes tick to tick, and every panel puts a given
    kind of figure at the same offset every frame.

Also holds the tty-mode helpers, since cbreak/key-polling exists only to serve
this dashboard's input loop.
"""

from __future__ import annotations

import json
import os
import re
import select
import sys
import termios
import time
import tty

import click
from rich.table import Table

from cli_common import _pnl_color
from polymarket import tape, updown_slugs


def _tape_slug(slug: str) -> str:
    """btc-updown-5m-1787442000 -> 'btc 5m 23:40' (window start, local time)."""
    return updown_slugs.display(slug)


_BRAKE_COLOR = {  # brake kind -> style name (Rich tag as-is; ANSI parses out "bold")
    "safety": "yellow", "distrust": "red", "avg_down": "magenta", "latched": "red bold",
}


_SAFETY_STRONG = 0.3  # display cue only; mirrors the deployed theta (docs/LESSONS.md#L13)


def _side_safety(sides: list[dict]) -> tuple[float | None, float | None]:
    """(up, down) safety values from an eval's `sides` list."""
    by_side = {s.get("side"): s.get("safety") for s in sides}
    return by_side.get("up"), by_side.get("down")


def _safety_text(sides: list[dict]) -> str:
    """`saf +x/-x`, "·" for a side with no book right now, "" if neither side evaluated."""
    up_s, dn_s = _side_safety(sides)
    if up_s is None and dn_s is None:
        return ""
    return "saf " + "/".join(f"{v:+.2f}" if v is not None else "·" for v in (up_s, dn_s))


def _safety_is_strong(sides: list[dict], p_up: float | None) -> bool:
    """True once the model's favored side clears the theta band."""
    up_s, dn_s = _side_safety(sides)
    fav = up_s if (p_up or 0) >= 0.5 else dn_s
    return fav is not None and abs(fav) >= _SAFETY_STRONG


def _brake_sides(sides: list[dict]) -> list[tuple[str, str]]:
    """[(side, brake_kind), ...] for sides currently braked (fire blocked)."""
    return [(s["side"], s["brake"]) for s in sides if s.get("brake")]


def _safety_rich(sides: list[dict], p_up: float | None) -> str:
    txt = _safety_text(sides)
    if not txt:
        return ""
    style = "green" if _safety_is_strong(sides, p_up) else "dim"
    return f"[{style}]{txt}[/{style}]"


def _brake_rich(sides: list[dict]) -> str:
    return " ".join(f"[{_BRAKE_COLOR.get(b, 'white')}]{side}:{b}[/]"
                     for side, b in _brake_sides(sides))


def _has_rtds_arm(arms: dict | None) -> bool:
    """True when some arm is actually reading the settlement stream."""
    return any(isinstance(a, dict) and a.get("feed") == "rtds"
               for a in (arms or {}).values())


def rtds_cells(h: dict | None) -> list[str]:
    """The stream-health segments, as cells: `[head, "3.0/s", "age 0s", ...]`.

    The head carries its own color (green connected / red down); the rest are
    raw so a caller can dim them, or lay them out in a grid, without having to
    re-derive a single number. `_rtds_rich` joins these into the one-line form
    both watch rows use — one computation, two presentations.
    """
    if not h or not (h.get("started") or h.get("events")):
        return []
    age = h.get("last_event_age_s")
    if h.get("connected"):
        head = "[green]rtds[/green]"
    else:
        head = "[red]rtds DOWN[/red]"
    bits = [f"{h.get('events_per_s', 0):.1f}/s",
            f"age {age:.0f}s" if age is not None else "no events yet",
            f"{h.get('consumers', 0)} arms"]
    if h.get("reconnects"):
        bits.append(f"{h['reconnects']} reconnects")
    if h.get("err") and not h.get("connected"):
        bits.append(str(h["err"])[:48])
    return [head] + bits


def _rtds_rich(h: dict | None) -> str:
    """One line for the shared RTDS settlement-stream supervisor, or "" when
    nothing has ever armed on it.

    Worth its own line rather than a per-arm field: it is ONE socket behind
    every stream-fed arm, so when it drops they all gate at once and the
    per-arm reasons all say the same thing. Red the moment it is not
    connected — a dark stream is a fleet-wide event.
    """
    cells = rtds_cells(h)
    if not cells:
        return ""
    head, bits = cells[0], cells[1:]
    return f"{head} [dim]{' · '.join(bits)}[/dim]"


def rtds_line_cells(status: dict | None) -> list[str]:
    """rtds_cells, gated the way _rtds_line is — empty unless an arm is on the
    stream right now."""
    status = status or {}
    if not _has_rtds_arm(status.get("arms")):
        return []
    return rtds_cells(status.get("rtds"))


def _rtds_line(status: dict | None) -> str:
    """_rtds_rich, but silent unless an arm is reading the stream right now.

    The socket is opened lazily by the first rtds arm and outlives it — once
    every stream-fed arm has rolled to binance or retired, its health is no
    longer a fact about the fleet, and a line that never goes away stops
    being read.
    """
    status = status or {}
    if not _has_rtds_arm(status.get("arms")):
        return ""
    return _rtds_rich(status.get("rtds"))


def _click_fg_bold(style: str) -> tuple[str | None, bool]:
    parts = style.split()
    return next((p for p in parts if p != "bold"), None), "bold" in parts


def _safety_ansi(sides: list[dict], p_up: float | None) -> str:
    txt = _safety_text(sides)
    if not txt:
        return ""
    strong = _safety_is_strong(sides, p_up)
    return click.style(txt, fg="green" if strong else None, dim=not strong)


def _brake_ansi(sides: list[dict]) -> str:
    parts = []
    for side, b in _brake_sides(sides):
        fg, bold = _click_fg_bold(_BRAKE_COLOR.get(b, "white"))
        parts.append(click.style(f"{side}:{b}", fg=fg, bold=bold))
    return "  ".join(parts)


_MARGIN_RE = re.compile(r"projected margin ([+-]?\d+\.?\d*)bp inside (\d+\.?\d*)bp")


def _gated_reason_compact(reason: str | None, e: dict | None = None) -> str:
    """`margin -4.9 vs 6.0bp` for a basis-guard gate; the elapsed-percent
    gate and anything else fall back to the raw (truncated) reason so
    nothing gets swallowed.

    The engine emits `margin_bp`/`guard_bp` as structured fields on the
    gated eval — prefer those. The regex is the legacy path only: an eval
    from an engine built before those fields shipped. It parses the same
    sentence the fields are formatted from, so the two can't disagree, but
    a reword breaks the regex and not the fields.
    """
    if e:
        margin, thresh = e.get("margin_bp"), e.get("guard_bp")
        if margin is not None and thresh is not None:
            return f"margin {margin:+.1f} vs {thresh:.1f}bp"
    reason = reason or ""
    m = _MARGIN_RE.search(reason)
    if m:
        margin, thresh = float(m.group(1)), float(m.group(2))
        return f"margin {margin:+.1f} vs {thresh:.1f}bp"
    return reason[:60] if reason else "gated"


def _evidence_style(banked: float, cushion: float, banked_decided: bool) -> str:
    """green once banked evidence alone clears the cushion (banked-decided),
    yellow once it clears the same theta=0.3 partial band as the per-side
    safety badge (_SAFETY_STRONG — same banked/cushion ratio, different
    field: this is the eval-level total, not one side's signed view), dim
    below that."""
    if banked_decided:
        return "green"
    if cushion <= 0:
        return "dim"
    ratio = abs(banked) / cushion
    if ratio >= 1.0:
        return "green"
    if ratio >= _SAFETY_STRONG:
        return "yellow"
    return "dim"


def _evidence_markup(e: dict) -> str:
    """`+5.2/9.3bp` (banked vs cushion) from an eval, colored by
    _evidence_style; '—' when either field is missing (a partial eval right
    after an engine restart, or a gated eval that never reached the model)."""
    banked, cushion = e.get("banked_bp"), e.get("cushion_bp")
    if banked is None or cushion is None:
        return "[dim]—[/dim]"
    style = _evidence_style(banked, cushion, bool(e.get("banked_decided")))
    return f"[{style}]{banked:+.1f}/{cushion:.1f}bp[/{style}]"


def _countdown_style(rem_s: float) -> str:
    if rem_s < 60:
        return "bold red"
    if rem_s < 300:
        return "white"
    return "dim"


def _countdown_markup(slug: str, now: float) -> str:
    """`m:ss` to window end, parsed straight from the slug — no eval needed,
    so it still renders through an engine restart. '—' for an unparseable slug."""
    w = updown_slugs.parse_updown_slug(slug)
    if w is None:
        return "[dim]—[/dim]"
    rem = w["end"] - now
    if rem <= 0:
        return "[dim]0:00[/dim]"
    m, s = divmod(int(rem), 60)
    style = _countdown_style(rem)
    return f"[{style}]{m}:{s:02d}[/{style}]"


def _mode_text(e: dict) -> str:
    """One unified regime label: `safe`/`spec` from an armed eval's own
    `mode` field, or `flip`/`quiesce` when that's the eval's `state` itself
    (the pre-model quiesce-window states carry no `mode` field of their own)."""
    state = e.get("state")
    if state in ("flip", "quiesce"):
        return state
    return e.get("mode") or "—"


_TAPE_TAG_WIDTH = 9  # "FIRE DOWN"/"FLIP DOWN"/etc — the widest natural tag, unpadded

_TAPE_AGG_WIDTH = 5  # "×9999" — one tape line never collapses more than that


def _tape_tag(text: str) -> str:
    """Left-pad an event tag to a fixed width so the fields after it land at
    the same column regardless of event type (FIRE/EXIT/eval/gated/ROLL)."""
    return f"{text:<{_TAPE_TAG_WIDTH}}"


def _tape_agg(n: int) -> str:
    """The collapse counter in ITS OWN fixed column, immediately after the
    tag — blank (but still occupied) for a line that collapsed nothing.

    ONE column for every line type is the whole point: the count used to sit
    inside the tag for a basis gate and at the end of the line for an eval,
    so the eye had to re-find it per line type. Deliberately not the right
    edge: tape bodies are 60-110 characters wide, so a right-aligned marker
    would have to pad past the terminal or truncate a body to hold a column.

    Plain text, never styled: it is concatenated into lines that are styled
    as a whole, and a nested reset would drop the rest of the line's color.
    """
    return f"{('×' + str(n)) if n > 1 else '':<{_TAPE_AGG_WIDTH}}"


def _hms(t: float) -> str:
    return time.strftime("%H:%M:%S", time.localtime(t))


def _zero(v: float) -> float:
    """Snap sub-cent float drift to 0.0 so committed/exposure never render a
    phantom "-$0.00" — a residual after fills settle to ~zero (mirrors
    stats_render._zeroed; same threshold)."""
    return 0.0 if abs(v) < 0.005 else v


def _tape_head(r: dict) -> str:
    """`HH:MM:SS  slug-padded-to-14` — the fixed-width prefix shared by every
    tape-line renderer, so eval/fire/gated/roll/exit lines all column-align.
    14 is the widest current display() form (e.g. "doge 60m 23:40")."""
    return f"{_hms(r.get('t', 0))}  {_tape_slug(r.get('slug', '')):<14}"


def _tape_render(line: str) -> str | None:
    try:
        r = json.loads(line)
    except ValueError:
        return None
    return _render_record(r, line)


def _render_record(r: dict, raw: str, n: int = 1) -> str:
    """One parsed tape record as a rendered line; `raw` is the fallback for an
    event this build doesn't know (never swallow a record we can't name).

    `n` is how many records this line stands for — it renders into the fixed
    aggregation column (_tape_agg) so every line type carries its count in the
    same place. 1 leaves that column blank but occupied.
    """
    head, agg = _tape_head(r), _tape_agg(n)

    def money(v: float) -> str:
        return f"${_zero(v):,.2f}".rstrip("0").rstrip(".")

    ev = r.get("ev")
    if ev == tape.EV_FIRE:
        tag = {"flip": "FLIP", "spec": "SPEC"}.get(r.get("mode", "safe"), "FIRE")
        label = click.style(_tape_tag(f"{tag} {r['side'].upper()}"), fg="green", bold=True)
        pct = f"  {r['elapsed_frac'] * 100:.0f}% thru" if "elapsed_frac" in r else ""
        return (
            f"{head} {label}{agg}{r['size']:g}sh @ {r['ask']:.2f}"
            f"  fair {r['fair']:.4f}  {r['net'] * 100:+.1f}¢"
            f"  ρ{r['rho']:+.2f}  {money(r['committed'])} in{pct}"
        )
    if ev == tape.EV_EXIT:
        label = click.style(_tape_tag(f"EXIT {r['side'].upper()}"), fg="red", bold=True)
        return (f"{head} {label}{agg}{r['size']:g}sh @ bid {r['bid']:.2f}"
                f"  fair {r['fair']:.4f}")
    if ev == tape.EV_EVAL:
        sides = r.get("sides") or []
        best = _best_side(sides)
        book = (
            f"{best['side']} @ {best['ask']:.2f} {best['net'] * 100:+.1f}¢"
            if best
            else "no book"
        )
        tags = click.style("  BANKED", fg="cyan") if r.get("banked_decided") else ""
        if r.get("maker_rest") is not None:
            tags += click.style(f"  ◇RESTING @{r['maker_rest']:.3f}", fg="cyan", bold=True)
        elif r.get("maker_candidate"):
            tags += click.style("  ◇maker-candidate", fg="cyan")
        body = (
            f"{head} {_tape_tag('eval')}{agg}p↑{r['p_up']:.4f}  {book}"
            f"  ρ{r['rho']:+.2f}  {money(r['committed'])} in"
        )
        extras = "  ".join(x for x in (_safety_ansi(sides, r.get("p_up")), _brake_ansi(sides)) if x)
        return click.style(body, dim=True) + tags + ("  " + extras if extras else "")
    if ev == tape.EV_GATED:
        def ask(v: float | None) -> str:
            return f"{v:.2f}" if v is not None else "—"
        asks = f"  up {ask(r['up_ask'])}/dn {ask(r['dn_ask'])}" if "up_ask" in r else ""
        return click.style(f"{head} {_tape_tag('gated')}{agg}{r.get('reason', '?')}{asks}",
                            fg="yellow", dim=True)
    if ev == tape.EV_ROLL:
        return click.style(f"{head} {_tape_tag('ROLL')}{agg}next window armed (${r['size']:g})",
                            fg="cyan")
    if ev == tape.EV_CLEANUP:
        # Tagged like every other event now: an untagged line was the one hole
        # in the tape's fixed column geometry.
        return click.style(f"{head} {_tape_tag('closed')}{agg}── window closed ──", dim=True)
    return raw.rstrip()


# ---------- tape run-collapsing ----------
#
# On a quiet tape 99% of records repeat: four arms each print a basis-guard
# gate, or the same theta gate, or an eval whose numbers haven't moved, every
# second — and the events that matter (fires, exits, brakes, rolls) drown in
# them. A "run" is consecutive records of one shape whose material state is
# unchanged; it renders as ONE line, updated in place, carrying the FRESHEST
# values plus ×N and the span it covers. Collapsing may hide repetition; it
# must never hide a transition, which is what the tolerances below are sized
# for and what every rule's signature exists to catch.

_NUM_RE = re.compile(r"[+-]?\d+(?:\.\d+)?")

# How deep under later output a live run's line may sit and still be updated.
# Sized for a fleet-wide roll: every arm emits its window-close, THEN every arm
# emits its roll, so the first arm's line is one-per-arm deep by the time its
# own roll arrives to merge into it. 16 leaves headroom over the 8 arms that
# have actually run at once.
_OWN_LOOKBACK = 16


def _run_suffix(run: _Run) -> str:
    """`⟨23:40:01→23:43:20⟩`, or "" for a run of one — so an isolated record
    renders byte-identically to an uncollapsed one.

    The COUNT is not here: it lives in the fixed _tape_agg column so every
    line type carries it at the same offset. This is only the span, which is
    variable-position by nature (it trails a variable-width body) and is read
    after the count, never instead of it.
    """
    if run.n < 2:
        return ""
    return click.style(f"  ⟨{_hms(run.t0)}→{_hms(run.t1)}⟩", dim=True)


def _within(anchor: dict, met: dict, tol: dict) -> bool:
    """True while every metric is still within tolerance of the run's FIRST
    record. Anchored, not chained: p_up creeping 0.002 a tick is a 0.6 move
    across five minutes, and a tick-to-tick comparison would never once call
    that material."""
    if anchor.keys() != met.keys():
        return False  # a field appearing or vanishing is itself a state change
    return all(abs(met[k] - v) <= tol.get(k, 0.0) for k, v in anchor.items())


class _Run:
    """One in-flight run: what it is keyed on, its anchor, and the exact string
    it owns in the caller's deque."""

    __slots__ = ("rule", "arm", "sig", "anchor", "rec", "raw", "state", "n", "t0", "t1", "out")

    def __init__(self, rule: _CollapseRule, arm: str | None, sig: tuple, anchor: dict) -> None:
        self.rule, self.arm, self.sig, self.anchor = rule, arm, sig, anchor
        self.rec: dict = {}
        self.raw = ""
        self.state: dict = {}   # rule-private accumulation across the run
        self.n = 0
        self.t0 = self.t1 = 0.0
        self.out: str | None = None


class _CollapseRule:
    """One repetitive tape shape that may collapse.

    `lane` is the run's identity: records in different lanes never join the
    same run, and a `scope == "arm"` rule keeps one lane per slug so
    interleaved arms don't tear each other's runs apart. Within a lane the run
    continues while `signature` is identical AND every `metrics` value stays
    within `tolerances` of the run's first record.
    """

    name = ""
    scope = "arm"                    # "arm": a lane per slug; "global": one lane, any arm
    tolerances: dict[str, float] = {}

    def matches(self, r: dict) -> bool:
        raise NotImplementedError

    def lane(self, r: dict) -> str:
        return f"{self.name}:{r.get('slug')}"

    def signature(self, r: dict) -> tuple:
        return ()

    def metrics(self, r: dict) -> dict:
        return {}

    def continues(self, run: _Run, r: dict) -> bool:
        """Rule-private veto: start a NEW run even though the signature and
        the metrics still match. Only a rule that holds a fixed set of records
        (the roll/close pair) needs it; repetition rules never do."""
        return True

    def fold(self, run: _Run, r: dict) -> None:
        """Accumulate whatever the rendered line needs beyond the freshest record."""

    def render(self, run: _Run) -> str:
        return _render_record(run.rec, run.raw, run.n) + _run_suffix(run)


class _BasisGuardRun(_CollapseRule):
    """Every arm's basis-guard gate on ONE line: `btc +1.0/6 · eth -4.9/6`.

    Global, not per-arm: the guard gates the whole fleet off the same
    cross-venue basis, so a mixed run of arms is one fact, not four. The
    margins themselves are freshest-value updates and never break the run —
    the arms table carries them as a live column too.
    """

    name, scope = "basis", "global"

    def matches(self, r: dict) -> bool:
        return (r.get("ev") == tape.EV_GATED
                and (r.get("reason") or "").startswith("basis guard"))

    def lane(self, r: dict) -> str:
        return "basis"

    def fold(self, run: _Run, r: dict) -> None:
        sym = (r.get("slug") or "?").split("-")[0]
        margin, guard = r.get("margin_bp"), r.get("guard_bp")
        if margin is not None and guard is not None:
            run.state[sym] = f"{margin:+.1f}/{guard:.0f}"
        else:
            m = _MARGIN_RE.search(r.get("reason") or "")
            run.state[sym] = f"{float(m.group(1)):+.1f}/{float(m.group(2)):.0f}" if m else "?"

    def render(self, run: _Run) -> str:
        # No span suffix: this is the widest line the tape emits (a per-symbol
        # margin for every arm in the fleet) and the ⟨from→to⟩ tail pushed it
        # past the panel, costing a second row to say what the line's own
        # timestamp and count already say.
        per = " · ".join(f"{sym} {txt}" for sym, txt in sorted(run.state.items()))
        return click.style(
            f"{_hms(run.t1)}  {'':<14} {_tape_tag('gated')}{_tape_agg(run.n)}"
            f"basis bp/guard: {per}",
            fg="yellow", dim=True)


class _EvalRun(_CollapseRule):
    """Consecutive evals of ONE arm whose read hasn't moved.

    An armed arm evaluates every tick and prints the same sentence until
    something happens. What counts as "something" is the whole safety
    argument for this rule: the tolerances are the width of the numbers as
    DISPLAYED, so anything a reader could see change on the line breaks the
    run and renders fresh.
    """

    name = "eval"
    # p_up to 2dp, committed to the cent, best-side net to the half-cent it is
    # printed in — below these the line would repaint identically anyway.
    tolerances = {"p_up": 0.01, "committed": 0.01, "net": 0.005}

    def matches(self, r: dict) -> bool:
        return r.get("ev") == tape.EV_EVAL

    def signature(self, r: dict) -> tuple:
        sides = r.get("sides") or []
        best = _best_side(sides)
        rest = r.get("maker_rest")
        return (
            bool(r.get("banked_decided")),
            tuple(_brake_sides(sides)),
            _safety_is_strong(sides, r.get("p_up")),
            # which sides even have a book: a side going dark is a transition
            tuple(s.get("side") for s in sides if s.get("safety") is not None),
            best.get("side") if best else None,
            None if rest is None else round(rest, 3),  # a repriced maker bid is an action
            bool(r.get("maker_candidate")),
            _mode_text(r),
        )

    def metrics(self, r: dict) -> dict:
        best = _best_side(r.get("sides") or [])
        m = {"p_up": r.get("p_up"), "committed": r.get("committed"),
             "net": best.get("net") if best else None}
        return {k: v for k, v in m.items() if v is not None}


class _GateRun(_CollapseRule):
    """Consecutive identical non-basis gates of ONE arm (theta, safety, feed
    stale, elapsed-percent) — these tick for minutes on end saying the same
    thing.

    The reason string is compared by SHAPE, never verbatim: it carries its own
    counters ("window 42% elapsed") that creep every tick, and a verbatim
    compare would end the run each time and collapse nothing. Whether the
    numbers actually moved is decided by the structured fields below, so a
    reworded reason can't silently change the rule's mind either.
    """

    name = "gate"
    # half a bp of margin/guard jitter is noise; the spot-age gate trips at 5s,
    # so a second of resolution still shows the feed going stale.
    tolerances = {"margin_bp": 0.5, "guard_bp": 0.5, "spot_age_s": 1.0}

    def matches(self, r: dict) -> bool:
        return (r.get("ev") == tape.EV_GATED
                and not (r.get("reason") or "").startswith("basis guard"))

    def signature(self, r: dict) -> tuple:
        return (_NUM_RE.sub("#", r.get("reason") or ""),
                "up_ask" in r, r.get("up_ask") is None, r.get("dn_ask") is None)

    def metrics(self, r: dict) -> dict:
        return {k: r[k] for k in self.tolerances if r.get(k) is not None}


class _RollClosePair(_CollapseRule):
    """A window closing and its arm re-arming the next one: ONE line.

    The engine emits both at the same instant — `cleanup` naming the window
    that just ended, `roll` naming the one it armed — so the pair always
    appeared as two lines that only meant something together, and a five-arm
    fleet printed ten of them every roll. Merged they read as the single fact
    they are: `09:30 closed → next window armed ($1,000)`.

    Keyed on the SERIES (`btc 5m`), not the slug, because the two records
    deliberately name different windows of it. Anchored on `t`: a later close
    is a later roll, never a continuation of this one.
    """

    name = "roll"
    tolerances = {"t": 5.0}  # one roll moment — the pair shares a timestamp

    def matches(self, r: dict) -> bool:
        return r.get("ev") in (tape.EV_ROLL, tape.EV_CLEANUP)

    def lane(self, r: dict) -> str:
        parsed = updown_slugs.parse(r.get("slug") or "")
        return f"roll:{parsed[4] if parsed else r.get('slug')}"

    def metrics(self, r: dict) -> dict:
        return {"t": r.get("t") or 0.0}

    def continues(self, run: _Run, r: dict) -> bool:
        # At most one close and one roll per line; a second of either is a
        # second event and must get its own line.
        return r.get("ev") not in run.state

    def fold(self, run: _Run, r: dict) -> None:
        run.state[r["ev"]] = r

    def render(self, run: _Run) -> str:
        closed = run.state.get(tape.EV_CLEANUP)
        rolled = run.state.get(tape.EV_ROLL)
        if rolled is None or closed is None:
            # Half the pair (a --no-roll arm closing, or a roll with no close
            # recorded) still renders as its own event, never as a merged line
            # implying a fact the tape didn't carry.
            return _render_record(rolled or closed, "")
        w = updown_slugs.parse_updown_slug(closed.get("slug") or "")
        shut = time.strftime("%H:%M", time.localtime(w["start"])) if w else "?"
        return click.style(
            f"{_tape_head(rolled)} {_tape_tag('ROLL')}{_tape_agg(1)}"
            f"{shut} closed → next window armed (${rolled.get('size', 0):g})",
            fg="cyan")


def _best_side(sides: list[dict]) -> dict | None:
    """The side the eval line prints — same pick as _render_record's."""
    return max(sides, key=lambda s: s["net"], default=None)


class TapeCollapser:
    """Collapse runs of repetitive tape records into single live-updating lines.

    One rule per repetitive shape (see _CollapseRule). A record that matches no
    rule — FIRE, EXIT, or any ev this build doesn't know — never collapses and
    ends EVERY open run: those are the lines the whole mechanism exists to make
    visible.

    ROLL and CLEANUP are the one pair that DOES merge (_RollClosePair), because
    the engine emits them together and neither reads without the other. They
    still end the runs of the arm they name, so an arm's eval run can never
    survive across its own window boundary.

    A record also ends the runs it contradicts rather than only its own: the
    global basis run dies on anything that isn't a basis gate (an eval means
    that arm cleared the guard), and an arm's own eval/gate runs die on each
    other, since one arm cannot be both at once.
    """

    _RULES: tuple[_CollapseRule, ...] = (_BasisGuardRun(), _EvalRun(), _GateRun(),
                                          _RollClosePair())

    def __init__(self) -> None:
        self._runs: dict[str, _Run] = {}

    def add(self, raw: str, lines) -> None:
        """Feed one raw tape line; appends/updates rendered output in `lines`."""
        try:
            r = json.loads(raw)
        except ValueError:
            return  # torn mid-write line: not a record, so not a run break either
        if not isinstance(r, dict):
            return
        try:
            run = self._route(r, raw)
        except Exception:
            run = None  # a malformed record must never take the dashboard down
        if run is not None:
            try:
                out = run.rule.render(run)
            except Exception:
                return
            slot = self._own_slot(run, lines)
            if slot is None:
                lines.append(out)
            else:
                lines[slot] = out
            run.out = out
            return
        self._runs.clear()
        try:
            rendered = _render_record(r, raw)
        except Exception:
            return
        if rendered:
            lines.append(rendered)

    def _route(self, r: dict, raw: str) -> _Run | None:
        rule = next((ru for ru in self._RULES if ru.matches(r)), None)
        if rule is None:
            return None
        lane, arm = rule.lane(r), r.get("slug")
        sig, met = rule.signature(r), rule.metrics(r)
        self._end_conflicting(lane, arm)
        run = self._runs.get(lane)
        if (run is None or run.sig != sig
                or not _within(run.anchor, met, rule.tolerances)
                or not rule.continues(run, r)):
            run = self._runs[lane] = _Run(rule, arm, sig, met)
        run.n += 1
        run.t1 = r.get("t", 0.0)
        if run.n == 1:
            run.t0 = run.t1
        run.rec, run.raw = r, raw
        rule.fold(run, r)
        return run

    def _end_conflicting(self, lane: str, arm: str | None) -> None:
        """Drop every run this record contradicts: any global run, and this
        arm's other runs. Other arms' runs survive, which is the whole point of
        per-arm lanes — four arms interleaving on the tape must not thrash."""
        for k, run in list(self._runs.items()):
            if k != lane and (run.rule.scope == "global" or run.arm == arm):
                del self._runs[k]

    @staticmethod
    def _own_slot(run: _Run, lines) -> int | None:
        """Index of the line this run owns, or None to append.

        Identity, not equality: a foreign line that happens to render the same
        text is never overwritten. Only the tail _OWN_LOOKBACK entries are
        searched — past that the run's line has scrolled away from the action
        and a fresh one below reads better than a stale one being edited
        off-screen.
        """
        if run.out is None or not lines:
            return None
        for i in range(len(lines) - 1, max(-1, len(lines) - 1 - _OWN_LOOKBACK), -1):
            if lines[i] is run.out:
                return i
        return None


_UNDECIDED_YELLOW_USD = 300.0  # R7 speculative-exposure threshold zone


_UNDECIDED_RED_USD = 500.0


def _risk_exposure(arms: dict | None) -> tuple[float, float, float]:
    """(committed, undecided, resting) USDC summed across a /status reply's arms.

    committed = every arm's filled_usdc. undecided = the slice of that still
    sitting in arms whose last eval hasn't banked-decided (including a
    missing eval entirely, e.g. right after an engine restart) — that's
    speculative exposure that could still flip. resting = notional tied up in
    post-only bids sitting on the book: not exposure yet, but one fill away,
    and it is already spending the arm's budget.

    `resting_usdc` is arm-level and always present on a current engine;
    `eval.resting` is the pre-status-field fallback (only emitted when > 0).
    """
    committed = undecided = resting = 0.0
    for a in (arms or {}).values():
        if not isinstance(a, dict):
            continue
        filled = a.get("filled_usdc") or 0.0
        committed += filled
        e = a.get("eval")
        e = e if isinstance(e, dict) else {}
        if not e.get("banked_decided"):
            undecided += filled
        resting += a.get("resting_usdc") or e.get("resting") or 0.0
    return committed, undecided, resting


def risk_cells(status: dict | None, sb: dict | None,
                rtds: bool = True) -> list[str]:
    """The exposure summary's segments, as cells:
    `["committed $Y", "$Z un-decided", ("◇resting $R"), "riding N windows $W"]`.

    THE one place committed/un-decided/resting/riding are computed and the
    un-decided threshold color is chosen. `exposure_rows` lays these into the
    header grid both the dashboard and `pmt crypto stats` render, so the two
    views cannot disagree about what is at risk.

    Reads only already-cached data (status/sb) — never fetches.
    """
    committed, undecided, resting = _risk_exposure((status or {}).get("arms"))
    riding_n = (sb or {}).get("riding_n", 0)
    riding_usd = (sb or {}).get("riding_usd", 0.0)
    color = ("red" if undecided > _UNDECIDED_RED_USD else
             "yellow" if undecided > _UNDECIDED_YELLOW_USD else "")
    undecided_s = f"${_zero(undecided):,.2f} un-decided"
    if color:
        undecided_s = f"[{color}]{undecided_s}[/{color}]"
    cells = [f"committed ${_zero(committed):,.2f}", undecided_s]
    # Only when a bid is actually on the book — a "◇resting $0.00" every tick
    # on a taker-only fleet is noise.
    if resting > 0.005:
        cells.append(f"[cyan]◇resting ${resting:,.2f}[/cyan]")
    cells.append(f"riding {riding_n} windows ${riding_usd:,.2f}")
    # One socket feeds every stream-fed arm, so its state belongs on the
    # fleet's risk line, not buried per-arm.
    health = _rtds_line(status) if rtds else ""
    if health:
        cells.append(health)
    return cells


def exposure_rows(status: dict | None, sb: dict | None) -> list[tuple]:
    """Live exposure as header-grid rows: `(label, v1, v2, v3)` tuples.

    The numbers and the un-decided threshold color come from `risk_cells`, so
    the watch dashboard and `pmt crypto stats` can never disagree about what
    is at risk — only about how many columns they had to say it in. Both
    render THESE rows; neither joins its own line any more, which is what let
    the two drift out of alignment in the first place.

    A resting maker bid earns its own row because it only exists on the days
    a bid is actually on the book.
    """
    cells = risk_cells(status or {}, sb, rtds=False)
    resting = next((c for c in cells if "resting" in c), None)
    riding = next(c for c in cells if c.startswith("riding"))
    rows = [("exposure", cells[0], cells[1], f"[dim]{riding}[/dim]")]
    if resting:
        rows.append(("resting", resting, "[dim]maker bid on the book[/dim]", ""))
    return rows


def feed_row(status: dict | None) -> tuple | None:
    """The shared settlement socket's health as one header-grid row, or None
    when no arm is reading the stream: one socket's state and the fleet's
    dollars answer different questions and get different rows."""
    cells = rtds_line_cells(status or {})
    if not cells:
        return None
    head, bits = cells[0], cells[1:]
    return ("feed",
            f"{head} [dim]{bits[0]}[/dim]" if bits else head,
            f"[dim]{bits[1]}[/dim]" if len(bits) > 1 else "",
            f"[dim]{' · '.join(bits[2:])}[/dim]" if len(bits) > 2 else "")


# ---------- the windows table: the fleet's state, one row per window ----------
#
# DESIGN NOTE (2026-08-23) — the dashboard's ONE table. The recent-windows chip
# strip, the trades table and the arms table are all folded in here; nothing
# else on the dashboard is a table.
#
# The strip was a projection of the trades table's own rows — same source, same
# order — so it spent three terminal rows restating them less precisely. The
# arms table was the same window seen from the other side: its rows and the
# trades panel's newest rows named the SAME windows, one panel apart, in two
# vocabularies, with `committed` and `size` quietly answering the same question
# from two sources. Two tables for one fleet is the thing this file no longer
# does.
#
# What replaces them is not a log of trades. It is the STRATEGY'S STATE. Its
# unit is the WINDOW, and a window has exactly one life:
#
#     armed ──► gated ──► fired / riding ──► decided W/L ──► rolled away
#       ○         ⊘             ◆               ✓  ✗          (ages off)
#
# Three properties let one table hold all of it:
#
#  1. ONE SORT KEY — window end, descending. A window's stage is a function of
#     its age, so time order IS lifecycle order for free: live windows (end
#     still in the future) sit on top, riding ones below them, then the decided
#     tail. Reading down the table is reading backwards through the fleet's
#     life. This is also the answer to "riding pinned vs recency": a fresh fill
#     already tops the panel under recency (a still-open window sorts above
#     every closed one), while a riding-always-first rule would hand four
#     windows stuck undecided for 13-25h (2026-08-23, $317) permanent tenancy.
#     Stuck money is totalled on the risk header's "riding N windows $W", which
#     is where a total belongs; this panel answers "where is the fleet now".
#
#  2. ONE ROW PER WINDOW, keyed by slug, merged from two sources. The live head
#     comes from the engine's /status arms — the ONLY place a window with no
#     fill yet exists, since the wallet has nothing to grade. Everything with
#     money in it comes from the ONE scoreboard (score_activity). A window in
#     both is ONE row that gains its live posture, so a fire does not spawn a
#     second row: it fills the row that was already on screen. The WALLET WINS
#     on money and verdict; the engine contributes posture, and its committed $
#     only for a window the wallet has not seen at all (rendered dim, because
#     it is the engine's figure and not yet ground truth).
#
#  3. THE STAGE GLYPH COLUMN carries everything the strip carried. Read
#     straight down it is the same ✓/✗ sequence in the same order (the strip
#     was ordered by these rows already), plus ◆ for a riding position and ○/⊘
#     for the live head. Nothing the strip said is gone; the arm label, the
#     P&L and the notional it compressed into a chip are now their own columns.
#     Glyph and the arms table's ⟳/≈/◇ flags share the `arm` cell — both are
#     properties of the row, not of a column of their own, and the glyph is
#     safe there from Rich's narrow-console squeeze (see _WINDOWS_COLUMNS).
#
#  4. EVERY COLUMN IS REPURPOSED BY STAGE rather than duplicated. This is what
#     made folding the arms table possible: a live un-filled window's money
#     cells are dead space, and a rolled-away window's model cells are dead
#     space, so the two sets share the SAME columns and the stage decides which
#     meaning is showing. The column count never changes, so the geometry never
#     jitters:
#
#       col       live (armed/gated)            settled (riding/decided)
#       ───────── ───────────────────────────── ─────────────────────────
#       t         T- countdown to the close     age since the close
#       state     armed/gated + mode + badges   held 3m12s (exposure time)
#                 or the compact gate reason
#       read      evidence · p↑ · ρ             20sh in 14:38 (what we took)
#       position  — (nothing held yet)          up 0.97→0.99 (entry → mark)
#       $         engine committed (dim)        wallet notional
#       P&L       —                             riding, or the signed P&L
#
#     `position`'s right-hand price is the one number that walks the whole
#     lifecycle in one place: the live mark while the window rides, and the
#     binary's own 1.00/0.00 once it settles.
#
# Grouping is FLAT, not by arm: a window belongs to a moment, not to an arm —
# an arm that rolled away still owns its riding position, and per-arm grouping
# would file that position under whichever arm happens to be alive now. The
# `arm` column carries the grouping the eye needs at a dozen rows.
#
# The money question the two tables used to answer twice is now answered once:
# `$` is the ENGINE's committed figure while the wallet has never seen the
# window (dim, because it is not ground truth yet) and the WALLET's notional
# from the moment it has. They can legitimately differ for a few seconds
# across a fill; which one wins is never in doubt.
#
# The arms table's one thing that is not a row — its red "engine unreachable or
# no arms" placeholder — moved to the header panel (see `engine_row`), because
# with no arms there are no live rows to hang it on and the exposure row would
# otherwise read a confident $0.00 committed.

_WINDOWS_COLUMNS = (
    # Stage glyph + series + flags in ONE cell: "◆ doge 15m ⟳≈◇" is 14. The
    # glyph does NOT get a column of its own — Rich squeezes every no_wrap
    # column equally on a console narrower than the table, and a 1-wide column
    # collapses to nothing, so the lifecycle would silently vanish first on
    # exactly the terminals that can least afford to lose it. Riding at the
    # head of a 14-wide cell it survives longest, and the cell ellipsises from
    # the right, which sheds the flags before the label and the label before
    # the glyph — the right order of importance.
    ("arm", "left", 14),
    ("t", "right", 6),         # T- past "59:59" on a not-yet-open window
    # Sized for the arms table's own worst case, which is a normal state and
    # not a corner: an armed arm with both sides' safety read AND a brake on
    # one of them — "armed safe  saf +0.90/-0.30  down:distrust" is 42. The
    # gate reason ("gated  margin -4.9 vs 6.0bp", 27) fits well inside that.
    # This and `read` are the two widest cells, so a console narrower than the
    # table's natural ~149 squeezes THEM first — which is the right place for
    # it to land: they are diagnostic prose, and every money column is sized to
    # its longest value and must never lose a digit.
    ("state", "left", 42),
    ("read", "right", 25),     # "+12.3/9.3bp p↑0.87 ρ+0.40" is 25
    ("position", "right", 15),  # "down 0.97→0.99" is 15
    ("$", "right", 15),        # "$1,234.56 ◇$450" is 15
    # 10, not 9: "-12,436.76" is a P&L this fleet has actually printed, and at
    # 9 the cell ellipsised a digit off it — the one failure a money column may
    # not have.
    ("P&L", "right", 10),
)

_STAGE_LEGEND = "○ armed · ⊘ gated · ◆ riding · ✓ won · ✗ lost"

# Stage -> (glyph, style). Anything else a live eval can say (flip, quiesce, a
# state this build doesn't know) is still "not firing", so it gets the blocked
# glyph without claiming to be a gate.
_STAGE_GLYPH = {
    "won": ("✓", "green"), "lost": ("✗", "red"), "riding": ("◆", "cyan"),
    "armed": ("○", "green"), "gated": ("⊘", "yellow"),
}


def _stage(w: dict) -> str:
    """Where this window is in its life: won/lost/riding, or the live eval's
    own state word (armed/gated/flip/quiesce/...) for the head of the chain.

    Money outranks posture. A window the wallet graded is decided whatever its
    arm is doing, and a window holding a position is `riding` even while that
    arm is gated out of adding to it — the gate blocks the next fire, not the
    position already on the books.
    """
    won = w.get("won")
    if won is not None:
        return "won" if won else "lost"
    if w.get("held"):
        return "riding"
    return w.get("state") or "armed"


def _stage_cell(w: dict) -> str:
    """The one-glyph lifecycle column. Dim for an ~estimated verdict, same
    lower-confidence convention the P&L cell and the old chips used."""
    glyph, style = _STAGE_GLYPH.get(_stage(w), ("⊘", "dim"))
    return f"[{'dim' if w.get('est') else style}]{glyph}[/]"


def _age_label(sec: float) -> str:
    """Compact time-since for a window row — `live` while the window is still
    open (which is every row of the live head), then s / m / h:mm. Age, not a
    wall clock: "how long ago" is the question a live dashboard is asked. The
    countdown to a live window's close is the arms table's T-, not this."""
    if sec < 0:
        return "live"
    if sec < 60:
        return f"{int(sec)}s"
    if sec < 3600:
        return f"{int(sec // 60)}m"
    h, m = divmod(int(sec // 60), 60)
    return f"{h}h{m:02d}"


def _window_pnl_cell(w: dict) -> str:
    """Signed P&L once the window is decided, `riding` while a position has no
    verdict, `—` while nothing is at risk at all. `~` marks an estimated figure
    (imputed win / gamma-unreachable), same convention as the header's
    "N ~estimated".

    Deliberately NOT the armed/gated word: the `state` cell two columns left
    already says it, in colour and with its reason, and the same word twice in
    one row is the duplication the fold exists to end.
    """
    stage = _stage(w)
    if stage == "riding":
        return "[cyan]riding[/cyan]"
    if stage not in ("won", "lost"):
        return "[dim]—[/dim]"
    v = _zero(float(w.get("pnl") or 0.0))
    return f"[{_pnl_color(v)}]{'~' if w.get('est') else ''}{v:+,.2f}[/{_pnl_color(v)}]"


def _dur_label(sec: float) -> str:
    """`45s` / `3m12s` / `1h04` — a duration, not a time-since (_age_label)."""
    if sec < 60:
        return f"{int(sec)}s"
    if sec < 3600:
        m, s = divmod(int(sec), 60)
        return f"{m}m{s:02d}s"
    h, m = divmod(int(sec // 60), 60)
    return f"{h}h{m:02d}"


def _t_cell(w: dict, now: float) -> str:
    """One column, one axis: how far this window is from its own end — the T-
    countdown while it is still open (the arms table's column, verbatim), the
    age since it closed after that. A live arm and a decided trade are asking
    the same question from opposite sides of the same instant."""
    end_ts = float(w.get("end_ts") or 0.0)
    if not end_ts:
        return "[dim]—[/dim]"
    if end_ts > now:
        return _countdown_markup(w.get("slug", ""), now)
    return _age_label(now - end_ts)


def _live_state_text(e: dict) -> str:
    """The arms table's `state` cell: the compact gate reason for a gated arm,
    the regime label plus safety/brake badges for an armed one. Every branch
    tolerates a missing or half-built eval — an engine restart mid-watch leaves
    last_eval None or partial, and the row must keep painting."""
    state = e.get("state", "?")
    if state == "gated":
        return f"[yellow]gated[/yellow]  {_gated_reason_compact(e.get('reason'), e)}"
    if state == "armed":
        mode = _mode_text(e)
        head = "[green]armed[/green]" + (f" [dim]{mode}[/dim]" if mode != "—" else "")
        sides = e.get("sides") or []
        badges = "  ".join(x for x in (_safety_rich(sides, e.get("p_up")),
                                        _brake_rich(sides)) if x)
        return f"{head}  {badges}" if badges else head
    # flip/quiesce ARE the mode, and an unknown state is printed as itself
    # rather than swallowed.
    return str(state)


def _state_cell(w: dict, now: float) -> str:
    """What this row is about right now: the ENGINE's posture while the window
    is live, the position's exposure once the arm has left it behind.

    The stage-repurposing that let the arms table fold in — an armed window has
    no exposure to report and a rolled-away one has no engine reading it, so
    one column carries whichever of the two exists.
    """
    if w.get("live"):
        return _live_state_text(w.get("eval") or {})
    start = float(w.get("entry_ts") or 0.0)
    if not start:
        return "[dim]—[/dim]"
    # Exposure time: what polymarket.effectiveness grades on, and what the
    # dashboard could never show while these were two tables.
    end = now if w.get("won") is None else (float(w.get("exit_ts") or 0.0)
                                            or float(w.get("end_ts") or 0.0) or now)
    return f"[dim]held[/dim] {_dur_label(max(end - start, 0.0))}"


def _read_cell(w: dict) -> str:
    """The model's case while the window is live — banked-vs-cushion evidence,
    p↑ and ρ, the three arms-table columns that only mean anything before a
    verdict — and what we actually took once it is settled."""
    if w.get("live"):
        e = w.get("eval") or {}
        bits = []
        if e.get("banked_bp") is not None and e.get("cushion_bp") is not None:
            bits.append(_evidence_markup(e))
        if "p_up" in e:
            bits.append(f"[dim]p↑{e['p_up']:.2f}[/dim]")
        if "rho" in e:
            bits.append(f"[dim]ρ{e['rho']:+.2f}[/dim]")
        # A pre-model eval (quiesce, or a half-built one after a restart) has
        # none of these; "— p↑— ρ—" is noise where "—" is the fact.
        return " ".join(bits) if bits else "[dim]—[/dim]"
    shares, ts = w.get("shares"), float(w.get("entry_ts") or 0.0)
    if not shares or not ts:
        return "[dim]—[/dim]"
    return f"{shares:,.0f}sh [dim]in[/dim] {time.strftime('%H:%M', time.localtime(ts))}"


def position_odds(odds: dict | None, w: dict) -> float | None:
    """The CURRENT mark for the side this window holds, or None.

    `odds` is polymarket.positions.current_odds' map — fetched on the watch
    worker's slow cadence and allowed to be empty (a failed or not-yet-run
    fetch), which is why every caller must tolerate None rather than reach
    for a fallback price of its own.
    """
    if not odds:
        return None
    return odds.get(((w.get("slug") or ""), (w.get("side") or "").lower()))


def _odds_cell(w: dict, odds: dict | None) -> str:
    """`0.99` — what the held side is worth RIGHT NOW, beside what we paid.

    Green once the side we're holding is the favourite, red once it isn't:
    on a binary that settles to 0 or 1 this is the honest read on a riding
    position, and it is the number the dashboard could not answer at all
    ("what are the odds now?") while a window sat undecided.
    """
    px = position_odds(odds, w)
    if px is None:
        return "[dim]—[/dim]"
    style = "green" if px >= 0.5 else "red"
    return f"[{style}]{px:.2f}[/{style}]"


def _mark_cell(w: dict, odds: dict | None) -> str:
    """What the held side is worth now — the live mark while it rides, and the
    binary's own 1.00/0.00 once it has settled. One number, walked all the way
    to the end of the lifecycle instead of going blank at the verdict."""
    won = w.get("won")
    if won is None:
        return _odds_cell(w, odds)
    style = "dim" if w.get("est") else ("green" if won else "red")
    return f"[{style}]{1.0 if won else 0.0:.2f}[/{style}]"


def _position_cell(w: dict, odds: dict | None) -> str:
    """`up 0.97→0.99` — the side we hold, what the average dollar paid for it,
    and what it is worth now. `—` for a window nothing is held in, which is
    every live row until its arm fires."""
    px = w.get("entry_px")
    if not px:
        return "[dim]—[/dim]"
    return f"{w.get('side') or '?'} {px:.2f}[dim]→[/dim]{_mark_cell(w, odds)}"


def _money_cell(w: dict) -> str:
    """Notional in this window, plus any resting maker bid sharing the cell.

    Dim while the figure is the ENGINE's own committed number on a window the
    wallet has not graded — it is a real figure and belongs on screen the
    instant the engine reports it, but the wallet is what makes it true.
    """
    money = f"${_zero(float(w.get('notional') or 0.0)):,.2f}"
    resting = float(w.get("resting") or 0.0)
    if resting > 0.005:
        money += f" [cyan]◇${resting:,.0f}[/cyan]"
    if not w.get("held") and w.get("won") is None:
        return f"[dim]{money}[/dim]"
    return money


def live_rows(arms: dict | None) -> list[dict]:
    """The fleet's CURRENT windows as window rows — the head of every chain.

    An armed window has no wallet row until it fires, so the scoreboard cannot
    see it at all; the engine's /status is the ONLY source for the ○/⊘ stage.
    Money here is the engine's own committed figure and is used only where the
    wallet has nothing to say (see window_rows) — the wallet stays ground truth
    everywhere it has an opinion.

    Reads the already-fetched status mapping; never fetches, never calls the
    engine's control plane.
    """
    out: list[dict] = []
    for slug, a in (arms or {}).items():
        parsed = updown_slugs.parse_updown_slug(slug)
        if parsed is None:
            continue  # not a window: this table's unit is one, so it has no row
        a = a if isinstance(a, dict) else {}
        e = a.get("eval")
        e = e if isinstance(e, dict) else {}
        committed = e.get("committed", a.get("filled_usdc")) or 0.0
        out.append({"slug": slug, "won": None, "pnl": None, "est": False,
                    "end_ts": float(parsed["end"]), "notional": committed,
                    "entry_px": None, "side": None, "shares": None, "held": False,
                    "state": e.get("state") or "armed", "live": True, "eval": e,
                    "flags": _arm_flags(a),
                    "resting": a.get("resting_usdc") or e.get("resting") or 0.0})
    return out


# What the engine adds to a window the wallet already priced: posture, never
# money or a verdict.
_LIVE_OVERLAY = ("live", "state", "eval", "flags", "resting")


def window_rows(sb: dict | None, arms: dict | None = None,
                limit: int | None = None) -> list[dict]:
    """Every window the fleet is in or has been in, newest window-end first.

    ONE row per window (keyed by slug) merged from the two sources that know
    about one: the scoreboard's riding/decided windows, and the engine's live
    arms. A window both know about is a single row — the wallet's money and
    verdict, the engine's posture — so a fire fills the row already on screen
    instead of adding a second one below it.

    Rows are COPIED before they are annotated: the scoreboard object is shared
    with the risk header's riding totals, and marking it up in place would
    quietly change what another panel is reading.
    """
    sb = sb or {}
    rows: list[dict] = []
    by_slug: dict[str, dict] = {}
    for src, held in ((sb.get("riding_windows"), True), (sb.get("windows"), False)):
        for r in src or []:
            row = dict(r)
            row["held"] = held
            rows.append(row)
            by_slug.setdefault(row.get("slug"), row)
    for lr in live_rows(arms):
        cur = by_slug.get(lr["slug"])
        if cur is None:
            rows.append(lr)
            by_slug[lr["slug"]] = lr
            continue
        # Posture only. The wallet already has money and a verdict for this
        # window and outranks the engine on both.
        cur.update({k: lr[k] for k in _LIVE_OVERLAY})
    rows.sort(key=lambda r: float(r.get("end_ts") or 0.0), reverse=True)
    return rows if limit is None else rows[:limit]


def windows_title(sb: dict | None, arms: dict | None = None,
                  shown: int | None = None) -> str:
    """`windows · 2 live · 1 riding · last 12 decided`, or `windows · 8 of 15 ·
    ...` when the panel is painting fewer rows than there are.

    Counted in the table's own top-to-bottom order, and retention STATED
    twice over: `last` marks the scoreboard's own decided cap
    (cli_crypto_stats.WINDOWS_SHOWN), `N of M` the panel's view cap. A cap the
    operator can't see is indistinguishable from a dropped window, which is the
    confusion this panel exists to end.
    """
    rows = window_rows(sb, arms)
    n_dec = sum(1 for r in rows if r.get("won") is not None)
    n_ride = sum(1 for r in rows if _stage(r) == "riding")
    held = f"{len(rows) - n_dec - n_ride} live · {n_ride} riding · last {n_dec} decided"
    if shown is None or shown >= len(rows):
        return f"windows · {held}"
    return f"windows · {shown} of {len(rows)} · {held}"


def build_windows_table(sb: dict | None, now: float,
                        arms: dict | None = None,
                        limit: int | None = None,
                        odds: dict | None = None) -> Table:
    """The dashboard's ONE table — every window the fleet is in or has been
    in, newest first, each cell reading whichever of its two meanings the
    row's stage calls for. See the design note above.

        ⊘  xrp 5m ⟳≈   3:41  gated  margin -4.9 vs 6.0bp   -3.2/9.3bp p↑0.44 ρ-0.10   —              $0.00       —
        ◆  bnb 5m       1m   held 3m12s                    20sh in 14:38              up 0.97→0.41   $19.44   riding
        ✓  eth 5m      10m   held 2m45s                    10sh in 14:31              down 0.95→1.00 $95.00   +12.30

    Renders EVERY row it is handed (the caller caps via `limit`), so a window
    the scoreboard knows about is always on screen somewhere — the guarantee
    the chip strip could not make, because it only ever carried decided
    windows. A riding row has to survive the arm rolling AWAY from that
    window: the position stays ours until the wallet grades it, and this is
    where the operator watches it land.

    Every cell tolerates a missing or half-built eval — an engine restart
    mid-watch leaves last_eval None or partial, and the row must keep painting
    rather than raise.

    `arms` is the engine's already-fetched /status arms mapping: it supplies
    the live head and each live row's posture, and nothing else. `odds` is the
    optional current-mark map (polymarket.positions), fetched on the watch
    worker's slow cadence; absent, empty or stale it degrades to `—` on the
    right of the `position` cell and changes nothing else.
    """
    # Natural widths, not expand=True: the columns are sized to their longest
    # value, and stretching them across a 200-column terminal puts a metre of
    # whitespace between a window's size and its P&L.
    t = Table(expand=False, pad_edge=False)
    for col, justify, width in _WINDOWS_COLUMNS:
        t.add_column(col, justify=justify, width=width, no_wrap=True, overflow="ellipsis")
    rows = window_rows(sb, arms, limit)
    for w in rows:
        t.add_row(
            _arm_cell(w),
            _t_cell(w, now),
            _state_cell(w, now),
            _read_cell(w),
            _position_cell(w, odds),
            _money_cell(w),
            _window_pnl_cell(w),
        )
    if not rows:
        # In the state cell, the widest one: a placeholder that ellipsizes is
        # worse than a short honest one, and this is the only column with room.
        t.add_row("—", "—", "[dim]no windows[/dim]", "—", "—", "—", "—")
    return t


def _arm_label(slug: str) -> str:
    """`btc 5m` — symbol + duration only. The window's own start clock (which
    the tape lines carry) would be redundant: the `t` column counts down to,
    or up from, that same window's end."""
    parts = slug.split("-")
    return f"{parts[0]} {parts[2]}" if len(parts) >= 3 else slug


_FLAG_LEGEND = "⟳ roll · ≈ stream-fed · ◇ maker bid"


def _arm_flags(a: dict) -> str:
    """`⟳≈◇` — the arm's roll/feed/maker markers (_FLAG_LEGEND), in that
    order. "·" keeps the roll slot occupied so the feed and maker markers never
    shift place between rows."""
    return (("⟳" if a.get("roll") else "·")
            + ("≈" if a.get("feed") == "rtds" else "")
            + ("◇" if a.get("maker_bid") else ""))


def _arm_cell(w: dict) -> str:
    """`◆ btc 5m ⟳≈◇` — the row's whole identity: where the window is in its
    life, which series it belongs to, and (while some arm is actually on it)
    that arm's flags. A rolled-away window is just glyph + series: the markers
    describe a live arm, and there is no longer one here.

    Glyph and flags ride in this cell rather than columns of their own because
    both are properties of the row — and because a 1-wide glyph column is the
    first thing Rich squeezes out of existence on a narrow console.
    """
    label = _arm_label(w.get("slug", ""))
    flags = w.get("flags") or ""
    cell = f"{_stage_cell(w)} {label}"
    return f"{cell} [dim]{flags}[/dim]" if flags else cell


def engine_row(status: dict | None) -> tuple | None:
    """The arms table's red "engine unreachable or no arms" placeholder, kept
    alive as a header row after the fold, or None while something is armed.

    It has to live somewhere: with no arms there are no live rows for the
    windows table to hang it on, the decided tail below keeps painting exactly
    as it did an hour ago, and the exposure row would read a confident
    "committed $0.00" — which is the one reading an unreachable engine and an
    idle fleet must never share.
    """
    if (status or {}).get("arms"):
        return None
    return ("engine", "[red]unreachable or no arms[/red]",
            "[dim]nothing is armed right now[/dim]", "")


def _cbreak_stdin() -> tuple[int, list] | None:
    """Put stdin in cbreak (no line buffering, no echo) so a single 'q'
    keypress is visible without Enter — SIGINT stays enabled (cbreak, unlike
    raw mode, leaves ISIG alone), so Ctrl-C still works. None when stdin
    isn't a real tty (piped input, a test harness) — termios would just raise.
    """
    if not sys.stdin.isatty():
        return None
    fd = sys.stdin.fileno()
    old = termios.tcgetattr(fd)
    tty.setcbreak(fd)
    return fd, old


def _restore_stdin(saved: tuple[int, list] | None) -> None:
    """Undo _cbreak_stdin — must run even on an exception, or the shell is
    left echo-less after the dashboard exits."""
    if saved is None:
        return
    fd, old = saved
    try:
        termios.tcsetattr(fd, termios.TCSADRAIN, old)
    except Exception:
        pass


def _poll_key(timeout: float = 0.0) -> str | None:
    """The waiting keypress (lowercased) or None. `timeout` is the select
    wait in seconds; 0 (the default) polls without blocking at all.

    os.read on the raw fd, NEVER sys.stdin.read — see docs/LESSONS.md#L30.
    """
    if not sys.stdin.isatty():
        return None
    try:
        fd = sys.stdin.fileno()
        ready, _, _ = select.select([fd], [], [], timeout)
        if not ready:
            return None
        ch = os.read(fd, 1)
        return ch.decode(errors="ignore").lower() or None
    except Exception:
        return None


def _wait_key(timeout: float) -> str | None:
    """Wait up to `timeout` for one keypress — the watch loop's ONLY pacing.

    The loop sleeps inside select(), so a keypress wakes it within
    microseconds instead of sitting in the tty buffer behind a sleep(1) and
    whatever network work followed it. With no tty (piped input, a test
    harness) there's nothing to select on, so it just paces the loop.
    """
    if not sys.stdin.isatty():
        time.sleep(timeout)
        return None
    return _poll_key(timeout)


_SB_EMPTY_SLIDING = {"wins": 0, "losses": 0, "net": 0.0, "rolls": 0, "estimated": 0}


_SB_EMPTY = {"wins": 0, "losses": 0, "net": 0.0, "rolls": 0, "series": {}, "cal": {},
             "estimated": 0, "riding_n": 0, "riding_usd": 0.0, "windows": [],
             "riding_windows": [], "sliding": dict(_SB_EMPTY_SLIDING)}


# ---------- the controls modal ----------

# Every key the dashboard reacts to: (keypress, label, one-line explanation).
# A None keypress is a signal, not a key, and has no handler to match.
# THE contract cli_crypto_watch.handle_key implements — the modal renders this
# list and a test drives both directions, so a new key that never reaches the
# panel (or a panel line with nothing behind it) fails rather than ships.
WATCH_KEYS = (
    ("h", "h", "show or hide this panel"),
    ("\x1b", "esc", "close this panel"),
    ("q", "q", "close this panel; quit the dashboard when it is closed"),
    (None, "ctrl-c", "quit from anywhere, even mid-fetch"),
)

# Keep in sync with cli_crypto_watch's cadence constants.
_REFRESH_LINE = "tape 1s · engine 2s · stats 10s · odds 30s · balance 60s"

_HELP_MODAL_W = 78  # readable prose width; clamped to the terminal by the caller


def build_help_modal(width: int | None = None):
    """The `h` overlay: every key the watch accepts, the glyph legends, and the
    refresh cadences.

    A FOREGROUND panel, not a strip — it takes the screen while it is open,
    which is what let the legends grow past the single line the old three-row
    controls slot could hold. The `--since` explanation is back on screen here
    for the same reason: it changes what the header's `recent` row means, and
    `--help` is not reachable without leaving the dashboard.

    Nothing here fetches. The data workers keep running underneath, so
    dismissing the panel restores a live frame rather than a frozen one.
    """
    from rich.console import Group
    from rich.panel import Panel
    from rich.text import Text

    keys = Table(box=None, pad_edge=False, padding=(0, 2), show_header=False)
    keys.add_column("key", justify="right", width=7, style="bold")
    keys.add_column("what", justify="left", overflow="fold")
    for _press, label, desc in WATCH_KEYS:
        keys.add_row(label, desc)

    legend = Table(box=None, pad_edge=False, padding=(0, 2), show_header=False)
    legend.add_column("what", justify="right", width=7, style="dim")
    legend.add_column("meaning", justify="left", overflow="fold")
    legend.add_row("stage", f"[cyan]{_STAGE_LEGEND}[/cyan] [dim]— it leads "
                            "every `arm` cell[/dim]")
    legend.add_row("flags", f"[cyan]{_FLAG_LEGEND}[/cyan] [dim]— trailing the "
                            "arm that is on this window right now[/dim]")
    # The columns that change meaning with the stage are the whole trick of the
    # one-table fold, so the modal is where they are spelled out.
    legend.add_row("columns", "[dim]state · read: the engine's posture while a "
                              "window is live (gate reason, evidence, p↑, ρ), "
                              "what we took once it is settled[/dim]")
    legend.add_row("", "[dim]position: entry→mark, and →1.00/0.00 once the "
                       "binary settles · $ dims while it is the engine's own "
                       "figure · ~ marks an estimated P&L[/dim]")
    legend.add_row("refresh", f"[dim]{_REFRESH_LINE}[/dim]")
    legend.add_row("--since", "[dim]moves the header's `recent` floor only "
                              "(default 6h) — all-time, riding and the windows "
                              "table always walk the full history[/dim]")
    body = Group(keys, Text(""), legend)
    return Panel(body, title="[bold]controls[/bold]", title_align="left",
                 subtitle="[dim]h · esc · q to close[/dim]", subtitle_align="right",
                 border_style="cyan",
                 width=min(_HELP_MODAL_W, width) if width else _HELP_MODAL_W)


# The top box is a LABEL/VALUE GRID, the same shape `pmt crypto stats` uses
# (stats_render._HDR_*), so a figure sits in the same place in both views.
# Widths are fixed and sized to the LONGEST value each column can hold, so the
# columns never re-flow tick to tick and a number never loses digits to an
# ellipsis: v1 "committed $12,345.67" (20), v2 "$12,345.67 un-decided" (22),
# v3 "riding 12 windows $1,234.56" (27). Total 9+20+22+27 + padding + border =
# 88 columns, well inside any terminal a full-screen dashboard runs in.
_HEAD_LABEL_W = 9
_HEAD_V1_W = 20
_HEAD_V2_W = 22
_HEAD_V3_W = 27


def _record_cell(wins: int, losses: int) -> str:
    """`40W-6L (87.0%)` — one W-L cell, so the recent row and the all-time row
    below it are read by eye rather than by arithmetic.

    One decimal, matching `pmt crypto stats`: rounded to "92%" a headline rate
    reads as level with a 92.5% break-even bar it is in fact under.
    """
    n = wins + losses
    wr = f"{wins / n * 100:.1f}%" if n else "—"
    return f"[bold]{wins}W-{losses}L[/bold] [dim]({wr})[/dim]"


def _pnl_cell(net: float) -> str:
    return f"[dim]P&L[/dim] [{_pnl_color(_zero(net))}]{_zero(net):+,.2f}[/]"


def header_rows(snap: dict, render_err: str | None = None) -> list[tuple]:
    """The top panel's rows as `(label, v1, v2, v3)` tuples.

    Row order is the order the question is asked: how are we doing lately,
    how are we doing overall, what is at risk right now, is the engine there,
    is the feed alive, and what broke. A row with nothing to say is dropped,
    never padded with a zero — which is why the panel's height is
    `header_height`, not a constant.
    """
    sb = snap.get("sb") or {}
    sliding = sb.get("sliding") or _SB_EMPTY_SLIDING
    est = sliding.get("estimated", 0) or 0
    rolls = f"[dim]{sliding['rolls']} rolls[/dim]"
    if est:
        # `~` is the report-wide mark for an imputed figure (a gamma-confirmed
        # win whose redeem row hasn't posted); it belongs beside the count it
        # qualifies, not on a line of its own.
        rolls += f" [dim]· ~{est} est[/dim]"
    bal = snap.get("bal")
    cap = (f"[dim]capital[/dim] [bold]${bal['total']:,.2f}[/bold]" if bal
           else "[dim]capital …[/dim]")
    rows = [
        ("recent", _record_cell(sliding["wins"], sliding["losses"]),
         _pnl_cell(sliding["net"]), rolls),
        ("all-time", _record_cell(sb.get("wins", 0), sb.get("losses", 0)),
         _pnl_cell(sb.get("net", 0.0)), cap),
    ]
    rows += exposure_rows(snap.get("status"), sb)
    for extra in (engine_row(snap.get("status")), feed_row(snap.get("status"))):
        if extra:
            rows.append(extra)
    return rows


def header_note(snap: dict, render_err: str | None = None):
    """The failure line, or None. A Text (not a grid row) because it is prose
    the full width of the panel, not a value in a 20-column money field: a
    traceback folded into one narrow cell costs five rows and says less.
    Clipped to one line, so the panel's height stays predictable."""
    note = render_err or snap.get("err")
    if not note:
        return None
    from rich.text import Text

    # Labelled and indented to the grid's own value column, so it reads as one
    # more row of the same box.
    t = Text.from_markup(f"[dim]{'note':<{_HEAD_LABEL_W}}[/dim]  [red dim]{note}[/]")
    t.no_wrap, t.overflow = True, "ellipsis"
    return t


def header_height(snap: dict, render_err: str | None = None) -> int:
    """Terminal rows the header panel will occupy — its rows, the failure line
    if there is one, plus the border. The watch layout sizes its head slot from
    this, the same way it sizes the arms slot from the arm count."""
    return (len(header_rows(snap, render_err))
            + (1 if header_note(snap, render_err) is not None else 0) + 2)


def _header_subtitle(snap: dict) -> str:
    """Scoreboard age + wall clock, on the panel's bottom border.

    Age renders "—" (not "0s ago") before the first wallet walk lands: an
    honest cue beats a confident zero. It belongs to the panel, not to a grid
    row, because it qualifies every number in the box at once.
    """
    if snap.get("sb_fetched_at") is None:
        age = "[dim]stats —[/dim]"
    else:
        age_s = time.time() - snap["sb_fetched_at"]
        age = f"[{'yellow' if age_s > 30 else 'dim'}]stats {age_s:.0f}s ago[/]"
    stale = " [yellow dim]· stale[/]" if snap.get("sb_stale") else ""
    return f"{age}{stale} [dim]· {time.strftime('%H:%M:%S')}[/dim]"


def build_header_panel(snap: dict, floor_label: str, render_err: str | None):
    """The dashboard's top panel: the recent pulse, the all-time ledger, live
    exposure, the settlement feed, and whatever went wrong last frame — one
    aligned label/value grid.

    `snap` is one WatchState.read() mapping. The `recent` row carries the
    --since-floored pulse; `all-time` comes off the same snapshot's
    full-history grade, in the same columns, so the two compare by eye.
    """
    from rich.console import Group
    from rich.panel import Panel

    t = Table(box=None, pad_edge=False, padding=(0, 1), show_header=False)
    t.add_column("label", justify="left", width=_HEAD_LABEL_W, no_wrap=True,
                 overflow="ellipsis", style="dim")
    # overflow="fold", not "ellipsis": on a terminal too narrow for the grid a
    # value must wrap intact rather than lose its last digits — losing digits
    # off a money figure is the one failure this box may not have.
    for w in (_HEAD_V1_W, _HEAD_V2_W, _HEAD_V3_W):
        t.add_column(justify="left", width=w, overflow="fold")
    for row in header_rows(snap, render_err):
        t.add_row(*row)
    note = header_note(snap, render_err)
    body = t if note is None else Group(t, note)
    return Panel(body, title=f"[bold]updown fleet[/bold] [dim]· {floor_label}[/dim]",
                 title_align="left", subtitle=_header_subtitle(snap),
                 subtitle_align="right", border_style="cyan")


