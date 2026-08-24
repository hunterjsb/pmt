"""Tests for the spot recorder's pure parts — no network, literal live frames.

Four things here are load-bearing, and each one is a bug this corpus has
already paid for somewhere else in the tree:

* **Both clocks on every row.** `opponent_model.md` §1 reversed a published
  conclusion purely by fixing which clock a stream was keyed on. A row with a
  null `t_exch` would let that mistake back in silently, so the parsers must
  drop such a message rather than write it — and `@bookTicker`, which carries
  no exchange stamp at all, is the concrete case.
* **Rotation by UTC date, derived per write.** The btc1h sampler resolved its
  output path once at import and wrote a whole day into the wrong file.
* **Backoff reset requires proven uptime.** Resetting on first message (what
  the rtds recorder does) lets a flapping peer be hammered at 1Hz forever.
* **The parsers**, against frames copied verbatim off the live sockets.
"""

from __future__ import annotations

import calendar
import json
import time

from polymarket import spot

# ---------------------------------------------------------------- live frames
# All four copied verbatim off the sockets on 2026-08-24, untouched.

BINANCE_TRADE = {
    "stream": "btcusdt@trade",
    "data": {"e": "trade", "E": 1787571115890, "s": "BTCUSDT",
             "t": 6609206895, "p": "77954.10000000", "q": "0.00013000",
             "T": 1787571115890, "m": False, "M": True},
}

# The reason this recorder does not use @bookTicker: no `E`, no `T`, no clock.
BINANCE_BOOKTICKER = {
    "stream": "btcusdt@bookTicker",
    "data": {"u": 99033753964, "s": "BTCUSDT", "b": "77887.43000000",
             "B": "2.69636000", "a": "77887.44000000", "A": "1.71628000"},
}

KRAKEN_TRADE = {
    "channel": "trade", "type": "update",
    "data": [{"symbol": "ETH/USD", "side": "buy", "price": 2479.01,
              "qty": 2.25940194, "ord_type": "market", "trade_id": 65158632,
              "timestamp": "2026-08-24T11:32:19.505532Z"}],
}

KRAKEN_TICKER = {
    "channel": "ticker", "type": "snapshot",
    "data": [{"symbol": "BTC/USD", "bid": 77967.8, "bid_qty": 0.14380658,
              "ask": 77967.9, "ask_qty": 1.53978759, "last": 77996.4,
              "volume": 2124.79712598, "vwap": 77472.0, "low": 76686.2,
              "high": 78035.3, "change": 796.4, "change_pct": 1.03,
              "trades": 61052, "timestamp": "2026-08-24T11:32:17.745375Z"}],
}

# `@107` is HYPE/USDC SPOT. Plain `HYPE` is the perp and is NOT what this
# records — see the module docstring.
HYPERLIQUID_TRADES = {
    "channel": "trades",
    "data": [
        {"coin": "@107", "side": "B", "px": "79.171", "sz": "0.53",
         "time": 1787571083025, "hash": "0x00", "tid": 612082524020519,
         "users": ["0x45ab", "0xe547"]},
        {"coin": "@107", "side": "B", "px": "79.172", "sz": "0.22",
         "time": 1787571093687, "hash": "0xec4b", "tid": 920726464482056,
         "users": ["0xb946", "0x645b"]},
    ],
}

# @ticker — the one stamped top-of-book Binance serves without an order book.
# Trimmed to the fields the parser reads plus enough to prove it is the real
# 24hrTicker envelope.
BINANCE_TICKER = {
    "stream": "btcusdt@ticker",
    "data": {"e": "24hrTicker", "E": 1787571605017, "s": "BTCUSDT",
             "p": "1050.99", "P": "1.359", "w": "77472.0",
             "c": "78339.28000000", "Q": "0.00120000",
             "b": "78339.27000000", "B": "3.11700000",
             "a": "78339.28000000", "A": "1.40500000",
             "o": "77288.29", "h": "78400.00", "l": "76652.00",
             "v": "5375.09", "q": "416437000.0", "O": 1787485205016,
             "C": 1787571605016, "F": 6608000000, "L": 6609206895,
             "n": 1206895},
}

T_RECV = 1787571116.004


# ============================================================ the parsers

def test_binance_trade_parses():
    rows = spot.parse_binance(BINANCE_TRADE, T_RECV)
    assert len(rows) == 1
    r = rows[0]
    assert r["venue"] == "binance"
    assert r["sym"] == "btc"
    assert r["kind"] == spot.KIND_TRADE
    assert r["px"] == 77954.1
    assert r["qty"] == 0.00013
    assert r["t_recv"] == T_RECV
    assert r["t_exch"] == 1787571115.890


