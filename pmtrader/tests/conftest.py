"""Session-wide test guards.

ONE JOB: no test may write to the operator's real `~/.pmt`. polymarket/errlog
appends a mark every time a belt somewhere catches something, and a good half
the suite deliberately feeds corrupt lines to parsers to prove they survive —
so without this every `pytest` run would salt the live swallowed-errors log with
fabricated failures and make `pmt crypto errors` lie about production.
"""

from __future__ import annotations

import pytest

from polymarket import errlog


@pytest.fixture(autouse=True)
def _errlog_to_tmp(tmp_path_factory, monkeypatch):
    """Redirect the errlog at a per-test temp file and mute its stderr copy.

    Also resets the in-process rate limiter, so a test that asserts on "the
    first occurrence" is not silently answered by a previous test's counter.
    """
    d = tmp_path_factory.mktemp("errlog")
    monkeypatch.setenv("PMT_ERRLOG_PATH", str(d / "swallowed-errors.jsonl"))
    monkeypatch.setenv("PMT_ERRLOG_STDERR", "0")
    errlog.reset()
    yield
    errlog.reset()
