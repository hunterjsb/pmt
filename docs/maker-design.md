# Maker mode — design brief (Phase 3.1 prelim)

Status: design only. Nothing here fires an order. Every phase in §6 is gated the way
ROADMAP.md already gates everything — replay A/B win → one small-size live night → full
size (ROADMAP.md:231) — and step 0 is the only piece that touches engine code at all
before a strategy exists to use it.

Sources fetched for this brief, cited inline by short tag:
- **[polybot]** github.com/YISOWAK/polybot-market-maker — `strategy/avellaneda_stoikov.py`,
  `strategy/pricing.py`, `strategy/fill_model.py`, `strategy/risk_manager.py`,
  `strategy/microstructure.py`
- **[zostaff §N]** github.com/zostaff/hft-pm `docs/hft_prediction_markets_EN.md`, by section
  number (§4 full AS derivation, §5 logit-space, §7.5/7.6 CLOB V2 + fees, §8 signals, §9
  complete algorithm, §12 Kelly)
- **[hansi]** github.com/blockchainhansi/market_maker_polymarket — `src/strategy_engine.py`,
  `src/config.py`, README
- **[FN26]** Feil & Nendel, *Optimal Market Making in Prediction Markets*, arXiv:2607.17991
- **[pm-docs]** docs.polymarket.com — `trading/fees.md`, `trading/place-orders.md`,
  `api-reference/trading-rate-limits.md`
- **[pmengine:file:lines]** this repo, `pmengine/src/...`
- **[ROADMAP:line]** this repo, `ROADMAP.md`

pascal-labs forensics (referenced in ROADMAP.md:200 as "the behavioral blueprint of a
profitable MM on exactly our market class") was **not** fetched for this pass — flagged as
an open follow-up before step 3 of the build plan below.

---

## 1. Quoting core

### 1.1 Fair value: ours, not theirs

Every piece of prior art fetched prices its own fair value differently — polybot uses a
Black-Scholes-binary FV fed by a Deribit IV surface (`calculate_black_scholes_binary`,
`get_trading_signal` **[polybot]** pricing.py:82-144), hansi uses no model FV at all (pure
structural top-of-book + pair-cost bidding, see §1.4), zostaff's worked example uses the
book microprice (§9 **[zostaff §9]**). None of them have what we have: a banked-TWAP model
already computing `p_up` from Chainlink-anchored settlement math, live per-symbol σ floors,
and a basis guard measured against real oracle rounds. ROADMAP.md is explicit that this is
the one thing to change: "**replace their fair value with ours** — simple BS digital FV is
weaker than banked TWAP + live σ on 5/15m crypto" (ROADMAP:196).

Concretely: maker mode's `p_fair` is `eval_model(p, feed, now, guard_bp).p_up`
**[pmengine:strategies/updown.rs:612]** — the exact same function updown already calls,
unmodified. Zero duplication of the fair-value math; this is the literal implementation of
"reuse of ArmView/decide pattern" from the task brief.

### 1.2 Reservation price — R10, cross-validated three ways

ROADMAP.md already specs the formula (R10, added 2026-08-23, ROADMAP:180-188):

```
r = p_fair − q · γ · p_fair · (1 − p_fair) · τ
```

This is the classical Avellaneda-Stoikov reservation price `r(t,q) = S_t − qγσ²(T−t)`
**[zostaff §4, boxed formula, line 168]**, with `σ² → p(1−p)` (Bernoulli variance — a
binary settles at exactly 0 or 1, so its instantaneous variance *is* `p(1-p)`, not some
externally-calibrated vol). Three independent sources converge on this exact substitution,
which is worth stating plainly since it's the one piece of "novel" math in this design:

1. **ROADMAP's own choice** (R9's `cushion_bp` already scales a σ term the same way the AS
   half-spread does — R10 was written to compose with it, not against it).
2. **zostaff §5.1**: transforming AS to logit space and back shows the price-space
   half-spread is `δ_price ≈ p(1−p) · δ_logit` — Bernoulli variance is *exactly* the right
   curvature term near the 0/1 boundary, which is where crypto 15m windows spend their last
   minute (**[zostaff §5.1, lines 314-326]**).