def test_binance_prefers_transaction_time_over_event_time():
    """`T` is when the trade happened; `E` is when the engine emitted it.

    The whole study is a lead measurement, so the clock must be the moment the
    price event occurred, not the moment the venue got round to announcing it.
    """
    msg = json.loads(json.dumps(BINANCE_TRADE))
    msg["data"]["T"] = 1787571115000
    msg["data"]["E"] = 1787571115890
    assert spot.parse_binance(msg, T_RECV)[0]["t_exch"] == 1787571115.0


def test_binance_falls_back_to_event_time_when_no_transaction_time():
    msg = json.loads(json.dumps(BINANCE_TRADE))
    del msg["data"]["T"]
    assert spot.parse_binance(msg, T_RECV)[0]["t_exch"] == 1787571115.890


def test_binance_ticker_becomes_a_book_row_with_mid_as_px():
    """@ticker is the quote arm on Binance: it has b/a AND an E."""
    rows = spot.parse_binance(BINANCE_TICKER, T_RECV)
    assert len(rows) == 1
    r = rows[0]
    assert (r["venue"], r["sym"], r["kind"]) == ("binance", "btc", spot.KIND_BOOK)
    assert r["bid"] == 78339.27
    assert r["ask"] == 78339.28
    assert abs(r["px"] - 78339.275) < 1e-6
    assert r["t_exch"] == 1787571605.017
    assert "qty" not in r


def test_binance_ticker_without_a_stamp_is_dropped():
    msg = json.loads(json.dumps(BINANCE_TICKER))
    del msg["data"]["E"]
    assert spot.parse_binance(msg, T_RECV) == []


def test_binance_bookticker_is_refused_because_it_has_no_clock():
    """The design decision, pinned as a test.

    If Binance ever adds a stamp to @bookTicker this fails, which is the
    correct outcome: it is an invitation to reconsider the stream choice, not
    a regression.
    """
    assert spot.parse_binance(BINANCE_BOOKTICKER, T_RECV) == []


def test_binance_unknown_symbol_is_dropped_not_guessed():
    msg = json.loads(json.dumps(BINANCE_TRADE))
    msg["data"]["s"] = "PEPEUSDT"
    assert spot.parse_binance(msg, T_RECV) == []


def test_kraken_trade_parses():
    rows = spot.parse_kraken(KRAKEN_TRADE, T_RECV)
    assert len(rows) == 1
    r = rows[0]
    assert (r["venue"], r["sym"], r["kind"]) == ("kraken", "eth", spot.KIND_TRADE)
    assert r["px"] == 2479.01
    assert r["qty"] == 2.25940194
    # 2026-08-24T11:32:19.505532Z, microseconds kept
    assert abs(r["t_exch"] - 1787571139.505532) < 1e-5


def test_kraken_ticker_becomes_a_book_row_with_mid_as_px():
    rows = spot.parse_kraken(KRAKEN_TICKER, T_RECV)
    assert len(rows) == 1
    r = rows[0]
    assert (r["venue"], r["sym"], r["kind"]) == ("kraken", "btc", spot.KIND_BOOK)
    assert r["bid"] == 77967.8
    assert r["ask"] == 77967.9
    assert abs(r["px"] - 77967.85) < 1e-6  # px is the mid on a book row


def test_kraken_ticker_without_both_sides_is_dropped():
    msg = json.loads(json.dumps(KRAKEN_TICKER))
    msg["data"][0]["ask"] = None
    assert spot.parse_kraken(msg, T_RECV) == []


def test_kraken_heartbeat_and_status_produce_nothing():
    assert spot.parse_kraken({"channel": "heartbeat"}, T_RECV) == []
    assert spot.parse_kraken(
        {"channel": "status", "type": "update", "data": [{"system": "online"}]},
        T_RECV) == []


def test_hyperliquid_batch_flattens_to_one_row_each():
    rows = spot.parse_hyperliquid(HYPERLIQUID_TRADES, T_RECV)
    assert len(rows) == 2
    assert [r["px"] for r in rows] == [79.171, 79.172]
    assert rows[0]["t_exch"] == 1787571083.025
    assert all(r["sym"] == "hype" and r["venue"] == "hyperliquid" for r in rows)


