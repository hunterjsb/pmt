"""The feeds chart panel for `pmt crypto watch` — pure math, pure rendering.

One question the tables could never answer, because it is a SHAPE rather than
a number: where is the underlying sitting relative to the strike we are
betting into, and how did it get there. The whole charts row belongs to it —
a cumulative P&L sparkline used to share this space and earned nothing (a
day's curve at braille resolution is a flat line with one cliff in it; the
header's money cells already say everything it said), so the fleet's
cross-engine nets live on a header row now (watch_ui.fleet_row) and every
terminal cell here goes to price.

Nothing here opens a file, a socket or a clock it was not handed. watch_feeds
does the tailing and cli_crypto_watch's fetch worker does the walking — same
split as watch_ui.py and for the same reason: the render loop is allowed ZERO
I/O it can be blocked on (docs/LESSONS.md#L28). Hand these functions a dict,
read the strings back; that is also why every one of them is unit-testable
without a terminal.
"""

from __future__ import annotations

import time

import watch_ui
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


# ---------- the echo correlator ----------
#
# Three clocks price one underlying: the exchange spot tape, the chainlink
# stream the window settles on, and the market's own odds. The vault's
# opponent model says the first leads the second by ~3s and the makers price
# off the first — so the odds should carry a time-shifted ECHO of the spot
# tape, and the lag that maximises delta-correlation is that echo measured.

def resample_1s(points: list[tuple[float, float]], floor: float,
                ceiling: float) -> list[float | None]:
    """Last sample per 1-second bin, NOT carried forward — the correlator
    works in deltas, and a carried sample would manufacture zero-deltas that
    dilute r toward nothing exactly when a feed goes quiet."""
    n = max(int(ceiling - floor), 1)
    out: list[float | None] = [None] * n
    for t, v in points:
        if floor <= t < ceiling:
            out[int(t - floor)] = float(v)
    return out


def _deltas(grid: list[float | None]) -> list[float | None]:
    prev = None
    out: list[float | None] = []
    for v in grid:
        out.append(v - prev if (v is not None and prev is not None) else None)
        if v is not None:
            prev = v
    return out


ECHO_WINDOW_S = 180.0    # the correlator's axis
ECHO_MAX_LAG_S = 8       # |lag| searched, seconds — the echo we hunt is ~1-4s
ECHO_MIN_N = 30          # overlapping delta pairs below this say nothing


def echo_lag(a: list[tuple[float, float]], b: list[tuple[float, float]],
             floor: float, ceiling: float
             ) -> tuple[int, float, int] | None:
    """(lag_s, r, n) at the |r|-maximising shift, or None below ECHO_MIN_N.

    Positive lag means A LEADS B: A's move at t correlates with B's move at
    t+lag. Pearson on 1s DELTAS, not levels — two random walks correlate on
    levels by construction, and the standing basis between venues cancels in
    differences (the same reason the spot-pred spec is delta-form only).
    """
    da = _deltas(resample_1s(a, floor, ceiling))
    db = _deltas(resample_1s(b, floor, ceiling))
    best: tuple[int, float, int] | None = None
    for lag in range(-ECHO_MAX_LAG_S, ECHO_MAX_LAG_S + 1):
        xs, ys = [], []
        for i, x in enumerate(da):
            j = i + lag
            if x is None or not 0 <= j < len(db) or db[j] is None:
                continue
            xs.append(x)
            ys.append(db[j])
        n = len(xs)
        if n < ECHO_MIN_N:
            continue
        mx, my = sum(xs) / n, sum(ys) / n
        sxx = sum((x - mx) ** 2 for x in xs)
        syy = sum((y - my) ** 2 for y in ys)
        if sxx <= 0 or syy <= 0:
            continue
        r = sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / (sxx * syy) ** 0.5
        if best is None or abs(r) > abs(best[1]):
            best = (lag, r, n)
    return best


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

