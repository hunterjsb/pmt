"""Unit tests for polymarket.scanner's pure scoring/parsing functions. No network."""

import pytest

from polymarket.scanner import (
    _select_target,
    flag_comments,
    parse_event_ref,
    sharp_score,
    sibling_precedent,
    side_sharpness,
)


def test_parse_event_ref_strips_full_url_to_slug():
    url = "https://polymarket.com/event/some-event-slug?tid=123"
    assert parse_event_ref(url) == "some-event-slug"


def test_parse_event_ref_passes_bare_slug_through():
    assert parse_event_ref("some-event-slug") == "some-event-slug"


def test_parse_event_ref_strips_trailing_slash_and_sub_path():
    assert parse_event_ref("https://polymarket.com/event/some-event-slug/") == "some-event-slug"
    # a trailing sub-market path segment (no query string) must also be stripped
    assert parse_event_ref("https://polymarket.com/event/some-event-slug/some-sub-market") == "some-event-slug"


def test_sharp_score_rewards_high_pnl_high_volume_whale_over_small_winner():
    # ground truth: denizz (lifetime whale) vs goatboss (small winner) on the same market
    denizz = sharp_score(2463507, 99858480, 995)
    goatboss = sharp_score(803, 58942, 48)
    assert denizz > goatboss * 1.5


def test_sharp_score_penalizes_high_volume_lifetime_loser_below_a_smaller_winner():
    # a high-volume lifetime LOSER is a churner, not a sharp — must not outscore a real winner
    haradwaith = sharp_score(-234277, 115702646, 71635)
    mepp = sharp_score(584709, 37711781, 1051)
    assert haradwaith < mepp


def test_sharp_score_negative_pnl_scores_lower_than_zero_pnl_at_same_volume():
    loser = sharp_score(-1000, 10000, 10)
    breakeven = sharp_score(0, 10000, 10)
    assert loser < breakeven


def test_sharp_score_zero_everything_wallet_is_zero():
    # brand-new / never-traded wallet — no pnl, no volume, no markets
    assert sharp_score(0, 0, 0) == 0.0


def test_side_sharpness_empty_holders_is_zero():
    assert side_sharpness([]) == 0.0


def test_side_sharpness_zero_total_amount_is_zero():
    # non-empty holder list but all amounts are zero — distinct code path from the empty-list case
    holders = [{"amount": 0, "pnl": 500, "volume": 1000, "markets": 5}]
    assert side_sharpness(holders) == 0.0


def test_side_sharpness_is_share_weighted_mean_of_holder_scores():
    holders = [
        {"amount": 100, "pnl": 1000, "volume": 100000, "markets": 100},
        {"amount": 900, "pnl": 0, "volume": 0, "markets": 0},
    ]
    expected = (100 * sharp_score(1000, 100000, 100) + 900 * sharp_score(0, 0, 0)) / 1000
    assert side_sharpness(holders) == pytest.approx(expected)


def test_flag_comments_matches_keywords_case_insensitively():
    comments = [
        {"body": "This market has a RESOLUTION issue"},
        {"body": "just here for the vibes"},
        {"body": "the UMA oracle is going to rule against this"},
    ]
    out = flag_comments(comments)
    assert out["total"] == 3
    assert len(out["flagged"]) == 2
    assert out["ratio"] == pytest.approx(2 / 3)


def test_flag_comments_empty_list_has_zero_ratio():
    out = flag_comments([])
    assert out == {"total": 0, "flagged": [], "ratio": 0.0}


def test_flag_comments_no_keyword_hits_has_zero_ratio_but_nonzero_total():
    comments = [{"body": "great market, going long"}, {"body": "lfg"}]
    out = flag_comments(comments)
    assert out["total"] == 2
    assert out["flagged"] == []
    assert out["ratio"] == 0.0


def test_flag_comments_tolerates_missing_body():
    out = flag_comments([{"body": None}, {}])
    assert out["total"] == 2
    assert out["flagged"] == []


def test_sibling_precedent_skips_open_sub_markets():
    event = {"markets": [{"closed": False, "outcomePrices": '["0.5", "0.5"]', "question": "still open"}]}
    assert sibling_precedent(event) == []


def test_sibling_precedent_derives_outcome_from_yes_price():
    event = {
        "markets": [
            {"closed": True, "groupItemTitle": "October 31", "volume": 2502858.18776,
             "outcomePrices": '["0", "1"]'},
            {"closed": True, "question": "Resolved yes market", "volume": 100,
             "outcomePrices": ["1", "0"]},  # already-parsed list form
        ]
    }
    out = sibling_precedent(event)
    assert out[0] == {"title": "October 31", "volume": pytest.approx(2502858.18776), "outcome": "NO"}
    assert out[1]["outcome"] == "YES"


def test_sibling_precedent_skips_closed_markets_missing_outcome_prices():
    event = {"markets": [{"closed": True, "groupItemTitle": "no quote", "volume": 0, "outcomePrices": None}]}
    assert sibling_precedent(event) == []


def test_sibling_precedent_skips_closed_markets_with_malformed_outcome_prices():
    event = {"markets": [{"closed": True, "groupItemTitle": "bad quote", "volume": 5,
                           "outcomePrices": "not-json"}]}
    assert sibling_precedent(event) == []


def test_sibling_precedent_skips_closed_markets_with_empty_string_outcome_prices():
    # "" is a str, so it bypasses the `raw or []` fallback that catches None/missing
    event = {"markets": [{"closed": True, "groupItemTitle": "empty quote", "volume": 5,
                           "outcomePrices": ""}]}
    assert sibling_precedent(event) == []


def test_select_target_raises_when_match_hits_nothing():
    # a typo/bogus --match must error, never silently deep-dive an unrelated market
    markets = [
        {"question": "December 31", "conditionId": "0x1", "volume": 100},
        {"question": "October 31", "conditionId": "0x2", "volume": 500},
    ]
    with pytest.raises(ValueError, match="totally-bogus"):
        _select_target(markets, "totally-bogus")


def test_select_target_match_narrows_to_the_matching_market():
    markets = [
        {"question": "December 31", "conditionId": "0x1", "volume": 100},
        {"question": "October 31", "conditionId": "0x2", "volume": 500},
    ]
    assert _select_target(markets, "december")["conditionId"] == "0x1"


def test_select_target_defaults_to_highest_volume_open_market_with_condition_id():
    markets = [
        {"question": "no token yet", "conditionId": None, "volume": 999999},
        {"question": "December 31", "conditionId": "0x1", "volume": 100},
        {"question": "October 31", "conditionId": "0x2", "volume": 500},
    ]
    assert _select_target(markets, None)["conditionId"] == "0x2"
