# Retry pricing — the $772 row, priced on the recorded tape (2026-08-23)

**Verdict: no retry policy clears the bar. Row (c) is rejected at 95% by every
policy tested. Today's re-decide stays exactly as it is.**

Trigger: `analysis/latency_report.txt` §6 row **(c) RETRY PRICING — $771.40 /
+3.396 c-share**, the largest unclaimed item in the report and the only one the
report itself flags as a hypothesis: *"at 12s the jump model predicts a 46%
adverse-move probability, which is above the 30% miss rate actually observed, so
(c) is the most overstated of the four … a hypothesis worth its own study, not a
change to make on this evidence."*

Driver: `analysis/retry_pricing.py`. Full output: `analysis/retry_pricing_report.txt`.

---

## Method

**Corpus** — 08-23 02:45:20Z → 08:41:09Z (5.93h): the intersection of book-tape
coverage and wallet coverage. Outside it one side of every counterfactual is
missing, and `~/.pmt` is read-only for this study so the activity feed was not
re-walked. 502 fires, 100 graded windows (93 wallet / 5 chainlink / 2 book —
L36-cleaned, wallet rows first), 12,718 filled shares, $9,624 crossed. Era mix
brake 338 / theta 56 / theta+payup 108. The 09:25–09:33Z blackout is excluded by
`latency_report.in_blackout`.

**Ground truth vs counterfactual.** A recorded fire's fill is *wallet* truth,
joined by `latency_report.match_fills` unchanged (newest-first, 12s TTL + 3s
on-chain grace). Every policy reuses that join for every clip it does not move.
A clip a policy would have **rested longer than the engine did** has no wallet
row; it is filled only where the recorded book actually traded through the
resting limit, and only **strictly after the recorded re-decide whose own ask
proves the live book had moved above it**. The fill is charged at *our* limit,
never at the better observed ask — a resting bid is the passive side.

**Three strictness tiers**, all reported: `ask` (one qualifying book sample),
`ask2` (two consecutive — one sample can be a stale straggler), `print` (a public
trade print on our outcome at ≤ our limit; needs no book-sampling assumption, but
`prints.jsonl` ends 07:39Z so it is a lower bound over the last 62min).
Give-up rules need no fill model at all — they only subtract recorded fills.

**Limit reconstruction is exact.** `pay_up_limit(ask, net, edge_req, pay_up_max,
max_price)` rebuilt from the fire record and tick-rounded reproduces **109/117**
joinable `order-latency-tape.jsonl` ack prices to the cent. (The 8 misses are all
after 15:00Z and are the live `pay_up_max` raise described at the bottom.)

**Valuation** — terminal settlement, hold-to-resolution, no exits, no fees, same
convention on every arm, so only the Δ is read (`hybrid_ab.md`'s rule). Base
absolute P&L is −$246.46 and is *not* wallet truth: the sim holds to settlement
where the live engine exits.

**Bootstrap** — percentile CI95 over **windows**, 10,000 resamples, the same unit
`aggression_sweep.md` used.

**Charges taken, stated once.** The fire schedule is held fixed — no policy is
credited with clips the tape does not contain, so budget a policy frees is never
re-deployed. `INFLIGHT_TTL_S` only gates *unfilled* clips (`updown.rs::on_fill`
removes the inflight entry), so a longer TTL is modelled as never blocking a
ladder add after a fill, and a held clip that fills frees the token immediately.
A resting bid changes the book it rests in; nobody can replay that.

---

## Result 0 — the population is a third smaller than "the chase" implies

81 re-decides of an unfilled clip in span. They are **not all chases**:

| the 12s re-decide came back | n | Δask |
|---|---|---|
| **WORSE** | 55 (68%) | p50 +3c, p90 +10c, max +35c |
| SAME | 13 (16%) | — |
| **BETTER** | 13 (16%) | p50 −1c, p90 −4c |

Chains: 139 total — 15 chase, 75 ladder, 49 singleton. A policy that pins the
original limit forgoes the *improvements* as well as the chase, and the table
below charges it for both.

**Does the book ever come back to the missed clip's own limit?** (an ask ≤ our
limit, scanned only after the re-decide that proved the live ask was higher)

