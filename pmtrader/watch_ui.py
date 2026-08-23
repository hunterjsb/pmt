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
"""

from __future__ import annotations

import io
import json
import re
import time

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

_DASH = "[dim]—[/dim]"  # what a cell with nothing honest to say prints


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


def _paint(text: str, style: str, ansi: bool) -> str:
    """One coloured fragment as Rich markup or as ANSI. The tables speak Rich
    and the tape speaks ANSI; every badge below is written once and painted
    for whichever is asking."""
    if not text:
        return ""
    if not ansi:
        return f"[{style}]{text}[/{style}]"
    words = style.split()
    return click.style(text, fg=next((w for w in words if w not in ("bold", "dim")), None),
                       bold="bold" in words, dim="dim" in words)


def _safety_badge(sides: list[dict], p_up: float | None, ansi: bool = False) -> str:
    """`saf +0.90/-0.30`, green once the model's favoured side clears theta."""
    return _paint(_safety_text(sides),
                  "green" if _safety_is_strong(sides, p_up) else "dim", ansi)


def _brake_badge(sides: list[dict], ansi: bool = False) -> str:
    """`up:safety down:distrust` — every side whose fire is currently blocked,
    coloured by which brake did it."""
    return " ".join(_paint(f"{side}:{b}", _BRAKE_COLOR.get(b, "white"), ansi)
                    for side, b in _brake_sides(sides))


def _has_rtds_arm(arms: dict | None) -> bool:
    """True when some arm is actually reading the settlement stream."""
    return any(isinstance(a, dict) and a.get("feed") == "rtds"
               for a in (arms or {}).values())


def rtds_cells(h: dict | None) -> list[str]:
    """The stream-health segments, as cells: `[head, "3.0/s", "age 0s", ...]`.

    The head carries its own colour (green connected / red down); the rest are
    raw, so a caller can dim them or lay them out in a grid without having to
    re-derive a number. One computation, two presentations.
    """
    if not h or not (h.get("started") or h.get("events")):
        return []
    age = h.get("last_event_age_s")
    head = "[green]rtds[/green]" if h.get("connected") else "[red]rtds DOWN[/red]"
    bits = [f"{h.get('events_per_s', 0):.1f}/s",
            f"age {age:.0f}s" if age is not None else "no events yet",
            f"{h.get('consumers', 0)} arms"]
    if h.get("reconnects"):
        bits.append(f"{h['reconnects']} reconnects")
    if h.get("err") and not h.get("connected"):
        bits.append(str(h["err"])[:48])
    return [head] + bits


def _one_line(cells: list[str]) -> str:
    """Health cells as one line: coloured head, the rest dimmed and dotted."""
    if not cells:
        return ""
    return f"{cells[0]} [dim]{' · '.join(cells[1:])}[/dim]"


def _rtds_rich(h: dict | None) -> str:
    """One line for the shared settlement-stream supervisor, or "" when nothing
    has ever armed on it.

    Worth its own line rather than a per-arm field: ONE socket sits behind
    every stream-fed arm, so when it drops they all gate at once and their
    per-arm reasons all say the same thing. Red the moment it is not
    connected — a dark stream is a fleet-wide event.
    """
    return _one_line(rtds_cells(h))


def rtds_line_cells(status: dict | None) -> list[str]:
    """rtds_cells, but empty unless an arm is reading the stream right now.

    The socket is opened lazily by the first rtds arm and outlives it: once
    every stream-fed arm has rolled to binance or retired, its health is no
    longer a fact about the fleet, and a line that never goes away stops being
    read.
    """
    status = status or {}
    if not _has_rtds_arm(status.get("arms")):
        return []
    return rtds_cells(status.get("rtds"))


def _rtds_line(status: dict | None) -> str:
    return _one_line(rtds_line_cells(status))


_MARGIN_RE = re.compile(r"projected margin ([+-]?\d+\.?\d*)bp inside (\d+\.?\d*)bp")


def _margin_guard(e: dict | None) -> tuple[float, float] | None:
    """(margin_bp, guard_bp) off a basis-guard gate, or None for a gate that
    has no such numbers.

    THE one place the two sources are reconciled. The engine emits the
    structured fields and they win; the regex is the legacy path for an eval
    from a build that predates them. It parses the same sentence the fields
    are formatted from, so the two cannot disagree, and a reword costs the
    regex and not the fields.
    """
    e = e or {}
    margin, guard = e.get("margin_bp"), e.get("guard_bp")
    if margin is not None and guard is not None:
        return float(margin), float(guard)
    m = _MARGIN_RE.search(e.get("reason") or "")
    return (float(m.group(1)), float(m.group(2))) if m else None


def _gated_reason_compact(reason: str | None, e: dict | None = None) -> str:
    """`margin -4.9 vs 6.0bp` for a basis-guard gate; every other gate falls
    back to its raw (truncated) reason so nothing gets swallowed."""
    mg = _margin_guard(dict(e or {}, reason=reason) if reason is not None else e)
    if mg:
        return f"margin {mg[0]:+.1f} vs {mg[1]:.1f}bp"
    return reason[:60] if reason else "gated"


