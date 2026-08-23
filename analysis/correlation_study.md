# Cross-symbol correlation — the 17:15Z five-arm event (2026-08-23)

**Verdict: the correlation is real and large, and it is not the bug. The five
arms did not make five bets that happened to agree; they made one bet, and the
bet was on a settlement rule the market does not use. No concentration cap in
this study clears the bar — the only two CI-significant rows in the whole
policy table are NEGATIVE, and every positive point estimate is the incident
window wearing a policy's name.**

Trigger: window epoch `1787505300` (2026-08-23 17:15:00Z, 5m). All five 5m arms
(btc/eth/sol/xrp/bnb) fired DOWN on banked evidence; a macro impulse in the
final ~90s carried every one of them to an UP settlement. ~$230 gone at once,
107-win streak over.

Driver: `analysis/correlation_study.py`. Full output:
`analysis/correlation_study.txt`. Read-only over `~/.pmt`; no engine, no orders,
no network.

---

## Method, and the charges taken

**Corpus.** Three instruments, three spans, stated once because L33 makes the
filenames meaningless on their own:

| instrument | span | size |
|---|---|---|
| `updown-tape.jsonl` | 1787436137 → 1787507705 (19.9h) | 912 slugs, 267 fired, 1163 clips |
| `outcomes.jsonl` (L36-clean) | epochs 1787436000 → 1787501100 | 734 graded (222 wallet / 376 book / 136 chainlink) |
| `rtds-20260823.jsonl` | 08:28:54 → 17:39:36Z (9.18h) | 8 symbols × 3 topics, ~0.9Hz |
| `klines-1m-*.jsonl` | 2026-05-25 → 2026-08-23 (90.0d) | 6 symbols, 25,927 complete 5m windows |

L33 bit while this was being written: the live tape grew from 889 to 912 slugs
between the first run of the driver and the last. The tables below are all from
the final run, and none of the new slugs is in the policy population (they post-
date the incident and are ungraded). A study that needs to be quoted rather than
re-run should point `TAPE` at a frozen copy first.

The tape **stops at t=1787505516, elapsed_frac 0.72 of the incident window** —
the engine stopped writing before the window closed. The 80% and 100% evals the
operator watched live are not on it. Everything about the last 84 seconds here
comes from the settlement stream, not from the engine's own record.

**Fill truth.** A fire is *intent*. Filled notional is the increment in the
engine's `committed` tracker between a fire and the next observation of
`committed` on that slug, clamped at what the clip asked for. This is
`r7_fleet_cap.py`'s convention and it exists because fire sizes lie: that study
found a $2,500 xrp fire at ask 0.01 that never moved `committed` past $106.58.
A clip that never filled is worth zero to every policy here and cannot flatter
one. Reconstructed against the tracker's own per-window peak: n=240, median
delta −$0.17.