| horizon | chase chains | ask | ask2 | print |
|---|---|---|---|---|
| 12s | 15 | 0 | 0 | 0 |
| 24s | 15 | 2 | 2 | 1 |
| 36s | 15 | 3 | 2 | 3 |
| 60s | 15 | 4 | 3 | 5 |
| 120s | 15 | 4 | 3 | 5 |

Holding recovers **at most a quarter** of the chases, and never inside the first
12 extra seconds.

Fill-model calibration (run over each clip's *own* life, where the wallet knows
the answer): `ask` precision 0.80 / recall 0.93, 88 false fills (17.5% of clips);
`ask2` 0.86 / 0.71, 44 false; `print` 0.83 / 0.96, 72 false. The model over-fills,
which is the direction that flatters every gain below.

---

## Result 1 — the policy table

Base: 502 clips, 379 filled, 12,718 shares, $9,624 notional, P&L −$246.46.

| policy | tier | clips | shares | lost | gained | **Δ P&L** | bootstrap CI95 |
|---|---|---|---|---|---|---|---|
| hold TTL 24s | ask | 433 | 10,716 | −2,649 | +647 | **+9.60** | [−60, +101] |
| hold TTL 36s | ask | 409 | 10,311 | −3,079 | +672 | **+17.28** | [−81, +160] |
| hold TTL 60s | ask | 391 | 10,132 | −3,355 | +769 | **−37.51** | [−105, +26] |
| ratchet (cap 0c) | ask | 419 | 11,144 | −1,908 | +334 | **+26.13** | [−131, +278] |
| chase cap 2c | ask | 441 | 11,699 | −1,228 | +209 | **+50.37** | [−93, +292] |
| chase cap 4c | ask | 470 | 12,098 | −729 | +109 | **+76.39** | [−55, +307] |
| chase cap 8c | ask | 495 | 12,555 | −213 | +50 | **−16.19** | [−44, −0] |
| give up after 0 (chain) | exact | 421 | 9,856 | −2,862 | 0 | **+39.91** | [−106, +274] |
| give up after 0 (window) | exact | 302 | 7,650 | −5,068 | 0 | **−53.89** | [−385, +377] |
| give up after 1 (window) | exact | 434 | 11,435 | −1,283 | 0 | **−95.86** | **[−205, −20]** |
| give up after 1 (chain) | exact | 486 | 12,428 | −290 | 0 | **−16.96** | **[−33, −5]** |
| give up after 2 (window) | exact | 484 | 12,483 | −235 | 0 | −8.08 | [−21, +0] |
| give up after 2 (chain) | exact | 498 | 12,667 | −51 | 0 | −3.10 | [−9, +0] |

Stricter tiers move the point estimates around and never rescue them:

| policy | ask | ask2 | print |
|---|---|---|---|
| hold TTL 24s | +9.60 | +31.91 | +3.13 |
| hold TTL 36s | +17.28 | +14.01 | **−17.55** |
| hold TTL 60s | −37.51 | −53.17 | −39.90 |
| ratchet (cap 0c) | +26.13 | **−0.80** | +33.57 |
| chase cap 2c | +50.37 | +25.67 | +56.29 |
| chase cap 4c | +76.39 | +71.89 | **−13.93** |
| chase cap 8c | −16.19 | −20.48 | −8.51 |

**Not one CI excludes zero on the positive side.** The only CI-significant rows
in the whole table are negative and they are the give-up rules: standing down
after one retry costs **−$95.86 [−205, −20]** window-scoped and −$16.96 [−33, −5]
chain-scoped. The stand-down forgoes 1,283 shares that were **100% winners**
(+$95.86, +7.47 c/share) — R9's whole design is to wait for banked evidence and
then ladder in, and giving up on the window throws that away.

---

## Result 2 — the sign flips under the fill attribution

The table rides on one free parameter: which fire a wallet row hangs on. Re-run
under the opposite attribution (`newest_first=False`, the same sweep
`latency_report` §1 does):

| policy | newest-first Δ | oldest-first Δ |
|---|---|---|
| base (absolute) | −246.46 | −242.71 |
| hold TTL 24s | +9.60 | **−9.45** |
| hold TTL 36s | +17.28 | **−22.86** |
| hold TTL 60s | −37.51 | −33.40 |
| ratchet (cap 0c) | +26.13 | **−46.34** |
| chase cap 2c | +50.37 | **−24.95** |
| chase cap 4c | +76.39 | **−21.77** |
| chase cap 8c | −16.19 | −11.68 |
| give up after 0 (chain) | +39.91 | +8.75 |
| give up after 1 (window) | −95.86 | −79.87 |
| give up after 1 (chain) | −16.96 | −11.98 |