_GATE_NUM_W = 13  # "  -4.0/ 6.0bp" — signed margin over its guard, fixed width


def _gate_margin(r: dict) -> str:
    """`  -4.0/ 6.0bp` in a field of its own, blank for a gate with no basis
    numbers (theta, feed stale, elapsed-percent).

    Fixed width and fixed column: how far each arm is from its own guard is a
    column to scan down, not a number to hunt for inside a sentence that moves
    with the arm's name.
    """
    mg = _margin_guard(r)
    return f"{mg[0]:+6.1f}/{mg[1]:4.1f}bp" if mg else ""


def _gate_reason(r: dict) -> str:
    """What the gate says, minus the numbers the margin field already shows."""
    reason = r.get("reason") or "?"
    if _margin_guard(r) and ":" in reason:
        reason = reason.split(":", 1)[0]
    return reason[:60]


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
        return _DASH
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
        return _DASH
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


_TAPE_TAG_WIDTH = 9   # "FIRE DOWN"/"FLIP DOWN"/etc — the widest natural tag
_TAPE_AGG_WIDTH = 15  # "→23:43:20 ×9999" — the widest a collapsed line's cell gets


def _tape_tag(text: str) -> str:
    """Left-pad an event tag to a fixed width so the fields after it land at
    the same column regardless of event type (FIRE/EXIT/eval/gated/ROLL)."""
    return f"{text:<{_TAPE_TAG_WIDTH}}"


def _tape_agg(n: int, t_end: float = 0.0) -> str:
    """`→23:43:20 ×12` — when the last record this line stands for landed, and
    how many of them there were. Blank (but still occupied) for a line that
    collapsed nothing.

    Span and count share ONE fixed cell, right after the line's own clock, so
    "when + how many" is a single glance and both read at the same offset on
    every line type. The clock to its left is the run's FIRST record, so the
    pair brackets exactly what the line covers. It may not trail the body: a
    variable-width body puts it at a different column on every line.

    Plain text, never styled: it is concatenated into lines that are styled
    as a whole, and a nested reset would drop the rest of the line's color.
    """
    cell = f"→{_hms(t_end)} ×{n}" if n > 1 else ""
    return f"{cell:<{_TAPE_AGG_WIDTH}}"


def _hms(t: float) -> str:
    return time.strftime("%H:%M:%S", time.localtime(t))


def _zero(v: float) -> float:
    """Snap sub-cent float drift to 0.0 so committed/exposure never render a
    phantom "-$0.00" — a residual after fills settle to ~zero (mirrors
    stats_render._zeroed; same threshold)."""
    return 0.0 if abs(v) < 0.005 else v


def _tape_head(r: dict, n: int = 1, t0: float | None = None) -> str:
    """`HH:MM:SS →HH:MM:SS ×12  slug-padded-to-14` — the fixed-width prefix
    shared by every tape-line renderer, so eval/fire/gated/roll/exit lines all
    column-align. 14 is the widest display() form (e.g. "doge 60m 23:40").

    A collapsed line opens on its run's FIRST record and closes the span in
    the aggregation cell; an uncollapsed one prints its own clock and leaves
    that cell blank but occupied.
    """
    t1 = r.get("t", 0)
    return (f"{_hms(t1 if t0 is None else t0)} {_tape_agg(n, t1)}"
            f" {_tape_slug(r.get('slug', '')):<14}")


def _tape_render(line: str) -> str | None:
    try:
        r = json.loads(line)
    except ValueError:
        return None
    return _render_record(r, line)


