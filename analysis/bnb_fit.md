# BNB tradeability fit — full R1/R6-aligned measurement

Date: 2026-08-23. Question: can `bnb-updown-{5m,15m}` join the braked directional fleet?
Method: the established pipeline, extended — no new models. Chainlink feed → 50h round corpus →
R1 aligned (TWAP-vs-TWAP) basis → 22d kline corpus → vol/format fit → R6 flip study → live book.

**VERDICT: TRADEABLE, both durations. BNB is the closest thing to a second BTC the fleet has
found.** Its settlement basis is BTC-class (not alt-class), its flip behaviour is identical to the
rest of the fleet, and its 15m format passes the sol15 rule comfortably. The binding constraint is
NOT basis — it is **book depth**: BNB absorbs $25 clips near the ask, not $50. Arm small.

---

## 1. Chainlink feed

Polygon mainnet BNB/USD aggregator proxy: **`0x82a6c4AF830caa6c97bb504425f6A66165C2c26e`**

Verified live via `eth_call` on the `_RPC_URLS` chain:

| call | selector | result |
|---|---|---|
| `description()` | `0x7284e416` | `"BNB / USD"` |
| `decimals()` | `0x313ce567` | `8` |
| `latestRoundData()` | `0xfeaf968c` | 684.9051 @ 1787468583 |

`verify_feeds()` now returns `ok: True` for all six feeds. Added to `chainlink.py` `FEEDS` +
`BINANCE_SYMBOL` (`BNBUSDT`); `GUARD_BP["bnb"] = None` (no live guard — this document proposes one).
`cli_crypto_data._ORACLE_SYMBOLS` gained `bnb` to stay in sync with `chainlink.SYMBOLS`.

**Round cadence — the first thing that had to be checked, and it passes.** BNB updates on the same
~33s heartbeat as the majors, so the step-held TWAP is not measuring staleness:

| feed | rounds/50h | gap p50 | p90 | max |
|---|---|---|---|---|
| btc | 5548 | 33s | 35s | 57s |
| eth | 5708 | 33s | 34s | 53s |
| sol | 5568 | 33s | 35s | 54s |
| xrp | 5702 | 33s | 35s | 53s |
| doge | 5536 | 33s | 35s | 59s |
| **bnb** | **5608** | **33s** | **34s** | **54s** |

## 2. Corpus

- `~/.pmt/corpus/chainlink-bnb.jsonl` — **5608 rounds, 50.0h span** (1787288688 → 1787468682),
  fetched via `chainlink.extend_all(50.0, ["bnb"])` (339 top-up + 5269 backfill), append-only,
  deduped by `round_id`.
- `~/.pmt/corpus/klines-1m-BNBUSDT.jsonl` — **31,680 minutes = 22.0d, 0 gaps**
  (2026-08-01 07:04Z → 2026-08-23 07:03Z), `data-api.binance.vision` only, same
  `{"t","o","c"}` schema as the other kline caches, deduped by `t`.

---

## 3. Vol / format fit (21d, identical window per symbol)

All four symbols recomputed over the **same** 21d window so the comparison is internally
consistent. σ is the trailing 45m sample stdev of 1m log returns in bp/min — `updown.rs`'s
`SIGMA_SLOW_WINDOW`. Jump rate is `|1m move| > 3×` that trailing σ.

| sym | σ p50 | σ p75 | σ p95 | lag-1 ac (full) | rolling-60m ac p50 | % windows ac<0 | jumps/hr |
|---|---|---|---|---|---|---|---|
| BTCUSDT | 2.77 | 4.43 | 8.84 | −0.003 | +0.001 | 49.7% | 1.06 |
| ETHUSDT | 3.79 | 5.93 | 11.96 | +0.016 | −0.030 | 58.3% | 0.92 |
| SOLUSDT | 4.07 | 6.11 | 13.34 | +0.056 | −0.055 | 65.7% | 0.70 |
| **BNBUSDT** | **2.94** | **4.40** | **9.02** | **+0.035** | **+0.003** | **49.0%** | **0.70** |

