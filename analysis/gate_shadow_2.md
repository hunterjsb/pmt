# Gate shadow, second read — 2026-08-23

What our own gate stack cost and saved, re-priced on the CURRENT regime against
wallet/resolution truth only. Driver: `analysis/gate_shadow_2.py`
(`--self-check`, `--trace <slug>`, `--json`). Inputs frozen at 21:15:34Z:
`updown-tape.jsonl` (66,224 records), the outcomes corpus refreshed to 1,134
terminal-graded windows, and a full wallet walk (2,410 rows). `~/.pmt` was
never written — the corpus refresh ran under a shadow `$HOME`.

**Nothing here is a recommendation to deploy.** Every loosen needs its own
replay A/B win (ROADMAP:288, ROADMAP:82). §7 says where those A/Bs should aim.

---

## 1. Method, and what it changes from the first read

The first shadow read used `polymarket/shadow.py` (`pmt crypto stats --gates`).
Four things in it move the answer, and all four are fixed here.

**(a) Terminal outcomes only.** `shadow.py` grades off `merge_outcomes()`, which
returns `chainlink` and `book` labels — our own read of settlement, which
`outcomes.py` itself forbids for W-L. Here a window prices only on a wallet
redeem or a gamma resolution. Coverage is fine: after the refresh **every one**
of the corpus's 1,134 rows is terminal (263 wallet, 871 resolution), and 92% of
the episodes in range price.

**(b) The entry price.** `shadow.py` prices each refusal run at its *lowest*
recorded ask. A basis-guard run spans a whole window (median 35s, p90 237s), and
its cheapest tick is the moment the book was most certain that side was wrong —
the moment our model was most likely wrong with it. The honest counterfactual is
the ask at the tick the gate *first* refused, which is where the clip would
actually have gone out. Both are reported. **The verdict flips on this choice
alone**: at `best` the stack looks +$1,586 over-tight; at `first` it is
**−$8,582** (i.e. it saved $8,582 net), at the run's median **−$11,310**.

**(c) Mode-correct thresholds.** `shadow.py` tests every side against 0.97/0.015
— the safe-mode bar. A speculative-mode side is judged at 0.55/0.08, so the old
ledger mislabels early sides in both directions.

**(d) The gates it cannot see.** An unbraked side that cleared fair and edge and
still didn't fire is dropped by `categorize_ticks` as noise. That is where
`chop`, the 0.985 price ceiling and the budget floor live.

Attribution follows `decide()` in its own order, so each refused side is charged
to the **first** gate that stopped it:

```
brake (safety=theta / distrust / avg_down / latched / fleet)
  -> chop (!unlocked && rho < -0.25)  -> min_fair -> min_edge
  -> price_cap (ask > 0.985)          -> budget (sized(room) < 5 shares)
  -> cooldown (residual: clip_cooldown_s / inflight — not on the tape)
```

The threshold table is **proved, not assumed**: `--self-check` replays every one
of the 1,202 real fires that carry a `mode` field against it. Zero violations.

### Two gates no tape study can see

`decide()` returns from both of these *before* it writes any record:

- **`quiesce_secs`** — the final 20s of every window (6.7% of a 5m window's
  life). Orders pulled, no new taker clips except the flip-proof carve-out. It
  leaves `last_eval` state=quiesce and pushes nothing. Every phase number below
  therefore stops at 93.3% elapsed.
- **`min_elapsed_frac`** — the retired clock gate; 0.0 on every live arm.

### The denominator

| | |
|---|---|
| armed windows since 11:29Z | **637** |
| windows that fired ≥1 clip | **111 (17%)** |
| wallet-traded windows | 108 |
| deployed notional | **$6,332** |
| realized | **+$92.69 (+1.46%)**, 101/108 windows up |
| mean notional per traded window | **$59** |

The shadow ledger prices one clip per refused side, ~$200k of hypothetical
notional against $6.3k actually deployed. Read every number below as "the gates
refuse roughly 30× the notional they pass, and that refusal is worth −$8.6k."

---

## 2. Per-gate net table

`net = missed wins − avoided losses`. **Positive = over-tight** (it missed more
than it dodged). Negative = paying for itself. Priced at the first-refusal ask;
`@median` and `@best` are the same episodes at the run's median and lowest ask.

### Stream era — since 11:29:30Z (e296336), 9.77h

