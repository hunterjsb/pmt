# What to do with the structurally-idle first half of an updown window

Research pass 2026-08-23. Question from the operator: R9's entry gate needs banked TWAP
evidence that accrues ~1 mark/minute, so directional entries land 50-60% through every
window and **the first half is structurally idle**. What non-directional or low-directional
use can that time and capital be put to?

**Short answer: on these books, none. There is no non-directional edge in the early window.**
The complete-set arb is structurally impossible (not rare — impossible). Market making the
early window loses money in every configuration tested, and loses more the more it fills.
Early print flow has no predictive power at all. The one thing in the early window that
looks like money is *directional* and needs a bigger corpus before it can be trusted.

The research did turn up three corpus defects worth more than any of the strategies, one of
which retroactively fills a Phase-0 gap ROADMAP.md declares unfillable. See §7.

---

## 0. Corpus and method

| input | what | span |
|---|---|---|
| `~/.pmt/corpus/book-tape-20260823-snapshot.jsonl` | frozen copy of the engine book tape, 33,445 samples, 235 windows | 2026-08-23 02:45Z - 07:45Z (~5h) |
| `~/.pmt/corpus/prints.jsonl` | **new** — 167,941 real Polymarket prints, 214 windows, backfilled from data-api | same |
| `~/.pmt/corpus/outcomes.jsonl` | wallet-first validated winners | 113 of the windows |
| live CLOB `/books` | 60-76 simultaneous both-leg snapshots, 5 symbols x 2 durations | 2026-08-23 07:30-07:50Z |

Outcome truth is the validated corpus where it reaches (113 windows) and the window's own
terminal book otherwise (116 windows; a settled updown pair quotes 0.999/0.001). Where both
exist they agree **111/112**, so the inferred half is trustworthy at that granularity.

The live tape is append-only and the engine is still writing to it, so a re-run silently
sees a different corpus — and can flip an inferred outcome as a window's terminal book
matures. Every number below comes from the frozen snapshot; `firsthalf_lib.py` honours
`PMT_BOOK_TAPE` so the run is reproducible:

```
export PMT_BOOK_TAPE=$HOME/.pmt/corpus/book-tape-20260823-snapshot.jsonl
uv run python analysis/firsthalf_q1_arb.py --live
uv run python analysis/firsthalf_q2_bookstruct.py
uv run python analysis/firsthalf_q3_maker.py
uv run python analysis/firsthalf_q4_flow.py
```

**Standing caveat on everything below: this is ~5 hours of one night, one regime.** Where a
result is a structural invariant (§1) that doesn't matter. Where it's a P&L estimate (§3) or
a hit rate (§2, §6) it matters a great deal, and CIs are given.

---

## 1. Complete-set arb — the opportunity does not exist, and cannot

Scanning the book tape naively finds what looks like a real business: 1,158 samples (5.76%
of two-sided samples) where `up_ask + dn_ask` plus both taker fees comes to under $1.00, in
179 of 235 windows, "worth $707".

**All of it is fictional.** Three independent tells:

1. **The impossible direction appears at the same rate.** `bid_sum > 1.00` — mint a set for
   $1, sell both legs, free money — shows up in 10.52% of samples, against 10.48% for
   `ask_sum < 1.00`. A real edge is one-directional; symmetric "edge" is a symmetric error.
2. **It scales with book speed.** Hit rate is 5.09% when the book is quiet and 12.44% when
   it is moving (>0.02/s). That is snapshot lag, not liquidity.
3. **It vanishes under a clean measurement.** In 60 *simultaneous* both-leg snapshots pulled
   from the exchange in one batch request (5 symbols x 2 durations x 3 rounds):

   ```
   EXACT mirror (up_ask == 1-dn_bid AND up_bid == 1-dn_ask) : 60/60
   ask-sum < 1.000  (the arb)                               :  0/60
   bid-sum > 1.000  (reverse arb)                           :  0/60
   ```

**UP and DOWN are two mirrored views of one price.** Polymarket's CLOB matches complementary
orders by minting, so the pair ask-sum is `1.00 + spread` by exchange construction. The
crypto taker fee (`0.07 * p * (1-p)` per leg, ~3.5c total at p=0.5) then sits on top. The
median recorded pair ask-sum is 1.010 and the median pair bid-sum is 0.990 — exactly one
tick of spread around par, all night, on every symbol.

