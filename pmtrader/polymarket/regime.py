"""Leader persistence — the fleet's book-only regime gauge.

ONE NUMBER, measured on WINDOWS and never on fills: **of the windows where the
book had a leader at elapsed 0.25, how often did that leader go on to win?**

Why it is worth a module. `pmt-alpha/analysis/underdog_search.md` §5 found this
quantity moved 79.7% -> 71.5% (two-proportion z = 3.12) between its training and
holdout periods — inside 24 hours — and that when it moved, the dog/favourite
axis inverted outright in elapsed [0.00, 0.25): dog bands 0.05-0.30 went
+7.96% to +40.93% while favourite bands 0.60-0.95 went -5.95% to -14.66%. A
binary's price band IS a volatility position (buying the favourite is short
volatility, buying the dog is long it), so when the early leader stops holding,
every favourite band gets worse and every dog band gets better, together. The
fleet takes that position on every window and has, until now, taken it blind.

This module MEASURES the quantity and nothing else. It reads no model, prices
no fill, touches no ledger, and gates nothing. See `docs/regime-gauge.md` for
the DARK sizing hook — the proposal for how it would modulate size once an A/B
has earned it.

THE DEFINITION, frozen (`METHOD`)
---------------------------------
For each resolved window:

  1. Take every book snapshot the tape holds for the slug with
     `start <= t < end`; `elapsed = (t - start) / dur_s`.
  2. The MARKED snapshot is the first one at or after `elapsed >= 0.25`
     (`ELAPSED_MARK`). A window whose tape never reaches the mark is skipped.
  3. FRESHNESS (`FRESH_MS` = 1000ms). The recorder samples the two half-books
     independently, so one row can carry an `up_ask` from 11 seconds ago beside
     a current `dn_ask`. Reading such a row as one instant prices a quote that
     had already gone. The marked snapshot must carry BOTH `up_age_ms` and
     `dn_age_ms` within the bound or the window is EXCLUDED — including rows
     that predate the recorder's age fields, which carry no age at all. The
     mark is not advanced to the next fresh row: the study read the snapshot at
     0.25 or nothing, and so does this.
  4. The book's own P(up) is the DE-VIGGED mid (`devig_up`): both half-books
     quote the same event, `mid_up` and `1 - mid_dn` disagree by the vig, and
     normalising the pair is the standard de-vig.
  5. A window has a LEADER only when `|dv_up - 0.5| > 0.05` (`LEAD_EPS`) — a
     coin-flip book is not a claim about direction and does not belong in
     either half of the ratio.
  6. HIT when `(dv_up > 0.5) == (winner == 'up')`.

Grading is the outcomes corpus, TERMINAL sources only (`wallet` /
`resolution`, `outcomes.is_terminal_source`). A chainlink or book-pinned guess
is our own read of settlement, which is an input and never a scoreboard.

THE GAUGE is that ratio over the trailing `TRAIL_DEFAULT` resolved windows,
ordered by window END, with a Wilson 95% interval; the TREND compares it to the
block of `trail` windows immediately before it. `trend_z` is signed
CURRENT MINUS PRIOR, so a deteriorating regime reads negative — the study
quoted the same comparison the other way round (train - holdout = +3.12).

Two scopes, both carried on every row: per SERIES (`btc 5m`) and FLEET-wide
(every series pooled, which is the scope a fleet-level size decision is made
at). The study's headline is 5m-only; per-series rows are how you read it back.
"""

from __future__ import annotations

import json
import math
import os
from collections.abc import Iterable, Iterator
from pathlib import Path

from . import errlog, tape
from .outcomes import OUTCOMES_PATH, is_terminal_source
from .updown_slugs import dur_label, parse_updown_slug, series_key

# The frozen methodology. Stamped onto every row this module writes, so a
# study joining against the corpus can tell a v1 row from a later re-cut
# without re-deriving anything. Bump it when any of the four constants below,
# the grading rule, or the ordering key changes — never for a render change.
METHOD = "leader@0.25/fresh1000ms/lead>0.05/terminal-grade/v1"

ELAPSED_MARK = 0.25
FRESH_MS = 1000.0
LEAD_EPS = 0.05
TRAIL_DEFAULT = 50

