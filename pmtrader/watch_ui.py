"""Render layer for `pmt crypto watch` (and the tape/stats renderers it shares).

Every function here turns already-fetched data into text, Rich markup, or a
Rich renderable. No network, no engine calls, no wallet walks — that all lives
on cli_crypto's WatchFetcher thread (docs/LESSONS.md#L28), which is exactly why
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


def _rtds_rich(h: dict | None) -> str:
    """One line for the shared RTDS settlement-stream supervisor, or "" when
    nothing has ever armed on it.

    Worth its own line rather than a per-arm field: it is ONE socket behind
    every stream-fed arm, so when it drops they all gate at once and the
    per-arm reasons all say the same thing. Red the moment it is not
    connected — a dark stream is a fleet-wide event.
    """
    if not h or not (h.get("started") or h.get("events")):
        return ""
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
    return f"{head} [dim]{' · '.join(bits)}[/dim]"


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


def _tape_head(r: dict) -> str:
    """`HH:MM:SS  slug-padded-to-14` — the fixed-width prefix shared by every
    tape-line renderer, so eval/fire/gated/roll/exit lines all column-align.
    14 is the widest current display() form (e.g. "doge 60m 23:40")."""
    import time as _t

    ts = _t.strftime("%H:%M:%S", _t.localtime(r.get("t", 0)))
    return f"{ts}  {_tape_slug(r.get('slug', '')):<14}"


def _tape_render(line: str) -> str | None:
    try:
        r = json.loads(line)
    except ValueError:
        return None
    head = _tape_head(r)

    def money(v: float) -> str:
        return f"${v:,.2f}".rstrip("0").rstrip(".")

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
        best = max(sides, key=lambda s: s["net"], default=None)
        book = (
            f"{best['side']} @ {best['ask']:.2f} {best['net'] * 100:+.1f}¢"
            if best
            else "no book"
        )
        banked = click.style("  BANKED", fg="cyan") if r.get("banked_decided") else ""
        body = (
            f"{head} {_tape_tag('eval')} p↑{r['p_up']:.4f}  {book}"
            f"  ρ{r['rho']:+.2f}  {money(r['committed'])} in"
        )
        extras = "  ".join(x for x in (_safety_ansi(sides, r.get("p_up")), _brake_ansi(sides)) if x)
        return click.style(body, dim=True) + banked + ("  " + extras if extras else "")
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
    return line.rstrip()


class TapeCollapser:
    """Collapse runs of basis-guard-gated evals into ONE live summary line.

    On a quiet tape 99% of records are per-arm "gated basis guard" — four
    arms each printing an identical-shaped line every eval drowned the
    events that matter (fires, exits, brakes, rolls). A run of consecutive
    basis-gated records, any mix of arms, renders as a single line holding
    each arm's FRESHEST margin-vs-guard plus a run count, updated in place.
    Any other event (including theta/brake gates, which are rare and
    meaningful) ends the run and renders normally.
    """

    def __init__(self) -> None:
        self._by: dict[str, str] = {}   # symbol -> freshest "+1.0/6" text
        self._n = 0
        self._t = 0.0
        self._out: str | None = None    # exactly what we last put in the deque

    def _render(self) -> str:
        import time as _t

        ts = _t.strftime("%H:%M:%S", _t.localtime(self._t))
        per = " · ".join(f"{sym} {txt}" for sym, txt in sorted(self._by.items()))
        return click.style(
            f"{ts}  {'':<14} {_tape_tag(f'gated ×{self._n}')} basis bp/guard: {per}",
            fg="yellow", dim=True)

    def add(self, raw: str, lines) -> None:
        """Feed one raw tape line; appends/updates rendered output in `lines`."""
        try:
            r = json.loads(raw)
        except ValueError:
            return
        reason = (r.get("reason") or "") if r.get("ev") == tape.EV_GATED else ""
        if reason.startswith("basis guard"):
            sym = (r.get("slug") or "?").split("-")[0]
            margin, guard = r.get("margin_bp"), r.get("guard_bp")
            if margin is not None and guard is not None:
                self._by[sym] = f"{margin:+.1f}/{guard:.0f}"
            else:
                m = _MARGIN_RE.search(reason)
                self._by[sym] = f"{float(m.group(1)):+.1f}/{float(m.group(2)):.0f}" if m else "?"
            self._n += 1
            self._t = r.get("t", 0.0)
            out = self._render()
            if self._out is not None and lines and lines[-1] == self._out:
                lines[-1] = out
            else:
                lines.append(out)
            self._out = out
            return
        # any other event ends the run and renders normally
        self._by, self._n, self._out = {}, 0, None
        try:
            rendered = _tape_render(raw)
        except Exception:
            return  # torn mid-write line must never take the dashboard down
        if rendered:
            lines.append(rendered)


def _eff_table(s: dict) -> Table:
    """The effectiveness block: each corrected number beside what it means."""
    def signed(v: float | None, unit: str = "", pct: bool = True, digits: int = 2) -> str:
        if v is None:
            return "[dim]—[/dim]"
        x = v * 100 if pct else v
        return f"[{'green' if x >= 0 else 'red'}]{x:+,.{digits}f}{unit}[/]"

    def rate(v: float | None, digits: int = 0) -> str:
        return "[dim]—[/dim]" if v is None else f"{v * 100:.{digits}f}%"

    rorc, bgr = s.get("rorc") or {}, s.get("bgr") or {}
    wr, be = s.get("win_rate"), s.get("breakeven_win_rate")
    # Show the growth denominator in the unit it actually has: "over 0.1d"
    # hides that a %/day figure is extrapolated from three hours.
    span = (f"{s['span_h'] / 24:.1f}d" if s["span_h"] >= 24 else f"{s['span_h']:.1f}h")
    hold_m = (rorc.get("avg_hold_h") or 0) * 60
    t = Table(title="Effectiveness — the win rate, corrected for size and time")
    t.add_column("metric"); t.add_column("value", justify="right"); t.add_column("means")
    t.add_row("$-weighted win rate", rate(s["mww_rate"]),
              "share of DOLLARS at risk that won"
              + (f" [dim](count: {wr * 100:.0f}%)[/dim]" if wr is not None else ""))
    # The bar the headline has to clear: with -100% losses against +2-8%
    # wins it sits in the nineties, which is the whole reason 92% flatters.
    t.add_row("break-even win rate",
              "[dim]—[/dim]" if be is None else
              f"[{'red' if wr is not None and wr < be else 'green'}]{be * 100:.1f}%[/]",
              "what THIS payoff shape needs just to stay flat")
    t.add_row("profit factor",
              "[dim]—[/dim]" if s["profit_factor"] is None else
              f"[{'green' if s['profit_factor'] >= 1 else 'red'}]{s['profit_factor']:.2f}[/]",
              f"gross wins ${s['gross_win']:,.0f} / gross losses ${s['gross_loss']:,.0f}"
              " — under 1.00 the book loses")
    t.add_row("return on notional", signed(s["return_on_notional"], "%"),
              f"P&L per dollar put at risk (${s['notional']:,.0f} traded), time ignored")
    t.add_row("RoRC", signed(rorc.get("per_hour"), "%/h"),
              "return per dollar-HOUR at risk — [bold]trade quality[/bold]"
              + (f" [dim](avg hold {hold_m:.1f}m)[/dim]" if hold_m else ""))
    t.add_row("bankroll growth", signed(bgr.get("per_day_pct"), "%/d", pct=False),
              "log growth of the whole book per calendar day — "
              f"[bold]capital effectiveness[/bold] [dim](over {span})[/dim]")
    t.add_row("utilization", rate(s["utilization"], 2),
              "share of bankroll-hours actually at risk (the bridge: "
              "growth ≈ RoRC × utilization)")
    return t


_UNDECIDED_YELLOW_USD = 300.0  # R7 speculative-exposure threshold zone


_UNDECIDED_RED_USD = 500.0


def _risk_committed(arms: dict | None) -> tuple[float, float]:
    """(committed, undecided) USDC summed across a /status reply's arms.

    committed = every arm's filled_usdc. undecided = the slice of that still
    sitting in arms whose last eval hasn't banked-decided (including a
    missing eval entirely, e.g. right after an engine restart) — that's
    speculative exposure that could still flip.
    """
    committed = undecided = 0.0
    for a in (arms or {}).values():
        if not isinstance(a, dict):
            continue
        filled = a.get("filled_usdc") or 0.0
        committed += filled
        e = a.get("eval")
        if not (isinstance(e, dict) and e.get("banked_decided")):
            undecided += filled
    return committed, undecided


def build_risk_header(status: dict | None, bal: dict | None, sb: dict | None) -> str:
    """`capital $X · committed $Y ($Z un-decided) · riding N windows $W` —
    the one-line risk summary between the scoreboard and the arms table.
    Reads only already-cached data (status/bal/sb) — never fetches."""
    committed, undecided = _risk_committed((status or {}).get("arms"))
    cap = f"${bal['total']:,.2f}" if bal else "…"
    riding_n = (sb or {}).get("riding_n", 0)
    riding_usd = (sb or {}).get("riding_usd", 0.0)
    color = ("red" if undecided > _UNDECIDED_RED_USD else
             "yellow" if undecided > _UNDECIDED_YELLOW_USD else "")
    undecided_s = f"${undecided:,.2f} un-decided"
    if color:
        undecided_s = f"[{color}]{undecided_s}[/{color}]"
    line = (f"capital {cap} · committed ${committed:,.2f} ({undecided_s}) · "
            f"riding {riding_n} windows ${riding_usd:,.2f}")
    # One socket feeds every stream-fed arm, so its state belongs on the
    # fleet's risk line, not buried per-arm.
    rtds = _rtds_rich((status or {}).get("rtds"))
    return f"{line} · {rtds}" if rtds else line


def _window_chip(w: dict) -> str:
    """`✓ btc5 +12` / `✗ eth15 -44` for one resolved window; dim for a
    ~estimated (gamma-unreachable, or gamma-confirmed-win-pending-redeem)
    read rather than the win/loss color, since it's a lower-confidence read."""
    parsed = updown_slugs.parse(w.get("slug", ""))
    label = f"{parsed[0]}{parsed[1] // 60}" if parsed else (w.get("slug", "?")[:8])
    mark = "✓" if w.get("won") else "✗"
    text = f"{mark} {label} {w.get('pnl', 0.0):+.0f}"
    style = "dim" if w.get("est") else ("green" if w.get("won") else "red")
    return f"[{style}]{text}[/{style}]"


