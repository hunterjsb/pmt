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
- ~~**Polymarket trade flow**~~ **SHIPPED 2026-08-23 05:30Z** (6242ceb): book tape now carries
  signed print flow per sample (up/dn × n/buy_vol/sell_vol) — the VPIN/R8 input. Phase 0's
  last forward-only gap is closed; the R8 corpus starts tonight.
- Backfillable now: klines, Chainlink rounds, resolved outcomes, Binance aggTrade flow.
  Forward-only: books, spot ticks, Polymarket prints.

## Phase 1 — Replay harness (the judge) · next week

- ~~Extract the decision core (`fair_p_up` + firing policy) behind a snapshot interface: a pure
  function of (recorded feed state, recorded book, params, t). No I/O in the core.~~
  **DONE 2026-08-23 (1bae00a)**: `decide(view, model, now) -> {actions, tape, finished}` +
  pure `eval_model(params, feed, now)`; live tick is a thin adapter; replay drives the same
  function. Replay-only `Tunables` make the pre-brake policy expressible for reproducing
  recorded nights (live arms cannot reach them).
- **`pmengine replay` SHIPPED 2026-08-23**: evals mode (drives decide() from the recorded
  eval tape) + full mode (rebuilds the model from book-tape + cached klines, strict
  no-look-ahead: only closed minutes bank; forming minute = (open+spot)/2 like the live
  feed). Conservative instant taker fills; `--outcomes` takes wallet truth (ALWAYS pass it —
  evals mode's fallback settles on the tape's last p_up, i.e. the model grading itself).
- **Acceptance PASSED on the −$370 window `btc-updown-15m-1787449500`** (01:45Z; wallet:
  $370.14 bought, $0 redeemed): pre-brake params reproduce the catastrophe — first sim fire
  at the identical tick as reality, $495/$500 committed, −$504 vs real −$370 (sim fills every
  clip; reality got partial fills before the guard cut in). Same night under today's braked
  policy: 3 fires, $59 committed, −$60 — the brakes cut the loss ~88% on recorded reality.
- Pinning lesson (2026-08-23): `btc-updown-15m-1787446800` looked like the loss on tape
  density but wallet shows $0 bought — 32 "fires" that never filled, re-emitted every 12s as
  inflight TTL expired. Two morals: (a) pick acceptance windows by WALLET, never by tape
  (the tape records intent, the wallet records reality); (b) replay's instant-fill assumption
  over-states exposure on windows where the book never filled us — a known, conservative gap.
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
R3 → R5/R6/R1 → R7/R8/R9.

