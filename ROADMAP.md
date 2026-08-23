# Updown Roadmap — from profitable night bot to measurable edge machine

Principle: **no change touches full-size capital until a replay of recorded reality says it's better.**
Tonight's −$370 window and both alt losses shared one cause — the model trusted itself where the
book, the oracle, or the tails knew better. Every phase below either records reality, replays it,
or submits a change to its judgment.

Standing truths this roadmap inherits (2026-08-23):
- Wallet-graded record is the ONLY scoreboard; the model never grades itself.
- Per-symbol vol is real: σ bp/min ≈ BTC 4 / ETH 6.5 / SOL 11+ / XRP 21; all pairs correlate
  0.65–0.82 at 1m — the fleet is closer to one levered bet than five independent ones.
- Every symbol throws a >3σ 1-minute jump about once an hour. Gaussian p_up ≥ 0.99 is fiction
  in exactly the region we trade.
- Shipped and live: auto-roll, per-arm basis guards (BTC 3bp, alts 6bp, XRP off), exposure
  release on unsubscribe, book-distrust brake (>15¢ claimed net), no-averaging-down brake,
  time-based full-budget unlock (last 120s).

## Phase 0 — Record everything (the corpus) · this week

The backtest is only as good as what we recorded. Books are not backfillable — every night we
don't record is a night the corpus can't judge.

- **Book tape**: best bid/ask + top-of-book size per side, every tick, per armed window
  (engine already holds the books; append `book-tape.jsonl` beside the eval tape).
- **Spot tape**: the engine's Binance spot ticks as seen (staleness included — the lag IS the data).
- **Chainlink rounds**: per-symbol oracle round history from Polygon RPC. Gives the TRUE
  Chainlink-vs-Binance basis distribution — replaces hand-set guards with measured ones and is
  the gate for any XRP re-entry.
- **Outcomes**: resolved winner per window (already flowing via wallet grading).
- **Sigma floor refresh on roll**: rolled arms currently clone the original arm-time σ floor
  forever; recompute from the feed's own trailing closes at each roll. (Smallest engine change,
  pending approval.)
- Backfillable now: klines, Chainlink rounds, resolved outcomes. Forward-only: books, spot ticks.

## Phase 1 — Replay harness (the judge) · next week

- Extract the decision core (`fair_p_up` + firing policy) behind a snapshot interface: a pure
  function of (recorded feed state, recorded book, params, t). No I/O in the core.
- `pmengine replay`: walk a corpus timestamp-faithfully — a decision at t sees only data ≤ t
  (look-ahead is the classic backtest lie). Fills are conservative: taker at recorded ask,
  capped at recorded ask size, fees applied.
- Outputs: per-window P&L, reliability diagram + ECE per stated-fair bucket, attribution by
  symbol / duration / session hour / ρ-regime, max drawdown.
- Acceptance test (non-negotiable): replaying the params we actually ran over the nights we
  actually recorded must reproduce the real fills within tolerance — including the −$370
  15m window AND a clean nothing-fired boundary. If the simulator can't reproduce last night,
  it can't judge next week.
- A/B protocol: candidate params vs baseline over the same corpus, bootstrap CI on the P&L
  delta. A change ships only on a positive CI, then runs one night at small size before full.

## Phase 2 — Research tracks (run on the harness)

Priority per the 2026-08-23 review: **R4 first** (does the documented 5m manipulation signature
exist in OUR books, and what does it cost by duration/moneyness), **R2 second** (reliability
diagrams by p-bucket × ρ-regime × time-to-expiry become the gate on any size increase), then
R3 → R5/R6/R1 → R7/R8.

- **R1 Oracle basis per symbol** — measured Chainlink-vs-Binance distribution → per-symbol
  guards with confidence, XRP verdict. Needs Phase 0 rounds data.
- **R2 Calibration gate + fractional Kelly** — clip size from quarter-Kelly on post-fee,
  post-haircut edge; hard per-window %-of-bankroll cap; no size increases until each p-bucket
  shows calibration over ≥30 decided windows. (Current reality check: stated fair ≥0.95 hits 92%.)
- **R3 Odds discipline** — test a max entry price (~0.70) for non-banked entries on the corpus.
  External research says high-price "sure things" are poor risk/reward after fees; our own tape
  can confirm or refute before we adopt it.
- **R4 5m vs 15m allocation** — the open contradiction: external research (Stanford/SMU, ~16k
  5m BTC contracts) finds near-settlement manipulation concentrated in 5m and prefers 15m; our
  wallet says 5m is 30-1 and 15m 5-1 including the −$370. Hypothesis: our 5m edge is real
  (flip-proof + manip cushion already defend it) and the 15m loss was a sizing/brake failure,
  now fixed — but the corpus decides: look for the reversing near-settlement flow spike
  signature in OUR recorded books before moving capital either way.
- **R5 Session + fast-ρ regimes** — add a 5–15m ρ estimate beside the 60m one; in negative-ρ
  regimes allow banked-decided entries only; attribute P&L by session (Asia chop vs London/NY)
  and gate accordingly.
- **R6 Fat tails** — jump-aware p_up (or a hard p_up cap ≈0.98 unless banked-decided). The
  once-an-hour >3σ jump rate says the Gaussian overpays exactly where we bet.
- **R7 Correlation-aware fleet cap** — cap total un-decided committed notional across arms;
  at ρ 0.7 the arms lose together.
- **R8 Near-even late-flow guard** — when the book is still ~50/50 in the final 30–60s and
  late Binance flow is abnormal, cut or zero the size multiplier: that combination is the
  manipulation fingerprint the 5m literature documents.

## Phase 3 — Strategy expansion (each gated by Phase 1)

1. **Maker mode** — rest quotes at fair-minus-edge; the fill-economics build (existing top
   backlog item). Prerequisite for everything two-sided.
2. **Tail-snipe** — final 30–60s, banked-decided/flip-proof only, a formalization of the mode
   we already trust.
3. **Complete-set scanner** — Up + Down < $1.00 after fees; structural, low-directional.
4. **Two-sided inventory rotator with directional tilt** — the pattern profitable wallets
   actually run at scale; biggest build, needs maker infra first.

## Operating rules while the roadmap runs

Posture: keep the braked directional fleet live at current (or modestly reduced) size,
instrument everything, force the harness to reproduce reality, then promote only what
survives R4 and the calibration gate.

- No size increases anywhere without an R2 calibration pass.
- Never loosen the three brakes (15¢ distrust, 2¢ no-averaging-down, final-120s unlock)
  or the basis guards without a replay A/B win — they encode the paid-for lessons.
- 5m arms stay at current size (experimental class) until R4 settles; XRP stays off until R1.
- Options-implied gaps and long-horizon mean-reversion findings are research inputs, never
  direct sizing inputs for the 5/15m fleet.
- No new strategy ships before the replay reproduces real nights.
- Every engine change: replay A/B win → one small-size live night → full size.
- Tape + wallet scoreboard are append-only ground truth; `~/.pmt/` is the durable home
  (scratchpads die nightly).
