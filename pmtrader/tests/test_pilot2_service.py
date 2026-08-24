"""The poll loop, fully network-stubbed.

The load-bearing test in this file is `test_shadow_mode_never_places_an_order`:
the trading client is a fake that RAISES if anything touches it, so a shadow
pass that reaches the order path fails loudly instead of quietly placing.
Everything else pins the laws end-to-end, through the real risk book.
"""

from __future__ import annotations

import json
import math

import pytest

from pilot2 import books, policy, risk, service, state, windows

END = 1787400300.0
START = 1787400000.0
SLUG = "doge-updown-5m-1787400000"


# --- fakes -----------------------------------------------------------------

class FakeStream:
    """Deterministic settlement-stream inputs. No socket, no clock."""

    def __init__(self, ref=100.0, spot=100.5, sigma=2e-4, banked=(), stale=False):
        self._ref, self._spot, self._sigma = ref, spot, sigma
        self._banked = list(banked)
        self._stale = stale

    def reference(self, symbol, start):
        return self._ref

    def spot(self, symbol, now, max_stale_s=5.0):
        return None if self._stale else (self._spot, 0.4)

    def banked(self, symbol, end, now):
        return list(self._banked)

    def sigma(self, symbol, start, now):
        return self._sigma


class FakePoller:
    """A fixed two-sided book. Counts reads so a test can prove the loop is
    not hammering the CLOB."""

    def __init__(self, up_ask=0.55, dn_ask=0.55, up_sz=1000.0, dn_sz=1000.0):
        self.up = books.Top(bid=up_ask - 0.01, bid_size=up_sz, ask=up_ask, ask_size=up_sz)
        self.dn = books.Top(bid=dn_ask - 0.01, bid_size=dn_sz, ask=dn_ask, ask_size=dn_sz)
        self.requests_made = 0
        self.failures = 0
        self._first = True

    def top(self, token):
        self.requests_made += 1
        return self.up if token == "TOK_UP" else self.dn


class FakeCache:
    def __init__(self, window):
        self.window = window
        self.lookups = 0

    def current(self, ser, now=None):
        self.lookups += 1
        return self.window if ser == self.window.series else None

    def sweep(self, now=None):
        pass


class TradedInShadow(BaseException):
    """Not an Exception on purpose: the service catches Exception around a
    live send, and a test tripwire that its own error handler swallows is not
    a tripwire."""


class ExplodingClient:
    """Any attribute access is a placed order as far as this test is concerned."""

    def __getattr__(self, name):
        raise TradedInShadow(f"shadow mode touched the trading client (.{name})")


def a_window(series_key="doge-updown-5m", slug=SLUG):
    return windows.Window(slug=slug, series=series_key, symbol="doge/usd",
                          start=START, end=END, dur_s=300,
                          token_up="TOK_UP", token_down="TOK_DOWN", fee_rate=0.07)


def a_pilot(tmp_path, **kw):
    w = kw.pop("window", a_window())
    return service.Pilot(
        home=tmp_path, live=kw.pop("live", False),
        live_series=kw.pop("live_series", None),
        shadow_series=kw.pop("shadow_series", [w.series]),
        stream=kw.pop("stream", FakeStream()),
        poller=kw.pop("poller", FakePoller(up_ask=0.55, dn_ask=0.55)),
        cache=kw.pop("cache", FakeCache(w)),
        clob_client=kw.pop("clob_client", ExplodingClient()),
        log=lambda *_: None, **kw)


def tape(tmp_path, ev=None):
    return list(state.iter_records(state.SHADOW_TAPE, tmp_path,
                                   evs=(ev,) if ev else None))


# --- shadow places nothing -------------------------------------------------

