"""Tests for the RTDS recorder's pure parts — no network, inline fixtures.

What actually needs guarding here is the lossless path: `full_accuracy_value`
is an E18 fixed-point string whose whole point is that it survives where the
float `value` does not, so anything that lets it round-trip through a float is a
silent corpus corruption. The rest is rotation naming, gap-record shape, and the
legacy-capture converter that has to make two ad-hoc capture formats uniform.
"""

from __future__ import annotations

import json
import time
from decimal import Decimal

from polymarket import rtds

# Literal envelope off the live socket (2026-08-23), trimmed of nothing.
ENVELOPE_TWAP60 = {
    "connection_id": "gYIzuU-D1WeIKEjo-A==",
    "payload": {
        "full_accuracy_value": "76227641250796072337408",
        "symbol": "btc/usd",
        "timestamp": 1787473709000,
        "value": 76227.64125079608,
        "window_s": 60,
    },
    "timestamp": 1787473710555,
    "topic": "crypto_prices_twap_sixty",
    "type": "update",
}

ENVELOPE_SPOT = {
    "connection_id": "gYI6GG4BbWeIKEhsCA==",
    "payload": {
        "full_accuracy_value": "807970804163331700000",
        "symbol": "zec/usd",
        "timestamp": 1787475015000,
        "value": 807.9708041633316,
    },
    "timestamp": 1787475016324,
    "topic": "crypto_prices_chainlink",
    "type": "update",
}

# The shape the server quietly downgrades to when you over-subscribe per symbol.
ENVELOPE_BINANCE_SNAPSHOT = {
    "payload": {"data": [{"timestamp": 1787474888000, "value": 93.22022889}],
                "symbol": "sol/usd"},
    "topic": "crypto_prices",
    "type": "update",
}


# ---------- E18 parsing ----------

def test_parse_e18_exact_integer():
    assert rtds.parse_e18("76227641250796072337408") == 76227641250796072337408


def test_parse_e18_survives_what_float_would_destroy():
    # The last digits are below float64 resolution at this magnitude; a parse
    # that went through float would come back changed.
    s = "76227641250796072337409"
    assert rtds.parse_e18(s) == 76227641250796072337409
    assert int(float(s)) != rtds.parse_e18(s)


def test_parse_e18_rejects_junk():
    for bad in (None, "", "  ", "abc", "1.5", "1e18", True, [], {}):
        assert rtds.parse_e18(bad) is None


def test_parse_e18_signs_and_ints():
    assert rtds.parse_e18("+5") == 5
    assert rtds.parse_e18("-5") == -5
    assert rtds.parse_e18(42) == 42


def test_e18_decimal_is_exact():
    d = rtds.e18_decimal("76227641250796072337408")
    assert d == Decimal("76227.641250796072337408")
    assert isinstance(d, Decimal)


def test_e18_decimal_matches_display_value_to_float_precision():
    d = rtds.e18_decimal(ENVELOPE_TWAP60["payload"]["full_accuracy_value"])
    assert abs(float(d) - ENVELOPE_TWAP60["payload"]["value"]) < 1e-6


def test_e18_decimal_none_on_junk():
    assert rtds.e18_decimal("nope") is None


# ---------- normalization ----------

def test_normalize_twap_sixty():
    rec = rtds.normalize(ENVELOPE_TWAP60, 1787473710.6789)
    assert rec == {
        "t_recv": 1787473710.679,
        "topic": "crypto_prices_twap_sixty",
        "symbol": "btc/usd",
        "ts": 1787473709000,
        "value": 76227.64125079608,
        "full_accuracy_value": "76227641250796072337408",
        "window_s": 60,
    }


def test_normalize_keeps_full_accuracy_value_as_string():
    rec = rtds.normalize(ENVELOPE_TWAP60, 1.0)
    assert isinstance(rec["full_accuracy_value"], str)
    assert rec["full_accuracy_value"] == ENVELOPE_TWAP60["payload"]["full_accuracy_value"]


