# R2 — calibrated quarter-Kelly sizing study

**Question asked:** peak concurrent committed looked like ~$423 against ~$1,300 of capital;
size up the proven arms via R2 (calibrated quarter-Kelly) to use the capital.

**Answer:** the premise is wrong in one place and the lever does not do what was hoped in
another. Peak concurrent *committed* capital post-brake is **$928.63 (71% of $1,300)** — the
$423 figure is the peak of the **un-decided (speculative) slice only**, and it comes from the
pre-brake part of the night (r7 puts it at 02:10:25Z, 27 minutes before this corpus starts).
Utilization is not low. And every R2 policy in the sweep **lowers** peak committed capital,
because a `cap_frac`-of-bankroll window ceiling (0.15 × 1300 = $195) is *smaller* than the
$350–400 window budgets already armed, and because calibration refuses the ≥0.98 asks the
fleet currently pays. **R2 is a risk-reallocation lever on this corpus, not a utilization
lever.**

Reproduce: `cd pmtrader && uv run python ../analysis/r2_sizing_sim.py --chosen 0.15,100,25,0.70`
Raw output frozen at `analysis/r2_sizing_report.txt`. Tape is live; n grows between runs.

---

## 1. Corpus (item 3)

`pmt crypto outcomes` re-run first (read-only refresh): 331 windows evaluated → 220 validated
rows (139 wallet, 81 Chainlink), 111 dropped stale.

| | n |
|---|---|
| post-brake FIRE records (t ≥ 1787452500) | **512** across **94 windows** |
| …in decided windows (the P&L corpus) | **466 fires / 87 windows** |
| …in undecided windows (excluded) | 21 fires / 3 windows |
| …fired but nothing crossed | 25 fires / 4 windows |
| calibration draws (validated outcome, fill or not) | **90 windows** |
| lifetime decided windows with fires (whole tape) | **152** |

Post-brake wallet-graded ledger: **82W–5L, −$25.37 realized on $9,444 bought.**

### Two corrections the corpus forced

**(a) `outcomes.jsonl` mislabels the largest post-brake loss.**
`btc-updown-15m-1787457600`: we bought $265.21 of **Down**, received $0, and the file records
`winner: down` from `source: wallet`. The bug is in `polymarket/outcomes.py::wallet_outcomes` —
that window's $0 REDEEM row carries `outcome: "Up"` with `size: 0`, and the code assumes a $0
redeem's `outcome` field names *our held side* and flips it. Here it names the **winner**, so
the flip runs backwards. Every other $0 redeem in the corpus carries our real held size
(e.g. `btc-updown-15m-1787454000`: `outcome: Down, size: 364.998`), which is why this is the
only window affected. `pmt crypto stats` grades it correctly (it uses redeem-payout, not the
outcome field). **This sim uses wallet truth and treats it as a $265.21 loss.** Anything
replayed against `outcomes.jsonl` as-is is currently scoring a −$265 window as a win.

**(b) $254.03 of un-redeemed exposure is not benign.**
`eth-updown-5m-1787462100` (−$147.91 at risk) and `sol-updown-5m-1787462100` (−$106.12) both
bought **Down** in the same minute, both watched the book collapse to $0.01/$0.10 on their own
side, and neither has paid out hours later. They are excluded per the corpus rule (validated or
dropped, never guessed). **If they settle as losses the night is −$279.40, not −$25.37** — and
they are a textbook R7 event: two arms, one minute, one direction, one macro move.

### Fill reality (v1's baseline was wrong)

| | |
|---|---|
| intended taker notional (Σ size × ask, the v1 baseline) | $13,502.05 |
| actually crossed (wallet BUY `usdcSize`) | **$9,753.09 = 72.2% of intent** |
| per-window fill ratio | median 0.802, range 0.000 → 1.017 |
| realized cost/share ÷ quoted ask | **median 1.0014** |

