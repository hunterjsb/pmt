# Should the 5m fleet move from `feed=binance` to `feed=rtds`?

Replay A/B, 2026-08-23. Drivers: `analysis/feed_ab.py` (corpus survey, guard
sizing, params), `analysis/feed_ab.sh` (the replay matrix),
`analysis/feed_ab_report.py` (roll-up), `analysis/feed_ab_lag.py` (relay lag
and staleness). Harness: `pmengine replay --mode full --fleet-cap 500` with
`--outcomes ~/.pmt/corpus/outcomes.jsonl` as ground truth on every leg, so
settlement is identical across arms and only the feed differs.

**Verdict in one line: yes for btc, eth and bnb — but only shipped together
with `--settle-tw 60`, which is not a detail. Without it the migration is
worse than doing nothing.** sol stays on binance. xrp is already on the
stream and should have its guard raised.

| symbol | verdict | change |
|---|---|---|
| **btc 5m** | **GO** | `--feed rtds --settle-tw 60`, guard stays 6 |
| **eth 5m** | **GO** | `--feed rtds --settle-tw 60`, guard **6 → 8** |
| **sol 5m** | **NO — stay binance** | no measurable benefit; guard 10 already clears the floor |
| **bnb 5m** | **GO (stage last)** | `--feed rtds --settle-tw 60`, guard stays 8 |
| **xrp 5m** | already rtds — **raise guard 12 → 16** | the live guard is below the stream's own noise floor |

Three findings matter more than the P&L table:

1. **`--settle-tw 60` is load-bearing, and the CLI says otherwise.** An rtds
   arm left at the engine's 5m default reads `crypto_prices_twap_thirty` —
   the wrong settlement series. Fleet-wide that is **−$630.67** against
   binance's −$269.49. With `--settle-tw 60` the same fleet is **+$517.50**.
   The `--settle-tw` help text says "Only terminal-aware paths consult it;
   range_avg arms ignore it", which is true of `terminal_lock` and false of
   `twap_topic_for`. §5.
2. **The stream does NOT fix the 17:15Z five-arm loss.** It survives every
   variant. Read off the settlement series itself, the market's rule says
   UP on all five and the arms' `range_avg` rule says DOWN on all five. That
   loss is a settlement-**rule** error, not a feed error, and no feed change
   reaches it. §4.
3. **The guard's remaining job is bigger than the basis.** Deleting the
   cross-venue term leaves 79–98% of the measured disagreement standing,
   because the arm still substitutes *spot* for a *60s TWAP*. Sized the way
   the binance guards were sized, the stream floor is btc 5 / eth 8 / sol 8 /
   bnb 5 / xrp 16 — which means eth and xrp are today trading **inside**
   their own noise band. §3.

---

## 1. Scope, and what the corpus actually supports

The brief said the recorder corpus starts ~11:29Z. It does not — it starts
**08:28:55Z**, and at the snapshot taken for this study it runs to
**20:03:39Z**: 11.58 hours, 898,086 lines, one file
(`~/.pmt/corpus/rtds/rtds-20260823.jsonl`). 11:30Z is the xrp *arm* cutover,
not the corpus start. The window set is correspondingly larger than the
brief assumed.

Every input was **frozen to a snapshot before the matrix ran** — the
recorder and both tapes append continuously, and two legs of an A/B reading
different corpora is not an A/B. The frozen set is the rtds corpus, the book
tape, the eval tape, `outcomes.jsonl` and `arms-state.json`.

**Comparable window set** — in the book tape, graded in `outcomes.jsonl`, and
fully inside the corpus (its settlement reference through its close):

| symbol | comparable | span | refused for want of corpus | ungraded |
|---|---|---|---|---|
| btc 5m | 131 | 08:30–19:30Z | 69 | 4 |
| eth 5m | 131 | 08:30–19:30Z | 69 | 4 |
| sol 5m | 131 | 08:30–19:30Z | 69 | 4 |
| bnb 5m | 131 | 08:30–19:30Z | 11 | 4 |
| xrp 5m | 96 | 11:30–19:30Z | 0 | 4 |

**Refusals are real and counted by the harness, not by this write-up.** A
census run over every graded 5m window in the book tape, stream-fed, is the
control:

```
census-rtds : 218 window(s) refused for want of corpus  (btc 69, eth 69, sol 69, bnb 11)
census-base : 0
```

