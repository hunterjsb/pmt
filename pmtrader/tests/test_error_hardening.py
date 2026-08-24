"""Regression tests for the crash class the errlog audit was opened on
(2026-08-24): the fleet scoreboard raising `AttributeError` and reaching the
operator as four words in a watch header cell.

Every test here pins ONE data shape that used to take the whole scoreboard —
`stats`, `watch` and `journal` all read it through the same function — down.
The shapes are not hypothetical: data-api really does answer /activity with a
JSON object, the live tape really does carry records from before `rho` existed,
and the wallet really is on a clock to cross data-api's 5000-row offset cap.
"""

from __future__ import annotations

import json

import pytest

import cli_crypto_stats as ccs
import stats_render
import watch_ui
from polymarket import errlog, tape, updown_slugs, wallet


class _Resp:
    def __init__(self, body, status=200, text=""):
        self._body, self.status_code, self.text = body, status, text or str(body)

    def json(self):
        if isinstance(self._body, _Unparseable):
            raise ValueError("no json")
        return self._body


class _Unparseable:
    pass


# ---------- THE crash: data-api answers /activity with an object ----------

def test_an_error_object_from_data_api_raises_a_named_error_not_attribute_error(monkeypatch):
    """The 2026-08-23 `scoreboard: AttributeError`.

    `.json() or []` handed the walk a dict; a dict is truthy, so `for a in page`
    iterated the KEYS and row_key() got the string "error" —
    `'str' object has no attribute 'get'`, thrown from inside the wallet walk
    with nothing naming the site.
    """
    body = {"error": "max historical activity offset of 5000 exceeded"}
    monkeypatch.setattr(wallet.requests, "get",
                        lambda *a, **k: _Resp(body, status=400))
    with pytest.raises(wallet.ActivityPageError) as ei:
        wallet.fetch_activity_page("0xabc", 5200)
    msg = str(ei.value)
    assert "5200" in msg           # which page
    assert "400" in msg            # what the server said about it
    assert "max historical activity offset" in msg   # and why, verbatim


def test_the_walk_itself_surfaces_the_offset_cap_rather_than_truncating(monkeypatch):
    """A short ledger printed confidently is worse than a loud failure: the
    walk must not quietly return page 1 and call it all-time."""
    full = [{"timestamp": 3_000_000 - i} for i in range(wallet.PAGE_SIZE)]

    def fake_get(url, params, headers, timeout):
        if params["offset"] == 0:
            return _Resp(full)
        return _Resp({"error": "max historical activity offset of 5000 exceeded"},
                     status=400)

    monkeypatch.setattr(wallet.requests, "get", fake_get)
    with pytest.raises(wallet.ActivityPageError):
        wallet.fetch_wallet_activity("0xabc", 0.0)


def test_a_non_json_body_names_the_status_and_the_body(monkeypatch):
    monkeypatch.setattr(wallet.requests, "get",
                        lambda *a, **k: _Resp(_Unparseable(), status=502,
                                              text="<html>bad gateway</html>"))
    with pytest.raises(wallet.ActivityPageError, match="502"):
        wallet.fetch_activity_page("0xabc", 0)


def test_a_non_object_row_is_raised_on_not_silently_dropped(monkeypatch):
    """Dropping it would be a silent hole in the ledger — the exact class of
    bug wallet.py's autopsy comment is about."""
    monkeypatch.setattr(wallet.requests, "get",
                        lambda *a, **k: _Resp([{"timestamp": 1}, "nope"]))
    with pytest.raises(wallet.ActivityPageError, match="str"):
        wallet.fetch_activity_page("0xabc", 0)


def test_a_good_page_still_comes_back_untouched(monkeypatch):
    rows = [{"timestamp": 2}, {"timestamp": 1}]
    monkeypatch.setattr(wallet.requests, "get", lambda *a, **k: _Resp(rows))
    assert wallet.fetch_activity_page("0xabc", 0) == rows


# ---------- the walk's early stop may crash on neither shape ----------