def test_normalize_spot_has_no_window():
    rec = rtds.normalize(ENVELOPE_SPOT, 1787475016.5)
    assert rec["topic"] == "crypto_prices_chainlink"
    assert rec["window_s"] is None
    assert rec["symbol"] == "zec/usd"


def test_normalize_infers_window_from_topic_when_payload_omits_it():
    env = json.loads(json.dumps(ENVELOPE_TWAP60))
    del env["payload"]["window_s"]
    assert rtds.normalize(env, 1.0)["window_s"] == 60
    env["topic"] = "crypto_prices_twap_thirty"
    assert rtds.normalize(env, 1.0)["window_s"] == 30


def test_normalize_rejects_untracked_topic():
    assert rtds.normalize(ENVELOPE_BINANCE_SNAPSHOT, 1.0) is None


def test_normalize_rejects_malformed():
    base = json.loads(json.dumps(ENVELOPE_TWAP60))
    assert rtds.normalize(None, 1.0) is None
    assert rtds.normalize("PONG", 1.0) is None
    assert rtds.normalize({"topic": "crypto_prices_twap_sixty"}, 1.0) is None
    no_sym = json.loads(json.dumps(base)); del no_sym["payload"]["symbol"]
    assert rtds.normalize(no_sym, 1.0) is None
    no_ts = json.loads(json.dumps(base)); del no_ts["payload"]["timestamp"]
    assert rtds.normalize(no_ts, 1.0) is None
    no_price = json.loads(json.dumps(base))
    del no_price["payload"]["value"]; del no_price["payload"]["full_accuracy_value"]
    assert rtds.normalize(no_price, 1.0) is None


def test_normalize_uses_payload_observation_time_not_envelope_emit_time():
    rec = rtds.normalize(ENVELOPE_TWAP60, 1787473710.6)
    assert rec["ts"] == ENVELOPE_TWAP60["payload"]["timestamp"]
    assert rec["ts"] != ENVELOPE_TWAP60["timestamp"]


def test_normalize_output_is_json_serializable():
    json.dumps(rtds.normalize(ENVELOPE_TWAP60, time.time()))


# ---------- rotation naming ----------

def test_daily_path_names_by_utc_day(tmp_path):
    # 1787473710 == 2026-08-23 08:28:30 UTC
    assert rtds.daily_path(1787473710.0, tmp_path).name == "rtds-20260823.jsonl"


def test_daily_path_rotates_at_utc_midnight(tmp_path):
    midnight = 1787443200.0  # 2026-08-23 00:00:00 UTC
    assert rtds.daily_path(midnight - 0.5, tmp_path).name == "rtds-20260822.jsonl"
    assert rtds.daily_path(midnight, tmp_path).name == "rtds-20260823.jsonl"


def test_daily_path_is_under_the_given_directory(tmp_path):
    assert rtds.daily_path(1787473710.0, tmp_path).parent == tmp_path


def test_writer_rotates_across_days(tmp_path):
    w = rtds.DailyWriter(tmp_path)
    w.write({"t_recv": 1787443199.0, "topic": "t", "symbol": "btc/usd"})
    w.write({"t_recv": 1787443201.0, "topic": "t", "symbol": "btc/usd"})
    w.close()
    assert sorted(p.name for p in tmp_path.glob("*.jsonl")) == \
        ["rtds-20260822.jsonl", "rtds-20260823.jsonl"]


def test_writer_appends_rather_than_truncates(tmp_path):
    for _ in range(2):
        w = rtds.DailyWriter(tmp_path)
        w.write(rtds.normalize(ENVELOPE_TWAP60, 1787473710.0))
        w.close()
    lines = (tmp_path / "rtds-20260823.jsonl").read_text().strip().split("\n")
    assert len(lines) == 2
    assert json.loads(lines[0]) == json.loads(lines[1])


# ---------- gap + marker records ----------

def test_gap_record_shape():
    rec = rtds.gap_record(1787473710.1234, 12.5678)
    assert rec == {"t_recv": 1787473710.123, "ev": "gap", "down_s": 12.568}


