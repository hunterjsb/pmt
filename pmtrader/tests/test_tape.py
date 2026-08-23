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


# ---------- freshness: is this file still being written? ----------
#
# The watch dashboard reads the tape straight off ~/.pmt/engine — which is the
# ENGINE's disk, not the operator's, once PMENGINE_CONTROL_URL points down a
# tunnel. `newest_t` is how it tells the two apart, so it has to be both cheap
# and unbothered by the torn last line an append-only file always risks.

def test_record_t_reads_a_timestamp(tmp_path):
    assert tape.record_t(json.dumps({"t": 12.5, "ev": "fire"})) == 12.5
    assert tape.record_t(b'{"t": 3, "ev": "eval"}') == 3.0


def test_record_t_is_none_for_anything_that_is_not_a_timestamped_record():
    assert tape.record_t('{"t":1,"ev":"fi') is None          # torn mid-write
    assert tape.record_t("") is None
    assert tape.record_t("   ") is None
    assert tape.record_t('["not","an","object"]') is None
    assert tape.record_t('{"ev":"eval"}') is None            # no t
    assert tape.record_t('{"t":"soon"}') is None             # t isn't a number
    assert tape.record_t('{"t":true}') is None               # ...and a bool isn't either


def test_newest_t_is_the_last_record_not_the_first(tmp_path):
    p = tmp_path / "tape.jsonl"
    _write(p, [json.dumps({"t": t, "ev": "eval"}) for t in (10.0, 20.0, 30.0)])
    assert tape.newest_t(str(p)) == 30.0


def test_newest_t_skips_back_over_a_torn_final_append(tmp_path):
    # The engine was killed mid-writeln. The record before it is still the
    # freshest thing anyone knows, and the file is emphatically not "dead".
    p = tmp_path / "tape.jsonl"
    p.write_text(json.dumps({"t": 40.0, "ev": "eval"}) + '\n{"t":41.0,"ev":"fi')
    assert tape.newest_t(str(p)) == 40.0


def test_newest_t_is_none_when_there_is_nothing_to_read(tmp_path):
    assert tape.newest_t(str(tmp_path / "no-such-file.jsonl")) is None
    empty = tmp_path / "empty.jsonl"
    empty.write_text("")
    assert tape.newest_t(str(empty)) is None


def test_newest_t_only_reads_the_tail(tmp_path):
    """Flat in file size — the live tape passed 24MB in August 2026 and this
    runs on the dashboard's fetch cadence."""
    p = tmp_path / "big.jsonl"
    with open(p, "w") as fh:
        for i in range(20_000):
            fh.write(json.dumps({"t": float(i), "ev": "eval",
                                 "pad": "x" * 200}) + "\n")
    assert p.stat().st_size > 4_000_000
    # A tail window far smaller than the file still finds the newest record,
    # and the partial line it opens inside is skipped rather than parsed.
    assert tape.newest_t(str(p), tail_bytes=4096) == 19_999.0