def _render_record(r: dict, raw: str, n: int = 1, t0: float | None = None) -> str:
    """One parsed tape record as a rendered line; `raw` is the fallback for an
    event this build doesn't know (never swallow a record we can't name).

    `n` is how many records this line stands for and `t0` when the first of
    them landed: both render into the fixed aggregation column (_tape_agg), so
    every line type carries its span and count in the same place. n == 1
    leaves that column blank but occupied.
    """
    head = _tape_head(r, n, t0)

    def tagged(tag: str, body: str, **tag_style) -> str:
        """`head  TAG  body` — the shape EVERY tape line has, built in one
        place so no event type can drift out of the column geometry."""
        label = click.style(_tape_tag(tag), **tag_style) if tag_style else _tape_tag(tag)
        return f"{head} {label}{body}"

    def money(v: float) -> str:
        return f"${_zero(v):,.2f}".rstrip("0").rstrip(".")

    ev = r.get("ev")
    if ev == tape.EV_FIRE:
        tag = {"flip": "FLIP", "spec": "SPEC"}.get(r.get("mode", "safe"), "FIRE")
        pct = f"  {r['elapsed_frac'] * 100:.0f}% thru" if "elapsed_frac" in r else ""
        return tagged(
            f"{tag} {r['side'].upper()}",
            f"{r['size']:g}sh @ {r['ask']:.2f}  fair {r['fair']:.4f}"
            f"  {r['net'] * 100:+.1f}¢  ρ{r['rho']:+.2f}  {money(r['committed'])} in{pct}",
            fg="green", bold=True)
    if ev == tape.EV_EXIT:
        return tagged(f"EXIT {r['side'].upper()}",
                      f"{r['size']:g}sh @ bid {r['bid']:.2f}  fair {r['fair']:.4f}",
                      fg="red", bold=True)
    if ev == tape.EV_EVAL:
        sides = r.get("sides") or []
        best = _best_side(sides)
        book = (f"{best['side']} @ {best['ask']:.2f} {best['net'] * 100:+.1f}¢"
                if best else "no book")
        tags = click.style("  BANKED", fg="cyan") if r.get("banked_decided") else ""
        if r.get("maker_rest") is not None:
            tags += click.style(f"  ◇RESTING @{r['maker_rest']:.3f}", fg="cyan", bold=True)
        elif r.get("maker_candidate"):
            tags += click.style("  ◇maker-candidate", fg="cyan")
        body = tagged("eval", f"p↑{r['p_up']:.4f}  {book}"
                              f"  ρ{r['rho']:+.2f}  {money(r['committed'])} in")
        extras = "  ".join(x for x in (_safety_badge(sides, r.get("p_up"), True),
                                       _brake_badge(sides, True)) if x)
        return click.style(body, dim=True) + tags + ("  " + extras if extras else "")
    if ev == tape.EV_GATED:
        def ask(v: float | None) -> str:
            return f"{v:.2f}" if v is not None else "—"
        asks = f"  up {ask(r['up_ask'])}/dn {ask(r['dn_ask'])}" if "up_ask" in r else ""
        return click.style(
            tagged("gated", f"{_gate_margin(r):<{_GATE_NUM_W}}  {_gate_reason(r)}{asks}"),
            fg="yellow", dim=True)
    if ev == tape.EV_ROLL:
        return click.style(tagged("ROLL", f"next window armed (${r['size']:g})"), fg="cyan")
    if ev == tape.EV_CLEANUP:
        # Tagged like every other event: an untagged line was the one hole in
        # the tape's fixed column geometry.
        return click.style(tagged("closed", "── window closed ──"), dim=True)
    return raw.rstrip()


# ---------- tape run-collapsing ----------
#
# On a quiet tape almost every record repeats: each arm prints the same gate,
# or an eval whose numbers haven't moved, every tick — and the events that
# matter (fires, exits, brakes, rolls) drown in them. A "run" is consecutive
# records of one shape whose material state is unchanged; it renders as ONE
# line, updated in place, carrying the FRESHEST values plus ×N and the span it
# covers. Collapsing may hide repetition; it must never hide a transition,
# which is what the tolerances below are sized for and what every rule's
# signature exists to catch.
#
# ONE grouping rule for evals and gates alike: a lane per arm, a signature that
# is the line's own discrete state, anchored tolerances the width of the
# numbers as displayed, and a lifetime that ends on the arm's next contrary
# record. A fleet-wide gate lane would read as a second mechanism on the same
# screen, and would break every time another arm spoke between two gates.

_NUM_RE = re.compile(r"[+-]?\d+(?:\.\d+)?")

# How deep under later output a live run's line may sit and still be updated.
# Sized for a fleet-wide roll: every arm emits its window-close, THEN every arm
# emits its roll, so the first arm's line is one-per-arm deep by the time its
# own roll arrives to merge into it. 16 leaves headroom over the 8 arms that
# have actually run at once.
_OWN_LOOKBACK = 16


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
    same run, and every lane is per-arm so interleaved arms don't tear each
    other's runs apart. Within a lane the run continues while `signature` is
    identical AND every `metrics` value stays within `tolerances` of the run's
    first record.
    """

    name = ""
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
        return _render_record(run.rec, run.raw, run.n, run.t0)


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
    """Consecutive gates of ONE arm — basis guard, theta, safety, feed stale,
    elapsed-percent alike. A gated arm ticks for minutes saying the same thing.

    The eval rule's twin, deliberately: one arm's lane, one signature, one set
    of anchored tolerances, one lifetime. An arm emits an eval OR a gate on a
    tick and never both, so the two rules tile the quiet tape between them and
    a reader learns ONE collapse behaviour rather than one per reason family.

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
        return r.get("ev") == tape.EV_GATED

    def signature(self, r: dict) -> tuple:
        return (_NUM_RE.sub("#", r.get("reason") or ""),
                "up_ask" in r, r.get("up_ask") is None, r.get("dn_ask") is None)

    def metrics(self, r: dict) -> dict:
        # A legacy record carries the margin only inside its sentence; read it
        # the same way the line will, so the tolerance sees the number shown.
        mg = _margin_guard(r)
        met = {k: r[k] for k in self.tolerances if r.get(k) is not None}
        if mg and "margin_bp" not in met:
            met["margin_bp"], met["guard_bp"] = mg
        return met


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
            return _render_record(rolled or closed, "", run.n, run.t0)
        # ×2 like every other collapsed line: this one absorbed two records,
        # and the panel's count is what says so. Without it a merged pair reads
        # as one record and the tape stops adding up.
        return click.style(
            f"{_tape_head(rolled, run.n, run.t0)} {_tape_tag('ROLL')}"
            f"{self._shut(closed)} closed → next window armed"
            f" (${rolled.get('size', 0):g})",
            fg="cyan")

    @staticmethod
    def _shut(closed: dict) -> str:
        w = updown_slugs.parse_updown_slug(closed.get("slug") or "")
        return time.strftime("%H:%M", time.localtime(w["start"])) if w else "?"