| gate | eps | priced | refused-side hit | missed wins | avoided losses | **net** | net/h | @median | @best |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| basis_guard | 1696 | 1624 | 62% | $11,601.55 | $10,968.56 | **+$632.99** | +$64.80 | −$2,235.63 | +$5,984.84 |
| feed_stale ¹ | 704 | 501 | 49% | $4,380.16 | $5,671.33 | **−$1,291.18** | −$132.19 | −$1,296.23 | −$1,235.94 |
| reference_wait ¹ | 68 | 57 | 51% | $318.58 | $595.79 | **−$277.21** | −$28.38 | −$277.75 | −$134.12 |
| theta | 1077 | 1053 | 47% | $6,226.15 | $10,623.73 | **−$4,397.58** | −$450.21 | −$4,180.32 | −$1,094.61 |
| distrust | 51 | 51 | 86% | $333.51 | $77.54 | **+$255.97** | +$26.21 | +$252.88 | +$325.44 |
| avg_down | 25 | 25 | 80% | $74.28 | $44.80 | **+$29.47** | +$3.02 | +$26.91 | +$34.61 |
| latch | 177 | 174 | 75% | $468.31 | $1,119.05 | **−$650.74** | −$66.62 | −$755.87 | −$470.92 |
| fleet | 1 | 1 | 100% | $0.34 | $0.00 | **+$0.34** | +$0.03 | +$0.34 | +$0.34 |
| chop | 16 | 16 | 100% | $68.21 | $0.00 | **+$68.21** | +$6.98 | +$51.78 | +$79.43 |
| min_fair | 148 | 146 | 13% | $676.46 | $3,750.22 | **−$3,073.76** | −$314.68 | −$2,960.03 | −$2,081.59 |
| min_edge | 178 | 174 | 95% | $110.21 | $82.49 | **+$27.73** | +$2.84 | +$14.09 | +$48.61 |
| budget | 7 | 7 | 100% | $0.64 | $0.00 | **+$0.64** | +$0.07 | +$0.58 | +$0.71 |
| cooldown | 94 | 94 | 93% | $153.50 | $60.55 | **+$92.95** | +$9.52 | +$48.88 | +$129.50 |
| **TOTAL** | 4242 | 3923 | 58% | $24,411.90 | $32,994.06 | **−$8,582.17** | −$878.62 | −$11,310.34 | +$1,586.30 |
| **DIRECTIONAL ONLY** | 3470 | 3365 | 59% | $19,713.16 | $26,726.94 | **−$7,013.78** | −$718.05 | −$9,736.37 | +$2,956.36 |

`price_cap` never appears: `p_cap` is 1.0 fleet-wide (no eval on the tape
carries `fair_raw`), and `ask > 0.985` never bound as the *first* gate.

¹ **Directionless.** A blind arm has no side to want, so the counterfactual has
to price both sides — which is why both land on a ~50% hit rate. That number is
an artifact of the construction, not a reading; their honest unit is exposure
(§6). **DIRECTIONAL ONLY** is the line to read.

### Today's regime — since 17:31:11Z (engine restart; 15m shut to $1, sol/xrp maker bids), 3.74h

| gate | eps | priced | hit | missed | avoided | **net** | net/h | @median | @best |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| basis_guard | 752 | 709 | 59% | $3,974.95 | $4,188.37 | **−$213.41** | −$57.07 | −$1,373.24 | +$1,499.95 |
| feed_stale ¹ | 248 | 188 | 48% | $1,210.06 | $1,839.18 | **−$629.12** | −$168.23 | −$632.87 | −$585.59 |
| reference_wait ¹ | 40 | 32 | 53% | $168.81 | $340.04 | **−$171.23** | −$45.79 | −$167.92 | −$29.85 |
| theta | 374 | 350 | 47% | $2,270.16 | $2,741.91 | **−$471.75** | −$126.15 | −$389.99 | +$898.78 |
| distrust | 10 | 10 | 70% | $56.88 | $35.28 | **+$21.60** | +$5.78 | +$29.59 | +$41.95 |
| avg_down | 7 | 7 | 86% | $18.27 | $8.82 | **+$9.45** | +$2.53 | +$9.30 | +$9.68 |
| latch | 41 | 38 | 68% | $86.18 | $156.28 | **−$70.10** | −$18.74 | −$95.19 | −$56.45 |
| chop | 1 | 1 | 100% | $1.80 | $0.00 | **+$1.80** | +$0.48 | +$1.34 | +$4.28 |
| min_fair | 39 | 37 | 11% | $193.34 | $1,006.17 | **−$812.83** | −$217.35 | −$704.57 | −$310.32 |
| min_edge | 52 | 48 | 94% | $19.69 | $26.62 | **−$6.94** | −$1.86 | −$11.59 | −$4.16 |
| budget | 4 | 4 | 100% | $0.24 | $0.00 | **+$0.24** | +$0.06 | +$0.18 | +$0.31 |
| cooldown | 24 | 24 | 88% | $31.88 | $25.62 | **+$6.26** | +$1.67 | +$1.90 | +$6.34 |
| **TOTAL** | 1592 | 1448 | 56% | $8,032.27 | $10,368.30 | **−$2,336.03** | −$624.65 | −$3,333.06 | +$1,474.92 |
| **DIRECTIONAL ONLY** | 1304 | 1228 | 57% | $6,653.39 | $8,189.07 | **−$1,535.68** | −$410.64 | −$2,532.27 | +$2,090.35 |