- **R1 Oracle basis per symbol — ALIGNED MEASUREMENT DONE 2026-08-23**
  (`analysis/r1_aligned_basis.py`, 48h corpus, step-interpolated Chainlink TWAP vs Binance
  minute marks, plus the settlement-shaped 30s/60s variant). Settlement-shaped p95 |basis|:
  BTC 8.0/7.6bp (5m/15m), ETH 10.0/8.0, SOL 11.1/8.7, XRP 22.5/16.8, DOGE 17.9/13.2.
  The first point-in-time method was right for BTC and over-stated alts 2-3x (SOL 26.8→11.5,
  XRP 49→18.6, DOGE 37→15 per-minute p95). Signed bias ≈ −2.5bp on all symbols (oracle lags
  drift). BTC/ETH distribution HALVED day1→day2 — non-stationary; re-run after 1-2 weeks
  before trusting any p99.
  - Verdicts: **BTC 3bp was too loose** → raised to 6bp 2026-08-23 04:15Z, backed by the
    first replay full-mode A/B over 23 recorded windows with Chainlink-truth settlement:
    guard 3 = 61 fires, 9-3, −$300; guard 6 = 21 fires, 6-0, +$71; guard 8 over-gates
    (1 window). ETH 8 / SOL 10 confirmed roughly right, stand. **XRP not tradeable** via
    Binance proxy (p95 alone would need a ~20-25bp guard that guts the edge; got WORSE
    day2). **DOGE not yet, but the closest alt candidate** — recheck after more corpus.
  - First measurement kept for the record: point-in-time p95 BTC 8.8 / ETH 13.6 / SOL 26.8 /
    XRP 49.2 / DOGE 37.4bp (upper bounds; outliers cluster in flash-move minutes).
  - **Dynamic guard BUILT, ships dark** (041597e + 6242ceb): per-arm Chainlink poller with
    LAG-ALIGNED samples (round.answer vs the Binance mark of the round's own updatedAt
    minute — instantaneous spot comparison would measure trend speed, not basis), raise-only
    p95 floor over the operator's param, oracle-tape corpus, status diagnostics. Activation
    requires PMENGINE_DYNAMIC_GUARD=1 + restart — deliberately NOT live so the R9 night
    attributes cleanly; deploy as its own change window after its oracle corpus warms up.
  - Tooling (aab232b): `pmt crypto basis --aligned` = the weekly re-measurement;
    `pmt crypto outcomes` = wallet-first validated outcomes for every replay A/B (stale
    Chainlink is dropped, never guessed — a stale step-extension mislabeled 9/37 windows
    on 2026-08-23 and briefly justified a guard change with wrong numbers).
  - Corrected guard A/B for the record (validated outcomes): on the calm 2026-08-23 night,
    btc guard 3 went 12-0 +$282 vs guard 6's 6-0 +$71 — the 6bp tightening cost winners and
    saved nothing THAT night. Kept at 6 on the 48h non-stationary measurement; revisit
    weekly with the corpus.
  - **Fill-chasing** (audit: 32% of intended taker notional never crossed; re-quotes chase
    the book upward): `pay_up_max` shipped dark (6242ceb, CLI `--pay-up`) — a clip's
    marketable limit may chase by surplus edge over the floor only. Enable per-arm after
    one clean night of print-flow data to measure against.