def _best_side(sides: list[dict]) -> dict | None:
    """The side the eval line prints — same pick as _render_record's.

    Only PRICED sides are candidates. A side the engine is quoting into rather
    than taking from carries `ask: null` and no `net` at all, and reaching for
    `s["net"]` on one raised straight through the collapser's crash belt — the
    record was routed nowhere, rendered as nothing, and took every open run
    with it. 2.2% of live evals carry such a side, all of them the maker-rest
    shape the newest arms emit. A side with no book is not the best side; it
    is not a side this line can print.
    """
    priced = [s for s in sides
              if s.get("net") is not None and s.get("ask") is not None]
    return max(priced, key=lambda s: s["net"], default=None)


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

    A record also ends the runs it contradicts rather than only its own: an
    arm's eval and gate runs die on each other, since one arm cannot be both
    at once. Another arm's runs survive — that is what per-arm lanes buy.
    """

    _RULES: tuple[_CollapseRule, ...] = (_EvalRun(), _GateRun(), _RollClosePair())

    def __init__(self) -> None:
        self._runs: dict[str, _Run] = {}

    def break_runs(self) -> None:
        """End every open run without rendering anything.

        For a discontinuity the tape itself cannot show. A run states a count
        and a span ("×12, 47s"), and both are lies if records went missing
        underneath it — so a remote feed whose answer came back `truncated`
        calls this before handing over the batch on the far side of the gap.
        """
        self._runs.clear()

    def add(self, raw: str, lines) -> None:
        """Feed one raw tape line; appends/updates rendered output in `lines`."""
        try:
            r = json.loads(raw)
        except ValueError:
            return  # torn mid-write line: not a record, so not a run break either
        if not isinstance(r, dict):
            return
        try:
            run = self._route(r, raw, lines)
        except Exception:
            run = None  # a malformed record must never take the dashboard down
        if run is not None:
            try:
                out = run.rule.render(run)
            except Exception:
                # This run can no longer render itself. Fall through to the raw
                # path below rather than return: a belt that swallows a record
                # is the failure the panel exists to prevent, and _runs.clear()
                # down there retires the broken run.
                run = None
            else:
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
            rendered = raw.rstrip()  # unrenderable, but never unseen
        if rendered:
            lines.append(rendered)

    def _route(self, r: dict, raw: str, lines) -> _Run | None:
        rule = next((ru for ru in self._RULES if ru.matches(r)), None)
        if rule is None:
            return None
        lane, arm = rule.lane(r), r.get("slug")
        sig, met = rule.signature(r), rule.metrics(r)
        self._end_conflicting(lane, arm)
        run = self._runs.get(lane)
        if (run is None or run.sig != sig
                or not _within(run.anchor, met, rule.tolerances)
                or not rule.continues(run, r)
                # Its line has scrolled past _OWN_LOOKBACK and can no longer be
                # updated. Continuing the run would append a SECOND line
                # restating a count for records the first one already carries —
                # the panel would then add up to more than the tape. Start
                # fresh: the records above stay counted above.
                or self._own_slot(run, lines) is None):
            run = self._runs[lane] = _Run(rule, arm, sig, met)
        run.n += 1
        run.t1 = r.get("t", 0.0)
        if run.n == 1:
            run.t0 = run.t1
        run.rec, run.raw = r, raw
        rule.fold(run, r)
        return run

    def _end_conflicting(self, lane: str, arm: str | None) -> None:
        """Drop this arm's other runs. Other arms' runs survive, which is the
        whole point of per-arm lanes — arms interleaving on the tape must not
        thrash each other's lines."""
        for k, run in list(self._runs.items()):
            if k != lane and run.arm == arm:
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


# Speculative-exposure threshold zone: undecided dollars above these turn the
# header's un-decided figure yellow, then red.
_UNDECIDED_YELLOW_USD = 300.0
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