The tape's 46.4% exact-mirror rate (69.9% on settled books, p90 deviation 5c) is therefore a
**measure of pmengine's own two-leg desync**, not of market structure. See §7.3.

Verdict: **Phase 3.3 "Complete-set scanner" should be struck from ROADMAP.** There is
nothing to scan for. The pair-merge machinery in maker-design.md §4 is still correct as
*bookkeeping* — it just never produces profit on its own, only capital recycling.

---

## 2. Early-book structure — the early book is informative *and* calibrated

**Spread and depth (first half vs second half).** The spread does not widen early; it is one
tick throughout. What changes is depth, which grows ~2.7x median into the second half as the
outcome decides:

| | first half | second half |
|---|---|---|
| top-of-book spread p50 / p90 | 1.0c / 3.0c | 1.0c / 5.0c |
| best-bid size p50 / p90 | 50 / 240 sh | 134 / 8,376 sh |
| pair bid-sum p50 | 0.990 | 0.990 |

**Two-sidedness.** The book is two-sided almost immediately — 80.7% in the first decile
(the ramp is market-open latency), then 99.8-100% from decile 2 through 6. It is the *late*
window that goes one-sided: by decile 0.9-1.0 only 13.4% of samples still have both legs
quoted. **The early window is the two-sided part of a window's life.** Whatever prevents
quoting early, it isn't book availability.

**Is the early mid informative?** Strongly, and the operator's coin-flip hypothesis does not
hold for the book (it may still hold for the *model*, which is a different thing — see below):

| measured at | n (|mid-0.5|>0.10) | favoured side won | 95% CI | Brier vs flat-0.5 |
|---|---|---|---|---|
| 2 min in (5m only) | 150 | 84.7% | 78-90% | 0.156 vs 0.250 |
| frac 0.25 | 164 | 82.9% | 76-88% | 0.167 vs 0.250 |
| frac 0.50 | 184 | 87.5% | 82-92% | 0.135 vs 0.250 |
| frac 0.75 | 217 | 92.2% | 88-95% | 0.079 vs 0.250 |

But informative is not the same as exploitable. The reliability table says the early book is
**calibrated**, which is the worst possible news for anyone hoping early takers are donating:

```
favoured-side mid   n    mean mid  realized   95% CI     verdict          (frac 0.25)
0.50-0.60          62      0.549     0.516    39-64      calibrated
0.60-0.70          48      0.649     0.729    59-83      calibrated
0.70-0.80          46      0.758     0.783    64-88      calibrated
0.80-0.90          44      0.838     0.977    88-100     book UNDERprices
0.90-1.00          16      0.928     0.938    72-99      calibrated
```

So: **early takers are not donating, and there is no free spread for a maker to collect.**
Four of five buckets price the outcome correctly. The 0.80-0.90 exception is §6.

Note this does *not* contradict the operator's measurement that early directional refusals
are coin flips. That measurement is over windows where **our model** wanted to fire early;
this one is over windows where **the book** has an opinion. Different populations. The
interesting implication is that at frac 0.25 the book already knows what our banked-TWAP
model won't know for another minute or two — but it charges a fair price for it.

---

## 3. Maker P&L — negative in every configuration, and worse the more it fills

The correct P&L identity, given §1's mirror (maker fee is zero — fees.md, "Makers are never
charged fees"):

```
net = SUM over fills of (payoff_of_that_token - fill_price)
```

Pair-merge does not appear in it. Holding `min(up, dn)` as a complete set redeems for $1
whatever happens, which is exactly what marking each leg separately already says. **Merging
frees capital early; it does not create P&L.** maker-design.md §4 is right that it comes for
free from two-sided quoting, but it should not be counted as an income line.

Fill model is maker-design.md §5.3 method 1 (queue-conservative): join the **back** of the
visible level, so `queue_ahead` = recorded top-of-book size at our price, and only opposing
print volume in **excess** of that queue reaches us, capped at our remaining size. Requote
5s, dead-band 0.005 — the cadence maker-design.md §3 recommends. Prints are the real
data-api backfill, not the engine's flow fields (§7.1).