3. **Feil & Nendel's terminal penalty**: solving the full HJB with settlement risk (not an
   asymptotic AS expansion) gives a terminal penalty `Φ(p,q) = −γ_T q² p(1−p)` — the same
   `p(1-p)` term falls out of a completely different derivation path (**[FN26]**, see §1.5).

`τ` should NOT be raw wall-clock time-to-expiry for `twap`-kind arms. Use the same
weighting `eval_model` already computes: `rem / window` (the unbanked fraction of the
averaging period, **[pmengine:strategies/updown.rs:678-679]**) — this makes the skew decay
*exactly* in step with R9's `cushion_bp`, so as a window becomes banked-decided, both the
entry gate (R9) and the quoting skew (R10) relax in the same direction for the same reason,
rather than two independently-tuned clocks drifting apart.

`γ` has no live value yet — ROADMAP is explicit: "γ swept on the replay harness like every
other knob; no live sizing input before an A/B win" (ROADMAP:186-188). §6 step 1 is where
that sweep starts.

### 1.3 Spread — why NOT the full closed form, initially

The full AS half-spread is `δ* = γσ²τ + (2/γ)ln(1+γ/κ)` **[zostaff §4, line 170]**, where
`κ` is the exponential decay rate of fill intensity with distance from mid. We have no `κ`
— calibrating it needs resting-order fill data we don't have yet (see §5's replay gap).
polybot hit the identical wall and made the identical call: its `AvellanedaStoikov` class
accepts `gamma`/`min_spread_ticks` params "legacy... acceptées mais ignorées" and reduces to
a fixed half-spread widened only by inventory/toxicity pressure, with the comment "Le
calibrage Poisson est exclu (données insuffisantes sur marchés 15-min)" — Poisson
calibration excluded, insufficient data on 15-min markets (**[polybot]**
avellaneda_stoikov.py:1-12, 41-51). That's precisely our night-1 constraint too.

Recommended day-1 reduction (port of **[polybot]** avellaneda_stoikov.py:74-86, with our
fair value swapped in per §1.1):

```
center = p_fair − inv_ratio · γ · p_fair·(1−p_fair)·τ     # §1.2, inv_ratio = clamp(q/q_max, -1, 1)
half   = base_half · vpin_mult(§1.6) · gross_mult          # gross_mult: +0 below 70% of gross cap, ramps to +3¢ at 100%
```

`base_half` starts from the same bp-scale we already trust for basis guards (R1's measured
guards: BTC 6bp, ETH 8bp, SOL 10bp, ROADMAP:92-93) rather than an untuned constant — a
maker's quote shouldn't be tighter than the basis noise we already know the settlement math
can't resolve. Graduate to the full `γσ²τ + (2/γ)ln(1+γ/κ)` form once step 1-2 of the build
plan (§6) produce enough resting-fill data to fit `κ`.

### 1.4 Arbitrage guard and no-short constraint

Two hard constraints, both mechanical, both already solved in **[polybot]**
avellaneda_stoikov.py:105-139 — port directly:

- **Bid-sum guard**: `yes_bid + no_bid` must never exceed `1.00`, or an arbitrageur drains
  us; if it would, shrink both bids by `ceil(excess/2/tick)*tick` (lines 107-111).
- **Ask-sum guard**: `yes_ask + no_ask` must never fall below `1.00`, symmetric logic
  (lines 112-117).
- **No-short guard**: Polymarket does not allow shorting a binary outcome token — an ask
  (sell) is only postable up to shares actually held (`min(desired_size, yes_shares)`,
  lines 133-134). This is an exchange constraint, not a risk choice we could relax.

### 1.5 Near-settlement behavior — widen and unwind, don't just pull

updown's existing quiesce logic pulls all standing orders inside `quiesce_secs` of
expiry, with one carve-out for flip-proof (**[pmengine:strategies/updown.rs:855-867]**).
Naively porting that "pull everything" rule to maker mode would be wrong per **[FN26]**'s
actual numerical result: solving the full HJB near `τ→0` shows spreads should **widen**
(not vanish) for prices near `p=0.5` where settlement risk peaks, while inventory skew
intensifies sharply to unwind aggressively — "positions must be aggressively unwound
through asymmetric quoting... strongly asymmetric quotes—moving bids down and offers
up—to encourage sales." Their simulation cut terminal inventory from ~49 to ~15 units this
way while keeping 99% of expected profit (**[FN26]**, §1 settlement-risk summary above).