Both census runs skip the same 116 out-of-scope windows: 86 at 15m (not in
scope) and 30 5m windows from 19:35–20:00Z that were still unresolved when
the snapshot was frozen. So the stream-fed leg can answer for 620 of the 838
graded 5m windows, and the binance leg can answer for all 838. The
A/B is run on the 620 the two can both reach; the baseline is restricted to
exactly that set, so no leg gets windows the other cannot see.

### Recorder gaps are the recorder's, not the engine's

The recorder is a second subscriber. Its dropped samples do not mean the
live arm was blind. Two classes:

- **Self-logged stalls**: 35 `gap` events, 1,278s total, 32–52s each.
- **Holes the recorder could not log about itself**: found by spot-topic
  spacing instead. Total **65 holes, 2,304s**, and the worst is a
  **721.7s outage at 08:44:15–08:56:17Z** whose only trace in the file is
  the next `start` event — the process was dead, so it logged no gap.
  `analysis/feed_ab.py:data_gaps` derives holes from the data for this
  reason.

Effect on the result: 16 comparable windows overlap the 722s hole; between
them the baseline nets +$2.91 and the stream leg +$17.68. Two windows
(`eth-updown-5m-1787474700`, `sol-updown-5m-1787475300`) are silenced on the
stream leg purely because the recorder was down — about $3 of foregone win.
Immaterial. Across all 65 holes, 58/131 windows per symbol are touched at
some point, but a touched window is not a lost window: the arm gates for the
duration and resumes.

**Short warmup**: `HISTORY_WARMUP_S` is 7,200s, so the 23 windows per symbol
starting before 10:28:55Z replay off a shorter tape than the live arm read,
which moves `rho` and the slow sigma. Replay says so on stderr for each. The
warm-only subset (108 windows for btc/eth/sol/bnb; xrp's 96 are all warm) is
reported alongside every headline and does not change any verdict.

---

## 2. Baseline fidelity — read the deltas, not the dollars

| symbol | live fires | sim fires | live notional | sim notional |
|---|---|---|---|---|
| btc | 29 | 39 | $2,321 | $3,641 |
| eth | 81 | 217 | $2,479 | $6,635 |
| sol | 61 | 130 | $1,387 | $3,514 |
| bnb | 21 | 32 | $160 | $279 |
| xrp | 60 | 123 | $441 | $1,025 |

Replay fills every `decide()` Buy instantly at the quoted price. It has no
order latency, no delta-matcher suppression and no unfilled marketable
limits, so it fires 1.3–2.7× more clips than the live arm did. Mean clip
size matches to the cent (eth: $30.6 sim vs $30.6 live), so this is a fire-
*count* gap, not a sizing error.

Consequence: **every absolute dollar below is the harness's, not the
wallet's, and is inflated.** The comparison is still sound — the same
optimism applies to both arms of every pair — but do not read "+$517 fleet"
as money that was available.

Per-window `size_usdc` comes from the eval tape's own `roll` records (the
operator moved btc 400→1000 and eth 350→900 across the day); `clip_usdc` is
not recorded, so it is read back as the max fire notional at each size
level. Both are identical across every leg.

---

## 3. Sizing a stream-appropriate guard

### Method, and why it is the same method

`analysis/chainlink_stream_scout.md` sized the deployed guards at ~p90 of
the disagreement, at one instant, between the series the arm prices off and
the series the market settles on. That is the quantity `basis_guard_bp`
exists to absorb.

On rtds the **venue** term of that disagreement is identically zero — the
arm reads the settlement object. What survives is the arm's one remaining
substitution: it prices the unformed remainder of the window off Chainlink
**spot** where the settlement quantity is that spot's **60s trailing mean**.
So: measure that, the same way, on the same clock, with the same estimator.
Three quantities at 1 Hz over the corpus, recorder holes and stale
counterparts excluded (a live arm gates there rather than pricing, so
charging the guard for it would double-count the stale gate):

| sym | `\|chainlink spot − twap60\|` (**rtds**) | `\|binance spot − twap60\|` (binance) | `\|binance − chainlink spot\|` (pure venue) |
|---|---|---|---|
| | med / p90 / p99 | med / p90 / p99 | med / p90 / p99 |
| btc | **1.31 / 4.40 / 10.64** | 2.10 / 5.58 / 12.53 | 1.81 / 2.50 / 3.83 |
| eth | **2.08 / 7.03 / 22.33** | 2.53 / 7.98 / 23.87 | 1.48 / 2.56 / 5.62 |
| sol | **2.82 / 7.94 / 18.33** | 3.35 / 9.21 / 20.90 | 2.01 / 3.27 / 6.09 |
| bnb | **1.71 / 4.94 / 10.81** | 2.37 / 5.78 / 11.70 | 1.63 / 2.33 / 3.89 |
| xrp | **5.39 / 15.73 / 32.85** | 5.42 / 15.98 / 34.27 | 0.55 / 3.98 / 10.88 |

