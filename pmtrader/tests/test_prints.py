"""Print recorder — window discovery, rotation, bounds, and the dedupe seam.

The bug this module exists to prevent is not a crash: it is a corpus that
looks fine and shares no instant with the RTDS corpus, which is what made
print-vs-stream lead unmeasurable in the first place. Most of these tests are
therefore about WHICH windows get picked up and WHEN.
"""

from __future__ import annotations

import json
import time

import pytest

from polymarket.prints import (
    DailyWriter,
    book_windows,
    daily_path,
    due_windows,
    harvested_slugs,
    prune,
    slim,
)

DAY = 86400
# 2026-08-23T00:00:00Z
T0 = 1787443200


def _book_tape(tmp_path, slugs, t=None):
    p = tmp_path / "book-tape.jsonl"
    with p.open("w") as f:
        for i, s in enumerate(slugs):
            f.write(json.dumps({"ev": "book", "slug": s,
                                "t": (t if t is not None else T0) + i}) + "\n")
    return str(p)


# ---------- window discovery ----------

def test_book_windows_parses_updown_slugs_and_skips_the_rest(tmp_path):
    tape = _book_tape(tmp_path, ["btc-updown-5m-1787505300", "not-a-window",
                                 "eth-updown-15m-1787505300"])
    w = book_windows(tape)
    assert set(w) == {"btc-updown-5m-1787505300", "eth-updown-15m-1787505300"}
    assert w["btc-updown-5m-1787505300"]["end"] == 1787505300 + 300
    assert w["eth-updown-15m-1787505300"]["end"] == 1787505300 + 900


def test_book_windows_dedupes_a_window_that_appears_on_many_records(tmp_path):
    """The book tape writes a record per tick; one window is thousands of rows."""
    tape = _book_tape(tmp_path, ["btc-updown-5m-1787505300"] * 50)
    assert len(book_windows(tape)) == 1


def test_due_windows_waits_for_the_settle_lag():
    """Harvesting at the bell would miss the prints still landing. The whole
    design leans on data-api serving full history after the fact, so waiting
    costs nothing and buys completeness."""
    w = {"btc-updown-5m-1787505300": {"start": 1787505300, "end": 1787505600}}
    end = 1787505600
    assert due_windows(w, set(), end + 10, 180) == []
    assert due_windows(w, set(), end + 180, 180) == ["btc-updown-5m-1787505300"]


def test_due_windows_skips_what_is_already_harvested():
    w = {"a-updown-5m-100": {"start": 100, "end": 400},
         "b-updown-5m-700": {"start": 700, "end": 1000}}
    due = due_windows(w, {"a-updown-5m-100"}, 99999, 0)
    assert due == ["b-updown-5m-700"]


def test_due_windows_is_oldest_first():
    """An interrupted catch-up resumes in time order, so the corpus stays
    roughly append-ordered instead of interleaving days."""
    w = {"b-updown-5m-700": {"start": 700, "end": 1000},
         "a-updown-5m-100": {"start": 100, "end": 400}}
    assert due_windows(w, set(), 99999, 0) == ["a-updown-5m-100", "b-updown-5m-700"]


# ---------- rotation ----------

def test_daily_path_rotates_on_the_utc_day():
    assert daily_path(T0).name == "prints-20260823.jsonl"
    assert daily_path(T0 + DAY).name == "prints-20260824.jsonl"
    # 23:59:59Z is still the same day; one second later is not.
    assert daily_path(T0 + DAY - 1).name == "prints-20260823.jsonl"


def test_writer_files_a_print_under_the_day_it_HAPPENED(tmp_path):
    """Rotating on print time, not harvest time, is what makes a day's prints
    one file that joins to rtds-YYYYMMDD.jsonl without a filter."""
    w = DailyWriter(tmp_path)
    w.write({"slug": "s", "t": T0 + 10})
    w.write({"slug": "s", "t": T0 + DAY + 10})
    w.close()
    assert (tmp_path / "prints-20260823.jsonl").exists()
    assert (tmp_path / "prints-20260824.jsonl").exists()