def exposure_rows(status: dict | None, sb: dict | None) -> list[tuple]:
    """Live exposure as header-grid rows: `(label, v1, v2, v3)` tuples.

    THE one place committed / un-decided / resting / riding are turned into
    cells and the un-decided threshold colour is chosen. The watch dashboard
    and `pmt crypto stats` both render THESE rows, so the two views cannot
    disagree about what is at risk — only about how many columns they said it
    in. Reads already-cached data; never fetches.

    A resting maker bid earns a row of its own: it only exists on the days a
    bid is actually on the book, and a "◇resting $0.00" every tick on a
    taker-only fleet is noise.
    """
    committed, undecided, resting = _risk_exposure((status or {}).get("arms"))
    sb = sb or {}
    color = ("red" if undecided > _UNDECIDED_RED_USD else
             "yellow" if undecided > _UNDECIDED_YELLOW_USD else "")
    riding = f"riding {sb.get('riding_n', 0)} windows ${sb.get('riding_usd', 0.0):,.2f}"
    undecided_s = f"${_zero(undecided):,.2f} un-decided"
    rows = [("exposure", f"committed ${_zero(committed):,.2f}",
             _paint(undecided_s, color, False) if color else undecided_s,
             f"[dim]{riding}[/dim]")]
    if resting > 0.005:
        rows.append(("resting", f"[cyan]◇resting ${resting:,.2f}[/cyan]",
                     "[dim]maker bid on the book[/dim]", ""))
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
# The dashboard's ONE table. It is not a log of trades; it is the strategy's
# state, and its unit is the WINDOW, which has exactly one life:
#
#     armed ──► gated ──► fired / riding ──► decided W/L ──► rolled away
#       ○         ⊘             ◆               ✓  ✗          (ages off)
#
# Four properties let one table hold all of it:
#
#  1. ONE SORT KEY — window end, descending. A window's stage is a function of
#     its age, so time order IS lifecycle order for free: live windows on top,
#     riding below them, then the decided tail. Deliberately not "riding
#     first": a window stuck undecided for a day would then hold a row
#     forever. Stuck money is totalled on the header's "riding N windows $W",
#     which is where a total belongs; this panel answers "where is the fleet
#     now", and a fresh fill already tops it under recency.
#
#  2. ONE ROW PER WINDOW, keyed by slug, merged from the two sources that know
#     about one. The live head comes from the engine's /status arms — the only
#     place a window with no fill yet exists, since the wallet has nothing to
#     grade. Everything with money in it comes from the scoreboard. A window
#     in both is ONE row that gains its live posture, so a fire fills the row
#     already on screen instead of adding a second below it. The WALLET WINS
#     on money and verdict; the engine contributes posture, and its committed
#     figure only where the wallet has never seen the window (rendered dim,
#     because it is not ground truth yet).
#
#  3. THE STAGE GLYPH leads the `arm` cell rather than owning a column: it is
#     a property of the row, and a 1-wide column is the first thing Rich
#     squeezes out of existence on a narrow console (see _WINDOWS_COLUMNS).
#     The ⟳/≈/◇ arm flags ride there for the same reason.
#
#  4. EVERY COLUMN IS REPURPOSED BY STAGE rather than duplicated: a live
#     un-filled window's money cells are dead space and a rolled-away
#     window's model cells are dead space, so the two sets share the SAME
#     columns and the stage decides which meaning is showing. The column
#     count never changes, so the geometry never jitters:
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
# would file it under whichever arm happens to be alive now.
#
# "Engine unreachable or no arms" is the one thing here that is not a row, so
# it lives on the header panel (see `engine_row`): with no arms there are no
# live rows to hang it on, and the exposure row would otherwise read a
# confident $0.00 committed.

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
    # Sized for the widest NORMAL live state, not a corner: an armed arm with
    # both sides' safety read AND a brake on one of them —
    # "armed safe  saf +0.90/-0.30  down:distrust" is 42. A gate reason
    # ("gated  margin -4.9 vs 6.0bp", 27) fits well inside that.
    # This and `read` are the two widest cells and the two the narrowing lands
    # on — see windows_columns: they are diagnostic prose, and every money
    # column is sized to its longest value and must never lose a digit.
    ("state", "left", 42),
    ("read", "right", 25),     # "+12.3/9.3bp p↑0.87 ρ+0.40" is 25
    ("position", "right", 15),  # "down 0.97→0.99" is 15
    ("$", "right", 15),        # "$1,234.56 ◇$450" is 15
    # 10, not 9: a five-figure loss ("-12,436.76") is a P&L this fleet can
    # print, and at 9 the cell sheds a digit — the one failure a money column
    # may not have.
    ("P&L", "right", 10),
)

# The two diagnostic prose columns. Narrowing is spent on THESE, in full,
# before any other column loses a character.
_WINDOWS_PROSE = ("state", "read")
# 12 = "gated  marg…" — below this the cell states a category and no evidence,
# at which point the column is worth less than the width it costs.
_PROSE_MIN_W = 12
# The order whole columns are dropped in once both prose columns are at their
# floor. `arm` (identity + lifecycle glyph), `$` and `P&L` (money) are not in
# it and never go: an unreadable row is better than a wrong one, and a blank
# money cell is a wrong one.
_WINDOWS_SHED_ORDER = ("read", "state", "position", "t")


def _table_width(cols) -> int:
    """Console columns Rich needs for a box table of these: every column's
    width plus its one space of padding either side, plus the box's own
    vertical rules (one between each pair, one at each end)."""
    return sum(w + 2 for _, _, w in cols) + len(cols) + 1