@pytest.mark.parametrize("tail", [{"timestamp": None}, {}, {"timestamp": "x"}])
def test_an_unreadable_tail_timestamp_walks_further_instead_of_raising(monkeypatch, tail):
    """+inf, not 0: this value decides whether to STOP paginating, so
    "can't tell" must cost one request and can never shorten the ledger."""
    page1 = [{"timestamp": 3_000_000, "transactionHash": f"0x{i}"}
             for i in range(wallet.PAGE_SIZE - 1)] + [dict(tail, transactionHash="0xtail")]
    page2 = [{"timestamp": 1_000_000, "transactionHash": "0xlast"}]
    pages = iter([page1, page2])
    monkeypatch.setattr(wallet.requests, "get",
                        lambda *a, **k: _Resp(next(pages)))
    rows = wallet.fetch_wallet_activity("0xabc", floor=2_000_000)
    assert len(rows) == wallet.PAGE_SIZE + 1   # it kept walking


def test_a_readable_tail_timestamp_still_stops_the_walk(monkeypatch):
    page1 = [{"timestamp": 3_000_000 - i, "transactionHash": f"0x{i}"}
             for i in range(wallet.PAGE_SIZE)]
    calls = []

    def fake_get(url, params, headers, timeout):
        calls.append(params["offset"])
        return _Resp(page1)

    monkeypatch.setattr(wallet.requests, "get", fake_get)
    wallet.fetch_wallet_activity("0xabc", floor=3_500_000)  # whole page predates floor
    assert calls == [0]


# ---------- a null slug is a slug we can't use, not a crash ----------

def test_window_start_of_a_non_string_is_zero_not_attribute_error():
    # `.get("slug", "")` returns None for an explicit `"slug": null` — the
    # default only covers a MISSING key. rsplit on None is an AttributeError,
    # which the handler's (IndexError, ValueError) never caught.
    assert updown_slugs.window_start(None) == 0.0
    assert updown_slugs.window_start(123) == 0.0
    assert updown_slugs.window_start({}) == 0.0
    assert updown_slugs.window_start("btc-updown-5m-1787419200") == 1787419200.0


def test_score_activity_survives_a_fire_record_with_no_usable_slug():
    """All-time (floor=0) is the DEFAULT for both `stats` and `watch`, and it
    is what made this reachable: a slug-less fire ranged as start 0.0, which
    0.0 accepts, and then indexed r["slug"]."""
    rows = [{"type": "TRADE", "side": "BUY", "slug": "btc-updown-5m-1787419200",
             "usdcSize": 10.0, "size": 20.0, "outcome": "Up", "timestamp": 1787419210},
            {"type": "REDEEM", "slug": "btc-updown-5m-1787419200",
             "usdcSize": 20.0, "size": 20.0, "outcome": "Up", "timestamp": 1787419500}]
    bad_fires = [{"ev": tape.EV_FIRE, "t": 1787419210, "slug": None},
                 {"ev": tape.EV_FIRE, "t": 1787419210},          # no key at all
                 {"ev": tape.EV_FIRE, "t": 1787419210, "slug": 7}]
    sb = ccs.score_activity(rows, 0.0, tape_records=bad_fires)
    assert sb["wins"] == 1 and sb["losses"] == 0


def test_score_activity_skips_a_fire_missing_its_fair_or_side():
    """An older tape generation reaches the calibration fold without the two
    fields a bucket needs. It used to KeyError out, taking every OTHER
    window's grade with it."""
    slug = "btc-updown-5m-1787419200"
    rows = [{"type": "TRADE", "side": "BUY", "slug": slug, "usdcSize": 10.0,
             "size": 20.0, "outcome": "Up", "timestamp": 1787419210},
            {"type": "REDEEM", "slug": slug, "usdcSize": 20.0, "size": 20.0,
             "outcome": "Up", "timestamp": 1787419500}]
    fires = [{"ev": tape.EV_FIRE, "t": 1787419210, "slug": slug, "side": "up"},
             {"ev": tape.EV_FIRE, "t": 1787419210, "slug": slug, "fair": None,
              "side": "up"},
             {"ev": tape.EV_FIRE, "t": 1787419210, "slug": slug, "fair": 0.61}]
    sb = ccs.score_activity(rows, 0.0, tape_records=fires)
    assert sb["wins"] == 1
    assert sb["cal"] == {}                      # nothing gradeable, nothing invented
    assert errlog.counts()[("cli_crypto_stats.score_activity.calibration",
                            "ValueError")]["n"] == 3