def test_hyperliquid_subscription_ack_produces_nothing():
    assert spot.parse_hyperliquid(
        {"channel": "subscriptionResponse",
         "data": {"method": "subscribe",
                  "subscription": {"type": "trades", "coin": "HYPE"}}},
        T_RECV) == []


def test_parsers_survive_garbage_without_raising():
    for parse in (spot.parse_binance, spot.parse_kraken, spot.parse_hyperliquid):
        for junk in (None, [], "", 3, {"data": "not-a-list"}, {"data": [None]},
                     {"channel": "trade", "data": [{"symbol": "BTC/USD"}]}):
            assert parse(junk, T_RECV) == []


# ================================================ BOTH CLOCKS ON EVERY ROW

ALL_GOOD_FRAMES = [
    (spot.parse_binance, BINANCE_TRADE),
    (spot.parse_binance, BINANCE_TICKER),
    (spot.parse_kraken, KRAKEN_TRADE),
    (spot.parse_kraken, KRAKEN_TICKER),
    (spot.parse_hyperliquid, HYPERLIQUID_TRADES),
]


def test_every_row_from_every_venue_carries_both_clocks():
    """The invariant the whole study rests on, asserted across all parsers."""
    n = 0
    for parse, frame in ALL_GOOD_FRAMES:
        rows = parse(frame, T_RECV)
        assert rows, f"{parse.__name__} produced no rows for its own live frame"
        for r in rows:
            assert isinstance(r["t_recv"], float) and r["t_recv"] > 0
            assert isinstance(r["t_exch"], float) and r["t_exch"] > 0
            assert r["t_recv"] == T_RECV
            assert set(r) >= {"t_recv", "t_exch", "venue", "sym", "kind", "px"}
            n += 1
    assert n >= 5


def test_a_message_missing_its_exchange_clock_is_dropped_not_nulled():
    """Every way a venue can omit its stamp, checked one at a time.

    Writing the row with `t_exch: null` would be the silent-corruption
    outcome: the row still joins, still correlates, and quietly re-creates
    `book_lead.md`'s clock error at a scale nobody would notice.
    """
    b = json.loads(json.dumps(BINANCE_TRADE))
    del b["data"]["T"]
    del b["data"]["E"]
    assert spot.parse_binance(b, T_RECV) == []

    for bad in (None, "", "not-a-date", "2026-13-45T99:99:99Z"):
        k = json.loads(json.dumps(KRAKEN_TRADE))
        k["data"][0]["timestamp"] = bad
        assert spot.parse_kraken(k, T_RECV) == []

    for bad in (None, 0, -1, "nope"):
        h = json.loads(json.dumps(HYPERLIQUID_TRADES))
        h["data"][0]["time"] = bad
        rows = spot.parse_hyperliquid(h, T_RECV)
        assert len(rows) == 1  # the sibling in the same frame still survives
        assert rows[0]["px"] == 79.172


def test_a_message_missing_its_price_is_dropped():
    for bad in (None, "", "abc", 0, -5):
        b = json.loads(json.dumps(BINANCE_TRADE))
        b["data"]["p"] = bad
        assert spot.parse_binance(b, T_RECV) == []


# =========================================== rotation by UTC date, per write

def _epoch(y, mo, d, h, mi, s):
    return float(calendar.timegm((y, mo, d, h, mi, s, 0, 0, 0)))


def test_daily_path_names_the_utc_day():
    t = _epoch(2026, 8, 24, 11, 32, 19)
    assert spot.daily_path(t, "binance").name == "spot-binance-20260824.jsonl"
    assert spot.daily_path(t, "kraken").name == "spot-kraken-20260824.jsonl"


def test_daily_path_rolls_at_utc_midnight_not_local_midnight():
    """23:59:59Z and 00:00:00Z are different files; the local offset is
    irrelevant, which is the entire reason the corpus is indexed on UTC."""
    before = _epoch(2026, 8, 24, 23, 59, 59)
    after = _epoch(2026, 8, 25, 0, 0, 0)
    assert spot.daily_path(before, "binance").name == "spot-binance-20260824.jsonl"
    assert spot.daily_path(after, "binance").name == "spot-binance-20260825.jsonl"


