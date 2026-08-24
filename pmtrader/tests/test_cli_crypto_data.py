"""Tests for the read-only crypto reports — currently `crypto outcomes`, whose
folded-in oracle-corpus refresh is the part worth pinning.

(`pmt crypto oracle` used to be a manual prerequisite of this command; the
fetch now runs inside it, so grading can never silently read a stale corpus
because the operator forgot the first step.)
"""

from __future__ import annotations

import json
import time

import cli_crypto_data as cd


def _rig_outcomes(monkeypatch, tmp_path, *, refresh, corpus=None, redeem_outcome="up"):
    """Run `crypto outcomes` over a synthetic tape with the RPC + data-api stubbed.

    Returns (result, calls) where `calls` is the ordered log of refresh /
    corpus-load / grade so a test can pin that the fetch precedes the grade.
    """
    from click.testing import CliRunner

    from polymarket import chainlink as ck
    from polymarket import outcomes as oc

    now = int(time.time())
    start = now - 3600
    slug = f"btc-updown-5m-{start}"
    end = start + 300

    updown = tmp_path / "updown-tape.jsonl"
    updown.write_text(json.dumps({"ev": "eval", "slug": slug, "t": start + 10}) + "\n")
    book = tmp_path / "book-tape.jsonl"
    book.write_text("")
    monkeypatch.setattr(cd.tape, "UPDOWN_TAPE", str(updown))
    monkeypatch.setattr(cd.tape, "BOOK_TAPE", str(book))

    calls: list[str] = []

    def fake_refresh(symbols, since, now_):
        calls.append(f"refresh:{','.join(symbols)}")
        return {s: dict(refresh) for s in symbols}

    def fake_load_corpus(sym, since=None):
        calls.append(f"load:{sym}")
        return list(corpus or [])

    monkeypatch.setattr(ck, "refresh_corpus", fake_refresh)
    monkeypatch.setattr(ck, "load_corpus", fake_load_corpus)

    real_build = oc.build_outcomes

    def fake_build(*a, **kw):
        calls.append("grade")
        return real_build(*a, **kw)

    monkeypatch.setattr(oc, "build_outcomes", fake_build)
    monkeypatch.setattr(cd.wallet, "funder_address", lambda: "0xabc")
    monkeypatch.setattr(cd.wallet, "fetch_wallet_activity", lambda addr, floor: [
        {"type": "REDEEM", "usdcSize": 11.0, "outcome": redeem_outcome,
         "slug": slug, "timestamp": end + 30},
    ])

    out_file = tmp_path / "outcomes.jsonl"
    args = ["--out", str(out_file), "--since", str(start - 60)]
    result = CliRunner().invoke(cd.crypto_outcomes, args)
    return result, calls, out_file


_REFRESH_OK = {"new": 7, "hours": 3.0, "error": None}


def test_outcomes_refreshes_the_oracle_corpus_before_grading(monkeypatch, tmp_path):
    result, calls, out_file = _rig_outcomes(monkeypatch, tmp_path, refresh=_REFRESH_OK)

    assert result.exit_code == 0, result.output
    assert calls.index("refresh:btc") < calls.index("load:btc") < calls.index("grade"), calls
    assert "oracle corpus: BTC +7 rounds" in result.output
    assert out_file.exists(), "grading still ran and wrote the outcomes file"


def test_outcomes_degrades_to_the_existing_corpus_when_the_fetch_fails(monkeypatch, tmp_path):
    # A dead Polygon RPC must not cost us the wallet-graded windows in the same
    # run — it warns, then grades off whatever corpus is already on disk.
    failed = {"new": 0, "hours": 0.0, "error": "all RPC urls failed"}
    result, calls, out_file = _rig_outcomes(monkeypatch, tmp_path, refresh=failed)

    assert result.exit_code == 0, result.output
    assert "oracle refresh failed" in result.output
    assert "all RPC urls failed" in result.output
    assert "oracle corpus:" not in result.output, "nothing was fetched, don't claim it was"
    assert "grade" in calls and calls.index("load:btc") < calls.index("grade")
    assert json.loads(out_file.read_text().strip())["source"] == "wallet"