**Every policy with a positive point estimate changes sign.** That is not a
coincidence: "clip k+1's fills landing on clip k" is precisely the event this
study calls "the chase filled", so a retry study lives in exactly the tail the
report already showed to be attribution-sensitive. The positive numbers are
measuring the join, not the market. Only the give-up rules keep their sign, and
they are negative.

---

## Result 3 — adverse selection, and where the damage actually is

| population | fills | shares | $notl | won% | P&L | c/sh | windows |
|---|---|---|---|---|---|---|---|
| base: not a chase re-decide | 326 | 9,856 | 8,317 | 82.3% | −206.55 | −2.10 | 89 |
| base: bought BY a chase re-decide | 53 | 2,862 | 1,307 | 44.3% | −39.91 | −1.39 | 26 |
| hold 24s: recovered at the original limit [ask] | 23 | 647 | 616 | 88.7% | −42.36 | **−6.55** | 12 |
| hold 24s: recovered [print] | 25 | 566 | 536 | 86.1% | −48.83 | **−8.63** | 13 |
| hold 36s: recovered [ask] | 23 | 672 | 634 | 88.1% | −42.46 | **−6.32** | 13 |
| hold 60s: recovered [ask] | 25 | 769 | 723 | 89.6% | −34.12 | −4.44 | 16 |
| hold 24s: chase fills the hold forgoes | 46 | 2,649 | 1,106 | 39.8% | −51.96 | −1.96 | 25 |

**Yes — held-price fills are adversely selected.** They win *more often*
(88.7% vs 39.8%) and lose *more money per share* (−6.55 c/sh vs −1.96), because
the ask returning to our limit is the market re-pricing our side cheaper, and the
minority that then settles against us was bought near 0.9. Every hold variant's
recovered population is **3–5× worse per share** than the chase fills it replaces,
on every tier. The hold policies' positive point estimates come entirely from
*trading less*, not from trading better — and `aggression_sweep.md` already
settled that frequency is not the lever.

**Price improvement is negative.** Cents/share the pinned limit saves against the
chase's own realised VWAP: hold 24s n=17, p50 **−1.00c**; hold 36s n=26, p50
**−1.00c**. Negative = the chase paid *less* than the limit we would have pinned.
That is the mirror of the 16% "repriced BETTER" row: §6's +10.64c mean pay-up is
a *gross* number against the first ask, and it does not translate into 10.64c of
recoverable price.

**By retry index** — the chase does not turn bad with depth:

| retry index | fills | shares | $notl | won% | P&L | c/sh | windows |
|---|---|---|---|---|---|---|---|
| 0 (original clip / ladder add) | 326 | 9,856 | 8,317 | 82.3% | −206.55 | −2.10 | 89 |
| 1 | 43 | 2,572 | 1,034 | 38.0% | −56.88 | −2.21 | 25 |
| 2 | 8 | 239 | 225 | 100.0% | +13.86 | +5.80 | 7 |
| 3 | 1 | 25 | 24 | 100.0% | +0.50 | +2.00 | 1 |
| 4+ | 1 | 26 | 23 | 100.0% | +2.60 | +10.00 | 1 |

Retry-1 looks lethal at 38% won — until it is split by which way the re-decide
moved:

| retry-1 fills | fills | shares | $notl | won% | P&L | c/sh | windows |
|---|---|---|---|---|---|---|---|
| re-decide came back **WORSE** (the chase) | 34 | 896 | 839 | **94.1%** | **+4.04** | **+0.45** | 22 |
| re-decide came back SAME | 0 | — | — | — | — | — | — |
| re-decide came back **BETTER** (ask fell) | 9 | 1,676 | 195 | 8.0% | −60.91 | −3.63 | 7 |

**The chase this study was commissioned to fix is profitable.** All the damage is
in the nine BETTER-priced re-decides, and those nine are two windows in costume:

- `eth-updown-5m-1787462100` — clip A asked 0.05 and missed, clip B asked **0.01**
  and bought **1,478 shares for $14.78**. A penny lottery clip that inflates every
  share-weighted statistic it touches and moves fifteen dollars.