# The prior block needs enough windows to be a comparison rather than noise.
# Below it the trend reads "·" (unknown) instead of an arrow that is one
# window's worth of luck.
MIN_PRIOR_N = 10
# What counts as a move rather than jitter, in probability points.
TREND_EPS = 0.02

CORPUS = Path.home() / ".pmt" / "corpus"
# The ONE file this package writes outside the outcomes/journal paths. Append
# only, one line per resolved window, keyed by slug.
REGIME_PATH = CORPUS / "regime.jsonl"

# Persistence bands for display. Anchored on the study, not on taste: 79.7%
# was the training regime, 71.5% the holdout that inverted the bias, and 75%
# is the dark sizing hook's proposed line (docs/regime-gauge.md).
BAND_STRONG = 0.78
BAND_WEAK = 0.75


# ---------- the marked snapshot ----------

def devig_up(up_bid, up_ask, dn_bid, dn_ask) -> float | None:
    """The book's own P(up) with the two-sided vig divided out, or None.

    Falls back to a one-sided mid when only one half-book is quoted, and to
    None when neither is. Verbatim in behaviour with the vault's
    `underdog_tape.devig_up`, which every family in the study compared a price
    against.
    """
    mu = _mid(up_bid, up_ask)
    md = _mid(dn_bid, dn_ask)
    if mu is not None and md is not None:
        s = mu + md
        return mu / s if s > 1e-9 else None
    if mu is not None:
        return mu
    if md is not None:
        return 1.0 - md
    return None


def _mid(bid, ask) -> float | None:
    b, a = _num(bid), _num(ask)
    if b is None or a is None:
        return None
    return (b + a) / 2.0


def _num(v) -> float | None:
    """A finite float, or None for null / non-numeric / nan / inf."""
    if v is None or isinstance(v, bool):
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


def both_fresh(rec: dict, fresh_ms: float = FRESH_MS) -> bool:
    """True when BOTH half-books on this snapshot were quoted within the bound.

    Rows predating the recorder's per-side age fields carry no age and return
    False: they can be read one side at a time and must never be read as a
    pair (underdog_search.md §2).
    """
    ua, da = _num(rec.get("up_age_ms")), _num(rec.get("dn_age_ms"))
    return ua is not None and da is not None and ua <= fresh_ms and da <= fresh_ms


def marked_snapshot(rows: Iterable[dict], start: float, dur_s: float,
                    mark: float = ELAPSED_MARK) -> dict | None:
    """The first snapshot at or after `elapsed >= mark`, or None.

    `rows` need not be sorted — elapsed is monotone in `t`, so the first row
    at-or-after the mark is exactly the eligible row with the SMALLEST elapsed.
    Ties keep the row seen first, which mirrors the vault's tape builder
    (earlier source wins on a duplicated `t`).
    """
    best: dict | None = None
    best_el = None
    for r in rows:
        t = _num(r.get("t"))
        if t is None or not (start <= t < start + dur_s):
            continue
        el = (t - start) / dur_s
        if el < mark:
            continue
        if best_el is None or el < best_el:
            best, best_el = r, el
    return best


# ---------- one window's observation ----------

# Why a window did not reach the ratio. Counted and printed, never silent: a
# gauge that quietly drops three quarters of the fleet is not a gauge.
SKIP_NO_MARK = "no_mark"        # tape never reached elapsed 0.25
SKIP_STALE = "stale"            # the marked snapshot failed the freshness bound
SKIP_NO_PRICE = "no_price"      # neither half-book quoted -> no de-vig
SKIP_NO_LEAD = "no_lead"        # |dv_up - 0.5| <= LEAD_EPS: the book had no leader