def test_shadow_mode_never_places_an_order(tmp_path):
    """The client raises on ANY attribute access. A shadow pass that reaches
    execution fails here rather than in production."""
    p = a_pilot(tmp_path)
    took = p.poll_once(now=START + 60.0)
    assert took == 1, "the EV gate fired: a would-be trade was recorded"
    shadow = tape(tmp_path, state.EV_SHADOW)
    assert len(shadow) == 1
    assert shadow[0]["would_trade"] is True
    assert shadow[0]["mode"] == service.MODE_SHADOW
    assert not (tmp_path / state.LIVE_TAPE).exists(), "shadow writes no live tape"


def test_shadow_decision_records_every_input_the_grader_and_a_refit_need(tmp_path):
    p = a_pilot(tmp_path)
    p.poll_once(now=START + 60.0)
    r = tape(tmp_path, state.EV_SHADOW)[0]
    for key in ("slug", "series", "symbol", "side", "token", "start", "end",
                "elapsed_frac", "ref", "spot", "spot_age_s", "sigma_s", "n_banked",
                "model_p_up", "book_p_up", "book_up_ask", "book_dn_ask",
                "book_up_ask_sz", "book_dn_ask_sz", "blend_p_up", "p_side",
                "w", "w_source", "w_rows", "ask", "ask_sz", "fee", "edge",
                "min_edge", "shares", "notional", "capped_by"):
        assert key in r, f"the shadow tape must carry {key}"
    # The model and the book are stored SEPARATELY as well as blended, so a
    # later fit can re-derive the weight the report says must not be frozen.
    assert r["model_p_up"] != r["blend_p_up"]
    assert r["w"] == policy.W_SEED and r["w_source"] == policy.W_SOURCE_SEED


def test_the_blend_weight_in_force_is_logged_on_every_decision(tmp_path):
    state.write_json(state.BLEND_WEIGHT,
                     {"w": 0.35, "source": "fit", "rows": 900}, tmp_path)
    p = a_pilot(tmp_path)
    p.poll_once(now=START + 60.0)
    r = tape(tmp_path, state.EV_SHADOW)[0]
    assert (r["w"], r["w_source"], r["w_rows"]) == (0.35, "fit", 900)
    assert r["blend_p_up"] == pytest.approx(
        0.35 * r["model_p_up"] + 0.65 * r["book_p_up"])


# --- the laws, end to end --------------------------------------------------

def test_one_clip_per_window_side_across_many_polls(tmp_path):
    """The escalation ban, exercised the way the book actually breaks it: the
    ask keeps getting cheaper, so every subsequent poll looks like a LARGER
    edge. §1.1's loss engine, and it must fire exactly once per side."""
    p = a_pilot(tmp_path)
    fired = 0
    for i, ask in enumerate((0.55, 0.50, 0.45, 0.40, 0.30, 0.20)):
        p.poller.up = books.Top(bid=ask - 0.01, bid_size=999.0, ask=ask, ask_size=999.0)
        fired += p.poll_once(now=START + 60.0 + i)
    assert fired == 1, "one clip per window-side, EVER — the ask falling is not a new decision"
    refusals = [r["refused"] for r in tape(tmp_path, state.EV_REFUSED)]
    assert refusals and set(refusals) == {risk.R_CLIP_ALREADY_FIRED}


def test_a_refused_would_be_trade_is_written_down_not_dropped(tmp_path):
    """The counterfactual is the point: the tape must say WHICH law stopped a
    trade, so a later study can price the law."""
    p = a_pilot(tmp_path)
    p.poll_once(now=START + 60.0)
    p.poll_once(now=START + 62.0)
    ref = tape(tmp_path, state.EV_REFUSED)
    assert ref and ref[0]["refused"] == risk.R_CLIP_ALREADY_FIRED
    assert ref[0]["edge"] >= policy.MIN_EDGE, "it cleared EV and was refused by risk, not by price"