def test_outcomes_fetch_only_stops_before_grading(monkeypatch, tmp_path):
    from click.testing import CliRunner

    from polymarket import chainlink as ck

    now = int(time.time())
    start = now - 3600
    updown = tmp_path / "updown-tape.jsonl"
    updown.write_text(json.dumps({"ev": "eval", "slug": f"btc-updown-5m-{start}",
                                   "t": start + 10}) + "\n")
    monkeypatch.setattr(cd.tape, "UPDOWN_TAPE", str(updown))
    monkeypatch.setattr(cd.tape, "BOOK_TAPE", str(tmp_path / "missing.jsonl"))

    refreshed: list[list[str]] = []

    def fake_refresh(symbols, since, now_):
        refreshed.append(list(symbols))
        return {s: dict(_REFRESH_OK) for s in symbols}

    monkeypatch.setattr(ck, "refresh_corpus", fake_refresh)

    def boom(*a, **kw):  # the cron form needs no wallet and must not call the data-api
        raise AssertionError("fetch-only must not touch the wallet")

    monkeypatch.setattr(cd.wallet, "funder_address", boom)
    monkeypatch.setattr(cd.wallet, "fetch_wallet_activity", boom)

    out_file = tmp_path / "outcomes.jsonl"
    result = CliRunner().invoke(cd.crypto_outcomes,
                                 ["--out", str(out_file), "--fetch-only"])

    assert result.exit_code == 0, result.output
    assert refreshed == [["btc"]]
    assert not out_file.exists(), "fetch-only never rewrites the outcomes corpus"


# ---------- `pmt crypto regime`: the output shape and what it refuses to claim ----------
#
# The gauge itself is tested in test_regime.py; these pin the COMMAND — that it
# prints the fleet figure, the per-series split, the definition it used, the
# grading coverage behind it, and the fact that it gates nothing. A report that
# quietly drops the coverage caveat is a report that reads as a trading signal.

def _rig_regime(monkeypatch, tmp_path, windows, args=()):
    """Run `crypto regime` over a synthetic book tape + outcomes corpus.

    ~/.pmt is never touched: the tapes are tmp files and `--out` is a tmp
    path, which is also how the command must behave for anyone auditing what
    it writes."""
    from click.testing import CliRunner

    from polymarket import regime as rg
    from tests.test_regime import tape_files

    book, out = tape_files(tmp_path, windows)
    monkeypatch.setattr(rg, "book_tape_sources", lambda *a, **k: [book])
    monkeypatch.setattr(rg, "OUTCOMES_PATH", out)
    dest = tmp_path / "regime.jsonl"
    result = CliRunner().invoke(cd.crypto_regime,
                                ["--out", str(dest), *args])
    return result, dest


def _regime_windows(n_hit, n_miss, sym="btc", t0=1_700_000_000):
    from tests.test_regime import _mixed

    return _mixed(n_hit, n_miss, sym=sym, t0=t0)


def test_regime_prints_the_fleet_gauge_its_interval_and_its_scope(monkeypatch, tmp_path):
    result, dest = _rig_regime(monkeypatch, tmp_path, _regime_windows(8, 2),
                               args=["--trail", "10"])
    assert result.exit_code == 0, result.output
    out = " ".join(result.output.split())          # the report wraps; shape doesn't
    assert "leader persistence" in out
    assert "FLEET 80.0%" in out
    assert "8/10 windows · trailing 10" in out
    assert "btc 5m" in out                          # the per-series split


def test_regime_names_the_method_it_measured_with(monkeypatch, tmp_path):
    """The definition is frozen in regime.METHOD, and the report quotes it —
    a number whose method is not on the page cannot be joined to anything."""
    from polymarket import regime as rg

    result, _dest = _rig_regime(monkeypatch, tmp_path, _regime_windows(5, 0))
    assert rg.METHOD in " ".join(result.output.split())


def test_regime_says_out_loud_that_it_gates_nothing(monkeypatch, tmp_path):
    result, _dest = _rig_regime(monkeypatch, tmp_path, _regime_windows(5, 0))
    out = " ".join(result.output.split())
    assert "MEASUREMENT ONLY" in out and "docs/regime-gauge.md" in out


def test_regime_reports_the_windows_it_had_to_drop(monkeypatch, tmp_path):
    from tests.test_regime import window

    ws = (_regime_windows(3, 0)
          + [window("btc", 1_700_100_000, 0.52, "up")]                   # no lead
          + [window("btc", 1_700_200_000, 0.70, "up", up_age=9_000.0)])  # stale
    result, _dest = _rig_regime(monkeypatch, tmp_path, ws)
    out = " ".join(result.output.split())
    assert "3/5 graded windows had a leader" in out
    assert "1 stale" in out and "1 no-lead" in out


