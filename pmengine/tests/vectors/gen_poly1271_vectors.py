#!/usr/bin/env python3
"""Freeze golden EIP-712 / ERC-7739 order-signing vectors from the REFERENCE
implementation (py-clob-client-v2), so `poly1271_golden.rs` can prove the Rust
order path signs a deposit-wallet order byte-identically — offline, with no
Python in the loop.

Deposit-wallet accounts (PM_SIGNATURE_TYPE=3, "POLY_1271") are the only class
whose signature is not a bare 65-byte ECDSA blob, so a shape mismatch here is
invisible until the CLOB rejects a live order. Vectors pin the whole shape:
the Order struct hash, the exchange contract picked per neg-risk, and the
wrapped signature the CLOB actually receives.

Run (regenerating only ever from the reference, never from Rust):

    uv venv .venv && VIRTUAL_ENV=.venv uv pip install py-clob-client-v2==1.1.0
    .venv/bin/python gen_poly1271_vectors.py > poly1271.json

KEYS ARE TEST KEYS. Never point this at a funded account.
"""

import json

from eth_account import Account
from eth_utils import keccak

from py_clob_client_v2.config import get_contract_config
from py_clob_client_v2.order_utils.exchange_order_builder_v2 import (
    DEPOSIT_WALLET_DOMAIN_SALT,
    DEPOSIT_WALLET_NAME_HASH,
    DEPOSIT_WALLET_VERSION_HASH,
    ORDER_TYPE_HASH,
    ORDER_TYPE_STRING,
    SOLADY_TYPE_HASH,
    SOLADY_TYPE_STRING,
    ExchangeOrderBuilderV2,
)
from py_clob_client_v2.order_utils.model.order_data_v2 import OrderDataV2
from py_clob_client_v2.order_utils.model.side import Side
from py_clob_client_v2.order_utils.model.signature_type_v2 import SignatureTypeV2
from py_clob_client_v2.signer import Signer
from eth_abi import encode as abi_encode

CHAIN_ID = 137  # Polygon; the only chain the engine trades

# Well-known throwaway keys. `EOA_KEY` stands in for the owner key that signs;
# `WALLET_KEY` is only used to mint a deterministic, obviously-synthetic address
# to play the deposit wallet contract. Neither has ever held funds.
EOA_KEY = "0x0000000000000000000000000000000000000000000000000000000000000001"
WALLET_KEY = "0x0000000000000000000000000000000000000000000000000000000000000002"

# Fixed order fields — every degree of freedom nailed down so the vector is a
# pure function of the signing code.
SALT = 479249096354
TOKEN_ID = "71321045679252212594626385532706912750332728571942532289631379312455583992563"
MAKER_AMOUNT = "1000000"
TAKER_AMOUNT = "2500000"
TIMESTAMP = "1747000000000"
EXPIRATION = "0"
BYTES32_ZERO = "0x" + "00" * 32


def _hexs(b: bytes) -> str:
    return "0x" + b.hex()


def poly1271_intermediates(chain_id: int, exchange: str, order) -> dict:
    """Recompute the ERC-7739 chain of hashes so a Rust-side mismatch names the
    step that broke, not just the final blob."""
    domain_type_hash = keccak(
        text="EIP712Domain(string name,string version,uint256 chainId,address verifyingContract)"
    )
    app_domain_separator = keccak(
        primitive=abi_encode(
            ["bytes32", "bytes32", "bytes32", "uint256", "address"],
            [
                domain_type_hash,
                keccak(text="Polymarket CTF Exchange"),
                keccak(text="2"),
                chain_id,
                exchange,
            ],
        )
    )
    contents_hash = keccak(
        primitive=abi_encode(
            [
                "bytes32", "uint256", "address", "address", "uint256", "uint256",
                "uint256", "uint8", "uint8", "uint256", "bytes32", "bytes32",
            ],
            [
                ORDER_TYPE_HASH,
                int(order.salt),
                order.maker,
                order.signer,
                int(order.tokenId),
                int(order.makerAmount),
                int(order.takerAmount),
                int(order.side),
                int(order.signatureType),
                int(order.timestamp),
                bytes.fromhex(order.metadata[2:]),
                bytes.fromhex(order.builder[2:]),
            ],
        )
    )
    typed_data_sign_struct_hash = keccak(
        primitive=abi_encode(
            ["bytes32", "bytes32", "bytes32", "bytes32", "uint256", "address", "bytes32"],
            [
                SOLADY_TYPE_HASH,
                contents_hash,
                DEPOSIT_WALLET_NAME_HASH,
                DEPOSIT_WALLET_VERSION_HASH,
                chain_id,
                order.signer,
                DEPOSIT_WALLET_DOMAIN_SALT,
            ],
        )
    )
    digest = keccak(primitive=b"\x19\x01" + app_domain_separator + typed_data_sign_struct_hash)
    return {
        "app_domain_separator": _hexs(app_domain_separator),
        "contents_hash": _hexs(contents_hash),
        "typed_data_sign_struct_hash": _hexs(typed_data_sign_struct_hash),
        "digest": _hexs(digest),
    }


