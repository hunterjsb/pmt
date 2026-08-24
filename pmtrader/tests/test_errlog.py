"""Tests for the swallowed-error surfacing helper (polymarket/errlog.py) and
the `pmt crypto errors` report over it.

The three properties that matter, in order:

  1. NEVER RAISES. note() is called from inside exception handlers whose whole
     job is to keep something alive. A read-only ~/.pmt must degrade to "the
     mark was not written", never to a second exception thrown out of the
     recovery path.
  2. RATE LIMITED. A failure inside a 10s fetch loop fires thousands of times a
     day; the first one keeps a traceback and the rest are counted, so the log
     stays readable and a storm still escalates visibly.
  3. THE COUNT SURVIVES. `n` is what separates "blinked once" from "has been
     dead for an hour" — the distinction the watch header's four-word
     `scoreboard: AttributeError` could never make.
"""

from __future__ import annotations

import json
import os
import stat

import pytest

from polymarket import errlog


def _boom(msg="kaboom", cls=ValueError):
    """A raised-and-caught exception, so it carries a real __traceback__."""
    try:
        raise cls(msg)
    except cls as e:
        return e


def _lines(path):
    return [json.loads(x) for x in path.read_text().splitlines() if x.strip()]


# ---------- the mark itself ----------

def test_first_occurrence_writes_a_full_record(tmp_path, monkeypatch):
    p = tmp_path / "errs.jsonl"
    monkeypatch.setenv("PMT_ERRLOG_PATH", str(p))
    rec = errlog.note("mod.fn", _boom("bad row"), slug="btc-updown-5m-1", offset=400)

    assert rec is not None and rec["kind"] == "first"
    (row,) = _lines(p)
    assert row["site"] == "mod.fn"
    assert row["exc"] == "ValueError"
    assert row["msg"] == "bad row"
    assert row["n"] == 1
    assert row["ctx"] == {"slug": "btc-updown-5m-1", "offset": 400}
    assert "t" in row and "first_t" in row and "pid" in row
    # The frames are the whole point of the first one: by the time anyone reads
    # the file the stack is long gone.
    assert "ValueError: bad row" in row["traceback"]