**The method validates against what is already deployed.** The middle column
reproduces the scout's independently-measured rung-4 p90s closely — btc 5.58
vs 6.39, eth 7.98 vs 9.50, sol 9.21 vs 11.20, bnb 5.78 vs 7.33 — and the
deployed guards (btc 6, sol 10, bnb 8) sit right on those numbers. A method
that recovers the existing calibration can be trusted to size the new one.

**The uncomfortable part:** moving to the stream removes far less of the
guard than the framing suggests. At p90 the reduction is btc 5.58→4.40
(−21%), eth 7.98→7.03 (−12%), sol 9.21→7.94 (−14%), bnb 5.78→4.94 (−15%),
xrp 15.98→15.73 (−2%). The cross-venue basis was never the big term; the
spot-for-TWAP substitution is, and the stream does not touch it. CLAUDE.md's
"the cross-venue basis the guard was sized for disappears" is true and
accounts for ~2–3bp of a 4–16bp guard.

### Relay lag is inside the measurement, not assumed away

Every reading above is taken on the **as-received** series (`StepSeries.at`
keyed on `t_recv`), so the ~1.7s relay lag is already priced into the p90 —
nothing here assumes 0-age marks. Measured separately, the lag itself is
tiny in bp:

| sym | lag p50 | p90 | p99 | max | bp cost of the lag (med / p90) |
|---|---|---|---|---|---|
| btc | 1.664s | 2.145 | 2.580 | 5.779 | 0.037 / 0.146 |
| eth | 1.664s | 2.147 | 2.578 | 5.779 | 0.060 / 0.244 |
| sol | 1.663s | 2.149 | 2.578 | 5.778 | 0.077 / 0.276 |
| bnb | 1.656s | 2.140 | 2.569 | 5.779 | 0.046 / 0.167 |
| xrp | 1.657s | 2.139 | 2.564 | 5.779 | 0.153 / 0.541 |

(The coordinator's issue-4 figures — p50 1,676ms, p99 2,616ms — reproduce
here independently over a longer span. Our max is higher, 5.78s, because the
span covers more of the day.)

Even at xrp, the worst symbol, the lag is worth 0.54bp at p90 against a
15.73bp guard: **3% of the band**. It is not what the guard is for.

### The guards

Deployed guards round p90 to a whole bp, so:

| sym | live guard | stream noise floor (⌈p90⌉) | live clears the floor? | recommended |
|---|---|---|---|---|
| btc | 6.0 | **5.0** | yes | **6.0** (unchanged) |
| eth | 6.0 | **8.0** | **NO — 25% inside its own noise** | **8.0** |
| sol | 10.0 | **8.0** | yes | **10.0** (unchanged) |
| bnb | 8.0 | **5.0** | yes | **8.0** (unchanged) |
| xrp | 12.0 | **16.0** | **NO — 25% inside its own noise** | **16.0** |

The noise measurement sets a **floor**, not a target. It says how thin a
margin the feed can no longer distinguish from nothing; it says nothing
about whether a margin the feed *can* resolve is worth trading. So the
recommended guard is `max(live, floor)` — never looser than the measurement
supports and never looser than what is deployed today. Two symbols move, and
both move **up**.

The A/B runs both sub-variants the brief asked for plus that composite:

- `rtds_liveguard` — feed changes, guard untouched. The pure feed effect.
- `rtds_streamguard` — guard set exactly to the measured floor. For btc, sol
  and bnb this *loosens* the guard below what is deployed.
- `rtds_floorguard` — `max(live, floor)`. The recommendation.
- `rtds_tw30` — feed changes, guard untouched, `settle_tw_s` left at the
  engine's 5m default. The control that turned out to matter most.

---

## 4. The 17:15Z five-arm loss — the stream does not touch it

`1787505300` is 17:15–17:20Z. All five 5m arms fired **down**; all five
settled **up**. Under the baseline it is **$676 of the $716 the whole fleet
loses across 620 windows.**