Two fill regimes are reported as bounds, because whether complementary matching fills our UP
bid from DOWN buy flow is supported by the corpus but not proven by it (the exact price
mirror and the never-violated bid-sum ≤ 1.00 say the mechanism is active; a size-consumption
regression is suggestive but noisy — corr 0.086 for buy-DOWN vs 0.020 for sell-UP).

| configuration | fills/win | net $/win | 95% bootstrap CI | c/share | net $/day |
|---|---|---|---|---|---|
| touch, 50sh, own-leg fills only (LOWER) | 3.4 | −1.72 | [−4.47, +1.62] | — | −338 |
| touch, 100sh, own-leg fills only (LOWER) | 4.0 | −2.36 | [−6.98, +3.44] | — | +140 |
| touch, 50sh, complementary (UPPER) | 43 | −11.55 | [−18.22, −4.52] | −2.4 | −13,535 |
| touch, 100sh, complementary (UPPER) | 71 | −20.78 | [−32.27, −9.16] | −2.5 | −22,563 |
| 1 tick behind touch, 100sh | 106 | −17.45 | [−28.09, −6.14] | −1.9 | −21,413 |
| 3 ticks behind touch, 100sh | 71 | −10.96 | [−20.89, −0.98] | −2.0 | −15,461 |

Read it as: **the only configurations that don't lose money are the ones that barely trade.**
The own-leg-only rows have CIs straddling zero because they fill 3-4 times a window; every
configuration that actually gets filled has a CI excluding zero on the wrong side.

Gross spread capture is real but trivial — $0.03 to $5 per window at the touch, up to $40 at
three ticks back — and adverse selection is 5-15x larger in every single row. Backing off
the touch raises gross capture a lot and loses *more*, because the fills you win further from
mid are the ones that ran through you.

**Why, in one number.** Compare what a maker can earn per fill against how fast the thing
they just bought reprices:

| series | half-spread | mean 5s \|Δmid\| | ratio |
|---|---|---|---|
| btc 5m | 0.51c | 3.65c | 0.14 |
| eth 5m | 0.59c | 3.90c | 0.15 |
| btc 15m | 0.53c | 2.30c | 0.23 |
| eth 15m | 0.71c | 2.18c | 0.33 |
| sol 5m | 1.55c | 3.72c | 0.42 |
| sol 15m | 0.85c | 1.83c | 0.46 |
| bnb 5m | 2.28c | 5.44c | 0.42 |

Every ratio is far below 1. The fair value moves 3-4 cents in the time it takes to requote,
and the entire spread is one cent. No γ, no κ, no inventory skew fixes that — the quote is
stale before it can earn its width. Note also that the ratio is roughly constant across
symbols: wider-spread symbols are wider because they are more volatile, not because they are
more generous. (XRP and DOGE quote 4-7c spreads live — the same trade, scaled up.)

**Queue position is not the problem either.** Re-running with `queue_ahead = 0` (we are
first at the level, not last) improves per-share adverse selection from −2.5c to −1.4c on
btc5m but makes the total *worse*, because it fills more: fleet net −$25.35/window vs
−$20.78. Being faster than the queue does not turn this positive; it just loses faster.

**The 20% maker rebate does not close the gap.** `feeSchedule` on the live market confirms
`{rate: 0.07, takerOnly: true, rebateRate: 0.2}`, and `rewardsMaxSpread: 4.5` /
`rewardsMinSize: 50` are configured, but `/rewards/markets/{cid}` returns an empty program
and `pmt rewards` shows **zero** reward events ever received. Even taking the rebate at face
value and assuming per-fill accrual, it is worth ~0.35c/share at p=0.5 — against 2.0-2.5c/share
of adverse selection. It is 13-16% of the hole. maker-design.md §2 is right not to bake it in.

---

## 4. Early flow as information — no signal, at all

