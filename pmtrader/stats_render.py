"""Rendering for `pmt crypto stats` — and nothing else.

Every function here is pure: it takes dicts that have ALREADY been computed
(the scoreboard from score_activity, the effectiveness summary, the CLOB
balance, the engine's /status reply, the tape folds from
polymarket.updown_stats) and returns a Rich renderable. No network, no
aggregation, no clock beyond a floor label. That split is the point:
cli_crypto_stats.py owns fetching and grading, this file owns pixels, and the
report can be redesigned without touching a single number.

The default report reads top-down in the order an operator actually asks:

    identity   who are we — a label/value GRID: record+streak, the current
               era's record beside it, P&L+capital, the break-even bar, live
               exposure, the fleet ration, the feed
    by era     the same record cut at the deploy moments that moved it
    by symbol  where it came from, on which feed, and what a TYPICAL window
               of that series pays
    effectiveness   is any of that good once corrected for size and time
    experiments     resting bids and pay-up — printed only when the tape has
               something to say about them

`--full` restores the two blocks that were demoted rather than deleted:
calibration (superseded by analysis/r6_report.txt) and live arms (which is
`pmt crypto watch`'s job, live, with four more columns).

Nothing here may wrap at 100 columns. Every table is fixed-width and
no-wrap; the prose in the effectiveness table is written to the width it
has, not trimmed by the terminal.

Colors follow the house convention shared with `pmt crypto watch`:
green = win/evidence, yellow = gated/partial, red = loss/brake, dim = meta.
"""

from __future__ import annotations

from datetime import datetime, timezone

from rich.console import Group
from rich.panel import Panel
from rich.rule import Rule
from rich.table import Table

from cli_common import _pnl_color

# Sub-cell resolution matters here: every win rate on this report lives in the
# 80-100% band, where a whole-block bar has 10pp of resolution and shows the
# best and worst series as the same length.
_EIGHTHS = "▏▎▍▌▋▊▉█"
_BAR_W = 10

# A rate this far under the bar it has to clear is "close", not "failing" —
# the amber step between green (clear) and red (bleeding).
_NEAR_MISS = 0.05

_HEADER_BORDER = "cyan"  # matches watch's "updown fleet" panel

# Same glyphs watch_ui._ARMS_FLAG_LEGEND uses, so a flag means one thing
# across both reports.
_RTDS_MARK = "≈"
_MAKER_MARK = "◇"


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


def _ms(v: float | None) -> str:
    return "[dim]—[/dim]" if v is None else f"{v:,.0f}ms"


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


