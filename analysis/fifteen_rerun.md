# Should btc/eth 15m re-open, and on which settlement rule?

Replay A/B, 2026-08-23, snapshot frozen 21:01:27Z. Drivers:
`analysis/fifteen_rerun.py` (`survey` / `report` / `depth`) and
`analysis/fifteen_rerun.sh` (the matrix). Harness:
`pmengine replay --mode full --fleet-cap 500` with
`--outcomes` as ground truth on every leg, so settlement is identical
across arms and only feed / `settle_rule` / guard differ.

This is the study `analysis/feed_ab.md` §10.3 owed — *"a `settle_rule =
terminal` A/B at `settle_tw 60` is the natural successor"* — and the one
`analysis/fifteen_stream_fit.md` §4 could not run, because 15m book coverage
ended 08:08:57Z and the RTDS corpus began 08:28:55Z. §4 asked for a
books-only observer arm. It went up at **17:06:36Z**. The two tapes now
overlap and the A/B is runnable for the first time.

---

**Verdict in one line: btc and eth 15m stay SHUT, and the observer arms stay
ARMED — they are not idling, they are the only 15m book recorder we have,
and they are provably incapable of firing. The rule gap is real and
confirmed in-sample, but neither rule that closes it can trade a 15m window
at all: `terminal` fires ZERO clips in 48 windows and no book-side gate is
what stops it.**

| question | answer |
|---|---|
| re-open btc 15m? | **NO** |
| re-open eth 15m? | **NO** |
| re-open sol 15m? | **NO** — refused by the vol study before this one started |
| disarm the 15m arms? | **NO — keep them armed as observers.** Three locks, one unconditional; 0 fires over 48 replayed windows |
| is `settle_rule=terminal` the fix? | it reads the settlement right (**42/48 vs range_avg's 31/48**, and **11/11** of the disagreements) and **cannot reach a single trade** |

Four findings, in the order they matter:

1. **The zero-fire premise was wrong about the cause.** The 15m arms are not
   inert "under binance + range_avg + theta + decided_k". They are
   **books-only observer arms** — `size_usdc 1.0`, `clip_usdc 1.0`,
   `min_fair 1.0`, `theta 1.0` — exactly what `fifteen_stream_fit.md` §4
   prescribed. `room` never exceeds $1 and `sized()` refuses anything under
   $5, so no clip can fire on any feed under any rule. Replaying them
   byte-for-byte over 48 windows returns 0 fires. §1.
2. **`settle_rule = terminal` is unreachable at 15m, and θ is why.**
   `terminal_lock` banks *nothing* while `rem > tw`, so `banked_margin_bp`
   and `side_safety` are identically 0 for 14 of every 15 minutes, and
   `safety_gate_blocks` refuses the first clip of a window whenever
   `safety < theta`. **93% of a 15m window is closed by construction.** In
   the remaining 60s the book has withdrawn: the winning side has a buyable
   ask on **10.8%** of ticks against **99.9%** at `rem > 300`. Opening every
   book-side gate at once — `min_fair 0.5`, `min_edge 0.001`,
   `max_price 0.999`, `quiesce 0`, `cooldown 0` — still yields **zero**
   fires. Only θ opens it, and only at exactly θ=0; even θ=0.05 is zero. §4.
3. **The rule gap is real, in-sample, and costed.** Over the 48 windows the
   terminal rule grades **42/48 (87.5%)** against `range_avg`'s **31/48
   (64.6%)**; the two disagree on 11 windows and **terminal is right on all
   11**. `eth-updown-15m-1787508000` (18:00Z) is the instance with a price
   on it: terminal **+23.15bp UP**, range_avg **−1.26bp DOWN**, settled
   **UP**, and the baseline arm bought down and lost **−$50.02 on a single
   clip** — 100% of the clip. §5.
4. **`settle_rule` is not an arm command.** There is no `--settle-rule` flag
   in `pmt crypto arm`; `ArmParams::settle_rule` has a `serde(default)` of
   `"range_avg"` and lives only in replay params. So "re-open 15m on
   terminal" is not a thing an operator can type today, and this A/B says
   there is nothing to type it for. §7.

---

## 1. What the live 15m arms actually are

`arms-state.json` at the snapshot, all three 15m arms:

```
size_usdc 1.0   clip_usdc 1.0   min_fair 1.0   theta 1.0
feed binance    settle_rule range_avg   settle_tw_s 0.0
basis_guard_bp  btc 6.0 / eth 8.0 / sol 10.0    roll true
```

That is the observer arm `fifteen_stream_fit.md` §4 specified ("armed so
that no clip can ever fire — `--size 0`, or `--min-fair 1.0`"). Three
independent locks, and the first is unconditional:

- **`size_usdc 1.0`.** `cap = size_usdc × (1.0 unlocked | early_frac 0.2)`,
  `room = (cap − committed − inflight − resting).min(budget)`, and
  `sized(r)` returns 0 unless `r > 5.0`. Room can never exceed $1. **No
  clip can fire at any point in any window, on any feed, under any rule.**
- **`min_fair 1.0`.** In the unlocked phase `fair_req = min_fair`, and
  `fair` is capped at `p_cap = 1.0`, so `fair >= 1.0` needs fair to be
  *exactly* 1.0. (Before the unlock `fair_req` is `EARLY_MIN_FAIR = 0.55`,
  so this lock is the last-120s one, not the whole window.)
- **`theta 1.0`.** The first clip of a window needs `safety >= 1.0`.

Replaying the arms **exactly as armed** over the 48 comparable windows:

```
asarmed : 48 window(s), 0 fired, 0 clips, net $0.00
```

So the 6,722 eval+gated records the arms have written since 17:06Z are the
observer doing its job, not a strategy declining to trade. Two corrections
to the framing worth carrying forward:

- **The arms went up at 17:06:36Z, not 13:00Z.** The 15m tape is dark
  08:08:57Z → 17:06:36Z (8h58m). As of the snapshot they had been up
  **3h55m**, not eight hours.
- **They are the reason this study exists.** Every replayable 15m window in
  the corpus is a window an observer arm subscribed. Disarming them stops
  the only 15m book recorder on the box and re-parks the A/B.

---

## 2. Scope, and what the corpus supports

Everything was **frozen to a snapshot before the matrix ran** — the
recorder and both tapes append continuously, and two legs reading different
corpora is not an A/B. Frozen set: the RTDS corpus, the book tape, the eval
tape, `arms-state.json`, and `outcomes.jsonl`.

RTDS corpus: **974,837 lines, 08:28:55Z → 21:01:26Z**, one file. Book tape:
**98 15m windows** in two disjoint blocks —

| block | windows / sym | span | why it exists |
|---|---|---|---|
| morning | btc 22, eth 22, sol 3 | 02:45:20Z–08:08:57Z | the live 15m fleet, before it was parked |
| evening | btc 16, eth 16, sol 16 | 17:06:36Z–21:00:13Z | the observer arms |

**Comparable set** — in the book tape, graded, and fully inside the corpus
(the settlement reference at `start − 60` through the close):

| sym | comparable | binance-reachable | refused for want of corpus | ungraded |
|---|---|---|---|---|
| btc 15m | **16** | 38 | 22 | 1 |
| eth 15m | **16** | 38 | 22 | 1 |
| sol 15m | **16** | 19 | 3 | 1 |

The refusals are the harness's, not this write-up's. A census run over every
graded 15m window, stream-fed:

```
census-rtds : 47 window(s) refused for want of corpus
```

— the entire morning block, exactly as `RtdsTimeline::build` should. The
ungraded window each is `1787518800` (21:00Z), still open at the snapshot.

**Outcomes were extended, not invented.** `outcomes.jsonl` on disk stopped
at 19:15Z. `pmt crypto outcomes --since 1787504400 --out <work>` was run
under a shadow `$HOME` (nothing under `~/.pmt` was written) and graded
17:00Z–20:45Z from **gamma market resolution** — 245 resolution rows, 0
dropped. Merged strongest-source-wins with the frozen file: 1,130 rows, 109
of them 15m. **Every window in the comparable set is graded `resolution`**
— the exchange's own answer, never a chainlink or book inference.

### Coverage caveats, stated up front

- **48 windows over 4h01m in one regime.** That is a screen, not a season.
  Every sign test below is insignificant and every bootstrap CI spans zero.
- **The first window is 44% short.** `1787504400` (17:00Z) has book from
  17:06:36Z. Every other window's book starts within 1.2–3.5% of its start.
  `1787505300` (17:15Z) is the thinnest at 44 book records over 15 minutes.
- **21 recorder holes inside the study span, 510s total (3.5%), worst
  39.3s**, touching 10 of 16 windows per symbol. The recorder is a SECOND
  subscriber, so its drops are not the engine's; a touched window is not a
  lost window. The 722s hole is at 08:44Z, outside this block.
- **Absolute dollars are the harness's, not the wallet's.** Replay fills
  every `decide()` Buy instantly at the quoted price. Read deltas.
- **Sizes are imposed, not read.** Every leg runs `btc/eth $350 size / $50
  clip`, `sol $150 / $25` — the level the 15m fleet actually ran on before
  it was parked (the eval tape's own `roll` records; max fire notional
  $49.92 / $49.98 / $24.91). They cannot be read per-window the way
  `feed_ab.py` reads the 5m ones, because every 15m `roll` since 17:00Z
  carries the observer's $1. Identical on every leg, so it cannot bias a
  delta; it does set the level.
- **`decided_k` was not passed anywhere.** `Tunables::law(dur)` bakes
  `1.25` in for any window longer than 300s, so every leg here already runs
  the 15m carve-out cap `analysis/carveout_ab.md` shipped.
- **The fleet cap never bound.** `0 block(s)` in every run; peak un-decided
  notional $141 across the real legs against a $500 cap. R7 is inert at 15m
  at these sizes — noted so nobody reads the cap as a constraint that
  shaped these numbers.

---

## 3. The guard floor

Measured exactly the way `analysis/feed_ab.py` measured it — same clock,
same estimator, same 1 Hz RTDS spot series, recorder holes and stale
counterparts dropped. The quantity is duration-independent (both 5m and 15m
settle on the sixty topic once `--settle-tw 60` is passed), so this is the
same measurement over a different span, and it reproduces.

| sym | live guard | `\|chainlink spot − twap60\|` p90 (whole corpus) | same, study span only | `\|binance spot − twap60\|` p90 | pure venue p90 | floor ⌈p90⌉ | **recommended** |
|---|---|---|---|---|---|---|---|
| btc | 6.0 | 4.27 | 3.19 | 3.77 | 1.95 | **5.0** | **6.0** (unchanged) |
| eth | 8.0 | 6.79 | 5.76 | 5.78 | 1.93 | **7.0** | **8.0** (unchanged) |
| sol | 10.0 | 7.74 | 6.74 | 6.99 | 2.50 | **8.0** | **10.0** (unchanged) |

The floor says how thin a margin the feed can no longer distinguish from
nothing. It is a **floor, not a target**, so the recommendation is
`max(live, floor)` — never looser than the measurement supports and never
looser than what is deployed. All three symbols already clear their floor,
so **no guard moves**, and every rtds leg below runs the live guard. eth's
15m guard is 8.0 because `feed_ab.md` already raised the 5m one; that
raise is confirmed here on an independent span (5.76–6.79 vs a guard of 8).

Two notes on comparing this to `feed_ab.md` §3. The rtds column reproduces
it to within 0.2bp on a corpus an hour longer — btc 4.27 vs 4.40, eth 6.79
vs 7.03, sol 7.74 vs 7.94 — which is the cross-check that the measurement
is stable. The **binance** column does not, and should not be read as a
correction to it: `feed_ab.py` reads the recorder's binance stamp off 5m
book records, this reads it off **15m** ones, which exist only in the two
blocks of §2. Its n is 10,392 against the 5m study's much larger sample and
its span is different, so it is an internal control here, not a re-measure
of the 5m guards.

---

## 4. The A/B

`--fleet-cap 500` on every run. `base` is `feed=binance, settle_rule=range_avg`
at the parked-era sizes — today's posture at a size that could trade. The
three variants are all `feed=rtds --settle-tw 60` under the live guards and
the `decided_k = 1.25` law.

### btc + eth (32 graded windows) — the decision set

| leg | fired | clips | W-L | net $ | notional $ | RoN | Δnet | Δ ex-top | better/worse | sign p |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| base (binance, range_avg) | 5 | 44 | 4-1 | −13.57 | 1,449.45 | −0.94% | — | — | — | — |
| **rtds + range_avg** | 6 | 49 | 5-1 | **−2.37** | 1,520.41 | −0.16% | **+11.20** | **−7.08** | 4/2 | 0.688 |
| **rtds + hybrid** | **0** | **0** | 0-0 | 0.00 | 0.00 | — | +13.57 | **−36.45** | 1/4 | 0.375 |
| **rtds + terminal** | **0** | **0** | 0-0 | 0.00 | 0.00 | — | +13.57 | **−36.45** | 1/4 | 0.375 |

The `+13.57` on the two zero-fire legs is not a gain — it is the baseline's
loss, not taken. Drop the single largest mover and it is **−$36.45**: the
whole of it is one window, `eth-updown-15m-1787508000`.

### Fleet (btc + eth + sol, 48 windows, one shared $500 cap)

| leg | fired | clips | W-L | net $ | notional $ | RoN | Δnet | Δ ex-top | b/w | sign p |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| base | 5 | 44 | 4-1 | −13.57 | 1,449.45 | −0.94% | — | — | — | — |
| rtds + range_avg | 7 | 50 | 5-2 | −27.16 | 1,545.03 | −1.76% | −13.59 | +11.20 | 4/3 | 1.000 |
| rtds + hybrid | 0 | 0 | 0-0 | 0.00 | 0.00 | — | +13.57 | −36.45 | 1/4 | 0.375 |
| rtds + terminal | 0 | 0 | 0-0 | 0.00 | 0.00 | — | +13.57 | −36.45 | 1/4 | 0.375 |

The fleet is worse than the pair on the stream for one reason: sol. Its only
stream fire is `sol-updown-15m-1787510700`, one clip, **−$24.79**, a window
the binance leg never entered. That is the vol study's refusal showing up in
dollars on a sample of one.

### Per symbol

| sym | leg | fired | clips | W-L | net $ | notional $ | RoN | Δnet | Δ ex-top | b/w | p |
|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| **btc** | base | 2 | 22 | 2-0 | +24.08 | 700.11 | +3.44% | — | — | — | — |
| | rtds range_avg | 2 | 20 | 2-0 | +16.86 | 700.87 | +2.41% | −7.22 | −1.08 | 0/2 | 0.500 |
| | rtds hybrid / terminal | 0 | 0 | 0-0 | 0.00 | 0.00 | — | −24.08 | −11.53 | 0/2 | 0.500 |
| **eth** | base | 3 | 22 | 2-1 | −37.65 | 749.34 | −5.02% | — | — | — | — |
| | rtds range_avg | 4 | 29 | 3-1 | −19.23 | 819.54 | −2.35% | +18.42 | +0.14 | 4/0 | 0.125 |
| | rtds hybrid / terminal | 0 | 0 | 0-0 | 0.00 | 0.00 | — | +37.65 | −12.37 | 1/2 | 1.000 |
| **sol** | base | 0 | 0 | 0-0 | 0.00 | 0.00 | — | — | — | — | — |
| | rtds range_avg | 1 | 1 | 0-1 | −24.79 | 24.62 | −100.7% | −24.79 | 0.00 | 0/1 | 1.000 |
| | rtds hybrid / terminal | 0 | 0 | 0-0 | 0.00 | 0.00 | — | 0.00 | 0.00 | 0/0 | — |

**Nothing here is significant and nothing survives its jackknife.** btc's
stream leg is −$7.22 and −$1.08 ex-top; eth's is +$18.42 and **+$0.14**
ex-top, i.e. one window (`1787507100`, +$18.28) is the entire result and
without it the two feeds are indistinguishable to fourteen cents. Every
sign test is ≥ 0.125. Every bootstrap CI spans zero.

### Why hybrid and terminal fire nothing — the gate ladder

Zero fires needs a mechanism, not a shrug. Each row relaxes ONE gate on top
of the live `rtds_terminal` / `rtds_hybrid` params, btc+eth, 32 windows:

| relaxation | terminal: windows / clips / net | hybrid: windows / clips / net |
|---|---|---|
| *(none — the leg above)* | **0 / 0 / $0.00** | **0 / 0 / $0.00** |
| `basis_guard_bp` 6,8 → 0.1 | 0 / 0 / $0.00 | 1 / 1 / +$1.12 |
| `min_fair` 0.97 → 0.50 | 0 / 0 / $0.00 | 0 / 0 / $0.00 |
| `min_edge` → 0.001 | 0 / 0 / $0.00 | 1 / 2 / +$0.99 |
| `max_price` → 0.999 | 0 / 0 / $0.00 | 0 / 0 / $0.00 |
| `quiesce_secs` 20 → 0 | 0 / 0 / $0.00 | 0 / 0 / $0.00 |
| `clip_cooldown_s` → 0 | 0 / 0 / $0.00 | — |
| **every book-side gate at once**, θ and guard LIVE | **0 / 0 / $0.00** | — |
| **every gate at once except θ** | **0 / 0 / $0.00** | 5 / 12 / +$2.11 |
| **`theta` 0.3 → 0** | **6 / 7 / −$1.73** | **13 / 22 / −$184.12** |
| everything, θ included | 25 / 49 / −$70.83 | 31 / 93 / −$414.61 |

θ is **necessary and sufficient**. And it has to go all the way to zero —
the sweep:

| θ | 0.30 (live) | 0.25 | 0.20 | 0.15 | 0.10 | 0.05 | 0.00 |
|---|---|---|---|---|---|---|---|
| terminal | 0 | 0 | 0 | 0 | 0 | **0** | 6 win / 7 clips / −$1.73 |
| hybrid | 0 | 0 | 1 / +$1.12 | 2 / +$12.44 | 3 / −$50.48 | 9 / −$67.45 | 13 / −$184.12 |

**The mechanism, read off the model:** `terminal_lock(rem, tw, …)` returns
`banked = 0.0` for every tick with `rem > tw`. `settle_tw_for` is 60s at
15m. So `banked_margin_bp ≡ 0`, `side_safety ≡ 0`, and
`safety_gate_blocks(0.3, "twap", no_clips_yet, 0.0)` is TRUE for the first
**840 of 900 seconds** of every window. A terminal 15m arm is closed by
construction for 93% of its market — not gated on evidence, but on the
absence of a quantity that does not exist yet.

**And the last 60s is where the book goes away.** Winner-side supply across
the 48 windows, 8,996 book ticks:

| bucket | ticks | winner-side ask present | ≤ `max_price` 0.985 | ≤ 0.955 (edge room) | terminal-margin side buyable |
|---|---:|---:|---:|---:|---:|
| `rem > 300` | 4,493 | 99.9% | 99.9% | 98.9% | 99.9% |
| `120 < rem ≤ 300` | 1,392 | 91.2% | 85.5% | 73.1% | 85.5% |
| `60 < rem ≤ 120` (late unlock) | 1,179 | 62.8% | 45.3% | 30.5% | 45.6% |
| **`rem ≤ 60`** (settlement window) | 1,932 | **15.5%** | **10.8%** | **7.3%** | **19.7%** |

The terminal rule's evidence arrives exactly when the book stops selling.
And on the ticks that *are* buyable in that last minute the entry chain
still never closes: with `min_fair 0.5`, `min_edge 0.001`,
`max_price 0.999` and `quiesce 0`, and θ and the guard left live, the leg
fires **zero**.

**The clincher.** At θ = 0 terminal does fire — 7 clips across 6 windows —
and **every single one lands at `rem` between 210s and 886s**, i.e. outside
the settlement window entirely, priced on pure diffusion with zero banked
evidence. That is precisely the trade θ exists to refuse. Turning θ off does
not buy the terminal rule's edge; it buys a coin flip wearing its name.

This is the same conclusion `fifteen_stream_fit.md` §3 flagged against
itself. Its bankability table (hybrid reaching `banked_decided` in 21–28 of
30 windows, `med rem@bk` **27–40s**) is a model-side ceiling with "no book,
no ask, no `min_edge`/`min_fair`/`max_price`, no quiesce" — and it said so:
*"Depth is the constraint that actually binds on 15m markets and none of it
is modelled here."* With a book in front of it, that ceiling is 0.

---

## 5. The rule gap is real — and unreachable

Read directly off the settlement series, no Binance anywhere: the 60s TWAP
print at range end against the print at range start (terminal), versus the
whole-range average of the same series (range_avg), over all 48 comparable
windows.

| rule | correct | rate |
|---|---|---|
| **terminal** | **42 / 48** | **87.5%** |
| range_avg | 31 / 48 | 64.6% |
| the two agree | 37 / 48 | — |
| **they disagree** | **11** | **terminal right 11/11** |

Terminal's 6 misses are shared with range_avg and five of them are inside
3.6bp — `+0.19`, `−0.51`, `−1.05`, `+1.44`, `−3.52`. That is the recorder's
step-held twap60 print at the wire against the oracle's own settlement
instant, not a rule error. Excluding `|margin| < 3.6bp`, **terminal is
42/43 = 97.7%**, in line with the 96.5% the brief cited.

The costed instance, in this very sample:

| `eth-updown-15m-1787508000` (18:00Z) | value |
|---|---|
| reference (twap60 @ start−60) | 2440.9919 |
| twap60 @ end | 2446.6426 |
| **terminal margin (the market's rule)** | **+23.15 bp → UP** |
| **range_avg margin (the arms' rule)** | **−1.26 bp → DOWN** |
| settled | **UP** |
| baseline arm | one down clip, **−$50.02** — 100% of the clip |

That single window is the largest mover in the entire study and the reason
the two zero-fire legs show a `+13.57` delta at all. It is the 15m version
of `feed_ab.md` §4's 17:15Z event: **a settlement-rule error, on data the
stream-fed arm had in hand, and no feed change reaches it.**

And a large slice of the block is un-tradeable for a duller reason: **22 of
the 48 windows finish with a terminal margin inside btc's own 6bp guard**
(`|margin|` p10 1.05 / p50 8.21 / p90 23.60 / max 36.44 bp). Nearly half
the sample is noise the arm is right to refuse — which is also why the rule
question is worth less at 15m than the 96.5%-vs-85.7% headline suggests:
the rules can only differ where there is a margin to get wrong.

---

## 6. Context: the binance/range_avg posture over a longer sample

The variants cannot reach the morning block (no corpus), but the incumbent
can. `feed=binance, settle_rule=range_avg` at the parked sizes, over every
graded 15m window the book tape holds, under the `decided_k = 1.25` law:

| block | windows | fired | clips | net $ | notional $ | RoN | *(real live fires)* |
|---|---:|---:|---:|---:|---:|---:|---:|
| morning 02:45–08:08Z | 47 | 18 | 153 | **+97.69** | 4,757.94 | +2.05% | 294 clips / $7,370 |
| evening 17:00–21:00Z | 48 | 5 | 44 | **−13.57** | 1,449.45 | −0.94% | 0 (observers) |
| **all** | **95** | **23** | **197** | **+84.12** | 6,207.39 | **+1.36%** | — |

The morning block replays positive, and that is the post-law counterfactual
the live arms never got (they ran pre-`decided_k`). **But it is two
windows**: `btc-updown-15m-1787458500` **+$151.40** and
`btc-updown-15m-1787464800` **−$156.54**. They cancel. Strip them and the
whole 95-window binance read is +$89 spread over 21 windows of small
change — one regime, one day, and the rule underneath it is the one the
carve-out ledger measured at **−$649.15** on real wallet fires.

The same leg with `settle_rule = terminal` on **binance**, over the same 95
windows: **0 fires**. The θ blockage is not an rtds artifact.

---

## 7. Verdict

### btc 15m — STAY SHUT

No leg is positive and none survives its jackknife. On the stream with
`range_avg` btc is Δ **−$7.22** (−$1.08 ex-top, 0 better / 2 worse). With
`hybrid` or `terminal` it cannot trade. There is no configuration to
re-open it at.

### eth 15m — STAY SHUT

eth is the only symbol whose stream leg is positive (Δ **+$18.42**) and it
is **+$0.14 ex-top** — one window, `1787507100`, is the entire result. Its
baseline is the worst RoN on the board (−5.02%) and its single largest loss,
`1787508000` at −$50.02, is a settlement-rule error that `range_avg` will
make again and that no arm command can fix.

### sol 15m — STAY SHUT (unchanged, and this study agrees)

Refused before this study by the vol read (P(|move| > 10bp in 7.5 min) =
66%). Nothing here disturbs it: sol's only stream fire in 16 windows is a
one-clip **−$24.79** in a window the binance leg never entered.

### The arms: KEEP THEM ARMED, exactly as they are

They are not idling. They are the 15m book recorder, and they are the only
reason a 15m A/B exists at all — `fifteen_stream_fit.md` §4's blocker was
*"the engine must SUBSCRIBE 15m books while the recorder runs"*, and that is
what these arms do. Their exposure is zero by construction (§1) and
confirmed by replay (0 fires / 48 windows). Disarming them costs the corpus
and buys nothing.

If the engine restarts and the observer arms need re-arming, the exact
commands that reproduce today's state — `--min-elapsed 0` and `--theta`
differ from the CLI defaults and must be passed:

```
pmt crypto arm <btc-updown-15m url|slug> --size 1 --clip 1 \
  --min-fair 1.0 --theta 1.0 --min-elapsed 0 --basis-guard 6