| symbol | truth | binance | rtds tw30 | rtds live guard | rtds stream guard | rtds floor guard |
|---|---|---|---|---|---|---|
| btc | up | −113.69 | −113.24 | −113.39 | −113.39 | −113.39 |
| eth | up | −443.63 | −337.30 | −328.03 | −328.03 | −328.03 |
| sol | up | −80.77 | −88.67 | −80.52 | −204.68 | −80.52 |
| bnb | up | −9.88 | 0.00 | −9.87 | −19.34 | −9.87 |
| xrp | up | −27.27 | −51.79 | −51.77 | −9.02 | −9.02 |
| **total** | | **−675.24** | **−591.00** | **−583.58** | **−674.46** | **−540.83** |

**No variant refuses it.** btc, eth, sol and xrp fire into it on every
single leg; only bnb ever escapes, and only under the tw30 leg that is worse
everywhere else. The recommended configuration trims the bill 20%, by sizing
into it slightly less — not by seeing it coming.

### Why: it is a rule error, and the rule is ours

Read directly off the settlement series, with no Binance anywhere — the 60s
TWAP print at range end against the print at range start, which is the rule
`analysis/settle_width.md` validated at 283/284:

| sym | twap60 @start | twap60 @end | **terminal margin (the market's rule)** | **range_avg margin (the arms' rule)** | truth |
|---|---|---|---|---|---|
| bnb | 699.609 | 699.875 | **+3.81 bp** | −6.65 bp | up |
| btc | 77286.4 | 77342.7 | **+7.29** | −7.65 | up |
| eth | 2447.65 | 2448.21 | **+2.28** | −18.75 | up |
| sol | 95.3658 | 95.4418 | **+7.97** | −11.36 | up |
| xrp | 1.51756 | 1.51924 | **+11.09** | −16.88 | up |

**5/5 to the terminal rule, 0/5 to range_avg**, on data the stream-fed arm
had in hand. The eth trace shows the shape — a V:

```
-60s 2448.789   +0s 2447.654   +60s 2442.975   +120s 2441.830
                              +180s 2443.167  +240s 2445.367  +300s 2448.211
```

The price dipped ~28bp through the middle and recovered to within 0.2bp of
where it started. A whole-range average reads the dip and calls it down; the
close reads flat-to-up, and the close is what settles.

This is the `range_avg`-vs-`terminal` error `analysis/correlation_study.md`
Result 0 already found (terminal_t60 288/289 vs terminal 282/289) and
`analysis/settle_width.md` re-confirmed. **17:15Z is that error costing $676
in one minute across five arms simultaneously — and it is a correlated
event, because all five arms run one rule.** The feed question cannot reach
it. Migrating the fleet to rtds without also fixing the settlement rule
leaves the single largest loss in the sample exactly where it is.

---

## 5. `--settle-tw 60` is not optional

For a stream-fed arm, `settle_tw_for(p)` reaches `twap_topic_for`, which
picks `crypto_prices_twap_thirty` at width ≤ 30 and `crypto_prices_twap_sixty`
above it. Left at the default, a 5m arm gets `settle_tw_secs(300) = 30` and
therefore prices off `twap_thirty` — a real, correctly-stamped series that
`analysis/settle_width.md` showed is **not** what these markets settle on.

| leg | fleet net | fleet RoN | vs binance |
|---|---|---|---|
| binance (baseline) | −$269.49 | −1.86% | — |
| **rtds, default settle_tw (reads twap_thirty)** | **−$630.67** | **−3.22%** | **−$361.18** |
| rtds, `--settle-tw 60`, live guards | +$517.50 | +3.23% | +$786.99 |
| rtds, `--settle-tw 60`, floor guards | +$138.16 | +0.99% | +$407.65 |

The tw30 leg also produces the sample's worst single window,
`eth-updown-5m-1787494500` at **−$1,022**, which no other leg loses at all.

**The `--settle-tw` help text is wrong for stream-fed arms.** It reads "Only
terminal-aware paths consult it; range_avg arms ignore it." That is true of
`terminal_lock`, and false of the topic selection — which is exactly the
path a range_avg *rtds* arm depends on. Not changed here (this branch ships
no code), but it is a documentation bug with $361 attached and it belongs on
the backlog.

---

## 6. The A/B

`--fleet-cap 500` on every run. Per-symbol runs cap that symbol alone; the
fleet run interleaves all five under one shared pool.

### Fleet (620 windows, one shared $500 cap)