def test_a_whipsawing_window_cannot_buy_both_sides_into_a_locked_loss(tmp_path):
    """The live-run finding, end to end: the model flips as the tape flips, and
    the second side would cost more to buy than the pair can ever collect."""
    p = a_pilot(tmp_path, poller=FakePoller(up_ask=0.53, dn_ask=0.53),
                stream=FakeStream(spot=99.5))
    assert p.poll_once(now=START + 60.0) == 1
    assert [r["side"] for r in tape(tmp_path, state.EV_SHADOW)] == ["down"]
    # The tape flips: now the model likes UP, at the same 0.53 ask.
    p.stream = FakeStream(spot=101.0)
    assert p.poll_once(now=START + 120.0) == 0
    refused = [r for r in tape(tmp_path, state.EV_REFUSED) if r["side"] == "up"]
    assert refused and refused[0]["refused"] == risk.R_PAIRED_LOSS
    assert refused[0]["edge"] >= policy.MIN_EDGE, "EV liked it; the arithmetic did not"


def test_no_entry_inside_the_final_thirty_seconds(tmp_path):
    p = a_pilot(tmp_path)
    assert p.poll_once(now=END - 29.0) == 0
    assert [r["refused"] for r in tape(tmp_path, state.EV_REFUSED)] == [risk.R_FINAL_SECONDS]


def test_share_cap_shapes_the_clip_at_a_cheap_ask(tmp_path):
    """A $5 clip at ask 0.05 would be 100 shares. The cap makes it 25."""
    p = a_pilot(tmp_path, poller=FakePoller(up_ask=0.05, dn_ask=0.90))
    p.poll_once(now=START + 60.0)
    r = tape(tmp_path, state.EV_SHADOW)[0]
    assert r["side"] == "up" and r["ask"] == 0.05
    assert r["shares"] == risk.MAX_SHARES_PER_WINDOW and r["capped_by"] == "shares"


def test_total_exposure_stops_the_ninth_window(tmp_path):
    p = a_pilot(tmp_path)
    for i in range(8):
        p.shadow_risk.record_fill(f"other-{i}", "up", shares=7.0,
                                  notional=risk.MAX_CLIP_USDC, ask=0.7, end=END)
    assert p.poll_once(now=START + 60.0) == 0
    assert [r["refused"] for r in tape(tmp_path, state.EV_REFUSED)] == [risk.R_TOTAL_EXPOSURE]


def test_halt_file_stops_the_pass_before_anything_is_priced(tmp_path):
    p = a_pilot(tmp_path)
    risk.halt_path(tmp_path).write_text("stop\n")
    assert p.poll_once(now=START + 60.0) == -1
    assert p.poller.requests_made == 0, "HALT is checked before the book is even read"
    assert tape(tmp_path) == []


def test_halt_file_appearing_mid_run_ends_the_loop(tmp_path):
    import threading
    p = a_pilot(tmp_path)
    stop = threading.Event()
    p.poll_once(now=START + 60.0)
    risk.halt_path(tmp_path).write_text("stop\n")
    p.run(stop, interval_s=0.0)
    evs = [r["ev"] for r in tape(tmp_path)]
    assert state.EV_HALT in evs and evs[-1] == state.EV_STOP


# --- the model refuses to guess -------------------------------------------

def test_a_stale_spot_print_prices_nothing(tmp_path):
    """5s is the predictor spec's own input contract. A stale feed is not a
    reason to price off the last thing we saw."""
    p = a_pilot(tmp_path, stream=FakeStream(stale=True))
    assert p.poll_once(now=START + 60.0) == 0
    assert p.poller.requests_made == 0, "no book read for a window we cannot price"


def test_a_missing_reference_print_prices_nothing(tmp_path):
    """The reference is the TWAP print AT window start. A dropped second there
    means this window has no reference, and inventing one is the failure the
    whole settlement-rule finding is about."""
    stream = FakeStream()
    stream.reference = lambda *_: None
    p = a_pilot(tmp_path, stream=stream)
    assert p.poll_once(now=START + 60.0) == 0