def test_writer_survives_a_print_with_no_timestamp(tmp_path):
    """Useless for a lead study, still evidence — filed under today, not dropped."""
    w = DailyWriter(tmp_path)
    w.write({"slug": "s", "t": None})
    w.close()
    written = list(tmp_path.glob("prints-*.jsonl"))
    assert len(written) == 1
    assert json.loads(written[0].read_text())["slug"] == "s"


# ---------- bounds ----------

def test_prune_drops_days_past_retention_and_keeps_the_rest(tmp_path):
    for day in ("20260801", "20260820", "20260823"):
        (tmp_path / f"prints-{day}.jsonl").write_text("{}\n")
    removed = prune(tmp_path, retention_days=7, now=T0)
    assert removed == ["prints-20260801.jsonl"]
    assert {p.name for p in tmp_path.glob("prints-*.jsonl")} == {
        "prints-20260820.jsonl", "prints-20260823.jsonl"}


def test_prune_is_disabled_at_zero(tmp_path):
    (tmp_path / "prints-20200101.jsonl").write_text("{}\n")
    assert prune(tmp_path, retention_days=0, now=T0) == []
    assert (tmp_path / "prints-20200101.jsonl").exists()


def test_prune_ignores_files_it_does_not_own(tmp_path):
    (tmp_path / "recorder.log").write_text("x\n")
    (tmp_path / "prints-notaday.jsonl").write_text("{}\n")
    prune(tmp_path, retention_days=1, now=T0)
    assert (tmp_path / "recorder.log").exists()
    assert (tmp_path / "prints-notaday.jsonl").exists()


# ---------- resume ----------

def test_harvested_slugs_reads_the_daily_corpus_and_the_legacy_backfill(tmp_path):
    """Seeding from the 54 MB one-shot backfill is what stops the recorder
    re-fetching 167k prints it already has."""
    (tmp_path / "prints-20260823.jsonl").write_text(
        json.dumps({"slug": "a-updown-5m-1"}) + "\n")
    legacy = tmp_path / "legacy.jsonl"
    legacy.write_text(json.dumps({"slug": "b-updown-5m-2"}) + "\n")
    assert harvested_slugs(tmp_path, legacy) == {"a-updown-5m-1", "b-updown-5m-2"}


def test_harvested_slugs_tolerates_a_torn_tail_line(tmp_path):
    """Line-buffered, never fsynced — a poweroff mid-write costs the tail line
    and must not cost the whole resume set."""
    (tmp_path / "prints-20260823.jsonl").write_text(
        json.dumps({"slug": "a-updown-5m-1"}) + "\n" + '{"slug": "b-upd')
    assert harvested_slugs(tmp_path, None) == {"a-updown-5m-1"}


def test_harvested_slugs_is_empty_when_nothing_exists(tmp_path):
    assert harvested_slugs(tmp_path, tmp_path / "nope.jsonl") == set()


# ---------- payload ----------

def test_slim_matches_the_backfill_field_for_field():
    """The daily corpus and the legacy prints.jsonl must concatenate with no
    shim, or every consumer needs two readers."""
    raw = {"timestamp": 123, "asset": "0xabc", "outcome": "Up", "side": "BUY",
           "size": 10, "price": 0.55, "proxyWallet": "0xdead",
           "transactionHash": "0xbeef", "profile": {"huge": "blob"}}
    assert slim(raw, "btc-updown-5m-1") == {
        "slug": "btc-updown-5m-1", "t": 123, "asset": "0xabc", "outcome": "up",
        "side": "buy", "size": 10, "price": 0.55, "wallet": "0xdead", "tx": "0xbeef"}


@pytest.mark.parametrize("missing", ["outcome", "side"])
def test_slim_lowercases_without_tripping_on_a_null(missing):
    raw = {"timestamp": 1, "outcome": "Up", "side": "BUY"}
    raw[missing] = None
    assert slim(raw, "s")[missing] == ""