| variant | windows fired | clips | W-L | net $ | notional $ | RoN | Δnet | ΔRoN |
|---|---|---|---|---|---|---|---|---|
| binance (baseline) | 112 | 521 | 106-6 | −269.49 | 14,467.50 | −1.86% | — | — |
| rtds_tw30 | 126 | 684 | 119-7 | −630.67 | 19,608.97 | −3.22% | −361.18 | −1.35pp |
| rtds_liveguard | 116 | 558 | 110-6 | **+517.50** | 16,011.71 | +3.23% | +786.99 | +5.09pp |
| rtds_streamguard | 134 | 612 | 125-9 | −1.27 | 17,500.98 | −0.01% | +268.22 | +1.86pp |
| **rtds_floorguard** | 95 | 442 | 89-6 | **+138.16** | 14,024.20 | +0.99% | +407.65 | +2.85pp |

### Per symbol (comparable set)

| symbol | variant | fired | clips | W-L | net $ | notional $ | RoN | Δnet | ΔRoN |
|---|---|---|---|---|---|---|---|---|---|
| **btc** | binance | 12 | 39 | 11-1 | +6.86 | 3,641.29 | +0.19% | — | — |
| | rtds_tw30 | 21 | 69 | 19-2 | −94.00 | 5,633.43 | −1.67% | −100.85 | −1.86pp |
| | **rtds_liveguard = floorguard** | 18 | 62 | 17-1 | **+108.55** | 5,306.95 | +2.05% | **+101.69** | +1.86pp |
| | rtds_streamguard (guard 5) | 22 | 76 | 19-3 | +7.37 | 6,807.26 | +0.11% | +0.51 | −0.08pp |
| **eth** | binance | 33 | 217 | 32-1 | −298.68 | 6,635.16 | −4.50% | — | — |
| | rtds_tw30 | 37 | 290 | 34-3 | −708.30 | 9,206.72 | −7.69% | −409.62 | −3.19pp |
| | rtds_liveguard (guard 6) | 37 | 221 | 36-1 | +321.66 | 6,286.38 | +5.12% | +620.35 | +9.62pp |
| | **rtds_floorguard (guard 8)** | 23 | 140 | 22-1 | **−40.93** | 4,543.00 | −0.90% | **+257.75** | +3.60pp |
| **sol** | binance | 29 | 130 | 28-1 | **+79.14** | 3,514.28 | **+2.25%** | — | — |
| | rtds_tw30 | 30 | 136 | 29-1 | +37.68 | 4,001.96 | +0.94% | −41.46 | −1.31pp |
| | rtds_liveguard = floorguard | 26 | 121 | 25-1 | +21.73 | 3,452.01 | +0.63% | **−57.40** | −1.62pp |
| | rtds_streamguard (guard 8) | 38 | 172 | 37-1 | −22.66 | 4,896.19 | −0.46% | −101.79 | −2.71pp |
| **bnb** | binance | 9 | 32 | 8-1 | −3.88 | 279.34 | −1.39% | — | — |
| | rtds_tw30 | 14 | 71 | 14-0 | +28.77 | 609.07 | +4.72% | +32.64 | +6.11pp |
| | **rtds_liveguard = floorguard** | 11 | 48 | 10-1 | **+10.75** | 431.61 | +2.49% | **+14.63** | +3.88pp |
| | rtds_streamguard (guard 5) | 35 | 159 | 33-2 | +22.26 | 1,311.12 | +1.70% | +26.14 | +3.09pp |
| **xrp** | binance | 29 | 123 | 27-2 | −39.28 | 1,024.74 | −3.83% | — | — |
| | rtds_tw30 | 24 | 131 | 23-1 | +121.84 | 1,053.88 | +11.56% | +161.13 | +15.39pp |
| | rtds_liveguard (guard 12) | 24 | 114 | 22-2 | +62.91 | 885.04 | +7.11% | +102.19 | +10.94pp |
| | **rtds_floorguard (guard 16)** | 17 | 78 | 15-2 | **+46.03** | 632.05 | +7.28% | **+85.31** | +11.12pp |

The 108-window warm-history subset moves nothing: every sign and every
verdict is the same there.

### What actually decides the P&L: six windows

The fleet enters 112 windows and wins 106 of them. The entire result is the
loss list.

| variant | losing windows | total loss | winning windows | total win |
|---|---|---|---|---|
| binance | 6 | −$716.26 | 106 | +$460.42 |
| rtds_tw30 | 7 | −$1,957.86 | 119 | +$1,343.86 |
| rtds_liveguard | 6 | −$614.42 | 110 | +$1,140.03 |
| rtds_streamguard | 9 | −$809.67 | 126 | +$821.75 |
| rtds_floorguard | 6 | −$545.76 | 89 | +$691.89 |

