"""Pure tests for the leader-persistence regime gauge (polymarket/regime.py).

No network, no ~/.pmt: every test builds a synthetic book tape with known
leaders and known outcomes, so the persistence the estimator reports can be
checked against arithmetic done by hand rather than against itself.

The three things worth pinning, in order of how expensive getting them wrong
would be:

  1. THE FRESHNESS CONVENTION. The recorder samples the two half-books
     independently, so a row can carry an `up_ask` from 11 seconds ago beside a
     current `dn_ask`. A de-vig over such a row prices a quote that had already
     gone (underdog_search.md §2). Stale rows — and rows predating the age
     fields entirely — must be EXCLUDED, and the mark must not slide forward to
     the next fresh row to rescue the window.
  2. THE ARITHMETIC. Known leaders + known winners -> an exact k/n, an exact
     Wilson interval, an exact trend sign.
  3. THE COLD START. No corpus file -> `latest()` is None -> the watch header
     drops the row rather than painting a 0% that reads as "the leader never
     holds".

The watch-side rendering of the same gauge is tested in test_watch_ui.py; the
CLI's output shape is in test_cli_crypto_data.py.
"""

from __future__ import annotations

import json

import pytest

from polymarket import regime


# ---------- synthetic tape helpers ----------

DUR = 300


def slug(sym: str, start: int, tenor: str = "5m") -> str:
    return f"{sym}-updown-{tenor}-{start}"


def book_row(slug_: str, t: float, up_bid: float, up_ask: float,
             dn_bid: float | None = None, dn_ask: float | None = None,
             up_age: float | None = 40.0, dn_age: float | None = 40.0) -> dict:
    """One book snapshot. The `dn` half defaults to the complement of `up`
    (what a two-sided quote on one event actually looks like)."""
    if dn_bid is None:
        dn_bid = round(1.0 - up_ask, 4)
    if dn_ask is None:
        dn_ask = round(1.0 - up_bid, 4)
    return {"ev": "book", "slug": slug_, "t": t,
            "up_bid": up_bid, "up_ask": up_ask,
            "dn_bid": dn_bid, "dn_ask": dn_ask,
            "up_age_ms": up_age, "dn_age_ms": dn_age}


def window(sym: str, start: int, up_price: float, winner: str,
           at: float = 0.26, **kw) -> tuple[str, list[dict], dict]:
    """(slug, rows, outcome) for one window whose book quotes `up_price`.

    Two rows: one before the mark and one at `at`, so a test that moves the
    mark or breaks the freshness of the marked row is testing selection, not
    an empty tape.
    """
    s = slug(sym, start)
    rows = [book_row(s, start + 5, up_price - 0.01, up_price + 0.01),
            book_row(s, start + DUR * at, up_price - 0.01, up_price + 0.01, **kw)]
    return s, rows, {"slug": s, "winner": winner, "source": "wallet"}


def tape_files(tmp_path, windows, name: str = "book-tape.jsonl"):
    """Write the rows to a book tape and the outcomes to a corpus file."""
    book = tmp_path / name
    with open(book, "w") as fh:
        for _s, rows, _o in windows:
            for r in rows:
                fh.write(json.dumps(r) + "\n")
    out = tmp_path / "outcomes.jsonl"
    with open(out, "w") as fh:
        for _s, _rows, o in windows:
            fh.write(json.dumps(o) + "\n")
    return book, out


def run(tmp_path, windows, **kw) -> dict:
    book, out = tape_files(tmp_path, windows)
    return regime.estimate(sources=[book], outcomes_path=out, **kw)


# ---------- the de-vig and the leader ----------

def test_devig_normalises_the_two_half_books():
    # up mid 0.62, dn mid 0.40 -> the pair sums to 1.02 (the vig) and the
    # de-vig divides it out.
    assert regime.devig_up(0.61, 0.63, 0.39, 0.41) == pytest.approx(0.62 / 1.02)


def test_devig_falls_back_to_one_side_then_gives_up():
    assert regime.devig_up(0.61, 0.63, None, None) == pytest.approx(0.62)
    assert regime.devig_up(None, None, 0.39, 0.41) == pytest.approx(0.60)
    assert regime.devig_up(None, None, None, None) is None