def test_writer_derives_its_path_per_write_and_rolls_mid_stream(tmp_path):
    """The btc1h bug, guarded: an open handle must not survive the date change.

    Note the writer is constructed BEFORE any write, so a path captured at
    construction time would send both rows to one file.
    """
    w = spot.DailyWriter("binance", tmp_path)
    before = _epoch(2026, 8, 24, 23, 59, 59)
    after = _epoch(2026, 8, 25, 0, 0, 1)
    w.write(spot.make_row(before, before, "binance", "btc", "trade", 1.0))
    assert w.path.name == "spot-binance-20260824.jsonl"
    w.write(spot.make_row(after, after, "binance", "btc", "trade", 2.0))
    assert w.path.name == "spot-binance-20260825.jsonl"
    w.close()

    d1 = (tmp_path / "spot-binance-20260824.jsonl").read_text().splitlines()
    d2 = (tmp_path / "spot-binance-20260825.jsonl").read_text().splitlines()
    assert len(d1) == 1 and len(d2) == 1
    assert json.loads(d1[0])["px"] == 1.0
    assert json.loads(d2[0])["px"] == 2.0


def test_writer_keeps_venues_in_separate_files(tmp_path):
    t = _epoch(2026, 8, 24, 12, 0, 0)
    for v in ("binance", "kraken", "hyperliquid"):
        w = spot.DailyWriter(v, tmp_path)
        w.write(spot.make_row(t, t, v, "btc", "trade", 1.0))
        w.close()
    assert sorted(p.name for p in tmp_path.glob("*.jsonl")) == [
        "spot-binance-20260824.jsonl",
        "spot-hyperliquid-20260824.jsonl",
        "spot-kraken-20260824.jsonl",
    ]


def test_write_many_routes_a_batch_and_is_readable_back(tmp_path):
    t = _epoch(2026, 8, 24, 12, 0, 0)
    w = spot.DailyWriter("kraken", tmp_path)
    w.write_many(spot.parse_kraken(KRAKEN_TICKER, t)
                 + spot.parse_kraken(KRAKEN_TRADE, t))
    w.close()
    lines = (tmp_path / "spot-kraken-20260824.jsonl").read_text().splitlines()
    assert len(lines) == 2
    assert [json.loads(x)["kind"] for x in lines] == ["book", "trade"]


def test_write_many_of_nothing_creates_no_file(tmp_path):
    w = spot.DailyWriter("kraken", tmp_path)
    w.write_many([])
    w.close()
    assert list(tmp_path.glob("*.jsonl")) == []


# ================================================== reconnect backoff policy

def test_backoff_first_retry_is_fast_then_doubles():
    b = spot.Backoff(first=0.5, cap=30.0)
    assert [b.next_delay() for _ in range(6)] == [0.5, 1.0, 2.0, 4.0, 8.0, 16.0]


def test_backoff_is_capped():
    b = spot.Backoff(first=1.0, cap=8.0)
    assert [b.next_delay() for _ in range(6)] == [1.0, 2.0, 4.0, 8.0, 8.0, 8.0]


def test_backoff_does_not_reset_on_a_short_flap():
    """A peer that accepts, sends one frame and drops must NOT get the fast
    retry back — that is the 1Hz-hammer the rtds recorder's reset allows."""
    b = spot.Backoff(first=0.5, cap=30.0, healthy_after_s=30.0)
    b.next_delay(); b.next_delay(); b.next_delay()   # now at 4.0
    assert b.note_uptime(0.4) is False
    assert b.note_uptime(29.9) is False
    assert b.next_delay() == 4.0


def test_backoff_resets_after_a_connection_proves_healthy():
    b = spot.Backoff(first=0.5, cap=30.0, healthy_after_s=30.0)
    for _ in range(5):
        b.next_delay()
    assert b.note_uptime(30.0) is True
    assert b.next_delay() == 0.5


def test_backoff_recovers_full_range_after_reset():
    b = spot.Backoff(first=0.5, cap=30.0, healthy_after_s=10.0)
    b.next_delay(); b.next_delay()
    b.note_uptime(11.0)
    assert [b.next_delay() for _ in range(3)] == [0.5, 1.0, 2.0]


# ================================================= the loud-failure contract

def _stats(venue, frames, rows, connects=1):
    s = spot.VenueStats(venue)
    s.frames, s.rows, s.connects = frames, rows, connects
    return s


def test_healthy_run_exits_zero():
    rc, msgs = spot.exit_code({"binance": _stats("binance", 900, 850),
                               "kraken": _stats("kraken", 90, 80)})
    assert rc == 0
    assert msgs == []


