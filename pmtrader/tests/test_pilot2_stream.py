"""The in-memory settlement-stream read model.

No socket: records are fed through `StreamState.ingest` in the exact shape
`polymarket.rtds.normalize` produces, so this pins the seam between the
recorder's protocol knowledge and the pilot's pricing inputs.
"""

from __future__ import annotations

from pilot2 import predict, stream
from polymarket import rtds

START = 1787400000.0
END = 1787400300.0


def _rec(topic, symbol, ts_s, price):
    """A normalised RTDS record. `full_accuracy_value` is the E18 string the
    recorder keeps precisely because the float `value` already lost bits."""
    return {"t_recv": ts_s, "topic": topic, "symbol": symbol,
            "ts": int(ts_s * 1000),
            "value": price,
            "full_accuracy_value": str(round(price * 10 ** 18)),
            "window_s": 60 if topic == rtds.TOPIC_TWAP60 else None}


def a_state(n_spot=120, spot0=100.0, step=0.001, t_end=START + 120.0):
    s = stream.StreamState()
    for i in range(n_spot):
        ts = t_end - (n_spot - 1 - i)
        s.ingest(_rec(rtds.TOPIC_SPOT, "doge/usd", ts, spot0 + i * step), ts)
    return s


def test_only_the_two_topics_the_pricer_needs_are_subscribed():
    """The 30s TWAP is what the width bug priced. Nothing here reads it."""
    assert stream.TOPICS == (rtds.TOPIC_TWAP60, rtds.TOPIC_SPOT)
    assert rtds.TOPIC_TWAP30 not in stream.TOPICS


def test_the_subscribe_frame_comes_from_the_recorder_not_a_copy():
    """One entry per TOPIC with filters omitted. 24 per-symbol entries made the
    server quietly downgrade the whole subscription to snapshot payloads."""
    import json
    msg = json.loads(rtds.subscribe_message(stream.TOPICS))
    assert msg["action"] == "subscribe"
    assert [e["topic"] for e in msg["subscriptions"]] == list(stream.TOPICS)
    assert all("filters" not in e for e in msg["subscriptions"])


def test_the_e18_string_is_what_gets_stored_not_the_lossy_float():
    s = stream.StreamState()
    rec = _rec(rtds.TOPIC_SPOT, "doge/usd", START, 0.0)
    rec["full_accuracy_value"] = "123456789012345678"   # 0.123456789012345678
    rec["value"] = 0.12345678901234568                   # the float that lost bits
    s.ingest(rec, START)
    got = s.spot("doge/usd", START)[0]
    assert got == float(rtds.e18_decimal("123456789012345678"))


def test_reference_is_the_twap_print_at_start_exactly():
    """Not the minute mark before it. A dropped second at the boundary means
    this window has no reference and must not be priced."""
    s = a_state()
    s.ingest(_rec(rtds.TOPIC_TWAP60, "doge/usd", START - 1, 99.0), START - 1)
    s.ingest(_rec(rtds.TOPIC_TWAP60, "doge/usd", START, 100.0), START)
    s.ingest(_rec(rtds.TOPIC_TWAP60, "doge/usd", START + 1, 101.0), START + 1)
    assert s.reference("doge/usd", START) == 100.0
    assert s.reference("doge/usd", START + 5) is None, "no print at start, no reference"
    assert s.reference("btc/usd", START) is None


def test_spot_goes_stale_at_the_specs_five_seconds():
    s = a_state(t_end=START + 100.0)
    assert s.spot("doge/usd", START + 100.0 + predict.MAX_STALE_S) is not None
    assert s.spot("doge/usd", START + 100.0 + predict.MAX_STALE_S + 0.001) is None


def test_banked_is_the_settlement_seconds_that_have_printed():
    """[end-60, now], and the width is 60s at EVERY duration."""
    s = a_state(n_spot=200, t_end=END - 10.0)
    b = s.banked("doge/usd", END, END - 10.0)
    assert len(b) == 51, "[end-60, end-10] inclusive at both ends"
    assert s.banked("doge/usd", END, END - 120.0) == [], \
        "nothing is banked before the settlement window opens"