def build_windows_strip(windows: list[dict] | None) -> str:
    """Chip row of the last resolved windows, newest first (caller supplies
    an already-sorted/capped list — see _tape_scoreboard's "windows")."""
    chips = [_window_chip(w) for w in (windows or [])]
    return "  ".join(chips) if chips else "[dim]no resolved windows yet[/dim]"


# Fixed widths (+ ellipsis overflow below) so the arms table's geometry never
# jitters as state/reason text length changes tick to tick.
_ARMS_COLUMNS = (
    ("window", "left", 14),
    ("T-", "right", 6),
    ("state", "left", 34),
    ("evidence", "right", 15),
    ("p_up", "right", 6),
    ("mode", "left", 8),
    ("rho", "right", 6),
    ("committed", "right", 12),
    ("roll", "right", 4),
)


def build_arms_table(arms: dict | None, now: float) -> Table:
    """Live-arms table: countdown, state (compact gate reason for a gated
    arm, safety/brake badges for an armed one), banked-vs-cushion evidence,
    model read (p_up/mode/rho), committed $, roll flag. Every cell tolerates
    a missing/partial eval — an engine restart mid-watch leaves last_eval
    None or half-built.
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
        committed_s = f"${committed:,.2f}" if committed is not None else "—"
        # "≈" = fed by the settlement stream rather than the Binance proxy.
        flags = ("⟳" if a.get("roll") else "·") + ("≈" if a.get("feed") == "rtds" else "")
        t.add_row(_tape_slug(slug), _countdown_markup(slug, now), state,
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
             "sliding": dict(_SB_EMPTY_SLIDING)}


def _controls_panel():
    """The 'h' help overlay — swaps into the strip slot so toggling never
    changes the layout geometry."""
    from rich.panel import Panel
    return Panel(
        "[bold]q[/bold] quit · [bold]h[/bold] toggle controls · Ctrl-C also quits"
        "  [dim]|[/dim]  refresh: tape 1s · engine 2s · scoreboard 10s · balance 60s"
        "  [dim]|[/dim]  [dim]--since floors the sliding P&L (hours or epoch)[/dim]",
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
    return Panel(
        f"[bold]{wins}W-{losses}L[/bold] ({wr}) · P&L [{color}]{net:+,.2f}[/] · "
        f"{rolls} rolls · capital {cap} · [dim]{floor_label}[/dim] · {age}"
        f"{all_time}{stale}{est}{err} · "
        f"[dim]{time.strftime('%H:%M:%S')}[/dim]",
        title="updown fleet", border_style="cyan")


