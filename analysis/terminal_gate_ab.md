# The terminal gate — partial banking, a late consistency gate, and the latch

Replay A/B, 2026-08-23, snapshot frozen **21:43:02Z**. Drivers:
`analysis/terminal_gate_ab.py` (`survey` / `report`) and
`analysis/terminal_gate_ab.sh` (the matrix). Harness:
`pmengine replay --mode full --fleet-cap 500` with `--outcomes` as ground
truth on every leg, so settlement is identical across legs and only the leg's
own knob differs.

Three builds, three questions, and they do not agree with each other:

| # | change | verdict |
|---|---|---|
| **1** | `terminal_lock` banks the partial/projected settlement TWAP | **SHIPS (dark).** The rule is reachable for the first time: **0 → 8 of 36** 15m windows, **0 → 38 of 433** 5m windows. `range_avg` is bit-identical |
| **2** | `late_terminal_agree` — terminal-consistency late gate | **DO NOT SHIP.** Refuses the V-window correctly and four winners with it: **−$194.98**, 1 better / 4 worse, CI spans zero. The mechanism cannot discriminate |
| **3** | `latch_release_on_proof` — release the window latch on proof | **STRONGEST RESULT IN THE STUDY.** **+$168.23** over 659 windows, **61 better / 4 worse**, sign p < 1e-5, **CI95 [+81.51, +252.33] excludes zero** |

Everything below defaults **off**. `settle_rule` has no arm flag, and both
`Tunables` knobs are replay-only, so nothing here is reachable from
`pmt crypto arm`.

---

## 0. What was frozen, and one harness error worth reading

Frozen before the matrix ran: the RTDS corpus (203 MB, 08:28:55Z → 21:43:02Z,
all 8 symbols), `book-tape.jsonl`, `updown-tape.jsonl`, `arms-state.json` and
`outcomes.jsonl`. The outcomes corpus was then extended under a shadow `$HOME`
(`pmt crypto outcomes --since 1787495000`), which added **146 windows and
upgraded 2 to a stronger source** — without it the block after 19:35Z is
ungraded and the V-window this study is named for is not in the sample.

**Comparable sets** — in the book tape, graded, and (for a stream-fed leg)
with the corpus spanning the settlement reference through the close:

| set | windows | symbols |
|---|---:|---|
| 5m stream | **433** | btc 156, eth 156, xrp 121 (all `feed=rtds`) |
| 5m binance control | **226** | sol (`feed=binance`) |
| 15m | **36** | btc 18, eth 18 |

### The error, stated because it invalidated a first pass

The first matrix ran against the **wrong binary**. Building the pre-change
fixture baseline overwrites `target/release/pmengine`, and the driver picked
that up: every `settle_rule=terminal` leg reported 0 fires, which reproduced
`fifteen_rerun.md` §4 perfectly and was therefore completely invisible as a
mistake.

It is now impossible to repeat: `terminal_gate_ab.sh` prints the engine's md5
and stamps it beside every output, and reuses a cached result **only** when
the binary still hashes the same.

The accident left something worth keeping — a clean pre-change run on the same
36 windows, used as the BEFORE column in §2.

---

## 1. BUILD 1 — the terminal rule can reach a trade now

`terminal_lock` returned `banked = 0` for every tick with `rem > tw`, so
`side_safety ≡ 0` and `safety_gate_blocks` refused the first clip of a window
for **840 of every 900 seconds** at 15m. `fifteen_rerun.md` §4 proved θ
necessary *and* sufficient for the resulting zero.

`terminal_bank` splits the one conflated number in two:

- **`proj_bp`** — the forward-projected terminal margin. Outside the
  settlement window that is the current stream level against the range-start
  reference (the martingale forecast of the terminal print); inside it, the
  formed share at its realized partial-TWAP value plus the unformed remainder
  projected off the current print. This is what θ scores.
- **`locked_bp`** — still identically zero while `rem > tw`. This alone feeds
  `banked_decided` / `flip_proof`, so the full-budget unlock, the fleet cap's
  exemption and the brake carve-outs keep their literal truth.

Measured on the terminal 15m leg's own decision trace (2,064 eval ticks):

| | before | after |
|---|---|---|
| ticks with `\|safety\| ≥ 0.3` | **0%** (identically zero by construction) | **95.8%** (p50 0.86, p90 2.00) |
| 15m windows fired | **0 / 36** | **8 / 36** |
| 5m stream windows fired | **0 / 433** | **38 / 433** |