Recommended behavior, deliberately mirroring updown's own asymmetry rather than inventing a
new rule:

- **Non-decided inventory** (banked_decided == false): widen spread hard and skew
  aggressively toward flat as `τ→0` (§1.2's `τ` term already does most of this
  automatically since skew scales with `1/τ`-like urgency as the unbanked fraction
  shrinks — verify on replay rather than assume).
- **Banked-decided / flip-proof inventory** (R9's own definitions, ROADMAP:155-179): do
  **not** flatten. A flip-proof position is a near-certain win; crossing the spread to exit
  it destroys edge for no risk reduction. This is exactly updown's existing flip-proof
  carve-out (**[pmengine:strategies/updown.rs:862-863]**) applied to a maker's own book
  instead of a taker clip — same asymmetry, same code shape, just gating quote-withdrawal
  instead of quote-*placement*.

### 1.6 Toxicity gate (VPIN) — shared signal with R8

ROADMAP's own R8 ("Near-even late-flow guard... Formalization: VPIN volume-bucket
imbalance," ROADMAP:148-154) and a maker's own quote-widening need the *same* signal.
**[polybot]** microstructure.py:118-210 ships a ready `VPINCalculator` — sliding window of
`(size, side)` trades, `vpin = |buy_vol − sell_vol| / total_vol`, and a
`get_spread_penalty()` that linearly interpolates a spread multiplier from 1.0 (at or below
threshold) to `penalty_factor` (fully toxic) — directly portable, and it already consumes
exactly the shape of data book-tape now records (`up_tbuy/up_tsell/dn_tbuy/dn_tsell`,
shipped 2026-08-23, ROADMAP:33). **[zostaff §8.3]** gives the same VPIN definition plus a
`breakeven_alpha` gate: widen or withdraw once the estimated informed-trader fraction α
exceeds `δ*/((V_H−V_L)π(1−π) + δ*)` (**[zostaff §4.8, lines 267-273]**) — a sharper
threshold than a flat VPIN cutoff since it accounts for the current spread and price level.
Recommend building **one** `VPINCalculator` module shared between R8 (taker gate on
updown) and maker mode's spread multiplier — same signal, two consumers, zero duplicated
math.

---

## 2. Polymarket CLOB mechanics for makers

Verified directly against **[pm-docs]** (not third-party summaries) per the task's
instruction:

- **Maker fee = 0, confirmed literally.** `trading/fees.md`: "Makers are never charged
  fees. Only takers pay fees." Crypto category: taker `feeRate = 0.07`, fee formula
  `fee = C × feeRate × p × (1−p)` (C = shares, p = price) — this is the *same shape*
  `updown.rs` already uses for its taker-fee estimate, `p.fee_rate * ask.min(1.0 - ask)`
  (**[pmengine:strategies/updown.rs:1000]**), confirming the engine's existing fee model is
  already the right formula for the taker side.
- **Maker rebate exists but its accrual mechanics are underspecified.** Crypto category
  rebate share is 20%, funded by taker fees and "redistributes fees daily to market makers"
  (**[pm-docs]** fees.md) — cross-checked against **[zostaff §7.6]**'s independent number
  (crypto 15-min: 1.80% peak taker rate, 20% maker rebate share) — same 20% figure, minor
  scale difference in the peak-rate framing (1.80% vs. docs' implied 1.75% at p=0.5 from
  `0.07 × 0.25`), close enough to be the same underlying schedule. **Do not bake the rebate
  into live edge/sizing math** until its per-fill vs. pooled-daily accrual is confirmed
  against an actual maker fill + wallet credit — same "wallet grades the model, never the
  reverse" discipline ROADMAP already applies everywhere else (ROADMAP:9).
- **`postOnly` is a real, documented field** — `postOnly: true` alongside a `GTC` or `GTD`
  `orderType`, on both the TS and Python SDKs and the raw API body (**[pm-docs]**
  place-orders.md). Behavior: "add liquidity only: if it would match immediately against
  the book, it is **rejected** instead of taking" — rejected, not repriced. This matters
  operationally: a reservation price that has drifted past the current best bid/ask on the
  wrong side (stale feed, fast-moving book) will bounce the whole order rather than
  silently cross it — quotes must be clamped inside the current book (mirroring polybot's
  `_round_tick` + arbitrage-guard clamping, **[polybot]** avellaneda_stoikov.py:151-156)
  *before* submission, or the engine will spend its rate-limit budget on rejections.
- **GTD has a ~2-minute minimum effective lifetime**, not useful for second-scale
  requoting: "GTD orders expire one minute before their stated expiration as a security
  threshold. To set an effective lifetime of N seconds, use `now + 60 + N`" (**[pm-docs]**
  place-orders.md). **Use GTC + explicit cancel/replace each requote**, the same pattern
  updown already uses for its own orders (`Action::Cancel` immediately followed by
  `Action::Buy` in the same `decide()` pass, **[pmengine:strategies/updown.rs:908-913]**).
