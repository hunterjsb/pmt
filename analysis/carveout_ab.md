# The `banked_decided` carve-out — ledger and tightening A/B (2026-08-23)

**Verdict: the carve-out earns its keep, and the axis it splits on is
DURATION, not the brakes. On real wallet-graded fires it is +$89.70 at 5m
and −$649.15 at 15m. Cap it only at 15m — `decided_k = 1.25` on 15m arms —
and change nothing on the 5m book, where every tightening tested is a wash
or a loss. The late-window unlock should not be capped at all: on the clock
alone it is +$211.93, and the premise that a fire could exceed its clip is a
units error.**

Three things this study kills, each with a number rather than an opinion:

1. **The late-120s unlock is not the defect.** Split by what actually opened
   the budget, the clock-alone half is **+$211.93** over 305 fires and the
   `banked_decided` half is **−$385.37** over 240. The clock is carrying the
   waiver's blame.
2. **No per-fire notional cap can reach the named 1478-share fire.** That
   fire was 1478 shares at $0.010 = **$14.78** against eth's **$110** clip —
   **0.13 clips**, not 148. `size` is `(clip_usdc / ask).min(...)`, so no
   fire on the whole night exceeded ~1.01 clips and only 2 of 552 late fires
   cleared $150. The campaign's "148×" compared shares against dollars.
3. **A global `decided_k` is a cliff, not a slope.** k1.10 is −$591 and
   k1.15 is −$706 while k1.25 is +$703. A knob whose sign flips twice across
   0.15 of its range is not measuring a policy.

Drivers: `analysis/carveout_ledger.py` (the stake, off real fires),
`analysis/carveout_ab.py` (the replay A/B), `analysis/carveout_robust.py`
(concentration / sign test / bootstrap). Read-only over `~/.pmt`; frozen
tape copies and a shadow `$HOME`, no engine, no orders, no network.

---

## 1. The ledger — what has actually flowed through the waiver

Every real fire on the frozen tape (2026-08-22 22:02Z → 2026-08-23 19:47Z),
attributed to the guard that would have blocked it, graded against
`~/.pmt/corpus/outcomes.jsonl` (wallet + resolution, terminal truth).

Attribution is read off the predicates, not guessed. `distrust_blocks(net,
0.15, bd)` is `net > 0.15 && !bd`, and a clip only fires with `brake ==
None` — so **a fire whose recorded `net > 0.15` proves `banked_decided` was
true and proves the carve-out is the only reason it fired.** Same argument
for `avg_down_blocks` against the window's previous fire on that token. The
latch class is reconstructed from the 5s-throttled eval tape and is a lower
bound, so it is reported apart.

| class | fires | notional | W | L | won | lost | **NET** |
|---|---:|---:|---:|---:|---:|---:|---:|
| ALL fires (denominator) | 1188 | $30,139 | 1053 | 135 | +$2,303.28 | −$2,972.09 | **−$668.81** |
| carve-out: distrust waived | 162 | $2,776 | 110 | 52 | +$729.08 | −$1,028.82 | −$299.74 |
| carve-out: avg_down waived | 128 | $2,184 | 81 | 47 | +$389.55 | −$853.27 | −$463.72 |
| carve-out: latch waived (lower bd) | 397 | $10,341 | 360 | 37 | +$598.05 | −$671.33 | −$73.28 |
| **carve-out: any of the three** | **609** | **$14,159** | 507 | 102 | +$1,411.34 | −$1,970.79 | **−$559.45** |
| late unlock: ≤120s left | 552 | $14,406 | 483 | 69 | +$917.40 | −$1,085.31 | −$167.91 |
| — of which, clock alone (bd false) | 305 | $8,985 | 293 | 12 | +$397.14 | −$185.21 | **+$211.93** |
| — of which, bd also true | 240 | $5,282 | 183 | 57 | +$514.73 | −$900.10 | **−$385.37** |
| **FAMILY = carve-out OR late unlock** | **885** | **$22,127** | 768 | 117 | +$1,702.89 | −$2,290.37 | **−$587.48** |
| neither (plain early/spec fires) | 303 | $8,013 | 285 | 18 | +$600.39 | −$681.72 | −$81.33 |

