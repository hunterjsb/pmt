"""Tests for scripts/pmt-backup.sh — what it would ship, and when it refuses.

The script is driven entirely through env (PMT_HOME, PMT_BACKUP_S3, PMT_AWS),
so a synthetic ~/.pmt and a stub `aws` are enough to pin both halves of the
contract without an AWS account, a network, or a byte of the operator's real
corpus. Nothing here uploads: the skip path returns before tar ever runs, and
every other assertion is against --dry-run.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "pmt-backup.sh"
DATE = "2026-08-23"

pytestmark = pytest.mark.skipif(shutil.which("bash") is None, reason="needs bash")


def _pmt_home(tmp_path: Path) -> Path:
    """A ~/.pmt shaped like the real one: a corpus with per-day rtds files and
    recorder noise, and an engine directory whose rotated logs dwarf the tapes.
    """
    home = tmp_path / ".pmt"
    corpus, rtds, engine = home / "corpus", home / "corpus" / "rtds", home / "engine"
    for d in (corpus, rtds, engine):
        d.mkdir(parents=True, exist_ok=True)

    (corpus / "activity.jsonl").write_text("{}\n")
    (corpus / "outcomes.jsonl").write_text("{}\n")
    (corpus / "klines-1m-BTCUSDT.jsonl").write_text("{}\n")
    (rtds / "rtds-20260822.jsonl").write_text("{}\n")
    (rtds / "rtds-20260823.jsonl").write_text("{}\n")
    (rtds / "recorder.log").write_text("noise\n")
    (rtds / "recorder.pid").write_text("123\n")

    (engine / "updown-tape.jsonl").write_text("{}\n")
    (engine / "book-tape.jsonl").write_text("{}\n")
    (engine / "arms-state.json").write_text("{}\n")
    (engine / "engine-20260823-030004.log").write_text("noise\n")
    (engine / "engine-20260823-023005.log.gz").write_text("noise\n")
    return home


def _stub_aws(tmp_path: Path, *, object_exists: bool) -> tuple[Path, Path]:
    """A fake `aws` that records its argv, optionally reports the object, and
    keeps whatever an upload hands it so the archive can be inspected."""
    log = tmp_path / "aws-calls.log"
    stub = tmp_path / "aws"
    listing = (f'2026-08-23 03:30:12   12345678 {DATE}.tar.zst\n'
               if object_exists else "")
    stub.write_text(
        "#!/bin/sh\n"
        f'printf "%s\\n" "$*" >> "{log}"\n'
        f'if [ "$2" = "ls" ]; then printf "{listing}"; exit 0; fi\n'
        f'if [ "$2" = "cp" ]; then cp "$3" "{tmp_path / "uploaded.tar.zst"}"; fi\n'
        "exit 0\n"
    )
    stub.chmod(0o755)
    return stub, log


def _run(tmp_path: Path, *args: str, object_exists: bool = False):
    home = _pmt_home(tmp_path)
    stub, log = _stub_aws(tmp_path, object_exists=object_exists)
    env = {**os.environ, "PMT_HOME": str(home), "PMT_AWS": str(stub),
           "PMT_BACKUP_S3": "s3://xanmc/pmt-backups",
           "PMT_BACKUP_TMPDIR": str(tmp_path)}
    proc = subprocess.run(["bash", str(SCRIPT), "--date", DATE, *args],
                          capture_output=True, text=True, env=env, timeout=60)
    return proc, log


def _members(stdout: str) -> list[str]:
    body = stdout.split("members", 1)[1].split("\n", 1)[1]
    return [ln.strip() for ln in body.splitlines() if ln.strip()]


# ---------- what goes in the archive ----------

def test_dry_run_lists_the_corpus_the_tapes_and_the_arm_state(tmp_path):
    proc, _ = _run(tmp_path, "--dry-run")
    assert proc.returncode == 0, proc.stderr
    members = _members(proc.stdout)
    assert "corpus/activity.jsonl" in members
    assert "corpus/klines-1m-BTCUSDT.jsonl" in members
    assert "engine/updown-tape.jsonl" in members
    assert "engine/book-tape.jsonl" in members
    assert "engine/arms-state.json" in members


def test_the_rtds_corpus_goes_in_as_the_per_day_files_it_already_is(tmp_path):
    # Granular restores are the whole reason: one dark day back, not the
    # entire stream.
    members = _members(_run(tmp_path, "--dry-run")[0].stdout)
    assert "corpus/rtds/rtds-20260822.jsonl" in members
    assert "corpus/rtds/rtds-20260823.jsonl" in members


def test_logs_and_pidfiles_are_left_behind(tmp_path):
    members = _members(_run(tmp_path, "--dry-run")[0].stdout)
    assert not [m for m in members if m.endswith((".log", ".log.gz", ".pid"))]


def test_the_rotated_engine_logs_are_not_shipped_nightly(tmp_path):
    members = _members(_run(tmp_path, "--dry-run")[0].stdout)
    assert not [m for m in members if m.startswith("engine/engine-")]


def test_the_member_list_is_paths_relative_to_pmt_home(tmp_path):
    # tar unpacks relative to wherever the restore runs; an absolute path in
    # the archive would restore over the live corpus.
    assert not [m for m in _members(_run(tmp_path, "--dry-run")[0].stdout)
                if m.startswith("/")]


def test_the_member_list_is_sorted_so_two_runs_read_the_same(tmp_path):
    members = _members(_run(tmp_path, "--dry-run")[0].stdout)
    assert members == sorted(members)


def test_dry_run_names_the_dated_destination_object(tmp_path):
    proc, _ = _run(tmp_path, "--dry-run")
    assert f"s3://xanmc/pmt-backups/{DATE}.tar.zst" in proc.stdout


def test_dry_run_uploads_nothing(tmp_path):
    _, log = _run(tmp_path, "--dry-run")
    assert "cp" not in log.read_text()


# ---------- skip if today's object is already up ----------

def test_dry_run_says_it_would_upload_when_the_object_is_absent(tmp_path):
    proc, _ = _run(tmp_path, "--dry-run", object_exists=False)
    assert "would upload" in proc.stdout


def test_dry_run_says_it_would_skip_when_the_object_is_present(tmp_path):
    proc, _ = _run(tmp_path, "--dry-run", object_exists=True)
    assert "skip" in proc.stdout


def test_a_real_run_stops_before_tar_when_the_day_is_already_backed_up(tmp_path):
    proc, log = _run(tmp_path, object_exists=True)
    assert proc.returncode == 0, proc.stderr
    assert "already in S3" in proc.stdout
    calls = log.read_text()
    assert "s3 ls" in calls and "cp" not in calls


def test_an_empty_pmt_home_is_refused_rather_than_shipped(tmp_path):
    home = tmp_path / "empty"
    home.mkdir()
    stub, _ = _stub_aws(tmp_path, object_exists=False)
    env = {**os.environ, "PMT_HOME": str(home), "PMT_AWS": str(stub)}
    proc = subprocess.run(["bash", str(SCRIPT), "--dry-run", "--date", DATE],
                          capture_output=True, text=True, env=env, timeout=60)
    assert proc.returncode != 0 and "nothing to archive" in proc.stderr


def test_an_unknown_flag_is_an_error_not_a_silent_full_run(tmp_path):
    proc, log = _run(tmp_path, "--upload-everything")
    assert proc.returncode == 2 and "unknown option" in proc.stderr
    assert not log.exists() or "cp" not in log.read_text()


# ---------- the archive it actually produces ----------

@pytest.mark.skipif(shutil.which("zstd") is None, reason="needs zstd")
def test_the_uploaded_archive_holds_exactly_the_listed_members(tmp_path):
    proc, log = _run(tmp_path, object_exists=False)
    assert proc.returncode == 0, proc.stderr
    assert f"s3://xanmc/pmt-backups/{DATE}.tar.zst" in log.read_text()

    listed = subprocess.run(
        ["tar", "--zstd", "-tf", str(tmp_path / "uploaded.tar.zst")],
        capture_output=True, text=True, timeout=60)
    members = sorted(m for m in listed.stdout.split() if not m.endswith("/"))
    assert members == sorted(_members(_run(tmp_path, "--dry-run")[0].stdout))


@pytest.mark.skipif(shutil.which("zstd") is None, reason="needs zstd")
def test_the_staging_file_does_not_outlive_the_run(tmp_path):
    # It is a full copy of the corpus on a disk that has room for one.
    _run(tmp_path, object_exists=False)
    assert not list(tmp_path.glob("pmt-backup-*.tar.zst"))
