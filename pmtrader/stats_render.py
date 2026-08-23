"""Rendering for `pmt crypto stats` — and nothing else.

Every function here is pure: it takes dicts that have ALREADY been computed
(the scoreboard from score_activity, the effectiveness summary, the CLOB
balance, the engine's /status reply) and returns a Rich renderable. No
network, no aggregation, no clock beyond a floor label. That split is the
point: cli_crypto.py owns fetching and grading, this file owns pixels, and
the report can be redesigned without touching a single number.

The layout is one header panel (the "am I making money" answer, with the
break-even GAP given the loudest cell on the screen because it is the number
that decides profitability) followed by rule-separated sections, in the order
an operator actually asks the questions: what did it earn → where did it come
from → is that any good once corrected for size and time → is the model
honest → what is live right now.

Colors follow the house convention shared with `pmt crypto watch`:
green = win/evidence, yellow = gated/partial, red = loss/brake, dim = meta.
"""

from __future__ import annotations

from datetime import datetime, timezone

from rich.console import Group
from rich.panel import Panel
from rich.rule import Rule
from rich.table import Table
from rich.text import Text

from cli_common import _pnl_color
from polymarket import updown_slugs

# Sub-cell resolution matters here: every win rate on this report lives in the
# 80-100% band, where a whole-block bar has 10pp of resolution and shows the
# best and worst series as the same length.
_EIGHTHS = "▏▎▍▌▋▊▉█"
_BAR_W = 10

# A rate this far under the bar it has to clear is "close", not "failing" —
# the amber step between green (clear) and red (bleeding).
_NEAR_MISS = 0.05

_HEADER_BORDER = "cyan"  # matches watch's "updown fleet" panel


# ---------- scalar formatting ----------

def _zeroed(v: float) -> float:
    """-0.00 is noise from float drift, not a number; print it as zero."""
    return 0.0 if abs(v) < 0.005 else v


def _money(v: float | None, signed: bool = True) -> str:
    if v is None:
        return "[dim]—[/dim]"
    v = _zeroed(v)
    if not signed:
        return f"${v:,.2f}"
    return f"[{_pnl_color(v)}]{v:+,.2f}[/]"


def _pct(v: float | None, digits: int = 0, signed: bool = False) -> str:
    """A rate in [0,1] as a percentage; '—' for the undefined case, which is
    not the same thing as zero and must never render as zero."""
    if v is None:
        return "[dim]—[/dim]"
    return f"{v * 100:{'+' if signed else ''}.{digits}f}%"


def _signed_pct(v: float | None, unit: str = "%", digits: int = 2,
                scale: bool = True) -> str:
    if v is None:
        return "[dim]—[/dim]"
    x = v * 100 if scale else v
    return f"[{_pnl_color(x)}]{x:+,.{digits}f}{unit}[/]"


def _bar(frac: float | None, style: str = "cyan", width: int = _BAR_W) -> str:
    """A fractional block bar. Empty cells stay dim so the bar's full scale
    is always visible — a short bar reads as "20% of the way", not as a
    narrow column."""
    if frac is None:
        return f"[dim]{'·' * width}[/dim]"
    frac = min(max(frac, 0.0), 1.0)
    total = frac * width
    full = int(total)
    part = _EIGHTHS[int((total - full) * 8) - 1] if int((total - full) * 8) else ""
    if full >= width:
        full, part = width, ""
    empty = width - full - len(part)
    return f"[{style}]{'█' * full}{part}[/{style}][dim]{'░' * empty}[/dim]"


def _rate_style(rate: float | None, bar: float | None) -> str:
    """Green once a rate clears the bar it is measured against, amber within
    a near-miss of it, red below. `bar` is the break-even win rate for a
    series and the stated fair for a calibration bucket — same question in
    both places: did this clear what it promised?"""
    if rate is None or bar is None:
        return "cyan"
    if rate >= bar:
        return "green"
    return "yellow" if rate >= bar - _NEAR_MISS else "red"


def floor_label(floor: float) -> str:
    if floor <= 0:
        return "all time"
    stamp = datetime.fromtimestamp(floor, tz=timezone.utc).strftime("%m-%d %H:%MZ")
    return f"windows since {stamp}"


# ---------- header ----------