**And θ is no longer the binding gate.** The sweep is flat:

| θ | 0.30 | 0.25 | 0.20 | 0.15 | 0.10 | 0.00 |
|---|---|---|---|---|---|---|
| 15m terminal | 8 win / 9 clips | 8 / 9 | 8 / 9 | 8 / 9 | 8 / 9 | 8 / 9 |
| 5m terminal | 38 / 60 | 38 / 60 | 38 / 60 | 38 / 60 | 38 / 60 | 38 / 60 |

What binds instead is the **decidedness waiver chain**: terminal spends most
of a window un-decided (correctly — nothing is locked), so the budget stays at
`early_frac` and the window latch seals it on its first distrust/avg_down
trip. The 15m trace shows `latched` on 955 side-ticks against `safety` on
1,392 and `FIRE-OK` on 1,462.

### `range_avg` is untouched, measured twice

| check | result |
|---|---|
| 20/20 characterization fixtures | **PASS**, and the full normalized stdout+stderr is **byte-identical** to a pre-change build |
| 15m `range_avg` leg, before vs after engine | **9 fired / 78 clips / +$62.92 on both** |
| 5m binance control, `late_terminal_agree` on | **0 windows moved, Δ +$0.00** |

All 20 fixtures are `range_avg` arms — verified, not assumed. Nothing was
blessed.

---

## 2. BUILD 1's A/B — what the terminal rule earns

### btc/eth 15m — 36 graded windows

| leg | fired | clips | W-L | net $ | RoN | Δnet | Δ ex-top | b/w | sign p | CI95 |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---|
| `range_avg` | 9 | 78 | 8-1 | **+62.92** | +2.77% | — | — | — | — | — |
| terminal θ0.3 | 8 | 9 | 4-4 | +1.71 | +1.69% | −61.21 | −111.09 | 2/12 | **0.013** | [−168.27, +74.92] |
| terminal θ0 | 8 | 9 | 4-4 | +1.71 | +1.69% | −61.21 | −111.09 | 2/12 | 0.013 | [−168.27, +74.92] |
| terminal + latch | 9 | 13 | 5-4 | +9.34 | **+4.42%** | −53.58 | −103.47 | 3/12 | 0.035 | [−161.57, +82.70] |

*(pre-change engine, same params: terminal fires **0** at every θ from 0.30 to
0.10 — that is `fifteen_rerun.md` §4 reproduced on this window set.)*

**Terminal becomes reachable at 15m and does not pay.** It is beaten on the
sign test (2 better / 12 worse, p = 0.013) though the bootstrap CI spans zero.
Nine clips against range_avg's 78 is not a rule comparison, it is a different
business.

### 5m stream — 433 graded windows

| leg | fired | clips | W-L | net $ | RoN | Δnet | Δ ex-top | b/w | sign p | CI95 |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---|
| `range_avg` | 60 | 248 | 55-5 | +77.34 | +1.43% | — | — | — | — | — |
| terminal θ0.3 | 38 | 60 | 35-3 | **+88.63** | **+5.58%** | +11.29 | −193.73 | 37/51 | 0.165 | [−537.35, +593.14] |
| terminal θ0 | 38 | 60 | 35-3 | +88.63 | +5.58% | +11.29 | −193.73 | 37/51 | 0.165 | [−537.35, +593.14] |
| terminal + latch | 39 | 82 | 36-3 | **+116.58** | +5.11% | +39.24 | −165.78 | 38/51 | 0.203 | [−511.46, +623.75] |

**At 5m the two rules are a wash on money and not on efficiency.** Terminal
earns as much or more (+$88.63, and +$116.58 with the latch released) on
**a quarter of the notional** — RoN 5.58% against 1.43%. Neither delta is
significant; both CIs are enormous, and Δ ex-top is negative in every row,
so a single window carries the point estimate.

### The matched-selectivity requirement, and why it cannot be met on θ

`analysis/cushion_calibration.md` requires the rule comparison to be reported
at matched selectivity, because the terminal cushion is ~1.68× range_avg's and
at constant θ a rule change is mechanically an entry-rate cut.

