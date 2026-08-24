"""Pure tests for the shadow P&L ledger. No network — everything here is
inline fixtures (tape lines, activity rows, winner maps).
"""

import json

from polymarket.shadow import (
    CATEGORY_ORDER,
    build_report,
    basis_guard_side,
    categorize_ticks,
    classify_sub_threshold,
    collapse_episodes,
    fee,
    iter_fires,
    iter_ticks,
    price_episode,
    shadow_value,
    summarize_categories,
    unfilled_episodes,
    verdict,
    wallet_fill_notional,
    window_clip_notional,
)


# ---------- basis-guard reason parsing ----------

def test_basis_guard_side_positive_margin_is_up():
    reason = "basis guard: projected margin +4.5bp inside 6.0bp noise band [banked +0.0bp cushion 21.8bp]"
    assert basis_guard_side(reason) == "up"


def test_basis_guard_side_negative_margin_is_down():
    reason = "basis guard: projected margin -5.6bp inside 6.0bp noise band [banked -0.0bp cushion 21.2bp]"
    assert basis_guard_side(reason) == "down"


def test_basis_guard_side_zero_margin_is_up():
    reason = "basis guard: projected margin +0.0bp inside 6.0bp noise band"
    assert basis_guard_side(reason) == "up"


def test_basis_guard_side_none_for_non_basis_guard_reason():
    assert basis_guard_side("feed stale") is None
    assert basis_guard_side("window 45% elapsed") is None  # pre-R9 clock gate


def test_basis_guard_side_none_when_unparseable():
    assert basis_guard_side("basis guard: something changed") is None


def test_basis_guard_side_prefers_the_structured_margin():
    # Reworded past the regex; the engine's margin_bp field still decides.
    assert basis_guard_side("basis guard: nope", -5.6) == "down"
    assert basis_guard_side("basis guard: nope", 4.5) == "up"
    assert basis_guard_side("basis guard: nope", 0.0) == "up"


def test_basis_guard_side_structured_margin_does_not_rescue_a_non_guard_reason():
    # A stale-feed gate is not a side signal no matter what rides with it.
    assert basis_guard_side("feed stale", -5.6) is None


def test_iter_ticks_reads_the_structured_margin_off_a_gated_line():
    line = json.dumps({
        "t": 100.0, "ev": "gated", "slug": "btc-updown-15m-100",
        "reason": "basis guard: reworded past the regex",
        "margin_bp": -5.6, "guard_bp": 6.0, "up_ask": 0.55, "dn_ask": 0.47,
    })
    ticks = list(iter_ticks([line]))
    assert [t["side"] for t in ticks] == ["down"]
    assert ticks[0]["ask"] == 0.47


# ---------- fee / shadow value ----------

def test_fee_symmetric_around_50c():
    assert abs(fee(0.3) - fee(0.7)) < 1e-12


def test_fee_matches_taker_fee_shape():
    # 0.07 * ask * (1-ask), same formula as crypto.taker_fee() — the shape
    # the wallet actually charged, not the old min(ask, 1-ask).
    assert abs(fee(0.85) - 0.07 * 0.85 * 0.15) < 1e-12
    assert abs(fee(0.50) - 0.0175) < 1e-12


def test_shadow_value_win_is_positive_and_net_of_fee():
    # clip $25 at ask 0.85 -> ~29.41 shares, pay $1 each net of fee
    pnl = shadow_value(0.85, 25.0, won=True)
    shares = 25.0 / 0.85
    expected = shares * (1.0 - 0.85 - fee(0.85))
    assert abs(pnl - expected) < 1e-9
    assert pnl > 0


def test_shadow_value_loss_is_the_whole_clip():
    assert shadow_value(0.85, 25.0, won=False) == -25.0


# ---------- tick extraction ----------

def _gated(t, slug, reason, up_ask=None, dn_ask=None):
    r = {"ev": "gated", "t": t, "slug": slug, "reason": reason}
    if up_ask is not None or dn_ask is not None:
        r["up_ask"], r["dn_ask"] = up_ask, dn_ask
    return json.dumps(r)


def _eval(t, slug, sides):
    return json.dumps({"ev": "eval", "t": t, "slug": slug, "p_up": 0.5, "sides": sides})