v1 spent the tape's `size × ask` as if it were money. It isn't — a quarter of it never crossed
(ROADMAP's fill-chasing audit, reproduced independently here), and the fraction swings 0.17×–1.01×
per window. Every policy in this study is scaled through the window's **measured** fill ratio and
its **measured** realized cost per share, which makes the "actual" policy reproduce the wallet to
**$0.10 on −$25.37** — that identity is the sim's own unit test.

Side finding: the realized taker cost is ~**0.14%** over quoted ask, not the engine's modeled
`7% × min(ask, 1−ask)` (≈0.5–0.9¢/share). The engine's `net` is conservative by roughly that
much. The edge model here keeps the engine's fee (conservative → smaller sizes); P&L uses the
measured cost.

---

## 2. What was fixed vs `r2_kelly_sim.py` (items 1 & 2)

| v1 gap | v2 fix |
|---|---|
| raw stated-fair degenerates (fair pins 0.999, Kelly always bets the cap) | window-level isotonic calibration; unpopulated buckets fall back to the nearest *populated* fitted value, never to raw fair |
| no per-clip sub-cap | `size = min(kelly, clip_cap[sym], window_room, fleet_room)`; `clip_cap` swept {25,50,75,100} btc/eth and {10,25} sol/bnb |
| clip-level bucketing overweights many-clip windows | calibration is **one Bernoulli draw per decided window**, stated value = first-fire `fair` (notional-weighted reported as sensitivity) |
| `size × ask` treated as cash | measured fill ratio + measured cost per share |

**bnb has no arms and no fires** — the tape contains btc/eth/sol/xrp only. The {10,25} minor grid
therefore exercises sol alone; the bnb cells are reserved, not measured.

### Window-level calibration (first-fire fair, primary)

| bucket | n | wins | raw | isotonic | Wilson 95% lo | ≥30? |
|---|---|---|---|---|---|---|
| [0.900,0.950) | 3 | 3 | 1.000 | 0.667 | 0.438 | fail |
| [0.950,0.970) | 3 | 1 | 0.333 | 0.667 | 0.061 | fail |
| [0.970,0.980) | 5 | 4 | 0.800 | 0.800 | 0.376 | fail |
| [0.980,0.990) | 11 | 11 | 1.000 | 0.975 | 0.741 | fail |
| [0.990,0.995) | 5 | 5 | 1.000 | 0.975 | 0.566 | fail |
| [0.995,0.999) | 6 | 6 | 1.000 | 0.975 | 0.610 | fail |
| **[0.999,1.000)** | **57** | **55** | 0.965 | **0.975** | **0.881** | **PASS** |

Notional-weighted sensitivity moves the top bucket to n=61, 59W, fit 0.967 — same story.

---

## 3. The headline finding: uncapped calibrated Kelly bets *hardest* on the losers

Calibrated Kelly sizes on `(p_cal − c)/(1 − c)` where **c is the book's ask**. Every loss window
in this corpus has the same shape — the book collapses on our side while the model stays pinned
near 1.0 — so a **collapsed ask reads to Kelly as an enormous edge**. With no ask floor,
calibrated quarter-Kelly puts its largest bets into precisely the windows that lose, and every
`min_ask = 0.00` cell at cap_frac 0.15/0.25 does **worse** than the flat clips the engine
actually traded (−$16 to −$377 vs −$25 actual).

The per-clip sub-cap alone cannot fix this: the sub-cap limits one clip, but a collapsing book
emits many clips (`btc-updown-15m-1787454000` fired 11 times, the last four at asks 0.62 → 0.15
→ 0.04). A **min_ask floor** is the missing lever, and it is a *different* lever from ROADMAP
R3's proposed 0.70 **max** entry price. Note that calibration produces R3's max-price effect for
free: **100 of 512 clips get zero size** because a calibrated p of ~0.975 cannot pay a 0.98 ask.

### Sweep, `min_ask = 0.70` (bankroll $1,300, quarter-Kelly)

| cap_frac | window $ | clip btc/eth | clip sol | clips | notional | P&L | max DD | worst window | peak $ | util | un-dec $ |
|---|---|---|---|---|---|---|---|---|---|---|---|
| 0.10 | 130 | 50 | 25 | 242 | 5,901 | +43.22 | −195.66 | −130.05 | 524 | 40% | 270 |
| 0.10 | 130 | 100 | 25 | 223 | 7,021 | +67.48 | −195.80 | −130.05 | 571 | 44% | 372 |
| 0.15 | 195 | 50 | 25 | 274 | 7,142 | +17.02 | −282.81 | −195.07 | 655 | 50% | 329 |
| 0.15 | 195 | 75 | 25 | 251 | 8,205 | +72.33 | −256.76 | −195.07 | 742 | 57% | 392 |
| **0.15** | **195** | **100** | **25** | **240** | **9,033** | **+105.35** | **−257.16** | **−195.15** | **791** | **61%** | **480** |
| 0.25 | 325 | 100 | 25 | 267 | 11,524 | +50.64 | −415.01 | −325.04 | 1,090 | 84% | 515 |

vs **actual: −$25.27 P&L, −$399.00 max DD, −$265.21 worst window, $928.63 peak (71%)**.

Full 24-cell grids for both ask floors are in `r2_sizing_report.txt`.

---

## 4. Per-series, chosen cell (cap_frac 0.15 / clip 100 btc-eth / 25 sol / min_ask 0.70)

| series | n | W–L | act $ | act P&L | act peak | pol $ | pol P&L | pol max DD | pol worst | pol peak |
|---|---|---|---|---|---|---|---|---|---|---|
| btc15m | 12 | 9-3 | 2,717 | −301.68 | 352 | 2,165 | −349.17 | −402 | −195.15 | 195 |
| btc5m | 20 | 20-0 | 1,576 | +110.24 | 150 | 2,079 | **+163.52** | 0 | 0.00 | 196 |
| eth15m | 12 | 11-1 | 2,489 | +123.74 | 355 | 1,712 | +110.97 | 0 | 0.00 | 195 |
| eth5m | 17 | 17-0 | 1,155 | +69.26 | 189 | 1,800 | **+135.27** | 0 | 0.00 | 197 |
| sol15m | 2 | 1-1 | 277 | −139.15 | 142 | 97 | −72.88 | −73 | −73.47 | 73 |
| sol5m | 24 | 24-0 | 1,231 | +112.32 | 155 | 1,179 | +117.65 | 0 | 0.00 | 195 |

The 5m arms are where sizing up pays (btc5m +$53, eth5m +$66 on tonight's fires); the 15m arms
are where the losses live and where the policy is roughly neutral-to-worse.

### The loss windows (item 4)

| window | actual $ | actual P&L | policy $ | policy P&L | Δ |
|---|---|---|---|---|---|
| btc-updown-15m-1787457600 | 265.21 | −265.21 | 194.67 | −194.67 | **+70.54** |
| btc-updown-15m-1787454000 | 169.84 | −169.84 | 195.15 | −195.15 | −25.32 |
| sol-updown-15m-1787457600 | 142.45 | −142.45 | 73.47 | −73.47 | **+68.98** |
| btc-updown-15m-1787464800 | 100.39 | −100.39 | 128.87 | −128.87 | −28.48 |
| eth-updown-15m-1787453100 | 18.32 | −18.32 | 0.00 | 0.00 | +18.32 |
| **TOTAL** | | **−696.20** | | **−592.16** | **+104.03** |

Did the policy shrink them? **Partly, and not by design.** It shrank the two biggest (the ones
that had run past today's $195-equivalent) and *grew* the two that were smaller than the window
cap — the cap binds downward but the Kelly term binds upward. The one it eliminated
(`eth-updown-15m-1787453100`) went to zero because its 0.83 ask couldn't clear a calibrated p,
not because a sizing rule caught it.

Note this is **five** loss windows, not four. `pmt crypto stats` shows 4 because
`eth-updown-15m-1787453100` (−$18.32) never emitted a redeem row and gamma still reads
unresolved, so stats grades it *open*; the Chainlink corpus validates it as a loss.

---

## 5. Honesty gates (item 5)

### Which buckets clear the ≥30-decided-window bar TODAY

- **Post-brake corpus: exactly one — `[0.999, 1.000)`, n=57 (55W), pooled across all six arms.**
  Every other bucket is n ≤ 11.
- **Whole tape (152 decided windows): still exactly one — `[0.999,1.000)`, n=80 (78W, 97.5%).**
  Next largest is `[0.980,0.990)` at n=15. More nights will not fix this quickly: the policy
  fires at fair ≥ 0.999 most of the time by construction, so the sub-0.999 bins fill slowly.
- **Per-(series × bucket) cells clearing the bar: NONE.** The largest is btc5m's top bucket at
  n=16. **No per-series size-up is authorized by R2 today**, including btc5m.
- The "btc5m 32-0 lifetime" figure doesn't reproduce on this join: lifetime tape-vs-outcomes puts
  btc5m at **38-2** (both losses pre-brake: 1787436000, 1787436900), post-brake **20-0**.

### How wide is "what the data justifies"?

Same passing bucket, three defensible readings of p, at $1,300 bankroll:

| ask | p = raw 0.965 | p = isotonic 0.975 | p = Wilson 95% lo 0.881 |
|---|---|---|---|
| 0.85 | $243 | $266 | $47 |
| 0.90 | $202 | $237 | no bet |
| 0.93 | $150 | $199 | no bet |
| 0.95 | $80 | $148 | no bet |
| 0.97 | no bet | $30 | no bet |

The point estimate says clips of $150–240 (3–5× today's). The 95% lower bound of a 57-window
sample says **decline every ask the fleet actually pays**. Quarter-Kelly is supposed to absorb
estimation error; at n=57 the estimation error is wider than the whole sizing decision. That gap
is what the ≥30-per-bucket bar is really protecting, and 57 windows in one bucket is the
*floor* of that bar, not comfortable clearance.

### Is tonight's improvement real?

Window bootstrap (4,000 resamples of the 90 decided windows):

| policy | P&L delta vs actual | 90% CI |
|---|---|---|
| chosen cell (0.15/100/25/0.70) | +$130.62 | **[−$60.65, +$345.89]** |
| bar-gated Kelly (R2-legal) | +$83.89 | **[−$98.91, +$287.80]** |

**Both intervals straddle zero.** The entire edge is carried by five windows out of ninety.
Tonight's improvement is not distinguishable from which five windows happened to lose.

### Depth

Sized-up clips have to fill. Top-of-book notional on our side at the fire tick (n=480 fires
matched within 15s): p25 $18.80, median $49.48, p75 $134.94.

| clip | covered by top-of-book alone |
|---|---|
| $10 | 83% |
| $25 | 70% |
| $50 | 50% |
| $75 | 40% |
| $100 | 31% |

A $100 clip walks the book 69% of the time and pays worse than the tape's quoted ask. The sim
does **not** model that extra slippage, so **every large-clip P&L above is an upper bound**.
The $100 cell's advantage over the $50 cell is the least trustworthy number in this report.

### R7 correlated-fleet constraint

Applied as a hard constraint on the chosen cell — a $250 fleet-wide cap on un-decided committed
notional:

| variant | clips | notional | P&L | max DD | worst window | peak $ | un-dec $ |
|---|---|---|---|---|---|---|---|
| no fleet cap | 240 | 9,033 | +105.35 | −257.16 | −195.15 | 791 | 480 |
| **fleet cap $250** | 231 | 8,307 | **+31.25** | −266.39 | −195.01 | 709 | 250 |

The R7 cap costs **$74 of the $105 edge** (15 clips shrunk or blocked) and the CI already
straddled zero before that haircut. Every R2 policy here *raises* the un-decided slice
($237 actual → $439–480 policy) while lowering total committed — it moves capital from decided
(safe) to speculative (correlated). That is exactly the pool R7 exists to cap, and the two
un-redeemed eth5m/sol5m windows that opened in the same minute are the live demonstration.

### Other caveats

- Calibration is fit **in-sample** on the same windows it sizes. Walk-forward belongs on the
  replay harness.
- Fill ratio is held constant per window under resizing. Smaller clips would likely fill a
  *higher* fraction and larger clips a lower one, so the sim is mildly generous to large caps
  (compounding the depth caveat).
- One night, one regime (2026-08-23 02:37Z → 07:23Z). ρ, session, and volatility are not
  controlled for.

---

## 6. Recommendation — what the data supports TODAY

| series | n decided | W–L | top-bucket n | current size/clip | **RECOMMENDED size/clip** |
|---|---|---|---|---|---|
| btc5m | 20 | 20-0 | 16 | 400/50 | **UNCHANGED (400/50)** |
| btc15m | 13 | 10-3 | 10 | 350/25 | **UNCHANGED (350/25)** |
| eth5m | 17 | 17-0 | 9 | 350/50 | **UNCHANGED (350/50)** |
| eth15m | 13 | 12-1 | 9 | 350/50 | **UNCHANGED (350/50)** |
| sol5m | 25 | 25-0 | 12 | 150/25 | **UNCHANGED (150/25)** |
| sol15m | 2 | 1-1 | 1 | 150/25 | **UNCHANGED (150/25)** |

**Every arm unchanged.** Not one (series × p-bucket) cell reaches 30 decided windows; the only
bucket that clears the bar clears it *pooled across all six arms*, and pooling assumes the arms
share one calibration curve — which the 5m/15m split in this very corpus argues against (5m arms
went 61-0 post-brake, 15m arms 21-5, and all five losses are 15m). ROADMAP's rule is explicit: no size increases without an
R2 calibration pass at ≥30 decided windows per bucket. **Today that pass does not exist at the
granularity a per-arm size change needs.**

### Expected capital-utilization change

| policy | peak concurrent committed | util | un-decided slice |
|---|---|---|---|
| actual flat clips | $928.63 | **71%** | $237.16 |
| bar-gated Kelly (R2-legal) | $780.80 | 60% | $439.03 |
| full calibrated Kelly (chosen cell) | $791.19 | 61% | $479.74 |

**Utilization goes DOWN by ~10 points, not up.** The only cell that raises it is
cap_frac 0.25 / clip 100 ($1,090 peak, 84%) and it costs half the P&L edge (+$50.64 vs +$105.35)
while doubling max drawdown (−$415 vs −$257). If the goal is deploying idle capital, R2 is the
wrong instrument — the honest routes are **more arms** (more independent windows open at once,
which R7's ρ measurement has to bless first) or **higher `cap_frac`**, which is a pure risk
decision with no calibration support behind it.

### What the data *does* support, without a size change

1. **Fix `wallet_outcomes`** — a $0 REDEEM row whose `outcome` names the winner is currently
   flipped backwards, scoring a −$265 window as a win in `outcomes.jsonl`. This poisons any
   replay A/B run against that file. Zero risk, immediate.
2. **A `min_ask` floor is the missing brake, not a smaller clip.** The engine's three brakes are
   speculative-entry brakes; once `banked_decided` flips, `btc-updown-15m-1787454000` bought
   $97 at asks of 0.15 and 0.04. Every loss window has the collapsed-book fingerprint. This
   needs a replay A/B before it goes near the live engine (ROADMAP: every engine change).
3. **Keep filling the [0.980,0.999) buckets.** They are the ones a size-up decision needs and
   they hold 15, 13, and 12 lifetime windows. Until they clear 30, the sizing question stays
   open regardless of how good btc5m's record looks.