- FAK/FOK ("market" order types) don't support `post_only` — irrelevant to maker mode,
  that's updown's taker path.

**Required engine change — `Urgency::Low` was a complete no-op. SHIPPED (step 0).**
`OrderManager::place_order` took `_urgency: Urgency` and dropped it entirely, so **every
order pmengine had ever placed, live, was a plain crossing-allowed GTC order regardless of
which `Urgency` a strategy asked for** — updown always asks for `Urgency::High` anyway, so
it never mattered until now.

As of step 0 `Urgency::Low` maps through `order.rs::wire_shape` to the SDK builder's
`.post_only(true)`. Verified against the vendored **0.7.0** source (not the 0.6.0-canary
this brief was drafted from): `OrderBuilder::post_only` at `src/clob/order_builder.rs:93-97`,
`build()` defaulting to `OrderType::GTC` + `post_only: Some(false)` at lines 325-326 and
refusing postOnly on anything but GTC/GTD at 334, and `SignedOrder`'s hand-written
`Serialize` emitting `postOnly` beside `order`/`orderType`/`owner` at
`src/clob/types/mod.rs:867-869`. pmengine POSTs that `SignedOrder` through its own L2 path
(`client.rs::l2_post`), so the flag reaches the wire without going through the SDK's own
`post_order`. Note the field was **already on the wire as `postOnly: false`** for every
order ever placed — step 0 only flips its value, it does not add a field.

One consequence the brief's own §1.3/§2 pricing must respect: rejection is not repricing, so
`order.rs` rounds a post-only price AWAY from the book (`ToZero` for a bid, `AwayFromZero`
for an ask) instead of to nearest. Half-up on a coarse tick would lift a 0.985 bid to 0.99 —
above the `max_price` the strategy clamped it under, and straight into a rejection.

---

## 3. Cancel/replace cadence + rate limits

**[pm-docs]** `api-reference/trading-rate-limits.md`, Standard tier (the tier before any
maker volume accrues):

| Bucket | Refill rate | Burst |
|---|---|---|
| Order placement (`POST /order`) | 40 tokens/s | 60 |
| Order cancellation (`DELETE /order`) | 80 tokens/s | 120 |

Tiers scale up (Copper through Elite, up to 600/1200 tokens/s) keyed on "cumulative maker
wallet volume over the preceding 30 days," reassessed every 3 hours — maker mode's own
volume raises its own ceiling over time, but budget for Standard-tier limits on day 1.

**Cadence must be strategy-level, not engine-tick-level.** `Strategy::tick_interval_ms()`
defaults to 1000ms but is overridable per strategy (**[pmengine:strategy.rs:219-228]**);
updown overrides it down to 50ms for taker-clip latency
(**[pmengine:strategies/updown.rs:1250-1252]**). A maker requoting both sides every tick at
50ms with a cancel-then-replace burst (2-4 order events per requote) would exhaust
Standard-tier placement tokens in under two seconds across even a single arm. The existing
(currently unregistered/dead) example market makers already made the right call here —
`market_maker.rs`/`dynamic_market_maker.rs` both declare `tick_interval_ms() -> 5000`
(**[pmengine:strategies/market_maker.rs:47-49]**) — and **[hansi]**'s
`REFRESH_INTERVAL` config defaults similarly (1-5s range, README config table). Recommend
starting maker mode at **3-5s per arm**, well inside Standard-tier headroom even across all
six fleet arms simultaneously quoting.

