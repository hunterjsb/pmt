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
  * column geometry is fixed (see _ARMS_COLUMNS, _TAPE_TAG_WIDTH), so the
    layout never jitters as state/reason text changes tick to tick.

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


def _tape_tag(text: str) -> str:
    """Left-pad an event tag to a fixed width so the fields after it land at
    the same column regardless of event type (FIRE/EXIT/eval/gated/ROLL)."""
    return f"{text:<{_TAPE_TAG_WIDTH}}"


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


def _render_record(r: dict, raw: str) -> str:
    """One parsed tape record as a rendered line; `raw` is the fallback for an
    event this build doesn't know (never swallow a record we can't name)."""
    head = _tape_head(r)

    def money(v: float) -> str:
        return f"${_zero(v):,.2f}".rstrip("0").rstrip(".")

    ev = r.get("ev")
    if ev == tape.EV_FIRE:
        tag = {"flip": "FLIP", "spec": "SPEC"}.get(r.get("mode", "safe"), "FIRE")
        label = click.style(_tape_tag(f"{tag} {r['side'].upper()}"), fg="green", bold=True)
        pct = f"  {r['elapsed_frac'] * 100:.0f}% thru" if "elapsed_frac" in r else ""
        return (
            f"{head} {label} {r['size']:g}sh @ {r['ask']:.2f}"
            f"  fair {r['fair']:.4f}  {r['net'] * 100:+.1f}¢"
            f"  ρ{r['rho']:+.2f}  {money(r['committed'])} in{pct}"
        )
    if ev == tape.EV_EXIT:
        label = click.style(_tape_tag(f"EXIT {r['side'].upper()}"), fg="red", bold=True)
        return f"{head} {label} {r['size']:g}sh @ bid {r['bid']:.2f}  fair {r['fair']:.4f}"
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
            f"{head} {_tape_tag('eval')} p↑{r['p_up']:.4f}  {book}"
            f"  ρ{r['rho']:+.2f}  {money(r['committed'])} in"
        )
        extras = "  ".join(x for x in (_safety_ansi(sides, r.get("p_up")), _brake_ansi(sides)) if x)
        return click.style(body, dim=True) + tags + ("  " + extras if extras else "")
    if ev == tape.EV_GATED:
        def ask(v: float | None) -> str:
            return f"{v:.2f}" if v is not None else "—"
        asks = f"  up {ask(r['up_ask'])}/dn {ask(r['dn_ask'])}" if "up_ask" in r else ""
        return click.style(f"{head} {_tape_tag('gated')} {r.get('reason', '?')}{asks}",
                            fg="yellow", dim=True)
    if ev == tape.EV_ROLL:
        return click.style(f"{head} {_tape_tag('ROLL')} next window armed (${r['size']:g})",
                            fg="cyan")
    if ev == tape.EV_CLEANUP:
        return click.style(f"{head} ── window closed ──", dim=True)
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

_OWN_LOOKBACK = 8  # how deep under later output a live run's line may sit and still be updated