def test_an_unreadable_book_leaves_the_model_standing_alone(tmp_path):
    """60% of corpus rows have no two-sided quote. The model still has an
    opinion — but only the quoted side is tradeable."""
    poller = FakePoller()
    poller.dn = books.EMPTY
    p = a_pilot(tmp_path, poller=poller, stream=FakeStream(spot=101.5))
    p.poll_once(now=START + 60.0)
    rows = tape(tmp_path, state.EV_SHADOW)
    assert [r["side"] for r in rows] == ["up"]
    assert rows[0]["book_p_up"] is None
    assert rows[0]["blend_p_up"] == rows[0]["model_p_up"]


# --- window lifecycle ------------------------------------------------------

def test_window_summary_and_calibration_row_are_written_at_close(tmp_path):
    p = a_pilot(tmp_path)
    p.poll_once(now=START + 60.0)
    p.poll_once(now=END + 1.0)
    summary = tape(tmp_path, state.EV_WINDOW)
    assert len(summary) == 1 and summary[0]["slug"] == SLUG
    assert summary[0]["fired"] == 1 and summary[0]["ev_pass"] >= 1
    calib = list(state.iter_records(state.CALIB, tmp_path))
    assert len(calib) == 1, "one blend-fit sample per WINDOW, not per clip (L34)"
    assert calib[0]["slug"] == SLUG and math.isfinite(calib[0]["book_p_up"])


def test_settled_exposure_is_released_so_the_next_window_can_use_it(tmp_path):
    p = a_pilot(tmp_path)
    p.poll_once(now=START + 60.0)
    assert p.shadow_risk.exposure_used > 0
    p.poll_once(now=END + service.SETTLE_GRACE_S + 1.0)
    assert p.shadow_risk.exposure_used == 0.0
    assert p.shadow_risk.has_fired(SLUG, "up"), "released is not re-openable"


# --- live wiring (still no network) ---------------------------------------

class RecordingClient:
    def __init__(self):
        self.orders = []

    def get_tick_size(self, token):
        return "0.01"


def test_live_mode_places_exactly_one_clip_and_books_it_before_sending(tmp_path, monkeypatch):
    sent = []

    def fake_place(client, plan, tick_size=None):
        sent.append(plan)
        return {"success": True, "status": "matched", "takingAmount": "5", "orderID": "abc"}

    monkeypatch.setattr(service.execution, "place", fake_place)
    w = a_window(series_key="doge-updown-5m")
    p = service.Pilot(home=tmp_path, live=True, live_series=["doge-updown-5m"],
                      shadow_series=[], stream=FakeStream(), poller=FakePoller(),
                      cache=FakeCache(w), clob_client=RecordingClient(), log=lambda *_: None)
    assert p.poll_once(now=START + 60.0) == 1
    assert p.poll_once(now=START + 62.0) == 0, "one clip per window-side, live too"
    assert len(sent) == 1 and sent[0].side == "up" and sent[0].price == 0.55
    live = list(state.iter_records(state.LIVE_TAPE, tmp_path))
    assert [r["ev"] for r in live][:2] == [state.EV_ORDER, state.EV_ACK]
    assert live[1]["filled"] == 5.0


def test_a_failed_send_still_spends_the_clip(tmp_path, monkeypatch):
    """No retry path. A retry is how a one-clip rule becomes a five-clip
    window, and §1.1 prices that at -9.48% RoN."""
    def boom(client, plan, tick_size=None):
        raise RuntimeError("clob said no")

    monkeypatch.setattr(service.execution, "place", boom)
    w = a_window()
    p = service.Pilot(home=tmp_path, live=True, live_series=["doge-updown-5m"],
                      shadow_series=[], stream=FakeStream(), poller=FakePoller(),
                      cache=FakeCache(w), clob_client=RecordingClient(), log=lambda *_: None)
    p.poll_once(now=START + 60.0)
    p.poll_once(now=START + 62.0)
    errors = [r for r in state.iter_records(state.LIVE_TAPE, tmp_path, evs=(state.EV_ERROR,))]
    assert len(errors) == 1
    assert p.live_risk.has_fired(SLUG, "up"), "the clip is spent even though the send failed"


