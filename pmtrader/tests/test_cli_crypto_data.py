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