def _run_suffix(run: _Run) -> str:
    """`×12 ⟨23:40:01→23:43:20⟩`, or "" for a run of one — so an isolated
    record renders byte-identically to an uncollapsed one and the count only
    appears once there is something to count."""
    if run.n < 2:
        return ""
    return click.style(f"  ×{run.n} ⟨{_hms(run.t0)}→{_hms(run.t1)}⟩", dim=True)


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

    def fold(self, run: _Run, r: dict) -> None:
        """Accumulate whatever the rendered line needs beyond the freshest record."""

    def render(self, run: _Run) -> str:
        return _render_record(run.rec, run.raw) + _run_suffix(run)


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
        per = " · ".join(f"{sym} {txt}" for sym, txt in sorted(run.state.items()))
        return click.style(
            f"{_hms(run.t1)}  {'':<14} {_tape_tag(f'gated ×{run.n}')} basis bp/guard: {per}",
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


def _best_side(sides: list[dict]) -> dict | None:
    """The side the eval line prints — same pick as _render_record's."""
    return max(sides, key=lambda s: s["net"], default=None)


class TapeCollapser:
    """Collapse runs of repetitive tape records into single live-updating lines.

    One rule per repetitive shape (see _CollapseRule). A record that matches no
    rule — FIRE, EXIT, ROLL, CLEANUP, or any ev this build doesn't know —
    never collapses and ends EVERY open run: those are the lines the whole
    mechanism exists to make visible.

    A record also ends the runs it contradicts rather than only its own: the
    global basis run dies on anything that isn't a basis gate (an eval means
    that arm cleared the guard), and an arm's own eval/gate runs die on each
    other, since one arm cannot be both at once.
    """

    _RULES: tuple[_CollapseRule, ...] = (_BasisGuardRun(), _EvalRun(), _GateRun())

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
        if run is None or run.sig != sig or not _within(run.anchor, met, rule.tolerances):
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

    Committed and un-decided are separate cells here and one clause in
    `build_risk_header` — the numbers and the un-decided threshold color are
    computed once, in this function, so a grid layout and the one-line layout
    can never disagree about what is at risk.

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


def build_risk_header(status: dict | None, sb: dict | None,
                       rtds: bool = True) -> str:
    """`committed $Y ($Z un-decided) · ◇resting $R · riding N windows $W` —
    the one-line exposure summary between the scoreboard and the arms table.

    Deliberately carries no capital figure: that is the top panel's, and two
    money-shaped lines stacked back to back read as one line printed twice.

    `rtds=False` drops the stream-health tail. The dashboard has a full-width
    row for this line; `pmt crypto stats` prints it inside a panel that has
    to hold 100 columns, and the two clauses answer different questions
    anyway — stats gives the stream its own line.
    """
    cells = risk_cells(status, sb, rtds)
    # committed and its un-decided share are one clause on a single line and
    # two cells in a grid — same numbers, laid out for the space available.
    return " · ".join([f"{cells[0]} ({cells[1]})"] + cells[2:])


def _chip_label(w: dict) -> str:
    """`btc5` / `eth15` — the compact arm label a chip has room for."""
    parsed = updown_slugs.parse(w.get("slug", ""))
    return f"{parsed[0]}{parsed[1] // 60}" if parsed else (w.get("slug", "?")[:8])


def _window_chip(w: dict) -> str:
    """`✓ btc5 +12` / `✗ eth15 -44` for one resolved window; dim for a
    ~estimated (gamma-unreachable, or gamma-confirmed-win-pending-redeem)
    read rather than the win/loss color, since it's a lower-confidence read."""
    mark = "✓" if w.get("won") else "✗"
    text = f"{mark} {_chip_label(w)} {w.get('pnl') or 0.0:+.0f}"
    style = "dim" if w.get("est") else ("green" if w.get("won") else "red")
    return f"[{style}]{text}[/{style}]"


def _riding_chip(w: dict) -> str:
    """`◆ bnb5 $19` — a FILLED window that hasn't decided yet.

    Leads the strip because it is the only chip that is still money at risk,
    and because it is the chip that used to be absent entirely: a win waits on
    its redeem row and a loss posts no row at all for 300s, and for that whole
    stretch the arm has already rolled and the fire has scrolled off the tape.
    """
    return f"[cyan]◆ {_chip_label(w)} ${w.get('notional') or 0.0:,.0f}[/cyan]"


def build_windows_strip(windows: list[dict] | None,
                         riding: list[dict] | None = None) -> str:
    """Chip row of the fleet's recent windows, newest first: ◆ for one still
    riding, ✓/✗ for a decided one. Ordered by `trade_rows`, so the strip and
    the trades table can never disagree about what happened when."""
    chips = [_riding_chip(w) if w.get("won") is None else _window_chip(w)
             for w in trade_rows({"windows": windows, "riding_windows": riding})]
    return "  ".join(chips) if chips else "[dim]no windows traded yet[/dim]"


# ---------- the trades table ----------
#
# The one place the dashboard names an individual trade. It is fed from the
# SAME scoreboard as everything else (score_activity's windows/riding_windows)
# — never a second read of the wallet or the tape.

_TRADES_COLUMNS = (
    ("age", "right", 5),
    ("arm", "left", 8),
    ("side", "left", 4),
    ("entry", "right", 5),
    ("size", "right", 9),
    ("P&L", "right", 9),
)


def _age_label(sec: float) -> str:
    """Compact time-since for a trades row — `live` while the window is still
    open, then s / m / h:mm. Age, not a wall clock: "how long ago" is the
    question a trade row on a live dashboard is actually asked."""
    if sec < 0:
        return "live"
    if sec < 60:
        return f"{int(sec)}s"
    if sec < 3600:
        return f"{int(sec // 60)}m"
    h, m = divmod(int(sec // 60), 60)
    return f"{h}h{m:02d}"


def _trade_pnl_cell(w: dict) -> str:
    """Signed P&L, or `riding` while the window has no verdict yet. `~` marks
    an estimated figure (imputed win / gamma-unreachable), same convention as
    the header's "N ~estimated"."""
    pnl = w.get("pnl")
    if pnl is None or w.get("won") is None:
        return "[cyan]riding[/cyan]"
    v = _zero(float(pnl))
    return f"[{_pnl_color(v)}]{'~' if w.get('est') else ''}{v:+,.2f}[/{_pnl_color(v)}]"


def trade_rows(sb: dict | None, limit: int | None = None) -> list[dict]:
    """Riding and decided windows in ONE list, newest window-end first.

    Recency, deliberately, rather than "riding always leads": a fresh fill
    tops the panel anyway — a still-open window sorts above every closed one —
    while a window stuck undecided since yesterday (four of them, $317, live on
    2026-08-23) stops squatting on a six-row panel forever. Stuck money is
    still totalled on the risk header's "riding N windows $W", which is where
    a total belongs; this panel answers "what just happened".
    """
    sb = sb or {}
    rows = list(sb.get("riding_windows") or []) + list(sb.get("windows") or [])
    rows.sort(key=lambda r: float(r.get("end_ts") or 0.0), reverse=True)
    return rows if limit is None else rows[:limit]


def trades_title(sb: dict | None, shown: int | None = None) -> str:
    """`trades · last 12 decided · 2 riding`, or `trades · 6 of 14 · 12
    decided · 2 riding` when the panel is painting fewer rows than the
    scoreboard holds.

    Retention STATED, not implied — a cap the operator can't see is
    indistinguishable from a dropped trade, which is the confusion this whole
    panel exists to end.
    """
    sb = sb or {}
    n_dec = len(sb.get("windows") or [])
    n_ride = len(sb.get("riding_windows") or [])
    held = f"{n_dec} decided · {n_ride} riding"
    if shown is None or shown >= n_dec + n_ride:
        return f"trades · last {held}"
    return f"trades · {shown} of {n_dec + n_ride} · {held}"


def build_trades_table(sb: dict | None, now: float,
                        limit: int | None = None) -> Table:
    """Per-trade table: age, arm, side, avg entry, notional, P&L.

    Renders EVERY row it is handed (the caller caps via `limit`), so a window
    present in the scoreboard is always on screen somewhere — the guarantee
    the chip strip alone could not make, because it only ever carried decided
    windows.
    """
    # Natural widths, not expand=True: six narrow columns stretched across 160
    # terminal columns puts a metre of whitespace between a trade's size and
    # its P&L. The arms table expands because it genuinely fills the row.
    t = Table(expand=False, pad_edge=False)
    for col, justify, width in _TRADES_COLUMNS:
        t.add_column(col, justify=justify, width=width, no_wrap=True, overflow="ellipsis")
    rows = trade_rows(sb, limit)
    for w in rows:
        px = w.get("entry_px")
        end_ts = float(w.get("end_ts") or 0.0)
        t.add_row(
            _age_label(now - end_ts) if end_ts else "—",
            _arm_label(w.get("slug", "")),
            w.get("side") or "—",
            f"{px:.2f}" if px else "—",
            f"${_zero(float(w.get('notional') or 0.0)):,.2f}",
            _trade_pnl_cell(w),
        )
    if not rows:
        # In the P&L cell, not a wider one: every column here is narrow, and a
        # placeholder that ellipsizes is worse than a short honest one.
        t.add_row("—", "—", "—", "—", "—", "[dim]no trades[/dim]")
    return t


# Fixed widths (+ ellipsis overflow below) so the arms table's geometry never
# jitters as state/reason text length changes tick to tick. `committed` is
# sized for "$1,234.56 ◇$450" — the resting bid shares the cell — and `flags`
# for all three markers at once plus its header; both were paid for out of the
# slack in evidence/p_up/mode/rho, each of which is now its longest value
# ("-100.0/100.0bp", "0.87", "quiesce", "+0.40") rather than a round number.
# T- keeps 6: an arm placed on a not-yet-open window counts down past "59:59".
def _arm_label(slug: str) -> str:
    """`btc 5m` — symbol + duration only. The start clock the tape lines
    carry would be redundant here: T- counts down to the same instant, and
    an arms row is always the CURRENT window."""
    parts = slug.split("-")
    return f"{parts[0]} {parts[2]}" if len(parts) >= 3 else slug


_ARMS_COLUMNS = (
    ("arm", "left", 8),
    ("T-", "right", 6),
    ("state", "left", 40),
    ("evidence", "right", 14),
    ("p_up", "right", 5),
    ("mode", "left", 7),
    ("rho", "right", 5),
    ("committed", "right", 16),
    ("flags", "right", 6),
)

_ARMS_FLAG_LEGEND = "⟳ roll · ≈ stream-fed · ◇ maker bid"

_CHIP_LEGEND = "◆ riding"  # the strip's own glyph: filled, no verdict yet


def build_arms_table(arms: dict | None, now: float) -> Table:
    """Live-arms table: countdown, state (compact gate reason for a gated
    arm, safety/brake badges for an armed one), banked-vs-cushion evidence,
    model read (p_up/mode/rho), committed $ (+ any resting maker bid), and
    the roll/feed/maker flags (_ARMS_FLAG_LEGEND). Every cell tolerates a
    missing/partial eval — an engine restart mid-watch leaves last_eval None
    or half-built.
    """
    t = Table(expand=True, pad_edge=False)
    for col, justify, width in _ARMS_COLUMNS:
        t.add_column(col, justify=justify, width=width, no_wrap=True, overflow="ellipsis")
    arms = arms or {}
    for slug, a in arms.items():
        if not isinstance(a, dict):
            a = {}
        e = a.get("eval")
        e = e if isinstance(e, dict) else {}
        state = e.get("state", "?")
        if state == "gated":
            state = f"[yellow]gated[/yellow]  {_gated_reason_compact(e.get('reason'), e)}"
        elif state == "armed":
            sides = e.get("sides") or []
            badges = "  ".join(x for x in (_safety_rich(sides, e.get("p_up")),
                                            _brake_rich(sides)) if x)
            state = f"[green]armed[/green]  {badges}" if badges else "[green]armed[/green]"
        p_up = f"{e['p_up']:.2f}" if "p_up" in e else "—"
        rho = f"{e['rho']:+.2f}" if "rho" in e else "—"
        committed = e.get("committed", a.get("filled_usdc"))
        committed_s = f"${_zero(committed):,.2f}" if committed is not None else "—"
        resting = a.get("resting_usdc") or e.get("resting") or 0.0
        if resting > 0.005:
            committed_s += f" [cyan]◇${resting:,.0f}[/cyan]"
        # _ARMS_FLAG_LEGEND, in that order. "·" keeps the roll slot occupied so
        # the feed/maker markers never shift column between arms.
        flags = (("⟳" if a.get("roll") else "·")
                 + ("≈" if a.get("feed") == "rtds" else "")
                 + ("◇" if a.get("maker_bid") else ""))
        t.add_row(_arm_label(slug), _countdown_markup(slug, now), state,
                  _evidence_markup(e), p_up, _mode_text(e), rho, committed_s, flags)
    if not arms:
        t.add_row("—", "—", "[red]engine unreachable or no arms[/red]",
                   "—", "—", "—", "—", "—", "—")
    return t


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


def _controls_panel():
    """The 'h' help overlay — swaps into the strip slot so toggling never
    changes the layout geometry. ONE content line: the slot is 3 rows, and a
    second line would be clipped rather than grow the panel. The --since hint
    lives in `--help` instead; it can't be changed mid-watch, whereas the flag
    legend explains glyphs that are on screen right now."""
    from rich.panel import Panel
    return Panel(
        "[bold]q[/bold] quit · [bold]h[/bold] controls · Ctrl-C quits"
        f"  [dim]|[/dim]  [cyan]{_ARMS_FLAG_LEGEND} · {_CHIP_LEGEND}[/cyan]"
        "  [dim]|[/dim]  refresh: tape 1s · engine 2s · scoreboard 10s · balance 60s",
        title="controls", border_style="cyan")


def build_header_panel(snap: dict, floor_label: str, render_err: str | None):
    """The dashboard's top panel: sliding W-L/P&L, capital, scoreboard data
    age, all-time totals, and whatever went wrong last frame.

    `snap` is one WatchState.read() mapping. The sliding block carries the
    --since-floored recent pulse; the all-time figures come off the same
    snapshot's full-history grade. Data age renders "—" (not "0s ago") before
    the first wallet walk lands — an honest cue beats a confident zero.
    """
    from rich.panel import Panel

    sb = snap["sb"]
    sliding = sb.get("sliding") or _SB_EMPTY_SLIDING
    wins, losses, net, rolls = sliding["wins"], sliding["losses"], sliding["net"], sliding["rolls"]
    n = wins + losses
    wr = f"{wins / n * 100:.0f}%" if n else "—"
    bal = snap["bal"]
    cap = f"${bal['total']:,.2f}" if bal else "…"
    color = _pnl_color(net)
    stale = " · [yellow dim]stats stale[/]" if snap["sb_stale"] else ""
    est = (f" · [dim]{sliding.get('estimated', 0)} ~estimated[/dim]"
           if sliding.get("estimated") else "")
    note = render_err or snap["err"]
    err = f" · [red dim]{note}[/]" if note else ""
    all_net = sb.get("net", 0.0)
    all_color = _pnl_color(all_net)
    all_time = (f" · [dim]all-time {sb.get('wins', 0)}W-{sb.get('losses', 0)}L "
                f"[{all_color}]{all_net:+,.2f}[/{all_color}][/dim]")
    if snap["sb_fetched_at"] is None:
        age = "[dim]—[/dim]"
    else:
        age_s = time.time() - snap["sb_fetched_at"]
        age_style = "yellow" if age_s > 30 else "dim"
        age = f"[{age_style}]{age_s:.0f}s ago[/{age_style}]"
    line1 = (f"[bold]{wins}W-{losses}L[/bold] ({wr}) · P&L [{color}]{net:+,.2f}[/] · "
             f"{rolls} rolls · capital {cap} · [dim]{floor_label}[/dim] · {age}"
             f"{all_time}{stale}{est}{err} · "
             f"[dim]{time.strftime('%H:%M:%S')}[/dim]")
    # The exposure summary lives here as the box's second line — a bare row
    # wedged between two panels read as chrome debris, not information.
    line2 = build_risk_header(snap.get("status"), sb)
    return Panel(f"{line1}\n{line2}", title="updown fleet", border_style="cyan")