def test_a_coin_flip_book_has_no_leader_and_is_not_counted():
    """|dv - 0.5| <= 0.05 is not a claim about direction. Counting it either
    way would put the gauge's own noise floor into the numerator."""
    s, rows, o = window("btc", 1_700_000_000, 0.52, "up")
    got, why = regime.observe(s, rows, o["winner"], "wallet")
    assert got is None and why == regime.SKIP_NO_LEAD


def test_a_leader_is_scored_against_the_winner():
    s, rows, _o = window("btc", 1_700_000_000, 0.70, "up")
    hit, _ = regime.observe(s, rows, "up", "wallet")
    miss, _ = regime.observe(s, rows, "down", "wallet")
    assert hit["leader"] == "up" and hit["hit"] == 1
    assert miss["leader"] == "up" and miss["hit"] == 0


# ---------- the freshness convention ----------

def test_a_stale_half_book_excludes_the_window():
    """ONE side over the bound is enough — the row is not one instant."""
    s, rows, _o = window("btc", 1_700_000_000, 0.70, "up",
                         up_age=11_000.0, dn_age=40.0)
    got, why = regime.observe(s, rows, "up", "wallet")
    assert got is None and why == regime.SKIP_STALE


def test_a_row_with_no_age_fields_is_stale_not_fresh():
    """Rows predating the recorder's age fields carry no age at all. They can
    be read one side at a time and must never be read as a pair."""
    s, rows, _o = window("btc", 1_700_000_000, 0.70, "up",
                         up_age=None, dn_age=None)
    got, why = regime.observe(s, rows, "up", "wallet")
    assert got is None and why == regime.SKIP_STALE
    assert regime.both_fresh({"up_bid": 0.6}) is False


def test_the_mark_does_not_slide_past_a_stale_row():
    """The study read the snapshot at 0.25 or nothing. Advancing to the next
    fresh row would silently sample a different, later book."""
    s = slug("btc", 1_700_000_000)
    rows = [book_row(s, 1_700_000_000 + 78, 0.69, 0.71, up_age=9_000.0),
            book_row(s, 1_700_000_000 + 90, 0.69, 0.71)]
    got, why = regime.observe(s, rows, "up", "wallet")
    assert got is None and why == regime.SKIP_STALE


def test_the_mark_is_the_first_snapshot_at_or_after_elapsed_025():
    s = slug("btc", 1_700_000_000)
    rows = [book_row(s, 1_700_000_000 + 60, 0.30, 0.32),   # elapsed 0.20
            book_row(s, 1_700_000_000 + 76, 0.69, 0.71),   # elapsed 0.2533 <- mark
            book_row(s, 1_700_000_000 + 200, 0.90, 0.92)]  # elapsed 0.667
    got, _ = regime.observe(s, rows, "up", "wallet")
    assert got["elapsed"] == pytest.approx(76 / 300)
    assert got["dv_up"] == pytest.approx(0.70)


def test_a_tape_that_never_reaches_the_mark_is_skipped():
    s = slug("btc", 1_700_000_000)
    rows = [book_row(s, 1_700_000_000 + 30, 0.69, 0.71)]
    got, why = regime.observe(s, rows, "up", "wallet")
    assert got is None and why == regime.SKIP_NO_MARK


# ---------- exact persistence on a synthetic tape ----------

def _mixed(n_hit: int, n_miss: int, sym: str = "btc", t0: int = 1_700_000_000):
    """`n_hit` windows the leader won plus `n_miss` it lost, alternating side
    so no result depends on 'up' in particular."""
    out, start = [], t0
    for i in range(n_hit):
        up = i % 2 == 0
        out.append(window(sym, start, 0.70 if up else 0.30,
                          "up" if up else "down"))
        start += DUR
    for i in range(n_miss):
        up = i % 2 == 0
        out.append(window(sym, start, 0.70 if up else 0.30,
                          "down" if up else "up"))
        start += DUR
    return out