def _fire(t, slug, side, ask, size):
    return json.dumps({"ev": "fire", "t": t, "slug": slug, "side": side, "ask": ask, "size": size})


def test_iter_ticks_basis_guard_with_asks():
    lines = [_gated(100.0, "btc-updown-5m-1000",
                     "basis guard: projected margin +4.5bp inside 6.0bp noise band",
                     up_ask=0.7, dn_ask=0.31)]
    ticks = list(iter_ticks(lines))
    assert len(ticks) == 1
    tk = ticks[0]
    assert tk["side"] == "up" and tk["category"] == "basis_guard" and tk["ask"] == 0.7


def test_iter_ticks_basis_guard_missing_asks_is_unpriced_tick():
    # old-style gated record with no up_ask/dn_ask key at all
    line = json.dumps({"ev": "gated", "t": 100.0, "slug": "btc-updown-5m-1000",
                        "reason": "basis guard: projected margin -3.0bp inside 6.0bp noise band"})
    ticks = list(iter_ticks([line]))
    assert len(ticks) == 1
    assert ticks[0]["side"] == "down" and ticks[0]["ask"] is None


def test_iter_ticks_skips_feed_stale_and_clock_gated():
    lines = [
        _gated(100.0, "btc-updown-5m-1000", "feed stale"),
        _gated(101.0, "btc-updown-5m-1000", "window 45% elapsed"),
    ]
    assert list(iter_ticks(lines)) == []


def test_iter_ticks_eval_side_with_brake():
    sides = [{"side": "up", "ask": 0.15, "fair": 0.02, "net": -0.13, "brake": "safety"},
             {"side": "down", "ask": 0.85, "fair": 0.98, "net": 0.12}]
    ticks = list(iter_ticks([_eval(100.0, "eth-updown-15m-2000", sides)]))
    by_side = {tk["side"]: tk for tk in ticks}
    assert by_side["up"]["category"] == "safety"
    assert by_side["down"]["category"] is None  # no brake -- caller classifies


def test_iter_ticks_respects_since_floor():
    lines = [_gated(100.0, "s", "basis guard: projected margin +1.0bp inside 6.0bp", up_ask=0.6, dn_ask=0.4)]
    assert list(iter_ticks(lines, since=200.0)) == []
    assert len(list(iter_ticks(lines, since=50.0))) == 1


def test_iter_ticks_tolerant_of_junk_lines():
    lines = ["", "not json", '{"ev": "cleanup", "t": 1, "slug": "s"}']
    assert list(iter_ticks(lines)) == []


def test_iter_fires_extracts_basic_fields():
    lines = [_fire(100.0, "btc-updown-5m-1000", "down", 0.92, 31.0)]
    fires = list(iter_fires(lines))
    assert fires == [{"t": 100.0, "slug": "btc-updown-5m-1000", "side": "down", "ask": 0.92, "size": 31.0}]


def test_iter_fires_ignores_non_fire_events():
    lines = [_eval(100.0, "s", [{"side": "up", "ask": 0.5, "fair": 0.5, "net": 0.0}])]
    assert list(iter_fires(lines)) == []


# ---------- sub_threshold classification ----------

def test_classify_sub_threshold_low_fair():
    tick = {"fair": 0.9, "net": 0.05}
    assert classify_sub_threshold(tick, min_fair=0.97, min_edge=0.015) is True


def test_classify_sub_threshold_low_edge():
    tick = {"fair": 0.99, "net": 0.005}
    assert classify_sub_threshold(tick, min_fair=0.97, min_edge=0.015) is True


def test_classify_sub_threshold_clears_both_bars():
    tick = {"fair": 0.99, "net": 0.05}
    assert classify_sub_threshold(tick, min_fair=0.97, min_edge=0.015) is False


def test_classify_sub_threshold_missing_data_is_false():
    assert classify_sub_threshold({"fair": None, "net": None}, 0.97, 0.015) is False


def test_categorize_ticks_drops_unbraked_sides_that_clear_thresholds():
    raw = [
        {"t": 1, "slug": "s", "side": "up", "category": None, "ask": 0.98, "fair": 0.99, "net": 0.05},
        {"t": 1, "slug": "s", "side": "down", "category": None, "ask": 0.05, "fair": 0.01, "net": -0.1},
    ]
    out = categorize_ticks(raw, min_fair=0.97, min_edge=0.015)
    assert len(out) == 1
    assert out[0]["side"] == "down" and out[0]["category"] == "sub_threshold"