FEED_WINDOW_S = 120.0         # every sparkline's axis
FOCUS_WINDOW_S = 30.0         # the rtds/spot/odds overlay: seconds, not minutes
CHARTS_MAX_INNER = 6          # inner rows, compact: sparks + the stats rows
CHARTS_MAX_INNER_TALL = 14    # inner rows, tall: each window gets a spark row
#                               + an intent row — sized so four 5m lanes + the
#                               "+N" note + the overlay/echo/latency block all
#                               seat (4*2 + 1 + 5 = 14)
CHARTS_CHROME = 2             # panel border, top and bottom
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


def _has_stake(w: dict) -> bool:
    """Money is in this window: the wallet knows a side, or the engine's own
    committed figure is nonzero."""
    return bool(w.get("side")) or float(w.get("notional") or 0.0) > 0.0


def _dur_of(w: dict) -> float:
    p = updown_slugs.parse_updown_slug(w.get("slug") or "")
    return float(p["end"]) - float(p["start"]) if p else float("inf")


def cared_windows(windows: list[dict]) -> list[dict]:
    """The windows worth a chart: every window with a stake, then the unfilled
    arms of the SHORTEST armed duration tier only.

    The long-duration arms are measurement canaries today — tiny, basis-guard
    gated, no money at risk — and a chart of one displaces a chart of a lane
    that fires. Duration is the discriminator rather than a series list or a
    dollar floor because it self-corrects: a 15m window that actually fills
    charts like anything else, and a fleet armed ONLY on 15m gets its charts
    back. The rest stay counted in the "+N more armed" note.
    """
    staked = [w for w in windows if _has_stake(w)]
    idle = [w for w in windows if not _has_stake(w)]
    if idle:
        min_dur = min(_dur_of(w) for w in idle)
        idle = [w for w in idle if _dur_of(w) == min_dur]
    return staked + idle


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