**On this corpus θ does not move terminal's entry rate at all** — 38 windows
at θ 0.30 and 38 at θ 0.00 (15m: 8 and 8). BUILD 1 removed θ as the binding
gate; what remains binding is the decidedness waiver. So there is no θ that
matches selectivity, and reporting one would be inventing it.

The nearest leg on the axis that *does* bind is **terminal + `latch_release`**:
39 fired against the baseline's 60 at 5m, and **9 against 9 at 15m** — matched
on windows, still 13 clips against 78. Read that row as the rule at its
closest achievable entry rate; the residual gap is the waiver chain, not the
gate, and closing it further means loosening `banked_decided` itself, which is
the thing L39 says not to do.

---

## 3. BUILD 2 — the terminal-consistency late gate is refused

`late_terminal_agree` refuses a fire inside `late_rem_s` when the live
terminal margin's sign disagrees with the side, or its magnitude sits inside
`CK_NOISE_FLOOR_BP = 5`.

### 5m stream fleet — 433 graded windows

| leg | fired | clips | W-L | net $ | RoN | Δnet | Δ ex-top | b/w | sign p | CI95 |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---|
| live | 60 | 248 | 55-5 | +77.34 | +1.43% | — | — | — | — | — |
| `late_terminal_agree` | 58 | 217 | 53-5 | −117.64 | −2.30% | **−194.98** | −36.67 | **1/4** | 0.375 | [−578.43, +23.92] |

Per symbol, and this is the shape of the whole finding:

| symbol | windows | Δnet | b/w |
|---|---:|---:|---:|
| btc | 156 | **+0.00** | 0/0 — *no btc fire was ever refused* |
| eth | 156 | **−167.15** | 0/3 |
| xrp | 121 | −27.83 | 1/1 |

**Binance control (sol, 226 windows): 0 windows moved, Δ +$0.00.** `term_bp`
is `None` off the stream and a missing read never refuses. Measured, not
asserted.

### The acceptance window passes — and that is not enough

`fixtures/xrp-updown-5m-1787516400`, the 20:20Z V-window, behaves exactly as
designed. The stream's terminal margin peaked at **+32.5bp** (rem 160) and was
through zero by rem 50; range_avg banked +20bp throughout; the arm bought UP
into a book repricing 0.97 → 0.13.

| | live | gate on |
|---|---|---|
| fires | 7 | **3** |
| notional | $29.50 | $14.66 |
| pnl | **−$30.42** | **−$14.69** |

The three surviving fires are at rem 108 / 103 / 88 with `term_bp` +18.3; the
four refused are at rem 31 / 29 / 26 / 23 with `term_bp` −2.65 / −0.51 /
−0.51 / −2.62. **Loss cut 52%.**

### Why it still fails

`eth-updown-5m-1787499000` is the same window with a different ending, and the
gate cannot tell them apart:

```
V-window (LOST)                    eth 1787499000 (WON +$158)
 rem 31  term -2.65  ask 0.58       rem 63  term -4.15  ask 0.28
 rem 29  term -0.51  ask 0.52       rem 49  term -4.13  ask 0.47
 rem 26  term -0.51  ask 0.12       rem 43  term +0.54  ask 0.80
 rem 23  term -2.62  ask 0.16       rem 34  term +1.82  ask 0.96
```

Both are "terminal margin collapsed into noise, arm buying a collapsing book
late". One settled the arm's way and one did not, and **nothing in the
terminal read separates them ex ante.** The gate refuses both, and on this
corpus it refused four winners for every loser.

Two variants do not rescue it, and the reason is the same:

- **A per-symbol floor** (eth 7.03bp, xrp 15.73bp from `feed_ab.md` §3)
  refuses *more*, not less — every read in the eth window is inside 4.2bp.
- **Contradiction-only** (`signed < −floor`, refuse only an actively opposite
  read) refuses almost nothing, the V-window included: its collapse produced a
  near-**zero** terminal margin, never a flipped one. The information was "the
  margin is gone", not "the margin reversed".

**The gate's premise is false at 5m.** `late_rem_s` is 120s — 40% of a 5m
window — and the settlement TWAP is 60s wide, so for the first half of the
gated region there is nothing realized to read and plenty of diffusion left.
This lands where `correlation_study.md`'s policy (e) landed (−$105.40, CI
[−257, −7.0]), more gently and for the same reason: **the terminal rule is
right about what settles and nearly worthless as an early signal.**