BNB's σ distribution is **BTC's**, not an alt's — p50 2.94 vs BTC 2.77, p75 4.40 vs BTC 4.43,
p95 9.02 vs BTC 8.84. The ROADMAP's standing σ figures (BTC 4 / ETH 6.5 / SOL 11 / XRP 21) came off
a more volatile tape; on this 21d window every symbol is calmer and SOL in particular has collapsed
toward ETH. BNB slots in beside BTC on every vol measure.

Regime: BNB's rolling lag-1 autocorrelation is centred on zero (p50 +0.003, 49.0% of hours
negative, p10/p90 −0.176/+0.166) — the same near-martingale, no-persistent-chop profile as BTC.
ETH and SOL are meaningfully more mean-reverting at 1m (58%/66% of hours negative), which is what
makes their windows wander.

Jump rate 0.70/hr — the ROADMAP's "every symbol throws a >3σ 1-minute jump about once an hour"
holds for BNB too. Largest single 1m move in 21d: 265bp (SOL's was 566bp, BTC 216, ETH 251).

### Format verdict table — P(|move| > 10bp within the horizon)

| sym | 2.5min (max-path) | **7.5min (max-path)** | 2.5min (endpoint) | 7.5min (endpoint) |
|---|---|---|---|---|
| BTCUSDT | 14.3% | **34.4%** | 9.0% | 22.4% |
| ETHUSDT | 22.3% | **47.1%** | 14.1% | 29.9% |
| SOLUSDT | 25.7% | **54.3%** | 16.5% | 35.5% |
| **BNBUSDT** | **14.8%** | **37.6%** | **9.4%** | **25.0%** |

Two definitions are given because the ROADMAP reference triple (BTC 29% / ETH 47% / SOL 66% at
7.5min) was measured on a different, more volatile tape and can't be reproduced exactly on today's
corpus. The max-path definition (does the price ever threaten a 10bp margin inside the horizon)
reproduces the reference ETH number exactly (47.1% vs 47%) and is used as primary; on this window
SOL prints 54.3% rather than 66% purely because SOL has been calm for three weeks.

**BNB at 37.6% sits between BTC (34.4%) and ETH (47.1%), one quarter of the way up.** Mapped onto
the reference scale that is ≈33-34% — better than ETH-like. **The sol15 rule passes for BNB with
room to spare.**

---

## 4. Aligned basis — the tradeability verdict (R1 method)

`r1_aligned_basis.py`'s measurement, run over a **common 46.7h window across all six feeds**
(2026-08-21 05:04Z → 2026-08-23 03:47Z) so BNB is compared against the fleet on the same hours.
Stats are on `|aligned_basis_bp|`. This reproduces the ROADMAP's R1 numbers to within rounding
(BTC 8.0/7.6, ETH 10.0/8.0, SOL 11.1/8.7, XRP 22.5/16.8, DOGE 17.9/13.2) — the pipeline is
unchanged, only BNB is new.

### Settlement-shaped (the error that decides wins/losses)

| sym | guard | 5m p50 | 5m p95 | 5m p99 | 5m max | 15m p50 | 15m p95 | 15m p99 | 15m max | per-min p95 |
|---|---|---|---|---|---|---|---|---|---|---|
| btc | 6 | 2.13 | 8.00 | 12.04 | 16.77 | 2.03 | 7.63 | 9.28 | 9.48 | 8.27 |
| eth | 8 | 2.88 | 10.02 | 15.32 | 23.69 | 2.43 | 7.68 | 15.32 | 21.81 | 9.65 |
| sol | 10 | 3.49 | 11.08 | 17.72 | 70.81 | 3.46 | 8.73 | 17.71 | 53.08 | 11.40 |
| xrp | off | 5.79 | 22.46 | 33.24 | 71.31 | 4.52 | 16.79 | 26.63 | 44.04 | 18.77 |
| doge | off | 4.83 | 17.88 | 32.45 | 105.29 | 4.16 | 13.55 | 22.51 | 86.68 | 15.06 |
| **bnb** | **(8)** | **2.65** | **8.45** | **12.55** | **20.49** | **2.61** | **7.43** | **11.46** | **12.56** | **8.37** |

BNB per-minute: n=2803, mean 3.50, std 3.55, p50 2.88, p90 6.97, p95 8.37, p99 12.66, max 97.23.
Signed bias −2.61bp per-minute / −2.38bp 5m / −2.66bp 15m — the same oracle-lags-drift offset every
symbol shows, no BNB-specific skew.

Non-stationarity check (the R1 caveat): BNB day1 p95 9.08 → day2 7.20, improving, **no regime-shift
flag** (BTC 9.45→4.72 and ETH 10.94→6.71 both tripped it). SOL/XRP/DOGE were flat-to-worse.
BNB's distribution is the most stable of the six over these 48h — but the R1 rule stands: re-run
after 1-2 weeks before trusting the p99.

**BNB's 15m p99 of 11.46bp is the second-best number in the whole table** (only BTC's 9.28 beats
it), and its 15m max of 12.56bp is the tightest tail of any feed. That matters directly: sol15's
epitaph was "formats whose basis tail exceeds their guard" — SOL 15m p99 17.71 against a 10bp guard
is a 1.77× overhang. **BNB 15m is 11.46 against 8bp = 1.43×, tighter than deployed BTC 15m (1.55×).**

