"""Line-chart panels for `pmt crypto watch` — pure math, pure rendering.

Two questions the tables could never answer, and both of them are SHAPES
rather than numbers: is the money curve going up, and where is the underlying
sitting relative to the strike we are betting into.

Nothing here opens a file, a socket or a clock it was not handed. watch_feeds
does the tailing and cli_crypto_watch's fetch worker does the walking — same
split as watch_ui.py and for the same reason: the render loop is allowed ZERO
I/O it can be blocked on (docs/LESSONS.md#L28). Hand these functions a dict,
read the strings back; that is also why every one of them is unit-testable
without a terminal.

The scoreboard is the ONLY source of a P&L figure here. Nothing on these
panels is priced off the tape's stated fairs or off a live mark —
`eff_windows` is what cli_crypto_stats graded against the wallet and the
outcomes corpus, and a chart drawn off anything else would disagree with the
header two rows above it.
"""

from __future__ import annotations

import time

import watch_ui
from cli_common import _pnl_color
from polymarket import updown_slugs

# ---------- braille canvas ----------
#
# 2 dot-columns x 4 dot-rows per character cell, so a one-row sparkline still
# has four vertical levels and a three-row chart has twelve. Block glyphs
# (▁▂▃▄▅▆▇█) were the alternative and lose the connection between adjacent
# samples, which is the whole difference between a line and a bar chart.

_BRAILLE_BASE = 0x2800
# Dot bit per (dot column, dot row). The bottom row is out of numeric order
# because dots 7/8 were bolted onto the original 6-dot code.
_DOTS = ((0x01, 0x02, 0x04, 0x40),
         (0x08, 0x10, 0x20, 0x80))

# A cell no sample reached. Not a space: the run of glyphs has to stay a run,
# or a gap in a corpus reads as the end of the chart.
BRAILLE_BLANK = chr(_BRAILLE_BASE)


def series_bounds(cols: list[float | None],
                  lo: float | None = None,
                  hi: float | None = None) -> tuple[float, float]:
    """(lo, hi) for a column series, padded so a flat line lands mid-cell.

    A degenerate range is the normal case here, not a corner: a quiet minute
    of chainlink prints is one price repeated, and dividing by a zero span
    would put every dot on one rail and read as a spike.
    """
    vals = [v for v in cols if v is not None]
    if not vals:
        return (0.0, 1.0)
    v_lo = min(vals) if lo is None else lo
    v_hi = max(vals) if hi is None else hi
    if v_hi - v_lo < 1e-12:
        pad = abs(v_hi) * 1e-6 or 1.0
        return (v_lo - pad, v_hi + pad)
    return (v_lo, v_hi)


