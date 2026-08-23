# Deposit wallet accounts (`PM_SIGNATURE_TYPE=3`)

The account class Polymarket started issuing after 2026-05-04. The funder is a
**contract** — a "deposit wallet" — that validates orders itself through
EIP-1271, rather than a proxy/Safe the owner EOA signs on behalf of.

## What actually differs from type 1/2

Everything outside the signature is the same. Auth is unchanged: L1 is the
EOA's `ClobAuthDomain` signature, L2 is the same HMAC over the same canonical
path. Funder plumbing is the same (`maker` = funder). The plain
`clob.polymarket.com` host accepts these orders.

Two things change, both inside the order:

1. **The `signer` FIELD of the order struct holds the deposit wallet, not the
   EOA.** For types 0/1/2 it is the EOA. Here it doubles as the EIP-1271
   verifying contract, so the wallet re-derives its own domain from it.
   (The EOA is still what actually signs — it just never appears in the struct.)

2. **The signature is an ERC-7739 / Solady `TypedDataSign` wrapper**, not a bare
   65-byte ECDSA blob:

   ```
   0x | 65-byte ECDSA | appDomainSeparator(32) | contentsHash(32)
      | contentsTypeString | uint16 len(contentsTypeString)
   ```

   The ECDSA half signs
   `keccak(0x1901 ‖ appDomainSeparator ‖ typedDataSignStructHash)`, where the
   struct hash binds the Order contents to the wallet's own domain
   (`name="DepositWallet"`, `version="1"`, `chainId`, `verifyingContract=order.signer`,
   `salt=0`). `appDomainSeparator` is the CTF Exchange V2 domain — and the
   exchange address depends on the token's **neg-risk** flag, so that lookup is
   an input to signing, not just to routing.

Type 3 is V2-only; the V1 exchange rejects it.

## Where it lives

`polymarket_client_sdk_v2` 0.7.0 implements all of the above
(`SignatureType::Poly1271`). pmengine only maps the env value onto it
(`src/client.rs`). The shape is pinned against the Python reference by
`pmengine/tests/poly1271_golden.rs` — frozen vectors in `tests/vectors/`,
regenerated ONLY from `gen_poly1271_vectors.py` (py-clob-client-v2 1.1.0),
never from the Rust side.

## Operator env

```
PM_PRIVATE_KEY=0x...            # the OWNER EOA key
PM_FUNDER_ADDRESS=0x...         # the deposit wallet CONTRACT address
PM_SIGNATURE_TYPE=3
```

Type 3 with no funder is refused at authentication — the deposit wallet has no
derivation from the EOA the way a proxy or Safe does, so it must be given.