def test_gap_record_optional_provenance():
    rec = rtds.gap_record(100.0, 30.0, t_last=70.0, reason="stall")
    assert rec["ev"] == "gap"
    assert rec["t_last"] == 70.0
    assert rec["reason"] == "stall"
    # down_s must reconcile with the bracketing timestamps, or a consumer can't
    # tell a real hole from a bookkeeping error.
    assert rec["t_recv"] - rec["t_last"] == rec["down_s"]


def test_gap_record_is_distinguishable_from_a_price_record():
    gap = rtds.gap_record(100.0, 5.0)
    price = rtds.normalize(ENVELOPE_TWAP60, 100.0)
    assert "ev" in gap and "ev" not in price
    assert "topic" in price and "topic" not in gap


def test_marker_records():
    assert rtds.marker_record(1.0, "start", pid=7) == {"t_recv": 1.0, "ev": "start", "pid": 7}
    assert rtds.marker_record(2.0, "stop")["ev"] == "stop"


# ---------- subscription framing ----------

def test_subscribe_message_is_one_entry_per_topic_with_no_filters():
    msg = json.loads(rtds.subscribe_message())
    assert msg["action"] == "subscribe"
    assert [s["topic"] for s in msg["subscriptions"]] == list(rtds.TOPICS)
    # per-symbol `filters` is the trap: it silently downgrades the subscription
    assert all("filters" not in s for s in msg["subscriptions"])
    assert all(s["type"] == "update" for s in msg["subscriptions"])


# ---------- legacy capture conversion ----------

LEGACY_V5 = {"topic": "crypto_prices_twap_sixty", "sym": "xrp/usd", "ts": 1787473996000,
             "v": 1.4776773335484439, "fav": "1477677333548443904", "w": 60,
             "recv_ms": 1787473997523}
# rtds4 predates the `w` field entirely.
LEGACY_V4 = {"topic": "crypto_prices_twap_thirty", "sym": "eth/usd", "ts": 1787473734000,
             "v": 2406.5111128949166, "fav": "2406511112894916591616",
             "recv_ms": 1787473735791}


def test_convert_legacy_record_matches_live_record_shape():
    legacy = rtds.convert_legacy_record(LEGACY_V5)
    live = rtds.normalize(ENVELOPE_TWAP60, 1.0)
    assert set(legacy) == set(live)
    assert legacy["symbol"] == "xrp/usd"
    assert legacy["full_accuracy_value"] == "1477677333548443904"
    assert legacy["ts"] == 1787473996000
    assert legacy["t_recv"] == 1787473997.523
    assert legacy["window_s"] == 60


def test_convert_legacy_record_infers_missing_window_from_topic():
    assert rtds.convert_legacy_record(LEGACY_V4)["window_s"] == 30


def test_convert_legacy_record_spot_window_is_none():
    d = dict(LEGACY_V4, topic="crypto_prices_chainlink")
    d.pop("w", None)
    assert rtds.convert_legacy_record(d)["window_s"] is None


def test_convert_legacy_record_rejects_junk():
    assert rtds.convert_legacy_record({"topic": "nope", "sym": "btc/usd", "ts": 1}) is None
    assert rtds.convert_legacy_record({"topic": "crypto_prices_twap_sixty"}) is None
    assert rtds.convert_legacy_record("not a dict") is None


def test_convert_legacy_file_routes_by_utc_day(tmp_path):
    src = tmp_path / ".rtds5.jsonl"
    late = dict(LEGACY_V5, recv_ms=1787443199000)   # 2026-08-22 23:59:59 UTC
    early = dict(LEGACY_V5, recv_ms=1787443201000)  # 2026-08-23 00:00:01 UTC
    src.write_text("\n".join(json.dumps(d) for d in (late, early)) + "\n")
    out = tmp_path / "corpus"
    counts = rtds.convert_legacy_file(src, out)
    assert counts == {"converted": 2, "off_topic": 0, "bad": 0}
    assert (out / "rtds-20260822.jsonl").read_text().count("\n") == 1
    assert (out / "rtds-20260823.jsonl").read_text().count("\n") == 1