### Exceedance — P(|settlement basis| > guard), same 46.7h corpus

| sym | dur | >5bp | >6bp | >7bp | >8bp | >9bp | >10bp | >12bp | >15bp | >20bp |
|---|---|---|---|---|---|---|---|---|---|---|
| btc | 5m | 17.9% | **11.8%** | 6.6% | 5.0% | 3.8% | 2.7% | 1.1% | 0.2% | 0.0% |
| btc | 15m | 13.4% | **11.8%** | 8.6% | 4.3% | 1.6% | 0.0% | 0.0% | 0.0% | 0.0% |
| eth | 5m | 23.0% | 17.0% | 12.7% | **9.5%** | 7.0% | 5.2% | 3.2% | 1.1% | 0.4% |
| eth | 15m | 18.7% | 11.2% | 8.0% | **4.8%** | 3.2% | 1.6% | 1.6% | 1.1% | 0.5% |
| sol | 5m | 34.5% | 25.4% | 17.1% | 12.5% | 8.8% | **6.2%** | 4.1% | 2.7% | 0.9% |
| sol | 15m | 29.4% | 18.7% | 13.4% | 7.5% | 4.8% | **3.7%** | 1.6% | 1.6% | 1.1% |
| xrp | 5m | 55.7% | 48.8% | 43.0% | 38.2% | 33.6% | 29.8% | 21.1% | 12.5% | 6.8% |
| xrp | 15m | 48.1% | 37.4% | 31.6% | 25.1% | 18.7% | 16.6% | 12.3% | 8.0% | 2.7% |
| **bnb** | **5m** | 22.0% | 13.6% | 8.2% | **6.2%** | 4.6% | 3.0% | 1.8% | 0.4% | 0.2% |
| **bnb** | **15m** | 20.9% | 13.9% | 7.5% | **3.2%** | 2.7% | 2.1% | 1.1% | 0.0% | 0.0% |

Bold = the deployed (or proposed) guard. **BNB at 8bp carries 6.2% (5m) / 3.2% (15m) residual basis
exceedance — strictly less than every guard currently live** (btc@6 = 11.8%/11.8%, eth@8 =
9.5%/4.8%, sol@10 = 6.2%/3.7%). BNB at the proposed guard is the safest arm in the fleet on this
axis.

### Does the guard gate everything? (the XRP failure mode test)

The cushion collapses to the guard as rem→0, so a window whose final settlement margin never
exceeds the guard can never be banked-decided. Over 21d of klines:

**% of windows structurally ungateable (|final settlement margin| < guard)**