**Dead-band the requote, don't reprice on every tick.** **[hansi]**'s `_update_bid` skips
placing a new order if `abs(price - last_price) < 0.005` (**[hansi]**
strategy_engine.py:457) — avoids cancel-thrashing on book noise that hasn't actually moved
the reservation price. Port the same idea: skip cancel+replace if the new desired quote is
within roughly half a tick of the standing one.

Batch endpoints (`POST /orders`, `DELETE /orders`) exist and cost tokens per-order (not a
flat batch cost) — no token-budget benefit, but they do save HTTP round-trips. Note as a
later optimization once arm count grows; not needed at the cadence and arm count in scope
for §6's early phases.

---

## 4. Inventory bounds + pair-merge exit

**Two independent throttles, recommend combining both** since they attack different
failure modes — spread protects the *next* fill's price, size protects the *shape* of it:

- **Spread widening under gross pressure**: **[polybot]** avellaneda_stoikov.py:80-86 —
  once gross inventory (yes+no shares) crosses 70% of `max_gross`, add
  `(gross_ratio − 0.7) × 0.10` to the half-spread, capping at +3¢ at 100%.
- **Exponential size decay**: **[hansi]** config.py — `size = base_size × exp(−η·|ΔQ|)`,
  citing Fushimi et al. (2018)'s dynamic order-sizing result (**[hansi]** README "Strategy"
  section) — shrinks clip size continuously as net inventory grows, rather than a hard cliff
  at a cap.