The regime slice moves one thing: **`basis_guard` flips from +$633 over-tight to
−$213 paying for itself**, and holds that sign at `@median`. On today's guards
(6/8/16, sol 10) the basis guard is not the leak the 9.8h number suggests.

### The posture the operator named

`20:45Z` sits *inside* a change, not at its start: the engine log dates
btc→rtds@60 at **20:25:50Z** and eth at 20:25:55Z (xrp had been on rtds since
17:31Z, and moved 30s→60s at 19:35Z). Both cuts are reported; neither carries a
conclusion at 0.5–0.8h of tape.

| slice | hours | eps | priced | net (directional) | net/h |
|---|---:|---:|---:|---:|---:|
| posture — since 20:25:50Z | 0.83 | 310 | 215 | **−$195.86** | −$236.29 |
| posture_asked — since 20:45:00Z | 0.51 | 192 | 105 | **−$289.47** | −$568.20 |

Both are small and both are negative, i.e. the stack is saving money on today's
posture too. The 20:45Z slice is also badly polluted by the reference-wait
incident in §6 — 22 of its 192 episodes are one blind window on three arms.

---

## 3. The core finding: every gate is two policies wearing one name

Split each gate's refusals by the ask it refused at, and the aggregate falls
apart. **The stack is correct on cheap sides and over-tight on dear ones**, and
this holds across three entry-price conventions and both time slices.

| gate | entry ask | eps | hit | missed wins | avoided losses | **net** |
|---|---|---:|---:|---:|---:|---:|
| basis_guard | 0.00–0.20 | 10 | 10% | $96.04 | $112.62 | −$16.59 |
| basis_guard | 0.20–0.50 | 212 | 47% | $2,295.82 | $2,322.35 | −$26.53 |
| basis_guard | 0.50–0.80 | 1213 | 61% | $8,781.56 | $8,190.37 | **+$591.19** |
| basis_guard | 0.80–0.95 | 147 | 82% | $409.51 | $343.21 | **+$66.30** |
| basis_guard | 0.95–1.00 | 42 | 100% | $18.62 | $0.00 | +$18.62 |
| theta | 0.00–0.20 | 241 | 9% | $2,528.08 | $5,076.58 | **−$2,548.49** |
| theta | 0.20–0.50 | 316 | 24% | $1,877.76 | $4,189.26 | **−$2,311.49** |
| theta | 0.50–0.80 | 244 | 72% | $1,252.11 | $863.23 | **+$388.88** |
| theta | 0.80–0.95 | 197 | 85% | $534.48 | $466.66 | +$67.82 |
| theta | 0.95–1.00 | 55 | 95% | $33.71 | $28.00 | +$5.70 |
| min_fair | 0.00–0.20 | 130 | 5% | $661.35 | $3,696.65 | **−$3,035.30** |
| min_fair | 0.20–1.00 | 16 | 75% | $15.11 | $53.57 | −$38.46 |
| latch | 0.00–0.20 | 33 | 9% | $158.48 | $912.96 | **−$754.48** |
| latch | 0.20–0.50 | 8 | 38% | $58.77 | $131.16 | −$72.39 |
| latch | 0.50–0.80 | 5 | 80% | $17.00 | $9.30 | +$7.70 |
| latch | 0.80–0.95 | 70 | 93% | $201.35 | $47.51 | **+$153.84** |
| latch | 0.95–1.00 | 58 | 97% | $32.71 | $18.12 | +$14.59 |

Robustness of the dear-side bands across all three conventions:

| gate × band | eps | hit | @first | @median | @best |
|---|---:|---:|---:|---:|---:|
| latch × 0.80–1.00 (stream) | 128 | 95% | **+$168.43** | **+$91.97** | +$220.14 |
| latch × 0.80–1.00 (regime) | 26 | 92% | **+$26.98** | **+$17.25** | +$40.63 |
| basis_guard × 0.80–1.00 (stream) | 189 | 86% | **+$84.92** | **+$108.61** | +$497.91 |
| basis_guard × 0.80–1.00 (regime) | 79 | 85% | **+$65.85** | **+$96.30** | +$308.58 |
| theta × 0.50–0.80 (stream) | 244 | 72% | +$388.88 | +$75.91 | +$524.99 |
| theta × 0.50–0.80 (regime) | 71 | 63% | +$44.49 | −$7.01 | +$52.83 |
| theta × 0.80–1.00 (regime) | 94 | 81% | −$106.13 | −$139.40 | −$89.54 |
| basis_guard × 0.50–0.80 (stream) | 1213 | 61% | +$591.19 | −$1,444.20 | +$4,079.29 |

Only the top two rows survive every convention and both slices. `theta`'s
dear-side surplus is convention-fragile and sign-flips in the regime slice;
`basis_guard × 0.50–0.80` swings $5.5k across conventions and is not a finding
at all.

---

## 4. Top five recurring miss patterns

### #1 — The window latch holds a side the model has since proved. +$168 / 9.8h

`brake_latched` is set by a raw distrust/avg_down brake and only ever released
by `banked_decided`. Two things make it bite harder than it reads:

- The latch is **set** before `brake` is chosen, so a raw brake that fires while
  `safety < theta` sets the latch and reports as `theta`. **A window can be
  latched with no distrust/avg_down record on the tape at all.**
- Release is `banked_decided` only. `safety` climbing past `theta`, or `fair`
  reaching 1.000, does not open it.

Measured cohort — latch-refused sides that had *already* cleared the R9 entry
gate and the fair bar (`safety ≥ 0.30 && fair ≥ 0.97`), fired at the first tick
that was true:

| slice | opens | hit | missed | avoided | **net** |
|---|---:|---:|---:|---:|---:|
| stream (9.8h) | 113 | 95% | $196.68 | $60.38 | **+$136.30** |
| regime (3.7h) | 22 | 91% | $35.58 | $18.12 | **+$17.46** |

**Specimen, fully traced** (`--trace eth-updown-5m-1787514300`) — eth 19:45Z,
$900/$110, guard 8bp, rtds, **winner UP**:

```
19:46:22  27.4%  up ask=0.92 fair=1.000 net=+0.074 safety=0.13 -> theta
19:46:32  30.9%  up ask=0.91 fair=1.000 net=+0.084 safety=0.40 -> latch   <-- theta cleared, latch takes over
19:47:09  43.3%  up ask=0.83 fair=1.000 net=+0.158 safety=0.44 -> distrust <-- the ONLY visible raw brake, 37s later
19:47:35  51.8%  up ask=0.90 fair=1.000 net=+0.093 safety=0.72 -> latch
```

The window's fair pinned at 1.000 from 27% elapsed, safety rose to 0.72, and the
arm never fired. Second specimen `eth-updown-5m-1787499300` (15:35Z, winner UP)
shows the same shape and cost +$79 across its `theta` and `latch` episodes.

### #2 — theta's marginal band: safety 0.25–0.30. +$208 / 9.8h

`theta` is the stack's biggest saver (−$4,398) and almost all of that saving is
at cheap asks (−$4,860 under $0.50). Its margin is the opposite. Dropping the
entry bar and firing at the first tick that clears the looser bar:

| theta → X | opens | priced | hit | missed | avoided | **net** |
|---|---:|---:|---:|---:|---:|---:|
| 0.28 | 92 | 89 | 92% | $237.71 | $43.53 | **+$194.18** |
| **0.25** | 121 | 118 | **92%** | $276.79 | $69.20 | **+$207.59** |
| 0.20 | 188 | 185 | 89% | $402.16 | $259.88 | +$142.28 |
| 0.15 | 229 | 225 | 88% | $538.88 | $319.90 | +$218.99 |
| 0.10 | 285 | 281 | 85% | $728.45 | $592.18 | +$136.26 |
| 0.05 | 330 | 325 | 82% | $997.63 | $865.85 | +$131.77 |

The band is self-selecting for dear asks — at safety ≥ 0.25 the first crossing is
already at ask ≥ 0.50 in 118 of 118 cases — so a compound "theta 0.25 AND
ask ≥ 0.50" buys exactly the same $208. **Specimen:**
`btc-updown-5m-1787506200` up, 17:31:24Z, entry 0.13, +$326 (a cheap-entry
outlier — the median band entry is 0.6–0.8).