- `btc-updown-15m-1787457600` — the **L22** window, a known loser whose clips lost
  on retry 0 as well as retry 1 (−$51.75 of the −$60.91).

Strip those two and the "cheaper re-decide is poison" story is gone. Under
oldest-first attribution the population is n=5, not 9. This is the **L37** shape
caught before it became a knob: a share-weighted slice of n=2 windows.

**Worked example** (`eth-updown-15m-1787461200 down`, wallet-graded winner=down —
a window we *won*):

```
  +  0.0s  fire down 57sh  ask 0.87 limit 0.87 net +12.1c  -> filled  0sh
  + 12.0s  fire down 52sh  ask 0.95 limit 0.95 net  +4.6c  -> filled 52sh @ 0.950
  + 24.1s  fire down 55sh  ask 0.90 limit 0.90 net  +9.3c  -> filled 55sh @ 0.883
  + 36.1s  fire down 50sh  ask 0.91 limit 0.91 net  +8.4c  -> filled 50sh @ 0.910
  + 48.2s  fire down 54sh  ask 0.91 limit 0.91 net  +8.4c  -> filled 54sh @ 0.910
  hold 24s: pinned at 0.87 — nothing crossed; the +12.0s clip (52sh) suppressed.
  hold 36s: pinned at 0.87 — 40sh @ 0.87 at +28.4s; the +12.0s and +24.1s clips
           (107sh of a winner) suppressed to get it.
```

The chase paid up 8c and bought 157 shares of a winning window. The hold buys 40.

---

## Result 4 — row (c) is rejected

Pro-rata by shares, §6 row (c) implies **$431.89** of retry-pricing value sitting
in this corpus (12,718 shares × 3.396 c/share). The 95% upper bound of every
policy × tier combination is below it — the largest upper bound anywhere in the
table is **+$307**:

| | ask | ask2 | print |
|---|---|---|---|
| hold TTL 24s | +101 | +193 | +84 |
| hold TTL 36s | +160 | +170 | +56 |
| hold TTL 60s | +26 | +23 | +23 |
| ratchet (cap 0c) | +278 | +260 | +283 |
| chase cap 2c | +292 | +280 | +297 |
| chase cap 4c | +307 | +304 | +17 |
| chase cap 8c | −0 | −0 | −0 |

(CI95 upper bounds. All < $432 → **REJECTED**.)

Why the model over-priced it, in the model's own terms: λ(12s)=46.3% is the
probability the ask ticks up *at all*, but 68% of re-decides are worse and only
a quarter of those chases ever see the book come back — and the 10.64c pay-up is
gross against the first ask, against which the chase's realised VWAP is a
*penny better*, not 10c worse. Row (c) is not a pot of money a shorter TTL or a
pinned limit picks up. It is the price of a chase that the recorded book mostly
does not give back, on a population that wins 94% of the time.

---

## Result 5 — the knob that actually moved: `pay_up_max`

Every policy above fixes the retry. `pay_up_max` *prevents* it, from the entry
side: a fatter marketable buffer means the clip crosses even though the ask
ticked up in flight, and a marketable limit still fills **at the book**, so the
buffer costs nothing unless the book actually moved.