With 167,941 real prints (vs the ~656 the engine's own recorder captured over the same span),
this is now well-powered, and the answer is clean.

**Raw:** signed first-half flow imbalance, folded onto one axis (`buy(UP) + sell(DOWN) −
sell(UP) − buy(DOWN)` — a taker buying DOWN *is* a taker selling UP, and counting the tokens
separately cancels the signal):

| window | n | flow's side won | 95% CI |
|---|---|---|---|
| frac 0.00-0.25, all | 219 | 48.9% | 42-55% |
| frac 0.00-0.25, \|imb\|≥0.2 | 82 | 47.6% | 37-58% |
| frac 0.00-0.50, \|imb\|≥0.2 | 63 | 60.3% | 48-71% |
| frac 0.25-0.50, all | 219 | 60.3% | 54-67% |

Nothing survives. The one bucket that looks interesting (0.25-0.50 at 60.3%) is a weaker
version of what the *price* at frac 0.50 already tells you at 87.5%.

**Incremental over the mid — the only part that would be new information:**

| | n | mid's side won | 95% CI |
|---|---|---|---|
| frac 0.25, flow AGREES with mid | 73 | 79.5% | 69-87% |
| frac 0.25, flow DISAGREES with mid | 63 | 82.5% | 71-90% |
| frac 0.50, flow AGREES with mid | 66 | 86.4% | 76-93% |
| frac 0.50, flow DISAGREES with mid | 51 | 90.2% | 79-96% |

**Flow disagreeing with the price makes the price *slightly more* right, not less.** The
proposed "full clip on agreement, half clip on disagreement" rule would size down exactly
the windows that perform best. Do not build it.

**What flow *does* say — the adverse-selection tax.** Marking every first-half print against
its eventual payoff:

| taker side | prints | shares | taker edge/share | vs the mid at the time |
|---|---|---|---|---|
| buy | 67,385 | 1,417,151 | −1.33c | +0.22c |
| sell | 15,848 | 129,190 | +1.25c | +2.44c |

Taker **buys** are uninformed: they pay the spread and get +0.22c of information back, i.e.
nothing. Taker **sells** are informed: +2.44c/share versus the mid. And 91.5% of all volume
is buys. That asymmetry is the whole story of §3 — the passive side of a sale is where the
money leaks, and the queue-conservative model only ever fills us in the bursts that sweep the
level, which is the toxic subset of an already-toxic side.

---

## 5. Literature (secondary)

**Avellaneda-Stoikov τ-dependence — the operator's hypothesis is backwards.** The AS optimal
half-spread is `δ* = γσ²τ + (2/γ)ln(1+γ/κ)`, in which the risk term is *largest* at large τ.
Early, large-τ quoting is where AS says the spread must be **widest**, not tightest or
safest: you are quoting against the most unresolved variance you will ever face in that
window, and the model wants to be paid for it. It is late (τ→0) that classical AS narrows,
to unwind. Our measured ratio table in §3 is the empirical form of the same statement — the
required width early is 3-7x the width the book actually offers.

**Feil & Nendel 2026 (arXiv 2607.17991) — settlement risk makes it U-shaped, not monotone.**
Solving the full HJB with a binary terminal payoff, "the spread generally decreases over
time, as the liquidity parameter k(t) rises," but this "reverses near settlement when prices
hover around p=0.5, where the spread widens for prices close to p=1/2, where settlement risk
is highest"; and at any fixed time, spreads are larger at p=0.5 than at extremes. On skew,
"as the remaining horizon shortens, the running inventory penalty becomes less important,
which pushes the skew toward zero. Closer to settlement, however, there is less time to
unwind positions before the terminal penalty is imposed, which leads to stronger quote
adjustments." So the required spread is widest early *and* again near settlement at p≈0.5,
with a trough in between — the early window is not the cheap end of the curve.

**blockchainhansi — quotes both sides from window open, but its edge is the pair-cost cap,
not the spread.** Per maker-design.md §4, hansi's whole strategy is bidding both YES and NO
and clamping each bid at `1.00 − opposite_avg_cost` so a merge always locks. On a book where
the pair bid-sum is pinned at 0.990 by construction (§1), that clamp binds immediately and
the locked profit is exactly one tick — before adverse selection. Its economics on Polymarket
crypto updown are the §3 table, whatever they may be on slower markets.

---

## 6. The one thing that looks like money — and it is directional

The 0.80-0.90 reliability bucket in §2 underprices badly, and it survives every split
including the one that removes inferred outcomes entirely:

```
split                        frac   n   mean mid  realized   95% CI
all                          0.25   44    0.838     0.977    88-100%
all                          0.50   49    0.852     0.980    89-100%
btc                          0.25   16    0.829     0.938    72-99%
eth                          0.25   15    0.847     1.000    80-100%
sol                          0.25   13    0.838     1.000    77-100%
5m only                      0.25   35    0.841     0.971    85-99%
15m only                     0.25    9    0.824     1.000    70-100%
wallet/chainlink truth only  0.25   36    0.836     0.972    86-100%
```

Priced as a real taker clip — buy the favourite **at the ask**, pay the fee, hold to
resolution:

| band | frac | n | avg ask | fee | win% | EV c/share | 95% CI | $/win @ $50 |
|---|---|---|---|---|---|---|---|---|
| 0.80-0.90 | 0.25 | 44 | 0.846 | 0.91c | 97.7% | **+12.23c** | [+7.5, +15.0] | +6.12 |
| 0.75-0.92 | 0.25 | 80 | 0.826 | 0.99c | 91.2% | **+7.66c** | [+1.2, +13.2] | +3.83 |
| 0.60-0.70 | 0.40 | 55 | 0.645 | 1.60c | 78.2% | +12.11c | [+1.1, +23.1] | +6.06 |
| 0.70-0.80 | 0.25 | 49 | 0.764 | 1.25c | 75.5% | −2.19c | [−14.7, +9.7] | −1.10 |
| 0.90-1.01 | 0.25 | 16 | 0.936 | 0.41c | 93.8% | −0.29c | [−13.4, +6.8] | −0.14 |

**Treat this as a hypothesis, not a finding.** Ten bands were tested at two time points; one
or two clearing p<0.05 is what noise produces, and the pattern is non-monotone (0.70-0.80 is
negative and 0.90-1.00 is negative, with 0.80-0.90 spiking between them), which is exactly
what a fluke looks like. It is also 5 hours of one night. But it is the only positive number
this pass produced, it survives per-symbol and per-duration splits, and it is cheap to test
properly: it reuses `decide()`, needs no new order type, and is the natural companion to R3's
max-entry-price work (R3 asks whether a ~0.70 cap helps; this asks whether there is a *floor*
below which early entries are actually the good ones).

---

## 7. Corpus defects found — worth more than any strategy above

### 7.1 Polymarket prints are backfillable. ROADMAP says they are not.

ROADMAP.md:36-37 lists Polymarket prints under "Forward-only", and Phase 0 treats the live
recorder as the only way to ever get them. **This is wrong.** `data-api.polymarket.com/trades?market={conditionId}`
serves the complete public print history per market long after the window resolves — side,
size, price, timestamp, outcome, and counterparty wallet. `analysis/firsthalf_harvest_prints.py`
pulled 167,941 prints across 214 windows in about 20 minutes, and it will reach back over
however much history the endpoint retains.

That single fact un-blocks R8 (which has been waiting on a live recorder) and gives the maker
replay in maker-design.md §5.3 a real fill tape today rather than after weeks of accumulation.

**Trap, and it cost this pass an hour:** the endpoint accepts `slug` and `after` parameters,
returns HTTP 200, and **silently ignores both**. A `slug`-keyed harvest returns the global
cross-market feed and looks like it worked. Only `market={conditionId}` filters. Verified:

```
after=<midpoint of the returned range>  -> same 20 rows, unchanged
after=<far future>                      -> same 20 rows, unchanged
```

### 7.2 The engine's own print-flow recorder is capturing ~0.4% of prints

`updown.rs record_book()` writes `up_tn/up_tbuy/up_tsell/dn_*` every sample. Over the 1.9h
those fields have existed it recorded **656 prints in 14 non-zero samples out of 12,312**.
Actual print count over the same windows: ~168,000. The non-zero samples arrive in ~5-minute
bursts of 40-58 prints and are zero in between.

`engine.rs:930` calls `get_market_trades_since(&cid, after, limit)`, and `client.rs:479`
builds `...&after={ts}` — the cursor §7.1 proves is ignored. The comment above it ("Per-condition
cursor (last_ts_per_cond) narrows each poll to new trades") describes behaviour the API does
not implement. That alone doesn't explain a 99.6% loss rate, so there is at least one more
fault in the path (the `seen` dedup set and the per-arm `trade_hwm` reset on roll are the
next suspects), but the recorder is definitively not producing an R8 corpus, and the warn
added at engine.rs:935 does not fire because the calls succeed — they just return already-seen rows.

Given §7.1, the cheapest fix is to stop depending on the live path for research: backfill.

### 7.3 The book tape's two legs desync, injecting phantom cross-leg noise

`record_book()` reads `ctx.order_books` for the two tokens independently, so the two legs of
one sample can be from different instants. Measured against the exchange invariant of §1:
only **46.4%** of fully two-sided samples are exact mirrors (69.9% on settled books), with a
p90 deviation of 5c and p99 of 18c.

Any analysis that treats UP and DOWN as two independent price sources — including the
`mid_up()` merge used in this document, and anything R4/R8 might do with cross-leg
disagreement — inherits up to 5-18c of noise that is not in the market. Two cheap fixes,
either sufficient: record only one leg and mirror it, or stamp each leg with its own
last-update time so consumers can drop stale legs.

---

## 8. Candidates ranked by expected $/day per unit of build effort

| # | candidate | measured opportunity | build cost / reuse | risk class |
|---|---|---|---|---|
| 1 | **Print-flow backfill into corpus tooling** | $0 direct, but unblocks R8, the maker replay fill model, and any toxicity work — a Phase-0 gap ROADMAP calls unfillable | **~zero, already written** (`firsthalf_harvest_prints.py`, ~150 LOC). Fold into `pmt crypto` beside `outcomes` | none — read-only HTTP |
| 2 | **Early book-band entry** (§6) | +7.7c/share, CI [+1.2, +13.2] on the 0.75-0.92 band at frac 0.25; ~+$3.8/window on a $50 clip. n=80, one night | small — reuses `decide()`, no new order type, composes with R3's price-cap sweep | directional; unproven, real multiple-comparison risk |
| 3 | **Two-leg desync fix** (§7.3) | $0 direct; removes 5-18c of phantom noise from every future book study | small — one function in `record_book()` | none |
| 4 | **Flow-informed sizing** (R5/R8 clip tilt) | **zero.** 48.9% raw; disagreement makes the mid *more* right, so the proposed rule sizes down the best windows | small | n/a — do not build |
| 5 | **Early maker** (maker-design.md Phase 3.1) | **−$13.5k to −$22.5k/day** at 100sh two-sided; the only non-losing configs fill 3-4x/window and straddle zero | large — steps 0-5 of §6 of the brief: post-only plumbing, new strategy, new `Action::Quote`, replay fill model | high — capital at risk against informed flow |
| 6 | **Complete-set sniper** (ROADMAP Phase 3.3) | **zero, structurally.** 0/60 live samples; the pair ask-sum is 1.00+spread by exchange construction | would have been medium (post-only + two-leg atomic execution) | n/a — strike from ROADMAP |

---

## 9. Recommendation — build #1, and only #1

**Ship the print-flow backfill as corpus tooling (`pmt crypto prints`, beside `pmt crypto
outcomes`), and re-point R8 at it.**

It is the only item on the list with a positive expected value per unit of effort that isn't
speculative. The code exists and is read-only. It costs nothing at runtime and risks no
capital. And it changes what the roadmap can do *this week* rather than in a month:

- **R8 stops waiting.** ROADMAP has R8 blocked on a live flow recorder that §7.2 shows is
  capturing 0.4% of prints. Backfill gives it a full corpus immediately — and this pass
  already used that corpus to answer R8's core question in §4 (the answer is no, which is
  worth knowing before building the guard).
- **maker-design.md §5.3's replay gap closes.** The brief's step 2 ("extend replay.rs with
  `Action::Quote` handling... using only fields book-tape.jsonl already records") was written
  around a flow tape that doesn't exist. It does now.
- **§6's hypothesis becomes testable.** The one positive result in this pass needs more
  windows to survive its multiple-comparison problem, and backfill is how you get them
  without waiting for calendar time.

**Do not build a first-half strategy.** The honest finding is that the first half is not idle
for lack of opportunity — it carries **47.7% of the day's taker notional** and is the *most*
two-sided part of a window's life. It is idle because the only edge available there is
directional, and the book already prices it correctly in four of five buckets. A one-cent
spread against a fair value that moves 3.65c every five seconds is not a market-making
opportunity at any size, queue position, skew, or cadence, and the 20% rebate covers 13% of
the gap.

The capital is better left uncommitted until R9's evidence arrives than spent buying the
right to be adversely selected for two and a half minutes.
