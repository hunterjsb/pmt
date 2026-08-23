"""The tape folds behind `pmt crypto stats` — arm flags, the resting-bid
experiment, the order path, the fleet ration.

Every function under test is pure, so these run with no engine, no wallet and
no ~/.pmt: hand in the record shapes the engine actually writes (see
pmengine/src/strategies/updown.rs and src/order_tape.rs) and assert on the
fold. Empty inputs are tested first and everywhere, because a fresh box has
no tapes at all and the report must simply omit the block.
"""

from __future__ import annotations

from polymarket import updown_stats as us

SLUG = "btc-updown-5m-1787452500"
OTHER = "eth-updown-15m-1787452500"


def _eval(slug=SLUG, t=1787452600.0, sides=(), **kw) -> dict:
    return {"ev": "eval", "slug": slug, "t": t, "sides": list(sides), **kw}


def _buy(slug=SLUG, outcome="Down", price=0.97, usd=25.0, t=1787452700.0) -> dict:
    return {"type": "TRADE", "side": "BUY", "slug": slug, "outcome": outcome,
            "price": price, "usdcSize": usd, "size": usd / price, "timestamp": t}


# ---------- empty everything ----------

def test_every_fold_survives_a_box_with_no_tapes():
    # A fresh machine has no ~/.pmt/engine at all; tape.iter_records yields
    # nothing and each fold must answer "nothing", never raise.
    assert us.arm_flags(None) == {}
    m = us.maker_summary([], [], [], [])
    assert m["candidates"] == 0 and m["rested"] == 0 and m["fills"] == 0
    assert m["pnl"] is None
    c = us.chase_summary([], [])
    assert c["acks"] == 0 and c["suppressed_share"] is None
    assert c["ack_p50"] is None and c["buffer_med_c"] is None
    f = us.fleet_summary([], None)
    assert f["cap"] == 0.0 and f["peak_undecided"] is None


# ---------- arm flags ----------

def test_arm_flags_key_by_series_and_default_to_the_taker_binance_arm():
    flags = us.arm_flags({
        SLUG: {"feed": "binance", "maker_bid": False},
        OTHER: {"feed": "rtds", "maker_bid": True},
        "xrp-updown-5m-1787452500": {},  # a pre-field engine reply
    })
    assert flags["btc 5m"] == {"feed": "binance", "maker_bid": False}
    assert flags["eth 15m"] == {"feed": "rtds", "maker_bid": True}
    assert flags["xrp 5m"] == {"feed": "binance", "maker_bid": False}


def test_arm_flags_ignores_anything_that_is_not_an_updown_arm():
    assert us.arm_flags({"some-other-market": {"feed": "rtds"}, SLUG: None}) == {}


# ---------- the resting-bid experiment ----------

def test_maker_counts_the_shadow_class_separately_from_real_rested_bids():
    # maker_candidate is written with the knob OFF — it prices the miss class
    # before any capital rides on it, and must never be counted as a bid.
    evals = [_eval(sides=[{"side": "down", "maker_candidate": True}]),
             _eval(slug=OTHER, sides=[{"side": "up", "maker_candidate": True}]),
             _eval(t=1787452650.0, sides=[{"side": "down", "maker_rest": 0.97}])]
    m = us.maker_summary(evals, [], [], [])
    assert m["candidates"] == 2 and m["candidate_windows"] == 2
    assert m["rested"] == 1 and m["rested_windows"] == 1
    assert m["fills"] == 0


def test_maker_attributes_a_buy_at_or_below_the_price_we_rested_at():
    evals = [_eval(t=1000.0, sides=[{"side": "down", "maker_rest": 0.97}])]
    graded = [{"slug": SLUG, "won": True, "pnl": 1.95}]
    m = us.maker_summary(evals, [], [_buy(price=0.97, t=1100.0)], graded)
    assert m["fills"] == 1 and m["fill_windows"] == 1
    assert m["wins"] == 1 and m["losses"] == 0
    assert m["pnl"] == 1.95


def test_maker_refuses_a_buy_that_paid_more_than_the_resting_bid():
    # A taker clip that crossed above our quote is not our quote filling.
    evals = [_eval(t=1000.0, sides=[{"side": "down", "maker_rest": 0.97}])]
    m = us.maker_summary(evals, [], [_buy(price=0.99, t=1100.0)], [])
    assert m["fills"] == 0