def windows_columns(width: int | None = None) -> tuple[tuple[str, str, int], ...]:
    """_WINDOWS_COLUMNS narrowed to what a console `width` wide can hold.

    Rich's own squeeze reduces every `no_wrap` column PROPORTIONALLY, which is
    not what this table's comment above claims and not what it needs: at 130
    columns the P&L cell sheds a digit off "+1,234.50", at 70 it renders
    completely blank with no ellipsis to say so, and at 100 the 14-wide `arm`
    cell is cut to eight, so `btc 5m` and `btc 15m` render byte-identically —
    two different windows, one row. (The `t` column, the other half of that
    disambiguation, is squeezed to nothing at the same width.)

    So the decision is made HERE, where what each column MEANS is known. The
    two prose columns absorb the whole narrowing down to _PROSE_MIN_W; past
    that whole columns are dropped in _WINDOWS_SHED_ORDER, and identity and
    money keep their natural width the whole way down. `None` (the default) is
    "no constraint" — the natural 149.
    """
    if width is None:
        return _WINDOWS_COLUMNS
    cols = [list(c) for c in _WINDOWS_COLUMNS]
    floor = {name: (_PROSE_MIN_W if name in _WINDOWS_PROSE else w)
             for name, _, w in _WINDOWS_COLUMNS}

    def floor_width(cs) -> int:
        return _table_width([(n, j, floor[n]) for n, j, _ in cs])

    for name in _WINDOWS_SHED_ORDER:
        if floor_width(cols) <= width:
            break
        cols = [c for c in cols if c[0] != name]
    # Hand the slack back to the prose columns, in table order, up to natural.
    slack = width - floor_width(cols)
    for c in cols:
        if c[0] in _WINDOWS_PROSE:
            grow = max(0, min(c[2] - _PROSE_MIN_W, slack))
            c[2] = _PROSE_MIN_W + grow
            slack -= grow
    return tuple(tuple(c) for c in cols)

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
    """The one-glyph lifecycle column. Dim for an ~estimated verdict, the same
    lower-confidence convention the P&L cell uses."""
    glyph, style = _STAGE_GLYPH.get(_stage(w), ("⊘", "dim"))
    return f"[{'dim' if w.get('est') else style}]{glyph}[/]"