def test_regime_warns_when_grading_covers_little_of_its_own_span(monkeypatch, tmp_path):
    """The corpus lags the tape, and TRADED windows grade first — a selection
    on the very axis being measured. That caveat is the headline's qualifier,
    not a footnote, so it is yellow and it names the fix."""
    from tests.test_regime import tape_files, window

    ws = _regime_windows(4, 0)
    book, out_path = tape_files(tmp_path, ws)
    with open(book, "a") as fh:
        for i in range(40):                       # marked, but nothing graded them
            s, rows, _o = window("eth", 1_700_000_000 + i * 300, 0.70, "up")
            for r in rows:
                fh.write(json.dumps(r) + "\n")

    from polymarket import regime as rg
    monkeypatch.setattr(rg, "book_tape_sources", lambda *a, **k: [book])
    monkeypatch.setattr(rg, "OUTCOMES_PATH", out_path)
    from click.testing import CliRunner
    result = CliRunner().invoke(cd.crypto_regime,
                                ["--out", str(tmp_path / "r.jsonl"), "--dry-run"])
    joined = " ".join(result.output.split())
    assert "await a grade" in joined
    assert "traded windows grade FIRST" in joined
    assert "pmt crypto outcomes" in joined


def test_regime_appends_one_row_per_window_and_repeats_nothing(monkeypatch, tmp_path):
    result, dest = _rig_regime(monkeypatch, tmp_path, _regime_windows(4, 1))
    assert result.exit_code == 0, result.output
    assert "5 row(s)" in " ".join(result.output.split())
    rows = [json.loads(ln) for ln in dest.read_text().splitlines() if ln.strip()]
    assert len(rows) == 5
    assert {r["slug"] for r in rows} == {r["slug"] for r in rows}
    # A second run says nothing new rather than doubling the file.
    again, _ = _rig_regime(monkeypatch, tmp_path, _regime_windows(4, 1))
    assert "already current" in " ".join(again.output.split())
    assert len(dest.read_text().splitlines()) == 5


def test_regime_dry_run_writes_nothing(monkeypatch, tmp_path):
    result, dest = _rig_regime(monkeypatch, tmp_path, _regime_windows(4, 1),
                               args=["--dry-run"])
    assert result.exit_code == 0, result.output
    assert "dry run" in " ".join(result.output.split())
    assert not dest.exists()


def test_regime_json_is_machine_readable_and_carries_no_prose(monkeypatch, tmp_path):
    result, _dest = _rig_regime(monkeypatch, tmp_path, _regime_windows(6, 4),
                                args=["--json", "--trail", "10"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["fleet"]["persist"] == 0.6
    assert payload["fleet"]["n"] == 10
    assert payload["method"]
    assert "obs" not in payload, "the per-window rows belong in the JSONL, not here"


def test_regime_refuses_a_nonsense_trail(monkeypatch, tmp_path):
    result, _dest = _rig_regime(monkeypatch, tmp_path, _regime_windows(2, 0),
                                args=["--trail", "0"])
    assert result.exit_code != 0
    assert "--trail must be positive" in result.output


def test_regime_on_a_cold_box_reports_rather_than_crashes(monkeypatch, tmp_path):
    """No tape, no outcomes, no corpus file — the command still prints a
    gauge that says it has nothing, which is what a cold start looks like."""
    result, dest = _rig_regime(monkeypatch, tmp_path, [])
    assert result.exit_code == 0, result.output
    out = " ".join(result.output.split())
    assert "FLEET —" in out
    assert not dest.exists() or dest.read_text() == ""


def test_regime_prints_the_grade_split_when_the_two_populations_disagree(
        monkeypatch, tmp_path):
    """Wallet rows grade FIRST, so the most recent slice of the gauge is its
    most selected slice. A headline quoted without that split is the engine's
    own entry filter read back as a fact about the market."""
    from tests.test_regime import _mixed

    ws = [(s, r, {**o, "source": "wallet"})
          for s, r, o in _mixed(20, 0)]
    ws += [(s, r, {**o, "source": "resolution"})
           for s, r, o in _mixed(10, 10, sym="eth", t0=1_700_100_000)]
    result, _dest = _rig_regime(monkeypatch, tmp_path, ws)
    out = " ".join(result.output.split())
    assert "by grade · wallet 100.0% (20) vs resolution 50.0% (20)" in out
    assert "we TRADED that window" in out


def test_regime_stays_quiet_about_the_split_with_one_population(monkeypatch, tmp_path):
    result, _dest = _rig_regime(monkeypatch, tmp_path, _regime_windows(5, 0))
    assert "by grade" not in result.output