pmt crypto arm <eth-updown-15m url|slug> --size 1 --clip 1 \
  --min-fair 1.0 --theta 1.0 --min-elapsed 0 --basis-guard 8

pmt crypto arm <sol-updown-15m url|slug> --size 1 --clip 1 \
  --min-fair 1.0 --theta 1.0 --min-elapsed 0 --basis-guard 10
```

`--size 1` is the lock that matters; the other two are belt and braces.
Nothing about them should be read as a starter position — **there is no
starter size recommended by this study, because there is no strategy to
start.** The sizing agent has nothing to size here.

### There is no command for the thing that would work

`pmt crypto arm` has no `--settle-rule`. `ArmParams::settle_rule` defaults
to `"range_avg"` through `serde` and is set only by replay params. A live
15m arm is a `range_avg` arm by construction. So the sentence "re-open
btc/eth 15m on `feed=rtds --settle-tw 60 --settle-rule terminal`" describes
a configuration that cannot be armed — and this study says that is fine,
because that configuration fires zero clips.

---

## 8. What would have to change first

The 15m hole is a **rule** problem sitting behind a **gate** problem sitting
behind a **depth** problem. In that order:

1. **`terminal_lock` has to bank something before the last 60 seconds.**
   Today `banked ≡ 0` while `rem > tw`, which makes `side_safety ≡ 0`, which
   makes the θ gate an unconditional refusal for 93% of a 15m window. This
   is the same site `fifteen_stream_fit.md` §2 already named for a different
   reason — its finding was that on `feed=rtds` the *partial settlement
   TWAP* is computable at 1 Hz and is a strictly better lock estimator than
   instantaneous spot (99–100% vs 89–93% on contested 15m windows). Both
   findings point at one function.
2. **θ needs a definition that works at 15m, or `terminal` needs its own.**
   Loosening θ globally is refused by the data: hybrid at θ 0.10/0.05/0 is
   −$50 / −$67 / −$184, and terminal at θ 0 trades only outside its own
   evidence window. The knob is not the answer; the estimator is.
3. **Depth is still the wall.** Even with 1 and 2 fixed, the winning side is
   buyable at ≤ 0.985 on **10.8%** of settlement-window ticks. Whatever
   reaches the 15m rule gap will have to reach it as a **maker** — resting
   into the last minute rather than taking — which is `docs/maker-design.md`
   territory, not an arm parameter.
4. **`late_rem_s = 120` is 13% of a 15m window** where it is 40% of a 5m
   one (`fifteen_stream_fit.md` §1). Untouched here on purpose — this A/B
   varies `settle_rule` and nothing else — and still owed a sweep.
5. **Re-run this on a multi-day corpus.** 48 windows over 4 hours in one
   regime cannot separate anything. The observer arms make that cheap: every
   night they run, the comparable set grows by ~96 windows/symbol at no
   risk.

---

## 9. Weaknesses

- **48 windows, 4h01m, one regime, one day.** No sign test in this document
  is significant; every bootstrap CI spans zero. The two zero-fire legs are
  the only results that are not sample-limited, because they are
  mechanistic.
- **Absolute dollars are the harness's.** Replay fills every decision
  instantly at the ask. Deltas are comparable; levels are not.
- **Sizes are imposed** ($350/$50, $150/$25) rather than read per-window,
  because every 15m `roll` since 17:00Z carries the observer's $1. Identical
  across legs, so it cannot bias a delta.
- **`sigma_bp_per_min` is a single cold-start value per symbol** (btc 9 /
  eth 25 / sol 12, the arms' own as-armed figures). It is only the vol
  floor's fallback and should rarely bind after warmup, but it is an
  approximation, and it is identical on every leg.
- **The rule grading in §5 uses the recorder's step-held twap60 print at
  the wire**, not the oracle's settlement instant. That is why 5 of
  terminal's 6 misses are under 3.6bp. It biases *against* terminal, so the
  87.5% is a floor on its accuracy, not a ceiling.
- **The `depth` table's "terminal-margin side" uses `sign(spot/ref − 1)`
  from the corpus**, reconstructed outside the engine. It is a
  characterisation of book supply, not a replay of the arm's own decision.
- **The morning block's `+$97.69` is a counterfactual**, not a wallet
  number: those windows ran live *before* `decided_k = 1.25` was law.
- **The 21:00Z window is ungraded** and is excluded everywhere.
- **`~/.pmt` was never written.** Every input is a frozen copy under a work
  directory outside the repo; `replay --mode full` ran under a shadow
  `$HOME` holding a copy of the kline cache and the RTDS corpus; the
  outcomes refresh wrote to `--out <work>` under that same shadow `$HOME`.
  The one network activity was that refresh: gamma resolution lookups plus
  a Polygon RPC oracle-corpus read and one wallet-activity fetch. No engine,
  no orders, no config, no state file.

---

## 10. Reproducing

```bash
W=~/Desktop/code/pmt-wt-fifteen-work
mkdir -p $W/home/.pmt/corpus/rtds $W/home/.pmt/engine

