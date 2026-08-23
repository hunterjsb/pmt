"""Tests for the shared decision-tape reader — in particular the watch-crash
lesson: a truncated/corrupt line must never raise, just be skipped.
"""

from __future__ import annotations

import json

from polymarket import tape


def _write(path, lines):
    path.write_text("\n".join(lines) + "\n")


def test_iter_records_yields_parsed_dicts(tmp_path):
    p = tmp_path / "tape.jsonl"
    _write(p, [json.dumps({"t": 1, "ev": "eval", "slug": "btc-updown-5m-1"}),
               json.dumps({"t": 2, "ev": "fire", "slug": "btc-updown-5m-1"})])
    records = list(tape.iter_records(str(p)))
    assert [r["ev"] for r in records] == ["eval", "fire"]


def test_iter_records_skips_corrupt_and_blank_lines(tmp_path):
    p = tmp_path / "tape.jsonl"
    good = json.dumps({"t": 1, "ev": "fire"})
    _write(p, [good, "", "  ", '{"t": 2, "ev": "trunc', "[1, 2, 3]", good])
    records = list(tape.iter_records(str(p)))
    # both valid dict lines survive; blank, truncated-JSON, and non-dict JSON are skipped
    assert len(records) == 2
    assert all(r["ev"] == "fire" for r in records)


def test_iter_records_missing_file_yields_nothing():
    assert list(tape.iter_records("/no/such/path/updown-tape.jsonl")) == []


def test_iter_records_filters_by_floor(tmp_path):
    p = tmp_path / "tape.jsonl"
    _write(p, [json.dumps({"t": 10, "ev": "eval"}),
               json.dumps({"t": 20, "ev": "eval"}),
               json.dumps({"t": 30, "ev": "eval"})])
    records = list(tape.iter_records(str(p), floor=20))
    assert [r["t"] for r in records] == [20, 30]


def test_iter_records_filters_by_evs(tmp_path):
    p = tmp_path / "tape.jsonl"
    _write(p, [json.dumps({"t": 1, "ev": "eval"}),
               json.dumps({"t": 2, "ev": "fire"}),
               json.dumps({"t": 3, "ev": "gated"})])
    records = list(tape.iter_records(str(p), evs={tape.EV_FIRE, tape.EV_GATED}))
    assert [r["ev"] for r in records] == ["fire", "gated"]


def test_iter_records_missing_t_treated_as_zero_for_floor(tmp_path):
    p = tmp_path / "tape.jsonl"
    _write(p, [json.dumps({"ev": "eval"})])  # no "t" key
    assert list(tape.iter_records(str(p), floor=1)) == []
    assert len(list(tape.iter_records(str(p), floor=0))) == 1