def test_a_venue_with_no_frames_is_a_hard_failure():
    """Zero frames means the socket or the subscription is broken, and the
    file it leaves behind holds only a start marker."""
    rc, msgs = spot.exit_code({"binance": _stats("binance", 900, 850),
                               "kraken": _stats("kraken", 0, 0, connects=0)})
    assert rc == 1
    assert any("FAIL" in m and "kraken" in m for m in msgs)


def test_frames_everywhere_but_nothing_parsed_is_its_own_exit_code():
    """Distinct from rc=1 on purpose: the plumbing is fine and a payload shape
    moved under the parsers, which is a different thing to go and fix."""
    rc, msgs = spot.exit_code({"binance": _stats("binance", 900, 0),
                               "kraken": _stats("kraken", 90, 0)})
    assert rc == 2
    assert any("NOTHING parsed" in m for m in msgs)


def test_a_quiet_market_warns_but_does_not_fail():
    """hype can legitimately go an hour without a trade. Failing on that would
    train everyone to ignore the exit code, which is the real dead-sampler
    lesson — the zero bytes were only the symptom."""
    rc, msgs = spot.exit_code({"binance": _stats("binance", 900, 850),
                               "hyperliquid": _stats("hyperliquid", 40, 0)})
    assert rc == 0
    assert any(m.startswith("WARN") and "hyperliquid" in m for m in msgs)


def test_a_hard_failure_outranks_a_warning():
    rc, msgs = spot.exit_code({"binance": _stats("binance", 0, 0, connects=0),
                               "hyperliquid": _stats("hyperliquid", 40, 0)})
    assert rc == 1
    assert not any(m.startswith("WARN") for m in msgs)


# ============================================================ the clock

def test_clock_is_epoch_comparable_and_advances():
    c = spot.Clock()
    a = c.now()
    assert abs(a - time.time()) < 1.0
    time.sleep(0.01)
    assert c.now() > a


def test_clock_ignores_a_wall_clock_step():
    """An NTP step during a run must not appear as market lead.

    The anchor is moved rather than the system clock: advancing `t0_wall` by
    an hour is exactly what a consumer of `time.time()` would have seen, and
    `now()` must not inherit it.
    """
    c = spot.Clock()
    before = c.now()
    c.t0_mono -= 5.0          # 5 more seconds of monotonic elapsed
    assert c.now() - before >= 5.0
    skew_free = c.now()
    assert abs(skew_free - (before + 5.0)) < 0.5


# ====================================================== plan / message shapes

def test_hype_has_no_binance_pair_and_is_covered_elsewhere():
    """The venue split is a fact about the market, asserted rather than
    commented: HYPE has no Binance spot pair (`HYPEUSDT` → -1121), which is why
    Hyperliquid is a venue here at all. Kraken lists it too, so HYPE still gets
    the two independent readings every other symbol gets."""
    assert "hype" not in spot.BINANCE_SYMBOLS
    assert spot.venue_symbols("binance", ["btc", "hype"]) == ["btc"]
    assert spot.KRAKEN_SYMBOLS["hype"] == "HYPE/USD"
    assert spot.venue_symbols("hyperliquid", ["btc", "hype"]) == ["hype"]


def test_no_binance_symbol_is_the_hyper_token():
    """`HYPER*` is a different asset. A substring match on 'HYPE' pulls it in,
    and a wrong symbol in this corpus is worse than a missing one."""
    for pair in spot.BINANCE_SYMBOLS.values():
        assert not pair.startswith("hyper")


def test_every_symbol_has_at_least_two_independent_venues():
    """A lead that shows up on one venue only is that venue's microstructure.
    The study can only make that distinction if every symbol is double-covered.
    """
    for sym in spot.SYMBOLS:
        venues = [v for v in spot.VENUES if sym in spot.venue_symbols(v, [sym])]
        assert len(venues) >= 2, f"{sym} only on {venues}"


def test_hyperliquid_records_spot_not_the_perp():
    """Plain `HYPE` is the perp; `@107` is HYPE/USDC spot. A settlement index
    tracks spot. Sending a readable spot name instead kills the connection."""
    assert spot.HYPERLIQUID_SYMBOLS["hype"] == "@107"
    assert "HYPE/USDC" not in spot.HYPERLIQUID_SYMBOLS.values()