---

## 4. BUILD 3 — the window latch, and the one result that clears zero

`brake_latched` is set by the first distrust/avg_down trip and released only
by `banked_decided`. `gate_shadow_2.md` §4 #1 measured the refused cohort at
113 opens, **95% winners**, +$136.30 over 9.8h. `latch_release_on_proof`
bypasses the latch while `safety >= theta && fair >= min_fair` — re-tested
every tick, never cleared, so a fresh trip re-binds it.

### All four live 5m arms — 659 graded windows

| leg | fired | clips | W-L | net $ | notional $ | RoN |
|---|---:|---:|---:|---:|---:|---:|
| live | 107 | 455 | — | +64.73 | 8,154.54 | +0.79% |
| **latch released** | 147 | 756 | — | **+232.96** | 13,469.51 | **+1.73%** |

**Δnet +$168.23 · Δ ex-top +$188.17 · 61 better / 4 worse · sign p < 1e-5 ·
CI95 [+81.51, +252.33]**

The jackknife is *favourable*: removing the largest single window makes the
delta bigger, because the largest single window is the one loss.

Per symbol:

| symbol | feed | windows | base net | latch net | Δnet | b/w | CI95 |
|---|---|---:|---:|---:|---:|---:|---|
| btc | rtds | 156 | +34.33 | +51.47 | +17.14 | 4/0 | [+1.79, +41.83] |
| eth | rtds | 156 | +37.25 | +116.87 | **+79.62** | 17/0 | [+38.01, +131.86] |
| xrp | rtds | 121 | +5.76 | −9.61 | −15.37 | 7/1 | [−58.36, +8.94] |
| **sol** | **binance** | 226 | −12.61 | +74.23 | **+86.84** | 33/3 | — |

**The caveat, and then why it survives it.** Dollar-wise the stream result is
one symbol: leave eth out and the 433-window delta falls from +$81.39 to
+$1.77. But the sign is consistent everywhere — **61 of 65 moved windows
improved**, in all four symbols, and the two largest dollar contributions come
from *different* symbols on *different feeds* (eth on rtds +$79.62, sol on
binance +$86.84). A knob that is feed-independent, symbol-independent in sign,
and 94% right on the windows it opens is not an eth artifact.

96.6% of opened windows won here against gate_shadow_2's independently
measured 95%.

### The ROADMAP question

ROADMAP:304 — *"never loosen the three brakes … without a replay A/B win"*.
The latch is **not** one of the three named brakes (15¢ distrust, 2¢
no-averaging-down, final-120s unlock); it shipped alongside R9 at
ROADMAP:217. All three named thresholds are untouched here, and the latch's
*setting* semantics are untouched too — a raw brake trip still latches the
window, still before `brake` is chosen, so a window still latches with no
distrust record on the tape. Only the release condition moves, and only while
the model is actively proving the side it refused.

That said: this is a replay A/B, not a deploy. It needs the ROADMAP:82
sequence — one small-size live night before full size — and it wants a second
day of corpus, because 659 windows over 13 hours is one regime.

---

## 5. Weaknesses

- **One day, one regime.** 13 hours, 2026-08-23. Every number here is a
  single session's.
- **The fill sim is not the wallet.** Only deltas are evidence; absolute P&L
  is not wallet truth (`hybrid_ab.md`). Baseline sim fires exceed live fires
  in every prior study and do here too.
- **(a) moved 5 windows out of 433.** The −$194.98 is one window
  (−$158.31) plus three smaller ones. Its CI spans zero and its sign test is
  p = 0.375 — the verdict is *refused for mechanism*, not *refuted by
  statistics*, and the mechanism section is the load-bearing part.
- **(b)'s 5m deltas have CIs ±$550** on a ±$40 point estimate. Nothing there
  is significant in either direction; the RoN gap is the only durable
  observation, and RoN is not a P&L verdict.
- **(c)'s dollars are eth-concentrated.** Stated above and not smoothed over.
- **`CK_NOISE_FLOOR_BP = 5` is corpus-wide** where the real floor is
  per-symbol (btc 4.40 → xrp 15.73). §3 shows a per-symbol floor makes (a)
  worse, so this is not what refused it, but the constant is not calibrated
  and should not be reused elsewhere as if it were.