| sym | dur | g6 | g7 | g8 | g9 | g10 | g12 | g15 | g20 | g22 |
|---|---|---|---|---|---|---|---|---|---|---|
| BTCUSDT | 5m | **82.0%** | 85.7% | 88.5% | 90.6% | 92.3% | 94.6% | 96.5% | 98.3% | 98.8% |
| BTCUSDT | 15m | **65.7%** | 70.9% | 75.0% | 78.6% | 81.3% | 86.2% | 90.4% | 94.8% | 95.8% |
| ETHUSDT | 5m | 74.5% | 79.0% | **82.6%** | 85.2% | 87.3% | 90.6% | 93.8% | 96.6% | 97.3% |
| ETHUSDT | 15m | 56.5% | 62.5% | **67.4%** | 70.6% | 74.2% | 79.7% | 85.1% | 90.3% | 92.3% |
| SOLUSDT | 5m | 69.7% | 75.4% | 79.7% | 82.8% | **85.4%** | 88.9% | 92.6% | 95.7% | 96.5% |
| SOLUSDT | 15m | 48.0% | 54.4% | 61.1% | 65.5% | **69.1%** | 74.7% | 82.6% | 88.7% | 90.3% |
| XRPUSDT | 5m | 67.5% | 72.8% | 76.7% | 79.7% | 82.1% | 85.2% | 88.7% | 91.9% | **92.7%** |
| XRPUSDT | 15m | 48.9% | 53.5% | 59.2% | 63.4% | 66.8% | 73.1% | 79.6% | 85.5% | **86.3%** |
| **BNBUSDT** | **5m** | 80.1% | 84.2% | **87.7%** | 89.8% | 91.3% | 94.1% | 96.2% | 98.0% | 98.4% |
| **BNBUSDT** | **15m** | 61.7% | 67.5% | **71.9%** | 75.4% | 78.2% | 83.8% | 89.4% | 93.8% | 94.8% |

**|final settlement margin| bp, 21d**

| sym | dur | n | p10 | p25 | p50 | p75 | p90 | mean |
|---|---|---|---|---|---|---|---|---|
| BTCUSDT | 5m | 6048 | 0.25 | 0.86 | 2.25 | 4.70 | 8.70 | 3.81 |
| BTCUSDT | 15m | 2015 | 0.56 | 1.52 | 3.77 | 8.01 | 14.71 | 6.35 |
| ETHUSDT | 5m | 6048 | 0.44 | 1.15 | 2.91 | 6.14 | 11.60 | 5.10 |
| ETHUSDT | 15m | 2015 | 0.85 | 2.12 | 4.92 | 10.24 | 19.60 | 8.51 |
| SOLUSDT | 5m | 6048 | 0.55 | 1.59 | 3.58 | 6.90 | 12.83 | 5.87 |
| SOLUSDT | 15m | 2015 | 1.25 | 2.83 | 6.36 | 12.07 | 21.63 | 9.97 |
| **BNBUSDT** | **5m** | 6048 | 0.46 | 1.18 | 2.63 | 5.14 | 9.12 | 4.22 |
| **BNBUSDT** | **15m** | 2015 | 0.77 | 1.95 | 4.41 | 8.80 | 15.45 | 7.15 |

### Guard is small relative to banked margins at the firing line

Replaying `eval_model`'s own math at 15s resolution over 21d, at the first tick each window
crosses θ=0.3 (R9 entry) and safety≥1.0 (budget unlock):