def test_halt_blocks_the_order_even_if_it_appears_after_the_pass_started(tmp_path, monkeypatch):
    def fake_place(client, plan, tick_size=None):
        raise AssertionError("an order left the process while HALT was present")

    monkeypatch.setattr(service.execution, "place", fake_place)
    w = a_window()
    p = service.Pilot(home=tmp_path, live=True, live_series=["doge-updown-5m"],
                      shadow_series=[], stream=FakeStream(), poller=FakePoller(),
                      cache=FakeCache(w), clob_client=RecordingClient(), log=lambda *_: None)
    # Simulate the file landing between the loop-top check and the send.
    original = p._fire_live

    def wrapped(*a, **kw):
        risk.halt_path(tmp_path).write_text("stop\n")
        return original(*a, **kw)

    p._fire_live = wrapped
    assert p.poll_once(now=START + 60.0) == 0
    refused = [r for r in state.iter_records(state.LIVE_TAPE, tmp_path, evs=(state.EV_REFUSED,))]
    assert refused and refused[0]["refused"] == risk.R_HALT


def test_shadow_and_live_keep_separate_exposure_ledgers(tmp_path, monkeypatch):
    monkeypatch.setattr(service.execution, "place", lambda *a, **kw: {"success": True})
    w = a_window()
    p = service.Pilot(home=tmp_path, live=True, live_series=["doge-updown-5m"],
                      shadow_series=[], stream=FakeStream(), poller=FakePoller(),
                      cache=FakeCache(w), clob_client=RecordingClient(), log=lambda *_: None)
    for i in range(8):
        p.shadow_risk.record_fill(f"paper-{i}", "up", shares=7.0,
                                  notional=risk.MAX_CLIP_USDC, ask=0.7, end=END)
    assert p.poll_once(now=START + 60.0) == 1, "paper inventory must not spend live budget"


def test_a_live_position_is_queued_for_the_manual_sweep_at_settlement(tmp_path, monkeypatch):
    monkeypatch.setattr(service.execution, "place",
                        lambda *a, **kw: {"success": True, "takingAmount": "9"})
    w = a_window()
    p = service.Pilot(home=tmp_path, live=True, live_series=["doge-updown-5m"],
                      shadow_series=[], stream=FakeStream(), poller=FakePoller(),
                      cache=FakeCache(w), clob_client=RecordingClient(), log=lambda *_: None)
    p.poll_once(now=START + 60.0)
    p.poll_once(now=END + service.SETTLE_GRACE_S + 1.0)
    queued = list(state.iter_records(state.REDEEM_QUEUE, tmp_path))
    assert len(queued) == 1 and queued[0]["slug"] == SLUG and queued[0]["token"] == "TOK_UP"


def test_no_key_material_ever_reaches_a_tape(tmp_path, monkeypatch):
    """The order path holds the key only inside the client. Nothing it writes
    may contain it."""
    monkeypatch.setenv("PM_PRIVATE_KEY", "0xdeadbeefcafe")
    monkeypatch.setattr(service.execution, "place",
                        lambda *a, **kw: {"success": True, "takingAmount": "5",
                                          "signature": "0xdeadbeefcafe_SIGNATURE"})
    w = a_window()
    p = service.Pilot(home=tmp_path, live=True, live_series=["doge-updown-5m"],
                      shadow_series=[], stream=FakeStream(), poller=FakePoller(),
                      cache=FakeCache(w), clob_client=RecordingClient(), log=lambda *_: None)
    p.poll_once(now=START + 60.0)
    for name in (state.LIVE_TAPE, state.SHADOW_TAPE):
        path = tmp_path / name
        if path.exists():
            body = path.read_text()
            assert "deadbeef" not in body, f"{name} carries something key-shaped"
            for line in body.splitlines():
                json.loads(line)  # every line is a whole record, not a torn one