**As of ~15:00Z today the live arms carry btc/eth `pay_up_max` 0.05, sol 0.04,
bnb/xrp 0.02** (`~/.pmt/engine/arms-state.json`, and the 8 order-tape prices this
study's reconstruction misses are exactly those). That is a 2.5× loosening of the
chase budget, deployed after this corpus was recorded and with no A/B behind it.
What the corpus says it buys — an unfilled clip whose re-decide came back at ask′
would have crossed on the *first* attempt if ask′ ≤ tick(ask + min(surplus,
pay_up_max)); ask′ is the engine's own next live read, so this needs no book
model:

| pay_up_max | worse re-decides pre-empted | buffer spent (ceiling on extra paid) |
|---|---|---|
| 0c | 0/55 | 0.00c |
| 2c | 24/55 | 1.83c ← this corpus's policy |
| 4c | 40/55 | 3.20c |
| **5c** | **42/55 (76%)** | **3.79c** ← live now on btc/eth |
| 8c | 48/55 | 4.83c |

The buffer is the only lever in this study that buys fills **without** buying a
stale price — the limit stays marketable, so it pays the book on arrival
(latency_report §1: filled clips pay at or better than their quoted ask 95% of
the time), and the right-hand column is a ceiling, not an expectation. What it
cannot reach are the clips whose re-decide moved further than the buffer: the p90
worse re-decide is +10c, twice the 5c now live.

---

## Verdict

1. **Nothing ships from this study.** No policy clears the never-loosen bar,
   because no policy clears zero. The best point estimates (chase cap 4c +$76,
   chase cap 2c +$50) all invert under the opposite fill attribution, and their
   CI95 upper bounds are below the $432 the row they were built to harvest
   implies. `INFLIGHT_TTL_S = 12.0` stays at 12.0; today's re-decide keeps
   repricing at the new ask + pay-up.
2. **Repricing LESS is a tightening and would have been deployable — it just
   isn't worth anything.** The ratchet (never quote above the original limit) is
   +$26 [−131, +278] on the loosest tier and −$46 under oldest-first. It forgoes
   1,908 shares to gain 334.
3. **Holding limits LONGER is refused on fill-risk grounds**, and would need
   the replay A/B anyway: it recovers ≤4 of 15 chases, and what it recovers is
   adversely selected — 3–5× worse per share than the
   chase fills it gives up, on every tier. The A/B that would be required is
   `pmengine replay --mode full` over the r7-frozen corpus with `INFLIGHT_TTL_S`
   promoted to an `ArmParam`, base 12s vs 24s/36s, bootstrap over windows —
   the `r7_fleet_ab.py` shape. Not worth building on these numbers.
4. **Giving up is actively wrong and it is the one CI-significant result here.**
   Stand down after one retry and you lose **$95.86 [−205, −20]** window-scoped,
   $16.96 [−33, −5] chain-scoped, because the fills you refuse are 100% winners.
   The chase deepens into R9's banked-evidence ladder; cutting it cuts the edge.
   **Do not build a give-up rule.**
5. **Row (c) of latency_report §6 should be marked down.** It is a modelled
   upper bound that the recorded book rejects at 95%. Section 6's ranking
   survives — (b) polling at $362 is still an order of magnitude above (a) — but
   (c) should no longer head the list of unclaimed money.
6. **The one open question this study created**: `pay_up_max` 0.02→0.05 went
   live today unmeasured, on btc and eth, the two largest arms. It pre-empts
   42/55 of the worse re-decides for ≤3.79c of buffer, and it is a *loosening*
   under the ROADMAP's rule ("Never loosen … without a replay A/B win"). It is
   also the only lever here that converts a miss into a fill without buying a
   stale price. **Run the A/B on `--pay-up` before it runs a full night at
   btc5m's $1000 size**, not on this corpus's 108 theta+payup fires.

## What would settle the retry question properly

- **Order-level truth, not a wallet join.** Every positive number in section 2
  died on the attribution sweep. `order-latency-tape.jsonl` has been carrying
  `decision_id` → `order_id` since ~10:28Z today; once it also carries the
  fill/cancel terminal state, the join disappears and this study can be re-run
  with no free parameter. That is the single highest-value change to the tape.
- **More corpus.** 15 chase chains and 81 re-decides in 5.93h is not enough to
  resolve a $50 effect against a base whose per-window P&L has a ±$150 bootstrap
  spread. A week of tape, not a night.
- **Extend book-tape coverage backwards.** The book tape starts 02:45Z and the
  wallet cache ends 08:41Z; that 5.93h intersection threw away half the fire era
  the latency report measured.

## Files

- `analysis/retry_pricing.py` — driver (read-only over `~/.pmt`; no engine, no
  orders, no network).
- `analysis/retry_pricing_report.txt` — full output, all tiers, worked example,
  matcher sweep.

## Addendum (operator, same day): the pay-up A/B this study demanded

`--pay-up 0.02 vs 0.05`, frozen corpus, same driver as aggression_sweep:
282 windows, Δnet **+$71.81**, CI95 [−$91.98, +$363.88]. Point-positive,
not CI-clearing. Kept at 0.05 for a live A/B night on these grounds:
pay_up_max is chase width funded only by surplus edge (min_edge floor
unchanged — not a brake or guard), and this study's own §chase population
measured 94.1% won. The loop grades tonight's realized fills; a negative
wallet read reverts it.