def make_case(name: str, sig_type: SignatureTypeV2, side: Side, neg_risk: bool) -> dict:
    eoa = Account.from_key(EOA_KEY)
    deposit_wallet = Account.from_key(WALLET_KEY).address

    cfg = get_contract_config(CHAIN_ID)
    exchange = cfg.neg_risk_exchange_v2 if neg_risk else cfg.exchange_v2

    signer = Signer(EOA_KEY, CHAIN_ID)
    builder = ExchangeOrderBuilderV2(exchange, CHAIN_ID, signer, generate_salt=lambda: SALT)

    # maker is always the funder; for POLY_1271 the *signer field* is the
    # deposit wallet too (it is the EIP-1271 verifying contract), while for
    # 0/1/2 it is the EOA. This is the only order-struct difference.
    maker = deposit_wallet if sig_type != SignatureTypeV2.EOA else eoa.address
    order_signer = None if sig_type != SignatureTypeV2.POLY_1271 else deposit_wallet

    order = builder.build_order(
        OrderDataV2(
            maker=maker,
            tokenId=TOKEN_ID,
            makerAmount=MAKER_AMOUNT,
            takerAmount=TAKER_AMOUNT,
            side=side,
            signer=order_signer,
            signatureType=sig_type,
            timestamp=TIMESTAMP,
            metadata=BYTES32_ZERO,
            builder=BYTES32_ZERO,
            expiration=EXPIRATION,
        )
    )
    typed_data = builder.build_order_typed_data(order)
    signature = builder.build_order_signature(typed_data)

    case = {
        "name": name,
        "chain_id": CHAIN_ID,
        "neg_risk": neg_risk,
        "exchange": exchange,
        "eoa": eoa.address,
        "deposit_wallet": deposit_wallet,
        "order": {
            "salt": str(order.salt),
            "maker": order.maker,
            "signer": order.signer,
            "tokenId": order.tokenId,
            "makerAmount": order.makerAmount,
            "takerAmount": order.takerAmount,
            "side": int(order.side),
            "signatureType": int(order.signatureType),
            "timestamp": order.timestamp,
            "metadata": order.metadata,
            "builder": order.builder,
            "expiration": order.expiration,
        },
        "signature": signature,
    }
    if sig_type == SignatureTypeV2.POLY_1271:
        case.update(poly1271_intermediates(CHAIN_ID, exchange, order))
    else:
        # Plain EIP-712: the digest IS the order's signing hash.
        case["digest"] = builder.build_order_hash(typed_data)
    return case


def main() -> None:
    doc = {
        "_source": "py-clob-client-v2==1.1.0",
        "_keys": "throwaway test keys, never funded",
        "order_type_string": ORDER_TYPE_STRING,
        "solady_type_string": SOLADY_TYPE_STRING,
        "cases": [
            make_case("eoa_buy", SignatureTypeV2.EOA, Side.BUY, False),
            make_case("poly1271_buy", SignatureTypeV2.POLY_1271, Side.BUY, False),
            make_case("poly1271_sell_negrisk", SignatureTypeV2.POLY_1271, Side.SELL, True),
        ],
    }
    print(json.dumps(doc, indent=2))


if __name__ == "__main__":
    main()