### #3 — Book depth, not any gate, truncates the first clip. The largest single leak

`sized(r) = min(clip_usdc/ask, ask_size, room/ask)`. On a window's **first**
clip the early room (0.2 × size_usdc) exceeds `clip_usdc` on every live arm, so
a short first clip was truncated by `ask_size` and nothing else.

| arm | first clips | clip_usdc | early room | median first clip | **% of clip** |
|---|---:|---:|---:|---:|---:|
| btc 5m | 15 | $150 | $200 | $96.5 | 64% |
| **eth 5m** | 34 | $110 | $180 | **$17.9** | **16%** |
| sol 5m | 30 | $50 | $80 | $24.4 | 49% |
| xrp 5m | 25 | $10 | $20 | $8.8 | 88% |

eth is armed at $900 with a $110 clip and the book hands it $18. Over the whole
era 81–93% of all fires land under 90% of their intended clip. This is the
mechanism behind "$6,332 deployed out of 637 armed windows" and it is not
reachable by any gate knob — it is the supply problem `analysis/freq_funnel_report.md`
found (9.6% of armed time has *no ask at any price*), which is what the sol/xrp
maker bids exist to attack. **btc and eth have no maker bid.**

### #4 — Whole-window write-offs on the settlement stream. 13 windows, incl. a 3-arm simultaneous loss

`range-start reference not printed yet` is the model failing to find
`per_min[start-60]`. It is supposed to clear in the first seconds. On rtds arms
it does not:

| gate | symbol | windows touched | of armed | ticks | median last tick | **held past 90%** |
|---|---|---:|---:|---:|---:|---:|
| reference_wait | xrp | 13 | 117 | 410 | **92%** | **11** |
| reference_wait | btc | 8 | 136 | 59 | 2% | 1 |
| reference_wait | eth | 7 | 136 | 58 | 1% | 1 |
| reference_wait | sol | 3 | 136 | 3 | 1% | 0 |
| reference_wait | bnb | 2 | 112 | 2 | 2% | 0 |
| feed_stale | xrp | 36 | 117 | 202 | 53% | 3 |
| feed_stale | btc/eth/sol/bnb | 293 | 520 | 356 | 2% | 1 |

Quiesce starts at 93.3% elapsed on a 5m window, so "held past 90%" means the
window was shut for its whole tradeable life. **13 such windows**, 11 of them
xrp — and at **20:45:00Z all three rtds arms lost the same window at once**
(`btc/eth/xrp-updown-5m-1787517900`, 52 ticks each): one missing 20:44Z
settlement mark took out the fleet's three largest arms simultaneously.

**Specimen, fully traced** (`--trace btc-updown-5m-1787517900`) — btc 20:45Z,
$1000/$150, rtds, **winner DOWN**; the down side was quoted 0.72–0.91 for the
entire window and the arm could not read a price to compare it to:

```
20:45:00  ROLL size=1000
20:45:18   6.3%  GATED range-start reference not printed yet  up_ask=0.16 dn_ask=0.85
...  (52 consecutive ticks, unchanged reason)
20:49:XX  92.0%  GATED range-start reference not printed yet
```

The next window (20:50Z) cleared in one tick on all three arms, so this is a
transient relay/mark gap, not a persistent config fault.

### #5 — The old #1 leak is closed: unfilled fires are worth $15

Pay-up went live at **15:43:32Z** (114 fires carry a `limit`, every one of them
above its ask, median chase 1.3¢, max 5¢).

| cohort | (slug,side) fires | fully filled | intended $ | filled $ | **fill rate** | unfilled $ | net of the gap |
|---|---:|---:|---:|---:|---:|---:|---:|
| not chased (pre 15:43Z) | 60 | 40 | $3,897 | $3,663 | **94.0%** | $234 | +$12.73 |
| chased (post 15:43Z) | 51 | 33 | $2,678 | $2,477 | **92.5%** | $202 | +$2.54 |

Two readings, and both matter: **pay-up did not move the fill rate** (93% either
side of it), and **the leak is no longer worth chasing** — $436 of unfilled
notional over 9.8h, worth +$15 in hindsight, against $8.6k of gate effect. At
today's clip sizes this class is closed; it would reopen if clips grew, since
the truncation in #3 means the fires are small enough to fill easily.

### Where the misses cluster