def test_maker_refuses_a_buy_on_the_other_side_or_before_we_rested():
    evals = [_eval(t=1000.0, sides=[{"side": "down", "maker_rest": 0.97}])]
    early = _buy(price=0.95, t=900.0)
    wrong_side = _buy(outcome="Up", price=0.95, t=1100.0)
    assert us.maker_summary(evals, [], [early, wrong_side], [])["fills"] == 0


def test_maker_bar_is_the_best_price_ever_rested_on_that_side():
    # A re-quote up the grid raises the bar; a fill at the older, lower price
    # still belongs to the experiment.
    evals = [_eval(t=1000.0, sides=[{"side": "down", "maker_rest": 0.95}]),
             _eval(t=1050.0, sides=[{"side": "down", "maker_rest": 0.98}])]
    m = us.maker_summary(evals, [], [_buy(price=0.97, t=1100.0)], [])
    assert m["rested"] == 2 and m["fills"] == 1


def test_maker_placed_counts_only_post_only_acks():
    orders = [{"stage": "ack", "post_only": True},
              {"stage": "ack", "post_only": True},
              {"stage": "ack"},                      # a taker clip
              {"stage": "suppressed", "post_only": True}]  # never reached the book
    assert us.maker_summary([], orders, [], [])["placed"] == 2


def test_maker_pnl_is_none_rather_than_zero_when_nothing_graded_yet():
    # A filled window still riding has no P&L; zero would read as break-even.
    evals = [_eval(t=1000.0, sides=[{"side": "down", "maker_rest": 0.97}])]
    m = us.maker_summary(evals, [], [_buy(t=1100.0)], [])
    assert m["fills"] == 1 and m["pnl"] is None


# ---------- the order path ----------

def test_chase_splits_acks_from_the_delta_matchers_suppressions():
    orders = [{"stage": "ack", "ack_ms": 100.0, "sign_done_ms": 0.2},
              {"stage": "ack", "ack_ms": 300.0, "sign_done_ms": 0.2},
              {"stage": "suppressed"}]
    c = us.chase_summary(orders, [])
    assert c["acks"] == 2 and c["suppressed"] == 1
    assert abs(c["suppressed_share"] - 1 / 3) < 1e-9
    assert c["ack_p50"] == 300.0  # nearest-rank: an observed value, not a mean


def test_chase_measures_the_pay_up_buffer_against_the_ask_it_chased_from():
    fires = [{"ask": 0.94, "limit": 0.96},   # 2c of chase
             {"ask": 0.98, "limit": 0.98},   # priced, chased nothing
             {"ask": 0.97}]                  # pre-limit era: unknown, not zero
    c = us.chase_summary([], fires)
    assert c["chase_n"] == 2 and c["chased"] == 1
    assert abs(c["buffer_max_c"] - 2.0) < 1e-9


def test_chase_never_counts_a_pre_limit_fire_as_a_zero_buffer():
    c = us.chase_summary([], [{"ask": 0.97}, {"ask": 0.95}])
    assert c["chase_n"] == 0 and c["buffer_med_c"] is None


# ---------- the fleet ration ----------

def test_fleet_peak_is_the_cap_minus_the_least_headroom_ever_seen():
    evals = [_eval(fleet_room=350.0), _eval(fleet_room=120.0), _eval(fleet_room=200.0)]
    f = us.fleet_summary(evals, 350.0)
    assert f["ticks"] == 3 and f["peak_undecided"] == 230.0


def test_fleet_room_absent_means_the_cap_was_off_not_that_exposure_was_zero():
    # The engine only writes fleet_room when a cap is set — an uncapped fleet
    # has infinite room, which is not a number any consumer should see.
    f = us.fleet_summary([_eval(), _eval()], 0.0)
    assert f["ticks"] == 0 and f["peak_undecided"] is None


def test_fleet_sums_what_the_cap_actually_refused():
    evals = [_eval(sides=[{"side": "up", "brake": "fleet", "fleet_blocked": 60.0},
                           {"side": "down", "brake": "safety", "fleet_blocked": 99.0}]),
             _eval(sides=[{"side": "up", "brake": "fleet", "fleet_blocked": 40.88}])]
    assert abs(us.fleet_summary(evals, 350.0)["blocked_usd"] - 100.88) < 1e-9