**The stake is −$587.48 of the tape's −$668.81 — 88% of the hole, on
$22,127 of notional.** The campaign's brief put it at ~$357 of −$512
all-time; the difference is convention, not disagreement (see §1.2).

### 1.1 The split that decides everything

| dur | class | fires | notional | W | L | NET |
|---:|---|---:|---:|---:|---:|---:|
| 5m | **carve-out waived** | 288 | $6,935 | 247 | 41 | **+$89.70** |
| 5m | late unlock only | 271 | $7,884 | 256 | 15 | −$30.35 |
| 5m | neither | 133 | $3,737 | 116 | 17 | −$342.47 |
| 5m | ALL | 692 | $18,556 | 619 | 73 | −$283.13 |
| 15m | **carve-out waived** | 321 | $7,224 | 260 | 61 | **−$649.15** |
| 15m | late unlock only | 5 | $84 | 5 | 0 | +$2.32 |
| 15m | neither | 170 | $4,276 | 169 | 1 | +$261.14 |
| 15m | ALL | 496 | $11,583 | 434 | 62 | −$385.68 |

The carve-out is **profitable at 5m and is the entire 15m hole** — the 15m
book's non-carve fires are +$261.14, so the waiver is not merely losing
there, it is losing more than the whole book's deficit.

This is not a new theory, it is an old one finally getting a number.
`docs/LESSONS.md` L39 and `analysis/fourh_fit.md` already say the
`range_avg` "banked mass" is settlement arithmetic only under a
range-average rule; under the real terminal rule it is a **momentum proxy
that works at 5m and lies with duration**. Every live arm is
`settle_rule = range_avg`. The waiver is exactly the place that proxy is
allowed to overrule the book, so if the proxy decays with duration the
waiver's P&L must decay with duration. It does, and the sign flips.

### 1.2 What these numbers are, and are not

Per-fire P&L uses replay's own `settle_pnl` convention (winning share pays
1.0, cost `size × price`, fee `size × fee_rate × min(price, 1−price)`) over
**decisions**, assuming each fire filled in full at its quoted ask. Real
fills were thinner: across the four xrp losing windows this reconstruction
reads −$262.89 against the wallet's −$228.86 (**1.15×**), and its
distrust-carve-out share is −$112.07 against `maker_grading.md` §3.2's
≈−$103 (**1.09×**). Read the ledger as internally consistent and ~10–15%
wide of realized, never as the wallet.

Two corrections to the campaign brief, both worth carrying forward:

- **The $228.86 is the four-window xrp hole, not the 17:15Z event.**
  $134.00 + $43.23 + $28.44 + $23.19 = $228.86 exactly
  (`maker_grading.md` §3.1). The "$103 of $228.86, 45% of the hole" belongs
  to those four xrp windows. The 17:15Z five-arm event is a *separate*
  ~$230, and it contains **no** distrust-carve-out fires at all — its
  largest net was 0.130, under the 0.15 threshold. Its carve-outs are
  `avg_down` and `latched`.
- **The 2500-share buy at 1c is `xrp-updown-5m-1787449200` at 01:44:46Z**
  (rem 14s, −$26.75, distrust-waived), not part of the 17:15Z event.

---

## 2. The A/B

`pmengine replay --mode full --fleet-cap 500`, interleaved driver, per-window
params off the frozen tapes (`size_usdc` from each window's own `roll`
record, `basis_guard_bp` from the minimum `guard_bp` it recorded), wallet
outcomes, RTDS corpus for the stream-fed xrp arm. Only the tunables vary.

Three knobs, all implemented dark in the submodule, all `serde(default)` to
today's exact values — **260/260 unit tests and 14/14 fixtures pass
unchanged**, which is the proof the defaults cannot move a live decision:

| knob | live | what it does |
|---|---|---|
| `decided_k` | 1.0 | decidedness needs `\|banked\| > k · cushion` |
| `decided_stale_s` | 0.0 | decidedness refused for N s after a staleness gate |
| `late_clip_mult` | ∞ | the last-`late_rem_s` ceiling held to m · `clip_usdc` |

