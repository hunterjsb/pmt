"""The EU auto-redeem sweeper's decision half, against frozen real receipts.

Every fixture in `deploy/eu/fixtures/` is a transaction that actually
happened on Polygon on the night of 2026-08-24, and the three receipts are
the whole argument:

  receipt_pusd_payout_zero      nine PayoutRedemption events, payout 0 each,
                                collateral pUSD — the L43 incident itself and
                                the negative fixture the assertion exists for
  receipt_ctf_usdce_paid        the same nine conditions redeemed with USDC.e,
                                $133.66 paid into the wallet as raw USDC.e
  receipt_adapter_pusd_paid     a third party's batch through the adapter:
                                CTF pays USDC.e to the adapter, the adapter
                                mints $16.563372 of pUSD straight to the user

The sweeper submits nothing here. What is under test is the part that decides
what to redeem and, afterwards, whether it was paid — the two questions that
cost $183.66 of hand recovery when they were assumed rather than checked.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
FIXTURES = REPO / "deploy" / "eu" / "fixtures"

# The sweeper ships as a hyphenated script (it is installed as a unit's
# ExecStart, not imported by anything on the box), so load it by path. It has
# to be in sys.modules before exec: its @dataclass resolves annotations
# through the module entry.
_spec = importlib.util.spec_from_file_location(
    "redeem_sweeper", REPO / "deploy" / "eu" / "redeem-sweeper.py")
sweeper = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = sweeper
_spec.loader.exec_module(sweeper)


def fixture(name: str):
    return json.loads((FIXTURES / f"{name}.json").read_text())


WALLET = sweeper.DEFAULT_WALLET
# The nine conditions of the incident, in the order the recovery batch redeemed
# them. Taken off the paying receipt, not retyped from anywhere.
PAID_RECEIPT = "receipt_ctf_usdce_paid"
ZERO_RECEIPT = "receipt_pusd_payout_zero"
ADAPTER_RECEIPT = "receipt_adapter_pusd_paid"


# --------------------------------------------------------------------------
# calldata
# --------------------------------------------------------------------------

def test_redeem_calldata_matches_the_batch_that_actually_paid():
    # Copied out of tx 0xce6425…4b10's first inner call, the one that paid
    # $36.66. The collateral word is USDC.e and that is the whole lesson.
    expected = (
        "0x01b7037c"
        "0000000000000000000000002791bca1f2de4661ed88a30c99a7a9449aa84174"
        "0000000000000000000000000000000000000000000000000000000000000000"
        "88b66379809f6e8d85df562df7d47d77761652b2f1af0e7527157aedde2bfd46"
        "0000000000000000000000000000000000000000000000000000000000000080"
        "0000000000000000000000000000000000000000000000000000000000000002"
        "0000000000000000000000000000000000000000000000000000000000000001"
        "0000000000000000000000000000000000000000000000000000000000000002")
    got = sweeper.encode_redeem_positions(
        sweeper.USDCE,
        "0x88b66379809f6e8d85df562df7d47d77761652b2f1af0e7527157aedde2bfd46")
    assert got == expected


def test_adapter_calldata_matches_a_third_partys_live_batch():
    # From tx 0x1c4618…ee71, routed through the adapter with the pUSD
    # parameter — the form that mints pUSD directly instead of waiting on the
    # wrap. Same selector, different collateral word, different target.
    expected = (
        "0x01b7037c"
        "000000000000000000000000c011a7e12a19f7b1f670d46f03b03f3342e82dfb"
        "0000000000000000000000000000000000000000000000000000000000000000"
        "e6013daf7f1f9e74db391ad3209ccd24a8afe907160eb14c3b4c0dae0807c808"
        "0000000000000000000000000000000000000000000000000000000000000080"
        "0000000000000000000000000000000000000000000000000000000000000002"
        "0000000000000000000000000000000000000000000000000000000000000001"
        "0000000000000000000000000000000000000000000000000000000000000002")
    got = sweeper.encode_redeem_positions(
        sweeper.PUSD,
        "0xe6013daf7f1f9e74db391ad3209ccd24a8afe907160eb14c3b4c0dae0807c808")
    assert got == expected


def test_set_approval_for_all_encodes_the_one_time_grant():
    got = sweeper.encode_set_approval_for_all(sweeper.ADAPTER, True)
    assert got == (
        "0x"
        "a22cb465"
        "000000000000000000000000ada100db00ca00073811820692005400218fce1f"
        "0000000000000000000000000000000000000000000000000000000000000001")


def test_word_refuses_a_value_wider_than_a_word():
    with pytest.raises(ValueError):
        sweeper._word("0x" + "ff" * 33)


# --------------------------------------------------------------------------
# candidate selection
# --------------------------------------------------------------------------

def _row(**kw):
    row = {"conditionId": "0x" + "ab" * 32, "slug": "bnb-updown-5m-1787536800",
           "asset": "123", "redeemable": True, "currentValue": 41.0,
           "negativeRisk": False}
    row.update(kw)
    return row


def test_candidates_come_from_the_frozen_eu_wallet_blob():
    rows = fixture("positions_eu_wallet")
    candidates, skipped = sweeper.candidate_conditions(rows)
    assert len(rows) == 8
    assert [c.slug for c in candidates] == [
        "bnb-updown-5m-1787536800", "bnb-updown-5m-1787539200",
        "bnb-updown-5m-1787537100", "bnb-updown-5m-1787537700",
        "bnb-updown-5m-1787542200", "bnb-updown-5m-1787540100"]
    assert round(sum(c.reported_value for c in candidates), 2) == 132.0
    assert skipped == []


def test_the_two_rows_the_blob_drops_are_the_held_losers():
    # Both are flagged redeemable and both mark at zero: sides we bought and
    # lost. Burning them for $0 would put a legitimate payout of zero in the
    # same batch as the winners and make the L43 assertion unusable.
    rows = fixture("positions_eu_wallet")
    losers = [r for r in rows if float(r["currentValue"]) == 0]
    assert [r["slug"] for r in losers] == ["bnb-updown-5m-1787529900",
                                           "btc-updown-5m-1787511000"]
    assert all(r["redeemable"] for r in losers)
    assert sweeper.candidate_conditions(losers) == ([], [])


def test_a_still_open_window_is_never_a_candidate():
    # Redeeming an open window is not merely useless: the CTF has no result
    # for it, the call reverts, and an atomic batch strands every winner in it.
    candidates, skipped = sweeper.candidate_conditions(
        [_row(redeemable=False, currentValue=41.0)])
    assert candidates == [] and skipped == []


def test_a_held_loser_marks_at_zero_and_is_left_alone():
    candidates, skipped = sweeper.candidate_conditions([_row(currentValue=0.0)])
    assert candidates == [] and skipped == []


def test_negative_risk_is_skipped_loudly_not_silently():
    # Its redemption goes through the NegRiskAdapter with a different call;
    # encoding the plain one would revert the whole batch.
    candidates, skipped = sweeper.candidate_conditions([_row(negativeRisk=True)])
    assert candidates == []
    assert skipped[0]["reason"] == "negative_risk"


def test_both_sides_of_one_market_collapse_into_one_condition():
    rows = [_row(asset="111", currentValue=41.0),
            _row(asset="222", currentValue=0.0, redeemable=True)]
    candidates, _ = sweeper.candidate_conditions(rows)
    assert len(candidates) == 1
    # The zero-marked side contributes no value and no second call.
    assert candidates[0].assets == ("111",)


def test_candidate_selection_never_raises_on_a_half_built_row():
    rows = ["not-a-dict", {}, _row(conditionId=""), _row(conditionId="0xdead"),
            _row(currentValue="n/a"), _row(currentValue="41")]
    candidates, skipped = sweeper.candidate_conditions(rows)
    assert [c.reported_value for c in candidates] == [41.0]
    assert {s["reason"] for s in skipped} == {"no_condition_id"}


# --------------------------------------------------------------------------
# gamma
# --------------------------------------------------------------------------

def test_clob_token_ids_arrive_as_a_json_string():
    market = {"clobTokenIds": '["640258439", "254297985"]'}
    assert sweeper.token_ids_from_gamma(market) == ("640258439", "254297985")


def test_a_market_gamma_will_not_call_closed_is_not_settled():
    assert sweeper.gamma_is_settled({"closed": True}) is True
    assert sweeper.gamma_is_settled({"closed": False}) is False
    assert sweeper.gamma_is_settled(None) is False
    assert sweeper.gamma_is_settled({}) is False


# --------------------------------------------------------------------------
# payout decoding — the L43 assertion
# --------------------------------------------------------------------------

def test_the_incident_receipt_decodes_as_nine_payouts_of_exactly_zero():
    receipt = fixture(ZERO_RECEIPT)
    paid = sweeper.decode_payouts(receipt, redeemer=WALLET,
                                  collateral=sweeper.PUSD)
    assert len(paid) == 9
    assert set(paid.values()) == {0}


def test_the_incident_receipt_shows_no_usdce_redemption_at_all():
    # The events exist and carry the pUSD collateral, so a decoder that keys
    # on USDC.e — the only collateral that can pay — sees nothing. Either
    # reading has to fail the run; this is the one the sweeper uses.
    receipt = fixture(ZERO_RECEIPT)
    assert sweeper.decode_payouts(receipt, redeemer=WALLET,
                                  collateral=sweeper.USDCE) == {}


def test_the_incident_receipt_credited_the_wallet_nothing():
    receipt = fixture(ZERO_RECEIPT)
    assert sweeper.erc20_credited(receipt, sweeper.USDCE, WALLET) == 0
    assert sweeper.erc20_credited(receipt, sweeper.PUSD, WALLET) == 0


def test_the_recovery_receipt_decodes_as_the_13366_that_was_actually_paid():
    receipt = fixture(PAID_RECEIPT)
    paid = sweeper.decode_payouts(receipt, redeemer=WALLET,
                                  collateral=sweeper.USDCE)
    assert len(paid) == 9
    assert sum(paid.values()) == 133_660_000
    assert 0 not in paid.values()


def test_the_recovery_receipt_credits_raw_usdce_from_the_ctf():
    receipt = fixture(PAID_RECEIPT)
    credited = sweeper.erc20_credited(receipt, sweeper.USDCE, WALLET,
                                      from_addr=sweeper.CTF)
    assert credited == 133_660_000
    # And nothing wrapped in the same transaction — that is the sweeper wait
    # the adapter path exists to skip.
    assert sweeper.erc20_credited(receipt, sweeper.PUSD, WALLET,
                                  from_addr=sweeper.ZERO_ADDRESS) == 0


def test_the_adapter_receipt_pays_the_ctf_in_usdce_and_mints_pusd_to_the_user():
    receipt = fixture(ADAPTER_RECEIPT)
    user = "0xe9fe838978dbb3449da2324a071ea5552af04730"
    paid = sweeper.decode_payouts(receipt, redeemer=sweeper.ADAPTER,
                                  collateral=sweeper.USDCE)
    assert paid == {
        "0x7c83e1f889ddaf02b9e41091d1902f20a5f1f93cac9e04ad8f8451a889278aa3":
            16_563_372}
    minted = sweeper.erc20_credited(receipt, sweeper.PUSD, user,
                                    from_addr=sweeper.ZERO_ADDRESS)
    assert minted == 16_563_372
    # The redeemer of record is the adapter, so keying on our own wallet the
    # way the CTF path does would read a paying batch as a zero.
    assert sweeper.decode_payouts(receipt, redeemer=user,
                                  collateral=sweeper.USDCE) == {}


# --------------------------------------------------------------------------
# grading
# --------------------------------------------------------------------------

def test_a_fully_paid_batch_has_no_problems():
    receipt = fixture(PAID_RECEIPT)
    paid = sweeper.decode_payouts(receipt, WALLET, sweeper.USDCE)
    credited = sweeper.erc20_credited(receipt, sweeper.USDCE, WALLET,
                                      from_addr=sweeper.CTF)
    assert sweeper.grade_payment(dict(paid), paid, credited) == []


def test_the_incident_grades_as_payout_zero_on_every_verified_holding():
    receipt = fixture(ZERO_RECEIPT)
    paid = sweeper.decode_payouts(receipt, WALLET, sweeper.PUSD)
    expected = {cid: 10_000_000 for cid in paid}
    problems = sweeper.grade_payment(expected, paid, credited=0)
    assert len(problems) == 9
    assert {p["reason"] for p in problems} == {"payout_zero"}


def test_a_missing_payout_event_is_a_problem_not_a_pass():
    problems = sweeper.grade_payment({"0xabc": 5_000_000}, {}, credited=0)
    assert problems == [{"condition_id": "0xabc", "reason": "no_payout_event",
                         "expected": 5_000_000}]


def test_a_short_payout_is_caught_even_though_the_tx_succeeded():
    problems = sweeper.grade_payment({"0xabc": 5_000_000},
                                     {"0xabc": 4_000_000}, credited=4_000_000)
    assert problems[0]["reason"] == "underpaid"


def test_a_payout_the_wallet_never_received_is_caught():
    # The CTF says paid, the wallet's balance says otherwise: the money went
    # somewhere that is not us.
    problems = sweeper.grade_payment({"0xabc": 5_000_000},
                                     {"0xabc": 5_000_000}, credited=0)
    assert problems == [{"reason": "not_credited", "paid": 5_000_000,
                         "credited": 0}]


# --------------------------------------------------------------------------
# the unwrapped-USDC.e watch
# --------------------------------------------------------------------------

def test_dust_below_the_floor_is_not_worth_a_note():
    assert sweeper.unwrapped_note(3_000_000, None, 1_000_000) == (None, None)


def test_a_fresh_balance_starts_the_clock_without_complaining():
    first_seen, note = sweeper.unwrapped_note(50_000_000, None, 1_000_000)
    assert first_seen == 1_000_000 and note is None


def test_usdce_still_unwrapped_past_45_minutes_gets_flagged():
    _, note = sweeper.unwrapped_note(50_000_000, 1_000_000, 1_000_000 + 46 * 60)
    assert note["reason"] == "unwrapped_usdce_stale"
    assert note["usdce"] == 50.0 and note["age_s"] >= 45 * 60


def test_the_clock_resets_once_the_sweeper_has_wrapped_it():
    first_seen, note = sweeper.unwrapped_note(0, 1_000_000, 9_000_000)
    assert first_seen is None and note is None


# --------------------------------------------------------------------------
# batch assembly
# --------------------------------------------------------------------------

def _cand(cid: str, slug: str = "s"):
    return sweeper.Candidate(condition_id=cid, slug=slug, assets=(),
                             reported_value=1.0)


def test_the_ctf_path_targets_the_ctf_with_usdce_and_needs_no_approval():
    calls = sweeper.build_calls("ctf", [_cand("0x" + "11" * 32)],
                                need_approval=True)
    assert len(calls) == 1
    target, data, _ = calls[0]
    assert target == sweeper.CTF
    assert sweeper.USDCE[2:].lower() in data


def test_the_adapter_path_prepends_the_grant_only_when_it_is_missing():
    cands = [_cand("0x" + "11" * 32), _cand("0x" + "22" * 32)]
    with_grant = sweeper.build_calls("adapter", cands, need_approval=True)
    assert len(with_grant) == 3
    assert with_grant[0][0] == sweeper.CTF          # the grant is on the CTF
    assert with_grant[1][0] == sweeper.ADAPTER
    assert sweeper.PUSD[2:].lower() in with_grant[1][1]
    already = sweeper.build_calls("adapter", cands, need_approval=False)
    assert len(already) == 2