def test_known_leaders_and_outcomes_give_exact_persistence(tmp_path):
    est = run(tmp_path, _mixed(8, 2), trail=10)
    g = est["fleet"]
    assert (g["k"], g["n"]) == (8, 10)
    assert g["persist"] == pytest.approx(0.8)
    lo, hi = regime.wilson_ci(8, 10)
    assert (g["lo"], g["hi"]) == (pytest.approx(lo), pytest.approx(hi))
    assert regime.band(0.8) == "strong"


def test_the_trailing_block_is_the_last_n_not_all_of_history(tmp_path):
    """20 windows: the first 10 all miss, the last 10 all hit. Trailing-10
    must read 100%, and the trend must see the block it replaced."""
    ws = _mixed(0, 10) + _mixed(10, 0, t0=1_700_000_000 + DUR * 10)
    est = run(tmp_path, ws, trail=10)
    g = est["fleet"]
    assert (g["k"], g["n"]) == (10, 10)
    assert g["prior_n"] == 10 and g["prior_persist"] == pytest.approx(0.0)
    assert g["delta"] == pytest.approx(1.0)
    assert g["arrow"] == "↑"


def test_a_deteriorating_regime_reads_negative(tmp_path):
    """The sign convention: CURRENT MINUS PRIOR. underdog_regime.py quoted the
    same comparison as train - holdout (+3.12); a live gauge that went the same
    way must show a MINUS, or the arrow points at the wrong regime."""
    ws = _mixed(10, 0) + _mixed(4, 6, t0=1_700_000_000 + DUR * 10)
    est = run(tmp_path, ws, trail=10)
    g = est["fleet"]
    assert g["persist"] == pytest.approx(0.4)
    assert g["delta"] == pytest.approx(-0.6)
    assert g["z"] < 0 and g["arrow"] == "↓"
    assert regime.band(g["persist"]) == "weak"


def test_the_trend_arrow_withholds_judgement_without_a_prior_block(tmp_path):
    est = run(tmp_path, _mixed(5, 0), trail=10)
    assert est["fleet"]["prior_n"] == 0
    assert est["fleet"]["arrow"] == "·"


def test_series_and_fleet_are_separate_scopes(tmp_path):
    ws = _mixed(10, 0, sym="btc") + _mixed(0, 10, sym="eth")
    est = run(tmp_path, ws, trail=50)
    assert est["series"]["btc 5m"]["persist"] == pytest.approx(1.0)
    assert est["series"]["eth 5m"]["persist"] == pytest.approx(0.0)
    assert est["fleet"]["persist"] == pytest.approx(0.5)


def test_skips_are_counted_and_named(tmp_path):
    ws = (_mixed(3, 0)
          + [window("btc", 1_700_100_000, 0.52, "up")]                  # no lead
          + [window("btc", 1_700_200_000, 0.70, "up", up_age=9_000.0)])  # stale
    est = run(tmp_path, ws, trail=50)
    assert est["observations"] == 3
    assert est["skips"][regime.SKIP_NO_LEAD] == 1
    assert est["skips"][regime.SKIP_STALE] == 1


def test_only_terminal_grades_count(tmp_path):
    """A chainlink or book-pinned verdict is our own read of settlement — an
    input to the gauge's inputs, never its scoreboard."""
    ws = _mixed(2, 0)
    book, out = tape_files(tmp_path, ws)
    with open(out, "w") as fh:
        for i, (_s, _rows, o) in enumerate(ws):
            fh.write(json.dumps({**o, "source": "wallet" if i == 0 else "chainlink"}) + "\n")
    est = regime.estimate(sources=[book], outcomes_path=out)
    assert est["resolved"] == 1 and est["observations"] == 1


def test_observations_are_ordered_by_window_end_not_tape_order(tmp_path):
    ws = list(reversed(_mixed(4, 0)))
    est = run(tmp_path, ws)
    ends = [o["end"] for o in est["obs"]]
    assert ends == sorted(ends)


# ---------- the corpus rows ----------

def test_every_row_carries_the_gauge_as_of_that_window(tmp_path):
    est = run(tmp_path, _mixed(0, 5) + _mixed(5, 0, t0=1_700_000_000 + DUR * 5),
              trail=5)
    rows = regime.rows_for(est["obs"], trail=5)
    assert len(rows) == 10
    assert rows[4]["fleet_persist"] == pytest.approx(0.0)   # after the 5 misses
    assert rows[9]["fleet_persist"] == pytest.approx(1.0)   # after the 5 hits
    assert all(r["method"] == regime.METHOD for r in rows)
    assert rows[0]["slug"].startswith("btc-updown-5m-")