Baseline (live policy): **176W-14L, 807 fires, $31,781 notional, net
−$1,187.53**. Replay runs 1.59× the real night's notional overall (it fills
every clip instantly at the ask), so read deltas, not levels.

### 2.1 Full-mode table

`forfeit` = P&L given up on windows the variant cut; `avoided` = loss
prevented on them; `reshaped` = windows not cut but not identical, where a
deferred clip refilled at a different ask.

| variant | W | L | net | **delta** | forfeit | avoided | reshaped | cut $ | wins lost |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| base | 176 | 14 | −1187.53 | +0.00 | — | — | — | 0 | 0 |
| k100 *(control)* | 176 | 14 | −1187.53 | **+0.00** | −0.00 | +0.00 | +0.00 | 0 | 0 |
| k110 | 167 | 14 | −1778.90 | −591.37 | −209.21 | +131.99 | −514.15 | 3118 | 32 |
| k115 | 165 | 14 | −1893.63 | −706.10 | −301.79 | +153.46 | −557.77 | 4707 | 37 |
| k125 | 160 | 12 | −484.43 | **+703.10** | −325.94 | +1077.35 | −48.31 | 7349 | 42 |
| k135 | 157 | 11 | −541.95 | +645.58 | −434.61 | +1142.17 | −61.98 | 8927 | 49 |
| k150 | 152 | 11 | −572.43 | +615.10 | −519.11 | +1173.50 | −39.29 | 10924 | 53 |
| stale30 | 176 | 14 | −1187.53 | **+0.00** | — | — | — | 0 | 0 |
| stale600 *(probe)* | 158 | 14 | −1506.46 | −318.93 | −256.47 | +293.59 | −356.05 | 5845 | 31 |
| m3 | 176 | 14 | −1204.99 | −17.46 | −140.77 | +134.99 | −11.68 | 3163 | 36 |
| m5 | 176 | 14 | −1272.88 | −85.35 | −89.79 | +4.44 | +0.00 | 928 | 15 |
| m10 | 176 | 14 | −1187.53 | **+0.00** | — | — | — | 0 | 0 |
| k125+m5 | 160 | 12 | −499.91 | +687.62 | −341.42 | +1077.35 | −48.31 | 7596 | 49 |
| k125+stale30 | 160 | 12 | −484.43 | +703.10 | −325.94 | +1077.35 | −48.31 | 7349 | 42 |
| k125+m5+stale30 | 160 | 12 | −499.91 | +687.62 | −341.42 | +1077.35 | −48.31 | 7596 | 49 |
| k150+m3+stale30 | 153 | 11 | −593.06 | +594.47 | −530.16 | +1174.48 | −49.86 | 11565 | 74 |
| **k125@15m** | 174 | 12 | −523.29 | **+664.24** | **−94.98** | **+807.67** | −48.45 | 1780 | **11** |
| k150@15m | 173 | 11 | −542.35 | +645.18 | −171.20 | +848.99 | −32.61 | 3425 | 18 |
| k125@5m | 162 | 14 | −1148.66 | +38.87 | −230.96 | +269.68 | +0.14 | 5569 | 31 |
| k150@5m | 155 | 14 | −1217.62 | −30.08 | −347.91 | +324.51 | −6.68 | 7499 | 35 |

`k100` and `m10` reproducing the baseline **to the cent** is what proves the
`--params` plumbing reaches `eval_model` rather than being silently dropped;
without those two rows nothing else in the table is admissible.

### 2.2 Robustness — where the headline numbers die