By phase (basis_guard only; every phase table stops at 93.3% — see §1):

| phase | eps | hit | missed | avoided | **net** |
|---|---:|---:|---:|---:|---:|
| early (0–33%) | 1156 | 59% | $8,864.73 | $7,865.32 | **+$999.40** |
| mid (33–66%) | 354 | 66% | $1,858.41 | $2,197.26 | −$338.86 |
| late (66–100%) | 141 | 72% | $493.90 | $414.23 | +$79.67 |

The guard's apparent over-tightness is an *early-window* phenomenon, which is
also where the entry prices are cheapest and the variance highest — it does not
survive the median convention (early goes to −$1,110). By symbol × gate the
biggest single lines are `sol · theta` −$1,458, `btc · theta` −$1,448,
`btc · min_fair` −$1,332 (all savers) and `eth · basis_guard` +$543,
`btc · basis_guard` +$502 (over-tight, both convention-fragile). No hour of the
day carries a distinct signature: **ten of the eleven hours are net-negative**,
and the one positive hour (17Z, +$416) is the 17:15Z five-arm correlated event.

---

## 5. The 15m question

**Verdict: 15m is correctly shut, and not for the reason the framing assumes.**

Since 13:00Z: **7,211** decision ticks on 15m windows (2,167 eval records / 3,994
eval sides + 5,044 window-level gated ticks), **0 fires**. The last 15m fire on
this tape is **07:40:35Z**, before the 15m arms were retired at 08:15Z.

What holds it shut, by bind frequency, since the 17:31Z restart (3,405 side-evals):

| gate | side-evals | share |
|---|---:|---:|
| **theta** | 3,110 | **91.3%** |
| min_edge | 147 | 4.3% |
| min_fair | 71 | 2.1% |
| budget | 48 | 1.4% |
| latch | 29 | 0.9% |

plus 4,889 window-level `basis_guard` ticks that never reach the eval loop at all.

**But theta is 1.0 on these arms and the arms are $1.** `arms-state.json`:
15m runs `size_usdc 1.0`, `clip_usdc 1.0`, `min_fair 1.0`, `theta 1.0`, feed
binance — re-armed that way at 17:00Z (the roll tape shows 15m at $300–$500 from
00:15Z to 08:15Z, then $1.0 from 17:15Z on). Those are shut settings, not a gate
reading. `sized(r)` needs `r > 5.0`; a $1 arm can never produce a 5-share clip
at any price.

Running the cascade again with **theta forced to 0.3 (the 5m value) and min_fair
0.97**, leaving size alone:

| would-bind | side-evals | share |
|---|---:|---:|
| theta @ 0.3 | 2,536 | 74.5% |
| min_edge | 548 | 16.1% |
| **budget ($1 arm)** | 282 | 8.3% |
| chop | 39 | 1.1% |
| **WOULD FIRE** | **0** | **0.0%** |

**Not one side would fire even with theta at the 5m value.** 15m is shut by
size first and theta second.

**`decided_k = 1.25` is not what holds 15m shut.** It only moves
`banked_decided` (`Tunables::law`, `dur_s > 300`), which sits *downstream* of
both theta and budget. 26.8% of 15m eval records carried `banked_decided = true`
anyway; the waiver was available and never reached.

**Is there a measured miss?** Yes, but it is a counterfactual about an arm that
does not exist. Pricing the 15m refusals at the clip a normally-sized 15m arm
used ($24, the median real 15m fire notional from the $300–500 era):

| gate | eps | priced | hit | missed | avoided | **net** | net/h |
|---|---:|---:|---:|---:|---:|---:|---:|
| theta | 129 | 119 | 50% | $1,969.48 | $1,461.60 | **+$507.88** | +$135.81 |
| all others | 21 | 12 | 100% | $10.29 | $0.00 | +$10.29 | +$2.75 |
| **TOTAL** | 150 | 131 | 54% | $1,979.77 | $1,461.60 | **+$518.17** | +$138.56 |

At the arm's **real** $1 clip the same refusals are worth **+$21.27** — which is
the honest number for the fleet as configured.