# Freeze first — the live tapes are append-only, so a re-run is not a re-run.
cp ~/.pmt/engine/book-tape.jsonl   $W/book-tape-frozen.jsonl
cp ~/.pmt/engine/updown-tape.jsonl $W/updown-tape-frozen.jsonl
cp ~/.pmt/engine/arms-state.json   $W/arms-state-frozen.json
cp ~/.pmt/corpus/outcomes.jsonl    $W/outcomes-frozen.jsonl
# Shadow $HOME: --mode full appends missing 1m klines under $HOME/.pmt/corpus.
cp ~/.pmt/corpus/klines-1m-*.jsonl        $W/home/.pmt/corpus/
cp ~/.pmt/corpus/chainlink-*.jsonl        $W/home/.pmt/corpus/
cp ~/.pmt/corpus/rtds/rtds-20260823.jsonl $W/home/.pmt/corpus/rtds/
ln -f $W/book-tape-frozen.jsonl   $W/home/.pmt/engine/book-tape.jsonl
ln -f $W/updown-tape-frozen.jsonl $W/home/.pmt/engine/updown-tape.jsonl

# Extend the outcomes corpus (gamma resolution), shadow HOME, explicit --out.
(cd pmtrader && PM_FUNDER_ADDRESS=... HOME=$W/home \
   uv run pmt crypto outcomes --since 1787504400 --out $W/outcomes-extended.jsonl)
# then merge strongest-source-wins into $W/outcomes-merged.jsonl

(cd pmengine && cargo build --release --features ec2)   # private flavor

uv run --project pmtrader python analysis/fifteen_rerun.py survey --work $W/ab \
  --book-tape $W/book-tape-frozen.jsonl --arms-state $W/arms-state-frozen.json \
  --outcomes $W/outcomes-merged.jsonl --rtds-dir $W/home/.pmt/corpus/rtds

WORK=$W analysis/fifteen_rerun.sh

uv run --project pmtrader python analysis/fifteen_rerun.py depth --work $W/ab \
  --book-tape $W/book-tape-frozen.jsonl --rtds-dir $W/home/.pmt/corpus/rtds
```

Engine branch `fifteen-rerun` off `master` (4dd28cd); submodule
`pm-trade/pmt-strategies` at the gitlinked `c8b0e53`, untouched — this
study changed no code. Nothing pushed.