- **The partial TWAP is an arithmetic mean of received prints**, not the
  oracle's own time-weighting. Right shape of estimator, not a calibrated
  reproduction — the same caveat `fifteen_stream_fit.md` §2 stated.
- **The recorder is a second subscriber**, so its drops are not the engine's;
  a window can replay as gated on a reference print the live arm did receive.
- **15m at n=36** cannot separate anything, and its sizes are the harness's
  (the parked 350/50), not the wallet's.

---

## 6. What should happen next

1. **Nothing ships live from this branch.** Both knobs default off,
   `settle_rule` has no arm flag, and all 20 fixtures are byte-identical.
2. **`latch_release_on_proof` is the candidate worth a second night.** Re-run
   it on a two-day corpus; if the sign test holds and eth stops being the only
   payer, it earns the ROADMAP:82 small-size night.
3. **`late_terminal_agree` should be left dark or deleted.** If it is kept,
   keep it for the instrumentation: `term_bp` on the tape is the axis
   `correlation_study.md` Result 2c localised every loss to, and it was
   previously unrecoverable from the tape at any price.
4. **The terminal rule's remaining blocker is the waiver chain, not θ.** That
   is a new question this study created: terminal is correct about settlement
   (386/386 in `cushion_calibration.md`), reachable now, 4× the RoN at 5m, and
   capped at ~a quarter of range_avg's volume because it honestly refuses to
   call a window decided. Whether a *terminal-native* unlock rule exists is
   worth its own study.
5. **Depth is still the wall at 15m** — 9 clips over 36 windows, and
   `fifteen_rerun.md` §4's depth table says the winning side is buyable at
   ≤ 0.985 on 10.8% of settlement-window ticks. That is
   `docs/maker-design.md` territory.

---

## 7. Reproducing

```bash
W=~/Desktop/code/pmt-wt-termgate-work
mkdir -p $W/home/.pmt/corpus/rtds $W/home/.pmt/engine $W/ab $W/bin

# Freeze first — the live tapes are append-only, so a re-run is not a re-run.
cp ~/.pmt/engine/book-tape.jsonl   $W/book-tape-frozen.jsonl
cp ~/.pmt/engine/updown-tape.jsonl $W/updown-tape-frozen.jsonl
cp ~/.pmt/engine/arms-state.json   $W/arms-state-frozen.json
cp ~/.pmt/corpus/outcomes.jsonl    $W/outcomes-frozen.jsonl
cp ~/.pmt/corpus/klines-1m-*.jsonl $W/home/.pmt/corpus/
cp ~/.pmt/corpus/chainlink-*.jsonl $W/home/.pmt/corpus/
cp ~/.pmt/corpus/rtds/rtds-20260823.jsonl $W/home/.pmt/corpus/rtds/
ln -f $W/book-tape-frozen.jsonl   $W/home/.pmt/engine/book-tape.jsonl
ln -f $W/updown-tape-frozen.jsonl $W/home/.pmt/engine/updown-tape.jsonl

# Extend the outcomes corpus (gamma resolution + chainlink), shadow HOME.
cp $W/outcomes-frozen.jsonl $W/outcomes-extended.jsonl
(cd pmtrader && PM_FUNDER_ADDRESS=0x... HOME=$W/home \
   uv run pmt crypto outcomes --since 1787495000 --out $W/outcomes-extended.jsonl)

(cd pmengine && cargo build --release --features ec2)   # private flavor

uv run --project pmtrader python analysis/terminal_gate_ab.py survey --work $W/ab \
  --book-tape $W/book-tape-frozen.jsonl --outcomes $W/outcomes-extended.jsonl \
  --arms-state $W/arms-state-frozen.json --rtds-dir $W/home/.pmt/corpus/rtds \
  --frozen-at 2026-08-23T21:43:02Z

WORK=$W TRACE=1 analysis/terminal_gate_ab.sh          # SETS=/CAP= to narrow
uv run --project pmtrader python analysis/terminal_gate_ab.py report --work $W/ab
```

The `--trace` flag on `pmengine replay` is new and is what made §1 and §3
measurable: it dumps every tape record `decide()` produced in full mode, so a
gate can be *attributed* instead of bounded by relaxing knobs one at a time.

Engine branch `terminal-gate` off `master` (53d6a2c); submodule
`pm-trade/pmt-strategies` branch `terminal-gate` off `origin/main` (97ad773).
Nothing pushed.