def test_first_occurrence_prints_a_traceback_to_stderr(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("PMT_ERRLOG_PATH", str(tmp_path / "errs.jsonl"))
    monkeypatch.setenv("PMT_ERRLOG_STDERR", "1")
    errlog.note("mod.fn", _boom("loud"))
    err = capsys.readouterr().err
    assert "mod.fn" in err and "ValueError: loud" in err


def test_stderr_can_be_muted_without_muting_the_file(tmp_path, monkeypatch, capsys):
    """`pmt crypto watch` owns the terminal — the marks still have to land."""
    p = tmp_path / "errs.jsonl"
    monkeypatch.setenv("PMT_ERRLOG_PATH", str(p))
    monkeypatch.setenv("PMT_ERRLOG_STDERR", "0")
    errlog.note("mod.fn", _boom())
    assert capsys.readouterr().err == ""
    assert len(_lines(p)) == 1


def test_context_values_that_are_not_json_survive_as_repr(tmp_path, monkeypatch):
    p = tmp_path / "errs.jsonl"
    monkeypatch.setenv("PMT_ERRLOG_PATH", str(p))

    class Weird:
        def __repr__(self):
            return "<Weird>"

    errlog.note("mod.fn", _boom(), obj=Weird(), path=tmp_path)
    (row,) = _lines(p)
    assert row["ctx"]["obj"] == "<Weird>"
    assert str(tmp_path) in row["ctx"]["path"]


def test_a_context_value_whose_repr_explodes_does_not_lose_the_mark(tmp_path, monkeypatch):
    p = tmp_path / "errs.jsonl"
    monkeypatch.setenv("PMT_ERRLOG_PATH", str(p))

    class Hostile:
        def __repr__(self):
            raise RuntimeError("no repr for you")

    errlog.note("mod.fn", _boom(), obj=Hostile())
    (row,) = _lines(p)
    assert row["ctx"]["obj"] == "<unreprable Hostile>"


# ---------- rate limiting ----------

def test_repeats_are_counted_and_written_on_a_power_of_two_schedule(tmp_path, monkeypatch):
    p = tmp_path / "errs.jsonl"
    monkeypatch.setenv("PMT_ERRLOG_PATH", str(p))
    for _ in range(20):
        errlog.note("hot.loop", _boom())

    rows = _lines(p)
    # 1, 2, 4, 8, 16 — five lines for twenty failures.
    assert [r["n"] for r in rows] == [1, 2, 4, 8, 16]
    assert rows[0]["kind"] == "first"
    assert all(r["kind"] == "repeat" for r in rows[1:])
    # Only the first keeps frames; a storm must not write 20 tracebacks.
    assert "traceback" in rows[0]
    assert all("traceback" not in r for r in rows[1:])
    # ...but the in-process tally knows the real number.
    assert errlog.counts()[("hot.loop", "ValueError")]["n"] == 20


def test_the_limiter_keys_on_site_AND_exception_type(tmp_path, monkeypatch):
    """Same site failing a NEW way is a new finding, not a repeat."""
    p = tmp_path / "errs.jsonl"
    monkeypatch.setenv("PMT_ERRLOG_PATH", str(p))
    errlog.note("mod.fn", _boom(cls=ValueError))
    errlog.note("mod.fn", _boom(cls=ValueError))
    errlog.note("mod.fn", _boom(cls=KeyError))
    errlog.note("other.fn", _boom(cls=ValueError))

    rows = _lines(p)
    assert {(r["site"], r["exc"], r["n"]) for r in rows} == {
        ("mod.fn", "ValueError", 1), ("mod.fn", "ValueError", 2),
        ("mod.fn", "KeyError", 1), ("other.fn", "ValueError", 1),
    }


def test_note_returns_none_for_a_counted_only_occurrence(tmp_path, monkeypatch):
    monkeypatch.setenv("PMT_ERRLOG_PATH", str(tmp_path / "errs.jsonl"))
    assert errlog.note("mod.fn", _boom()) is not None   # 1
    assert errlog.note("mod.fn", _boom()) is not None   # 2
    assert errlog.note("mod.fn", _boom()) is None       # 3


# ---------- it may never raise ----------

def test_an_unwritable_home_never_raises(tmp_path, monkeypatch):
    """The one hard rule. A read-only ~/.pmt loses the mark, not the caller."""
    ro = tmp_path / "readonly"
    ro.mkdir()
    ro.chmod(stat.S_IRUSR | stat.S_IXUSR)
    monkeypatch.setenv("PMT_ERRLOG_PATH", str(ro / "sub" / "errs.jsonl"))
    try:
        assert errlog.note("mod.fn", _boom()) is not None  # returned, not written
        assert not (ro / "sub").exists()
    finally:
        ro.chmod(stat.S_IRWXU)


def test_a_path_that_is_a_directory_never_raises(tmp_path, monkeypatch):
    monkeypatch.setenv("PMT_ERRLOG_PATH", str(tmp_path))  # a dir, not a file
    errlog.note("mod.fn", _boom())  # must not raise


def test_note_survives_an_exception_with_a_hostile_str(tmp_path, monkeypatch):
    p = tmp_path / "errs.jsonl"
    monkeypatch.setenv("PMT_ERRLOG_PATH", str(p))

    class Hostile(Exception):
        def __str__(self):
            raise RuntimeError("no str for you")

    try:
        raise Hostile()
    except Hostile as e:
        errlog.note("mod.fn", e)
    (row,) = _lines(p)
    assert row["exc"] == "Hostile" and row["msg"] == ""


def test_note_is_safe_from_a_thread(tmp_path, monkeypatch):
    import threading
    p = tmp_path / "errs.jsonl"
    monkeypatch.setenv("PMT_ERRLOG_PATH", str(p))
    threads = [threading.Thread(target=lambda: errlog.note("t.fn", _boom()))
               for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errlog.counts()[("t.fn", "ValueError")]["n"] == 8


# ---------- rotation ----------

def test_the_file_rotates_instead_of_growing_forever(tmp_path, monkeypatch):
    p = tmp_path / "errs.jsonl"
    monkeypatch.setenv("PMT_ERRLOG_PATH", str(p))
    monkeypatch.setattr(errlog, "MAX_BYTES", 200)
    for i in range(40):
        errlog.reset()  # force every one to be a "first", i.e. a big record
        errlog.note(f"site{i}", _boom("x" * 50))
    assert p.stat().st_size <= 200 + 6000  # one oversize record may straddle
    assert p.with_suffix(p.suffix + ".1").exists()


# ---------- reading it back ----------

def test_load_skips_corrupt_lines_and_honours_since(tmp_path):
    p = tmp_path / "errs.jsonl"
    p.write_text("\n".join([
        json.dumps({"t": 100, "site": "a", "exc": "ValueError", "n": 1}),
        "{not json",
        "",
        json.dumps({"t": 200, "site": "b", "exc": "KeyError", "n": 1}),
    ]) + "\n")
    assert [r["site"] for r in errlog.load(p)] == ["a", "b"]
    assert [r["site"] for r in errlog.load(p, since=150)] == ["b"]


def test_load_of_a_missing_file_is_empty_not_an_error(tmp_path):
    assert errlog.load(tmp_path / "nope.jsonl") == []


def test_aggregate_takes_the_high_water_count_not_the_line_count(tmp_path):
    """Repeats are written on a log2 schedule, so SUMMING rows would understate
    a storm by orders of magnitude — the one number an operator reads first."""
    recs = [
        {"t": 1, "site": "hot", "exc": "ValueError", "n": 1, "msg": "first",
         "traceback": "Traceback ...", "first_t": 1},
        {"t": 2, "site": "hot", "exc": "ValueError", "n": 2, "msg": "again", "first_t": 1},
        {"t": 9, "site": "hot", "exc": "ValueError", "n": 4096, "msg": "still", "first_t": 1},
        {"t": 5, "site": "cold", "exc": "KeyError", "n": 1, "msg": "once", "first_t": 5},
    ]
    rows = errlog.aggregate(recs)
    assert [r["site"] for r in rows] == ["hot", "cold"]   # worst first
    assert rows[0]["n"] == 4096
    assert rows[0]["marks"] == 3
    assert rows[0]["msg"] == "still"                     # newest message wins
    assert rows[0]["first_t"] == 1
    assert rows[0]["traceback"] == "Traceback ..."       # kept from the first


def test_aggregate_of_nothing_is_nothing():
    assert errlog.aggregate([]) == []


# ---------- path resolution ----------

def test_path_defaults_under_the_engine_dir(monkeypatch):
    monkeypatch.delenv("PMT_ERRLOG_PATH", raising=False)
    p = errlog.path()
    assert p.name == "swallowed-errors.jsonl"
    assert p.parent == errlog.ENGINE_DIR


def test_path_env_override_is_read_per_call(tmp_path, monkeypatch):
    monkeypatch.setenv("PMT_ERRLOG_PATH", str(tmp_path / "a.jsonl"))
    assert errlog.path().name == "a.jsonl"
    monkeypatch.setenv("PMT_ERRLOG_PATH", str(tmp_path / "b.jsonl"))
    assert errlog.path().name == "b.jsonl"


# ---------- `pmt crypto errors` ----------

@pytest.fixture()
def runner():
    from click.testing import CliRunner
    # A wide console: Rich drops whole columns to fit 80, and the count is the
    # first casualty. Real terminals are wider; the test should not assert
    # against a squeeze.
    return CliRunner(env={"COLUMNS": "200"})


def _seed(p, rows):
    p.write_text("\n".join(json.dumps(r) for r in rows) + "\n")


def test_errors_command_reports_no_marks_on_a_cold_box(runner, tmp_path):
    from cli_crypto_data import crypto_errors
    res = runner.invoke(crypto_errors, ["--path", str(tmp_path / "nope.jsonl")])
    assert res.exit_code == 0
    assert "no swallowed errors" in res.output
    # A clean file and a missing file are the same good news; say which.
    assert "no file yet" in res.output


def test_errors_command_aggregates_worst_first(runner, tmp_path):
    from cli_crypto_data import crypto_errors
    p = tmp_path / "errs.jsonl"
    _seed(p, [
        {"t": 10, "site": "watch.fetch_sb", "exc": "AttributeError", "n": 1,
         "msg": "'str' object has no attribute 'get'", "first_t": 10,
         "traceback": "Traceback (most recent call last): boom"},
        {"t": 20, "site": "watch.fetch_sb", "exc": "AttributeError", "n": 512,
         "msg": "'str' object has no attribute 'get'", "first_t": 10},
        {"t": 30, "site": "engine.fetch", "exc": "ConnectionError", "n": 1,
         "msg": "refused", "first_t": 30},
    ])
    res = runner.invoke(crypto_errors, ["--path", str(p)])
    assert res.exit_code == 0
    out = res.output
    assert out.index("watch.fetch_sb") < out.index("engine.fetch")
    assert "512" in out
    assert "AttributeError" in out
    # The traceback is opt-in; the table stays one line per finding.
    assert "Traceback" not in out
    res2 = runner.invoke(crypto_errors, ["--path", str(p), "--trace"])
    assert "Traceback" in res2.output


def test_errors_command_json_and_filters(runner, tmp_path):
    from cli_crypto_data import crypto_errors
    p = tmp_path / "errs.jsonl"
    _seed(p, [
        {"t": 10, "site": "watch.fetch_sb", "exc": "AttributeError", "n": 4, "first_t": 10},
        {"t": 30, "site": "engine.fetch", "exc": "ConnectionError", "n": 1, "first_t": 30},
    ])
    res = runner.invoke(crypto_errors, ["--path", str(p), "--json"])
    assert res.exit_code == 0
    rows = json.loads(res.output)
    assert [r["site"] for r in rows] == ["watch.fetch_sb", "engine.fetch"]

    res = runner.invoke(crypto_errors, ["--path", str(p), "--site", "engine", "--json"])
    assert [r["site"] for r in json.loads(res.output)] == ["engine.fetch"]


def test_errors_command_tail_is_chronological_not_aggregated(runner, tmp_path):
    from cli_crypto_data import crypto_errors
    p = tmp_path / "errs.jsonl"
    _seed(p, [
        {"t": 10, "site": "a.fn", "exc": "ValueError", "n": 1, "msg": "one", "first_t": 10},
        {"t": 20, "site": "b.fn", "exc": "KeyError", "n": 1, "msg": "two", "first_t": 20},
        {"t": 30, "site": "c.fn", "exc": "OSError", "n": 1, "msg": "three", "first_t": 30},
    ])
    res = runner.invoke(crypto_errors, ["--path", str(p), "--tail", "2"])
    assert res.exit_code == 0
    assert "a.fn" not in res.output
    assert res.output.index("b.fn") < res.output.index("c.fn")


def test_errors_command_reads_the_env_path_when_none_is_given(runner, tmp_path, monkeypatch):
    from cli_crypto_data import crypto_errors
    p = tmp_path / "errs.jsonl"
    _seed(p, [{"t": 10, "site": "a.fn", "exc": "ValueError", "n": 3, "first_t": 10}])
    monkeypatch.setenv("PMT_ERRLOG_PATH", str(p))
    res = runner.invoke(crypto_errors, [])
    assert res.exit_code == 0 and "a.fn" in res.output


def test_errors_is_registered_on_the_crypto_group():
    from cli_crypto import crypto_group
    assert "errors" in crypto_group.commands


def test_the_suite_never_writes_the_operators_real_errlog():
    """conftest's guard, asserted rather than assumed: a suite that salts the
    live log makes `pmt crypto errors` lie about production."""
    assert os.environ.get("PMT_ERRLOG_PATH")
    assert errlog.path() != errlog.ENGINE_DIR / errlog.ERRLOG_NAME