def _record_line(sb: dict, bal: dict | None) -> str:
    wins, losses = sb.get("wins", 0), sb.get("losses", 0)
    n = wins + losses
    # One decimal, not zero: rounded to "92%" the headline rate reads as if it
    # were level with a 92.5% break-even bar it is in fact under.
    wr = _pct(wins / n, 1) if n else "[dim]—[/dim]"
    cap = f"[bold]${bal['total']:,.2f}[/bold]" if bal else "[dim]?[/dim]"
    net = _zeroed(sb.get("net", 0.0))
    return (f"[bold]{wins}W-{losses}L[/bold] [dim]({wr})[/dim]"
            f"     [dim]P&L[/dim] [bold {_pnl_color(net)}]{net:+,.2f}[/]"
            f"     [dim]capital[/dim] {cap}"
            f"     [dim]{sb.get('rolls', 0)} rolls[/dim]")


def _gap_line(eff: dict) -> str:
    """The break-even GAP: the count win rate minus the win rate THIS payoff
    shape needs to stay flat. Positive is the whole game; a 92% win rate
    against a 92.5% bar is a losing book, and that fact deserves to be the
    loudest thing on the report rather than row two of a seven-row table.
    """
    wr, be = eff.get("win_rate"), eff.get("breakeven_win_rate")
    if be is None or wr is None:
        return "[dim]break-even — not enough decided windows to size the bar yet[/dim]"
    gap = (wr - be) * 100
    if gap >= 0:
        verdict, style = "clear of break-even", "green"
    else:
        verdict, style = "SHORT of break-even", "red"
    return (f"[dim]break-even[/dim]  need [bold]{be * 100:.1f}%[/bold]"
            f"  [dim]·[/dim]  actual [bold]{wr * 100:.1f}%[/bold]"
            f"  [dim]·[/dim]  [bold {style}]GAP {gap:+.1f}pp[/]  [{style}]{verdict}[/]")


def _eff_line(eff: dict) -> str:
    """The effectiveness story compressed to one line — the same numbers the
    section below explains, here for the read that doesn't scroll."""
    rorc, bgr = eff.get("rorc") or {}, eff.get("bgr") or {}
    span_h = eff.get("span_h") or 0.0
    span = f"{span_h / 24:.1f}d" if span_h >= 24 else f"{span_h:.1f}h"
    parts = [
        f"[dim]$W[/dim] {_pct(eff.get('mww_rate'))}",
        f"[dim]PF[/dim] " + ("[dim]—[/dim]" if eff.get("profit_factor") is None else
                             f"[{_pnl_color(eff['profit_factor'] - 1)}]"
                             f"{eff['profit_factor']:.2f}[/]"),
        f"[dim]$ret[/dim] {_signed_pct(eff.get('return_on_notional'))}",
        f"[dim]RoRC[/dim] {_signed_pct(rorc.get('per_hour'), '%/h')}",
        f"[dim]BGR[/dim] {_signed_pct(bgr.get('per_day_pct'), '%/d', scale=False)}",
        f"[dim]util[/dim] {_pct(eff.get('utilization'), 2)}",
    ]
    return "  [dim]·[/dim]  ".join(parts) + f"   [dim]over {span}[/dim]"


def _risk_line(sb: dict, status: dict | None) -> str:
    """Money currently exposed: an arm's committed budget (and how much of it
    hasn't banked-decided yet) plus notional still riding in unresolved
    windows. Shares _risk_committed with the watch header so the two reports
    can never disagree about what is at risk."""
    from cli_crypto import _UNDECIDED_RED_USD, _UNDECIDED_YELLOW_USD, _risk_committed

    committed, undecided = _risk_committed((status or {}).get("arms"))
    style = ("red" if undecided > _UNDECIDED_RED_USD else
             "yellow" if undecided > _UNDECIDED_YELLOW_USD else "dim")
    return (f"[dim]committed[/dim] ${_zeroed(committed):,.2f} "
            f"[{style}](${_zeroed(undecided):,.2f} un-decided)[/{style}]"
            f"  [dim]·[/dim]  [dim]riding[/dim] {sb.get('riding_n', 0)} windows "
            f"${sb.get('riding_usd', 0.0):,.2f}")


def header_panel(sb: dict, eff: dict, bal: dict | None, status: dict | None,
                 floor: float) -> Panel:
    """Record, P&L, capital, the break-even gap, the effectiveness one-liner
    and live exposure — everything the operator came for, in four lines."""
    lines = [_record_line(sb, bal), _gap_line(eff)]
    if eff.get("n"):
        lines.append(_eff_line(eff))
    lines.append(_risk_line(sb, status))
    est = sb.get("estimated") or 0
    if est:
        lines.append(f"[dim]{est} ~estimated "
                     f"(gamma unreachable or pending redeem)[/dim]")
    # expand=False: the panel hugs its own content instead of stretching to a
    # 200-column terminal, which is what keeps the report reading as a card
    # rather than a banner. Rich still clamps it on a narrow terminal.
    return Panel(Text.from_markup("\n".join(lines)), expand=False,
                 title=f"[bold]updown fleet[/bold] [dim]· {floor_label(floor)}[/dim]",
                 title_align="left", border_style=_HEADER_BORDER, padding=(0, 1))