def test_score_activity_survives_a_row_with_no_type():
    slug = "btc-updown-5m-1787419200"
    rows = [{"slug": slug, "usdcSize": 5.0, "timestamp": 1787419210},   # no "type"
            {"type": "TRADE", "side": "BUY", "slug": slug, "usdcSize": 10.0,
             "size": 20.0, "outcome": "Up", "timestamp": 1787419210},
            {"type": "REDEEM", "slug": slug, "usdcSize": 20.0, "size": 20.0,
             "outcome": "Up", "timestamp": 1787419500}]
    assert ccs.score_activity(rows, 0.0, tape_records=[])["wins"] == 1


# ---------- the tape's range comparison ----------

def test_record_floor_t_never_compares_against_a_non_number():
    assert tape.record_floor_t({"t": 5}) == 5.0
    assert tape.record_floor_t({"t": None}) == 0.0
    assert tape.record_floor_t({"t": "5"}) == 0.0
    assert tape.record_floor_t({"t": True}) == 0.0
    assert tape.record_floor_t({}) == 0.0


def test_iter_records_with_a_floor_survives_a_null_timestamp(tmp_path):
    """This comparison runs over every record of a 49MB tape on every
    `pmt crypto stats`, from a call site OUTSIDE the report's belt."""
    p = tmp_path / "tape.jsonl"
    p.write_text("\n".join([
        json.dumps({"t": None, "ev": "eval"}),
        json.dumps({"t": 100, "ev": "fire"}),
    ]) + "\n")
    assert [r["ev"] for r in tape.iter_records(str(p), floor=50)] == ["fire"]


def test_iter_records_marks_a_mid_file_corrupt_line_but_not_a_torn_tail(tmp_path):
    good = json.dumps({"t": 1, "ev": "fire"})
    p = tmp_path / "tail.jsonl"
    p.write_text(good + "\n" + '{"t": 2, "ev": "tor')
    list(tape.iter_records(str(p)))
    assert ("tape.iter_records.corrupt_line", "ValueError") not in errlog.counts()

    p2 = tmp_path / "mid.jsonl"
    p2.write_text('{"t": 2, "ev": "tor' + "\n" + good + "\n")
    list(tape.iter_records(str(p2)))
    assert ("tape.iter_records.corrupt_line", "ValueError") in errlog.counts()


# ---------- a series in the history but not on this box ----------

def test_a_history_only_series_renders_a_dash_feed_instead_of_raising():
    """xrp migrated off this box on 2026-08-23: it is all over the wallet
    history and absent from every live arm. The by-symbol table joins the two
    and must not require the second."""
    series = {"xrp 5m": {"w": 3, "l": 2, "open": 0, "pnl": -4.5, "usd": 90.0,
                         "est": 0, "med": -1.0},
              "btc 5m": {"w": 9, "l": 1, "open": 1, "pnl": 12.0, "usd": 300.0,
                         "est": 1, "med": 1.5}}
    flags = {"btc 5m": {"feed": "rtds", "maker_bid": True}}   # no xrp arm
    t = stats_render.symbol_table(series, flags, breakeven=0.55)
    assert t.row_count == 2
    # and with NO live arms at all (engine down): still renders
    assert stats_render.symbol_table(series, None).row_count == 2
    assert stats_render.symbol_table(series, {}).row_count == 2


def test_window_rows_renders_history_only_windows_with_no_arms():
    sb = {"windows": [{"slug": "xrp-updown-5m-1787419200", "won": False,
                       "pnl": -9.5, "est": False, "end_ts": 1787419500,
                       "notional": 9.5, "shares": 20.0, "entry_px": 0.47,
                       "side": "up", "entry_ts": 1787419210,
                       "exit_ts": 1787419500}],
          "riding_windows": []}
    for arms in (None, {}, {"btc-updown-5m-1787419200": {"feed": "binance"}}):
        rows = watch_ui.window_rows(sb, arms)
        # The history-only window keeps its row whether or not an arm for its
        # series still exists on this box; a live arm only ADDS rows.
        assert "xrp-updown-5m-1787419200" in [r["slug"] for r in rows]
        watch_ui.build_windows_table(sb, 1787419600.0, arms=arms)  # renders