def test_categorize_ticks_preserves_brake_category():
    raw = [{"t": 1, "slug": "s", "side": "up", "category": "latched", "ask": 0.2, "fair": 0.02, "net": -0.1}]
    out = categorize_ticks(raw)
    assert out[0]["category"] == "latched"


# ---------- episode collapsing ----------

def test_collapse_episodes_groups_continuous_ticks():
    ticks = [
        {"t": 100.0, "slug": "s", "side": "up", "category": "safety", "ask": 0.2, "fair": 0.1, "net": -0.1},
        {"t": 105.0, "slug": "s", "side": "up", "category": "safety", "ask": 0.18, "fair": 0.1, "net": -0.08},
        {"t": 110.0, "slug": "s", "side": "up", "category": "safety", "ask": 0.22, "fair": 0.1, "net": -0.1},
    ]
    eps = collapse_episodes(ticks)
    assert len(eps) == 1
    ep = eps[0]
    assert ep["n_ticks"] == 3 and ep["best_ask"] == 0.18
    assert ep["start"] == 100.0 and ep["end"] == 110.0


def test_collapse_episodes_splits_on_gap():
    ticks = [
        {"t": 100.0, "slug": "s", "side": "up", "category": "safety", "ask": 0.2, "fair": None, "net": None},
        {"t": 105.0, "slug": "s", "side": "up", "category": "safety", "ask": 0.2, "fair": None, "net": None},
        {"t": 200.0, "slug": "s", "side": "up", "category": "safety", "ask": 0.3, "fair": None, "net": None},
    ]
    eps = sorted(collapse_episodes(ticks), key=lambda e: e["start"])
    assert len(eps) == 2
    assert eps[0]["n_ticks"] == 2
    assert eps[1]["n_ticks"] == 1 and eps[1]["best_ask"] == 0.3


def test_collapse_episodes_separates_by_side_and_category():
    ticks = [
        {"t": 100.0, "slug": "s", "side": "up", "category": "safety", "ask": 0.2, "fair": None, "net": None},
        {"t": 100.0, "slug": "s", "side": "down", "category": "safety", "ask": 0.8, "fair": None, "net": None},
        {"t": 100.0, "slug": "s", "side": "up", "category": "latched", "ask": 0.25, "fair": None, "net": None},
    ]
    eps = collapse_episodes(ticks)
    assert len(eps) == 3


def test_collapse_episodes_all_unpriced_leaves_best_ask_none():
    ticks = [{"t": 100.0, "slug": "s", "side": "up", "category": "basis_guard", "ask": None,
              "fair": None, "net": None}]
    eps = collapse_episodes(ticks)
    assert eps[0]["best_ask"] is None


# ---------- clip sizing ----------

def test_window_clip_notional_uses_median_of_fires():
    fires = [{"size": 322.0, "ask": 0.92}, {"size": 31.0, "ask": 0.92}, {"size": 9.0, "ask": 0.98}]
    notionals = sorted(f["size"] * f["ask"] for f in fires)
    assert window_clip_notional(fires) == notionals[1]


def test_window_clip_notional_default_when_no_fires():
    assert window_clip_notional([], default=25.0) == 25.0


# ---------- pricing ----------

def test_price_episode_unpriced_when_no_ask():
    ep = {"slug": "s", "side": "up", "category": "basis_guard", "best_ask": None}
    out = price_episode(ep, winner="up", clip_notional=25.0)
    assert out["status"] == "unpriced" and out["pnl"] is None


def test_price_episode_unresolved_when_no_winner():
    ep = {"slug": "s", "side": "up", "category": "basis_guard", "best_ask": 0.7}
    out = price_episode(ep, winner=None, clip_notional=25.0)
    assert out["status"] == "unresolved" and out["pnl"] is None


def test_price_episode_priced_win():
    ep = {"slug": "s", "side": "up", "category": "basis_guard", "best_ask": 0.7}
    out = price_episode(ep, winner="up", clip_notional=25.0)
    assert out["status"] == "priced" and out["won"] is True and out["pnl"] > 0