- **Boundary-aware exposure cap**: `|q| ≤ M·√(p(1−p))` (**[zostaff §5.1 & §12]**,
  cross-cited from **[FN26]**'s own boundary-risk framing) ties maximum exposure to
  distance-from-certainty — it shrinks automatically as a window becomes banked-decided
  (`p_fair → 0` or `1`), which composes cleanly with R9: the moment R9's safety gate would
  call a window `banked_decided`, this cap is already choking off new maker inventory on
  its own, right where updown's flip-proof taker carve-out (§1.5) should take over instead
  of the maker continuing to add. **Recommend maker mode stop quoting new inventory (not
  necessarily withdraw existing) once `flip_proof` is true** — same handoff logic as §1.5,
  now for entry instead of exit.

### Pair-merge exit — this is Phase 3.3, done for free as a side effect

**[hansi]**'s entire strategy *is* the pair-merge trade: bid on both YES and NO, and once
`YES_cost + NO_cost < $1.00`, the pair is a locked, zero-price-risk profit redeemable at
$1.00 regardless of outcome (README: "buy 10 YES at $0.48 and 10 NO at $0.48... Total cost:
$9.60... guaranteed payout $10.00... profit $0.40"). This is ROADMAP's own Phase 3.3
("Complete-set scanner — Up + Down < $1.00 after fees; structural, low-directional,"
ROADMAP:207) — **maker mode gets it for free** as a side effect of quoting both sides of
the same window, not as a separate strategy, *if* the bookkeeping tracks it.

Recommended implementation, porting **[hansi]** strategy_engine.py:391-441 directly:

- Track `paired_quantity = min(shares_up, shares_dn)` and cumulative `locked_profit` the
  way **[hansi]**'s `Inventory.record_fill` does.
- Guardrail: **never let one side's bid price rise above what would still lock a merge
  profit against the other side's current average cost** — `max_profitable_bid = 1.00 −
  opposite_avg_cost` (**[hansi]** strategy_engine.py:426-431). This is a pure structural
  cap, independent of the AS reservation-price math in §1 — apply it as a final clamp on
  top of whatever §1.3 computes.
- **Redemption, not resale**: once paired, the position doesn't need to be sold — redeem
  the complete set for exactly $1.00/pair at resolution, no counterparty, no further price
  risk. This is why **[hansi]**'s bot has no "sell" path on the happy path
  (`sell_positions.py` is a manual/shutdown-only tool per the repo layout) — mirror that:
  a stuck one-sided position should first try to buy the cheap complement to lock a merge
  (capital-efficient, zero remaining risk) before falling back to updown's existing panic
  exit (`EXIT_FAIR`/`EXIT_MAX_DISCOUNT`, **[pmengine:strategies/updown.rs:34-39,716-758]**),
  which donates edge into a bid.
- **Bookkeeping split**: keep `q_paired` (capital-recycling-speed bound — USDC tied up
  until redemption, not directional risk) separate from `q_net` (the R10 skew term's actual
  input) in whatever state struct tracks inventory. Only `q_net` should feed §1.2's skew;
  the paired stack is risk-free and skewing against it would be a bug, not caution.

---

## 5. Integration plan into pmengine

### 5.1 New strategy, not an updown mode

Reasons:

- **Different state shape.** updown tracks `inflight`/`last_clip`/`last_clip_ask` per side
  for a one-shot taker clip (**[pmengine:strategies/updown.rs:307-313]**); a maker needs
  standing-order ids per side, queue-ahead estimates (§5.3), and split paired/net inventory
  (§4) instead. Threading both through the same 2200-line match arms in `decide()` would
  make an already-large file harder to reason about for no shared benefit.
- **Blast-radius discipline.** ROADMAP: "Never loosen the three brakes... without a replay
  A/B win" (ROADMAP:225) — a shared file raises the surface area for an accidental
  regression of the live fleet's tuned brakes while iterating on unrelated maker code.
- **They belong as siblings, not a merge.** `StrategyRuntime` already runs N independent
  strategies off one shared `StrategyContext` per tick
  (**[pmengine:strategy.rs:271-395]**) — no engine reason exists to fuse them. The natural
  end state (ROADMAP Phase 3 intro, "Toxicity widens spread / pauses quotes... pascal-labs
  forensics = the behavioral blueprint of a profitable MM," ROADMAP:199-201) has maker mode
  resting through most of a window while updown's flip-proof taker path still snipes the
  final seconds on the *same* tokens — two strategies, one market, exactly the shape
  `StrategyRuntime` was built for.

### 5.2 What gets reused verbatim (the "imitate updown.rs's decide() core" instruction)

- The **pure-core shape**: `ArmParams` / `ArmView` / `Action` / `DecideOut` /
  `fn decide(&mut self, view: &ArmView, model: Result<ModelEval, String>, now: f64) ->
  DecideOut` (**[pmengine:strategies/updown.rs:245-273,824]**) — a `MakerArmState::decide()`
  with the identical signature, so the **same replay harness seam** drives both strategies:
  "a pure function of (recorded feed state, recorded book, params, t). No I/O in the core"
  is Phase 1's own acceptance requirement (ROADMAP:41-46), not just a style preference.
- **`eval_model()` unchanged, called directly** — no fork, no copy. This is the concrete
  form of §1.1's "replace their fair value with ours."
- **`arm_view()`'s snapshot pattern** (**[pmengine:strategies/updown.rs:1122-1147]**) —
  extend with our own resting-order state (ids, prices, queue-ahead-at-placement per side)
  so `decide()` can diff current-vs-desired quote with zero engine I/O inside the pure core.
- **`to_signal()`'s Action→Signal mapping** (**[pmengine:strategies/updown.rs:1151-1169]**)
  — same shape, but maker's `Buy`/`Sell` need `urgency: Urgency::Low` (§2's engine fix)
  routed through.
- **The `on_command` arm/disarm/status control-plane pattern** and the roll/gamma-fetch
  machinery (`next_window`, `fetch_gamma_tokens`, `RollTask`,
  **[pmengine:strategies/updown.rs:351-364,1320-1382,1184-1233]**) — an operator arms a
  maker the same way updown is armed today, reusing window discovery rather than
  reimplementing it.

### 5.3 The replay gap — what "simulate maker fills honestly" actually requires

This is the hard part, and it's a real gap, not a formality. `replay.rs`'s `FillSim` /
`apply_fills` (**[pmengine:replay.rs:231-284]**) treats every `Action::Buy`/`Sell` as an
**instant fill at the quoted price**, full size (capped only by room/ask_size) — that's
honest for updown, because updown's actions *are* marketable taker orders, and Phase 1's
own acceptance test already validated the conservative-taker assumption against a real
recorded night (ROADMAP:52-61). It would be **badly dishonest for a resting post-only
quote**: a maker `Buy` action means "this order now rests at this price," not "this
notional is filled." Replaying it as an instant fill would manufacture liquidity that was
never actually crossed — exactly the class of lie Phase 1 exists to catch (ROADMAP:3, "no
change touches full-size capital until a replay of recorded reality says it's better").

**What the corpus has today**: `book-tape.jsonl` samples top-of-book bid/ask price *and
size* per side every ~1-5s, plus signed print flow between samples (`up_tn/up_tbuy/
up_tsell/dn_tn/dn_tbuy/dn_tsell`, shipped 2026-08-23, ROADMAP:33-35) — top-of-book only, no
L2 depth beyond level 1.

**What's NOT available**: true per-order queue position. **[zostaff §8.6]**'s
`L2OrderBook` class (doc lines 887-1008) tracks exact queue-ahead by diffing L2 book deltas
and attributing shrinkage to cancels-in-front-of-us vs. trades-through-us — it needs L3/full
-depth data pmt does not record and (per Phase 0's own accounting, ROADMAP:36-37) "Polymarket
trade flow" is forward-only anyway, so retrofitting deeper history isn't an option.

**Two honest ways forward, smallest first:**

1. **Conservative queue-ahead bound — buildable today, zero new recording.** At the tick a
   maker action places or refreshes a quote, record the *live* best-bid/ask size at that
   price as "queue ahead of us" — assume we join at the back of the visible level, worst
   case, never front-of-queue. Between then and the next sample, count only the opposing
   print flow that *exceeds* that recorded queue-ahead (`tsell` volume against our bid,
   `tbuy` volume against our ask) as a fill on our resting order, capped at our own order
   size. This directly mirrors **[zostaff]**'s `process_trade`/`queue_position_estimate`
   logic (doc lines 947-1005) but is built entirely from fields `book-tape.jsonl` already
   has, instead of the L2 deltas that method assumes. It is conservative in the *safe*
   direction for a strategy whose entire edge depends on not overstating how easily a thin
   top-of-book fills — the same directional care ROADMAP already flags for updown's
   opposite bias ("replay's instant-fill assumption over-states exposure on windows where
   the book never filled us — a known, conservative gap," ROADMAP:61); here the equivalent
   maker bias should point the other way, toward *under*-filling, not over.
2. **Probabilistic fill model — fallback / cross-check, not primary.** If queue-ahead
   bookkeeping proves too noisy against Polymarket's actual print cadence, fall back to a
   sigmoid fill-probability model keyed on our-size/available-depth ratio + book imbalance
   + spread (**[polybot]** fill_model.py, `fill_probability_v1`/`v2`, lines 11-63) as a
   Monte-Carlo overlay on top of method 1. Useful for confidence-interval sanity checks on
   the A/B sweep, **not** as the primary acceptance mechanism — Phase 1's acceptance test
   wants one reproducible number against one specific recorded night (ROADMAP:52-61), not a
   distribution.

**Resting and taking must be distinguishable in the action stream, not inferred.**
Overloading `Action::Buy` for "place/refresh a resting quote" would corrupt updown's own
replay semantics the moment shared code processed both. Step 0 ships the smaller form of
this brief's `Action::Quote` proposal: `Action::Buy` carries a `post_only: bool`, so
`apply_fills` dispatches on the flag rather than re-deriving intent from `Urgency`, and the
Buy→Signal mapping stays single. `replay.rs` currently **skips** post-only buys entirely —
an honest under-fill (§5.3's safe direction) until the conservative queue-ahead model lands
as step 2. A separate `Quote` variant is still the right shape once maker mode grows a
two-sided quoting strategy of its own (§5.1); one flag on `Buy` is what a single resting bid
needs.

**Acceptance bar mirrors Phase 1's own**: before any maker A/B result is trusted, reproduce
at least one recorded night's fills within tolerance using the conservative queue model —
the identical discipline ROADMAP already applied to updown's taker path (ROADMAP:52-61,
"Same night under today's braked policy... the brakes cut the loss ~88% on recorded
reality"), just pointed at resting fills instead of marketable ones.

---

## 6. Phased build plan — smallest shippable slice first

Every step gated per ROADMAP:231 ("replay A/B win → one small-size live night → full
size"). Nothing below step 4 touches a live order.

**0. Post-only plumbing + the optimistic resting bid. SHIPPED, DARK.**
Two halves, both inert until an operator arms them:

*Plumbing.* `Urgency::Low` → `.post_only(true)` (§2). `Placement` carries the flag back so
the Phase 7 order tape labels a resting quote (`"post_only": true`, additive — absent on a
taker line, so every tape line already on disk still reads correctly).

*The slice.* An `ArmParams.maker_bid` knob (`serde(default)` off; `pmt crypto arm
--maker-bid`) on the ONE miss class a taker cannot reach: the 9.6% of armed time where our
side is bid near 1.00 and **nothing is offered at any price**, median 82% elapsed
(analysis/freq_funnel_report.md). Deliberately not a market maker — early-window maker
measured negative everywhere (0.5¢ half-spread against 3.65¢/5s drift), which is why every
one of §1's quoting mechanics is *absent* here. One post-only bid, priced
`min(fair − edge_req, best_bid + tick, max_price)` floored onto the tick grid, only when
ALL of: no ask at all · `side_safety ≥ theta` on that side · outside the quiesce window ·
not brake-latched · not chop-blocked · `fair ≥ fair_req` · the avg-down brake clears at that
price · cooled · nothing in flight. The bid's notional counts against the arm's budget and
R7's fleet pool from the moment it RESTS (it is un-decided speculative exposure, not
un-committed cash), it re-quotes through the existing delta matcher only when its price
actually moves, and the quiesce sweep, a window close, a gate, and an ask reappearing each
pull it.

*Shadow first.* With the knob OFF the slice still stamps `"maker_candidate": true` (plus
`maker_px` / `maker_size`) on the eval record for every moment it WOULD have quoted, with
`ask` left null so the funnel still charges the moment to `book_quoted`.
`analysis/freq_funnel.py` reports the count and would-be notional. **That measurement is
the gate on arming it**, per ROADMAP:231 — read the counts, then arm ONE symbol.

**1. Pure `decide()` core + quoting math, no live wiring.**
New `src/strategies/maker.rs` with `ArmParams`/`ArmView`/`Action::Quote`/`decide()`
following §5.2's shape exactly, computing §1.2-1.4's reservation price, spread, and
arbitrage/no-short guards from `eval_model()`'s existing output. Deliberately **not**
registered in the strategy registry / `load_strategies` yet — it only runs inside unit
tests. This is where `γ`, `base_half`, and the VPIN threshold (§1.6) get their first sweep,
entirely on paper, matching R9/R1's own sweep methodology (ROADMAP:71-72,169-171).

**2. Replay fill model — conservative queue-ahead.**
Extend `replay.rs` with `Action::Quote` handling per §5.3's method 1, using only fields
`book-tape.jsonl` already records — no new recorder needed. Acceptance: replay a maker
policy over any already-recorded night (book-tape/flow data exists regardless of which
strategy was live) and confirm fill count/timing is directionally sane — fills cluster
where opposing print flow was heavy, never exceed recorded top-of-book size at the sample
that produced them.

**3. Corpus A/B: maker-lite vs. do-nothing, and vs. updown's existing edge on the same
windows.**
Sweep `γ`, `base_half`, VPIN threshold via bootstrap CI, same methodology as every other
roadmap knob (ROADMAP:71-72). Read pascal-labs forensics (flagged open above) before this
step, since ROADMAP names it as the behavioral reference for what a profitable MM looks
like on exactly this market class. Only proceed past this gate on a positive CI.

**4. Live shadow mode.**
Run the strategy live with the client's existing `dry_run` path
(**[pmengine:client.rs:297-308]**, confirmed present) — quotes computed and logged against
real live books, no order ever actually posted. Lets the operator watch a full session's
worth of would-be fills against reality before capital is at risk.

**5. One arm, small size, live, post-only only.**
Single BTC or ETH 15m arm, tiny `size_per_quote`/`max_inventory`, pair-merge bookkeeping
(§4) wired, boundary cap (§4) active, VPIN gate (§1.6) active, **no taker fallback yet** —
maker-only, isolated. First real capital at risk, and only after steps 1-4 each
individually cleared their own gate.

**6. Fleet rollout + sibling coexistence with updown's taker path.**
Maker rests through most of a window; updown's flip-proof taker snipe still fires the final
seconds on the *same* tokens (§1.5, §4's boundary-cap handoff). Deferred until step 5 proves
the pair-merge/inventory-bound machinery is solid — running two strategies on the same
tokens multiplies the ways a bug could double-spend budget, so it waits for the simpler
single-strategy case to be trusted first.