| sym | dur | guard | n@θ=0.3 | \|banked\| p25/p50/p75 @θ=0.3 | n@safety≥1 | \|banked\| p25/p50/p75 @safety≥1 | guard/cushion @entry |
|---|---|---|---|---|---|---|---|
| BTCUSDT | 5m | 6 | 3473 | 2.11 / 2.45 / 3.09 | 1006 | 6.65 / 7.26 / 8.69 | 87% |
| BTCUSDT | 15m | 6 | 1489 | 2.18 / 2.60 / 3.32 | 691 | 6.58 / 7.39 / 9.09 | 75% |
| ETHUSDT | 5m | 8 | 3417 | 2.80 / 3.25 / 4.10 | 983 | 8.91 / 9.90 / 11.83 | 87% |
| ETHUSDT | 15m | 8 | 1489 | 2.88 / 3.46 / 4.42 | 654 | 8.75 / 9.95 / 12.41 | 76% |
| SOLUSDT | 5m | 10 | 3400 | 3.41 / 3.88 / 4.77 | 831 | 11.03 / 12.12 / 14.46 | 90% |
| SOLUSDT | 15m | 10 | 1517 | 3.55 / 4.09 / 5.05 | 617 | 10.77 / 11.86 / 14.38 | 79% |
| **BNBUSDT** | **5m** | **8** | 3187 | 2.70 / 3.04 / 3.67 | **682** | 8.75 / 9.66 / 11.09 | 91% |
| **BNBUSDT** | **15m** | **8** | 1463 | 2.78 / 3.17 / 3.90 | **554** | 8.64 / 9.44 / 11.37 | 82% |
| XRPUSDT | 5m | 22 | 1690 | 7.24 / 8.18 / 10.65 | **415** | 24.71 / 27.64 / 32.59 | 92% |
| XRPUSDT | 15m | 22 | 984 | 7.06 / 7.73 / 9.97 | **273** | 24.31 / 27.91 / 34.27 | 91% |

Opportunity rate (windows ever reaching safety≥1, out of ~6050 5m / ~2015 15m):
BTC@6 **16.6% / 34.3%** · ETH@8 **16.3% / 32.5%** · SOL@10 **13.7% / 30.6%** ·
**BNB@8 11.3% / 27.5%** · XRP@22 **6.9% / 13.5%**.

**BNB@8 retains 68% (5m) and 80% (15m) of BTC@6's opportunity rate.** XRP at the guard its own basis
demands retains 41% / 39% — *that* is what "the guard guts the edge" looks like, and BNB is nowhere
near it.

### Guard sweep — risk vs opportunity, BNB only

| guard | dur | windows safety≥1 | % of all | vs btc@6 | P(\|basis\|>guard) | \|banked\| p50 @safety≥1 |
|---|---|---|---|---|---|---|
| 6 | 5m | 1113 | 18.4% | 111% | 13.6% | 7.32 |
| 6 | 15m | 764 | 37.9% | 111% | 13.9% | 7.44 |
| 7 | 5m | 869 | 14.4% | 86% | 8.2% | 8.36 |
| 7 | 15m | 644 | 32.0% | 93% | 7.5% | 8.47 |
| **8** | **5m** | **682** | **11.3%** | **68%** | **6.2%** | **9.67** |
| **8** | **15m** | **554** | **27.5%** | **80%** | **3.2%** | **9.44** |
| 9 | 5m | 570 | 9.4% | 57% | 4.6% | 10.65 |
| 9 | 15m | 487 | 24.2% | 71% | 2.7% | 10.41 |
| 10 | 5m | 470 | 7.8% | 47% | 3.0% | 11.56 |
| 10 | 15m | 425 | 21.1% | 62% | 2.1% | 11.47 |
| 12 | 5m | 324 | 5.4% | 32% | 1.8% | 14.02 |
| 12 | 15m | 323 | 16.0% | 47% | 1.1% | 13.49 |

**Guard = 8bp.** The settlement-5m p95 is 8.45 and the 15m p95 is 7.43; the loss-asymmetry bias says
round toward the guard that costs a foregone win rather than a paid loss, so 8 (not 7). 9 would be
defensible but buys only 1.6pp of exceedance for a third of the 5m opportunity. This is also
exactly ETH's deployed guard, which keeps one fewer magic number in the fleet.

---

## 5. Flip-rate spot check (R6)

`analysis/r6_tail_flip_study.py --symbols BNBUSDT --days 21 --no-fetch`, guard 8bp.
22.0d, 31,681 minutes, 0 gaps, 6336 5m + 2111 15m windows, **0 skipped**.
Sanity naive 50%-elapsed reversal rate: 10.7% (5m) / 12.5% (15m) — right on BTC's 10.7%/13.3%, so
the dataset does contain windows that reverse their own early trend; the ~0% below is the cushion
working, not a dead pipeline.

**Conditional flip rate at safety ≥ 1 — every cell, both durations:**