def test_price_episode_priced_loss():
    ep = {"slug": "s", "side": "up", "category": "basis_guard", "best_ask": 0.7}
    out = price_episode(ep, winner="down", clip_notional=25.0)
    assert out["status"] == "priced" and out["won"] is False and out["pnl"] == -25.0


# ---------- unfilled fires ----------

def test_unfilled_episodes_winning_side_partial_fill_is_missed_win():
    fires = [{"t": 1.0, "slug": "s", "side": "down", "ask": 0.9, "size": 50.0}]  # intended $45
    fills = {("s", "down"): 20.0}  # only $20 actually filled
    winners = {"s": "down"}
    eps = unfilled_episodes(fires, fills, winners)
    assert len(eps) == 1
    ep = eps[0]
    assert ep["status"] == "priced" and ep["won"] is True and ep["pnl"] > 0


def test_unfilled_episodes_losing_side_partial_fill_is_avoided_loss():
    fires = [{"t": 1.0, "slug": "s", "side": "up", "ask": 0.9, "size": 50.0}]  # intended $45
    fills = {("s", "up"): 20.0}
    winners = {"s": "down"}
    eps = unfilled_episodes(fires, fills, winners)
    ep = eps[0]
    assert ep["status"] == "priced" and ep["won"] is False and ep["pnl"] < 0


def test_unfilled_episodes_fully_filled_yields_no_episode():
    fires = [{"t": 1.0, "slug": "s", "side": "up", "ask": 0.9, "size": 50.0}]  # $45 intended
    fills = {("s", "up"): 45.0}
    assert unfilled_episodes(fires, fills, {"s": "up"}) == []


def test_unfilled_episodes_unresolved_window():
    fires = [{"t": 1.0, "slug": "s", "side": "up", "ask": 0.9, "size": 50.0}]
    fills = {}
    eps = unfilled_episodes(fires, fills, {})
    assert eps[0]["status"] == "unresolved"


def test_wallet_fill_notional_sums_buys_by_side():
    rows = [
        {"type": "TRADE", "side": "BUY", "slug": "btc-updown-5m-100", "outcome": "Up", "usdcSize": 10.0},
        {"type": "TRADE", "side": "BUY", "slug": "btc-updown-5m-100", "outcome": "Up", "usdcSize": 5.0},
        {"type": "TRADE", "side": "SELL", "slug": "btc-updown-5m-100", "outcome": "Up", "usdcSize": 3.0},
        {"type": "TRADE", "side": "BUY", "slug": "not-updown", "outcome": "Up", "usdcSize": 99.0},
    ]
    assert wallet_fill_notional(rows) == {("btc-updown-5m-100", "up"): 15.0}


# ---------- rollups ----------

def test_summarize_categories_computes_net_and_hit_rate():
    episodes = [
        {"category": "safety", "status": "priced", "won": True, "pnl": 10.0},
        {"category": "safety", "status": "priced", "won": False, "pnl": -25.0},
        {"category": "safety", "status": "unresolved", "won": None, "pnl": None},
        {"category": "safety", "status": "unpriced", "won": None, "pnl": None},
    ]
    out = summarize_categories(episodes)
    s = out["safety"]
    assert s["episodes"] == 4 and s["priced"] == 2 and s["unresolved"] == 1 and s["unpriced"] == 1
    assert s["missed_wins"] == 10.0 and s["avoided_losses"] == 25.0
    assert s["net"] == -15.0
    assert s["hit_rate"] == 0.5


def test_summarize_categories_no_priced_hit_rate_is_none():
    episodes = [{"category": "basis_guard", "status": "unresolved", "won": None, "pnl": None}]
    out = summarize_categories(episodes)
    assert out["basis_guard"]["hit_rate"] is None


def test_verdict_paying_for_itself_when_avoided_exceeds_missed():
    s = {"priced": 2, "net": -15.0}
    assert verdict(s) == "paying for itself"


def test_verdict_over_tight_when_missed_exceeds_avoided():
    s = {"priced": 2, "net": 15.0}
    assert verdict(s) == "over-tight"


def test_verdict_no_priced_episodes():
    assert verdict({"priced": 0, "net": 0.0}) == "no priced episodes"


def test_category_order_matches_spec():
    assert CATEGORY_ORDER == ("basis_guard", "safety", "latched", "distrust",
                               "avg_down", "sub_threshold", "unfilled_fires")