def test_writing_is_idempotent_by_slug(tmp_path):
    est = run(tmp_path, _mixed(4, 1))
    rows = regime.rows_for(est["obs"])
    dest = tmp_path / "regime.jsonl"
    assert regime.write_rows(rows, dest) == 5
    assert regime.write_rows(rows, dest) == 0     # a re-run says nothing new
    assert len(regime.load_rows(dest)) == 5


def test_rebuild_recuts_the_file(tmp_path):
    dest = tmp_path / "regime.jsonl"
    est = run(tmp_path, _mixed(4, 1))
    regime.write_rows(regime.rows_for(est["obs"]), dest)
    regime.write_rows(regime.rows_for(est["obs"])[:2], dest, rebuild=True)
    assert len(regime.load_rows(dest)) == 2


def test_latest_reads_the_newest_row_and_tolerates_junk(tmp_path):
    dest = tmp_path / "regime.jsonl"
    est = run(tmp_path, _mixed(3, 0))
    regime.write_rows(regime.rows_for(est["obs"]), dest)
    with open(dest, "a") as fh:
        fh.write("{not json\n\n")
    got = regime.latest(dest)
    assert got is not None and got["fleet_n"] == 3


def test_latest_is_none_on_a_cold_box(tmp_path):
    """THE cold-start contract every consumer holds to."""
    assert regime.latest(tmp_path / "nope.jsonl") is None
    assert regime.load_rows(tmp_path / "nope.jsonl") == []


# ---------- reading real-shaped tapes ----------

def test_corrupt_and_foreign_lines_are_skipped(tmp_path):
    ws = _mixed(2, 0)
    book, out = tape_files(tmp_path, ws)
    with open(book, "a") as fh:
        fh.write("{truncated mid-write\n")
        fh.write(json.dumps({"ev": "book", "slug": "nfl-game-123", "t": 1}) + "\n")
        fh.write("\n")
    est = regime.estimate(sources=[book], outcomes_path=out)
    assert est["observations"] == 2


def test_book_tape_sources_globs_the_archives_and_appends_the_live_tape(tmp_path):
    (tmp_path / "r7-book-tape-frozen.jsonl").write_text("")
    (tmp_path / "book-tape-20260823-snapshot.jsonl").write_text("")
    (tmp_path / "outcomes.jsonl").write_text("")
    engine = tmp_path / "engine"
    engine.mkdir()
    live = engine / "book-tape.jsonl"          # ~/.pmt/engine, not the corpus
    live.write_text("")
    got = [str(p.relative_to(tmp_path))
           for p in regime.book_tape_sources(engine_tape=str(live),
                                             corpus=tmp_path)]
    assert got == ["book-tape-20260823-snapshot.jsonl",
                   "r7-book-tape-frozen.jsonl", "engine/book-tape.jsonl"]


def test_a_missing_live_tape_is_not_an_error(tmp_path):
    """A fresh clone, or a box where the engine has never run. The archives
    still answer, and the estimator still reports."""
    (tmp_path / "r7-book-tape-frozen.jsonl").write_text("")
    got = regime.book_tape_sources(engine_tape=str(tmp_path / "gone.jsonl"),
                                   corpus=tmp_path)
    assert [p.name for p in got] == ["r7-book-tape-frozen.jsonl"]


def test_tenor_and_series_are_read_filters_on_one_estimator(tmp_path):
    ws = _mixed(4, 0, sym="btc") + _mixed(0, 4, sym="eth")
    ws += [(s, r, o) for s, r, o in
           [window("btc", 1_700_500_000 + i * 900, 0.70, "up") for i in range(3)]]
    book, out = tape_files(tmp_path, ws)
    only_btc = regime.estimate(sources=[book], outcomes_path=out, series="btc")
    assert set(only_btc["series"]) == {"btc 5m"}
    only_eth = regime.estimate(sources=[book], outcomes_path=out, series="eth 5m")
    assert only_eth["fleet"]["persist"] == pytest.approx(0.0)


