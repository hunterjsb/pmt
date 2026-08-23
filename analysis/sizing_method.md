# Sizing method — how much each arm gets, and why (2026-08-23)

**Question asked:** "an actual methodology behind how we allocate funds to each
arm — aren't they still just random arb numbers?"

**Answer: yes, they are, and they are 6.5x too big.** The live fleet's
per-window ceiling is **$2,400 against a $2,283 bankroll**. The
correlation-adjusted *full*-Kelly total on the fleet's own graded record is
**$367**. Quarter-Kelly — the ROADMAP's standing intent — is **$92**. Live
sizing is not "aggressive Kelly", it is past the point where the arithmetic has
a positive answer at all: at $2,400 of concurrent exposure, a fleet slot this
book *actually produced today* (17:15Z, five arms, five losses, −100% of the
slot) takes the account past zero. The growth ladder below prints `RUIN` there,
and that is a measurement, not a tail assumption.

Reproduce:

```bash
uv run --project pmtrader python analysis/sizing_method.py            # frozen snapshot
uv run --project pmtrader python analysis/sizing_method.py --refresh  # re-walk the wallet
```

Frozen output at `analysis/sizing_method.txt`; the graded per-window snapshot the
whole table is computed from is `analysis/sizing_windows.json` (280 windows), so
every number here is checkable without a wallet walk.

---

## 0. The one identity everything else hangs off

A window that wins pays $1/share on shares bought at `c`. A window that loses
pays $0 and the **entire** committed notional is gone. That second half is not a
modelling choice — across all 280 wallet-graded windows, every era, every arm,
the loss leg is exactly **−100.00% of notional**. There is no partial loss in
this book.

So per dollar of notional the bet is `+(1−c)/c` with probability `p` and `−1`
otherwise, which collapses to:

> **break-even win rate == the price we pay.**

Buy at 0.948 and you must win 94.8% of the time to tread water. Everything
downstream is "does this arm's win rate beat its own entry price, by how much,
and how sure are we".

Two consequences worth stating out loud because the live ladder violates both:

* **Size cannot create edge.** Only entering cheaper or picking better can.
  Doubling an arm doubles the exposure to whatever sign the edge already carries,
  and past the growth optimum it makes a positive edge lose money faster.
* **The all-time ledger is exactly the signature of over-betting a real edge.**
  257W–23L (91.8%) and **−$565.75** on $25,478 of notional. A fleet that wins
  92% of its windows and loses money is not a fleet with no edge; it is a fleet
  whose stake is past the growth optimum, where the wins are too small to pay
  for the losses they fund.

---

## 1. Measuring the edge (per arm)

Source: the ONE stats acquisition path, `cli_crypto_stats.score_activity` over
the wallet's own activity plus the resolution cross-check — never the model's
own read of its own window. 280 graded windows.

### The payoff is measured on WINNING windows only

This is the load-bearing estimator choice and two wrong ones are easy to reach
for first:

* `sum($)/sum(shares)` across all windows is correct about the past and useless
  about the future. One eth window bought into a book that had collapsed to
  **8.5c** contributed **1,740 shares** and drags eth's apparent cost from 0.95
  to **0.40**. Those shares paid $0.
* The notional-weighted price across *all* windows is better but still biased in
  our favour, because **a cheap entry price is correlated with losing**. Buying
  at 0.45 lowers the average price (raising apparent payoff) while its loss is
  counted only once in `p`. Pairing an all-window price with an all-window win
  rate books the discount and ignores that we only got the discount on trades
  that went to zero.

Only winners ever collect `(1−c)/c`, so only winners can measure it. The loss leg
needs no estimator: it is −100%, exactly.

### Era and feed weighting — `n_eff`, not a headcount

`n_eff` weights each window by how much its policy regime still predicts the
present, keyed to `polymarket/eras.py`:

| era | weight | why (the registry's own reason) |
|---|---|---|
| `pre-brake` | **0.00** | no brakes, no theta. ROADMAP forbids ever restoring this policy — those windows measure a strategy that cannot legally be run again |
| `brakes` | 0.25 | brakes exist, but entry is the 50% clock gate R9 retired, and the book is the 2s REST poller |
| `theta` | 0.50 | theta entry + window brake latch — the current decision core, still on the REST book |
| `ws+scale` | 1.00 | WS-authoritative book, today's size ladder |
| `stream` | 1.00 | current regime |

Times a **feed discount of 0.5** on any pre-`stream` window belonging to an arm
that has since migrated to the Chainlink settlement stream (btc5, eth5, xrp5): a
binance-fed window measured a different market-data path than the arm runs now.
Half, not zero, because the decision core (theta, brakes, cushion) is unchanged
across the migration.

xrp needs no special case in the end: **its entire binance record is two
pre-brake windows, both losses, both basis events** — the ones that struck it off
the tradeable list. Era weight 0 removes them by construction, which is the
correct treatment and not a thumb on the scale. Its 26 stream-era windows are the
whole of its evidence.

### Small-sample discount

The win rate is shrunk toward the arm's **own break-even** with a Beta prior of
strength **K = 30**:

```
p_shrunk = (wins_eff + K*c) / (n_eff + K)
```

The prior mean is `c`, i.e. **zero edge**: an arm with no record sizes to zero
and has to earn its way up. K is 30 because that is the ROADMAP's own calibration
bar ("no size increases until each p-bucket shows calibration over ≥30 decided
windows") — an arm's record only outvotes the null once it clears that bar.

### The table

| arm | raw W–L | n_eff | feed | c = break-even | p_raw | p_shrunk | Wilson 90% lo | raw edge | quarter-Kelly | state |
|---|---|---|---|---|---|---|---|---|---|---|
| sol 5m | 63–3 | **40.5** | binance | 0.9344 | 0.9630 | 0.9508 | 0.9035 | **+2.85pp** | 6.25% | kelly |
| eth 5m | 69–4 | **37.4** | rtds | 0.9516 | 0.9632 | 0.9580 | 0.9004 | **+1.16pp** | 3.33% | kelly |
| xrp 5m | 23–5 | 26.0 | rtds | 0.8311 | 0.8846 | 0.8559 | 0.7806 | **+5.36pp** | 3.68% | kelly |
| btc 5m | 55–3 | 17.4 | rtds | 0.9441 | 0.9424 | 0.9435 | 0.8258 | −0.16pp | 0.00% | measure |
| bnb 5m | 9–1 | 9.0 | binance | 0.9663 | 0.8889 | 0.9485 | 0.6916 | −7.74pp | 0.00% | **off** |
| btc 15m | 21–4 | 4.8 | binance | 0.9149 | 0.7895 | 0.8977 | 0.4955 | −12.54pp | 0.00% | **off** |
| eth 15m | 14–2 | 4.5 | binance | 0.9464 | 0.9444 | 0.9462 | 0.6578 | −0.20pp | 0.00% | measure |
| sol 15m | 2–1 | 0.5 | binance | 0.9761 | 0.5000 | 0.9683 | 0.0622 | −47.61pp | 0.00% | **off** |
| xrp 15m | 1–0 | 0.0 | — | — | — | — | — | — | 0.00% | no data |

**Three states, not one dial.** `kelly` = shrunk edge positive, gets a slice.
`measure` = raw edge within 2pp of break-even, gets a fixed 1%-of-bankroll
research size — small enough that being wrong costs nothing, large enough that
the record keeps growing (the ROADMAP's own "one small-size live night"
doctrine). `off` = win rate clearly below its own entry price; **no size makes a
negative edge positive**, so the answer is zero rather than "smaller".

**How much data each arm actually has**, unweighted, per era:

```
btc 5m    pre-brake 17-2   brakes 17-0   theta  9-0                 stream 12-1
eth 5m    pre-brake 14-1   brakes 10-1   theta 13-1   ws+scale 1-0  stream 31-1
sol 5m    pre-brake  5-1   brakes 18-0   theta 11-1                 stream 29-1
xrp 5m    pre-brake  0-2                                            stream 23-3
bnb 5m                                   theta  2-0   ws+scale 1-0  stream  6-1
btc 15m   pre-brake 10-1   brakes  7-2   theta  4-1                 (none)
eth 15m   pre-brake  2-1   brakes  7-1   theta  5-0                 (none)
sol 15m   pre-brake  1-0   brakes  1-1                              (none)
```

**The 15m arms have zero windows in the current regime.** They are parked at
$1/$1 with `min_fair 1.0` and `theta 1.0` (they cannot fire), so the stream era
contains nothing from them at all. Any 15m number in this document is
pre-`stream` evidence about a fleet that has since changed its book source, its
entry gate and its market-data feed.

### The 20:45Z law, and why it doesn't move a single number here

`e182e04 @ 1787517763` = **2026-08-23T20:42:43Z** bumped pmt-strategies to
`c8b0e53`: the **`decided_k = 1.25` law on 15m arms**, from
`analysis/carveout_ab.md` (the `banked_decided` carve-out is **+$89.70 at 5m and
−$649.15 at 15m**, so it gets capped at 15m and nowhere else). It has a
repo-citable moment and it will need a real row in `polymarket/eras.py` — this
study does not add one, because adding a boundary is that registry's own
append-only decision and nothing here depends on it. Two facts settle that:

* **It is 15m-scoped.** Every 5m arm's evidence and every 5m recommendation in
  this document is untouched by it, by construction.
* **The corpus contains zero windows under it.** The last graded window starts
  20:30Z, twelve minutes before the deploy, and the 15m arms are parked so no
  post-law 15m window exists either. `windows at or after the law: 0`.

What it *does* change is how much weight the 15m `off` verdicts deserve going
forward. btc 15m's −12.5pp and eth 15m's −0.2pp were both earned under an
uncapped carve-out that the A/B says was bleeding −$649 at exactly that duration.
So `off` is the right call **today** (no evidence in the current regime), but the
reason it is `off` rather than `never` is this law: when the operator unparks
them, they come back at a measurement size and re-earn a slice against a policy
that has been fixed in the place it was losing. Sizing them off pre-law numbers
in either direction would be grading a regime by its predecessor's record.

### Sensitivity — the era weighting is doing real work

| arm | weighted (used) n / edge | stream-only n / edge | all-time flat n / edge |
|---|---|---|---|
| sol 5m | 40.5 / **+2.9** | 30 / +2.7 | 66 / +3.9 |
| eth 5m | 37.4 / **+1.2** | 32 / +1.7 | 73 / +2.0 |
| xrp 5m | 26.0 / **+5.4** | 26 / +5.4 | 28 / **−1.0** |
| btc 5m | 17.4 / **−0.2** | 13 / **−2.1** | 58 / **+2.6** |
| bnb 5m | 9.0 / −7.7 | 7 / −11.9 | 10 / −6.4 |
| btc 15m | 4.8 / −12.5 | — | 25 / −5.4 |
| eth 15m | 4.5 / −0.2 | — | 16 / −6.9 |
| sol 15m | 0.5 / −47.6 | — | 3 / −25.0 |

Two rows are the whole argument for cutting at eras rather than summing history:

* **btc 5m flips sign.** All-time it looks like a +2.6pp arm on 58 windows. In
  the era it is actually trading it is **−2.1pp on 13**. The $1,000 ladder was
  set on the first number.
* **xrp 5m flips the other way.** All-time it is −1.0pp; strip the two pre-brake
  binance basis losses that are not evidence about its rtds present and it is
  +5.4pp on 26. Its record is short but it is the *cleanest* record on the fleet
  — one feed, one policy, no regime mixing.

### The honesty column nobody clears

**Not one arm's Wilson 90% one-sided lower bound clears its own break-even.**
sol 0.9035 vs 0.9344. eth 0.9004 vs 0.9516. xrp 0.7806 vs 0.8311. btc 0.8258 vs
0.9441. At 90% confidence this fleet has not demonstrated an edge anywhere. That
is the same verdict r2 reached from a different direction ("at n=57 the
estimation error is wider than the whole sizing decision"), and it is why the
recommended posture is measurement-heavy and why the size-increase gate in §5 is
written in Wilson units.

---

## 2. The fleet risk budget — derived twice, taken at the minimum

The arms are not five bets. `analysis/correlation_study.md` Result 1 measures
mean pairwise correlation of 5m settlement margin at **0.767** over 25,927
windows / 90 days; the intraday terminal-margin cut says 0.810; the trailing-15m
distribution over fired clips is p10 0.60 / p50 0.70 / p90 0.81. **All five
symbols settle the same direction 53.4% of the time against an independence null
of 6.3% — 8.5x.** 0.767 is the mid-range, least cherry-picked of the three.

### (a) Bottom-up — per-arm Kelly with a correlation haircut

For an equal-weight book of N assets with mean pairwise ρ, portfolio variance is
σ²(1+(N−1)ρ)/N, so the log-optimal **total** is

```
N_eff = N / (1 + (N-1)*rho)          = 4 / (1 + 3*0.767) = 1.21
budget = (N_eff / N) * sum_i(f_qtr_i) * bankroll
```

N is the count of arms that will actually be **concurrently exposed** (4), not
the eight in the arms table — an arm sized to zero contributes no correlated
exposure and counting it would punish the survivors for the sins of the retired.

```
sum of standalone quarter-Kelly   13.26%  =  $303   if the arms were independent
correlation haircut N_eff/N        0.303
-> $92
```

### (b) Top-down — the fleet as ONE bet, no correlation assumption at all

Group every graded window into **fleet slots** (maximal clusters of windows that
overlap in time — overlap and not equal-start, because a 15m window shares its
clock with three 5m windows). 102 slots. Each slot is one bet with its own
notional and its own P&L, so a slot where five arms lost together is simply one
bad bet at −100%, and the correlation is inside the data rather than in a
parameter.

Sizing uses the **empirical** Kelly — argmax of `E[log(1 + f·R)]` over the actual
slot returns — not a two-outcome (p, g, l) summary. The summary is a trap here:
averaging the loss leg turns "one slot in a hundred goes to −100%" into "losing
slots return −50%", and Kelly on that happily recommends betting **98.75%** of
the bankroll. The empirical form cannot make that mistake — one −100%
observation drives `log(1−f)` and pins `f` strictly below 1 forever.

```
102 slots (n_eff 74.4)   p=0.9415  g=+6.67%  l=50.52%   worst slot -100.0% (-$367)
full Kelly 58.25%  ->  quarter 14.56% = $333       (point estimate)
bootstrap p10, 1000 slot resamples: full 11.00%  ->  quarter 2.75% = $63
```

The bootstrap p10 is reported as the **uncertainty band, not a second
multiplier**: fractional Kelly is already the uncertainty discount the ROADMAP
mandates, and stacking a confidence haircut on it discounts the same doubt twice.
It is here to say how wide the estimate is (a 5x band on 102 slots), and it is
the number a *size increase* has to clear.

### The budget

```
FLEET BUDGET = min($92, $333) = $92 = 4.0% of bankroll
  binding: bottom-up (per-arm Kelly x correlation haircut)
  of which measurement line   $23   (1 arm x 1.0% of bankroll)
  of which Kelly line         $69
```

The live `fleet_undecided_cap` is **$500** (21.9% of bankroll) and was never
derived from anything. **$92 replaces it as a derived number**, and it is
now an output of the record rather than an input to it.

### Where live sizing sits on that scale

```
correlation-adjusted FULL Kelly total       $367
live per-window ceiling                   $2,400  = 6.5x FULL Kelly
slot-distribution full Kelly              $1,330  (no shrinkage, no haircut)
```

The two optima disagree by ~3.6x and that gap **is** the estimation error: the
per-arm view pays for shrinkage and a theoretical ρ, the slot view pays for
neither. What they do not disagree about is where live sizing sits. Expected log
growth per fleet slot, measured on the slot-return distribution itself:

| stake | $ | % of bankroll | growth per slot |
|---|---|---|---|
| **recommended** | 92 | 4.0% | **+0.1304%** |
| 2x recommended | 183 | 8.0% | +0.2541% |
| bottom-up full Kelly | 367 | 16.1% | +0.4811% |
| live fleet cap | 500 | 21.9% | +0.6282% |
| slot-view full Kelly | 1,330 | 58.2% | +1.1221% |
| **live arm sizes** | **2,400** | **105.1%** | **RUIN** |

`RUIN` is not a modelled tail. It means an observed slot return makes
`1 + f·R ≤ 0`: at $2,400 of concurrent exposure the 17:15Z five-arm slot — which
happened today, on this fleet, at these sizes — is larger than the account.

---

## 3. The allocation table

Bankroll: **$2,283.31** free USDC on the desktop wallet (`pmt balance`,
2026-08-23 17:00Z) plus **$182** on the EU L0 box, which gets its own budget
because a wipeout there cannot be funded from here without a bridge.

| arm | live size | live clip | **rec size** | **rec clip** | x | basis |
|---|---|---|---|---|---|---|
| btc 5m | 1,000 | 150 | **23** | **6** | 0.02x | measure — stream-era edge −2.1pp, n_eff 17.4 |
| eth 5m | 900 | 110 | **17** | **5** | 0.02x | kelly — +1.16pp, n_eff 37.4 (most evidence on the fleet) |
| sol 5m | 400 | 50 | **32** | **8** | 0.08x | kelly — +2.85pp, n_eff 40.5 (best edge x evidence) |
| xrp 5m | 100 | 10 | **19** | **5** | 0.19x | kelly — +5.36pp, n_eff 26.0, cleanest single-regime record |
| btc 15m | PARKED | — | **hold parked** | — | — | if restarted: **OFF**, −12.5pp, no stream-era data |
| eth 15m | PARKED | — | **hold parked** | — | — | if restarted: $23 measurement only, n_eff 4.5 |
| sol 15m | PARKED | — | **hold parked** | — | — | if restarted: **OFF**, 2W–1L is not a record |
| bnb 5m (EU) | 50 | 10 | **0 — OFF** | — | 0.00x | −7.74pp on n_eff 9.0; an entry price of 0.966 needs a 96.6% win rate and the arm has delivered 90.0% raw / 88.9% era-weighted |
| **TOTAL** | **2,400** | | **92** | | **0.04x** | |

Parked arms stay parked — this study sizes arms, it does not decide which
experiments are running. The "if restarted" column is what they would get.

**Clip rule** (`clip = max($5, min(size/4, $25))`), and neither binding reason is
Kelly:

* **The brakes only bite BETWEEN clips.** The 15c distrust brake, the 2c
  no-averaging-down brake and the window latch can each only refuse the *next*
  clip. A window that spends its whole budget in two fires cannot be braked at
  all, so a window must be at least 4 clips wide or the brake system is
  decorative. Live btc5 at 1000/150 is **6.7 clips**; live xrp5 at 100/10 is 10.
  Both fine. The rule mostly binds on the way down.
* **Book depth.** r2's depth scan (n=480 fires matched within 15s) puts
  top-of-book on our side at p25 $18.80 / median $49.48. A $25 clip is covered
  70% of the time; a **$100 clip is covered 31%** — which means live btc5's $150
  clip walks the book on nearly every fire, and every backtest that priced it at
  quoted ask is an upper bound.

Sanity check before deploying: confirm a $5 clip clears the CLOB minimum order
size (≈5 shares at these prices — it should, but check rather than discover it
in a log).

---

## 4. Effect on fleet risk

Measured correlated-loss events, from the graded record — the premise is not
hypothetical and it is **four events, not two**:

| when | arms lost | cost | on notional | legs |
|---|---|---|---|---|
| 08-23 01:45Z | 2 of 7 | −$298.62 | $1,153 | btc15 −370, xrp5 −43 |
| 08-23 04:00Z | 2 of 8 | −$366.98 | $1,075 | btc15 −265, sol15 −142 |
| 08-23 05:15Z | 2 of 3 | −$251.59 | $325 | eth5 −148, sol5 −106 |
| **08-23 17:15Z** | **5 of 5** | −$222.28 | $222 | btc5 −19, bnb5 −5, **eth5 −165**, sol5 −5, xrp5 −28 |

Slots with ≥2 arms armed, by number of losing arms: **0 → 53, 1 → 10, 2 → 3,
5 → 1**. Under independence at a ~6% per-arm loss rate, five-of-five is
`0.06^5 ≈ 1 in 1,300,000` slots. We saw it in 67. Independence is refuted by
about five orders of magnitude; the fleet must be sized as roughly one bet.

**The survival argument:**

| | live | recommended |
|---|---|---|
| concurrent exposure at risk in one correlated event | $2,400 | **$92** |
| as a fraction of bankroll | **105.1%** | **4.0%** |
| four such events back to back | account gone on the first | **−15.1%** |
| expected log growth per slot at that stake | **RUIN** | +0.13% |
| historical peak simultaneous notional actually reached | $995 (43.6% of bankroll) | — |

At $92 the fleet survives an unbroken run of correlated wipeouts long enough for
the edge to matter; at $2,400 it does not survive the first one, and the only
reason it has not already happened is that peak concurrency has so far reached
$995 rather than the $2,400 the arms are authorised to spend.

**A caveat that matters more than the cap:** `fleet_undecided_cap` does **not**
bind banked-decided fires — `undecided_committed` returns 0 once
`last_banked_decided` is set, and the correlation study measured **468 fires /
$11,454 of intended notional (39%) fired while banked_decided, structurally
invisible to the cap**. During the 17:15Z event `fleet_room` never fell below
$470 of $500, and eth alone was **$165 of the $222** (74% of the damage), fired
in `safe` mode on a banked_decided certificate. **The per-arm `--size` is therefore the only hard
ceiling on a correlated event.** Setting the fleet cap to $92 without cutting the
arm sizes would change nothing about the event that actually happened.

---

## 5. Refresh cadence — when sizes must be re-derived

Re-run `analysis/sizing_method.py --refresh` and re-deploy when **any** of:

1. **A new era is appended to `polymarket/eras.py`** — i.e. a policy deploy. The
   weights change by construction and every arm's `n_eff` moves. *Live example:*
   the 20:45Z `decided_k` law is a pending boundary with no windows behind it
   yet; the first 15m window traded under it triggers this rule.
2. **An arm's `--feed` changes.** A binance→rtds migration retroactively halves
   the weight of that arm's whole prior record.
3. **Any arm's `n_eff` grows by ≥50%** since the last derivation. Below that the
   K=30 prior dominates and nothing moves; above it the shrinkage does.
4. **Bankroll moves ±25%** — every size is a fraction of it.
5. **Any multi-arm correlated loss event** (≥2 arms losing overlapping windows).
   The slot distribution just gained its single most informative observation and
   the top-down budget is stale until it is folded in.
6. **Weekly floor**, even with none of the above. ρ is non-stationary (the study
   measured 0.612 in calm vol quartiles, 0.803 in wild), and a 48h-old
   correlation is not this week's.

**The gate for a size INCREASE, specifically** — the calibration gate restated in
sizing units, because the ROADMAP's "no size increases without an R2 calibration
pass" needs a number the driver can check:

> An arm may only be sized **up** when its one-sided **90% Wilson lower bound
> clears its own break-even price** (`p_w90 > c`) on windows from the current era
> and the current feed.

**Today, no arm clears it.** The driver prints how far each one is, holding its
current win rate fixed — the requirement scales as 1/edge², so the arms are not
uniformly "a bit short", they are orders of magnitude apart:

| arm | n_eff now | n_eff needed | more windows | ≈ hours of running |
|---|---|---|---|---|
| **xrp 5m** | 26.0 | **81** | +55 | ~10h |
| **sol 5m** | 40.5 | **124** | +84 | ~15h |
| eth 5m | 37.4 | 560 | +523 | ~4 days |
| btc 5m | 17.4 | **never** | — | win rate is at/below break-even |
| bnb 5m, all 15m arms | — | **never** | — | same |

(Hours assume the stream era's observed rate of ~30 windows per arm per 5.5h.)

"Never" is the important word: an arm whose win rate sits at or below its own
entry price does not open the gate by accumulating windows. More data makes the
bound *tighter around a number that is still under break-even*. btc 5m is not
slow, it is — on the era and feed it is actually trading — wrong, and the way to
change that is a cheaper entry (R3's max-price cap, the maker path) or a better
pick, not patience and not size.

---

## 6. The commands

Recommendations only — the operator deploys. Nothing in this study touched a live
arm, a config, or `~/.pmt`.

```bash
# desktop fleet
pmt crypto arm <btc-updown-5m url> --size 23 --clip 6 --feed rtds    --theta 0.3
pmt crypto arm <eth-updown-5m url> --size 17 --clip 5 --feed rtds    --theta 0.3
pmt crypto arm <sol-updown-5m url> --size 32 --clip 8 --feed binance --theta 0.3
pmt crypto arm <xrp-updown-5m url> --size 19 --clip 5 --feed rtds    --theta 0.3
pmt crypto fleet --cap 92

# btc 15m / eth 15m / sol 15m: leave parked (operator's A/B) — no command
# bnb 5m on the EU box: OFF. An average entry of 0.966 needs a 96.6% win rate;
#   the arm has delivered 90.0% over 10 windows. Disarm rather than shrink.
```

Everything else about the arms is unchanged: `--theta 0.3`, the measured
per-symbol basis guards, the three brakes, the latch, `min_fair 0.97`,
`max_price 0.985`. **This study moves size and clip only.** It has no opinion on
the decision core and deliberately does not touch a gate that a replay A/B has
already won.

### If the operator wants more throughput than $92

The honest ladder, rather than a negotiation. Every rung is a real Kelly multiple
on the same evidence:

| posture | fleet total | multiple of full Kelly | growth/slot | one wipeout costs |
|---|---|---|---|---|
| quarter-Kelly (**recommended**) | $92 | 0.25x | +0.13% | 4.0% of bankroll |
| half-Kelly | $183 | 0.50x | +0.25% | 8.0% |
| full Kelly | $367 | 1.00x | +0.48% | 16.1% |
| today's fleet cap | $500 | 1.36x | +0.63% | 21.9% |
| **today's arm sizes** | **$2,400** | **6.5x** | **RUIN** | **105.1%** |

Half-Kelly is a defensible operator choice and doubles the throughput for double
the single-event drawdown. Anything at or above the current $500 cap is a
deliberate bet that the measured ρ is too high and the measured win rates too
low — and the Wilson column in §1 says the error runs the *other* way.

---

## 7. What this study does NOT claim

* **That the edges are real.** No arm clears a 90% lower bound. Three arms have a
  positive shrunk edge and that is the best available estimate, not a
  demonstration. The recommended sizes are what you stake on a *best estimate you
  do not yet trust*, which is exactly what quarter-Kelly is for.
* **That $92 is optimal.** It is the minimum of two derivations that disagree by
  3.6x, and the bootstrap says the top-down alone spans $63–$333. The number that
  is robust is the *direction and magnitude* of the gap to $2,400.
* **That the 15m arms are bad.** They have no evidence in the current regime
  because they are parked, and every number they do have predates the 20:45Z
  `decided_k` law that was written specifically to stop the bleed at their
  duration. `off` for btc15/sol15 is a verdict on a pre-brake, pre-theta,
  REST-book, pre-law record; if they come back, they come back at a measurement
  size and re-earn a slice like everything else.
* **That correlation is the whole risk.** ρ sets the haircut; the concentration
  finding underneath it is sharper — post-theta, windows where ≥4 arms took the
  same side lost **35.7%** of the time against a 6.5% break-even loss rate. Sizing
  bounds that damage. It does not stop the fleet from walking into it, which is
  R8's job.