**Valuation.** Hold-to-settlement, gross of fees, identical on every variant, so
only DELTAS are read (`hybrid_ab.md`'s rule). The live engine exits; this does
not. Absolute P&L here is not wallet truth.

**Bootstrap.** Percentile CI95, 10,000 resamples, unit = **EPOCH**, not window
and not clip. Resampling windows would count one five-arm macro event as five
independent draws, which is the exact error the study is about.

**Independence nulls** are permutations of the recorded series (each symbol
shuffled in time independently, preserving its own up/down mix), never an
assumed binomial.

**Charges not taken, stated plainly.** The fire schedule is fixed; no policy is
credited with clips the tape does not contain, and freed budget is never
redeployed. A blocked clip does not change the book it would have traded in.

---

## Result 0 — the settlement rule the arms price is not the settlement rule

This was not the question asked, and it is the answer.

Graded against the L36-clean corpus over the RTDS span:

| rule | wallet | book (unselected) | ALL |
|---|---|---|---|
| `terminal` (settlement-width TWAP, close vs open) | 73/73 100% | 282/289 **97.6%** | 98.1% |
| `terminal_t60` | 73/73 100% | 288/289 **99.7%** | 99.7% |
| `range_avg` — **what every live arm prices** | 75/75 100% | 262/296 **88.5%** | 90.8% |
| `ck_close_open` | 70/73 95.9% | 265/289 91.7% | 92.5% |

`range_avg` is the whole window's average against its open — a *momentum proxy*.
`terminal` is the settlement-width TWAP at the close against the same TWAP at the
open (`updown_model.rs::settle_tw_secs`: 30s for a 5m market, 60s above it;
`outcomes.py::ck_settlement_width_s` mirrors it). Every live arm carries
`settle_rule: "range_avg"` in `arms-state.json`; `updown.rs::d_settle_rule`
returns it, and no pmtrader path sets anything else.

The `wallet` column showing range_avg at 100% is **selection, not vindication** —
it contains only windows the gates chose to trade. `book` is the unselected
control, and there range_avg is wrong on **1 window in 8.7**.

**35 of 362 in-span graded windows (9.7%) have range_avg and the terminal rule
naming different winners.** On those 35, the terminal rule is right 31 times and
range_avg 4. And the disagreements cluster: 12 epochs with one symbol
disagreeing, 8 with two, 1 with three, **1 with four**.

### The incident, priced under both rules

| sym | t30@open | t30@close | range_avg | terminal | fired / settled |
|---|---|---|---|---|---|
| btc | 77278.82 | 77348.32 | **−2.74bp** | **+8.99bp** | DOWN / UP |
| eth | 2447.30 | 2448.57 | **−12.59bp** | **+5.19bp** | DOWN / UP |
| sol | 95.3682 | 95.4460 | **−9.51bp** | **+8.16bp** | DOWN / UP |
| xrp | 1.5179 | 1.5195 | **−20.81bp** | **+10.55bp** | DOWN / UP |
| bnb | 699.541 | 699.931 | **−3.16bp** | **+5.57bp** | DOWN / UP |

The terminal column reproduces the operator's reported +5.2 to +10.6bp settlement
margins to the second decimal. range_avg said DOWN on all five and was wrong on
all five.

Controls that have never been armed, on the same stream, same window: **doge
range_avg −7.06 / terminal +34.17. hype −3.25 / +13.57. zec −15.55 / +14.69.**
Eight for eight. This is one macro impulse crossing one model error, not five
windows going wrong.

**The fleet was not wrong five times. It was wrong once — about which prices
decide a window — and that one error was worth five positions because the
impulse was macro.**

---

## Result 1 — Q1: the correlation, and it is enormous

**90 days, 25,927 complete 5m windows, 6 symbols, Pearson on settlement margin
(bp):**

|  | btc | eth | sol | xrp | bnb | doge |
|---|---|---|---|---|---|---|
| btc | 1.00 | 0.87 | 0.82 | 0.71 | 0.75 | 0.76 |
| eth | 0.87 | 1.00 | 0.85 | 0.72 | 0.75 | 0.79 |
| sol | 0.82 | 0.85 | 1.00 | 0.77 | 0.75 | 0.82 |
| xrp | 0.71 | 0.72 | 0.77 | 1.00 | 0.67 | 0.79 |
| bnb | 0.75 | 0.75 | 0.75 | 0.67 | 1.00 | 0.73 |
| doge | 0.76 | 0.79 | 0.82 | 0.79 | 0.73 | 1.00 |

Mean pairwise |r| across the tradeable fleet: **0.767**. Mean pairwise sign
agreement: **0.771** (0.5 = independent).

**How often the fleet agrees, against a permutation null:**

| max symbols on one side | observed | obs % | independence null | ratio |
|---|---|---|---|---|
| 3 of 5 | 5,469 | 21.1% | 16,182 | 0.34× |
| 4 of 5 | 6,610 | 25.5% | 8,113 | 0.81× |
| **5 of 5** | **13,848** | **53.4%** | **1,632 (6.3%)** | **8.49×** |
| ≥4 of 5 | 20,458 | 78.9% | 9,745 (37.6%) | 2.10× |

**All five symbols settle the same direction 53.4% of the time. Independence
would say 6.3%.** Five 94% bets on the same side are, in settlement terms,
roughly one bet — this is the quantitative answer to the operator's question.

### Conditional structure

**vs |margin| — near-flat windows are LESS correlated, not more:**

| median cross-sectional \|bp\| | n | mean r | sign agr | ≥4 same | 5 same |
|---|---|---|---|---|---|
| 0–2 | 3,310 | 0.080 | 0.542 | 46.7% | 12.1% |
| 2–5 | 7,797 | 0.243 | 0.643 | 65.0% | 28.3% |
| 5–10 | 7,182 | 0.512 | 0.821 | 88.4% | 61.1% |
| 10–20 | 5,059 | 0.724 | 0.940 | 97.3% | 86.5% |
| 20–40 | 2,057 | 0.839 | 0.981 | 99.7% | 95.4% |
| 40+ | 522 | 0.889 | 0.995 | 100.0% | 98.9% |

A small common move is swamped by each symbol's own noise, so near-flat signs
scatter. **Concentration risk rises with the SIZE of the common move.** The basis
guard is looking at the opposite end of this axis.

**vs volatility regime** (trailing 1h stdev of btc's 5m margins): monotone but
mild — mean r 0.612 (calm Q1) → 0.803 (wild Q4); 5-of-5 41.8% → 62.2%.

**vs hour of day:** weak. Least correlated 10:00Z (r=0.676), most 22:00Z
(r=0.824). The incident hour (17:00Z) is unremarkable at r=0.785. **Time of day
is not a lever.**

### Intraday, off the settlement stream (8 symbols, 103 windows)

Fleet-internal mean |r| = **0.816**. Fleet vs never-armed (hype/zec) = **0.508**.
Even assets the fleet has never touched carry half the fleet's correlation. The
common factor is the market, not the symbol selection.

---

## Result 2 — Q2: concentration episodes, and how thin they are

**Same-side concentration on the tape, 5m:**

| same-side arms | epochs | windows | wins | hit rate | P&L | $filled | $/window |
|---|---|---|---|---|---|---|---|
| 1 | 72 | 74 | 70 | 94.6% | −306.07 | 3,345 | −4.14 |
| 2 | 38 | 74 | 73 | 98.6% | +274.81 | 4,731 | +3.71 |
| 3 | 18 | 54 | 51 | 94.4% | −31.06 | 3,894 | −0.58 |
| 4 | **2** | 8 | 7 | 87.5% | +107.97 | 1,015 | +13.50 |
| 5 | **2** | 10 | 5 | 50.0% | −209.05 | 411 | −20.90 |

**Count the episodes, not the windows.** In 19.3h of tape there are **two**
four-arm epochs and **two** five-arm epochs. One five-arm epoch won all five
(1787499300); one lost all five (the incident). The 50% hit rate in that row is
`n=2`. No threshold may be fitted to it — this is the L37 shape, caught before
it became a knob.

### Adverse selection (post-theta 5m, the decision basis)

| population | windows | hit rate | P&L | $/window | worst window |
|---|---|---|---|---|---|
| solo (1 arm this side) | 43 | 100.0% | +117.16 | +2.72 | +0.00 |
| 2 same side | 38 | 100.0% | +180.01 | +4.74 | +0.00 |
| 3 same side | 33 | 93.9% | −117.24 | −3.55 | −114.94 |
| ≥4 same side | 14 | 64.3% | −180.93 | −12.92 | −173.94 |

Solo 100% vs concentrated 91.8% — a −8.2pp gap, **permutation p = 0.098, not
distinguishable from noise at this n.** The hit rate is the wrong statistic
anyway (L27): the money runs monotonically from +$2.72/window solo to
−$12.92/window at ≥4 same side, and only the concentrated rows have a worst-case
worth naming.

**Answer to "is concentrated-fire hit rate LOWER than solo": directionally yes,
monotonically, and not significantly.**

### The break-even that decides whether a cap could ever pay

Buy $1 at price p. Win → +$(1−p)/p. Lose → −$1. Refusing is +EV exactly when the
loss probability q exceeds **q\* = 1 − p**.

Fill-weighted entry price on post-theta 5m clips: **p = 0.934 → q\* = 6.6%.**

| population | windows | losses | loss rate | vs q* |
|---|---|---|---|---|
| 1 arm same side | 43 | 0 | 0.0% | below |
| 2 arms | 38 | 0 | 0.0% | below |
| 3 arms | 33 | 2 | 6.1% | below (at it) |
| **≥4 arms** | 14 | 5 | **35.7%** | **ABOVE, 5.4×** |

The gradient is monotone and crosses break-even **exactly at N=4**. A cap at
N≤3 would destroy value; a cap at N=4 has the right shape. What is missing is n:
that row is three epochs, one of which is the incident the policy was designed
after. *"A cap would pay if this loss rate is real"* and *"the loss rate is
real"* are different claims, and only the second licenses a deploy.

### Case study: the 17:15Z window

Fires, all DOWN, from the tape (truncated at elapsed 0.72):

```
bnb  t+ 94  efrac 0.31  ask 0.87   6sh  spec
bnb  t+106  efrac 0.35  ask 0.88   6sh  spec
btc  t+111  efrac 0.37  ask 0.91  21sh  spec
sol  t+120  efrac 0.40  ask 0.91   5sh  spec
eth  t+128  efrac 0.43  ask 0.91  15sh  spec
eth  t+150  efrac 0.50  ask 0.95  19sh  SAFE   <- banked_decided unlock
eth  t+162  efrac 0.54  ask 0.90  15sh  SAFE
xrp  t+167  efrac 0.56  ask 0.89   5sh  spec
eth  t+174  efrac 0.58  ask 0.86  50sh  SAFE
xrp  t+182  efrac 0.61  ask 0.93  10sh  SAFE
eth  t+186  efrac 0.62  ask 0.93  50sh  SAFE
eth  t+198  efrac 0.66  ask 0.97  24sh  SAFE
xrp  t+200  efrac 0.67  ask 0.97   5sh  SAFE
eth  t+210  efrac 0.70  ask 0.92  10sh  SAFE
xrp  t+212  efrac 0.71  ask 0.90  11sh  SAFE
```

Last eval each arm wrote:

| sym | efrac | margin_bp | banked_bp | cushion_bp | safety | banked_decided | fleet_room |
|---|---|---|---|---|---|---|---|
| bnb | 0.39 | −8.20 | −3.80 | 12.58 | 0.30 | False | 471 |
| btc | 0.57 | −6.23 | −4.70 | 7.93 | 0.59 | False | 472 |
| **eth** | 0.72 | −14.65 | −11.80 | 8.04 | **1.47** | **True** | 483 |
| sol | 0.69 | −11.67 | −9.87 | 12.67 | 0.78 | False | 478 |
| **xrp** | 0.72 | −27.37 | −22.74 | 17.48 | **1.30** | **True** | 483 |

Reconstructed realised: eth $173.94 filled / −$173.94, xrp $17.49 / −$17.49,
btc $19.14 / −$19.14, sol $4.56, bnb $5.28. **Total −$220.41** on 15 recorded
clips, against the operator's ~$230 (the tape truncates, so late clips are
missing). **79% of the damage is eth alone, and eth's damage is the clips fired
in `safe` mode on a `banked_decided` certificate.**

`banked_decided: true` is the engine asserting that no remaining path can
overturn the elapsed average. That assertion was **true about the average and
irrelevant to the settlement**, which reads only the final 30 seconds of a 5m
window. Sound arithmetic about the wrong number.

---

## Result 3 — the fleet cap could not have stopped this at any cap value

The cap was armed. It is not `PMENGINE_MAX_TOTAL_EXPOSURE` — it is
`fleet_undecided_cap`, reconstructed from the tape at **$350** (2,718 ticks),
raised to **$500** later. During the incident `fleet_room` never fell below
**$470 of $500**.

`updown.rs:1405-1407`:

```rust
let fleet_bound = if m.banked_decided { INFINITY }
                  else { fleet_room.max(0.0) };
```

and `ArmState::undecided_committed` (`updown.rs:568-579`) returns `0.0` the
moment `last_banked_decided` is set. **A banked-decided arm neither counts
against the cap nor can be capped by it.** eth had been banked_decided since
elapsed 0.51 and was carrying **zero** against the cap while it grew to $169.

Across the whole tape, **464 fires / $11,240 of intended notional (38%) were
fired while banked_decided** — structurally invisible to the cap.

So "the fleet cap never addressed same-side concentration" understates it.
**The cap could not have addressed this event at any cap value, because the
positions had exempted themselves — on a certificate computed under a rule that
is wrong one window in nine.**

---

## Result 4 — Q3: the impulse class, and there is no leader

**Impulse catalog** (≥4 of 8 stream symbols moving ≥X bp the same way in 30s,
non-overlapping):

| threshold | events | per hour | in final 90s | → produced a ≥4-symbol rule disagreement |
|---|---|---|---|---|
| ≥3bp | 710 | 77.4 | 213 | 4 |
| ≥5bp | 435 | 47.4 | 123 | 3 |
| ≥8bp | 216 | 23.5 | 63 | 3 |
| ≥12bp | 92 | 10.0 | 25 | 1 |
| ≥20bp | 23 | 2.5 | 5 | 0 |

**Synchronised final-90s impulses are common — 123/9.18h at ≥5bp, about 13 an
hour. Five-arm traps are not.** The trap needs a conjunction: an impulse, AND
range_avg already pointing the other way, AND enough banked mass to have passed
theta on several arms at once. Gating on the impulse alone would gate constantly.

### Lead-lag: no leader exists

1Hz contemporaneous correlation is already substantial (btc–eth 0.68, btc–sol
0.65; hype/zec ~0.28). Cross-correlation of 10s returns, r(btc_{t−k}, alt_t):

| alt | k=0 | k=1 | k=2 | k=3 | k=5 | k=10 | peak |
|---|---|---|---|---|---|---|---|
| eth | **0.743** | 0.702 | 0.640 | 0.572 | 0.462 | 0.161 | k=0 |
| sol | **0.758** | 0.710 | 0.639 | 0.565 | 0.440 | 0.112 | k=0 |
| xrp | **0.625** | 0.581 | 0.523 | 0.463 | 0.364 | 0.128 | k=0 |
| bnb | **0.752** | 0.719 | 0.657 | 0.588 | 0.460 | 0.130 | k=0 |
| doge | **0.675** | 0.630 | 0.565 | 0.498 | 0.389 | 0.129 | k=0 |

The symmetric direction, r(alt_{t−k}, btc_t), peaks at k=0 too, with a
near-mirror decay. **Every pair peaks at zero lag in both directions.** That is
a common factor hitting every oracle in the same second, not a lead.

Warning-window measurement on final-90s ≥5bp impulses — seconds by which btc
crosses ±3bp before each alt does:

| alt | n | median | mean | p25 | p75 |
|---|---|---|---|---|---|
| eth | 84 | **−1.0s** | −4.1s | −6.0s | 0.0s |
| sol | 85 | **−2.0s** | −6.0s | −9.0s | 0.0s |
| xrp | 84 | **−4.0s** | −8.9s | −17.0s | 0.0s |
| bnb | 79 | **0.0s** | −1.6s | −4.0s | 0.0s |
| doge | 86 | **−5.0s** | −7.7s | −15.0s | 0.0s |

Every median is at or below zero. **The alts cross first as often as btc does.
The warning window is zero or negative. A leader-veto has nothing to lead on**,
and policy (c) below is therefore not a leader-veto — at best it is a
contemporaneous momentum veto using btc as a proxy for the factor.

---

## Result 5 — Q4: the policy table

Bootstrap unit = epoch. `top` = |largest single epoch| ÷ |net delta|; above 100%
means one epoch exceeds the whole result and everything else nets against it.

### post-theta (128 graded 5m windows, $6,367 filled)

| policy | eps ch | $refused | net delta | CI95 | top |
|---|---|---|---|---|---|
| **(a)** same-side cap max 1 | 33 | 2,711 | +163.59 | [−211, +712] | 132% |
| **(a)** same-side cap max 2 | 14 | 766 | +142.60 | [−116, +570] | 137% |
| **(a)** same-side cap max 3 | 3 | 300 | +188.10 | [−9.6, +574] | **102%** |
| **(a)** same-side cap max 4 | 2 | 67 | +15.41 | [−6.2, +52] | 113% |
| **(a$)** $100/epoch | 19 | 2,976 | +100.17 | [−217, +532] | 140% |
| **(a$)** $300/epoch | 5 | 509 | −0.00 | [−54, +66] | — |
| **(b)** $100 cap, banked EXEMPT (as built) | 4 | 248 | **−28.65** | **[−68.2, −1.70]** | 48% |
| **(b)** $100 cap, no exemption | 19 | 2,976 | +100.17 | [−217, +532] | 140% |
| **(b)** $350 cap, no exemption | 1 | 237 | −14.34 | [−43, +0] | 100% |
| **(c)** btc veto T=20s Y=4bp | 9 | 276 | +38.05 | [−96, +223] | 203% |
| **(c)** btc veto T=30s Y=4bp | 11 | 497 | +50.46 | [−142, +332] | 243% |
| **(c)** btc veto T=60s Y=2bp | 22 | 1,679 | −7.60 | [−310, +417] | 2337% |
| **(d)** corr≥0.50 → clip ×0 | 51 | 4,648 | −138.23 | [−509, +393] | 159% |
| **(d)** corr≥0.85 → clip ×0 | 3 | 48 | +28.32 | [−1.5, +87] | 102% |
| **(e)** terminal-margin gate ≥0bp | 2 | 179 | −63.47 | [−181, +0] | 85% |
| **(e)** terminal-margin gate ≥5bp | 7 | 388 | **−103.16** | **[−254, −5.0]** | 59% |
| **(f)** no banked_decided-only unlock | 4 | 275 | **+71.33** | [−17.7, +234] | 110% |
| **(f2)** banked-only unlock at half size | 4 | 138 | +35.66 | [−8.9, +117] | 110% |

**Not one positive row's CI excludes zero. The only two CI-significant rows in
the entire table are negative:** the same-side cap *as the engine would actually
apply it* (with the banked exemption) at **−$28.65 [−68, −1.70]**, and the
terminal-margin gate at 5bp at **−$103.16 [−254, −5.0]**.

### The attribution, which is the finding

```
(a) same-side cap: max 3 arms   net +188.10, top epoch = 102% of it
    epoch 1787505300   stream   +191.44   <<< THE INCIDENT
    epoch 1787499300   stream     -3.18
    epoch 1787486700   stream     -0.15

(a) same-side cap: max 2 arms   net +142.60, top epoch = 137% of it
    epoch 1787505300   stream   +195.99   <<< THE INCIDENT
    epoch 1787493900   stream    -35.08
    epoch 1787462400   theta      -4.66
    ...

(f) no banked_decided-only unlock  [stream era]  net +77.74, top = 101%
    epoch 1787505300   stream    +78.40   <<< THE INCIDENT
    epoch 1787504700   stream     -0.66
```

**Every positive same-side-cap number in this study is the incident window and
nothing else.** The cap makes money on exactly the window it was designed after
and loses money on every other window it touches. That is not a policy; it is a
description of one afternoon.

### Why (e), the "obvious" fix, is CI-significantly negative

Switching the *gate* to the terminal rule fails, and the failure is informative.
Mid-window the terminal margin is just "is the oracle above the window's opening
TWAP right now" — a single noisy print. The whole value of range_avg's banked
evidence is that it is an **average**, and averages are what a gate can stand on.
The terminal rule is right about what settles and nearly worthless as an early
signal, which is exactly `hybrid_ab.md`'s "the lock is invisible until the wire".

**You cannot simply switch to the terminal rule and keep the volume.** That is
why range_avg is live despite being wrong one window in nine, and it means this
incident is a *structural cost of the strategy*, not a bug with a cheap fix.

### (f) is the one candidate with the right mechanism

`mode == "safe"` means the arm's full budget was unlocked, for one of two
reasons: the window is late (`rem <= late_rem_s`, 120s), or it is
`banked_decided`. Under `settle_rule="hybrid"` the second door closes until the
settlement TWAP starts locking (`terminal_lock`: banked == 0 while `rem > tw`).
Blocking exactly the clips unlocked *only* by a range_avg banked_decided
certificate is the tape's view of what hybrid's cushion buys.

It is +$71.33 [−17.7, +234] post-theta, +$77.74 [−2.0, +235] stream era, and it
targets the mechanism precisely — eth's three biggest clips in the incident
(19+15+50 shares at rem 150/138/126) were banked_decided-only unlocks. **But
101% of its stream-era delta is the incident.** Point-positive, mechanism-
correct, evidence-starved.

---

## Verdict — ranked

**1. Nothing in this study ships as a deployed knob.** No policy clears zero.
Every positive point estimate is one window; the only CI-significant results are
negative. The same conclusion `retry_pricing_study.md` reached, for the same
reason, on the same kind of evidence.

**2. The finding is Result 0, and it is not a knob — it is a mismatch.** The
live arms price `range_avg`; the market settles `terminal`. They disagree on
9.7% of windows, the disagreements cluster across symbols because their cause is
macro, and `banked_decided` — the certificate that both passes the theta gate and
*exempts a position from the fleet cap* — is arithmetic over the wrong one. This
belongs in LESSONS regardless of what ships.

**3. The fleet cap's `banked_decided` exemption is a real structural hole and
the cheapest thing to look at next.** 38% of all fired notional was invisible to
the cap. Removing the exemption is a **tightening** and would be deployable on
that basis — except that measured here it is not worth anything ((b) "no
exemption" rows are +$100 [−217, +532] and −$14 [−43, +0], i.e. noise), and
leaving the exemption in place while capping is **CI-significantly negative**.
So: do not arm the cap against same-side piles on this evidence. Do note in the
code that the exemption's soundness is inherited entirely from `settle_rule`.

**4. Re-run the hybrid A/B. This is the recommendation with the most behind it.**
`hybrid_ab.md` parked hybrid because "the minute-grain per_min feed sees the
forming 60s settlement TWAP as ONE sample — the lock is invisible until the
wire. **Sub-minute RTDS feed is the unlock.**" That feed shipped on 2026-08-23 at
11:29:30Z (era `stream`, `e296336`). **The stated blocker no longer exists.**
Result 0 is the evidence that it matters and (f) is the tape's estimate of the
size of the prize. This **needs a replay A/B** — it replaces a gate's arithmetic
— run as `pmengine replay --mode full` over a frozen corpus, `settle_rule`
range_avg vs hybrid, bootstrap over epochs, the `r7_fleet_ab.py` shape.

**5. A same-side concentration cap at N=4 is the right shape and the wrong time.**
The break-even arithmetic (q\* = 6.6% vs 35.7% observed at ≥4 same side) says a
cap would pay if that loss rate is real. Three epochs cannot establish that it
is. **Do not deploy. Do instrument**: the tape already carries everything needed
to count same-side piles per epoch, so the count can accumulate at zero risk
until the row has an n worth reading. Revisit at ≥20 four-arm epochs.

**6. Kill the leader-veto idea.** There is no leader. Every cross-correlation
peaks at k=0 in both directions and every measured warning window is at or below
zero seconds. Policy (c)'s positive point estimates (+$38 to +$50) are 200–2300%
attributable to single epochs and have no mechanism behind them. Do not build it.

**7. Correlation-regime clip scaling (d) is refused on its own numbers.** The
trailing fleet correlation over fired clips runs p10 0.60 / p50 0.70 / p90 0.81 —
there is no "high correlation regime" to detect because the fleet is *always* in
one. Thresholds low enough to fire are a size cut in a correlation costume
(−$138 at ≥0.50); thresholds high enough to be selective touch 3 epochs.

**8. One open question this study created.** `terminal_t60` grades the corpus at
99.7% while the width-correct `terminal` (30s for 5m, per
`updown_model.rs::settle_tw_secs`) grades it at 97.6% — 7 misses vs 1 over 362
windows. Either the recorder's `twap_thirty` topic is noisier than
`twap_sixty` at the sampling instant, or **5m markets do not settle on a 30s
TWAP and the engine's assumed width is wrong**. That is worth an hour with the
Chainlink round corpus before the hybrid A/B, because hybrid's `terminal_lock`
is parameterised on exactly that width.

---

## Draft LESSONS entry — do NOT commit here

`docs/LESSONS.md` is a symlink into the private `pmt-alpha` repo. Draft only.

> ### <a id="L39"></a>L39 — Five arms, one bet: `banked_decided` certified the wrong average
>
> **2026-08-23.** All five 5m arms fired DOWN inside one window on banked
> evidence; a macro impulse in the final 90s settled all five UP. ~$230 at once,
> ending a 107-win streak. The fleet cap was armed at $500 and never fell below
> $470 of it.
>
> Two things had to be true at once, and both were structural. First, the live
> arms price `settle_rule = range_avg` — the whole window's average — while the
> market settles on the settlement-width TWAP at the close. Measured against the
> unselected (book-graded) corpus those rules name different winners on **9.7% of
> windows**, and range_avg is the one that is wrong. Second, `banked_decided` is
> arithmetic over range_avg, and a banked-decided arm is *exempt* from the fleet
> cap by construction (`fleet_bound = if m.banked_decided { INFINITY }`;
> `undecided_committed` returns 0.0). 38% of all fired notional was therefore
> invisible to the cap. The largest position in the event had been banked_decided
> since elapsed 0.51 and carried **zero** against the cap while it grew to $169.
> (`fleet_room` on that arm's last eval: 483 of 500.)
>
> The correlation was never the surprise — 5m settlement directions agree across
> all five symbols 53% of the time against an independence null of 6%, and
> hype/zec, which the fleet has never armed, flipped the same way in the same
> window. The surprise is that a single model error is worth as many positions as
> the fleet has arms, and that the one gate sized to ration fleet-wide exposure
> hands out an exemption keyed on that same model.
>
> **Changed:** nothing yet — deliberately. Every same-side concentration cap
> priced against the recorded tape (`analysis/correlation_study.md`) turns out to
> be this one window wearing a policy's name: the best row is +$188 of which
> +$191 is the incident and the remainder is negative, and the only
> CI-significant rows in the whole table are negative. The lesson recorded here
> is the mismatch, not a knob. The actionable follow-up is the hybrid A/B that
> `hybrid_ab.md` parked for want of a sub-minute feed — the RTDS stream removed
> that blocker on the same day this window traded.

---

## Files

- `analysis/correlation_study.py` — driver (read-only over `~/.pmt`; no engine,
  no orders, no network).
- `analysis/correlation_study.txt` — full output: all matrices, all conditional
  cuts, the impulse catalog, every policy row and its per-epoch attribution.