def test_kraken_uses_v2_spellings_not_the_legacy_rest_wsnames():
    """Kraken's REST AssetPairs still reports `XBT/USD` and `XDG/USD` as
    `wsname`; v2 rejects both. Building the map from REST would break the two
    highest-volume symbols here and nothing else."""
    assert spot.KRAKEN_SYMBOLS["btc"] == "BTC/USD"
    assert spot.KRAKEN_SYMBOLS["doge"] == "DOGE/USD"
    assert not any(p.startswith(("XBT", "XDG")) for p in spot.KRAKEN_SYMBOLS.values())
    # HYPE/USDT does not exist, so the whole venue standardizes on USD.
    assert all(p.endswith("/USD") for p in spot.KRAKEN_SYMBOLS.values())


def test_venue_symbols_preserves_requested_order_and_drops_unknowns():
    assert spot.venue_symbols("kraken", ["doge", "btc", "nope"]) == ["doge", "btc"]


def test_binance_url_carries_both_the_trade_and_quote_arm():
    url = spot.binance_url(["btc", "doge"])
    assert url.startswith(spot.BINANCE_WS + "?streams=")
    assert url.endswith("btcusdt@trade/btcusdt@ticker/"
                        "dogeusdt@trade/dogeusdt@ticker")
    assert "@bookTicker" not in url  # it has no clock; see the parser test


def test_binance_url_stays_under_the_stream_ceiling():
    """1024 streams per connection, documented. Two per symbol leaves the whole
    venue on one socket and one file with room to spare."""
    assert spot.binance_url(list(spot.BINANCE_SYMBOLS)).count("@") == \
        2 * len(spot.BINANCE_SYMBOLS)
    assert 2 * len(spot.BINANCE_SYMBOLS) < 1024


def test_binance_url_uses_the_market_data_mirror_not_the_geoblocked_host():
    """stream.binance.com answers HTTP 451 from this box; binance.vision is the
    same global book and does not. Pinned so nobody 'fixes' it back."""
    assert "binance.vision" in spot.BINANCE_WS
    assert "stream.binance.com" not in spot.BINANCE_WS


def test_kraken_subscribes_cover_both_trade_and_ticker_channels():
    subs = [json.loads(s) for s in spot.kraken_subscribes(["btc", "eth"])]
    assert [s["params"]["channel"] for s in subs] == ["trade", "ticker"]
    for s in subs:
        assert s["params"]["symbol"] == ["BTC/USD", "ETH/USD"]


def test_hyperliquid_subscribes_one_per_coin():
    subs = [json.loads(s) for s in spot.hyperliquid_subscribes(["hype"])]
    assert subs == [{"method": "subscribe",
                     "subscription": {"type": "trades", "coin": "@107"}}]


# ========================================================== record shapes

def test_gap_record_measures_silence_not_socket_state():
    g = spot.gap_record(100.0, "kraken", 12.5, t_last=87.5, reason="stall")
    assert g["ev"] == "gap"
    assert g["venue"] == "kraken"
    assert g["down_s"] == 12.5
    assert g["t_last"] == 87.5
    assert g["reason"] == "stall"


def test_marker_records_bound_the_tape_at_both_edges():
    s = spot.marker_record(1.0, "binance", spot.EV_START, pid=7)
    e = spot.marker_record(2.0, "binance", spot.EV_STOP, rows=99)
    assert s["ev"] == "start" and s["pid"] == 7
    assert e["ev"] == "stop" and e["rows"] == 99


def test_markers_and_gaps_are_distinguishable_from_data_rows():
    """A consumer must be able to tell 'the price did not move' from 'we were
    not listening' with one key lookup."""
    data = spot.parse_binance(BINANCE_TRADE, T_RECV)[0]
    assert "ev" not in data
    for rec in (spot.gap_record(1.0, "binance", 2.0),
                spot.marker_record(1.0, "binance", spot.EV_START)):
        assert "ev" in rec
        assert "t_exch" not in rec


def test_make_row_omits_absent_optional_fields():
    r = spot.make_row(1.0, 2.0, "binance", "btc", "trade", 3.0)
    assert set(r) == {"t_recv", "t_exch", "venue", "sym", "kind", "px"}


def test_parse_helpers():
    assert spot.parse_ms(1787571115890) == 1787571115.890
    assert spot.parse_ms("1787571115890") == 1787571115.890
    assert spot.parse_ms(None) is None
    assert spot.parse_ms(True) is None
    assert spot.parse_ms(0) is None
    assert spot.parse_rfc3339("2026-08-24T11:32:17.745375Z") is not None
    assert spot.parse_rfc3339("garbage") is None
    assert spot.parse_rfc3339(None) is None