Five of the six baseline losses are the *same minute*, 17:15Z, on five
symbols. So the honest reading of the whole study is: **the stream leaves the
losses roughly where they were and improves the win side** — binance +$460
vs floorguard +$692 on slightly *less* notional. That is a real improvement
and it is not the improvement the migration was pitched on.

Note also that `rtds_streamguard` — the guard set exactly to the measured
floor, which *loosens* btc 6→5, sol 10→8 and bnb 8→5 — is the only leg that
grows the loss count, from 6 to 9. Loosening a guard to its noise floor
finds new losers. That is the evidence for `max(live, floor)`.

### Robustness — the deltas are concentrated

| symbol | variant | windows moved | Δ>0 | Δ<0 | Δnet | Δnet less top mover |
|---|---|---|---|---|---|---|
| btc | rtds_liveguard | 20 | 13 | 7 | +101.69 | **+17.54** |
| eth | rtds_liveguard | 45 | 23 | 22 | +620.35 | +250.65 |
| eth | rtds_floorguard | 38 | 12 | 26 | +257.75 | +101.54 |
| sol | rtds_liveguard | 31 | 13 | 18 | −57.40 | **+3.62** |
| bnb | rtds_liveguard | 10 | 7 | 3 | +14.63 | **+0.91** |
| xrp | rtds_liveguard | 33 | 13 | 20 | +102.19 | **−20.78** |
| xrp | rtds_floorguard | 32 | 9 | 23 | +85.31 | +38.24 |

Read this honestly:

- **btc, bnb and sol are one-window results.** Drop the largest mover and
  btc's +$102 becomes +$18, bnb's +$15 becomes +$1, sol's −$57 becomes +$4.
  None of the three has a P&L case; sol's negative is as fragile as btc's
  positive.
- **xrp's live-guard gain reverses sign** when the top window is dropped
  (+$102 → −$21). At the floor guard it holds (+$85 → +$38). That is a
  second, independent reason to raise xrp's guard.
- **Only eth survives the jackknife with room to spare** at both guards.
- **No sign test is significant.** btc 13/7 is p≈0.13 one-sided; eth is
  23/22, a coin flip. The gains live in a few large windows, not in a broad
  tilt.

So the P&L is supporting evidence, not proof. The load-bearing argument for
migrating is mechanistic — the arm prices the series the market settles on,
and its reference and banked marks stop carrying a venue error — plus the
fact that the measured downside is confined to one symbol (sol) where the
effect is indistinguishable from zero.

---

## 7. Staleness: MAX_SPOT_AGE_S ∈ {3, 5} changes nothing

`route_sample` stamps `spot_ts = now`, the **local receipt** time. So
`MAX_SPOT_AGE_S` (5.0s) measures age-since-receipt and is structurally blind
to the ~1.7s relay lag in front of it: a print already 1.7s old by its own
clock registers as 0s old the instant it lands. The true age of a priced
mark is receipt age + relay lag. The relay half has its own bound —
`MAX_SAMPLE_LAG_S = 10.0` drops an over-lagged spot sample, which freezes
`spot_ts` and lets the 5s gate bind — so the design ceiling on true mark age
is **15s**, and today's observed ceiling is 5 + 5.78 = 10.8s.

Measured at the timestamps replay actually fired on:

| variant | fires timed | receipt age p50/p99/max | true age p50/p99/max | receipt >3s | receipt >5s |
|---|---|---|---|---|---|
| rtds_tw30 | 126 | 0.51 / 1.70 / **2.26** | 2.19 / 3.61 / 3.69 | **0** | **0** |
| rtds_liveguard | 116 | 0.50 / 1.62 / **1.67** | 2.18 / 3.32 / 3.32 | **0** | **0** |
| rtds_streamguard | 135 | 0.51 / 1.67 / **2.00** | 2.19 / 3.34 / 3.42 | **0** | **0** |
| rtds_floorguard | 95 | 0.51 / 1.67 / **1.67** | 2.19 / 3.34 / 3.34 | **0** | **0** |

**No replayed fire, in any variant, opened on a mark older than 3s.** The
A/B verdict is completely insensitive to MAX_SPOT_AGE_S ∈ {3, 5}.

That table times one clip per window (`first_fire_t` is the only fire
timestamp the report carries), so here is the same question from the other
side — receipt age across *every* armed tick in the comparable set:

| sym | armed ticks | receipt age p50 / p90 | in (3, 5] | >5s (already gated) |
|---|---|---|---|---|
| btc | 15,099 | 0.56 / 1.36 | 43 (0.28%) | 655 (4.34%) |
| eth | 15,095 | 0.56 / 1.36 | 42 (0.28%) | 657 (4.35%) |
| sol | 15,093 | 0.56 / 1.36 | 43 (0.28%) | 658 (4.36%) |
| bnb | 15,090 | 0.56 / 1.36 | 42 (0.28%) | 654 (4.33%) |
| xrp | 10,667 | 0.56 / 1.29 | 27 (0.25%) | 265 (2.48%) |

The (3, 5] band is **0.25–0.28% of armed time** because the distribution is
bimodal: at 1 Hz the receipt age is under ~1.4s essentially always, and when
the stream stalls it goes straight past 5s (32–52s stalls, plus the 722s
outage). There is almost no population between 3 and 5, which is why the
choice cannot move the result. 4.3% of armed ticks are already gated at 5s —
the staleness gate is doing real work, just not near its threshold.

**Nothing was changed.** `MAX_SPOT_AGE_S` and `MAX_SAMPLE_LAG_S` are
untouched. What the measurement does justify recording: the gate's blindness
to relay lag means the effective worst-case price age is `MAX_SPOT_AGE_S +
MAX_SAMPLE_LAG_S`, and if a tighter true-age bound is ever wanted, the lever
is `MAX_SAMPLE_LAG_S` (10s, currently 1.7× the worst lag observed) rather
than `MAX_SPOT_AGE_S`.

---

## 8. Verdicts and the exact commands

Every command reproduces the arm's live params, changing only what this
study justifies. `--min-elapsed 0`, `--clip`, `--theta`, `--pay-up` and
`--basis-guard` all differ from the CLI defaults and must be passed
explicitly. `<url|slug>` is the current window in the series; the arm rolls
from there.

### GO — btc 5m (guard unchanged)

```
pmt crypto arm <url|slug> --size 1000 --clip 150 --min-elapsed 0 \
  --theta 0.3 --pay-up 0.05 --basis-guard 6 \
  --feed rtds --settle-tw 60
```

Δnet +$101.69, RoN +0.19% → +2.05%. Guard 6 already clears its 5.0 floor, so
the feed is the only thing that moves. Evidence is one window deep
(+$17.54 ex-top) — the case is mechanistic, and the risk is bounded because
no gate changes.

### GO — eth 5m (guard 6 → 8)

```
pmt crypto arm <url|slug> --size 900 --clip 110 --min-elapsed 0 \
  --theta 0.3 --pay-up 0.05 --basis-guard 8 \
  --feed rtds --settle-tw 60
```

Δnet +$257.75, RoN −4.50% → −0.90%, and the only symbol whose gain survives
the jackknife comfortably (+$101.54 ex-top). **The guard raise is the
independently justified half**: eth's live 6.0 sits 15% below its own measured
stream noise floor of 7.03bp, and below the binance floor (7.98) too —
so 6.0 is under-guarded on *either* feed and should go to 8 regardless of
what happens to the feed.

On the day, keeping eth at guard 6 on the stream earned more (+$321.66 vs
−$40.93). Take that as unvalidated: those 14 extra windows are trades taken
inside a band the feed cannot resolve, and one day of 14-0 is not evidence
that it can.

### GO, stage last — bnb 5m (guard unchanged)

```
pmt crypto arm <url|slug> --size 100 --clip 10 --min-elapsed 0 \
  --theta 0.3 --pay-up 0.02 --basis-guard 8 \
  --feed rtds --settle-tw 60
```

Δnet +$14.63 on a $279 baseline — the sign is right and the guard is
untouched, but the sample is 9 baseline windows and +$0.91 ex-top. This is
"no reason not to", not "it works". Its $100 size bounds the cost of being
wrong; move it after btc and eth have run live for a day.

### NO — sol 5m stays on binance

sol is the only symbol where the stream is negative (Δnet −$57.40, RoN
+2.25% → +0.63%), and it is also the fleet's best baseline RoN. Ex-top the
delta is +$3.62, i.e. indistinguishable from zero — the −$57 is one window
(14:05Z) where the stream arm simply took less of a winner, not a loss it
walked into. There is no evidence of harm and no evidence of benefit, so the
incumbent stays. sol's guard (10) already clears its 8.0 floor, so nothing
changes.

Revisit when the corpus covers more than one day.

### xrp 5m — already rtds; raise the guard 12 → 16

```
pmt crypto arm <url|slug> --size 100 --clip 10 --min-elapsed 0 \
  --theta 0.3 --pay-up 0.02 --basis-guard 16 --maker-bid \
  --feed rtds --settle-tw 60
```