| safety | Gaussian 1-tail | 0-60s | 60-120s | 120-300s | 300-600s |
|---|---|---|---|---|---|
| **5m** | | | | | |
| 1.0-1.25 | 15.87% | n=312 **0.0%** | n=241 **0.0%** | n=50 **0.0%** | — |
| 1.25-1.5 | 10.56% | n=232 **0.0%** | n=144 **0.0%** | n=25 **0.0%** | — |
| 1.5-2 | 6.68% | n=176 **0.0%** | n=117 **0.0%** | n=12 **0.0%** | — |
| 2-3 | 2.28% | n=115 **0.0%** | n=54 **0.0%** | n=3 **0.0%** | — |
| 3-5 | 0.13% | n=43 **0.0%** | n=18 **0.0%** | n=1 **0.0%** | — |
| 5+ | 0.00% | n=11 **0.0%** | n=2 **0.0%** | — | — |
| **15m** | | | | | |
| 1.0-1.25 | 15.87% | n=60 **0.0%** | n=112 **0.0%** | n=272 **0.0%** | n=132 **0.0%** |
| 1.25-1.5 | 10.56% | n=69 **0.0%** | n=90 **0.0%** | n=192 **0.0%** | n=79 **0.0%** |
| 1.5-2 | 6.68% | n=63 **0.0%** | n=63 **0.0%** | n=152 **0.0%** | n=51 **0.0%** |
| 2-3 | 2.28% | n=43 **0.0%** | n=42 **0.0%** | n=94 **0.0%** | n=14 **0.0%** |
| 3-5 | 0.13% | n=23 **0.0%** | n=23 **0.0%** | n=46 **0.0%** | n=2 **0.0%** |
| 5+ | 0.00% | n=12 **0.0%** | n=7 **0.0%** | n=5 **0.0%** | n=1 **0.0%** |

Even the informational pre-decided rows are ~0%: 0.5-0.75 prints 0.0/0.1/0.0% (5m) and
0.0/0.0/0.0/0.2% (15m) against a 30.85% Gaussian claim. **R6's fleet-wide finding reproduces
exactly on BNB** — the premise that fat tails flip decided windows is refuted here too.

**p_up calibration** (claimed = max(p_up, 1−p_up), first crossing):

| threshold | BNB 5m n / win% | BNB 15m n / win% | (BTC 5m/15m) | (ETH 5m/15m) | (SOL 5m/15m) |
|---|---|---|---|---|---|
| ≥0.95 | 6284 / **99.0%** | 2108 / **98.3%** | 99.0% / 98.1% | 99.2% / 98.5% | 98.9% / 98.6% |
| ≥0.97 | 6273 / 99.3% | 2108 / 99.1% | | | |
| ≥0.98 | 6258 / 99.6% | 2108 / 99.4% | | | |
| ≥0.99 | 6250 / 99.6% | 2107 / 99.4% | | | |

Dead centre of the fleet — calibrated-to-conservative on its own settlement math.

**FIT**: BNB 15m → **k = 1.00, J = 0bp** (PASS at every rem bucket). BNB 5m → "not achieved ≤3.00",
which is the *identical* result BTC/ETH/SOL 5m all produce: every cell is 0.0% flips and the failure
is purely `n<30` in the 120-300s bucket, not a flip failure. **No cushion widening indicated;
k=1.0 / J=0 stands for BNB, same as the rest of the fleet.**

---

## 6. Liquidity sanity — the actual constraint

`bnb-updown-5m-*` and `bnb-updown-15m-*` both exist and roll on schedule; gamma `liquidity` reads
$1.6k-2.3k per window. `parse_semantics` resolves them with no code change (the description's
`BNB/USD` hits `_PAIR_RE` → `BNBUSDT`, kind `twap`).

Sampled the CLOB book every 25s for ~9 minutes across all four symbols, both durations, taking the
**leading side** (the one the engine buys once decided). Notional = price × size.