# ---------- end-to-end build_report ----------

def test_build_report_end_to_end_small_fixture():
    lines = [
        # a basis_guard refusal on "up" (positive margin), 5s apart -> one episode
        _gated(100.0, "btc-updown-5m-1000",
               "basis guard: projected margin +4.5bp inside 6.0bp noise band", up_ask=0.7, dn_ask=0.31),
        _gated(105.0, "btc-updown-5m-1000",
               "basis guard: projected margin +5.0bp inside 6.0bp noise band", up_ask=0.68, dn_ask=0.33),
        # a safety-braked down side, and an unbraked up side that's also
        # sub-threshold (fair 0.6 < default min_fair 0.97)
        _eval(120.0, "btc-updown-5m-1000",
              [{"side": "up", "ask": 0.7, "fair": 0.6, "net": -0.15},
               {"side": "down", "ask": 0.3, "fair": 0.4, "net": 0.09, "brake": "safety"}]),
        # a real fire on "up" that only partially filled per wallet activity below
        _fire(150.0, "btc-updown-5m-1000", "up", 0.7, 20.0),  # $14 intended
    ]
    activity = [
        {"type": "TRADE", "side": "BUY", "slug": "btc-updown-5m-1000", "outcome": "Up", "usdcSize": 7.0},
    ]
    winners = {"btc-updown-5m-1000": "up"}  # "up" won -> basis_guard episode was a missed win,
                                             # safety brake (on "down") avoided a loss,
                                             # unfilled "up" remainder is a missed win too

    report = build_report(lines, winners, activity, since=0.0)
    cats = report["categories"]

    assert cats["basis_guard"]["episodes"] == 1
    assert cats["basis_guard"]["priced"] == 1
    assert cats["basis_guard"]["missed_wins"] > 0
    assert cats["basis_guard"]["avoided_losses"] == 0

    assert cats["safety"]["episodes"] == 1
    assert cats["safety"]["avoided_losses"] > 0
    assert cats["safety"]["missed_wins"] == 0

    # unbraked "up" side at fair 0.6 (< default min_fair 0.97) never fired on
    # its own merits either -- "up" is the actual winner, so this
    # sub_threshold episode is also a missed win
    assert cats["sub_threshold"]["episodes"] == 1
    assert cats["sub_threshold"]["missed_wins"] > 0

    assert cats["unfilled_fires"]["episodes"] == 1
    assert cats["unfilled_fires"]["missed_wins"] > 0

    assert report["totals"]["episodes"] == 4
    assert report["coverage"]["windows"] == 1
    assert report["coverage"]["unpriced_episodes"] == 0
    assert report["coverage"]["skipped_unresolved"] == 0


def test_build_report_unresolved_window_shows_as_coverage_gap_not_zero():
    lines = [_gated(100.0, "btc-updown-5m-1000",
                     "basis guard: projected margin +4.5bp inside 6.0bp", up_ask=0.7, dn_ask=0.31)]
    report = build_report(lines, winners={}, activity_rows=[], since=0.0)
    assert report["totals"]["missed_wins"] == 0.0
    assert report["totals"]["avoided_losses"] == 0.0
    assert report["coverage"]["skipped_unresolved"] == 1


def test_build_report_since_filters_old_ticks():
    lines = [_gated(50.0, "s", "basis guard: projected margin +1.0bp", up_ask=0.6, dn_ask=0.4)]
    report = build_report(lines, winners={"s": "up"}, activity_rows=[], since=100.0)
    assert report["totals"]["episodes"] == 0


# ---------- the ask a counterfactual clip pays (bughunt 2026-08-24) ----------
#
# The ledger used to price every episode at its LOWEST recorded ask. A win pays
# clip/ask shares and a loss is -clip flat, so that choice inflates missed_wins
# and cannot move avoided_losses at all — a one-directional thumb on the scale
# that made every gate read "over-tight". These pin the entry-ask convention
# and the asymmetry that motivates it.