def observe(slug: str, rows: Iterable[dict], winner: str, grade: str,
            mark: float = ELAPSED_MARK, fresh_ms: float = FRESH_MS,
            lead_eps: float = LEAD_EPS) -> tuple[dict | None, str | None]:
    """One resolved window -> (observation, None) or (None, skip reason).

    The observation carries the marked snapshot's own coordinates (`t`,
    `elapsed`, `dv_up`) as well as the verdict, because the mark is only
    bounded BELOW: a tape gap can put it well past 0.25 and a study joining
    against the corpus has to be able to see that and filter.
    """
    w = parse_updown_slug(slug)
    if w is None:
        return None, SKIP_NO_MARK
    r = marked_snapshot(rows, w["start"], w["dur_s"], mark)
    if r is None:
        return None, SKIP_NO_MARK
    if not both_fresh(r, fresh_ms):
        return None, SKIP_STALE
    dv = devig_up(r.get("up_bid"), r.get("up_ask"), r.get("dn_bid"), r.get("dn_ask"))
    if dv is None:
        return None, SKIP_NO_PRICE
    if abs(dv - 0.5) <= lead_eps:
        return None, SKIP_NO_LEAD
    leader = "up" if dv > 0.5 else "down"
    t = float(r["t"])
    return {
        "slug": slug,
        "series": series_key(w["symbol"], w["dur_s"]),
        "symbol": w["symbol"],
        "tenor": dur_label(w["dur_s"]),
        "start": w["start"],
        "end": w["end"],
        "t": t,
        "elapsed": (t - w["start"]) / w["dur_s"],
        "dv_up": dv,
        "leader": leader,
        "winner": winner,
        "grade": grade,
        "hit": 1 if leader == winner else 0,
    }, None


# ---------- statistics ----------

def wilson_ci(k: int, n: int, z: float = 1.96) -> tuple[float, float]:
    """Wilson score interval on k/n — the vault's convention for a rate."""
    if n <= 0:
        return (0.0, 1.0)
    p = k / n
    d = 1.0 + z * z / n
    c = p + z * z / (2 * n)
    m = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n))
    return ((c - m) / d, (c + m) / d)


def two_prop_z(k1: int, n1: int, k2: int, n2: int) -> float | None:
    """Two-proportion z for p1 - p2, pooled. None when either side is empty.

    Sign is CURRENT MINUS PRIOR when called by `gauge` — a deteriorating
    regime reads negative. underdog_regime.py quoted the same comparison as
    train - holdout (+3.12); this is that number with the sign flipped.
    """
    if n1 <= 0 or n2 <= 0:
        return None
    p = (k1 + k2) / (n1 + n2)
    se = math.sqrt(p * (1 - p) * (1 / n1 + 1 / n2))
    if se <= 0:
        return None
    return (k1 / n1 - k2 / n2) / se


def trend_arrow(delta: float | None, prior_n: int,
                eps: float = TREND_EPS, min_prior: int = MIN_PRIOR_N) -> str:
    """The one-glyph read on the trend, or `·` when there is nothing to compare."""
    if delta is None or prior_n < min_prior:
        return "·"
    if delta >= eps:
        return "↑"
    if delta <= -eps:
        return "↓"
    return "→"


def gauge(obs: list[dict], trail: int = TRAIL_DEFAULT) -> dict:
    """The gauge over the LAST `trail` observations of an ordered list.

    `obs` must already be in window-END order; this function does not sort,
    because the caller's ordering IS the as-of semantics (see `rows_for`).
    Returns zeros — never raises — on an empty list, so a cold corpus renders
    rather than crashes.
    """
    cur = obs[-trail:] if trail > 0 else list(obs)
    prior = obs[-2 * trail:-trail] if trail > 0 else []
    n, k = len(cur), sum(o["hit"] for o in cur)
    pn, pk = len(prior), sum(o["hit"] for o in prior)
    persist = k / n if n else None
    prior_persist = pk / pn if pn else None
    delta = (persist - prior_persist
             if persist is not None and prior_persist is not None else None)
    lo, hi = wilson_ci(k, n)
    return {
        "n": n, "k": k, "persist": persist, "lo": lo, "hi": hi,
        "prior_n": pn, "prior_k": pk, "prior_persist": prior_persist,
        "delta": delta, "z": two_prop_z(k, n, pk, pn),
        "arrow": trend_arrow(delta, pn), "trail": trail,
        # The block's own span, so a reader can tell a trailing-50 covering
        # four hours from one covering four days.
        "span_start": cur[0]["end"] if cur else None,
        "t_end": cur[-1]["end"] if cur else None,
    }


def band(persist: float | None) -> str:
    """`strong` / `mixed` / `weak` — the display band, anchored on the study."""
    if persist is None:
        return "unknown"
    if persist >= BAND_STRONG:
        return "strong"
    if persist >= BAND_WEAK:
        return "mixed"
    return "weak"


# ---------- reading the book tape ----------