def braille_rows(cols: list[float | None], width: int, height: int = 1,
                 lo: float | None = None, hi: float | None = None) -> list[str]:
    """`height` strings of `width` braille cells plotting `cols` as a LINE.

    `cols` carries one value (or None for "no sample here") per DOT column, so
    a chart `width` cells wide wants 2*width of them — which is exactly what
    downsample() produces. Adjacent samples are joined vertically: at four dot
    rows a scatter of unconnected dots reads as noise, and the join is what
    makes a trend legible in a single terminal row.

    Values outside [lo, hi] are CLAMPED to the rail rather than dropped. A
    chart that silently loses its outlier is worse than one whose outlier sits
    on the edge, and the numeric field beside every chart carries the exact
    figure anyway.
    """
    width = max(int(width), 1)
    height = max(int(height), 1)
    rows = 4 * height
    lo, hi = series_bounds(cols, lo, hi)
    span = hi - lo

    def row_of(v: float) -> int:
        return max(0, min(rows - 1, int(round((hi - v) / span * (rows - 1)))))

    grid = [[0] * width for _ in range(height)]
    prev: int | None = None
    for i in range(2 * width):
        v = cols[i] if i < len(cols) else None
        if v is None:
            prev = None
            continue
        r = row_of(float(v))
        # Fill from the previous sample's row to this one so a step renders as
        # a stroke, not as two orphaned dots.
        lo_r, hi_r = (r, r) if prev is None else (min(prev, r), max(prev, r))
        for rr in range(lo_r, hi_r + 1):
            grid[rr // 4][i // 2] |= _DOTS[i % 2][rr % 4]
        prev = r
    return ["".join(chr(_BRAILLE_BASE + b) for b in row) for row in grid]


def sparkline(cols: list[float | None], width: int,
              lo: float | None = None, hi: float | None = None) -> str:
    """One braille row — the compact form every per-symbol line uses."""
    return braille_rows(cols, width, 1, lo, hi)[0]


# ---------- turning samples into columns ----------

def downsample(points: list[tuple[float, float]], n: int,
               floor: float, ceiling: float) -> list[float | None]:
    """`n` column values from (t, v) samples on the half-open [floor, ceiling)
    time axis: the LAST sample landing in each column, carried forward.

    Carried forward rather than interpolated. A cumulative P&L curve genuinely
    IS a step function between settlements, and a gap in a recorder corpus is a
    gap — sloping across one would draw prices that never printed. Columns
    before the first sample stay None and are simply not plotted, which is how
    a chart says "no history here yet" instead of implying a flat zero.
    """
    n = max(int(n), 1)
    out: list[float | None] = [None] * n
    span = ceiling - floor
    if span <= 0:
        return out
    for t, v in points:
        if t < floor or t >= ceiling:
            continue
        i = min(n - 1, max(0, int((t - floor) / span * n)))
        out[i] = float(v)
    carry: float | None = None
    for i in range(n):
        if out[i] is None:
            out[i] = carry
        else:
            carry = out[i]
    return out


# ---------- P&L: cumulative, wallet-graded, per engine ----------

def settle_ts(w: dict) -> float:
    """When a graded window's money actually landed: its redeem/exit row, else
    the window's own close. The exit row is preferred because that is the
    instant the capital came back, and it is the field `eff_windows` records."""
    return float(w.get("exit_ts") or 0.0) or float(w.get("end_ts") or 0.0)


def cumulative_pnl(windows: list[dict] | None, floor: float = 0.0
                   ) -> list[tuple[float, float]]:
    """[(settlement instant, running $)] over graded windows, oldest first.

    Riding windows (`pnl` None) contribute NOTHING — an undecided position is
    not P&L, and drawing a mark-to-market line here would put a number on this
    panel the wallet has never agreed to.

    `floor` re-bases the curve at zero: a trailing-24h chart answers "what did
    today do", not "where does the all-time ledger stand", and starting it at
    the all-time level would flatten the day into a rounding error.
    """
    graded = [(settle_ts(w), float(w.get("pnl") or 0.0))
              for w in (windows or [])
              if w.get("pnl") is not None and settle_ts(w) >= floor]
    graded.sort(key=lambda p: p[0])
    out: list[tuple[float, float]] = []
    run = 0.0
    for t, pnl in graded:
        run += pnl
        out.append((t, run))
    return out


def pnl_series(windows: list[dict] | None, width: int, now: float,
               window_s: float) -> tuple[list[float | None], float]:
    """(column values, closing P&L) for one engine's trailing window.

    The closing figure is returned rather than re-read off the last column:
    the columns are a picture and the number is the answer, and a settlement
    landing inside the final column must not round the headline figure away.
    """
    floor = now - window_s
    curve = cumulative_pnl(windows, floor)
    # The curve opens at zero AT the floor so the line starts on the axis
    # rather than jumping in wherever the first settlement happened to land.
    cols = downsample([(floor, 0.0)] + curve, 2 * max(int(width), 1), floor, now)
    return cols, (curve[-1][1] if curve else 0.0)


# ---------- feeds: the underlying against the window's own strike ----------

def delta_bp(px: float | None, target: float | None) -> float | None:
    """How far the underlying sits from the window's settlement reference, in
    basis points — the same quantity the engine's basis guard gates on, so the
    chart and the `gated  margin -4.9 vs 6.0bp` cell share one axis."""
    if px is None or not target:
        return None
    return (float(px) / float(target) - 1.0) * 1e4


def target_of(marks: dict | None, start: float) -> float | None:
    """The window's up/down strike: the settlement TWAP printed AT its open.

    Keyed exactly as polymarket.rtds_read.twap_marks keys it (the print at
    wall time `m+60` averages minute `m`), so `marks[start - 60]` IS the
    reference `polymarket.crypto._model_twap` prices the window off. One
    keying convention, or the chart's zero line is not the market's.
    """
    if not marks:
        return None
    v = marks.get(float(start) - 60.0)
    if isinstance(v, bool) or not isinstance(v, (int, float)) or v <= 0:
        return None
    return float(v)


def deviation_cols(points: list[tuple[float, float]], width: int,
                   floor: float, ceiling: float) -> list[float | None]:
    """A price path as deviation from its OWN mean over the window.

    The rtds reference and a spot venue carry a standing basis between them
    that is routinely wider than half a minute's price range, so plotting both
    on one absolute scale pins each to an opposite rail and the shapes stop
    being comparable. Centring each on itself is what makes the ~3s lead
    visible as a horizontal offset rather than as two flat lines at different
    heights.
    """
    cols = downsample(points, 2 * max(int(width), 1), floor, ceiling)
    vals = [v for v in cols if v is not None]
    if not vals:
        return cols
    mean = sum(vals) / len(vals)
    return [None if v is None else v - mean for v in cols]


def shared_span(*col_sets: list[float | None]) -> tuple[float, float]:
    """One symmetric (lo, hi) covering every series — the comparison only
    means anything while both are drawn at the same scale."""
    lim = 0.0
    for cols in col_sets:
        for v in cols:
            if v is not None:
                lim = max(lim, abs(v))
    lim = lim or 1.0
    return (-lim, lim)


def span_with_zero(cols: list[float | None], floor_span: float = 2.0
                   ) -> tuple[float, float]:
    """(lo, hi) that always CONTAINS zero — the strike is always on the chart.

    Not centred on zero: a window sitting 20-40bp above its strike is entirely
    on one side, and forcing the axis symmetric would squash the whole path
    onto the top rail and throw the shape away. Including zero instead keeps
    both facts — how far from the strike, and what the path did to get there —
    with the strike as the rail the line is measured off.

    `floor_span` stops a window that has barely moved from being drawn as a
    full-scale swing: a ±0.2bp wobble rendered rail to rail reads as a market
    running away from us.
    """
    vals = [v for v in cols if v is not None]
    lo, hi = min(vals + [0.0]), max(vals + [0.0])
    if hi - lo < floor_span:
        pad = (floor_span - (hi - lo)) / 2
        lo, hi = lo - pad, hi + pad
    return (lo, hi)


# ---------- panel geometry ----------

PNL_WINDOW_S = 24 * 3600.0    # the trailing P&L chart's axis
FEED_WINDOW_S = 120.0         # per-symbol path against the strike
FOCUS_WINDOW_S = 30.0         # the rtds-vs-spot lead block: seconds, not minutes
CHARTS_MAX_INNER = 6          # inner rows either panel may claim
CHARTS_CHROME = 2             # panel border, top and bottom
_PNL_LABEL_W = 8
_PNL_VALUE_W = 10
_FEED_IDENT_W = 10            # "hype 15m ▲" — the side rides the identity cell
_FEED_TAIL_W = 16             # "  -4.7bp   2:41"
_MIN_CHART_W = 8              # below this a line is a smudge; the row drops it


def _chart_width(panel_w: int, used: int) -> int:
    """Cells left for the line after the row's fixed fields, or 0 when the
    panel is too narrow to draw one worth looking at."""
    inner = (panel_w or 0) - watch_ui.PANEL_CHROME_W
    return inner - used if (inner - used) >= _MIN_CHART_W else 0


def _px(v: float) -> str:
    """A price at the precision its own magnitude needs — doge and btc share
    this column and 0.09 must not print as 0.09 beside 77,700."""
    a = abs(v)
    if a >= 100:
        return f"{v:,.2f}"
    return f"{v:,.4f}" if a >= 1 else f"{v:,.6f}"


# ---------- P&L panel ----------

def engine_curves(snap: dict, now: float,
                  window_s: float = PNL_WINDOW_S) -> list[dict]:
    """One entry per engine whose ledger this box can actually see.

    The local engine's rows come straight off the scoreboard the header is
    already painting — the SAME `_tape_scoreboard` walk, no second acquisition
    (cli_crypto_stats' single-source rule). A peer comes off the worker's
    `peers` publication, which runs that same function against another wallet.

    An engine with no graded window in the trailing span still gets its line,
    flat at zero. An engine we cannot see gets no row at all: "this box made
    nothing today" and "we cannot reach this box's ledger" are opposite facts
    and may not share a rendering.
    """
    out: list[dict] = []
    sb = snap.get("sb") or {}
    if snap.get("sb_fetched_at") is not None:
        out.append({"label": (snap.get("node") or "local"),
                    "windows": sb.get("eff_windows") or []})
    for label, book in sorted((snap.get("peers") or {}).items()):
        if not isinstance(book, dict) or book.get("windows") is None:
            continue
        out.append({"label": label, "windows": book.get("windows") or []})
    if len(out) > 1:
        # The fleet line SUMS ledgers rather than averaging them: two engines
        # under the series partition trade disjoint series, so their windows
        # concatenate with no way to double-count one.
        out.append({"label": "fleet",
                    "windows": [w for e in out for w in e["windows"]]})
    return out


def pnl_rows(snap: dict, panel_w: int, now: float | None = None,
             window_s: float = PNL_WINDOW_S) -> list[str]:
    """`desktop  +12.34  ⣀⣠⣴⣶⣿` per engine, newest money on the right.

    Rich markup, one string per terminal row. Empty when no engine's ledger
    has landed yet — the panel says so rather than painting a confident zero.
    """
    now = time.time() if now is None else now
    chart_w = _chart_width(panel_w, _PNL_LABEL_W + _PNL_VALUE_W + 2)
    rows: list[str] = []
    for e in engine_curves(snap, now, window_s):
        cols, net = pnl_series(e["windows"], chart_w or 1, now, window_s)
        style = _pnl_color(net)
        label = e["label"][:_PNL_LABEL_W]
        money = f"{net:+,.2f}"
        cell = f"[{style}]{sparkline(cols, chart_w)}[/{style}]" if chart_w else ""
        rows.append(f"[dim]{label:<{_PNL_LABEL_W}}[/dim] "
                    f"[{style}]{money:>{_PNL_VALUE_W}}[/{style}] {cell}")
    return rows


# ---------- feeds panel ----------

def armed_windows(snap: dict) -> list[dict]:
    """The live armed windows, soonest to settle first, each carrying whatever
    the wallet already knows about our position in it.

    Same two sources the windows table merges (watch_ui): the engine's /status
    is the only place an arm with no fill yet exists, and the scoreboard is the
    only place a side and an entry price do. Sorted by close because this panel
    is about what is about to settle, not about what was armed first.
    """
    sb = snap.get("sb") or {}
    by_slug: dict[str, dict] = {}
    for src in (sb.get("riding_windows"), sb.get("windows")):
        for r in src or []:
            by_slug.setdefault(r.get("slug") or "", r)
    out = []
    for row in watch_ui.live_rows((snap.get("status") or {}).get("arms")):
        pos = by_slug.get(row["slug"]) or {}
        out.append({**row, "side": pos.get("side"), "entry_px": pos.get("entry_px"),
                    "entry_ts": pos.get("entry_ts")})
    out.sort(key=lambda r: float(r.get("end_ts") or 0.0))
    return out


def _side_style(side: str | None, d_bp: float | None) -> str:
    """Green while the underlying sits on the side we bought, red while it does
    not. Dim when we hold nothing — an unfilled arm has no stake in the sign,
    and colouring it would read as a position."""
    if not side or d_bp is None:
        return "dim"
    return "green" if (d_bp >= 0 if side == "up" else d_bp <= 0) else "red"


def _feed_ident(w: dict, side: str | None) -> str:
    """`btc 15m ▲` — the row's identity plus the side we are actually on."""
    arrow = {"up": "▲", "down": "▼"}.get((side or "").lower(), "")
    return f"{watch_ui._arm_label(w.get('slug', ''))} {arrow}".rstrip()


def feed_row(w: dict, feeds: dict, panel_w: int, now: float) -> str | None:
    """One armed window: its underlying's recent path against the strike.

    The chart's axis always contains the strike (span_with_zero), so the line's
    distance from that rail IS the margin the window settles on and its shape
    is how the margin got there — the number beside it is the same figure to a
    decimal. The side we took rides the identity glyph and the colour: green
    while the underlying is on the side we bought, red while it is not.

    None when this box's rtds corpus has nothing for the symbol: a price chart
    with no prices in it is a claim about the feed's health, and the header's
    own feed row is where that belongs.
    """
    slug = w.get("slug") or ""
    parsed = updown_slugs.parse_updown_slug(slug)
    if parsed is None:
        return None
    samples = (feeds.get("chain") or {}).get(parsed["symbol"]) or []
    if not samples:
        return None
    target = target_of((feeds.get("marks") or {}).get(parsed["symbol"]),
                       parsed["start"])
    floor = now - FEED_WINDOW_S
    side = (w.get("side") or "").lower() or None
    chart_w = _chart_width(panel_w, _FEED_IDENT_W + _FEED_TAIL_W + 3)
    if target:
        cols = downsample([(t, delta_bp(px, target)) for t, px in samples],
                          2 * max(chart_w, 1), floor, now)
        lo, hi = span_with_zero(cols)
    else:
        cols = downsample(samples, 2 * max(chart_w, 1), floor, now)
        lo = hi = None
    d_bp = delta_bp(samples[-1][1], target)
    style = _side_style(side, d_bp)
    delta = f"{d_bp:+.1f}bp" if d_bp is not None else "tgt —"
    line = f"[{style}]{sparkline(cols, chart_w, lo, hi)}[/{style}]" if chart_w else ""
    ident = _feed_ident(w, side)
    return (f"{watch_ui._stage_cell(w)} [dim]{ident:<{_FEED_IDENT_W}}[/dim] {line} "
            f"[{style}]{delta:>9}[/{style}] {watch_ui._countdown_markup(slug, now)}")


def focus_rows(w: dict | None, feeds: dict, panel_w: int, now: float) -> list[str]:
    """The settlement stream against a spot venue, one window, one axis.

    This is the ~3s maker lead made visible rather than asserted: the two lines
    are the same 30 seconds at the same scale, each centred on its own mean
    (see deviation_cols), so a move reaching the spot line several dot columns
    before it reaches the chainlink line IS the lead we are paying. Dropped
    whole when either side is missing — half a comparison is not a comparison.
    """
    if w is None:
        return []
    parsed = updown_slugs.parse_updown_slug(w.get("slug") or "")
    if parsed is None:
        return []
    sym = parsed["symbol"]
    chain = (feeds.get("chain") or {}).get(sym) or []
    spot = (feeds.get("spot") or {}).get(sym) or []
    if not chain or not spot:
        return []
    chart_w = _chart_width(panel_w, _FEED_IDENT_W + _FEED_TAIL_W + 3)
    if not chart_w:
        return []
    floor = now - FOCUS_WINDOW_S
    c_cols = deviation_cols(chain, chart_w, floor, now)
    s_cols = deviation_cols(spot, chart_w, floor, now)
    lo, hi = shared_span(c_cols, s_cols)
    venue = (feeds.get("venue") or {}).get(sym) or "spot"
    return [_focus_row("rtds", c_cols, chart_w, lo, hi, chain[-1][1],
                       "cyan", "settles"),
            _focus_row(venue[:7], s_cols, chart_w, lo, hi, spot[-1][1],
                       "magenta", "leads")]


def _focus_row(label: str, cols: list[float | None], chart_w: int,
               lo: float, hi: float, last: float, style: str, note: str) -> str:
    tail = f"{_px(last)} {note}"
    return (f"[dim]{'  ' + label:<{_FEED_IDENT_W}}[/dim] "
            f"[{style}]{sparkline(cols, chart_w, lo, hi)}[/{style}] "
            f"[dim]{tail:>{_FEED_TAIL_W}}[/dim]")


def feeds_rows(snap: dict, panel_w: int, now: float | None = None) -> list[str]:
    """The feeds panel's body: one line per armed window, then the lead block.

    The lead block is budgeted FIRST and drawn whole or not at all — a
    comparison missing its second line is not a comparison, and truncating the
    panel from the bottom is exactly how that used to happen. Whatever rows
    remain go to armed windows, soonest to settle first, and any window that
    does not get one is counted in a "+N more armed" note: a cap the operator
    cannot see is indistinguishable from an arm that has gone missing.

    Degrades a row at a time. No arms, no rtds corpus, no spot recorder, an
    armed symbol the stream does not carry — each removes what it removes and
    the rest of the panel still paints.
    """
    now = time.time() if now is None else now
    feeds = snap.get("feeds") or {}
    windows = armed_windows(snap)
    built = [(w, line) for w in windows
             if (line := feed_row(w, feeds, panel_w, now)) is not None]
    # The focus is the window closest to settling that has both feeds: the one
    # where three seconds of lead is still worth something.
    focus = focus_rows(built[0][0] if built else None, feeds, panel_w, now)
    cap = max(CHARTS_MAX_INNER - len(focus), 0)
    if len(windows) > cap:
        cap = max(cap - 1, 0)  # one row is owed to the "+N more" note
    rows = [line for _, line in built[:cap]]
    extra = len(windows) - len(rows)
    if rows and extra > 0:
        rows.append(f"[dim]{'':<{_FEED_IDENT_W}} +{extra} more armed[/dim]")
    return (rows + focus)[:CHARTS_MAX_INNER]


# ---------- the row of panels ----------

def split_widths(width: int) -> tuple[int, int]:
    """(P&L width, feeds width) — Rich's own even split, computed here so the
    row builders can size their charts before the Layout exists."""
    width = max(int(width or 0), 0)
    left = width // 2
    return left, width - left


def charts_inner_height(snap: dict, width: int, now: float | None = None) -> int:
    """Inner rows the charts row wants, or 0 when it has nothing to say.

    Zero is the whole degradation contract: a box with no peer wallet, no armed
    symbol and no corpus paints no charts row at all rather than an empty box
    taking rows off the tape.
    """
    now = time.time() if now is None else now
    left, right = split_widths(width)
    return min(max(len(pnl_rows(snap, left, now)),
                   len(feeds_rows(snap, right, now))), CHARTS_MAX_INNER)


def _panel(rows: list[str], empty: str, title: str):
    from rich.panel import Panel
    from rich.text import Text

    body = Text.from_markup("\n".join(rows) if rows else empty)
    # One chart, one row. A wrapped line silently costs the panel a second row
    # and the layout's height arithmetic stops holding — same rule as the tape.
    body.no_wrap, body.overflow = True, "ellipsis"
    return Panel(body, title=title, title_align="left", border_style="dim")


def build_pnl_panel(snap: dict, width: int, now: float | None = None,
                    window_s: float = PNL_WINDOW_S):
    """Cumulative wallet-graded P&L per engine over the trailing window."""
    span = f"{window_s / 3600:.0f}h" if window_s >= 3600 else f"{window_s / 60:.0f}m"
    return _panel(pnl_rows(snap, width, now, window_s),
                  "[dim]no graded window in the trailing span[/dim]",
                  f"[bold]P&L[/bold] [dim]· {span} · wallet-graded[/dim]")


def build_feeds_panel(snap: dict, width: int, now: float | None = None):
    """The underlying against each armed window's strike, plus the lead block."""
    return _panel(feeds_rows(snap, width, now),
                  "[dim]no armed symbol this box's corpus can price[/dim]",
                  "[bold]feeds[/bold] [dim]· price vs strike[/dim]")