def feed_row(w: dict, feeds: dict, panel_w: int, now: float,
             h: int = 1) -> list[str] | None:
    """One armed window: a compact sparkline of the underlying against the
    strike, and (at h=2) an INTENT row under it — what the engine is doing
    about this window right now: gate state, its own reason string, the
    market's de-vigged P(up), and the money already committed.

    The spark's axis always contains the strike (span_with_zero), so the
    line's distance from that rail IS the margin the window settles on — the
    number beside it is the same figure to a decimal. The side we took rides
    the identity glyph and the colour: green while the underlying is on the
    side we bought, red while it is not.

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
    ident = _feed_ident(w, side)
    line = (f"[{style}]{sparkline(cols, chart_w, lo, hi)}[/{style}]"
            if chart_w else "")
    spark = (f"{watch_ui._stage_cell(w)} [dim]{ident:<{_FEED_IDENT_W}}[/dim] "
             f"{line} [{style}]{delta:>9}[/{style}] "
             f"{watch_ui._countdown_markup(slug, now)}")
    if h <= 1:
        return [spark]
    return [spark, _intent_row(w, feeds, parsed["symbol"], panel_w)]


def _intent_row(w: dict, feeds: dict, sym: str, panel_w: int) -> str:
    """`state · the engine's own reason · mkt p(up) · $committed` — the
    reason string verbatim because it already names the binding gate with its
    numbers (that is what it is for), clipped to the panel rather than
    paraphrased."""
    e = w.get("eval") or {}
    state = str(w.get("state") or e.get("state") or "armed")
    odds = (feeds.get("odds") or {}).get(sym) or []
    p_up = f"p(up) {odds[-1][1]:.2f}" if odds else "p(up) —"
    committed = float(w.get("notional") or 0.0)
    tail = f"[cyan]{p_up}[/cyan] [dim]· in ${committed:,.0f}[/dim]"
    # 24 ≈ the tail's rendered width + separators; the reason gets the rest.
    room = max((panel_w or 0) - watch_ui.PANEL_CHROME_W
               - (_FEED_IDENT_W + 3) - len(state) - 24, 8)
    reason = str(e.get("reason") or "").strip()[:room]
    pad = " " * (_FEED_IDENT_W + 3)
    return (f"{pad}[bold dim]{state}[/bold dim] [dim]{reason}[/dim] {tail}"
            if reason else f"{pad}[bold dim]{state}[/bold dim] {tail}")


def _z_cols(cols: list[float | None]) -> list[float | None]:
    """Deviation columns scaled to their own std, so series in different
    units (dollars, probability) share one axis by SHAPE — which is all the
    overlay compares."""
    vals = [v for v in cols if v is not None]
    if len(vals) < 2:
        return cols
    m = sum(vals) / len(vals)
    sd = (sum((v - m) ** 2 for v in vals) / len(vals)) ** 0.5
    if sd <= 0:
        return cols
    return [None if v is None else (v - m) / sd for v in cols]


def focus_rows(w: dict | None, feeds: dict, panel_w: int, now: float,
               h: int = 1) -> list[str]:
    """The echo triangle for the window closest to settling: rtds, the spot
    venue and the market's own odds overlaid on one 30s axis, then the
    cross-correlation and latency stats underneath.

    The overlay is the ~3s maker lead made visible; the echo row is the same
    thing MEASURED — the delta-correlation-maximising lag for each pair over
    the trailing 3 minutes, positive meaning the left series leads. The
    latency row says how stale and how dense each of the three clocks is,
    because a lag number read off a stale feed is a lie with a decimal point.

    Overlay lines paint only at h>=2 (they are shape, and shape is what the
    tall panel buys); the stats rows always try. Pairs below ECHO_MIN_N
    overlapping deltas print nothing rather than a confident r off noise.
    """
    if w is None:
        return []
    parsed = updown_slugs.parse_updown_slug(w.get("slug") or "")
    if parsed is None:
        return []
    sym = parsed["symbol"]
    chain = (feeds.get("chain") or {}).get(sym) or []
    spot = (feeds.get("spot") or {}).get(sym) or []
    odds = (feeds.get("odds") or {}).get(sym) or []
    chart_w = _chart_width(panel_w, _FEED_IDENT_W + _FEED_TAIL_W + 3)
    venue = ((feeds.get("venue") or {}).get(sym) or "spot")[:7]
    floor = now - FOCUS_WINDOW_S
    rows: list[str] = []
    if h >= 2 and chart_w and chain and spot:
        series = [("rtds", chain, "cyan", "settles"),
                  (venue, spot, "magenta", "leads")]
        if odds:
            series.append(("odds", odds, "yellow", "p(up)"))
        cols = {label: _z_cols(deviation_cols(pts, chart_w, floor, now))
                for label, pts, _s, _n in series}
        lo, hi = shared_span(*cols.values())
        for label, pts, style, note in series:
            tail = f"{_px(pts[-1][1])} {note}"
            rows.append(f"[dim]{'  ' + label:<{_FEED_IDENT_W}}[/dim] "
                        f"[{style}]{sparkline(cols[label], chart_w, lo, hi)}"
                        f"[/{style}] [dim]{tail:>{_FEED_TAIL_W}}[/dim]")
    echo = _echo_row(chain, spot, odds, venue, now)
    if echo:
        rows.append(echo)
    lat = _latency_row(chain, spot, odds, venue, now)
    if lat:
        rows.append(lat)
    return rows


def _echo_row(chain, spot, odds, venue: str, now: float) -> str | None:
    e_floor = now - ECHO_WINDOW_S
    pairs = [(venue, "rtds", spot, chain),
             (venue, "odds", spot, odds),
             ("rtds", "odds", chain, odds)]
    cells = []
    for a_name, b_name, a, b in pairs:
        if not a or not b:
            continue
        hit = echo_lag(a, b, e_floor, now)
        if hit is None:
            continue
        lag, r, _n = hit
        cells.append(f"{a_name}→{b_name} [bold]{lag:+d}s[/bold] r{r:+.2f}")
    if not cells:
        return None
    return (f"[dim]{'  echo':<{_FEED_IDENT_W}}[/dim] " +
            " [dim]·[/dim] ".join(cells) +
            f" [dim]({ECHO_WINDOW_S:.0f}s deltas)[/dim]")


def _latency_row(chain, spot, odds, venue: str, now: float) -> str | None:
    def cell(label: str, pts) -> str | None:
        if not pts:
            return None
        age = max(now - pts[-1][0], 0.0)
        rate = sum(1 for t, _v in pts if t >= now - 60.0) / 60.0
        style = "red" if age > 10.0 else "dim"
        return (f"{label} [{style}]{age:.1f}s[/{style}] "
                f"[dim]{rate:.1f}/s[/dim]")
    cells = [c for c in (cell("rtds", chain), cell(venue, spot),
                         cell("odds", odds)) if c]
    if not cells:
        return None
    return (f"[dim]{'  lat':<{_FEED_IDENT_W}}[/dim] " +
            "   ".join(cells) + " [dim]age · rate[/dim]")


def feeds_rows(snap: dict, panel_w: int, now: float | None = None,
               tall: bool = False) -> list[str]:
    """The feeds panel's body: each armed window's chart, then the lead block.

    `tall` doubles every chart's terminal rows (eight braille levels instead
    of four) and the row budget with it — the panel owns the whole charts row,
    so the trade is windows-shown vs vertical resolution, and the caller picks
    by what the screen affords.

    The lead block is budgeted FIRST and drawn whole or not at all — a
    comparison missing its second line is not a comparison, and truncating the
    panel from the bottom is exactly how that used to happen. Whatever rows
    remain go to the cared windows (cared_windows: stakes first, then the
    shortest-duration idle arms, soonest to settle first within each), and
    every armed window that does not get a chart — cared or not — is counted
    in a "+N more armed" note: a cap the operator cannot see is
    indistinguishable from an arm that has gone missing.

    Degrades a row at a time. No arms, no rtds corpus, no spot recorder, an
    armed symbol the stream does not carry — each removes what it removes and
    the rest of the panel still paints.
    """
    now = time.time() if now is None else now
    h = 2 if tall else 1
    cap = CHARTS_MAX_INNER_TALL if tall else CHARTS_MAX_INNER
    feeds = snap.get("feeds") or {}
    windows = armed_windows(snap)
    cared = cared_windows(windows)
    built = [(w, lines) for w in cared
             if (lines := feed_row(w, feeds, panel_w, now, h)) is not None]
    # The focus is the window closest to settling that has both feeds: the one
    # where three seconds of lead is still worth something.
    focus = focus_rows(built[0][0] if built else None, feeds, panel_w, now, h)
    budget = max(cap - len(focus), 0)
    n_fit = budget // h
    if len(windows) > n_fit:
        n_fit = max((budget - 1) // h, 0)  # one row is owed to the "+N more" note
    shown = built[:n_fit]
    rows = [line for _, lines in shown for line in lines]
    extra = len(windows) - len(shown)
    if rows and extra > 0:
        rows.append(f"[dim]{'':<{_FEED_IDENT_W}} +{extra} more armed[/dim]")
    return (rows + focus)[:cap]


# ---------- the panel ----------

def charts_inner_height(snap: dict, width: int, now: float | None = None,
                        tall: bool = False) -> int:
    """Inner rows the charts row wants at this height, or 0 when it has
    nothing to say.

    Zero is the whole degradation contract: a box with no armed symbol and no
    corpus paints no charts row at all rather than an empty box taking rows
    off the tape. The caller asks tall first and falls back to compact — the
    all-or-nothing rule in charts_rows_shown then means a short screen gets
    one-row charts before it gets none.
    """
    now = time.time() if now is None else now
    cap = CHARTS_MAX_INNER_TALL if tall else CHARTS_MAX_INNER
    return min(len(feeds_rows(snap, width, now, tall)), cap)


def _panel(rows: list[str], empty: str, title: str):
    from rich.panel import Panel
    from rich.text import Text

    body = Text.from_markup("\n".join(rows) if rows else empty)
    # One chart, one row. A wrapped line silently costs the panel a second row
    # and the layout's height arithmetic stops holding — same rule as the tape.
    body.no_wrap, body.overflow = True, "ellipsis"
    return Panel(body, title=title, title_align="left", border_style="dim")


def build_feeds_panel(snap: dict, width: int, now: float | None = None,
                      tall: bool = False):
    """The underlying against each armed window's strike, plus the lead block."""
    return _panel(feeds_rows(snap, width, now, tall),
                  "[dim]no armed symbol this box's corpus can price[/dim]",
                  "[bold]feeds[/bold] [dim]· price vs strike[/dim]")