def _dur_label(sec: float, coarse: bool = False) -> str:
    """`45s` / `3m12s` / `1h04`. `coarse` drops the seconds off the minutes
    form, which is what a time-since wants and a duration does not."""
    if sec < 60:
        return f"{int(sec)}s"
    if sec < 3600:
        m, s = divmod(int(sec), 60)
        return f"{m}m" if coarse else f"{m}m{s:02d}s"
    h, m = divmod(int(sec // 60), 60)
    return f"{h}h{m:02d}"


def _age_label(sec: float) -> str:
    """Time-since for a window row, `live` while the window is still open.
    Age, not a wall clock: "how long ago" is what a live dashboard is asked;
    the countdown to a live window's close is the `t` column's other half."""
    return "live" if sec < 0 else _dur_label(sec, coarse=True)


def _window_pnl_cell(w: dict) -> str:
    """Signed P&L once the window is decided, `riding` while a position has no
    verdict, `—` while nothing is at risk at all. `~` marks an estimated figure
    (imputed win / gamma-unreachable), same convention as the header's
    "N ~estimated".

    Deliberately NOT the armed/gated word: the `state` cell two columns left
    already says it, in colour and with its reason.
    """
    stage = _stage(w)
    if stage == "riding":
        return "[cyan]riding[/cyan]"
    if stage not in ("won", "lost"):
        return _DASH
    v = _zero(float(w.get("pnl") or 0.0))
    return f"[{_pnl_color(v)}]{'~' if w.get('est') else ''}{v:+,.2f}[/{_pnl_color(v)}]"


def _t_cell(w: dict, now: float) -> str:
    """One column, one axis: how far this window is from its own end — the T-
    countdown while it is still open, the age since it closed after that. A
    live arm and a decided trade ask the same question from opposite sides of
    the same instant."""
    end_ts = float(w.get("end_ts") or 0.0)
    if not end_ts:
        return _DASH
    if end_ts > now:
        return _countdown_markup(w.get("slug", ""), now)
    return _age_label(now - end_ts)


def _live_state_text(e: dict) -> str:
    """A live window's `state` cell: the compact gate reason for a gated arm,
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
        badges = "  ".join(x for x in (_safety_badge(sides, e.get("p_up")),
                                        _brake_badge(sides)) if x)
        return f"{head}  {badges}" if badges else head
    # flip/quiesce ARE the mode, and an unknown state is printed as itself
    # rather than swallowed.
    return str(state)


def _state_cell(w: dict, now: float) -> str:
    """What this row is about right now: the ENGINE's posture while the window
    is live, the position's exposure once the arm has left it behind.

    One column repurposed by stage: an armed window has no exposure to report
    and a rolled-away one has no engine reading it, so the column carries
    whichever of the two exists.
    """
    if w.get("live"):
        return _live_state_text(w.get("eval") or {})
    start = float(w.get("entry_ts") or 0.0)
    if not start:
        return _DASH
    # Exposure time: what polymarket.effectiveness grades on, and what the
    # dashboard could never show while these were two tables.
    end = now if w.get("won") is None else (float(w.get("exit_ts") or 0.0)
                                            or float(w.get("end_ts") or 0.0) or now)
    return f"[dim]held[/dim] {_dur_label(max(end - start, 0.0))}"


def _read_cell(w: dict) -> str:
    """The model's case while the window is live — banked-vs-cushion evidence,
    p↑ and ρ, the three figures that only mean anything before a verdict —
    and what we actually took once it is settled."""
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
        return " ".join(bits) if bits else _DASH
    shares, ts = w.get("shares"), float(w.get("entry_ts") or 0.0)
    if not shares or not ts:
        return _DASH
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
        return _DASH
    style = "green" if px >= 0.5 else "red"
    return f"[{style}]{px:.2f}[/{style}]"


def _position_cell(w: dict, odds: dict | None) -> str:
    """`up 0.97→0.99` — the side we hold, the average dollar paid for it, and
    what it is worth now. `—` for a window nothing is held in, which is every
    live row until its arm fires.

    The right-hand price walks the whole lifecycle rather than going blank at
    the verdict: the live mark while the window rides, the binary's own
    1.00/0.00 once it has settled.
    """
    px = w.get("entry_px")
    if not px:
        return _DASH
    won = w.get("won")
    if won is None:
        mark = _odds_cell(w, odds)
    else:
        style = "dim" if w.get("est") else ("green" if won else "red")
        mark = f"[{style}]{1.0 if won else 0.0:.2f}[/{style}]"
    return f"{w.get('side') or '?'} {px:.2f}[dim]→[/dim]{mark}"


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


def tape_title(remote: bool = False) -> str:
    """`tape`, or `tape · remote` when the panel is being fed by the engine's
    control plane rather than a local file.

    Which one is showing is not cosmetic: a watch pointed at a remote engine
    reads the tape over the same tunnel as everything else, so its records can
    lag or arrive with a gap the engine admitted to, where a local file just
    stops. An operator who can't tell the two apart reads a quiet remote panel
    as a quiet fleet.
    """
    return "tape · remote" if remote else "tape"


def build_windows_table(sb: dict | None, now: float,
                        arms: dict | None = None,
                        limit: int | None = None,
                        odds: dict | None = None,
                        width: int | None = None) -> Table:
    """The dashboard's ONE table — every window the fleet is in or has been
    in, newest first, each cell reading whichever of its two meanings the
    row's stage calls for. See the design note above.

        ⊘  xrp 5m ⟳≈   3:41  gated  margin -4.9 vs 6.0bp   -3.2/9.3bp p↑0.44 ρ-0.10   —              $0.00       —
        ◆  bnb 5m       1m   held 3m12s                    20sh in 14:38              up 0.97→0.41   $19.44   riding
        ✓  eth 5m      10m   held 2m45s                    10sh in 14:31              down 0.95→1.00 $95.00   +12.30

    Renders EVERY row it is handed (the caller caps via `limit`), so a window
    the scoreboard knows about is always on screen somewhere. A riding row
    must survive the arm rolling AWAY from that window: the position stays
    ours until the wallet grades it.

    Every cell tolerates a missing or half-built eval — an engine restart
    mid-watch leaves last_eval None or partial, and the row must keep painting
    rather than raise.

    `arms` is the engine's already-fetched /status arms mapping: it supplies
    the live head and each live row's posture, and nothing else. `odds` is the
    optional current-mark map (polymarket.positions); absent, empty or stale
    it degrades to `—` on the right of the `position` cell and nothing else.

    `width` is the console width the table has to live in; the column set is
    chosen for it (see windows_columns) rather than left to Rich's proportional
    squeeze, which blanks money cells. None means the natural 149.
    """
    # Natural widths, not expand=True: the columns are sized to their longest
    # value, and stretching them across a 200-column terminal puts a metre of
    # whitespace between a window's size and its P&L.
    t = Table(expand=False, pad_edge=False)
    cols = windows_columns(width)
    for col, justify, w_ in cols:
        t.add_column(col, justify=justify, width=w_, no_wrap=True, overflow="ellipsis")
    rows = window_rows(sb, arms, limit)
    cells = {
        "arm": _arm_cell, "t": lambda w: _t_cell(w, now),
        "state": lambda w: _state_cell(w, now), "read": _read_cell,
        "position": lambda w: _position_cell(w, odds),
        "$": _money_cell, "P&L": _window_pnl_cell,
    }
    for w in rows:
        t.add_row(*(cells[name](w) for name, _, _ in cols))
    if not rows:
        # In the state cell where there is one, the widest column: a
        # placeholder that ellipsizes is worse than a short honest one.
        empty = {"state": "[dim]no windows[/dim]", "arm": "[dim]no windows[/dim]"}
        names = [name for name, _, _ in cols]
        first = "state" if "state" in names else "arm"
        t.add_row(*(empty[first] if name == first else "—" for name in names))
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
    describe a live arm, and there is none here.
    """
    label = _arm_label(w.get("slug", ""))
    flags = w.get("flags") or ""
    cell = f"{_stage_cell(w)} {label}"
    return f"{cell} [dim]{flags}[/dim]" if flags else cell


def engine_row(status: dict | None) -> tuple | None:
    """The red "engine unreachable or no arms" placeholder as a header row, or
    None while something is armed.

    It has to live somewhere: with no arms the windows table has no live row
    to hang it on, its decided tail keeps painting as if nothing changed, and
    the exposure row would read a confident "committed $0.00" — the one
    reading an unreachable engine and an idle fleet must never share.
    """
    if (status or {}).get("arms"):
        return None
    return ("engine", "[red]unreachable or no arms[/red]",
            "[dim]nothing is armed right now[/dim]", "")


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
_REFRESH_LINE = ("tape 1s (remote 2s) · engine 2s · stats 10s · odds 30s · "
                 "balance 60s")

_HELP_MODAL_W = 78  # readable prose width; clamped to the terminal by the caller

# The legend's own copy. Which columns change meaning with the stage is the
# trick the one table turns, so this is where it is spelled out.
_MODAL_LEGEND = (
    ("stage", f"[cyan]{_STAGE_LEGEND}[/cyan] [dim]— it leads every `arm` cell[/dim]"),
    ("flags", f"[cyan]{_FLAG_LEGEND}[/cyan] [dim]— trailing the arm that is on "
              "this window right now[/dim]"),
    ("columns", "[dim]state · read: the engine's posture while a window is live "
                "(gate reason, evidence, p↑, ρ), what we took once it is settled[/dim]"),
    ("", "[dim]position: entry→mark, and →1.00/0.00 once the binary settles · "
         "$ dims while it is the engine's own figure · ~ marks an estimated P&L[/dim]"),
    ("refresh", f"[dim]{_REFRESH_LINE}[/dim]"),
    ("--since", "[dim]moves the header's `recent` floor only (default 6h) — "
                "all-time, riding and the windows table always walk the full "
                "history[/dim]"),
)


def _modal_grid(label_style: str) -> Table:
    """The borderless two-column grid both halves of the modal are laid out
    in, so the key list and the legend line up as one page."""
    t = Table(box=None, pad_edge=False, padding=(0, 2), show_header=False)
    t.add_column("label", justify="right", width=7, style=label_style)
    t.add_column("text", justify="left", overflow="fold")
    return t


def build_help_modal(width: int | None = None):
    """The `h` overlay: every key the watch accepts, the glyph legends, and the
    refresh cadences.

    A FOREGROUND panel, not a strip — it takes the screen while it is open,
    which is what gives the legends more than the single line an inline
    controls strip could hold. `--since` is explained here for the same
    reason: it changes what the header's `recent` row means, and `--help` is
    not reachable without leaving the dashboard.

    Nothing here fetches. The data workers keep running underneath, so
    dismissing the panel restores a live frame rather than a frozen one.
    """
    from rich.console import Group
    from rich.panel import Panel
    from rich.text import Text

    keys = _modal_grid("bold")
    for _press, label, desc in WATCH_KEYS:
        keys.add_row(label, desc)
    legend = _modal_grid("dim")
    for label, text in _MODAL_LEGEND:
        legend.add_row(label, text)
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


def header_rows(snap: dict) -> list[tuple]:
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


# The grid at its natural size, in a panel: four fixed columns, a space of
# padding either side of each, the panel's own padding and its border.
_HEAD_NATURAL_W = (_HEAD_LABEL_W + _HEAD_V1_W + _HEAD_V2_W + _HEAD_V3_W
                   + 4 * 2 + 2 + 2)


def header_height(snap: dict, render_err: str | None = None,
                  width: int | None = None) -> int:
    """Terminal rows the header panel will occupy, MEASURED by rendering it.

    It used to be `len(header_rows) + note + border`, which assumes one grid
    line per row. Two rows break that assumption at EVERY width: `engine_row`
    and `feed_row` carry prose in columns sized for money (22 and 26 characters
    in the 20- and 22-wide `overflow="fold"` columns), so they always paint two
    grid lines, and a feed row explaining a dropped websocket paints three. The
    watch layout sized its head slot to the undercount and Rich cropped the
    bottom off the panel — taking the border and the `stats Ns ago · stale`
    freshness subtitle with it, exactly when the engine is unreachable and the
    operator most needs to know how stale the numbers on screen are.

    Measured rather than re-derived because the fold is Rich's arithmetic, not
    ours: word breaks make a cell's line count more than its length over the
    column width. `width` is the console it will paint into (the grid is fixed
    width, so anything at or above its natural size folds identically).
    """
    from rich.console import Console

    console = Console(width=max(width or _HEAD_NATURAL_W, 20),
                      file=io.StringIO(), force_terminal=False, no_color=True)
    return len(console.render_lines(build_header_panel(snap, "", render_err),
                                    pad=False))


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
    for row in header_rows(snap):
        t.add_row(*row)
    note = header_note(snap, render_err)
    body = t if note is None else Group(t, note)
    return Panel(body, title=f"[bold]updown fleet[/bold] [dim]· {floor_label}[/dim]",
                 title_align="left", subtitle=_header_subtitle(snap),
                 subtitle_align="right", border_style="cyan")