def _stamp(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%m-%d %H:%MZ")


def era_span_label(start: float, end: float) -> str:
    """`05:00Z→10:39Z` — an era's boundaries, short enough for a panel title.

    `open` on the left edge and `now` on the right are the two honest ends:
    the first era has no left boundary by design (nothing may fall outside
    the registry) and the last one has not ended.
    """
    lo = "open" if start <= 0 else datetime.fromtimestamp(
        start, tz=timezone.utc).strftime("%H:%MZ")
    hi = "now" if end == float("inf") else datetime.fromtimestamp(
        end, tz=timezone.utc).strftime("%H:%MZ")
    return f"{lo}→{hi}"


def _span_label(span_h: float | None) -> str:
    """A duration in the unit it actually has: "over 0.1d" hides that a
    %/day figure is extrapolated from three hours."""
    h = span_h or 0.0
    return f"{h / 24:.1f}d" if h >= 24 else f"{h:.1f}h"


# ---------- header ----------

def _streak_text(eff: dict) -> str:
    """`streak 7 (best 41)` — the run of wins the next loss will end.

    With losses paying -100% against wins paying a few percent, run length
    between losses is the pulse the P&L total smooths away.
    """
    st = eff.get("streak") or {}
    cur, best = st.get("current"), st.get("longest")
    if best is None:
        return ""
    style = "green" if cur else "dim"
    return f"[dim]streak[/dim] [{style}]{cur}[/{style}] [dim](best {best})[/dim]"


# The identity box is a LABEL/VALUE GRID, not stacked prose. Every row names
# itself in the label column and every value field starts at the same column,
# so the record, the era, the money, the bar, the exposure and the ration read
# down as a column of comparable figures instead of six ragged sentences —
# and the record row can no longer wrap "streak 101 (best 101)" across a line
# break the way a dot-joined line did once the numbers grew a digit.
#
# Widths are fixed so the shape is stable between runs and between eras, and
# each is sized to the LONGEST value its column can hold, so a number never
# loses digits to an ellipsis:
#   v1 "committed $12,345.67" (20) — was 18, which cut a four-figure exposure
#   v2 "peak un-decided $1,500" (22) — was 21, which cut the ration itself
#   v3 "GAP -0.1pp  SHORT of break-even" (31); the note cells are written to it
# Budget: 13 + 20 + 22 + 31 = 86, plus Rich's inter-column padding, plus the
# panel's border and padding — under 100 columns, which is the report's law.
# The label column holds the longest label the box can emit, `era pre-brake`.
_HDR_LABEL_W = 13
_HDR_V1_W = 20
_HDR_V2_W = 22
_HDR_V3_W = 31


def _record_cells(sb: dict, eff: dict) -> tuple:
    """`record | 147W-13L (91.9%) | streak 7 (best 41) | 335 rolls`."""
    wins, losses = sb.get("wins", 0), sb.get("losses", 0)
    n = wins + losses
    # One decimal, not zero: rounded to "92%" the headline rate reads as if it
    # were level with a 92.5% break-even bar it is in fact under.
    wr = _pct(wins / n, 1) if n else "[dim]—[/dim]"
    return ("record", f"[bold]{wins}W-{losses}L[/bold] [dim]({wr})[/dim]",
            _streak_text(eff), f"[dim]{sb.get('rolls', 0)} rolls[/dim]")


def _money_cells(sb: dict, bal: dict | None) -> tuple:
    """`P&L | -436.76 | capital $1,649.14 |`.

    Split off the record row rather than trailing it: two money figures and a
    W-L on one line is what pushed the old identity line past 100 columns.
    """
    cap = f"[dim]capital[/dim] [bold]${bal['total']:,.2f}[/bold]" if bal \
        else "[dim]capital ?[/dim]"
    net = _zeroed(sb.get("net", 0.0))
    return ("P&L", f"[bold {_pnl_color(net)}]{net:+,.2f}[/]", cap, "")


def era_cells(era: dict | None, scoped: bool = False) -> tuple | None:
    """`era stream | 12W-1L (92.3%) | P&L +34.56 | vs all-time above`.

    Deliberately the same column shape as the record row directly above it, so
    the era's W-L sits under all-time's W-L and the two are read by eye rather
    than by arithmetic. That comparison IS the point of the row.

    `scoped` says the whole report is already inside this era (`--era`), so the
    row above is NOT all-time and the note must not claim it is — the same two
    numbers on both rows is then the honest confirmation that scoping took.
    """
    if not era:
        return None
    sb = era["sb"]
    wins, losses = sb.get("wins", 0), sb.get("losses", 0)
    n = wins + losses
    wr = _pct(wins / n, 1) if n else "[dim]—[/dim]"
    net = _zeroed(sb.get("net", 0.0))
    note = "scoped: drop --era for all-time" if scoped else "vs all-time above"
    return (f"era {era['name']}",
            f"[bold]{wins}W-{losses}L[/bold] [dim]({wr})[/dim]",
            f"[dim]P&L[/dim] [bold {_pnl_color(net)}]{net:+,.2f}[/]",
            f"[dim]{note}[/dim]")


def _gap_cells(eff: dict) -> tuple:
    """The break-even GAP: the count win rate minus the win rate THIS payoff
    shape needs to stay flat. Positive is the whole game; a 92% win rate
    against a 92.5% bar is a losing book, and that fact deserves to be the
    loudest thing on the report rather than row two of a seven-row table.
    """
    wr, be = eff.get("win_rate"), eff.get("breakeven_win_rate")
    if be is None or wr is None:
        # Empty value cells rather than a fabricated 0.0%: the bar is not zero,
        # it is unknown, and the row says which.
        return ("break-even", "", "",
                "[dim]too few decided windows to size it[/dim]")
    gap = (wr - be) * 100
    if gap >= 0:
        verdict, style = "clear of break-even", "green"
    else:
        verdict, style = "SHORT of break-even", "red"
    return ("break-even", f"[dim]need[/dim] [bold]{be * 100:.1f}%[/bold]",
            f"[dim]actual[/dim] [bold]{wr * 100:.1f}%[/bold]",
            f"[bold {style}]GAP {gap:+.1f}pp[/]  [{style}]{verdict}[/]")


def _fleet_cells(fleet: dict | None) -> tuple | None:
    """The R7 ration and how close the fleet came to it. Absent entirely
    when no cap is set — an uncapped fleet has no headroom to report."""
    if not fleet or not fleet.get("cap"):
        return None
    peak, cap = fleet.get("peak_undecided"), fleet["cap"]
    if peak is None:
        return ("fleet cap", f"[dim]${cap:,.0f}[/dim]", "",
                "[dim]no capped ticks in range[/dim]")
    style = "yellow" if peak >= cap * 0.9 else "dim"
    tail = f"[dim]over {fleet['ticks']:,} ticks[/dim]"
    if fleet.get("blocked_usd", 0.0) > 0.5:
        tail += f"[dim] · refused ${fleet['blocked_usd']:,.0f}[/dim]"
    return ("fleet cap", f"[dim]${cap:,.0f}[/dim]",
            f"[dim]peak un-decided[/dim] [{style}]${peak:,.0f}[/{style}]", tail)


def _exposure_rows(status: dict | None, sb: dict) -> list[tuple]:
    """The live exposure rows — watch_ui's, not a second copy.

    watch_ui.exposure_rows builds the SAME tuples the dashboard's own header
    grid renders, so this report and `pmt crypto watch` cannot disagree about
    what is at risk or about which column it sits in.
    """
    from watch_ui import exposure_rows

    return exposure_rows(status, sb)


def _feed_row(status: dict | None) -> tuple | None:
    """The shared settlement socket's health row — again watch_ui's, so one
    socket has one presentation across both views."""
    from watch_ui import feed_row

    return feed_row(status)


def header_grid(sb: dict, eff: dict, bal: dict | None, status: dict | None,
                fleet: dict | None = None, era_now: dict | None = None,
                scoped: bool = False) -> Table:
    """The identity box's rows, as one aligned grid.

    Row order is the order an operator reads: who we are, who we are RIGHT
    NOW, the money, the bar the money has to clear, what is at risk, the
    ration, the feed. A row with nothing to say is dropped, never padded with
    a zero.
    """
    rows: list[tuple] = [_record_cells(sb, eff)]
    era = era_cells(era_now, scoped=scoped)
    if era:
        rows.append(era)
    rows += [_money_cells(sb, bal), _gap_cells(eff)]
    rows += _exposure_rows(status, sb)
    fleet_row = _fleet_cells(fleet)
    if fleet_row:
        rows.append(fleet_row)
    feed = _feed_row(status)
    if feed:
        rows.append(feed)
    est = sb.get("estimated") or 0
    if est:
        rows.append(("estimated", f"[dim]{est} windows[/dim]", "",
                     "[dim]gamma dark or a redeem pending[/dim]"))

    t = Table(box=None, pad_edge=False, padding=(0, 1), show_header=False)
    t.add_column("label", justify="left", width=_HDR_LABEL_W, no_wrap=True,
                 overflow="ellipsis", style="dim")
    # max_width, not width, on the value columns: the grid still aligns (a
    # table renders one width per column for every row) but a sparse box hugs
    # its content instead of padding out to a fixed 86 on a narrow terminal.
    #
    # overflow="fold", not "ellipsis": on a terminal too narrow for the grid,
    # Rich squeezes the columns, and a squeezed money cell used to lose its
    # last digits ("240W-19L (92…", "peak un-decided $…"). Wrapping keeps the
    # number whole, which is the only property this box may not trade away.
    for w in (_HDR_V1_W, _HDR_V2_W, _HDR_V3_W):
        t.add_column(justify="left", max_width=w, overflow="fold")
    for row in rows:
        t.add_row(*row)
    return t


def header_panel(sb: dict, eff: dict, bal: dict | None, status: dict | None,
                 floor: float, fleet: dict | None = None,
                 era_now: dict | None = None, scope_label: str | None = None) -> Panel:
    """Identity, the current era, the break-even gap, live exposure, the ration
    — one label/value grid, every value field on the same column."""
    grid = header_grid(sb, eff, bal, status, fleet, era_now,
                       scoped=scope_label is not None)
    # expand=False: the panel hugs its own content instead of stretching to a
    # 200-column terminal, which is what keeps the report reading as a card
    # rather than a banner. Rich still clamps it on a narrow terminal.
    return Panel(grid, expand=False,
                 title=f"[bold]updown fleet[/bold] "
                       f"[dim]· {scope_label or floor_label(floor)}[/dim]",
                 title_align="left", border_style=_HEADER_BORDER, padding=(0, 1))


# ---------- sections ----------

def section(title: str, note: str = "") -> Rule:
    """`── by symbol · wallet-graded ─────────`. The leading dashes are part of
    the title because Rich only draws the rule to one side of a left-aligned
    one, and a section header flush against column zero doesn't read as a
    divider."""
    label = f"[dim]──[/dim] [bold]{title}[/bold]" + (f" [dim]· {note}[/dim]" if note else "")
    return Rule(label, style="dim", align="left")


def _feed_cell(flags: dict | None) -> str:
    """`binance` / `≈rtds` / `≈rtds ◇` — the market-data source this series
    is armed on, plus the maker-bid flag. Dim when nothing is armed: the
    series traded, but no live arm is claiming these params right now."""
    if not flags:
        return "[dim]—[/dim]"
    feed = flags.get("feed") or "binance"
    cell = f"[cyan]{_RTDS_MARK}rtds[/cyan]" if feed == "rtds" else "[dim]binance[/dim]"
    if flags.get("maker_bid"):
        cell += f" [cyan]{_MAKER_MARK}[/cyan]"
    return cell


def symbol_table(series: dict, flags: dict | None = None,
                 breakeven: float | None = None) -> Table:
    """Per-series record, feed, P&L and win rate, with each series' win rate
    drawn against the break-even bar: a green bar is a series paying for
    itself, a red one is a series whose hit rate cannot cover its payoff.

    `median` is the row the totals hide. One -$300 tail on forty +$4 windows
    reads as a broken series by sum and a working one by median, and which
    of those is true is the whole sizing question.
    """
    flags = flags or {}
    t = Table(box=None, pad_edge=False, padding=(0, 1))
    t.add_column("symbol", justify="left", width=8, no_wrap=True)
    t.add_column("feed", justify="left", width=9, no_wrap=True)
    t.add_column("W-L", justify="right", width=6, no_wrap=True)
    t.add_column("", justify="left", width=8, no_wrap=True)  # open / ~estimated
    t.add_column("net", justify="right", width=9, no_wrap=True)
    t.add_column("median", justify="right", width=8, no_wrap=True)
    t.add_column("notional", justify="right", width=8, no_wrap=True)
    t.add_column("win %", justify="right", width=6, no_wrap=True)
    t.add_column("", justify="left", width=_BAR_W, no_wrap=True)
    for k in sorted(series):
        s = series[k]
        decided = s["w"] + s["l"]
        rate = s["w"] / decided if decided else None
        marks = " ".join(x for x in (
            f"{s['open']} open" if s.get("open") else "",
            f"~{s['est']}" if s.get("est") else "") if x)
        t.add_row(k, _feed_cell(flags.get(k)), f"{s['w']}-{s['l']}",
                  f"[dim]{marks}[/dim]" if marks else "",
                  _money(s["pnl"]), _money(s.get("med")), f"${s['usd']:,.0f}",
                  _pct(rate), _bar(rate, _rate_style(rate, breakeven)))
    return t


def era_table(era_rows: list[dict], marked: str | None = None) -> Table:
    """The record cut at the policy boundaries that actually moved it.

    This earns default placement because it answers the question the all-time
    line cannot: the headline sums windows fired under brakes that no longer
    exist, off a book that was a 2s REST poller, at sizes since scaled — and
    the operator's standing question is whether the CURRENT policy is paying.

    Every era in polymarket.eras is a row, including ones with nothing in
    them. An era rendering `0-0` is information; an era omitted for being
    empty is how a bad regime gets quietly forgotten. `--era` scopes the rest
    of the report and never this table.

    GAP is that era's own win rate minus the break-even rate ITS OWN payoff
    shape needed — a bar computed over all time would grade a regime against
    a payoff structure it never traded.
    """
    t = Table(box=None, pad_edge=False, padding=(0, 1))
    t.add_column("era", justify="left", width=10, no_wrap=True)
    t.add_column("from", justify="left", width=12, no_wrap=True)
    t.add_column("span", justify="right", width=7, no_wrap=True)
    t.add_column("W-L", justify="right", width=7, no_wrap=True)
    t.add_column("net", justify="right", width=10, no_wrap=True)
    t.add_column("win %", justify="right", width=6, no_wrap=True)
    t.add_column("need", justify="right", width=6, no_wrap=True)
    t.add_column("GAP", justify="right", width=8, no_wrap=True)
    t.add_column("", justify="left", width=3, no_wrap=True)
    for r in era_rows:
        sb = r["sb"]
        wins, losses = sb.get("wins", 0), sb.get("losses", 0)
        decided = wins + losses
        rate = wins / decided if decided else None
        be = r.get("breakeven")
        gap = None if (rate is None or be is None) else (rate - be) * 100
        name = r["name"]
        t.add_row(
            f"[bold cyan]{name}[/]" if name == marked else name,
            "[dim]open[/dim]" if r["start"] <= 0 else _stamp(r["start"]),
            "[dim]—[/dim]" if r.get("span_h") is None else _span_label(r["span_h"]),
            f"{wins}-{losses}",
            _money(sb.get("net", 0.0)) if decided else "[dim]—[/dim]",
            f"[{_rate_style(rate, be)}]{_pct(rate)}[/]",
            _pct(be),
            "[dim]—[/dim]" if gap is None else f"[{_pnl_color(gap)}]{gap:+.1f}pp[/]",
            "[cyan]◀[/cyan]" if name == marked else "")
    return t


def era_footnote(era_rows: list[dict], marked: str | None = None) -> list[str]:
    """Say the rules out loud, on the report, every time it prints."""
    lines = [
        "[dim]boundaries are DEPLOY moments (commit / recorded deploy), cited per era in "
        "polymarket/eras.py.[/dim]",
        f"[dim]all {len(era_rows)} eras listed — none may be hidden; all-time above stays "
        "the ledger of record.[/dim]",
    ]
    if marked:
        why = next((r["why"] for r in era_rows if r["name"] == marked), "")
        if why:
            lines.append(f"[cyan]◀ {marked}[/cyan] [dim]— {why}[/dim]")
    return lines


def effectiveness_table(eff: dict) -> Table:
    """Each corrected number beside what it means. Borderless, and every
    explanation is written to fit the width it has at 100 columns — the box
    drawing plus a longer sentence is what used to wrap the growth row.
    """
    rorc, bgr = eff.get("rorc") or {}, eff.get("bgr") or {}
    wr, be = eff.get("win_rate"), eff.get("breakeven_win_rate")
    span = _span_label(eff.get("span_h"))
    hold_m = (rorc.get("avg_hold_h") or 0) * 60

    t = Table(box=None, pad_edge=False, padding=(0, 1))
    t.add_column("metric", justify="left", width=20, no_wrap=True)
    t.add_column("value", justify="right", width=10, no_wrap=True)
    t.add_column("means", justify="left", overflow="fold")
    t.add_row("$-weighted win rate", _pct(eff.get("mww_rate")),
              "share of DOLLARS at risk that won"
              + (f" [dim](count {wr * 100:.1f}%)[/dim]" if wr is not None else ""))
    t.add_row("break-even win rate",
              "[dim]—[/dim]" if be is None else
              f"[{_rate_style(wr, be)}]{be * 100:.1f}%[/]",
              "what THIS payoff shape needs just to stay flat")
    t.add_row("profit factor",
              "[dim]—[/dim]" if eff.get("profit_factor") is None else
              f"[{_pnl_color(eff['profit_factor'] - 1)}]{eff['profit_factor']:.2f}[/]",
              f"gross wins ${eff['gross_win']:,.0f} / losses "
              f"${eff['gross_loss']:,.0f} — under 1.00 loses")
    t.add_row("return on notional", _signed_pct(eff.get("return_on_notional")),
              f"P&L per dollar at risk (${eff['notional']:,.0f} traded), "
              "time ignored")
    t.add_row("RoRC", _signed_pct(rorc.get("per_hour"), "%/h"),
              "return per dollar-HOUR at risk — [bold]trade quality[/bold]"
              + (f" [dim](hold {hold_m:.1f}m)[/dim]" if hold_m else ""))
    t.add_row("bankroll growth", _signed_pct(bgr.get("per_day_pct"), "%/d", scale=False),
              "book-wide log growth per day — [bold]capital "
              f"effectiveness[/bold] [dim]({span})[/dim]")
    t.add_row("utilization", _pct(eff.get("utilization"), 2),
              "share of bankroll-hours at risk (growth ≈ RoRC × util)")
    return t


# ---------- experiments ----------

def resting_lines(maker: dict) -> list[str]:
    """Maker step 0's ledger: what the slice reached, what rested, what
    landed in it.

    The fill row is labelled experiment-grade on purpose. A post-only order
    id never reaches the wallet feed, so a "fill" here is any BUY on the same
    slug and side at or below a price we were resting at — a taker clip that
    crossed cheap satisfies that too. `placed` is the hard number beside it:
    post-only acks the engine really did submit.
    """
    lines = []
    if maker.get("candidates"):
        lines.append(f"[dim]candidates[/dim] {maker['candidates']:,} ticks "
                     f"[dim]across {maker['candidate_windows']:,} windows "
                     f"(knob off — the class, unpriced)[/dim]")
    if maker.get("rested"):
        lines.append(f"[dim]rested[/dim] {maker['rested']:,} ticks "
                     f"[dim]across {maker['rested_windows']:,} windows[/dim]"
                     f"   [dim]placed[/dim] {maker.get('placed', 0):,} "
                     f"[dim]post-only acks[/dim]")
    if maker.get("fills"):
        w, ln = maker.get("wins", 0), maker.get("losses", 0)
        n_win = maker["fill_windows"]
        lines.append(f"[dim]fills[/dim] {maker['fills']:,} "
                     f"[dim]${maker['fill_usd']:,.0f} in {n_win} "
                     f"window{'' if n_win == 1 else 's'}[/dim]   "
                     f"[bold]{w}W-{ln}L[/bold]   "
                     f"[dim]P&L[/dim] {_money(maker.get('pnl'))}")
        lines.append("[yellow]experiment-grade attribution[/yellow] [dim]— a "
                     "post-only fill is inferred from price/side, not proven[/dim]")
    return lines


def chase_lines(chase: dict) -> list[str]:
    """The order path and the pay-up buffer, two lines.

    Chase is reported as `used of allowed` because that is the decision it
    informs: a buffer nothing spends is a knob to turn down, and one pinned
    at its max is a knob starving fills.
    """
    lines = []
    if chase.get("acks") or chase.get("suppressed"):
        share = chase.get("suppressed_share")
        line = (f"[dim]orders[/dim] {chase['acks']:,} acked  "
                f"[dim]·[/dim]  [dim]suppressed[/dim] {chase['suppressed']:,} "
                f"[dim]({_pct(share, 1)})[/dim]  [dim]·[/dim]  "
                f"[dim]ack[/dim] p50 {_ms(chase.get('ack_p50'))} "
                f"p90 {_ms(chase.get('ack_p90'))}")
        # Build+sign is sub-millisecond on a warm tick cache; printing "0ms"
        # implies a stage that measured zero rather than one that isn't a
        # stage. It earns a cell only once it costs something.
        sign = chase.get("sign_p50")
        if sign is not None and sign >= 1.0:
            line += f"  [dim]·[/dim]  [dim]sign[/dim] p50 {_ms(sign)}"
        elif sign is not None:
            line += "  [dim]·[/dim]  [dim]sign <1ms[/dim]"
        lines.append(line)
    if chase.get("chase_n"):
        med, mx = chase.get("buffer_med_c"), chase.get("buffer_max_c")
        lines.append(f"[dim]pay-up[/dim] {chase['chased']:,} of "
                     f"{chase['chase_n']:,} priced fires chased  [dim]·[/dim]  "
                     f"[dim]buffer[/dim] median {med:.2f}c "
                     f"[dim]· max {mx:.2f}c above the ask[/dim]")
    return lines


# ---------- demoted to --full ----------

def calibration_table(cal: dict) -> Table:
    """Did a clip fired at a stated fair actually hit that often?

    Demoted behind --full: analysis/r6_report.txt answers the same question
    per symbol AND duration at four thresholds over thousands of samples,
    and this table's hit attribution is partly circular — a window with no
    redeem row and no gamma read takes its winning side FROM our fired side,
    so those buckets can only ever agree with the win rate.
    """
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


# Sized to hold "paying for itself" whole at 100 columns — the verdict is the
# column an operator reads first, and an ellipsized one says nothing. The two
# money headers are abbreviated to pay for it; gates_footer spells them out.
_GATES_COLUMNS = (
    ("refusal", "left", 12),
    ("episodes", "right", 8),
    ("priced", "right", 6),
    ("hit %", "right", 5),
    ("missed W", "right", 9),
    ("avoided L", "right", 9),
    ("net", "right", 10),
    ("verdict", "left", 18),
)


def gates_table(report: dict, category_order: list[str], verdict) -> Table:
    """`--gates`: what our own refusals cost or saved, per reason.

    Sign convention is the shadow ledger's, not the P&L's: `net` is the
    counterfactual money the refusals turned down, so POSITIVE is a gate
    that cost us (over-tight) and NEGATIVE is a gate paying for itself.
    That inversion is why the colors are flipped here relative to every
    other money cell on the report.
    """
    t = Table(box=None, pad_edge=False, padding=(0, 1))
    for col, justify, width in _GATES_COLUMNS:
        t.add_column(col, justify=justify, width=width, no_wrap=True,
                     overflow="ellipsis")
    cats = report.get("categories") or {}
    for cat in category_order:
        s = cats.get(cat)
        if not s or not s["episodes"]:
            continue
        hr = _pct(s["hit_rate"]) if s["hit_rate"] is not None else "[dim]—[/dim]"
        net = s["net"]
        t.add_row(cat, f"{s['episodes']:,}", f"{s['priced']:,}", hr,
                  f"{s['missed_wins']:,.0f}", f"{s['avoided_losses']:,.0f}",
                  f"[{_pnl_color(-net)}]{net:+,.0f}[/]", verdict(s))
    return t


def gates_footer(report: dict) -> list[str]:
    totals, cov = report["totals"], report["coverage"]
    net = totals["net"]
    return [
        f"[bold]total[/bold]  [dim]missed wins[/dim] {totals['missed_wins']:,.0f}"
        f"  [dim]·[/dim]  [dim]avoided losses[/dim] {totals['avoided_losses']:,.0f}"
        f"  [dim]·[/dim]  [dim]net[/dim] [bold {_pnl_color(-net)}]{net:+,.0f}[/]",
        f"[dim]{cov['windows']:,} windows · {cov['unpriced_episodes']:,} unpriced "
        f"(no recorded ask) · {cov['skipped_unresolved']:,} unresolved[/dim]",
    ]


# ---------- the whole report ----------

def render_stats(sb: dict, eff: dict, bal: dict | None, status: dict | None,
                 floor: float, *, blocks: dict | None = None,
                 full: bool = False, gates: dict | None = None,
                 era_rows: list[dict] | None = None, era_now: dict | None = None,
                 scope_label: str | None = None, eras_omitted: bool = False) -> Group:
    """The single renderable `pmt crypto stats` prints.

    `blocks` carries the tape folds (polymarket.updown_stats): arm flags,
    the maker experiment, the order path, the fleet ration. Sections with no
    data drop out entirely rather than printing an empty box — an experiment
    block with nothing in it is a claim the report has no business making.

    The by-era table is the ONE exception to that rule: it prints every era
    the registry knows, empty ones included, because the whole point of it is
    that no regime can go missing. It drops out only when `--since` floored
    the wallet walk itself, and then it says so rather than showing a partial
    set of eras — half an era table is worse than none.
    """
    status = status or {}
    blocks = blocks or {}
    marked = (era_now or {}).get("name")
    parts: list = [header_panel(sb, eff, bal, status, floor, blocks.get("fleet"),
                                era_now=era_now, scope_label=scope_label), ""]

    if era_rows:
        parts += [section("by era", "policy regimes, pinned to deploy moments"),
                  era_table(era_rows, marked)]
        parts += era_footnote(era_rows, marked) + [""]
    elif eras_omitted:
        parts += [section("by era", "omitted"),
                  "[dim]--since floors the wallet walk, so every older era would read "
                  "short.[/dim]",
                  "[dim]Drop --since for the full table, or --era <name> to scope the "
                  "report and keep it.[/dim]", ""]

    series = sb.get("series") or {}
    flags = blocks.get("flags") or {}
    if series:
        note = "wallet-graded"
        if any(f.get("feed") == "rtds" or f.get("maker_bid") for f in flags.values()):
            note += f" · {_RTDS_MARK} stream-fed · {_MAKER_MARK} maker bid"
        parts += [section("by symbol", note),
                  symbol_table(series, flags, eff.get("breakeven_win_rate")), ""]

    if eff.get("n"):
        parts += [section("effectiveness", "the win rate, corrected for size and time"),
                  effectiveness_table(eff), ""]

    resting = resting_lines(blocks.get("maker") or {})
    if resting:
        parts += [section("resting bids", "maker step 0 — no-ask windows")]
        parts += resting + [""]

    chase = chase_lines(blocks.get("chase") or {})
    if chase:
        parts += [section("order path", "pay-up and the wire")]
        parts += chase + [""]

    if gates:
        from polymarket import shadow

        parts += [section("gates", "what our refusals cost, hindsight-priced"),
                  gates_table(gates, shadow.CATEGORY_ORDER, shadow.verdict)]
        parts += gates_footer(gates) + [""]

    if not full:
        return Group(*parts)

    cal = sb.get("cal") or {}
    if cal:
        parts += [section("calibration", "clips fired at stated fair vs realized"),
                  calibration_table(cal), ""]

    arms = status.get("arms") or {}
    if arms:
        # watch's own arms table, not a second one: a static snapshot that
        # drifts from the live dashboard is worse than no snapshot.
        import time as _t

        from watch_ui import build_arms_table

        parts += [section("live arms", "a snapshot — `pmt crypto watch` for live"),
                  build_arms_table(arms, _t.time())]
        if status.get("pending_rolls"):
            parts.append(f"[dim]pending rolls: {', '.join(status['pending_rolls'])}[/dim]")
    return Group(*parts)