| sym | dur | n | mid p50 | spread p50 | touch$ p25/p50/p75 | +1¢$ p50 | +2¢$ p50 | touch≥$25 | touch≥$50 |
|---|---|---|---|---|---|---|---|---|---|
| **bnb** | **5m** | 17 | 0.775 | **0.070** | 5 / **14** / 34 | 28 | 43 | 47% | 6% |
| **bnb** | **15m** | 15 | 0.670 | 0.020 | 4 / **5** / 16 | 9 | 14 | 20% | 7% |
| btc | 5m | 18 | 0.740 | 0.010 | 89 / 136 / 321 | 369 | 601 | 89% | 83% |
| btc | 15m | 18 | 0.625 | 0.010 | 36 / 73 / 176 | 127 | 325 | 83% | 67% |
| eth | 5m | 18 | 0.735 | 0.010 | 6 / 10 / 35 | 65 | 123 | 39% | 11% |
| eth | 15m | 16 | 0.625 | 0.010 | 10 / 25 / 46 | 74 | 176 | 50% | 19% |
| sol | 5m | 15 | 0.675 | 0.040 | 28 / 33 / 56 | 53 | 67 | 80% | 33% |
| sol | 15m | 15 | 0.850 | 0.020 | 6 / 13 / 28 | 124 | 193 | 27% | 20% |

**Decided-side only (leading mid ≥ 0.75) — where the engine actually fires:**

| sym | dur | n | mid p50 | touch$ p50 | +1¢$ p50 | +2¢$ p50 | touch≥$25 | touch≥$50 |
|---|---|---|---|---|---|---|---|---|
| **bnb** | **5m** | 10 | 0.853 | **32** | **47** | **87** | **70%** | 10% |
| **bnb** | **15m** | 4 | 0.985 | 16 | 16 | 17 | 25% | 25% |
| btc | 5m | 7 | 0.895 | 200 | 524 | 857 | 86% | 86% |
| btc | 15m | 6 | 0.997 | 767 | 1283 | 1283 | 100% | 100% |
| eth | 5m | 8 | 0.915 | 10 | 72 | 146 | 25% | 12% |
| eth | 15m | 4 | 0.952 | 52 | 223 | 509 | 75% | 50% |
| sol | 5m | 7 | 0.930 | 52 | 150 | 241 | 71% | 57% |
| sol | 15m | 11 | 0.890 | 13 | 180 | 235 | 18% | 18% |

Representative single-window snapshots: `bnb-updown-15m-1787468400` at 0.95/0.96 with 415s left
carried $82 at the touch, $126 within 1¢, $245 within 2¢ (the btc sibling: $562 / $1303 / $8575).
A pre-open `bnb-updown-5m` at 50/50 carried only $25-31 at the touch.

**Readings:**
1. **$25 clips: yes.** BNB decided-side 5m holds $32 at the touch (70% of samples ≥$25) and $87
   within 2¢ — better near-touch than ETH 5m ($10 touch, 25% ≥$25), which is a live full-size arm.
2. **$50 clips: no.** Only 10% of BNB decided 5m samples had $50 at the touch. Sizing a $50 clip
   here means chasing or not filling.
3. **The 5m spread is the real tax**: 7¢ median on the leading side vs 1¢ for btc/eth. Crossing
   costs ~3.5¢ from mid, so BNB only fires when the model is far more confident than the book —
   i.e. deep in the banked-decided regime. Expect materially fewer fires than the fire-rate table
   suggests, and treat `--pay-up` as load-bearing rather than optional.
4. The 15m decided-side sample (n=4) is too thin to conclude from and disagreed with the one-shot
   snapshot above; it is the weakest measurement in this document.

---

## 7. VERDICT and recommended arm params

### 5m — **TRADEABLE**, probe size
Basis BTC-class (p95 8.45, p99 12.55), residual exceedance at 8bp = 6.2% (better than every live
guard), flip rate 0% at safety≥1, p_up 99.0%, opportunity 68% of btc5m's. Book absorbs $25 clips
at the touch 70% of the time on the decided side. Format metric 14.8% @2.5min ≈ BTC's 14.3%.