So: 15m has a nominal +$518/3.7h counterfactual, at a 54% refused-side hit rate,
resting entirely on the `theta` family and on cheap entries. Given `range_avg`'s
15m record was the hole this configuration was built to close, that is not
evidence to reopen it — it is the specification for an A/B (§7 #5).

---

## 6. Gates binding on data-quality artifacts, not market reality

**(a) `reference_wait` on rtds arms — see §4 #4.** 13 whole-window write-offs,
11 xrp; a 3-arm simultaneous loss at 20:45Z. This is the settlement relay, not
the market. The engine cannot distinguish "the mark has not arrived" from "the
mark will never arrive", so it holds the window instead of falling back. Note
`docs`' own warning that the RTDS *recorder* is a second subscriber whose drops
are not the engine's — here the drops are the engine's, on the live tape.

**(b) `spot_age_s` reports an epoch when the feed has never printed.**
`eval_model` computes `now - f.spot_ts`; a `FeedState` that has never received a
sample leaves `spot_ts` at 0.0, so the field carries an absolute unix time.

- **123** gated ticks report `spot_age_s > 1e6`.
- **120 of 123** land in the first 30s of a window: the roll chain arms the next
  window before its feed thread's first print.
- sol 48, eth 29, btc 24, bnb 22 — every one of them binance-fed.
- The 237 well-formed readings: median 27.8s, p90 60.7s.

Two consequences. The instrument is wrong (any study or alert thresholding on
`spot_age_s` sees garbage on 34% of the readings that have one), and the gate's
prose collapses two different failures — *stopped printing* and *never started*
— into one sentence. Cost is small (one tick per roll, cleared within 5s) but it
is a refusal on a cold cache, not on market state.

**(c) `feed_stale` subtypes.** 421 bare "feed stale", 62 `rtds stalled Ns with
the socket open`, 29 binance `data-api ticker` HTTP errors. The rtds-stall class
(median age 44.6s, max 91.5s) is a live relay problem: an open socket delivering
nothing for a minute and a half.

**(d) Basis-guard margin distance** — where the gated ticks actually sit, against
the guard *that tick* enforced:

| symbol | guard (bp) | 0–1bp under | 1–2bp | 2–3bp | 3–6bp | 6bp+ | total |
|---|---|---:|---:|---:|---:|---:|---:|
| bnb | 8 | 249 (5%) | 391 (8%) | 532 (11%) | 1726 (37%) | 1797 (38%) | 4695 |
| btc | 6 | 470 (7%) | 781 (12%) | 848 (14%) | 4166 (66%) | 4 (0%) | 6269 |
| eth | 6/8 | 485 (10%) | 558 (11%) | 623 (13%) | 2593 (52%) | 689 (14%) | 4948 |
| sol | 10 | 461 (8%) | 411 (7%) | 590 (10%) | 1722 (29%) | 2658 (45%) | 5842 |
| xrp | 12/16 | 162 (6%) | 163 (6%) | 179 (6%) | 658 (23%) | 1749 (60%) | 2911 |

Only 5–10% of gated ticks sit in the last bp under the guard. The marginal band
is thin everywhere; the guards are not narrowly missing, they are refusing
windows that are nowhere near the bar.

---

## 7. Ranked A/B candidates — where the experiments should aim

**None of these is a recommendation to deploy.** Each names the replay
experiment that would have to win first.

| # | candidate | measured opportunity | robust? | the experiment |
|---|---|---|---|---|
| 1 | **Latch releases on `safety ≥ theta && fair ≥ min_fair`**, not on `banked_decided` alone | +$136 / 9.8h and +$17 / 3.7h; 113 opens, 95% hit | ✅ all 3 conventions, both slices | **Needs a knob first.** The latch release is hardcoded; add a replay-only `Tunables.latch_release_safety` (same shape as `decided_k`), then `replay --mode full --params` base vs candidate over the stream-era corpus, bootstrap CI on ΔP&L (ROADMAP:82). Note ROADMAP:288's "never loosen the three brakes" — the latch is brake-adjacent and this A/B has to be argued as *overturning* that line, not slipping past it |
| 2 | **theta 0.30 → 0.25** | +$208 / 9.8h; 121 opens, 92% hit | ⚠️ band is real but theta's dear-side surplus sign-flips by slice | Runnable today: `theta` is an `ArmParams` field. `replay --mode full --params` with theta 0.30 / 0.28 / 0.25 / 0.20, same corpus, same everything else. Pair it with the 0.28 rung — it buys 94% of the money at half the loosening |
| 3 | **Fix `reference_wait`, don't relax it** | 13 whole-window write-offs incl. 3 arms at once; ~2% of armed windows | ✅ mechanism is certain | Not a gate A/B — a hub change: seed `per_min[start-60]` from the hub's `SymbolHistory` (or a REST backfill) when the live mark is missing, then `replay --mode full` over the 13 windows and confirm they price at all. The regression test is that they stop gating |
| 4 | **Maker bids on btc + eth** (they have none; sol/xrp do) | eth realizes 16% of a $110 clip, btc 64%; the fleet deploys $6.3k against 637 armed windows | ✅ depth measurement is exact | `maker_bid` is an `ArmParams` field: `replay --mode full` btc/eth with `maker_bid` on vs off over the book tape. `analysis/maker_grading.md` already has the grading harness |
| 5 | **15m at a real size with theta 0.3** | +$518 / 3.7h at a $24 clip — but 54% hit and 0 fires possible at $1 | ❌ counterfactual about an arm that does not exist | `analysis/fifteen_stream_ab.sh` already exists. Re-run it with `size_usdc`/`clip_usdc` at the pre-08:15Z values, theta 0.3 vs 1.0, `settle_rule` range_avg vs hybrid, over the 17:31Z+ corpus. Do not touch live 15m until it wins — `range_avg`'s 15m record is what shut these arms |
| 6 | **Basis guard −1bp** (btc 6→5, eth 8→7, sol 10→9) | +$529 raw, **+$83 after the theta gate** (22 clips) / 9.8h | ⚠️ small, and mostly re-blocked downstream | `basis_guard_bp` is an `ArmParams` field. Worth running only *after* #2 — the relaxation's value is bounded by theta, and 90% of what a 1bp loosen opens is refused again by the entry gate. ROADMAP:288 applies |
| 7 | **Basis guard carve-out on dear sides** (skip the guard when the refused side's ask ≥ 0.80) | +$85 / 9.8h, +$66 / 3.7h; 189 + 79 opens, 85% hit | ✅ all 3 conventions, both slices | Needs a knob (`guard_ask_bypass`). Robust but small; sequence it behind #1 and #2 |

**Explicitly NOT candidates.** `min_fair` has no marginal band at all — relaxing
0.97 → 0.85 opens **6** episodes worth **$2.21**, because min_fair refuses sides
the model priced at 0.00–0.20, not 0.96. It saved $3,074 doing so. `min_edge`
below 0.010 goes net-negative immediately. `distrust` and `avg_down` are
over-tight by +$256 and +$29 but are the two lines ROADMAP:288 names directly,
and their entry-price profile shows them refusing at 0.50–0.80, exactly where
the book-distrust lesson was learned. `chop`, `budget`, `cooldown` and `fleet`
are each worth under $100 over 9.8h.

---

## 8. Coverage and caveats

- **Coverage.** 3,923 of 4,242 gate-family episodes priced (92.5%). 67
  unresolved (windows still riding at the freeze), 252 unpriced (no ask recorded
  on the refused side — a gated tick logged before the book was subscribed).
  Unfilled fires: 111 (slug, side) groups, 73 filled clean, 38 with a priced gap.
- **One clip per refusal.** A window that entered would likely have fired
  several. Every number here is conservative in magnitude on both sides.
- **Clip sizing** is the window's own median real fire notional, else its
  (symbol, duration) median, else the arm's `clip_usdc`. It is measured, never
  assumed — which means it already carries the depth truncation of §4 #3.
- **`budget` is a lower bound.** Room is reconstructed as `cap − committed`; the
  tape does not carry inflight or resting notional, so it under-counts room and
  therefore under-counts how often budget binds.
- **`cooldown` is a residual** — `clip_cooldown_s` and the inflight set are the
  only blockers with no tape representation.
- **The last 20s of every window is invisible** (§1), as is `min_elapsed_frac`.
- **Blind gates are not P&L.** `feed_stale` and `reference_wait` price both
  sides by necessity; read their exposure table, not their net.
- The `guard_ladder`'s "after theta" column applies the R9 gate exactly
  (`side_safety` is computable from `banked_bp`/`cushion_bp` on a gated record)
  but **cannot** apply min_fair/min_edge — no `p_up` on a gated record — so
  those figures are upper bounds.

## 9. Reproduction

```bash
# frozen inputs, read-only over ~/.pmt (corpus refresh under a shadow $HOME)
python3 analysis/gate_shadow_2.py --self-check          # 1202 fires, 0 violations
python3 analysis/gate_shadow_2.py --json .work/ledger.json
python3 analysis/gate_shadow_2.py --trace eth-updown-5m-1787514300
python3 analysis/gate_shadow_2.py --trace btc-updown-5m-1787517900
```

Frozen at tape 21:15:34Z (66,224 records) · outcomes 1,134 terminal rows
(263 wallet / 871 resolution) · wallet walk 2,410 rows.
