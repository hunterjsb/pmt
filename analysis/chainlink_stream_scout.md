# Chainlink settlement-stream feasibility scout (2026-08-23)

Commissioned by the settlement-rule discovery (`analysis/fourh_fit.md`, `analysis/settlement_rule_check.py`):
the crypto up/down markets settle on Chainlink's **60s-TWAP data stream**, not on anything we
currently read. `pmengine` prices those windows off Binance klines plus a measured basis guard.
This scouts what it would take to consume the real settlement object, and what the fallbacks cost.

**Headline: the settlement object is already free, public, unauthenticated, and live — but only
going forward.** Polymarket's own RTDS websocket relays it at 1 Hz on topic
`crypto_prices_twap_sixty`. There is no history, no replay, and no on-chain copy. Every hour we
do not record it is calibration data that cannot be recovered.

Everything below was verified live today unless marked otherwise. Reproduction commands are in
the appendix.

---

## 1. ACCESS — what Chainlink Data Streams is, and whether we can have it

### What it is

Data Streams is **pull-based**, not push. Chainlink's own words
(<https://docs.chain.link/data-streams>):

> Chainlink's push-based oracles regularly publish price data onchain. By contrast, Chainlink
> Data Streams relies on a pull-based design, letting you retrieve a report and verify it onchain
> whenever you need it.

Concretely, that means it is a **hosted API**, not a contract. There is no `AggregatorV3Interface`
to call, no `latestRoundData()`, no proxy address. You fetch a DON-signed report blob off an HTTP
or WS endpoint, and if you want on-chain trust you pass the blob to a `VerifierProxy.verify()`
call that checks the signatures and hands the decoded report **back to your calling contract**.
`verify()` neither stores nor emits the price (docs:
`/data-streams/reference/data-streams-api/onchain-verification`). This is the structural reason
there is no free on-chain shadow — see §2.

This is a **different product from the Polygon push feeds we already read.** Our
`pmtrader/polymarket/chainlink.py` walks `AggregatorV3` proxy rounds on Polygon
(`0xc907E116...` for BTC/USD etc.). Those are Data **Feeds**. The settlement object is a Data
**Stream**. Different pipeline, different cadence, different value.

### Is it accessible without paying? No.

| Probe | Result |
|---|---|
| `GET https://api.dataengine.chain.link/api/v1/reports/latest?feedID=0x0002ee67…` (no headers) | `HTTP 400` — `Headers.UserId`, `Headers.Timestamp`, `Headers.HmacSignature` all `required` |
| same against `api.testnet-dataengine.chain.link` | identical `HTTP 400` — **testnet is gated too** |
| `GET https://priceapi.dataengine.chain.link/api/v1/symbol_info` (Candlestick API, no headers) | `HTTP 401` — `Unauthorized - Authorization header is required` |
| `wss://ws.dataengine.chain.link/api/v1/ws?feedIDs=…` (no auth) | `HTTP 400` |

Auth is HMAC-SHA256 over the request with a UUID user id + shared secret
(`Authorization`, `X-Authorization-Timestamp`, `X-Authorization-Signature-SHA256`).

Sign-up is self-serve but **not free**. From <https://docs.chain.link/data-streams/sign-up>:

> All Data Streams subscriptions are paid. There is no free account tier.

Prerequisites include "a credit card or supported payment method for stream subscriptions
(billing is handled through Stripe)"; feed selection starts at **$150/month**. Billing switched
to subscription — the old pay-per-verification model is deprecated
(<https://docs.chain.link/data-streams/billing>).

### The TWAP streams do exist in the public catalog, with IDs

The catalog is public even though the data is not. `https://docs.chain.link/data-streams/crypto-streams`
embeds the whole feed directory as serialized page data (17.7 MB). Extracted entries — all
`status: live`, `schema: v2` (the `0x0002…` feed-ID prefix), `feedCategory: "custom"`,
`sourceChain: 42161` (Arbitrum One), `proxyAddress: null`, `contractAddress: 0x000…000`:

| Pair | 60s stream ID | 30s stream ID |
|---|---|---|
| BTC/USD | `0x0002ee6757e8822c00d273bc340fc24c9cafe123a4ff2ea1dbdb31944bc7d95f` | `0x0002e6b03af5a87b65f4c5c0c8eaf6027e4156f467cd16bdcf8a5b4347f9c087` |
| ETH/USD | `0x0002fbf9f58ef89c93385770d289d7c10c01c250641daa811063e303ee095593` | `0x00020ee5ba8c3b643145974fd94552d591f2a3bb165c196ccf4c30ba600fbffd` |
| SOL/USD | `0x00028f809156a08fbb1baa92e4e7ee9f056dd262e86df0fa1e74637be7725676` | `0x000248f549418ab11af95961667f03378b4c26174662e2ac3075f7a01d3c8bef` |
| XRP/USD | `0x0002d8a05177b903142185df7a75b7d6fd5d535ca1050e9b6c051a49ee1ad3da` | `0x0002d7781a55cbd01bd705d3e7f662e5d385bd29147d21d0421bb04d789614b9` |
| DOGE/USD | `0x00021251b82fb66152ed854da5b7228fca7fa6165305e06c564c5604e884073b` | `0x0002376c3f32f981c7847057949db4f8ca4905ce9825d5996db910d8926cd685` |
| BNB/USD | `0x000207429501edf217ef5939ca5f51e0e3a792a042ad75f3da4fd8ce5378e9c9` | `0x0002a5e83361be0767d4967f321bc2db02f5f774c2991a5744d9aa22e6fbdb19` |

Also present: LINK, TRX, HYPE, ZEC (30s and 60s each), plus a mirror set on Arbitrum Sepolia
(`sourceChain: 421614`, `status: testing`). Every one of our six updown symbols is covered, and
**30s and 60s variants both exist for all of them** — which matters for the width question in §3.

Two caveats worth carrying:

- `feedCategory: "custom"`. Whether a $150/mo self-serve subscription can even buy a custom feed
  is **unverified** — the sign-up page only advertises "individual feeds" and points custom plans
  at sales.
- The TWAP streams have **no prose documentation at all**. `data-streams/llms-full.txt` (654 KB,
  the complete docs corpus) contains exactly one incidental mention of the word TWAP, in an
  unrelated migration checklist. There is no published spec for sampling boundaries, weighting,
  or rounding. Polymarket says so explicitly (<https://docs.polymarket.com/market-data/chainlink-twap>):
  > Chainlink does not currently publish the custom feed's sampling boundaries, weighting,
  > rounding, or missing-input behavior, so do not independently reproduce the value without a
  > specification from Chainlink.

### The `data.chain.link` UI is not a data source

`https://data.chain.link/streams/btc-usd-twap-60s-streams` sits behind a **Vercel Security
Checkpoint**. Plain `curl` gets `HTTP 429` on repeat; a headless Chrome renders
`"Failed to verify your browser / Code 29"`. There is no public JSON endpoint behind it that a
browser reads unauthenticated — the value is fetched by an authenticated app-side call.
(The sibling directory `https://reference-data-directory.vercel.app/feeds-matic-mainnet.json`
*is* public and returns 200, but that is the **push feeds** catalog — the same
`AggregatorV3` proxies we already read, not streams. Useful confirmation of our feed metadata:
BTC/USD Matic `heartbeat: 27`, `threshold: 0.1`, `proxyAddress: 0xc907E116054Ad103354f2D350FD2514433D57F6f`.)

### But: Polymarket relays the stream for free

**This is the finding that changes the plan.** `wss://ws-live-data.polymarket.com/` — the RTDS —
is public and unauthenticated, and it carries the settlement object itself.

Subscribe frame (verified live, 2026-08-23 08:28 UTC):

```json
{"action":"subscribe","subscriptions":[
  {"topic":"crypto_prices_twap_sixty","type":"update","filters":"{\"symbol\":\"btc/usd\"}"}]}
```

`filters` is a JSON-**encoded string** for the chainlink/twap topics (a bare CSV symbol list for
the Binance topic); omit it for all symbols; `type` accepts `"update"` or `"*"`. Documented
keepalive is a plain-text `PING` frame every 5 s, and it is not optional: a capture without it
went silent after 228 s (08:28:54→08:32:42 UTC) while the socket stayed open; the same capture
with the keepalive ran its full duration.

Literal payload:

```json
{"connection_id":"gYIzuU-D1WeIKEjo-A==",
 "payload":{"full_accuracy_value":"76227641250796072337408",
            "symbol":"btc/usd","timestamp":1787473709000,
            "value":76227.64125079608,"window_s":60},
 "timestamp":1787473710555,"topic":"crypto_prices_twap_sixty","type":"update"}
```

`full_accuracy_value` is an E18 fixed-point **string** — 18 decimals, the Data Streams report
scale, **not** the 8-decimal Polygon aggregator. Parse it as an integer; `value` is a lossy
display float. `payload.timestamp` is the Chainlink observation time; the envelope `timestamp` is
when Polymarket emitted it.

Topics that exist (a bad topic name returns a `401` body naming it, which is how the registry was
enumerated):

| topic | source | what it carries | symbols seen live |
|---|---|---|---|
| `crypto_prices` | Binance | spot/last, `btcusdt` style | 6 |
| `crypto_prices_chainlink` | Chainlink | spot/last, `btc/usd` style, no `window_s` | 8 |
| `crypto_prices_twap_thirty` | Chainlink | **30s TWAP**, `window_s: 30` | 8 |
| `crypto_prices_twap_sixty` | Chainlink | **60s TWAP**, `window_s: 60` | 8 |

The eight Chainlink-side symbols are `btc/usd, eth/usd, sol/usd, xrp/usd, doge/usd, bnb/usd,
hype/usd, zec/usd` — precisely the intersection of the TWAP stream catalog with Polymarket's
updown roster. Measured cadence and latency from a 482-second capture of all four topics
(13 999 messages, 2026-08-23 08:33–08:41 UTC):

| topic | msgs | median gap | median relay lag | p95 relay lag |
|---|---|---|---|---|
| `crypto_prices` (Binance) | 2891 | 1.00 s | 177 ms | 251 ms |
| `crypto_prices_chainlink` | 3702 | 1.00 s | 1324 ms | 1917 ms |
| `crypto_prices_twap_thirty` | 3704 | 1.00 s | 1403 ms | 1996 ms |
| `crypto_prices_twap_sixty` | 3702 | 1.00 s | 1387 ms | 1986 ms |

"Relay lag" is envelope timestamp minus payload observation timestamp. **The Chainlink family
runs ~1.4 s behind its own observation time**, vs ~0.18 s for the Binance mirror, and both figures
reproduced to within 30 ms across two separate captures. That is a real constraint on tail-snipe:
inside the last few seconds of a window the reference price we can see is over a second stale, and
the final settlement print is not observable until after the boundary.

**The one hard limitation: no history.** Polymarket's docs are explicit —
"Subscriptions start with the next update. There is no snapshot, history, or replay after a
disconnect." Our own connect confirmed it: values begin at the current second, no backfill. So
the feed cannot be used to re-grade the past, and a 4h window's start reference only exists if we
were already connected four hours earlier.

### Semantics check — is `crypto_prices_twap_sixty` really a 60s TWAP?

Yes, and it is a *rolling* one updated every second, not a discrete bucket. Comparing each
`twap_sixty` print against the trailing mean of the `crypto_prices_chainlink` prints over the
preceding 60 s and 30 s:

| sym | n | vs trailing 60 s: median | p90 | max | vs trailing 30 s: median | p90 |
|---|---|---|---|---|---|---|
| bnb | 416 | 0.125 bp | 0.384 | 0.856 | 1.050 | 2.995 |
| btc | 416 | 0.134 | 0.502 | 1.211 | 1.037 | 3.067 |
| eth | 415 | 0.286 | 0.814 | 1.534 | 2.293 | 6.221 |
| xrp | 415 | 0.294 | 0.904 | 1.563 | 2.742 | 6.158 |
| sol | 416 | 0.306 | 0.607 | 1.259 | 1.968 | 4.739 |
| doge | 416 | 0.348 | 0.945 | 1.759 | 2.314 | 6.110 |

The 60s hypothesis fits roughly an order of magnitude better than the 30s one for every symbol.
The `window_s` field is telling the truth, and the residual (0.1–0.35 bp) is just the difference
between a true time-weighted integral and our 1 Hz sampled mean of the same underlying.

---

## 2. THE ON-CHAIN SHADOW — there isn't one

Short answer: **the settlement value never touches a blockchain.** Only the binary payout does.

### These markets are not UMA-resolved

Polymarket's public resolution docs still describe UMA as the only mechanism and list only the
`UmaCtfAdapter` addresses. That is stale for this market class. Verified directly against Polygon
(`eth_getLogs` on ConditionalTokens `0x4D97DCd97eC945f40cF65F87097ACe5EA0476045`,
`ConditionResolution` topic0 `0xb44d84d3289691f71497564b85d4233648d9dbae8cbdbb4329f301c3a0185894`):

```
btc-updown-4h-1787457600
  conditionId 0xf2c7f4fad7a7b7585408ea1674827c43c6ff2520dc67a7ef3edce7967574ff7d
  tx          0x4ded8d855cb7093dd711600aa5733658fe831386c1fb3871a42401f49011c576  (block 92511373)
  oracle      0x58e1745bEdDa7312C4CDDb72618923da1b90eFdE     <- NOT a UmaCtfAdapter
  payouts     [0, 1]                                         <- matches gamma outcomePrices ["0","1"]
```

The same oracle address appears on the 5m and 15m tenors. It is an automated reporter contract
(4295 bytes of code, unverified on Sourcify for chain 137) that gets `reportPayouts(bytes32,uint256[])`
called on it by an operator account through an ERC-4337 bundler.

### The resolution transaction contains no price

Full receipt for the tx above — six logs, none of them a Chainlink verifier:

```
0x4337084d… (ERC-4337 EntryPoint)   topic0 0xbb47ee3e…   0 bytes
0x58e1745b… (resolver)              topic0 0xc50439cd…   320 bytes  -> (marketId, payouts)
0x4d97dcd9… (ConditionalTokens)     topic0 0xb44d84d3…   320 bytes  -> ConditionResolution
0x4337084d… (EntryPoint)            topic0 0x49628fd1…   256 bytes  -> UserOperationEvent
0x0000…1010 (MATIC precompile)      ×2                              -> gas
```

No report bytes, no verified price, no TWAP value. The comparison happens entirely inside
Polymarket's off-chain operator; the chain learns only `[1,0]` or `[0,1]`. **There is nothing
on-chain to audit the reference price against, and no free exact feed of settlement prints hiding
in a verifier contract.**

That is consistent with the stream architecture: `VerifierProxy.verify()` returns the decoded
report to its caller and does not persist or emit it, so even a Polygon deployment would leave no
readable trace. (The catalog's only non-zero verifier entries are the "Global" /
"GlobalTestnet" addresses `0x534a7FF707Bc862cAB0Dda546F1B817Be5235b66` /
`0xA403a4a521be034B4A0D54019aF469A207094246`; `eth_getCode` for the former on Polygon returns
zero bytes. Not deployed there, and moot either way.)

### gamma fields, for the record

`https://gamma-api.polymarket.com/events?slug=btc-updown-{5m,15m,4h}-…`, fetched today:

```
resolutionSource      = "https://data.chain.link/streams/btc-usd-twap-60s-streams"   (ALL tenors)
automaticallyResolved = true          (once resolved; null while the market is still open)
umaResolutionStatus   = "resolved"    (vestigial — set with no UMA involvement)
umaResolutionStatuses = "[]"          (empty; there is no proposal/dispute lifecycle)
marketMakerAddress    = ""
'oracle' key          = ABSENT from the market object entirely
```

The reliable discriminator for this market class is `automaticallyResolved` plus a
`resolutionSource` pointing at `data.chain.link`. Do **not** read `umaResolutionStatus` as
evidence of UMA.

**Note for the 5m tier:** every tenor — 5m included — names `btc-usd-twap-60s-streams`. Some
third-party write-ups claim 5m uses the 30s stream; live gamma contradicts them. See §3 for why
our code currently uses a 30s width at 5m anyway, and why that is not the same question.

### The August cutover

An April market (`btc-updown-5m-1775181000`, endDate 2026-04-03) still carries
`resolutionSource = "https://data.chain.link/streams/btc-usd"` — **spot**, with a
"Bitcoin price at the end of the time range … greater than or equal to the price at the beginning"
description. So the TWAP rule is recent; secondary write-ups put the cutover around 2026-08-07,
which matches our own corpus (`fourh_fit.py` already notes "the 60s-TWAP regime (post
2026-08-07)"). Any replay reaching back past that boundary is fitting a **different settlement
rule** against a **different feed**, and must switch at the cutover.

---

## 3. FALLBACK LADDER — ranked, and quantified

The rungs, best to worst:

| # | Source | Fidelity to settlement | Cost | Verdict |
|---|---|---|---|---|
| 0 | **RTDS `crypto_prices_twap_sixty`** | it *is* the settlement object | free, public, 1 Hz | **take it** |
| 1 | Chainlink Data Streams direct (`api.dataengine.chain.link`) | identical value, lower relay lag | ≥$150/mo, custom-feed access unverified | not worth it |
| 2 | Polygon `AggregatorV3` rounds, 60s step-TWAP (what we do now for grading) | margin error med 1.3–4.5 bp at 5m lag, p99 2.6–9.5 | free, already built | good backstop |
| 3 | Faster polling of `latestRoundData` | **no gain** — see below | free | pointless |
| 4 | Binance klines + measured basis (what the engine prices with today) | margin error med 2.0–7.7 bp, p99 13–43 bp | free, already built | the thing to replace |

### Why rung 3 is a dead end

Two independent reasons.

**It adds no information.** `chainlink.fetch_rounds` already walks *every* round in the phase.
Polling `latestRoundData` more often cannot surface a value the aggregator never published — it
only shortens the delay before we notice one. The Polygon aggregator's cadence, measured over the
54-hour corpus in `~/.pmt/corpus/chainlink-*.jsonl`:

| sym | rounds | span | median gap | p90 gap | max gap | updates per aligned 60s window | 0 upd | 1 upd | 2 upd | ≥3 |
|---|---|---|---|---|---|---|---|---|---|---|
| btc | 6014 | 54.2 h | 33 s | 35 | 57 | 1.85 | 0.0% | 19.4% | 76.9% | 3.8% |
| eth | 6181 | 54.2 | 33 | 34 | 53 | 1.90 | 0.0% | 18.2% | 74.9% | 7.0% |
| sol | 5916 | 54.2 | 33 | 35 | 54 | 1.82 | 0.0% | 20.3% | 77.7% | 2.0% |
| xrp | 6167 | 54.2 | 33 | 34 | 53 | 1.90 | 0.0% | 17.9% | 75.5% | 6.6% |
| doge | 5993 | 54.1 | 33 | 35 | 59 | 1.84 | 0.0% | 19.6% | 77.2% | 3.2% |
| bnb | 5728 | 51.1 | 33 | 34 | 54 | 1.87 | 0.0% | 18.7% | 76.7% | 4.6% |

Every 60s window contains at least one update, and usually two. That's the whole budget: the
"TWAP" we reconstruct is a two-step staircase approximating a 60-sample average.

**Dropping the averaging is actively harmful.** Using the latest round *instead of* the 60s
step-TWAP, at real 15m boundaries over the corpus:

| sym | n | median \|Δ\| | p90 | p99 | max | **verdict flips** | median \|true margin\| |
|---|---|---|---|---|---|---|---|
| btc | 214 | 1.77 bp | 6.49 | 14.38 | 20.32 | **5.1%** | 13.7 bp |
| eth | 215 | 2.55 | 7.44 | 16.19 | 41.11 | **7.4%** | 17.8 |
| sol | 215 | 3.28 | 13.22 | 24.65 | 31.83 | **7.0%** | 26.8 |
| xrp | 215 | 6.25 | 18.94 | 40.54 | 201.98 | **6.1%** | 53.7 |
| doge | 215 | 5.00 | 15.86 | 23.37 | 36.59 | **9.8%** | 38.6 |
| bnb | 202 | 1.94 | 5.98 | 19.58 | 31.47 | **4.0%** | 18.8 |

A point-in-time oracle read flips 4–10% of 15-minute windows relative to the TWAP. Any
"just poll the oracle faster" instinct is a 5%-error instinct.

### Rung 2 vs rung 0, measured directly against the real thing

With `twap_sixty` in hand this stops being a guess. Comparing the Polygon 60s step-TWAP
reconstruction against the RTDS `twap_sixty` print at the same second, over the capture window:

| sym | n | level error med | p90 | p99 | max | *latest-round-only* level error med | p90 | max |
|---|---|---|---|---|---|---|---|---|
| btc | 463 | 0.65 bp | 1.68 | 2.64 | 3.28 | 1.24 | 3.60 | 19.31 |
| eth | 462 | 1.59 | 2.81 | 4.29 | 4.77 | 2.44 | 9.51 | 18.15 |
| bnb | 463 | 1.77 | 3.86 | 4.63 | 4.68 | 1.13 | 3.13 | 4.20 |
| xrp | 462 | 1.79 | 5.13 | 5.85 | 5.98 | 2.67 | 7.98 | 17.44 |
| sol | 463 | 2.06 | 4.12 | 4.64 | 5.23 | 1.10 | 4.05 | 10.71 |
| doge | 463 | 3.01 | 7.92 | 8.66 | 9.01 | 2.75 | 8.68 | 11.43 |

Level error is not what decides a window — the *margin* is (two TWAPs differenced, so correlated
bias cancels). Same capture, differencing at a **300 s lag** (a real 5m window length) and at 60 s:

| sym | lag | n | margin err med | p90 | p99 | max | median \|true margin\| |
|---|---|---|---|---|---|---|---|
| btc | 300 s | 171 | **1.27 bp** | 2.23 | 2.56 | 2.61 | 19.10 bp |
| eth | 300 s | 170 | **1.40** | 4.44 | 5.76 | 5.83 | 20.96 |
| bnb | 300 s | 171 | **1.45** | 3.88 | 4.35 | 4.38 | 11.16 |
| sol | 300 s | 171 | **3.22** | 5.10 | 6.65 | 6.94 | 22.02 |
| xrp | 300 s | 170 | **3.27** | 7.30 | 7.78 | 7.86 | 22.53 |
| doge | 300 s | 171 | **4.53** | 7.88 | 9.52 | 9.62 | 18.73 |
| btc | 60 s | 391 | 0.99 | 1.76 | 2.71 | 3.31 | 2.52 |
| eth | 60 s | 389 | 1.56 | 4.92 | 6.73 | 7.34 | 6.13 |
| sol | 60 s | 391 | 1.73 | 3.11 | 4.42 | 4.66 | 6.20 |
| xrp | 60 s | 390 | 1.85 | 5.61 | 6.66 | 6.83 | 7.32 |
| bnb | 60 s | 391 | 2.40 | 4.86 | 6.32 | 6.58 | 2.28 |
| doge | 60 s | 391 | 4.96 | 7.42 | 11.35 | 11.75 | 6.11 |

Compare rung 2's 300 s margin error against rung 4's, measured over the full 54-hour corpus at
real 5m boundaries (below): btc 1.27 vs 2.00, eth 1.40 vs 2.73, bnb 1.45 vs 2.54, sol 3.22 vs
3.93, doge 4.53 vs 6.17, xrp 3.27 vs 7.68. **The oracle reconstruction roughly halves the median
error, and cuts the p99 by ~5× for btc/eth** (2.6/5.8 bp vs 13.3/18.5 bp).

Sample caveat: this is an 8-minute snapshot, and it is regime-dependent — an earlier, quieter
227-second capture put every one of these numbers at roughly half. Treat the *ordering* as solid,
the absolute values as one busy-tape observation, and the 54-hour corpus tables as the durable
measure.

btc/eth/bnb reconstruct the settlement margin to ~1.5 bp. sol/xrp land near 3 bp, doge near 4.5 bp
— all of which is still under every deployed guard, and all of which is dwarfed by the median
window margin (11–23 bp).

### Rung 4's margin error, over the full 54-hour corpus

The engine's current quantity — `|margin_binance − margin_chainlink_corpus|` at real window
boundaries, i.e. exactly what the basis guard exists to absorb:

| sym | dur | n | median | p90 | p99 | max | deployed guard |
|---|---|---|---|---|---|---|---|
| btc | 5m | 648 | 2.00 bp | 6.39 | 13.31 | 20.38 | 6 |
| btc | 15m | 214 | 1.85 | 6.12 | 10.92 | 16.85 | 6 |
| eth | 5m | 648 | 2.73 | 9.50 | 18.49 | 26.48 | 8 |
| eth | 15m | 215 | 2.51 | 6.46 | 15.06 | 26.40 | 8 |
| bnb | 5m | 610 | 2.54 | 7.33 | 15.14 | 19.10 | — |
| bnb | 15m | 202 | 2.29 | 5.96 | 9.64 | 11.50 | — |
| sol | 5m | 648 | 3.93 | 11.20 | 21.64 | 53.64 | 10 |
| sol | 15m | 215 | 3.35 | 8.95 | 34.43 | 60.48 | 10 |
| doge | 5m | 648 | 6.17 | 17.71 | 40.46 | 91.53 | — |
| doge | 15m | 215 | 5.00 | 13.30 | 33.74 | 109.85 | — |
| xrp | 5m | 648 | 7.68 | 23.87 | 43.02 | 81.14 | — |
| xrp | 15m | 215 | 6.30 | 17.38 | 27.55 | 29.49 | — |

The deployed guards (btc 6, eth 8, sol 10) sit right around each symbol's p90. That is a
deliberate-looking calibration, and it is why the gate-passing result below comes out clean — but
it also means the guard is throwing away every window thinner than a p90 basis excursion. xrp and
doge carry 3–4× the error of btc, which is exactly why they have never been armed.

The same basis is now observable **live at 1 Hz** rather than reconstructed after the fact —
`|twap_sixty − Binance spot|` over the 8-minute capture:

| sym | n | median | p90 | p99 | max |
|---|---|---|---|---|---|
| bnb | 461 | 3.14 bp | 6.77 | 10.66 | 15.31 |
| btc | 461 | 3.89 | 8.36 | 16.56 | 26.92 |
| doge | 461 | 4.52 | 13.71 | 21.25 | 27.06 |
| xrp | 460 | 4.83 | 14.41 | 19.53 | 23.42 |
| sol | 461 | 5.53 | 12.19 | 17.11 | 26.89 |
| eth | 460 | 6.12 | 13.62 | 25.96 | 31.38 |

(Same regime caveat: the quiet 227 s capture gave 2.2–3.4 bp medians for the same quantity. The
point is not the level, it is that `updown_oracle.rs`'s 20-second `latestRoundData` poll can be
replaced by a 1 Hz direct read of the exact disagreement it is trying to estimate.)

### Rung 4 (today's engine) vs rung 2, on real resolutions

Scored against `~/.pmt/corpus/outcomes.jsonl` (345 windows). `wallet`-sourced rows are the only
non-circular ground truth: `chainlink`-sourced rows were graded *by* the rung-2 rule, so its score
on those is 100% by construction and is shown only for context.

```
-- ground truth = wallet (n=139 windows with kline coverage) --
   Binance terminal rule (rung 4)    : 131/139 = 94.24%
   Chainlink-corpus rule (rung 2)    : 133/139 = 95.68%
   head-to-head where they disagree (n=2): binance 0, chainlink 2
   |margin| of Binance-rule misses  : med 1.7 bp, max 8.7 bp   (all windows: med 12.3 bp)
   |margin| of Chainlink-rule misses: med 0.6 bp, max 3.2 bp
     15m: n=35   binance 32/35   chainlink 32/35
      5m: n=104  binance 99/104  chainlink 101/104
```

Sliced by how thin the window was:

| \|margin\| band | n (wallet) | rung 4 (Binance) | rung 2 (Chainlink corpus) |
|---|---|---|---|
| 0–5 bp | 25 | 72.0% | 76.0% |
| 5–10 bp | 35 | 97.1% | **100.0%** |
| 10–20 bp | 41 | 100.0% | 100.0% |
| ≥20 bp | 38 | 100.0% | 100.0% |

**And the finding that shapes the whole verdict:**

```
GATE-PASSING windows (|margin| >= the deployed per-symbol basis guard): n=99
   Binance rule   99/99 = 100.00%      losses that passed the guard: 0
   Chainlink rule 99/99 = 100.00%      losses that passed the guard: 0
```

*Every* disagreement between the Binance proxy and reality lives strictly below the deployed basis
guards. The guards are doing exactly the job they were paid for. So the real stream is **not** a
loss-prevention upgrade at current settings — it is an **opportunity unlock**. The guards refuse
**40 of 139 wallet windows (29%)** for sitting under their symbol's threshold; the sub-10 bp band
where the proxy is unreliable holds 60 of 139 (43%). A feed that reads the settlement quantity
directly is the only thing that makes that band tradeable.

### One thing the ladder cannot settle: the settlement width

`outcomes.ck_settlement_width_s` uses **30 s at 5m closes, 60 s above**. Scored against real
resolutions using the Polygon rounds:

```
ground truth = wallet:
   5m :  30s width 101/104   |  60s width 100/104
   15m:  30s width  35/35    |  60s width  32/35
```

30 s wins at *both* tenors — but gamma says every tenor resolves against the **60 s** stream.
These are not contradictory: with a 33-second heartbeat, a "30 s window" over the step function is
close to *the last round value*, while the 60 s window blends two rounds with stale weight. The
30 s width is a better **estimator given a coarse input**, not evidence about the market rule.

This question dissolves the moment we record the real feed: subscribe to `twap_thirty` and
`twap_sixty` simultaneously and check which reproduces settled outcomes. That is a one-day
experiment once the recorder exists, and it is worth doing because `ck_settlement_width_s`
currently encodes a guess that is *probably wrong about the rule* while being right about the
estimator.

---

## 4. VERDICT — recommended architecture, and what Phase 3 builds first

### Feed architecture for the `eval_model` re-spec

**Primary: RTDS `crypto_prices_twap_sixty` over `wss://ws-live-data.polymarket.com/`.**

- **Source of truth for the settlement quantity.** Not "a better proxy" — the actual value
  Polymarket's own resolver compares. Free, unauthenticated, 1 Hz per symbol, all six of our
  symbols plus hype/zec.
- **Cadence** 1 Hz; **relay lag** ~1.4 s median / ~1.9 s p95 behind observation time.
- **Fidelity to actual settlement: exact**, modulo that ~1.4 s lag and our own recording gaps.
- Ride it alongside the Binance feed rather than replacing it. Binance stays the **volatility
  and momentum** input (176 ms lag, real depth, real trades); the TWAP topic becomes the
  **level and reference** input. `eval_model`'s `ref` and `spot` come from `twap_sixty`;
  σ keeps coming from Binance klines.
- **What the basis guard becomes:** with both ends of the digital read off the settlement object,
  `basis_guard_bp` stops being a model correction and becomes a **feed-health guard** — refuse
  when the TWAP feed is stale, when the recorded window-start reference is missing, or when
  `twap_sixty` and the Binance mark disagree by more than the live-measured basis — which we can
  now observe directly at 1 Hz instead of estimating from a 20-second oracle poll. Do not delete
  the guards; re-point them.

**Backstop: the existing Polygon round corpus** (`chainlink.py`), unchanged. It is the only source
that can grade the *past*, it survives an RTDS outage, and it reconstructs the settlement margin
to under 1 bp for btc/eth/bnb. Keep it as the grader's fallback and as the cross-check that
detects an RTDS feed defect.

**Do not buy Data Streams.** ≥$150/mo, custom-feed eligibility unverified, and all it buys over
RTDS is some fraction of that ~1.4 s relay lag (an unknown part of which is Chainlink-side and
unavoidable) plus a DON-signed payload we have no on-chain use for. Revisit only if RTDS proves
unreliable, or if sub-second reference latency turns out to be the binding constraint on
tail-snipe — which the recorder will tell us.

### Engineering sizes

| Work | Size | Notes |
|---|---|---|
| RTDS TWAP recorder (Python, `pmtrader`) | **S** | `websockets` is already a dependency (`gamewatch.py`); ~150 lines; append-only JSONL under `~/.pmt/corpus/` |
| Re-point `outcomes.chainlink_outcome` at the recorded tape, Polygon rounds as fallback | **S** | pure function, existing tests |
| Settle the 30s-vs-60s width empirically | **S** | falls out of the recorder after one day |
| `pmengine` RTDS feed + `eval_model` re-spec | **M** | new WS consumer beside the Binance one; `updown_oracle.rs` already owns per-arm oracle state and a 20 s poller to model it on; needs staleness handling, replay support, and a replay A/B before it touches sizing |
| Direct Data Streams subscription | **L** + cost | HMAC client, subscription, custom-feed access negotiation — and no benefit over RTDS |

### What Phase 3 of issue #4 should build first

**1. The recorder — today, before anything else.** RTDS has no history, no snapshot, no replay.
Every window that closes while we are not connected is a settlement print that can never be
recovered, and the corpus we would need to validate an `eval_model` re-spec only starts existing
once something is writing it down. Record all four topics (`crypto_prices`,
`crypto_prices_chainlink`, `crypto_prices_twap_thirty`, `crypto_prices_twap_sixty`) for all
symbols, at full 1 Hz, append-only to `~/.pmt/corpus/`, with the 5 s `PING` keepalive and
reconnect-on-drop. Persist `full_accuracy_value` as the string — never round-trip through a
float. This is a **measurement-only** build and needs no gate.

Note the sizing consequence: a 4h window's start reference exists only if the recorder was up 4
hours earlier, so it wants to be a resident service (`pmt` engine-style pidfile + timestamped
logs), not a script someone remembers to run.

**2. Re-point the grader.** `outcomes.chainlink_outcome` should prefer the recorded TWAP tape and
fall back to the Polygon rounds when the tape has a hole. This immediately upgrades every
outcome row from "reconstruction" to "the actual settled quantity", and it makes the 30s-vs-60s
width question answerable from data instead of argument.

**3. Then, and only then, the `eval_model` re-spec.** With a week of recorded tape you can replay
the terminal rule against the true reference and measure what the currently-refused 0–10 bp band
is actually worth — which is the whole prize, since §3 shows the guards already protect us
everywhere above it. Usual rules apply: replay A/B win, one small-size live night, full size.

**4. Deprioritize the rest of Phase 4 relative to this.** Multi-venue feeds, the market-data bus,
and latency telemetry are all still worth doing, but none of them changes what we are pricing.
This does.

---

## Appendix — reproduction

```bash
# 1. RTDS is public: subscribe and watch the settlement object arrive
python - <<'EOF'
import json
from websockets.sync.client import connect
sub = {"action":"subscribe","subscriptions":[
    {"topic":"crypto_prices_twap_sixty","type":"update","filters":"{\"symbol\":\"btc/usd\"}"}]}
with connect("wss://ws-live-data.polymarket.com/") as ws:
    ws.send(json.dumps(sub))
    for _ in range(5):
        print(ws.recv())
EOF
# keepalive: send the text frame "PING" every 5s or the flow stops after ~4 minutes

# 2. Data Streams is gated, mainnet and testnet alike
curl -s "https://api.dataengine.chain.link/api/v1/reports/latest?feedID=0x0002ee6757e8822c00d273bc340fc24c9cafe123a4ff2ea1dbdb31944bc7d95f"
curl -s "https://api.testnet-dataengine.chain.link/api/v1/reports/latest?feedID=0x0002e64f0b0166fa748cc05cd510a11442be16279873574f98c8cfa06b42b3dd"
# both -> HTTP 400, Headers.UserId / Timestamp / HmacSignature required

# 3. The public stream catalog (feed IDs) is embedded in the docs page data
curl -s https://docs.chain.link/data-streams/crypto-streams > cs.html   # ~17.7 MB
#   then grep for '"name":[0,"…TWAP…"]' and the following '"feedId":[0,"0x…"]'

# 4. The resolution transaction carries no price
#   eth_getLogs on 0x4D97DCd97eC945f40cF65F87097ACe5EA0476045
#   topic0 0xb44d84d3289691f71497564b85d4233648d9dbae8cbdbb4329f301c3a0185894
#   topic1 = the market's conditionId  ->  oracle 0x58e1745bEdDa7312C4CDDb72618923da1b90eFdE
#   eth_getTransactionReceipt on the tx -> 6 logs, no verifier, payout vector only
```

The two raw captures behind the RTDS tables are on disk at `~/Desktop/code/.rtds4.jsonl`
(228 s, no keepalive, quiet tape) and `~/Desktop/code/.rtds5.jsonl` (661 s / 19 038 messages,
with keepalive, busier tape). Both are outside `~/.pmt/` and outside the repo — they are the first
and so far only recording of the settlement object we have, and they should be folded into
whatever corpus path the recorder ends up using rather than left where they are.

Measurement scripts for the tables above were throwaway; the reusable pieces already live in
`pmtrader/polymarket/chainlink.py` (`twap_over_window`, `load_corpus`, `fetch_rounds`),
`pmtrader/polymarket/outcomes.py` (`chainlink_outcome`, `ck_settlement_width_s`) and
`analysis/fourh_fit.py` (`terminal_state`, `window_winner_terminal`).