def test_convert_legacy_file_separates_off_topic_from_corrupt(tmp_path):
    """The captures carry thousands of Binance `crypto_prices` lines we skip by
    design; counting those as `bad` would hide a real parse failure."""
    src = tmp_path / ".rtds4.jsonl"
    src.write_text("\n".join([json.dumps(LEGACY_V4), "", '{"topic": "trunc',
                              json.dumps({"topic": "crypto_prices", "sym": "btc/usd",
                                          "ts": 1, "v": 1.0}),
                              json.dumps({"topic": "crypto_prices_twap_sixty"}),
                              json.dumps(LEGACY_V5)]) + "\n")
    counts = rtds.convert_legacy_file(src, tmp_path / "corpus")
    # 2 good; 1 off-topic (Binance); 2 bad (truncated JSON + on-topic but fieldless);
    # the blank line is not an error.
    assert counts == {"converted": 2, "off_topic": 1, "bad": 2}


def test_convert_legacy_is_idempotent(tmp_path):
    """Second run must not double the corpus — these files are append-only."""
    src = tmp_path / ".rtds5.jsonl"
    src.write_text(json.dumps(LEGACY_V5) + "\n")
    out = tmp_path / "corpus"
    first = rtds.convert_legacy([src], out)
    assert first[".rtds5.jsonl"]["converted"] == 1
    second = rtds.convert_legacy([src], out)
    assert second[".rtds5.jsonl"]["skipped"] == "already-converted"
    assert (out / "rtds-20260823.jsonl").read_text().count("\n") == 1


def test_convert_legacy_force_reconverts(tmp_path):
    src = tmp_path / ".rtds5.jsonl"
    src.write_text(json.dumps(LEGACY_V5) + "\n")
    out = tmp_path / "corpus"
    rtds.convert_legacy([src], out)
    rtds.convert_legacy([src], out, force=True)
    assert (out / "rtds-20260823.jsonl").read_text().count("\n") == 2


def test_convert_legacy_reports_missing_sources(tmp_path):
    result = rtds.convert_legacy([tmp_path / "nope.jsonl"], tmp_path / "corpus")
    assert result["nope.jsonl"]["skipped"] == "missing"


# ---------- pidfile ----------

def test_claim_pidfile_writes_our_pid(tmp_path):
    import os
    p = tmp_path / "recorder.pid"
    assert rtds.claim_pidfile(p) is True
    assert rtds.read_pidfile(p) == os.getpid()


def test_claim_pidfile_refuses_when_a_live_recorder_holds_it(tmp_path, monkeypatch):
    p = tmp_path / "recorder.pid"
    p.write_text("4242\n")
    monkeypatch.setattr(rtds, "pid_alive", lambda pid: pid == 4242)
    monkeypatch.setattr(rtds.Path, "read_bytes", lambda self: b"python\x00-m\x00polymarket.rtds\x00")
    assert rtds.claim_pidfile(p) is False
    assert rtds.read_pidfile(p) == 4242  # untouched


def test_claim_pidfile_takes_over_a_stale_one(tmp_path, monkeypatch):
    """The nightly poweroff guarantees stale pidfiles; refusing to start after
    every reboot would be worse than the double-start it prevents."""
    import os
    p = tmp_path / "recorder.pid"
    p.write_text("4242\n")
    monkeypatch.setattr(rtds, "pid_alive", lambda pid: False)
    assert rtds.claim_pidfile(p) is True
    assert rtds.read_pidfile(p) == os.getpid()


def test_read_pidfile_tolerates_missing_and_garbage(tmp_path):
    assert rtds.read_pidfile(tmp_path / "nope.pid") is None
    p = tmp_path / "bad.pid"
    p.write_text("not-a-pid")
    assert rtds.read_pidfile(p) is None


def test_release_pidfile_only_removes_our_own(tmp_path):
    p = tmp_path / "recorder.pid"
    p.write_text("4242\n")
    rtds.release_pidfile(p)
    assert p.exists()
    rtds.claim_pidfile(p)
    rtds.release_pidfile(p)
    assert not p.exists()