def test_coverage_names_what_the_gauge_could_not_see(tmp_path):
    """The gauge joins to the OUTCOMES corpus, and that corpus lags the book
    tape. A number quoted off the graded slice of a span without saying so is
    worse than no number — traded windows grade first."""
    ws = _mixed(4, 0)
    book, out = tape_files(tmp_path, ws)
    # Two more windows on the tape that nothing has graded yet.
    with open(book, "a") as fh:
        for i in range(2):
            s, rows, _o = window("btc", 1_700_900_000 + i * DUR, 0.70, "up")
            for r in rows:
                fh.write(json.dumps(r) + "\n")
    cov = regime.estimate(sources=[book], outcomes_path=out)["coverage"]
    assert cov["pending"] == 2
    assert cov["book_end"] > cov["gauge_end"]
    assert cov["lag_s"] > 0


# ---------- statistics ----------

def test_wilson_matches_the_studys_published_interval():
    """underdog_regime.txt: 417/523 = 79.7% Wilson95 [76.1, 83.0], and
    392/548 = 71.5% [67.6, 75.2]. Same arithmetic or the gauge is not the
    study's number."""
    lo, hi = regime.wilson_ci(417, 523)
    assert (round(lo * 100, 1), round(hi * 100, 1)) == (76.1, 83.0)
    lo, hi = regime.wilson_ci(392, 548)
    assert (round(lo * 100, 1), round(hi * 100, 1)) == (67.6, 75.2)


def test_two_prop_z_matches_the_studys_published_z():
    """The study reported train - holdout = +3.12."""
    assert regime.two_prop_z(417, 523, 392, 548) == pytest.approx(3.12, abs=0.01)


def test_two_prop_z_is_none_on_an_empty_side():
    assert regime.two_prop_z(0, 0, 5, 10) is None


def test_band_thresholds_are_anchored_on_the_study():
    assert regime.band(0.797) == "strong"    # the training regime
    assert regime.band(0.715) == "weak"      # the holdout that inverted the bias
    assert regime.band(0.76) == "mixed"
    assert regime.band(None) == "unknown"


def test_gauge_on_an_empty_corpus_does_not_raise():
    g = regime.gauge([], trail=50)
    assert g["n"] == 0 and g["persist"] is None and g["arrow"] == "·"


# ---------- the selection check: two grading populations, one gauge ----------

def _graded(n_hit, n_miss, source, sym="btc", t0=1_700_000_000):
    return [(s, rows, {**o, "source": source})
            for s, rows, o in _mixed(n_hit, n_miss, sym=sym, t0=t0)]


def test_by_grade_splits_the_two_grading_populations(tmp_path):
    """A `wallet` grade exists because we TRADED the window; a `resolution`
    grade exists whether we did or not. They are not one population, and the
    gauge has to be able to say so."""
    ws = (_graded(10, 0, "wallet")
          + _graded(5, 5, "resolution", sym="eth", t0=1_700_100_000))
    est = run(tmp_path, ws)
    bg = est["by_grade"]
    assert bg["sources"]["wallet"]["persist"] == pytest.approx(1.0)
    assert bg["sources"]["resolution"]["persist"] == pytest.approx(0.5)
    assert bg["z"] is not None and bg["z"] > 0


def test_by_grade_has_no_z_with_only_one_population(tmp_path):
    est = run(tmp_path, _graded(6, 4, "wallet"))
    assert est["by_grade"]["z"] is None
    assert set(est["by_grade"]["sources"]) == {"wallet"}


def test_by_grade_reproduces_the_corpus_selection_gap():
    """The live corpus reads wallet 172/186 against resolution 341/447 — a
    16-point gap at z 4.73. Pinned as arithmetic so a refactor that quietly
    changed the split would fail rather than merely look different."""
    assert regime.two_prop_z(172, 186, 341, 447) == pytest.approx(4.73, abs=0.01)
    lo, hi = regime.wilson_ci(172, 186)
    assert (round(lo * 100, 1), round(hi * 100, 1)) == (87.8, 95.5)