def test_episode_carries_entry_ask_and_best_ask_separately():
    ticks = [
        {"t": 100.0, "slug": "s", "side": "up", "category": "safety", "ask": 0.60, "fair": None, "net": None},
        {"t": 105.0, "slug": "s", "side": "up", "category": "safety", "ask": 0.10, "fair": None, "net": None},
        {"t": 110.0, "slug": "s", "side": "up", "category": "safety", "ask": 0.40, "fair": None, "net": None},
    ]
    ep = collapse_episodes(ticks)[0]
    assert ep["entry_ask"] == 0.60, "entry ask is the book at the moment of refusal"
    assert ep["best_ask"] == 0.10, "best ask is still carried, for diagnostics only"


def test_episode_entry_ask_is_chronological_not_list_order():
    # ticks handed in newest-first still report the OLDEST ask as entry
    ticks = [
        {"t": 110.0, "slug": "s", "side": "up", "category": "safety", "ask": 0.40, "fair": None, "net": None},
        {"t": 100.0, "slug": "s", "side": "up", "category": "safety", "ask": 0.60, "fair": None, "net": None},
    ]
    assert collapse_episodes(ticks)[0]["entry_ask"] == 0.60


def test_price_episode_prices_at_entry_ask_not_the_episode_minimum():
    ep = {"slug": "s", "side": "up", "category": "safety",
          "entry_ask": 0.50, "best_ask": 0.05}
    out = price_episode(ep, winner="up", clip_notional=25.0)
    # $25 at 0.50 buys 50 shares; at the 0.05 minimum it would buy 500 and book
    # roughly ten times the "missed win".
    assert out["pnl"] == shadow_value(0.50, 25.0, True)
    assert out["pnl"] < shadow_value(0.05, 25.0, True) / 5.0


def test_the_ask_convention_moves_missed_wins_but_never_avoided_losses():
    """The asymmetry the fix exists for, stated as an invariant."""
    win_at_entry = shadow_value(0.50, 25.0, True)
    win_at_min = shadow_value(0.05, 25.0, True)
    loss_at_entry = shadow_value(0.50, 25.0, False)
    loss_at_min = shadow_value(0.05, 25.0, False)
    assert win_at_min > win_at_entry, "a lower ask inflates a missed win"
    assert loss_at_min == loss_at_entry == -25.0, "a loss is -clip at any ask"


def test_unfilled_episode_entry_and_best_ask_are_the_same_volume_weighted_ask():
    fires = [{"t": 1.0, "slug": "s", "side": "up", "ask": 0.9, "size": 50.0}]
    ep = unfilled_episodes(fires, fills={("s", "up"): 9.0}, winners={"s": "up"})[0]
    assert ep["entry_ask"] == ep["best_ask"] == 0.9


def test_price_episode_still_prices_an_episode_built_without_an_entry_ask():
    # older callers hand in best_ask alone; they must not become "unpriced"
    ep = {"slug": "s", "side": "up", "category": "basis_guard", "best_ask": 0.7}
    out = price_episode(ep, winner="up", clip_notional=25.0)
    assert out["status"] == "priced" and out["pnl"] == shadow_value(0.7, 25.0, True)


def test_build_report_verdict_follows_the_entry_ask_through_a_falling_book():
    """End-to-end: a refused side whose ask collapses mid-episode. Priced at
    the minimum this reads 'over-tight'; priced at entry it reads as a gate
    paying for itself, on identical input."""
    lines = [
        _eval(100.0, "btc-updown-5m-1000",
              [{"side": "up", "brake": "safety", "ask": 0.50, "fair": 0.6, "net": -0.1}]),
        _eval(105.0, "btc-updown-5m-1000",
              [{"side": "up", "brake": "safety", "ask": 0.02, "fair": 0.6, "net": -0.1}]),
        # a losing refused side, so the category has both halves
        _eval(100.0, "btc-updown-5m-1000",
              [{"side": "down", "brake": "safety", "ask": 0.50, "fair": 0.4, "net": -0.2}]),
    ]
    report = build_report(lines, winners={"btc-updown-5m-1000": "up"},
                          activity_rows=[], since=0.0)
    safety = report["categories"]["safety"]
    assert safety["priced"] == 2
    # entry ask 0.50 on a $25 clip -> ~$25 missed, against the $25 avoided:
    # net stays at or below zero instead of the ~$1,200 the 0.02 minimum books.
    assert safety["missed_wins"] == shadow_value(0.50, 25.0, True)
    assert safety["avoided_losses"] == 25.0
    assert safety["net"] <= 0.0
    assert verdict(safety) == "paying for itself"