def book_tape_sources(engine_tape: str | None = None,
                      corpus: Path | None = None) -> list[Path]:
    """Every book tape on this box, oldest archive first, live tape last.

    Globbed rather than named: the frozen archives (`r7-book-tape-frozen`,
    `book-tape-YYYYMMDD-snapshot`) are dated one-offs and a hard-coded list
    goes stale the next time one is cut. Order matters only for duplicate
    `t` resolution, where first-seen wins.
    """
    root = CORPUS if corpus is None else Path(corpus)
    live = Path(tape.BOOK_TAPE if engine_tape is None else engine_tape)
    out = [p for p in sorted(root.glob("*book-tape*.jsonl"))]
    if live.exists():
        out.append(live)
    seen: set[str] = set()
    uniq = []
    for p in out:
        rp = str(p.resolve())
        if rp in seen or not p.exists():
            continue
        seen.add(rp)
        uniq.append(p)
    return uniq


def iter_book(paths: Iterable[Path]) -> Iterator[dict]:
    """Book snapshots from every tape, skipping blank and corrupt lines.

    Same tolerance rule as `tape.iter_records`: a line truncated mid-write by
    a crashing engine must never take a consumer down with it.
    """
    for p in paths:
        try:
            fh = open(p)
        except OSError:
            continue
        with fh:
            for line in fh:
                if "-updown-" not in line:
                    continue
                try:
                    r = json.loads(line)
                except ValueError:
                    continue
                if isinstance(r, dict) and r.get("slug"):
                    yield r


def collect_marks(paths: Iterable[Path], slugs: set[str] | None = None,
                  mark: float = ELAPSED_MARK) -> dict[str, dict]:
    """{slug: the marked snapshot} over every tape, in ONE streaming pass.

    Only one row per slug is ever held: the mark is the eligible snapshot with
    the smallest elapsed, so a running minimum is exact and the whole book
    history costs a few thousand dicts rather than a few hundred megabytes.
    """
    best: dict[str, tuple[float, dict]] = {}
    for r in iter_book(paths):
        slug = r["slug"]
        if slugs is not None and slug not in slugs:
            continue
        w = parse_updown_slug(slug)
        if w is None:
            continue
        t = _num(r.get("t"))
        if t is None or not (w["start"] <= t < w["end"]):
            continue
        el = (t - w["start"]) / w["dur_s"]
        if el < mark:
            continue
        prev = best.get(slug)
        if prev is None or el < prev[0]:
            best[slug] = (el, r)
    return {s: r for s, (_el, r) in best.items()}


def resolved_windows(outcomes_path: Path | None = None) -> dict[str, dict]:
    """{slug: {'winner', 'source'}} for every TERMINALLY graded window.

    Terminal only (`wallet` / `resolution`, via `outcomes.is_terminal_source`).
    A chainlink or book-pinned row is our own read of settlement — an input to
    the gauge's inputs, never its scoreboard, exactly as the vault's tape
    builder treats it.
    """
    path = OUTCOMES_PATH if outcomes_path is None else Path(outcomes_path)
    out: dict[str, dict] = {}
    if not path.exists():
        return out
    with open(path) as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                r = json.loads(line)
            except ValueError as e:
                errlog.note("regime.load_winners.corrupt_line", e,
                            path=str(path), line=line[:200])
                continue
            if not isinstance(r, dict):
                continue
            slug, winner = r.get("slug"), r.get("winner")
            if not slug or winner not in ("up", "down"):
                continue
            if not is_terminal_source(r.get("source")):
                continue
            out[slug] = {"winner": winner, "source": r.get("source")}
    return out


def observations(marks: dict[str, dict], winners: dict[str, dict],
                 mark: float = ELAPSED_MARK, fresh_ms: float = FRESH_MS,
                 lead_eps: float = LEAD_EPS) -> tuple[list[dict], dict[str, int]]:
    """(observations in window-END order, skip counts by reason).

    The ordering key is the window END and the slug, never the tape's write
    order: two series close on the same second and the gauge's as-of state has
    to be reproducible from the corpus alone.
    """
    obs: list[dict] = []
    skips: dict[str, int] = {}
    for slug, w in winners.items():
        r = marks.get(slug)
        if r is None:
            skips[SKIP_NO_MARK] = skips.get(SKIP_NO_MARK, 0) + 1
            continue
        o, why = observe(slug, [r], w["winner"], w.get("source") or "terminal",
                         mark, fresh_ms, lead_eps)
        if o is None:
            skips[why] = skips.get(why, 0) + 1
            continue
        obs.append(o)
    obs.sort(key=lambda o: (o["end"], o["slug"]))
    return obs, skips