The only change is the guard. xrp's live 12.0 sits 24% below its measured
stream floor of 15.73bp — the *thinnest* guard on the board against the
*widest* noise. Replay agrees twice over: at guard 16 the delta holds under
jackknife (+$85.31, +$38.24 ex-top) where at guard 12 it reverses sign
(+$102.19, −$20.78 ex-top), and 17:15Z costs −$9.02 instead of −$51.77.

`analysis/maker_grading.md` §3.3 called xrp "the tightest basis guard on the
board" and read that as conservative. On the stream it is the opposite: it is
the only guard, other than eth's, that is below its own noise.

### Fleet-wide, and not negotiable

**`--settle-tw 60` ships with every one of these commands.** An rtds 5m arm
without it reads `twap_thirty` and is worse than binance by $361 fleet-wide
(§5). If the migration is staged, stage the flag pair together — never the
feed alone.

---

## 9. Weaknesses

- **One day, and the deltas are concentrated.** 620 windows over 11.6h in one
  volatility regime. Drop one window and btc/bnb/sol/xrp all lose their
  headline (§6). No sign test is significant. The measured guard p90s moved
  by a whole bp for eth when the corpus grew by 40 minutes mid-study — these
  are one-day estimates, not constants.
- **Absolute dollars are the harness's, not the wallet's.** Replay fills
  every decision instantly and fires 1.3–2.7× more clips than the live arm
  (§2). Deltas are comparable; levels are not.
- **`clip_usdc` is inferred**, not recorded — read back as the max fire
  notional at each `size_usdc` level. Identical across every leg, so it
  cannot bias a delta, but it can misstate a level.
- **`sigma_bp_per_min` is the arm's current value on every window**, since
  the per-window value is not recorded anywhere. It is only the cold-start
  fallback for the vol floor and should rarely bind after warmup, but it is
  an approximation.
- **The guard measurement is deliberately conservative.** `|spot − twap60|`
  is the substitution error at full weight, but it enters the projected
  margin scaled by `rem/window`, so it bites early in a window and is damped
  late. The guards derived from it are therefore tighter than strictly
  needed near the close — the same conservatism the binance guards carry.
- **The 23 earliest windows per symbol replay off short RTDS history**, which
  moves `rho` and the slow sigma. The warm-only subset agrees with every
  headline, so this is bounded, not absent.
- **Recorder holes total 2,304s** and touch 58/131 windows per symbol at some
  point. The 722s one silences two stream-leg windows outright (~$3). The
  recorder is a second subscriber, so none of this reflects what the live
  arm saw.
- **The 17:15Z result is one event.** It is decisive about *that* event —
  the settlement rule reads it backwards on the settlement data — but five
  correlated symbols in one minute is n=1 for the rule question, not n=5.
  The rule case rests on `correlation_study.md` and `settle_width.md`; this
  is a costed instance of it.
- **`~/.pmt` write.** The first matrix run appended missing 1m klines to
  `~/.pmt/corpus/klines-1m-{BTC,ETH,SOL,BNB,XRP}USDT.jsonl` (~22KB each) —
  `replay --mode full` does that by design when its cache does not cover the
  span. Additive public market data; no engine, config or state file was
  touched, and the kline cache is read only by replay, never by the engine.
  Every run after that goes through a shadow `HOME` holding a copy of the
  cache (`analysis/feed_ab.sh:SHADOW_HOME`), and the re-run under it produced
  byte-identical reports.

## 10. Backlog this produced

1. **Fix the `--settle-tw` help text** — "range_avg arms ignore it" is false
   for stream-fed arms and is worth $361 fleet-wide (§5).
2. **Refuse an rtds 5m arm without `settle_tw_s ≥ 60`**, or default
   `settle_tw_secs` to 60 as `analysis/settle_width.md` §Consequence already
   recommends. The current default silently selects the wrong settlement
   series for exactly the arms that care most.
3. **The settlement rule, not the feed, is where the money is.** 17:15Z is
   $676 of a $716 loss column and no feed change reaches it. A
   `settle_rule = terminal` A/B at `settle_tw 60` is the natural successor to
   this study.
4. **Re-measure the guards on a multi-day corpus.** The p90s moved a whole bp
   inside this session; eth 6→8 and xrp 12→16 both deserve confirmation.
5. **Consider `MAX_SAMPLE_LAG_S`, not `MAX_SPOT_AGE_S`**, if a tighter bound
   on true mark age is ever wanted (§7).