# ---------- sections ----------

def section(title: str, note: str = "") -> Rule:
    """`── by series · wallet-graded ─────────`. The leading dashes are part of
    the title because Rich only draws the rule to one side of a left-aligned
    one, and a section header flush against column zero doesn't read as a
    divider."""
    label = f"[dim]──[/dim] [bold]{title}[/bold]" + (f" [dim]· {note}[/dim]" if note else "")
    return Rule(label, style="dim", align="left")


def series_table(series: dict, breakeven: float | None = None) -> Table:
    """Per-series record and P&L, with each series' win rate drawn against
    the break-even bar: a green bar is a series paying for itself, a red one
    is a series whose hit rate cannot cover its payoff shape.
    """
    t = Table(box=None, pad_edge=False, padding=(0, 1))
    t.add_column("series", justify="left", width=8, no_wrap=True)
    t.add_column("W-L", justify="right", width=6, no_wrap=True)
    t.add_column("", justify="left", width=9, no_wrap=True)  # open / ~estimated
    t.add_column("P&L", justify="right", width=9, no_wrap=True)
    t.add_column("notional", justify="right", width=8, no_wrap=True)
    t.add_column("win %", justify="right", width=6, no_wrap=True)
    t.add_column("", justify="left", width=_BAR_W, no_wrap=True)
    for k in sorted(series):
        s = series[k]
        decided = s["w"] + s["l"]
        rate = s["w"] / decided if decided else None
        flags = " ".join(x for x in (
            f"{s['open']} open" if s.get("open") else "",
            f"~{s['est']}" if s.get("est") else "") if x)
        t.add_row(k, f"{s['w']}-{s['l']}",
                  f"[dim]{flags}[/dim]" if flags else "",
                  _money(s["pnl"]), f"${s['usd']:,.0f}",
                  _pct(rate), _bar(rate, _rate_style(rate, breakeven)))
    return t


def effectiveness_table(eff: dict) -> Table:
    """Each corrected number beside what it means. Borderless: the box drawing
    was costing four columns that the explanation column needed, which is what
    made the bankroll-growth row wrap in the first place.
    """
    rorc, bgr = eff.get("rorc") or {}, eff.get("bgr") or {}
    wr, be = eff.get("win_rate"), eff.get("breakeven_win_rate")
    span_h = eff.get("span_h") or 0.0
    # Show the growth denominator in the unit it actually has: "over 0.1d"
    # hides that a %/day figure is extrapolated from three hours.
    span = f"{span_h / 24:.1f}d" if span_h >= 24 else f"{span_h:.1f}h"
    hold_m = (rorc.get("avg_hold_h") or 0) * 60

    t = Table(box=None, pad_edge=False, padding=(0, 1))
    t.add_column("metric", justify="left", width=20, no_wrap=True)
    t.add_column("value", justify="right", width=10, no_wrap=True)
    t.add_column("means", justify="left", overflow="fold")
    t.add_row("$-weighted win rate", _pct(eff.get("mww_rate")),
              "share of DOLLARS at risk that won"
              + (f" [dim](count: {wr * 100:.1f}%)[/dim]" if wr is not None else ""))
    t.add_row("break-even win rate",
              "[dim]—[/dim]" if be is None else
              f"[{_rate_style(wr, be)}]{be * 100:.1f}%[/]",
              "what THIS payoff shape needs just to stay flat")
    t.add_row("profit factor",
              "[dim]—[/dim]" if eff.get("profit_factor") is None else
              f"[{_pnl_color(eff['profit_factor'] - 1)}]{eff['profit_factor']:.2f}[/]",
              f"gross wins ${eff['gross_win']:,.0f} / gross losses "
              f"${eff['gross_loss']:,.0f} — under 1.00 the book loses")
    t.add_row("return on notional", _signed_pct(eff.get("return_on_notional")),
              f"P&L per dollar put at risk (${eff['notional']:,.0f} traded), "
              "time ignored")
    t.add_row("RoRC", _signed_pct(rorc.get("per_hour"), "%/h"),
              "return per dollar-HOUR at risk — [bold]trade quality[/bold]"
              + (f" [dim](avg hold {hold_m:.1f}m)[/dim]" if hold_m else ""))
    t.add_row("bankroll growth", _signed_pct(bgr.get("per_day_pct"), "%/d", scale=False),
              "log growth of the whole book per calendar day — "
              f"[bold]capital effectiveness[/bold] [dim](over {span})[/dim]")
    t.add_row("utilization", _pct(eff.get("utilization"), 2),
              "share of bankroll-hours actually at risk "
              "(the bridge: growth ≈ RoRC × utilization)")
    return t