# ---------- the corpus rows ----------

def rows_for(obs: list[dict], trail: int = TRAIL_DEFAULT) -> list[dict]:
    """One corpus row per observation, carrying the gauge AS OF that window.

    Both scopes are stamped on every row: the SERIES gauge (that series' own
    trailing `trail` windows up to and including this one) and the FLEET gauge
    (every series pooled, same rule). Recomputed forwards from the start of
    `obs`, so the file is a function of the corpus and not of the order the
    estimator happened to run in.
    """
    per_series: dict[str, list[dict]] = {}
    running: list[dict] = []
    out: list[dict] = []
    for o in obs:
        running.append(o)
        s = per_series.setdefault(o["series"], [])
        s.append(o)
        sg = gauge(s, trail)
        fg = gauge(running, trail)
        row = dict(o)
        row["method"] = METHOD
        row["trail"] = trail
        for scope, g in (("series", sg), ("fleet", fg)):
            for f in ("n", "k", "persist", "lo", "hi", "prior_n",
                      "prior_persist", "delta", "z", "arrow"):
                row[f"{scope}_{f}"] = g[f]
        out.append(row)
    return out


def load_rows(path: Path | None = None) -> list[dict]:
    """Every row on disk, oldest first. Empty (never an error) on a cold box."""
    p = REGIME_PATH if path is None else Path(path)
    out: list[dict] = []
    if not p.exists():
        return out
    with open(p) as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                r = json.loads(line)
            except ValueError as e:
                # regime.jsonl is idempotent BY SLUG: a row lost to a bad line
                # is a window the next run will happily re-append, which is a
                # duplicate in a file whose whole contract is one row per slug.
                errlog.note("regime.load_rows.corrupt_line", e,
                            path=str(p), line=line[:200])
                continue
            if isinstance(r, dict) and r.get("slug"):
                out.append(r)
    return out


def write_rows(rows: list[dict], path: Path | None = None,
               rebuild: bool = False) -> int:
    """Append the rows this file does not already have; return how many landed.

    Idempotent by SLUG, which is what makes a re-run (or an overlapping
    backfill) add nothing it already said. `rebuild` rewrites the file from
    scratch — the only way to re-cut it after a `METHOD` bump, because the
    gauge state on a row is a function of everything before it.
    """
    p = REGIME_PATH if path is None else Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    if rebuild:
        tmp = p.with_suffix(p.suffix + ".tmp")
        with open(tmp, "w") as fh:
            for r in rows:
                fh.write(json.dumps(r, sort_keys=True) + "\n")
        os.replace(tmp, p)
        return len(rows)
    have = {r["slug"] for r in load_rows(p)}
    new = [r for r in rows if r["slug"] not in have]
    if new:
        with open(p, "a") as fh:
            for r in new:
                fh.write(json.dumps(r, sort_keys=True) + "\n")
    return len(new)


_TAIL_BYTES = 8192


def latest(path: Path | None = None) -> dict | None:
    """The newest row, read off the tail — or None when the gauge has never run.

    None is the COLD-START contract every consumer holds to: the watch header
    drops its regime row entirely rather than painting a zero, because "we
    have not measured this yet" and "the leader never holds" are opposite
    facts and must not share a rendering.
    """
    p = REGIME_PATH if path is None else Path(path)
    try:
        size = p.stat().st_size
        with open(p, "rb") as fh:
            if size > _TAIL_BYTES:
                fh.seek(-_TAIL_BYTES, os.SEEK_END)
            chunk = fh.read()
    except OSError:
        return None
    for raw in reversed(chunk.decode(errors="ignore").splitlines()):
        raw = raw.strip()
        if not raw:
            continue
        try:
            r = json.loads(raw)
        except ValueError:
            continue
        if isinstance(r, dict) and r.get("slug"):
            return r
    return None


# ---------- the whole estimate, in one call ----------