- **R2 Calibration gate + fractional Kelly** — clip size from quarter-Kelly on post-fee,
  post-haircut edge; hard per-window %-of-bankroll cap; no size increases until each p-bucket
  shows calibration over ≥30 decided windows. (Current reality check: stated fair ≥0.95 hits 92%.)
  Variance for sizing/quoting on a binary is Bernoulli: σ² = p(1−p) (issue #3, tfrmma).
- **R3 Odds discipline** — test a max entry price (~0.70) for non-banked entries on the corpus.
  External research says high-price "sure things" are poor risk/reward after fees; our own tape
  can confirm or refute before we adopt it.
- **R4 5m vs 15m allocation** — the open contradiction: external research (Stanford/SMU, ~16k
  5m BTC contracts) finds near-settlement manipulation concentrated in 5m and prefers 15m; our
  wallet says 5m is 30-1 and 15m 5-1 including the −$370. Hypothesis: our 5m edge is real
  (flip-proof + manip cushion already defend it) and the 15m loss was a sizing/brake failure,
  now fixed — but the corpus decides: look for the reversing near-settlement flow spike
  signature in OUR recorded books before moving capital either way. Regime note: settlement
  moved to Chainlink TWAP ~2026-08-07 (30s on 5m, 60s on 15m/4h) — the study's pure
  last-print snapshot attack is largely closed; a push now has to be sustained across the
  averaging window. R4 must therefore split pre/post-change nights, and residual near-close
  risk is mostly path vol of the unfinished average, not single-print manipulation.
- **R5 Session + fast-ρ regimes** — add a 5–15m ρ estimate beside the 60m one; in negative-ρ
  regimes allow banked-decided entries only; attribute P&L by session (Asia chop vs London/NY)
  and gate accordingly.
- **R6 Fat tails — MEASURED 2026-08-23, PREMISE REFUTED** (`analysis/r6_tail_flip_study.py`,
  21 days × 48k simulated windows): conditional flip rate at safety ≥ 1.0 is **~0%** in every
  symbol × duration × rem bucket — far BETTER than the Gaussian promise (15.9% at 1.0), and
  the model's p_up is calibrated-to-conservative on its own settlement math (claimed ≥0.95
  realizes 96.8-99.2%). The wallet's ~92% at stated ≥0.95 is explained by BASIS events, not
  path reversals: replaying the sol15 −$142 window shows the Binance math was RIGHT (down)
  and Chainlink settled UP on a ~36bp settlement-boundary divergence — >p99 of SOL's measured
  basis. Verdicts: cushion widening NOT supported, k=1.0/J=0 stands; `p_cap` (built dark,
  76d6bfc) stays dark — capping a calibrated model fixes nothing; the residual tail is R1's
  domain (settlement basis), defended by measured static guards + the dark dynamic guard,
  and by not trading formats whose basis tail exceeds their guard (sol15's epitaph).
- **R7 Correlation-aware fleet cap** — cap total un-decided committed notional across arms;
  at ρ 0.7 the arms lose together.
- **R8 Near-even late-flow guard** — when the book is still ~50/50 in the final 30–60s and
  late Binance flow is abnormal, cut or zero the size multiplier: that combination is the
  manipulation fingerprint the 5m literature documents. Formalization (issue #3): VPIN
  volume-bucket imbalance (Easley/López de Prado/O'Hara) on Binance aggTrades (backfillable)
  and Polymarket prints (needs the Phase 0 flow recorder); Bartlett & O'Hara (2026, Kalshi)
  show one-sided flow predicts maker losses — the same signal should gate our taker clips.
  Cheap v1 the corpus can already test: consecutive-same-side-flow counter.
- **R9 Entry gate: banked evidence, not clock % — DEPLOYED 2026-08-23 05:00Z** (7b1948d,
  `--theta 0.3 --min-elapsed 0` fleet-wide): the 50% clock gate is RETIRED. First clip
  requires side-signed |banked|/cushion ≥ θ; banked-lag makes entry self-gate ~2-3min in.
  Replay validation: pure R9 went 11-0 on btc+eth over the validated night corpus (both
  post-brake losses had entered at safety < 0.25; median winner 0.57). Deployed alongside:
  window brake LATCH (first distrust/avg_down trip closes speculative entry for the window —
  the audit showed brakes flagged 4/4 losses but blocked only 10-51% of exposure). θ=0.3 is
  the opening value, re-swept as the full-window instrumentation corpus grows. Original spec
  (kept for the record, 2026-08-23 deep-research pass):
  - `safety = |banked_margin_bp| / max(cushion_bp, ε)` — a dimensionless path-moneyness score
    from quantities `fair_p_up` already computes (the σ√(T/3) cushion is the standard
    Asian-average residual-risk term; the live 45m σ floor feeds it, so safety adapts to
    regime without retuning). Side agreement: sign(banked) must match the side considered.
  - First clip requires `safety ≥ θ` (θ=1 ≡ today's banked_decided); everything else
    (edge, basis, ρ, brakes, budget unlock, quiesce) unchanged. Sweep θ over
    {0.6, 0.75, 1.0, 1.25, 1.5} against a `min_elapsed` sweep {0, 0.2, 0.3, 0.4, 0.5} as the
    baseline to beat — same corpus, conservative fills.
  - Couples with R3, not substitutes: low-safety entries get the ~0.70 max-price cap;
    high-safety/banked entries may buy near 1.0 (residual path risk is small there).
  - Sub-A/B: duration-aware banked lag — settlement moved to Chainlink TWAP ~2026-08-07
    (30s on 5m, 60s on 15m/4h); our banked cut is a flat now−30s. Align per duration so
    "banked" never includes mass still inside the settlement TWAP.
  - **Instrumented 2026-08-23**: eval tape now records margin_bp / banked_bp / cushion_bp
    every tick (guard-gated ticks carry them in the gate reason) — the θ sweep has corpus
    data from tonight forward.
- **R10 AS-lite inventory tilt** (added 2026-08-23, issue #3) — pre-maker reservation-price
  skew as a pure function: `r = p_fair − q·γ·p_fair·(1−p_fair)·τ`, with p_fair = OUR model
  (banked-adjusted p_up), never book mid. Used first as a size/side tilt on existing taker
  clips — long UP makes adding UP less attractive and exiting easier, the continuous
  generalization of the no-averaging-down brake. Evidence for priority: the Paradigm
  challenge writeup found inventory skew alone was make-or-break (−$7 edge without it).
  Unit test: skew direction (long UP → lower UP reservation). γ swept on the replay
  harness like every other knob; no live sizing input before an A/B win. Becomes the
  quoting core of Phase 3.1 when the maker path lands.

## Phase 3 — Strategy expansion (each gated by Phase 1)

1. **Maker mode** — rest quotes at fair-minus-edge; the fill-economics build (existing top
   backlog item). Prerequisite for everything two-sided. Design library (issue #3): keep
   the Avellaneda–Stoikov shell (reservation price + optimal spread + R10 inventory skew,
   Bernoulli variance σ² = p(1−p)), but **replace their fair value with ours** — simple BS
   digital FV is weaker than banked TWAP + live σ on 5/15m crypto. Reading order: polybot
   `avellaneda_stoikov.py`/`pricing.py` → zostaff hft-pm docs §4–5 (incl. logit-space quotes
   near 0/1) → blockchainhansi 15m BTC strategy engine (skew + pair merge) → Feil & Nendel
   2026 (HJB with settlement risk — interacts with R9's cushion as τ→0). Toxicity widens
   spread / pauses quotes (R8's VPIN). pascal-labs forensics = the behavioral blueprint of
   a profitable MM on exactly our market class.
2. **Tail-snipe** — final 30–60s, banked-decided/flip-proof only, a formalization of the mode
   we already trust. Extension candidate (2026-08-23): the 4h up/down series' final stretch is
   the same trade — arm 4h windows with high min_elapsed and harvest banked-decided books with
   the identical code path. Needs a look at 4h near-expiry liquidity first; adds zero
   diversification (same ρ), so it waits for R7's fleet cap like everything else here.
3. **Complete-set scanner** — Up + Down < $1.00 after fees; structural, low-directional.
4. **Two-sided inventory rotator with directional tilt** — the pattern profitable wallets
   actually run at scale; biggest build, needs maker infra first.

## Operating rules while the roadmap runs

Posture: keep the braked directional fleet live at current size, instrument everything,
force the harness to reproduce reality, then promote only what survives R4 and the
calibration gate. Operator-approved bump 2026-08-23 (post-brakes/σ-floor/R9-instrumentation):
btc5m 400/50, btc15m 350/25, eth5m+15m 350/50, sol5m unchanged 150/25 — weighted toward the
wallet's proven edge (btc5m 30-1), clips deliberately NOT raised (clip size is the brake
system's risk-rate lever), SOL flat until R1's aligned basis verdict. Sixth arm added
2026-08-23 04:00Z: sol-updown-15m at 150/25/10 (same symbol we already price, guard already
calibrated, and 15m accumulates more banked evidence per window than its 5m sibling). XRP/DOGE
stay off pending the R1 aligned (TWAP-vs-TWAP) measurement, in flight tonight.

- No further size increases without an R2 calibration pass (the 2026-08-23 bump above is
  the operator's call and the new baseline).
- Never loosen the three brakes (15¢ distrust, 2¢ no-averaging-down, final-120s unlock)
  or the basis guards without a replay A/B win — they encode the paid-for lessons.
- 5m arms stay at current size (experimental class) until R4 settles; XRP stays off until R1.
- Options-implied gaps and long-horizon mean-reversion findings are research inputs, never
  direct sizing inputs for the 5/15m fleet.
- No new strategy ships before the replay reproduces real nights.
- Every engine change: replay A/B win → one small-size live night → full size.
- Tape + wallet scoreboard are append-only ground truth; `~/.pmt/` is the durable home
  (scratchpads die nightly).