def calibration_table(cal: dict) -> Table:
    """Did a clip fired at a stated fair actually hit that often? The bar is
    colored against the bucket's OWN fair, so an over-confident bucket goes
    red without the operator doing the arithmetic."""
    t = Table(box=None, pad_edge=False, padding=(0, 1))
    t.add_column("fair ≥", justify="right", width=6, no_wrap=True)
    t.add_column("clips", justify="right", width=6, no_wrap=True)
    t.add_column("hits", justify="right", width=9, no_wrap=True)
    t.add_column("hit %", justify="right", width=6, no_wrap=True)
    t.add_column("", justify="left", width=_BAR_W, no_wrap=True)
    for b in sorted(cal):
        tot, hit = cal[b]
        rate = hit / tot if tot else None
        style = _rate_style(rate, b)
        t.add_row(f"{b:.2f}", f"{tot:,}", f"[dim]{hit:,}/{tot:,}[/dim]",
                  f"[{style}]{_pct(rate)}[/{style}]", _bar(rate, style))
    return t


_ARMS_COLUMNS = (
    ("window", "left", 14),
    ("state", "left", 34),
    ("fair", "right", 8),
    ("committed", "right", 11),
    ("roll", "center", 4),
)


def arms_table(arms: dict | None) -> Table:
    """Live arms. Fixed widths with ellipsis overflow: a basis-guard reason is
    a whole sentence, and letting it wrap tore every other column's alignment
    apart — it goes through the same compact `margin -4.9 vs 6.0bp` renderer
    the watch dashboard uses."""
    from cli_crypto import _gated_reason_compact

    t = Table(box=None, pad_edge=False, padding=(0, 1))
    for col, justify, width in _ARMS_COLUMNS:
        t.add_column(col, justify=justify, width=width, no_wrap=True,
                     overflow="ellipsis")
    for slug, a in (arms or {}).items():
        a = a if isinstance(a, dict) else {}
        e = a.get("eval")
        e = e if isinstance(e, dict) else {}
        state = e.get("state", "?")
        if state == "gated":
            state = f"[yellow]gated[/yellow] [dim]{_gated_reason_compact(e.get('reason'), e)}[/dim]"
        elif state == "armed":
            state = "[green]armed[/green]"
        else:
            state = f"[dim]{state}[/dim]"
        fair = f"{e['p_up']:.4f}" if "p_up" in e else "[dim]—[/dim]"
        committed = e.get("committed", a.get("filled_usdc", 0)) or 0.0
        t.add_row(updown_slugs.display(slug), state, fair,
                  f"${_zeroed(committed):,.2f}",
                  "[green]⟳[/green]" if a.get("roll") else "[dim]·[/dim]")
    return t


# ---------- the whole report ----------

def render_stats(sb: dict, eff: dict, bal: dict | None, status: dict | None,
                 floor: float) -> Group:
    """The single renderable `pmt crypto stats` prints. Sections that have no
    data drop out entirely rather than printing an empty box."""
    status = status or {}
    parts: list = [header_panel(sb, eff, bal, status, floor), ""]

    series = sb.get("series") or {}
    if series:
        parts += [section("by series", "wallet-graded"),
                  series_table(series, eff.get("breakeven_win_rate")), ""]

    if eff.get("n"):
        parts += [section("effectiveness", "the win rate, corrected for size and time"),
                  effectiveness_table(eff), ""]

    cal = sb.get("cal") or {}
    if cal:
        parts += [section("calibration", "clips fired at stated fair vs realized"),
                  calibration_table(cal), ""]

    arms = status.get("arms") or {}
    if arms:
        parts += [section("live arms"), arms_table(arms)]
        if status.get("pending_rolls"):
            parts.append(f"[dim]pending rolls: {', '.join(status['pending_rolls'])}[/dim]")
    return Group(*parts)