# ---------- the cold regime gauge ----------

def test_a_cold_gauge_drops_its_row_rather_than_painting_a_zero():
    """'we have not measured this' and 'the leader never holds' are opposite
    facts and must not share a rendering."""
    assert watch_ui.regime_row(None) is None
    assert watch_ui.regime_row({}) is None
    assert watch_ui.regime_row({"fleet_persist": None, "fleet_n": 40}) is None
    assert watch_ui.regime_row({"fleet_persist": 0.72, "fleet_n": 0}) is None
    assert watch_ui.regime_row({"fleet_persist": 0.72, "fleet_n": None}) is None
    assert watch_ui.regime_row("not a row") is None


def test_the_header_renders_with_a_cold_gauge_and_names_no_regime():
    snap = {"sb": dict(watch_ui._SB_EMPTY), "status": {}, "bal": {},
            "odds": {}, "regime": None, "sb_stale": False, "sb_fetched_at": None,
            "err": None}
    labels = [r[0] for r in watch_ui.header_rows(snap)]
    assert "regime" not in labels
    watch_ui.build_header_panel(snap, "all time", None)   # renders

    snap["regime"] = {"fleet_persist": 0.715, "fleet_n": 412,
                      "fleet_lo": 0.67, "fleet_hi": 0.76, "fleet_arrow": "↓",
                      "end": 1787419500}
    assert "regime" in [r[0] for r in watch_ui.header_rows(snap)]


# ---------- /status may be any JSON a 2xx can carry ----------

def test_stats_treats_a_non_object_status_the_way_watch_already_did(monkeypatch):
    """engine.post hands back whatever the body parsed to. watch guarded with
    isinstance; stats did not, so `status.get("arms")` was an AttributeError
    thrown from OUTSIDE the report's belt."""
    from click.testing import CliRunner

    monkeypatch.setenv("PM_FUNDER_ADDRESS", "0xabc")
    monkeypatch.setattr(ccs, "_fire_roll_records", lambda: [])
    monkeypatch.setattr(ccs, "_tape_scoreboard",
                        lambda *a, **k: dict(watch_ui._SB_EMPTY, activity=[]))
    monkeypatch.setattr(ccs, "_engine_post", lambda *a, **k: ["not", "a", "dict"])
    monkeypatch.setattr(ccs, "_api", lambda: (_ for _ in ()).throw(RuntimeError("no auth")))

    res = CliRunner(env={"COLUMNS": "200"}).invoke(ccs.crypto_stats, ["--json"])
    assert res.exit_code == 0, res.output
    assert json.loads(res.output)["arms"] == {}
    assert ("cli_crypto_stats.crypto_stats.status_shape", "TypeError") in errlog.counts()


# ---------- the watch worker's belt now leaves a mark ----------

def test_a_failing_watch_source_is_marked_with_its_site_and_kept_alive():
    """The `scoreboard: AttributeError` experience, with the missing half
    restored: which source, what message, and how many times."""
    import cli_crypto_watch as ccw

    state = ccw.WatchState()
    f = ccw.WatchFetcher(state, sliding_floor=0.0)

    def boom():
        raise AttributeError("'str' object has no attribute 'get'")

    f.fetch_sb = boom
    for name in ("status", "odds", "bal", "tape", "regime"):
        setattr(f, f"fetch_{name}", lambda: None)

    f.tick(0.0)
    f.tick(1e6)   # every cadence is due again

    assert state.read()["err"] == "scoreboard: AttributeError"   # unchanged UX
    mark = errlog.counts()[("watch.fetch_sb", "AttributeError")]
    assert mark["n"] == 2
    assert "no attribute" in mark["last_msg"]