def coverage(marks: dict[str, dict], winners: dict[str, dict],
             fleet: dict) -> dict:
    """How much of the book tape the gauge could actually see.

    The gauge is joined to the OUTCOMES corpus, and that corpus is refreshed
    by `pmt crypto outcomes` / `pmt crypto stats --gates`, not by this module.
    So the binding constraint on the gauge is usually grading, not the tape:
    on this box the tape reached 11:00Z while the corpus stopped at 02:35Z,
    leaving 797 marked-but-ungraded windows. A gauge quoting a number off the
    graded 11% of a span without saying so is worse than no gauge — the
    windows that get graded first are the ones we TRADED, which is a selection
    on exactly the axis being measured. Hence: every consumer prints this.
    """
    ends = {s: parse_updown_slug(s)["end"] for s in marks
            if parse_updown_slug(s) is not None}
    book_end = max(ends.values(), default=None)
    gauge_end = fleet.get("t_end")
    pending = sum(1 for e in ends.values()
                  if gauge_end is not None and e > gauge_end)
    span_marked = span_graded = 0
    lo = fleet.get("span_start")
    if lo is not None and gauge_end is not None:
        for s, e in ends.items():
            if lo <= e <= gauge_end:
                span_marked += 1
                span_graded += 1 if s in winners else 0
    return {
        "book_end": book_end,
        "gauge_end": gauge_end,
        "lag_s": (book_end - gauge_end
                  if book_end is not None and gauge_end is not None else None),
        "pending": pending,
        "span_marked": span_marked,
        "span_graded": span_graded,
        "span_frac": span_graded / span_marked if span_marked else None,
    }


def by_grade(obs: list[dict]) -> dict:
    """Persistence split by GRADE SOURCE — the gauge's own selection check.

    A `wallet` grade exists because we redeemed the window, i.e. because we
    TRADED it; a `resolution` grade exists because gamma closed the market,
    trade or no trade. So the two populations are not the same population, and
    on this corpus they do not agree: 92.5% wallet against 76.3% resolution,
    z = 4.73. That gap is our own entry filter — the engine fires when the
    model agrees with the book's direction and the cushion holds, which is
    close to a definition of "the leader is going to hold".

    It matters because the corpus grades wallet rows FIRST (a redeem posts in
    minutes; gamma resolution lands when the report walks it), so the most
    RECENT slice of the gauge is the most selected slice of it. Every consumer
    prints this beside the headline for exactly that reason.
    """
    srcs: dict[str, dict] = {}
    for o in obs:
        d = srcs.setdefault(o["grade"], {"k": 0, "n": 0})
        d["n"] += 1
        d["k"] += o["hit"]
    for d in srcs.values():
        d["persist"] = d["k"] / d["n"] if d["n"] else None
        d["lo"], d["hi"] = wilson_ci(d["k"], d["n"])
    a, b = srcs.get("wallet"), srcs.get("resolution")
    return {"sources": srcs,
            "z": two_prop_z(a["k"], a["n"], b["k"], b["n"]) if a and b else None}


def estimate(trail: int = TRAIL_DEFAULT, series: str | None = None,
             tenor: str | None = None, sources: list[Path] | None = None,
             outcomes_path: Path | None = None) -> dict:
    """Read the tapes, join the outcomes, and return the current gauge.

    `series` filters on the series key prefix (`btc`, `btc 5m`); `tenor`
    filters on the slug's duration label (`5m`). Both are read filters on the
    SAME estimator — never a second definition of it.
    """
    paths = book_tape_sources() if sources is None else list(sources)
    winners = {s: w for s, w in resolved_windows(outcomes_path).items()
               if _keep(s, series, tenor)}
    marks = {s: r for s, r in collect_marks(paths).items()
             if _keep(s, series, tenor)}
    obs, skips = observations(marks, winners)
    by_series: dict[str, list[dict]] = {}
    for o in obs:
        by_series.setdefault(o["series"], []).append(o)
    fleet = gauge(obs, trail)
    return {
        "trail": trail,
        "method": METHOD,
        "sources": [str(p) for p in paths],
        "resolved": len(winners),
        "observations": len(obs),
        "skips": skips,
        "fleet": fleet,
        "series": {k: gauge(v, trail) for k, v in sorted(by_series.items())},
        "coverage": coverage(marks, winners, fleet),
        "by_grade": by_grade(obs),
        "obs": obs,
    }


def _keep(slug: str, series: str | None, tenor: str | None) -> bool:
    w = parse_updown_slug(slug)
    if w is None:
        return False
    if tenor and dur_label(w["dur_s"]) != tenor:
        return False
    if series and not series_key(w["symbol"], w["dur_s"]).startswith(series):
        return False
    return True
