#!/usr/bin/env python3
"""Redeem the EU box's settled winners before the engine starves on its own wins.

A deposit-wallet (sig-type-3) account books every win as CTF outcome tokens,
not as cash. Nothing spends those tokens: until someone redeems them the
wallet's *collateral* only goes down, and a winning night walks the balance
under the clip size and freezes the engine. That happened — twice in one
night, $133.66 + $50 recovered by hand (vault docs/LESSONS.md#L43, #L41).

Three facts from that night are load-bearing and are asserted here rather
than assumed:

* **Position tokens are keyed on USDC.e**, not the pUSD wrapper the wallet
  displays. `redeemPositions(pUSD, ...)` straight at the CTF *succeeds* —
  nine PayoutRedemption events, payout 0 each — because it derives phantom
  position ids the wallet holds none of. "Redeem succeeded" is not "redeem
  paid", so this script decodes the payout out of the receipt and treats a
  zero on a verified holding as a loud failure.
* **data-api is the candidate enumerator; the chain is truth.** Its
  `redeemable` flag still showed the paid-zero conditions as redeemable
  afterwards. Every condition that reaches a batch is re-proven on-chain:
  resolved (`payoutDenominator > 0`), ours (`balanceOf > 0`), and worth
  something (`payoutNumerators` says our side won).
* **One unresolved condition reverts the whole batch.** The CTF refuses to
  redeem a condition with no reported result, and a batch is atomic, so a
  single open window in the list strands every other winner in it. Hence the
  gamma `closed=true` gate *and* the on-chain denominator check.

Two redeem paths exist, both verified on-chain (evidence in README.md):

    adapter   0xAdA100Db… `redeemPositions(pUSD, …)` — redeems at the CTF
              with USDC.e internally, wraps, and mints pUSD straight to the
              wallet in the same transaction. Cash is spendable immediately.
              Needs a one-time CTF `setApprovalForAll` from the wallet.
    ctf       0x4D97DCd9… `redeemPositions(USDC.e, …)` — pays raw USDC.e into
              the wallet; Polymarket's own sweeper wraps it to pUSD 30–60 min
              later, and until it does the engine cannot see the money.

The path is chosen by whether the wallet has already approved the adapter, so
the fallback path is what runs by default and the fast path switches itself on
the moment a human grants that approval once (`--grant-approval`). Granting a
blanket ERC-1155 approval is a human's call, not a timer's.

Runs from a systemd timer every 10 minutes. Reads its key material on the box
and never logs, prints or transmits it.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

import requests

# ---------------------------------------------------------------------------
# Addresses, selectors, topics. Every one of these was read back off a real
# Polygon transaction before it was written down (README.md § Evidence).
# ---------------------------------------------------------------------------

CHAIN_ID = 137

CTF = "0x4D97DCd97eC945f40cF65F87097ACe5EA0476045"
USDCE = "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174"
PUSD = "0xC011a7E12a19f7B1f670d46F03B03f3342E82DFB"
ADAPTER = "0xAdA100Db00Ca00073811820692005400218FcE1f"

DEFAULT_WALLET = "0x6da3e7Dd76cE67B32ae7911e19da0c00550F1D71"
DEFAULT_RELAYER = "https://relayer-v2.polymarket.com"
DEFAULT_RPC = "https://polygon-bor-rpc.publicnode.com"
DATA_API = "https://data-api.polymarket.com"
GAMMA_API = "https://gamma-api.polymarket.com"

SEL_REDEEM_POSITIONS = "01b7037c"      # redeemPositions(address,bytes32,bytes32,uint256[])
SEL_SET_APPROVAL_FOR_ALL = "a22cb465"  # setApprovalForAll(address,bool)
SEL_IS_APPROVED_FOR_ALL = "e985e9c5"   # isApprovedForAll(address,address)
SEL_BALANCE_OF_1155 = "00fdd58e"       # balanceOf(address,uint256)
SEL_BALANCE_OF_20 = "70a08231"         # balanceOf(address)
SEL_PAYOUT_NUMERATORS = "0504c814"     # payoutNumerators(bytes32,uint256)
SEL_PAYOUT_DENOMINATOR = "dd34de67"    # payoutDenominator(bytes32)
SEL_WALLET_NONCE = "affed0e0"          # nonce()

TOPIC_PAYOUT_REDEMPTION = (
    "0x2682012a4a4f1973119f1c9b90745d1bd91fa2bab387344f044cb3586864d18d")
TOPIC_ERC20_TRANSFER = (
    "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef")

ZERO_ADDRESS = "0x0000000000000000000000000000000000000000"
ZERO_BYTES32 = "0x" + "00" * 32

# A binary condition's two index sets: 0b01 is outcome 0, 0b10 is outcome 1.
# Redeeming both in one call is what every live batch does, ours included.
BINARY_INDEX_SETS = (1, 2)

USDC_DECIMALS = 6
MICROS = 10 ** USDC_DECIMALS

# Nine calls fit in one batch comfortably (that was the recovery batch). Cap
# well under any relayer gas ceiling and chunk beyond it rather than discover
# the limit with real money in the call.
MAX_CALLS_PER_BATCH = 12

# 240s was rejected "deadline too soon" — the relayer queues before it signs.
DEADLINE_SECONDS = 900

# Raw USDC.e in the wallet is money the engine cannot spend until Polymarket's
# sweeper wraps it. Worth flagging, never worth fixing from here: the
# wallet->EOA leg needs a human.
UNWRAPPED_FLOOR_MICROS = 10 * MICROS
UNWRAPPED_STALE_SECONDS = 45 * 60

DEFAULT_LOG = "/home/ec2-user/.pmt/engine/redeem-log.jsonl"
DEFAULT_STATE = "/home/ec2-user/.pmt/engine/redeem-state.json"
DEFAULT_L0_ENV = "/home/ec2-user/.pmt/l0.env"
DEFAULT_BUILDER_ENV = "/home/ec2-user/.pmt/builder.env"

# The box's env files were written by hand during onboarding; accept the names
# they plausibly carry rather than fail a 10-minute timer on a spelling.
PRIVATE_KEY_NAMES = ("PMT_L0_PRIVATE_KEY", "L0_PRIVATE_KEY", "PM_PRIVATE_KEY",
                     "POLYMARKET_PRIVATE_KEY", "PRIVATE_KEY", "PK")
BUILDER_KEY_NAMES = ("POLYMARKET_BUILDER_API_KEY", "POLY_BUILDER_API_KEY",
                     "BUILDER_API_KEY")
BUILDER_SECRET_NAMES = ("POLYMARKET_BUILDER_SECRET", "POLY_BUILDER_SECRET",
                        "BUILDER_SECRET")
BUILDER_PASSPHRASE_NAMES = ("POLYMARKET_BUILDER_PASSPHRASE",
                            "POLY_BUILDER_PASSPHRASE", "BUILDER_PASSPHRASE")

EXIT_OK = 0
EXIT_CONFIG = 1        # cannot even try: creds, transport, disagreeing state
EXIT_TX_FAILED = 2     # the batch did not land, or we cannot prove it landed
EXIT_PAYOUT_ZERO = 3   # it landed and paid nothing — the L43 class

UA = {"User-Agent": "pmt-redeem-sweeper/1.0"}


# ---------------------------------------------------------------------------
# Pure: ABI encoding
#
# Hand-rolled rather than via eth-abi so the whole decision half of this script
# imports with nothing but the stdlib and `requests`, and so the tests can pin
# the bytes against calldata copied off real transactions.
# ---------------------------------------------------------------------------

def _word(value: int | str) -> str:
    """One 32-byte ABI word as 64 hex chars, from an int or a hex string."""
    if isinstance(value, str):
        v = value[2:] if value.startswith(("0x", "0X")) else value
        if len(v) > 64:
            raise ValueError(f"value wider than a word: {value}")
        return v.lower().rjust(64, "0")
    if value < 0:
        raise ValueError("negative values are not encodable here")
    return format(value, "064x")


def encode_redeem_positions(collateral: str, condition_id: str,
                            index_sets=BINARY_INDEX_SETS,
                            parent_collection_id: str = ZERO_BYTES32) -> str:
    """Calldata for `redeemPositions(address,bytes32,bytes32,uint256[])`.

    `collateral` is the whole lesson: pass the token the position id was
    minted with. At the CTF that is USDC.e; the adapter takes pUSD and does
    the translation itself.
    """
    head = [_word(collateral), _word(parent_collection_id), _word(condition_id)]
    head.append(_word(0x80))  # the array's tail sits after the four head words
    tail = [_word(len(index_sets))] + [_word(int(i)) for i in index_sets]
    return "0x" + SEL_REDEEM_POSITIONS + "".join(head + tail)


def encode_set_approval_for_all(operator: str, approved: bool = True) -> str:
    """Calldata for the one-time ERC-1155 grant the adapter path needs."""
    return ("0x" + SEL_SET_APPROVAL_FOR_ALL + _word(operator)
            + _word(1 if approved else 0))


# ---------------------------------------------------------------------------
# Pure: candidate selection from a data-api positions blob
# ---------------------------------------------------------------------------

@dataclass
class Candidate:
    """A conditionId data-api thinks is redeemable, before the chain agrees."""
    condition_id: str
    slug: str
    assets: tuple[str, ...]          # token ids the feed shows us holding
    reported_value: float            # data-api currentValue, a mark not a fact
    # Filled in by the chain pass:
    resolved: bool = False
    holdings: dict[str, int] = field(default_factory=dict)  # tokenId -> micros
    expected_micros: int = 0

    @property
    def condition_hex(self) -> str:
        return self.condition_id[2:] if self.condition_id.startswith("0x") else self.condition_id


def candidate_conditions(rows, min_value: float = 0.0):
    """Split a data-api positions blob into `(candidates, skipped)`.

    A candidate is a conditionId that is flagged redeemable, still marked
    above `min_value`, and not negative-risk. Rows are grouped by condition
    because a wallet can hold both sides of one market and both redeem in the
    same call.

    Negative-risk markets are skipped on purpose: their redemption goes
    through the NegRiskAdapter with a different signature, and the updown book
    has none. Skipping loudly beats encoding a call that reverts the batch.
    """
    by_condition: dict[str, Candidate] = {}
    skipped: list[dict] = []
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        cid = str(row.get("conditionId") or "").lower()
        slug = str(row.get("slug") or "")
        if not cid.startswith("0x") or len(cid) != 66:
            skipped.append({"slug": slug, "reason": "no_condition_id"})
            continue
        if not row.get("redeemable"):
            continue  # open or already swept; not an anomaly, not worth a line
        try:
            value = float(row.get("currentValue") or 0.0)
        except (TypeError, ValueError):
            value = 0.0
        if value <= min_value:
            # A held loser marks at 0. Redeeming it burns shares for nothing
            # and would muddy the payout assertion, so it stays where it is.
            continue
        if row.get("negativeRisk"):
            skipped.append({"slug": slug, "condition_id": cid,
                            "reason": "negative_risk"})
            continue
        asset = str(row.get("asset") or "")
        cand = by_condition.get(cid)
        if cand is None:
            by_condition[cid] = Candidate(condition_id=cid, slug=slug,
                                          assets=(asset,) if asset else (),
                                          reported_value=value)
        else:
            if asset and asset not in cand.assets:
                cand.assets = cand.assets + (asset,)
            cand.reported_value += value
    return list(by_condition.values()), skipped


def token_ids_from_gamma(market: dict) -> tuple[str, ...]:
    """The market's two CLOB token ids, index-aligned with its outcomes.

    gamma hands `clobTokenIds` back as a JSON *string*; position ids in the
    CTF are ordered by outcome index, so index 0 of this list is index set 1.
    """
    raw = market.get("clobTokenIds")
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except json.JSONDecodeError:
            return ()
    if not isinstance(raw, list):
        return ()
    return tuple(str(t) for t in raw)


def gamma_is_settled(market: dict | None) -> bool:
    """True only for a market gamma calls closed. Open windows never redeem."""
    if not isinstance(market, dict):
        return False
    return bool(market.get("closed"))


# ---------------------------------------------------------------------------
# Pure: receipt forensics
# ---------------------------------------------------------------------------

def _topic_address(topic: str) -> str:
    return "0x" + topic[-40:].lower()


def decode_payouts(receipt: dict, redeemer: str, collateral: str) -> dict[str, int]:
    """`{conditionId: payout_micros}` from a receipt's PayoutRedemption events.

    Keyed on `(redeemer, collateral)` because both matter and both have been
    wrong: the pUSD-collateral events are the paid-zero phantoms, and on the
    adapter path the redeemer of record is the adapter, not our wallet.
    """
    out: dict[str, int] = {}
    for log in receipt.get("logs") or []:
        topics = log.get("topics") or []
        if not topics or topics[0].lower() != TOPIC_PAYOUT_REDEMPTION:
            continue
        if len(topics) < 4:
            continue
        if _topic_address(topics[1]) != redeemer.lower():
            continue
        if _topic_address(topics[2]) != collateral.lower():
            continue
        data = log.get("data", "0x")[2:]
        if len(data) < 192:
            continue
        condition_id = "0x" + data[0:64]
        payout = int(data[128:192], 16)   # head is (conditionId, offset, payout)
        out[condition_id.lower()] = out.get(condition_id.lower(), 0) + payout
    return out


def erc20_credited(receipt: dict, token: str, to_addr: str,
                   from_addr: str | None = None) -> int:
    """Micros moved into `to_addr` by `token` in this receipt.

    `from_addr=ZERO_ADDRESS` reads as "minted to", which is exactly what the
    adapter path does with pUSD and the only proof that the money is spendable
    without waiting on a wrap.
    """
    total = 0
    for log in receipt.get("logs") or []:
        if str(log.get("address", "")).lower() != token.lower():
            continue
        topics = log.get("topics") or []
        if len(topics) < 3 or topics[0].lower() != TOPIC_ERC20_TRANSFER:
            continue
        if _topic_address(topics[2]) != to_addr.lower():
            continue
        if from_addr is not None and _topic_address(topics[1]) != from_addr.lower():
            continue
        total += int(log.get("data", "0x0"), 16)
    return total


def grade_payment(expected: dict[str, int], paid: dict[str, int],
                  credited: int) -> list[dict]:
    """Every way this batch can have "succeeded" without paying.

    An empty list is the only acceptable answer. Anything here is the L43
    class and must reach the operator as a failed unit, not a log line.
    """
    problems: list[dict] = []
    for cid, want in expected.items():
        got = paid.get(cid.lower())
        if got is None:
            problems.append({"condition_id": cid, "reason": "no_payout_event",
                             "expected": want})
        elif got == 0 and want > 0:
            problems.append({"condition_id": cid, "reason": "payout_zero",
                             "expected": want})
        elif got < want:
            problems.append({"condition_id": cid, "reason": "underpaid",
                             "expected": want, "paid": got})
    total_paid = sum(paid.get(c.lower(), 0) for c in expected)
    if total_paid > 0 and credited < total_paid:
        # The CTF said it paid and the wallet's balance disagrees: the money
        # went somewhere else. Never seen; would be the worst version of L43.
        problems.append({"reason": "not_credited", "paid": total_paid,
                         "credited": credited})
    return problems


def unwrapped_note(usdce_micros: int, first_seen: float | None, now: float,
                   floor_micros: int = UNWRAPPED_FLOOR_MICROS,
                   stale_after: int = UNWRAPPED_STALE_SECONDS):
    """Track raw USDC.e that Polymarket's sweeper has not wrapped yet.

    Returns `(new_first_seen, note_or_None)`. The note only appears once the
    balance has sat above the floor longer than a wrap normally takes; the
    fix is a human's (the wallet->EOA leg is not ours to drive).
    """
    if usdce_micros < floor_micros:
        return None, None
    started = first_seen if first_seen else now
    age = int(now - started)
    if age >= stale_after:
        return started, {"usdce": round(usdce_micros / MICROS, 6),
                         "age_s": age, "reason": "unwrapped_usdce_stale"}
    return started, None


# ---------------------------------------------------------------------------
# I/O: chain, APIs, env
# ---------------------------------------------------------------------------

class Rpc:
    """The smallest JSON-RPC client that can answer "is this actually ours"."""

    def __init__(self, url: str, timeout: float = 20.0):
        self.url = url
        self.timeout = timeout
        self._session = requests.Session()

    def call(self, method: str, params: list):
        r = self._session.post(
            self.url, json={"jsonrpc": "2.0", "id": 1, "method": method,
                            "params": params},
            headers={**UA, "Content-Type": "application/json"},
            timeout=self.timeout)
        r.raise_for_status()
        body = r.json()
        if "error" in body:
            raise RuntimeError(f"rpc {method}: {body['error']}")
        return body.get("result")

    def eth_call(self, to: str, data: str) -> str:
        return self.call("eth_call", [{"to": to, "data": data}, "latest"])

    def call_uint(self, to: str, data: str) -> int:
        raw = self.eth_call(to, data)
        return int(raw, 16) if raw and raw != "0x" else 0

    def erc1155_balance(self, owner: str, token_id: str) -> int:
        data = ("0x" + SEL_BALANCE_OF_1155 + _word(owner)
                + _word(int(token_id)))
        return self.call_uint(CTF, data)

    def erc20_balance(self, token: str, owner: str) -> int:
        return self.call_uint(token, "0x" + SEL_BALANCE_OF_20 + _word(owner))

    def payout_denominator(self, condition_hex: str) -> int:
        return self.call_uint(CTF, "0x" + SEL_PAYOUT_DENOMINATOR + _word(condition_hex))

    def payout_numerator(self, condition_hex: str, index: int) -> int:
        return self.call_uint(
            CTF, "0x" + SEL_PAYOUT_NUMERATORS + _word(condition_hex) + _word(index))

    def is_approved_for_all(self, owner: str, operator: str) -> bool:
        data = "0x" + SEL_IS_APPROVED_FOR_ALL + _word(owner) + _word(operator)
        return self.call_uint(CTF, data) == 1

    def wallet_nonce(self, wallet: str) -> int:
        return self.call_uint(wallet, "0x" + SEL_WALLET_NONCE)

    def receipt(self, tx_hash: str):
        return self.call("eth_getTransactionReceipt", [tx_hash])


def fetch_positions(wallet: str, timeout: float = 20.0) -> list[dict]:
    r = requests.get(f"{DATA_API}/positions",
                     params={"user": wallet, "limit": 500},
                     headers=UA, timeout=timeout)
    r.raise_for_status()
    return r.json() or []


def fetch_closed_markets(condition_ids, timeout: float = 20.0) -> dict[str, dict]:
    """`{conditionId: market}` for the conditions gamma calls CLOSED.

    `closed=true` is mandatory: without it gamma returns nothing at all for a
    settled market, which reads as "unknown market" and would skip every
    winner we are here to redeem.
    """
    if not condition_ids:
        return {}
    out: dict[str, dict] = {}
    ids = list(condition_ids)
    for i in range(0, len(ids), 20):
        chunk = ids[i:i + 20]
        r = requests.get(f"{GAMMA_API}/markets",
                         params=[("condition_ids", c) for c in chunk]
                                + [("closed", "true")],
                         headers=UA, timeout=timeout)
        r.raise_for_status()
        for market in r.json() or []:
            cid = str(market.get("conditionId") or "").lower()
            if cid:
                out[cid] = market
    return out


def read_env_file(path: str) -> dict[str, str]:
    """KEY=VALUE lines. Never echoed; the values here are the whole account."""
    out: dict[str, str] = {}
    p = Path(path)
    if not p.exists():
        return out
    for line in p.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        v = v.strip()
        if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
            v = v[1:-1]
        out[k.strip()] = v
    return out


def pick(env: dict[str, str], names) -> str | None:
    for n in names:
        v = env.get(n) or os.environ.get(n)
        if v:
            return v
    return None


# ---------------------------------------------------------------------------
# The run
# ---------------------------------------------------------------------------

def log_line(path: str, record: dict) -> None:
    record = {"ts": int(time.time()), **record}
    try:
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        with p.open("a") as fh:
            fh.write(json.dumps(record, separators=(",", ":")) + "\n")
    except OSError as exc:
        print(f"redeem-sweeper: could not write log {path}: {exc}", file=sys.stderr)
    print(json.dumps(record, separators=(",", ":")), file=sys.stderr)


def read_state(path: str) -> dict:
    try:
        return json.loads(Path(path).read_text())
    except (OSError, json.JSONDecodeError):
        return {}


def write_state(path: str, state: dict) -> None:
    try:
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(state))
    except OSError:
        pass


def verify_on_chain(rpc: Rpc, wallet: str, candidates, markets) -> tuple[list, list]:
    """Keep only the candidates the chain confirms are resolved, ours, and won.

    This is where data-api stops being believed. A condition survives if gamma
    calls it closed, the CTF has a payout denominator for it, and we hold a
    balance of a token whose numerator is nonzero.
    """
    verified, dropped = [], []
    for cand in candidates:
        market = markets.get(cand.condition_id)
        if not gamma_is_settled(market):
            dropped.append({"slug": cand.slug, "condition_id": cand.condition_id,
                            "reason": "not_closed_on_gamma"})
            continue
        denominator = rpc.payout_denominator(cand.condition_hex)
        if denominator <= 0:
            # Resolved for gamma, not yet reported to the CTF. Including it
            # would revert the batch and strand every other winner in it.
            dropped.append({"slug": cand.slug, "condition_id": cand.condition_id,
                            "reason": "no_payout_reported"})
            continue
        cand.resolved = True
        tokens = token_ids_from_gamma(market)
        if len(tokens) != len(BINARY_INDEX_SETS):
            dropped.append({"slug": cand.slug, "condition_id": cand.condition_id,
                            "reason": "not_a_binary_market"})
            continue
        expected = 0
        for index, token_id in enumerate(tokens):
            balance = rpc.erc1155_balance(wallet, token_id)
            if balance <= 0:
                continue
            cand.holdings[token_id] = balance
            numerator = rpc.payout_numerator(cand.condition_hex, index)
            expected += balance * numerator // denominator
        if not cand.holdings:
            # data-api's redeemable flag going stale is the documented case:
            # it kept showing the paid-zero conditions as redeemable.
            dropped.append({"slug": cand.slug, "condition_id": cand.condition_id,
                            "reason": "no_on_chain_balance"})
            continue
        if expected <= 0:
            dropped.append({"slug": cand.slug, "condition_id": cand.condition_id,
                            "reason": "holds_only_losers"})
            continue
        cand.expected_micros = expected
        verified.append(cand)
    verified.sort(key=lambda c: c.expected_micros, reverse=True)
    return verified, dropped


def build_calls(path: str, candidates, need_approval: bool):
    """`[(target, calldata, label)]` for one relayer batch.

    The approval rides in the same batch as the first redemption — atomic, so
    a granted approval can never outlive a failed redeem. That is exactly how
    the third-party batch we verified against did it.
    """
    calls = []
    if path == "adapter":
        if need_approval:
            calls.append((CTF, encode_set_approval_for_all(ADAPTER, True),
                          "setApprovalForAll(adapter)"))
        for cand in candidates:
            calls.append((ADAPTER,
                          encode_redeem_positions(PUSD, cand.condition_id),
                          f"adapter.redeem {cand.slug}"))
    else:
        for cand in candidates:
            calls.append((CTF,
                          encode_redeem_positions(USDCE, cand.condition_id),
                          f"ctf.redeem {cand.slug}"))
    return calls


def submit_batch(client, wallet: str, calls, nonce: int, deadline: int):
    from py_builder_relayer_client.models import DepositWalletCall
    payload = [DepositWalletCall(target=t, value="0", data=d) for t, d, _ in calls]
    return client.execute_deposit_wallet_batch(
        calls=payload, wallet_address=wallet, nonce=str(nonce),
        deadline=str(deadline))


def await_receipt(rpc: Rpc, tx_hash: str, timeout_s: int = 180):
    """Poll the chain for the receipt. The relayer's state machine is a hint;
    the receipt is the only thing that can answer "did it pay"."""
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        receipt = rpc.receipt(tx_hash)
        if receipt:
            return receipt
        time.sleep(3)
    return None


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--wallet", default=os.environ.get("PMT_DEPOSIT_WALLET",
                                                       DEFAULT_WALLET))
    ap.add_argument("--rpc-url", default=os.environ.get("PMT_POLYGON_RPC", DEFAULT_RPC))
    ap.add_argument("--relayer-url", default=os.environ.get("PMT_RELAYER_URL",
                                                            DEFAULT_RELAYER))
    ap.add_argument("--l0-env", default=DEFAULT_L0_ENV)
    ap.add_argument("--builder-env", default=DEFAULT_BUILDER_ENV)
    ap.add_argument("--log", default=os.environ.get("PMT_REDEEM_LOG", DEFAULT_LOG))
    ap.add_argument("--state", default=os.environ.get("PMT_REDEEM_STATE", DEFAULT_STATE))
    ap.add_argument("--path", choices=("auto", "adapter", "ctf"), default="auto",
                    help="auto picks adapter when the wallet has approved it, "
                         "else the CTF+USDC.e path")
    ap.add_argument("--grant-approval", action="store_true",
                    help="prepend the one-time CTF setApprovalForAll(adapter) "
                         "that unlocks the direct-pUSD path. A human decision.")
    ap.add_argument("--min-value", type=float, default=0.0,
                    help="data-api currentValue floor for a candidate")
    ap.add_argument("--max-calls", type=int, default=MAX_CALLS_PER_BATCH)
    ap.add_argument("--dry-run", action="store_true",
                    help="print the plan; read no key and submit nothing")
    args = ap.parse_args(argv)

    if args.grant_approval and args.path == "ctf":
        ap.error("--grant-approval only means anything on the adapter path")

    wallet = args.wallet
    rpc = Rpc(args.rpc_url)

    try:
        positions = fetch_positions(wallet)
    except (requests.RequestException, ValueError) as exc:
        log_line(args.log, {"status": "error", "reason": "data_api_unreachable",
                            "detail": str(exc)})
        return EXIT_CONFIG

    candidates, skipped = candidate_conditions(positions, args.min_value)

    try:
        markets = fetch_closed_markets([c.condition_id for c in candidates])
        verified, dropped = verify_on_chain(rpc, wallet, candidates, markets)
        usdce_before = rpc.erc20_balance(USDCE, wallet)
        pusd_before = rpc.erc20_balance(PUSD, wallet)
        adapter_approved = rpc.is_approved_for_all(wallet, ADAPTER)
    except (requests.RequestException, ValueError, RuntimeError) as exc:
        log_line(args.log, {"status": "error", "reason": "chain_unreachable",
                            "detail": str(exc)})
        return EXIT_CONFIG

    # The unwrapped-USDC.e watch runs every tick, redemption or not: the
    # window it is watching for opens precisely when we have just been paid.
    state = read_state(args.state)
    first_seen, note = unwrapped_note(usdce_before,
                                      state.get("unwrapped_since"), time.time())
    state["unwrapped_since"] = first_seen
    if not args.dry_run:
        write_state(args.state, state)

    if args.path == "auto":
        redeem_path = "adapter" if (adapter_approved or args.grant_approval) else "ctf"
    else:
        redeem_path = args.path
    if redeem_path == "adapter" and not adapter_approved and not args.grant_approval:
        log_line(args.log, {"status": "error", "reason": "adapter_not_approved",
                            "detail": "run once with --grant-approval, or use "
                                      "--path ctf"})
        return EXIT_CONFIG
    need_approval = redeem_path == "adapter" and not adapter_approved

    base = {"path": redeem_path, "adapter_approved": adapter_approved,
            "conditions": [{"slug": c.slug, "condition_id": c.condition_id,
                            "expected": round(c.expected_micros / MICROS, 6)}
                           for c in verified],
            "expected": round(sum(c.expected_micros for c in verified) / MICROS, 6),
            "pusd_before": round(pusd_before / MICROS, 6),
            "usdce_before": round(usdce_before / MICROS, 6)}
    if skipped:
        base["skipped"] = skipped
    if dropped:
        base["dropped"] = dropped
    if note:
        base["unwrapped"] = note

    if not verified:
        # The grant rides with a redemption and there is none, so it did NOT
        # happen — say so, or the next run silently falls back to the CTF path
        # and whoever ran this believes the fast path is armed.
        if need_approval:
            base["grant_deferred"] = "nothing to redeem; approval not granted"
        log_line(args.log, {"status": "idle", **base})
        return EXIT_OK

    batch = verified[:args.max_calls - (1 if need_approval else 0)]
    if len(batch) < len(verified):
        base["deferred_to_next_run"] = len(verified) - len(batch)
        base["conditions"] = base["conditions"][:len(batch)]
        base["expected"] = round(sum(c.expected_micros for c in batch) / MICROS, 6)
    calls = build_calls(redeem_path, batch, need_approval)

    if args.dry_run:
        log_line(args.log, {"status": "dry_run", **base,
                            "calls": [{"target": t, "label": lbl,
                                       "data": d} for t, d, lbl in calls]})
        return EXIT_OK

    # --- everything past here touches key material -------------------------
    l0 = read_env_file(args.l0_env)
    builder = read_env_file(args.builder_env)
    private_key = pick(l0, PRIVATE_KEY_NAMES)
    b_key = pick(builder, BUILDER_KEY_NAMES)
    b_secret = pick(builder, BUILDER_SECRET_NAMES)
    b_passphrase = pick(builder, BUILDER_PASSPHRASE_NAMES)
    if not private_key or not (b_key and b_secret and b_passphrase):
        log_line(args.log, {"status": "error", "reason": "missing_credentials",
                            "detail": "l0 key names tried "
                                      f"{list(PRIVATE_KEY_NAMES)}; builder key "
                                      f"names tried {list(BUILDER_KEY_NAMES)}",
                            **base})
        return EXIT_CONFIG

    try:
        from py_builder_relayer_client.client import RelayClient
        from py_builder_signing_sdk.config import BuilderConfig
        from py_builder_signing_sdk.sdk_types import BuilderApiKeyCreds
        client = RelayClient(
            relayer_url=args.relayer_url, chain_id=CHAIN_ID,
            private_key=private_key,
            builder_config=BuilderConfig(local_builder_creds=BuilderApiKeyCreds(
                key=b_key, secret=b_secret, passphrase=b_passphrase)),
            rpc_url=args.rpc_url)
        signer_address = client.signer.address()
        nonce_payload = client.get_nonce(signer_address, "WALLET") or {}
        relayer_nonce = int(nonce_payload.get("nonce"))
    except Exception as exc:                       # noqa: BLE001 - creds/transport
        log_line(args.log, {"status": "error", "reason": "relayer_setup_failed",
                            "detail": str(exc), **base})
        return EXIT_CONFIG

    chain_nonce = rpc.wallet_nonce(wallet)
    if relayer_nonce != chain_nonce:
        # A batch of ours is still in flight. Signing against a nonce the
        # wallet has not consumed gets the batch rejected at best; waiting one
        # timer tick costs nothing and the candidates do not expire.
        log_line(args.log, {"status": "deferred", "reason": "nonce_in_flight",
                            "relayer_nonce": relayer_nonce,
                            "chain_nonce": chain_nonce, **base})
        return EXIT_OK

    deadline = int(time.time()) + DEADLINE_SECONDS
    try:
        response = submit_batch(client, wallet, calls, relayer_nonce, deadline)
    except Exception as exc:                       # noqa: BLE001 - relayer refusal
        log_line(args.log, {"status": "error", "reason": "submit_failed",
                            "detail": str(exc), **base})
        return EXIT_TX_FAILED

    mined = response.wait()
    tx_hash = (mined or {}).get("transactionHash") or response.transaction_hash
    base["relayer_id"] = response.transaction_id
    base["tx"] = tx_hash
    base["nonce"] = relayer_nonce
    if not tx_hash:
        log_line(args.log, {"status": "error", "reason": "no_tx_hash", **base})
        return EXIT_TX_FAILED

    receipt = await_receipt(rpc, tx_hash)
    if receipt is None:
        log_line(args.log, {"status": "error", "reason": "receipt_timeout", **base})
        return EXIT_TX_FAILED
    if str(receipt.get("status", "")).lower() not in ("0x1", "1"):
        log_line(args.log, {"status": "error", "reason": "tx_reverted", **base})
        return EXIT_TX_FAILED

    # The whole point of L43: read what it PAID, not what it returned.
    if redeem_path == "adapter":
        paid = decode_payouts(receipt, redeemer=ADAPTER, collateral=USDCE)
        credited = erc20_credited(receipt, PUSD, wallet, from_addr=ZERO_ADDRESS)
        credited_token = "pUSD"
    else:
        paid = decode_payouts(receipt, redeemer=wallet, collateral=USDCE)
        credited = erc20_credited(receipt, USDCE, wallet, from_addr=CTF)
        credited_token = "USDC.e"

    expected = {c.condition_id: c.expected_micros for c in batch}
    problems = grade_payment(expected, paid, credited)
    base["paid"] = round(sum(paid.values()) / MICROS, 6)
    base["credited"] = round(credited / MICROS, 6)
    base["credited_token"] = credited_token

    if problems:
        # Nine PayoutRedemption events and $0 is what a wrong collateral looks
        # like. Fail the unit so systemd shows it rather than logging a win.
        log_line(args.log, {"status": "error", "reason": "payout_assertion_failed",
                            "problems": problems, **base})
        return EXIT_PAYOUT_ZERO

    log_line(args.log, {"status": "redeemed", **base})
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