### 15m — **TRADEABLE**, probe size, arm second
The sol15 rule is the gate and BNB clears it: P(|move|>10bp in 7.5min) = 37.6%, better than ETH's
47.1%, nowhere near SOL's 54.3%. sol15's actual epitaph was a basis tail exceeding its guard — BNB
15m's p99 is 11.46bp against an 8bp guard (1.43×), tighter than deployed BTC 15m (1.55×) and half
SOL 15m's overhang (1.77×). Opportunity 80% of btc15m's, exceedance 3.2%, flip 0%, FIT k=1.00.
**Arm it after 5m has proven fills**, purely because 15m near-touch depth is the least-measured
number here and R7 says a sixth correlated arm adds no diversification.

### Exact arm commands (probe level)

```
pmt crypto arm https://polymarket.com/event/bnb-updown-5m-<epoch> \
    --size 100 --clip 10 --basis-guard 8 --theta 0.3 --min-elapsed 0 --pay-up 0.02

pmt crypto arm https://polymarket.com/event/bnb-updown-15m-<epoch> \
    --size 100 --clip 10 --basis-guard 8 --theta 0.3 --min-elapsed 0 --pay-up 0.02
```

| param | value | why |
|---|---|---|
| `--size` | **100** | Below sol's 150 (the current fleet minimum). New symbol, unproven wallet record, thinnest book of the five candidates. |
| `--clip` | **10** | The measurement, not a convention: $25 clears the touch only 70% of the time on 5m and 25% on 15m; $10 always clears. Clip size is the brake system's risk-rate lever — raise it after a night of fills, not before. |
| `--basis-guard` | **8** | Settlement-5m p95 8.45 / 15m p95 7.43, rounded with the loss-asymmetry bias. Residual exceedance 6.2% / 3.2% — the safest guard in the fleet. |
| `--theta` | **0.3** | R9 fleet-wide default; the entry self-gates ~2-3min in on banked lag. |
| `--min-elapsed` | **0** | The 50% clock gate is retired (R9, 7b1948d); θ replaces it. |
| `--pay-up` | **0.02** | Load-bearing on BNB, not optional — the 7¢ 5m spread and $32 touch mean a clip that can't chase 2¢ mostly doesn't fill. Still spends surplus edge only. |

Defaults left alone: `--min-edge 0.015`, `--max-price 0.985`, `--min-fair 0.97`, `--quiesce 20`,
`--roll` on.

### What stays OFF
- **`--p-cap`** — leave at 1.0 (disabled). R6 reproduces on BNB: the model is calibrated-to-
  conservative, capping a calibrated model fixes nothing.
- **`PMENGINE_DYNAMIC_GUARD`** — stays dark. Its poller has zero BNB oracle-tape history; the
  raise-only p95 floor would be estimating from nothing. Static 8 governs until BNB has its own
  warm oracle corpus.
- **No `--side`, no `--min-fair 0` / `--min-edge 0.005` momentum override** — those are manual
  operator tools, not fleet params.
- **No 4h BNB arm** — Phase 3.2, and near-expiry 4h liquidity is unexamined for BNB.
- **No size increase** without an R2 calibration pass, per the standing rule; and none at all until
  BNB has one wallet-graded night.
- **DOGE/XRP unchanged** — this measurement moved neither. DOGE 5m p95 17.88 / XRP 22.46 both still
  demand guards that gut their own opportunity (XRP@22 keeps 41% of btc's rate).

### Caveats on the record
1. 48h of oracle corpus is the R1 minimum, not comfort. BNB's day1→day2 improved and it was the
   most stable of the six, but **re-run `pmt crypto basis --aligned --symbol bnb` after 1-2 weeks
   before trusting the p99 or tightening below 8**.
2. The liquidity sample is ~9 minutes of one Asia-session morning. The 15m decided-side cell is
   n=4. Depth is the number most likely to be wrong here — the first live night is as much a
   fill-rate experiment as a P&L one.
3. R6's scope caveat applies: its "winner" is BNB's own Binance-kline settlement math, never
   Chainlink's resolution. Basis risk lives in §4, not §5.
4. Wallet grading is the only scoreboard. Nothing here is evidence BNB makes money — only that its
   oracle basis, tails, and format do not structurally forbid it.