def test_banked_is_clamped_at_the_window_end_not_at_now():
    """[end-60, min(now, end)]. Past the close the settlement average is
    finished, and a print stamped after it is a second the exchange did not
    average — without the clamp a window polled after its own end banks MORE
    than sixty seconds of mass, which is not a window that exists. The Rust
    port has always clamped `to = now.min(end)`; this is the same rule.
    """
    s = a_state(n_spot=400, t_end=END + 60.0)
    full = s.banked("doge/usd", END, END)
    assert len(full) == 61, "[end-60, end] inclusive at both ends"
    # Thirty seconds after the close, with the stream still printing: the same
    # sixty seconds, not ninety.
    assert s.banked("doge/usd", END, END + 30.0) == full
    assert len(s.banked("doge/usd", END, END + 30.0)) <= 61


def test_sigma_lookback_floors_at_start_minus_sixty():
    """[max(now-300, start-60), now]. Early in a window the lookback is only
    ~70s and the reported results were produced that way; a full trailing 300s
    is a DIFFERENT model and would have to be re-validated before it shipped.

    Pinned by putting a violent move BEFORE the floor: if the floor is wrong,
    that move lands in the sample and sigma jumps.
    """
    s = stream.StreamState()
    for i in range(400):                      # start-360 .. start+39
        ts = START - 360.0 + i
        px = 100.0 + 0.001 * i
        if ts < START - 60.0:
            px *= 2.0                         # a 2x step, far outside the floor
        s.ingest(_rec(rtds.TOPIC_SPOT, "doge/usd", ts, px), ts)

    now = START + 30.0
    got = s.sigma("doge/usd", START, now)
    inside_ts = [START - 60.0 + i for i in range(91)]
    inside_px = [100.0 + 0.001 * (i + 300) for i in range(91)]
    assert got == predict.sigma_per_second(inside_ts, inside_px)
    assert got < 1e-4, "the pre-floor 2x step is excluded, so sigma stays small"


def test_sigma_uses_a_full_three_hundred_seconds_once_the_window_is_old():
    """The floor is max(now-300, start-60): late in a long window the 300s
    trailing term is the binding one."""
    s = a_state(n_spot=500, t_end=START + 600.0)
    got = s.sigma("doge/usd", START, START + 600.0)
    ts = [START + 300.0 + i for i in range(301)]
    px = [100.0 + 0.001 * (i + 199) for i in range(301)]
    assert got == predict.sigma_per_second(ts, px)


def test_a_repeated_second_is_dropped_rather_than_read_as_a_zero_gap():
    """The feed can repeat a second across a reconnect. A duplicate timestamp
    would read as a 0s interval and poison sigma's consecutive test."""
    s = stream.StreamState()
    for i in range(10):
        s.ingest(_rec(rtds.TOPIC_SPOT, "doge/usd", START + i, 100.0 + i), START + i)
    before = s.spot("doge/usd", START + 9)
    s.ingest(_rec(rtds.TOPIC_SPOT, "doge/usd", START + 9, 999.0), START + 9)
    assert s.spot("doge/usd", START + 9) == before


def test_a_snapshot_payload_never_reaches_the_pricer():
    """The shape the server downgrades to when over-subscribed. `normalize`
    refuses it, and the pilot only ingests what normalize produced."""
    envelope = {"payload": {"data": [{"timestamp": 1, "value": 1.0}], "symbol": "sol/usd"},
                "topic": "crypto_prices", "type": "update"}
    assert rtds.normalize(envelope, START) is None


def test_health_reports_silence_and_reconnects():
    s = a_state()
    h = s.health(now=START + 130.0)
    assert h["symbols"] == 1 and h["messages"] > 0 and h["silent_s"] is not None