| variant | delta | moved | −top1 | −top3 | −top5 | better | worse | sign p | boot 2.5% | 97.5% |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| k110 | −591.37 | 51 | −695.47 | −720.44 | −728.21 | 9 | 42 | 0.000 | −1642.64 | +74.18 |
| k115 | −706.10 | 54 | −821.53 | −848.32 | −856.97 | 9 | 45 | 0.000 | −1779.73 | +4.83 |
| k125 | +703.10 | 60 | +351.01 | **−62.65** | −318.59 | 15 | **45** | 0.000 | −154.26 | +1794.69 |
| k135 | +645.58 | 63 | +293.49 | −291.48 | −486.31 | 9 | 54 | 0.000 | −359.30 | +1905.51 |
| k150 | +615.10 | 64 | +263.00 | −363.57 | −558.40 | 6 | 58 | 0.000 | −435.82 | +1914.56 |
| m3 | −17.46 | 42 | −128.80 | −152.45 | −153.62 | 5 | 37 | 0.000 | −271.80 | +285.30 |
| m5 | −85.35 | 16 | −89.79 | −89.48 | −89.08 | 1 | 15 | 0.001 | −230.01 | −3.27 |
| stale600 | −318.93 | 41 | −596.24 | −611.96 | −612.52 | 5 | 36 | 0.000 | −1106.16 | +489.51 |
| **k125@15m** | **+664.24** | 24 | +312.14 | −28.22 | −142.63 | **10** | **14** | **0.541** | −46.75 | +1581.09 |
| k150@15m | +645.18 | 26 | +293.09 | −203.81 | −208.73 | 4 | 22 | 0.001 | −208.81 | +1750.37 |
| k125@5m | +38.87 | 36 | −182.08 | −229.28 | −230.96 | 5 | 31 | 0.000 | −329.63 | +588.74 |
| k150@5m | −30.08 | 38 | −307.40 | −354.48 | −353.78 | 2 | 36 | 0.000 | −554.16 | +685.97 |

**Every global variant makes the typical window WORSE.** Global k1.25 moved
60 windows: 15 better, 45 worse, sign p < 0.001 *against*. Its whole +$703
is 3–5 catastrophic windows it avoided; delete them and it is −$62.65.

**`k125@15m` is the only variant in the study whose per-window sign test is
not significantly harmful** (10 better / 14 worse, p = 0.541). It is also
the only one with a forfeit/avoid ratio worth the name — 8.5:1 (−$95 for
+$808) against global k1.25's 3.3:1 — and it costs 11 winning windows
rather than 42.

### 2.3 Evals-mode cross-check, and where the modes disagree

`--mode evals` reaches the whole night (the book recorder only starts
02:45Z) and fills against an unbounded book. Independent corpus reach,
different fill model. Baseline **185W-13L, 583 fires, net −$1,674.15**.

| variant | full delta | evals delta | agree? |
|---|---:|---:|---|
| k100 / stale30 / m10 | +0.00 | +0.00 | ✅ controls exact |
| k110 | −591.37 | −419.94 | ✅ both negative |
| k125 | +703.10 | +231.01 | ✅ both positive |
| k135 | +645.58 | +784.79 | ✅ |
| k150 | +615.10 | +972.77 | ✅ but optimum differs (k1.25 vs k1.50) |
| m3 | −17.46 | **+211.36** | ❌ **sign flip** |
| m5 | −85.35 | **+265.51** | ❌ **sign flip** |
| stale600 | −318.93 | **+683.42** | ❌ **sign flip** |
| **k125@15m** | **+664.24** | **+236.22** | ✅ |
| k150@15m | +645.18 | +570.97 | ✅ |
| k125@5m | +38.87 | −5.21 | ✅ both ≈ 0 |
| k150@5m | −30.08 | **+401.32** | ❌ **sign flip** |

Evals-mode robustness repeats the story: global k1.25 is +$231 but −$497.61
with its top 3 removed, and moved 71 windows of which **63 got worse**.
`stale600`'s +$683 rests on **5 windows**, of which one — the 17:15Z
incident — supplies +$726.12 while the other four lose; sign p = 0.375.

**Three variants flip sign between modes. None of them is shippable on this
data**, and that includes every `late_clip_mult` setting.

### 2.4 The three named events

Full mode (`xrp-updown-5m-1787508300` replays with **zero** fires — the RTDS
recorder is a second subscriber and its dropped samples are not the live
arm's, so the 18:05Z column is the btc/eth/sol arms of that epoch only):

| variant | 17:15Z five-arm | 05:15Z eth/sol | 18:05Z epoch |
|---|---:|---:|---:|
| base | −700.70 (31f $697) | −498.89 (33f $901) | +3.91 (9f $251) |
| k125 | −437.00 (18f $434) | −494.44 (33f $896) | +3.91 |
| k150 | −376.19 (11f $374) | −505.57 (36f $902) | +3.91 |
| m3 | −566.69 (19f $564) | **−511.74** (32f $912) | +3.91 |
| m5 | −696.26 (30f $693) | −498.89 (33f $901) | +3.91 |
| stale30 | −700.70 | −498.89 | +3.91 |
| **k125@15m** | **−700.70 (unchanged)** | **−498.89 (unchanged)** | +3.91 |

Evals mode reaches the real xrp window:

| variant | 17:15Z five-arm | 05:15Z eth/sol | 18:05Z epoch |
|---|---:|---:|---:|
| base | −1280.83 (21f $1274) | −492.51 (10f $904) | +0.77 (7f $674) |
| k125 | −958.46 (14f $954) | −492.87 | −2.09 (6f $499) |
| k150 | −495.63 (9f $492) | −492.08 | −2.09 |
| m3 | −910.78 (14f $906) | −472.88 (9f $884) | −4.73 (4f $339) |
| stale600 | −554.71 (15f $551) | −492.51 | −2.09 |

**Read the k125@15m row twice. The tightening that pays does not touch a
single one of the three named events — all three are 5m — and the
tightening that moves them does not pay.** That is the study's most
uncomfortable result and the one most worth believing, because it is the
one every mode agrees on.

### 2.5 Why the 05:15Z event is immovable

Nothing moved it: −$498.89 → −$494.44 at k1.25, −$505.57 at k1.50, **worse**
at m3. The eval tape says why. Decidedness margin `|banked| / cushion` on
`eth-updown-5m-1787462100`:

```
rem=126  |b|/c 0.96  bd=False
rem=116  |b|/c 1.01  bd=True     <- flips, INSIDE the 120s clock unlock
rem= 86  |b|/c 1.26                 k=1.25 would flip here
rem= 69  |b|/c 1.56                 k=1.50 would flip here
rem= 33  |b|/c 1.97
```

`terminal_lock` makes the banked share grow and the cushion shrink as the
window closes, **by construction**. So in a window that is going to be
decided, `|banked|/cushion` rises monotonically and a `k`-multiple can only
change *when* decidedness flips, never *whether*. Here it flipped at rem
116, already inside `late_rem_s = 120` — so the clock had opened the full
budget anyway and `k` bought nothing but a few seconds of worse asks. That
is also why m3 made this window **worse**: capping room deferred clips into
a further-collapsed book (the `reshaped` column, −$514 at k110).

`decided_k` can only help where decidedness flips **well before** the 120s
clock. That is 15m windows. The 05:15Z and 17:15Z events are 5m, and the
whole 5m result follows from this one mechanism.

---

## 3. Verdict

### Ship

**`decided_k = 1.25`, scoped to 15m arms only** (`btc-updown-15m`,
`eth-updown-15m`, `sol-updown-15m` — all three still rolling as of 19:47Z).

| measure | value |
|---|---|
| full-mode delta | **+$664.24** (176W-14L → 174W-12L) |
| evals-mode delta | **+$236.22** (both modes agree in sign) |
| forfeited wins | **−$94.98** across 11 winning windows |
| avoided losses | **+$807.67** |
| ratio | **8.5 : 1** |
| notional cut | $1,780 of $31,781 (5.6%) |
| per-window sign test | 10 better / 14 worse, **p = 0.541** — the only variant not significantly harmful per window |
| −top3 trim | −$28.22 |
| bootstrap 95% | −$46.75 … +$1,581.09 |

The prior is independent of the fit: `LESSONS` L39 and `fourh_fit.md`
already say the `range_avg` banked proxy lies with duration, and the real
wallet-graded ledger says the same thing in dollars (+$89.70 at 5m,
−$649.15 at 15m). The A/B is the third witness, not the only one.

**Sell it as tail insurance, not edge.** −top3 is −$28.22 and the bootstrap
touches zero: the value is 3–4 catastrophic 15m windows in one night, and
n=4 is n=4. It buys a smaller left tail on the book that produced −$649.15
of carve-out losses, at a measured cost of $95 in forfeited wins.

### Do not ship

- **Any global `decided_k`.** k1.10 −$591, k1.15 −$706, k1.25 +$703 — the
  sign flips twice across 0.15 of range, and at every setting the *typical*
  window gets worse (45 of 60 at k1.25; 58 of 64 at k1.50). The positive
  settings are 15m windows dragging a 5m book along for the ride.
- **Any `decided_k` on 5m.** k125@5m is +$38.87 full / −$5.21 evals — zero,
  bought with 31–38 forfeited wins and $5.5k of churn. **This is the
  carve-out earning its keep**, and §2.5 is why: at 5m, decidedness flips
  inside the clock unlock, so `k` moves the timestamp and not the trade.
- **`decided_stale_s` at any N.** Inert at N=30 in **both** modes — 0
  windows moved. Not for want of stale gates: the tape carries 782 of them
  across 577 windows, spread evenly over the fleet (btc 171, eth 174, xrp
  173, sol 161, bnb 103 — xrp is only 22%, so this is not an rtds
  artifact). What is rare is the *conjunction* the knob needs — a stale gate
  followed inside N seconds by a fire that depended on the waiver. Widening
  N to a deliberately absurd **600s (the whole window)** still moves only
  **5 windows** in evals mode, and **+$726.12 of its +$683.42 is one
  window**, `eth-updown-5m-1787505300` — the 17:15Z incident itself. The
  other four all lose. That is the exact failure mode
  `analysis/correlation_study.md` names: a policy whose entire point
  estimate is the incident window wearing the policy's name. Sign p = 0.375
  on n=5, and the two modes flip sign (−$319 full / +$683 evals). **No
  evidence.**
- **`late_clip_mult` at any m.** m10 inert; m5/m3 flip sign between modes;
  and the premise does not survive contact with `size = (clip_usdc /
  ask).min(...)` — no fire all night exceeded ~1.01 clips, so a per-fire
  m ≥ 3 cap is unreachable by construction. The cumulative-ceiling form
  implemented here *can* bite, and it made the 05:15Z event **worse**.
- **Anything touching the last-120s unlock.** On the clock alone it is
  **+$211.93** over 305 fires, 293W-12L. It is the healthiest class in the
  ledger. The −$385.37 sitting next to it belongs to `banked_decided`, which
  would have unlocked the budget with or without the clock.

### Deploy note

`k125@15m` flips `sol-updown-15m-1787457600` from −$147.64 to $0. That is a
**committed fixture** — and per `fixtures/README.md` it is specifically the
tripwire for any `eval_model` re-spec, whose verdict "is *supposed* to
move". Shipping this needs a deliberate `--regen` on that one fixture with
the move justified in the commit message. Fixtures pass **14/14 unchanged**
today because they run the defaults.

---

## 4. Reproducing

```bash
# Freeze first — the live tapes are append-only, so a re-run is not a re-run.
cp ~/.pmt/engine/updown-tape.jsonl  $WORK/updown-tape-frozen.jsonl
cp ~/.pmt/engine/book-tape.jsonl    $WORK/book-tape-frozen.jsonl
cp ~/.pmt/corpus/outcomes.jsonl     $WORK/outcomes-frozen.jsonl
# Shadow $HOME: --mode full writes a klines cache under $HOME/.pmt/corpus.
mkdir -p $WORK/home/.pmt/corpus/rtds
cp ~/.pmt/corpus/klines-1m-*.jsonl        $WORK/home/.pmt/corpus/
cp ~/.pmt/corpus/rtds/rtds-20260823.jsonl $WORK/home/.pmt/corpus/rtds/

(cd pmengine && cargo build --release --features ec2)   # private flavor
python3 analysis/carveout_ledger.py
python3 analysis/carveout_ab.py                          # full mode
python3 analysis/carveout_ab.py --mode evals --work $WORK/ab-evals
python3 analysis/carveout_robust.py
```

Engine branch `carveout-ab`; submodule `pm-trade/pmt-strategies` branch
`carveout-ab` at **7a116ba**, based on 875153b (the sha pmt master
gitlinks — `origin/main`'s ee24b6a needs a `crate::series_guard` that has
not landed in public pmt). **Neither is pushed.**
