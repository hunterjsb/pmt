# Hybrid settle-rule A/B — momentum evidence, terminal risk

## Round 2 (2026-08-23, stream-fed, 60s settlement width) — **hybrid still loses at 5m**

Re-run of the 5m A/B on the terms the first round asked for: every arm
`feed=rtds` (the model reads the settlement stream directly, not the Binance
proxy) and `settle_tw_s=60`, the width `analysis/settle_width.md` established.

Driver: `analysis/hybrid_5m_ab.py`, report `analysis/hybrid_5m_ab_report.py`.
575 5m windows in the RTDS span with book coverage, 396 of them carrying truth
(77 wallet + 314 book + the 5 incident arms graded off the stream's terminal
rule, which nothing else covers). Live per-symbol tunables from
`arms-state.json` (theta .3, per-symbol guard/clip/size, pay-up), interleaved
fleet driver, shared `--fleet-cap 500`.

### Fleet, 396 graded windows

| variant | fired | clips | notional | pnl | ROI | W-L |
|---|---|---|---|---|---|---|
| `range_avg` @30s (today's width) | 85 | 460 | $16,470 | **−$1,144.70** | −6.95% | 79-6 |
| `range_avg` @60s | 78 | 361 | $13,172 | **−$76.56** | −0.58% | 73-5 |
| `hybrid` @60s | 42 | 159 | $5,514 | **−$118.55** | −2.15% | 39-3 |
| `terminal` @60s | 1 | 2 | $10 | +$0.37 | +3.88% | 1-0 |

Split at the incident, because it is the only thing in the corpus that loses:

| variant | ex-incident pnl | ex-incident W-L | incident pnl |
|---|---|---|---|
| `range_avg` @30s | −$552.78 | 79-2 | −$591.92 (4 arms) |
| `range_avg` @60s | **+$507.95** | **73-0** | −$584.50 (5 arms) |
| `hybrid` @60s | +$103.53 | 39-0 | **−$222.08 (3 arms)** |
| `terminal` @60s | +$0.37 | 1-0 | $0.00 (0 arms) |

### Per symbol (fired / pnl, all 396 windows)

| variant | bnb | btc | eth | sol | xrp |
|---|---|---|---|---|---|
| `range_avg` @30s | 9f / +$26 | 17f / +$118 | 26f / −$1,338 | 20f / +$57 | 13f / −$8 |
| `range_avg` @60s | 7f / +$9 | 15f / +$114 | 27f / −$185 | 18f / +$28 | 11f / −$43 |
| `hybrid` @60s | 6f / +$3 | 6f / +$34 | 13f / −$142 | 10f / −$12 | 7f / −$1 |

### The five-arm event (epoch 1787505300, 17:15:00Z)

Truth from the settlement stream at 60s: **all five settled UP** — bnb +3.81bp,
btc +7.29bp, eth +2.28bp, sol +7.97bp, xrp +11.09bp. The engine's own book tape
stops 84s before the wire, so no wallet redemption and no book pin grades this
window; the stream is the only witness, and it reproduces the operator's
reported up-settlement on all five.

| arm | `range_avg` @60s | `hybrid` @60s |
|---|---|---|
| bnb | 1 clip, −$9.87 | **skipped** |
| btc | 3 clips, −$113.39 | **skipped** |
| eth | 12 clips, −$328.95 | 3 clips, −$167.07 |
| sol | 3 clips, −$80.52 | 1 clip, −$50.43 |
| xrp | 9 clips, −$51.77 | 1 clip, −$4.58 |
| **total** | **−$584.50** | **−$222.08** |

**Hybrid shrinks the event by 62% (−$362.42). It does not prevent it** — it
still enters three of the five arms on the wrong side. Only pure `terminal`
avoids it outright, and pure terminal fires twice in ten hours.

### Verdict: NO-GO on hybrid at 5m

Hybrid buys $362 of incident protection for $404 of forgone edge, and it is
not a discriminator — it is a size reducer. The 36 windows `range_avg` fired
that hybrid skipped entirely are worth only **$58.74** between them (34 winners,
2 losers). The rest of the give-up is smaller clips on the same windows:
hybrid runs 42% of the notional for 20% of the P&L, so its return per dollar
on winners is *worse* (1.96% vs 4.03%). That is the shape of paying up for
certainty on a corpus where certainty was needed once.

Break-even is an incident rate above ~1.1 per 10.4h span. We observed one.
This is a coin-flip call resting on n=1, and the tie goes to the rule with the
proven live record — same conclusion the first round reached, now on stream-fed
data at the right width.

**5m LIVE: stays `range_avg`. Hybrid code stays dark behind `settle_rule`.**

### The width, not the rule, is the deployable finding

`range_avg` @30s → @60s is a +$1,068 swing. Read it honestly: **96% of it is
one window.** `eth-updown-5m-1787494500` (14:15:00Z) is a second range_avg
wrong-side entry of exactly the incident's class — the terminal rule says DOWN
at both widths (−6.09bp / −7.10bp) and the market resolved DOWN, while the
range-avg momentum proxy read **+6.86bp** off the 30s marks and only **+2.84bp**
off the smoother 60s marks. At 30s the arm cleared its edge floor and put 16
clips into the wrong side for −$1,021.80; at 60s the same proxy fell below the
floor and the arm never fired. Strip that window out and the two widths are a
wash (−$46 on $16k of notional).

So the width does not make the model *right* — it damps the momentum proxy just
enough that one catastrophic entry didn't clear the gates. The case for
changing it rests on `analysis/settle_width.md`'s grading evidence (6-0 on
every discriminating window), not on this P&L.

### What to deploy

```jsonc
// ArmParams — 5m arms only
"settle_tw_s": 60.0,     // was: implicit 30 via settle_tw_secs()
"settle_rule": "range_avg"   // UNCHANGED — hybrid stays dark
```

Plus `outcomes.ck_settlement_width_s` → 60 at 5m, or the Chainlink fallback
grader keeps writing 30s verdicts into the corpus.

**Scope this honestly before shipping.** `settle_tw_s` reaches three places,
and under today's live config two of them are inert:

1. **Grading** (`ck_settlement_width_s`) — live effect now, and the reason to
   ship. 30s currently mis-grades ~1 window in 40.
2. **`per_min` topic for `feed=rtds` arms** (`twap_topic_for`) — this is what
   moved the P&L above. Today only the xrp 5m arm runs `rtds`; the other four
   are `binance` and take their marks from klines, where the width is not
   consulted at all.
3. **`terminal_lock`** — used only by `settle_rule` `terminal`/`hybrid`, both
   dark. `eval_range_avg` never calls `settle_tw_for`.

So on the live fleet as configured today this change is a **grading fix plus a
one-arm model change**, not a fleet-wide one. The replay's +$1,068 is what it
would be worth *if the 5m fleet were stream-fed*. Moving 5m to `feed=rtds` is a
separate decision with its own evidence bar — note the recorder's second-
subscriber caveat, and that a dropped stream gates every rtds arm at once.

### Caveats carried forward from round 1

- The fill sim's ABSOLUTE pnl is not wallet truth: it fills clips live latency
  and the delta matcher would have suppressed, and it holds to settlement where
  the live engine exits. It prices the incident at −$584 where the wallet lost
  ~$230. Only the RELATIVE read between variants is used.
- The wallet-only truth set (77 windows) cannot show risk reduction at all —
  every variant goes W-L n-0 on it, because it contains only windows the gates
  chose to trade and the fleet mostly won. `range_avg` +$243.89 on 47 fired vs
  hybrid +$72.89 on 25 fired there is selection, not evidence. The book-extended
  set is the control.
- One incident in one 10.4h span. Every frequency argument here is n=1.

---

## Round 1 (2026-08-23, minute-grain feed, 30s width)

`settle_rule="hybrid"`: p_up / margin / banked evidence from the range-avg
momentum proxy (what the wallet proved at 5m), cushion + banked_decided +
flip_proof from TERMINAL-rule arithmetic (what actually settles). Replay
`--mode full`, live-policy params (theta .3, pay-up .02, per-symbol guards),
outcomes = the L36-cleaned corpus (wallet/chainlink≥5bp/terminal-book).

### Results (graded windows only, aggregate rows excluded)

| set | rule | fired | clips | notional | pnl | W-L |
|---|---|---|---|---|---|---|
| 15m (47w) | range_avg | 25 | 235 | $7,689 | **−$654.22** | 21-4 |
| 15m (47w) | hybrid    | 2  | 3   | $100   | **+$9.21**   | 2-0 |
| 5m (298w) | range_avg | 63 | 307 | $9,439 | −$536.02 | 59-4 |
| 5m (298w) | hybrid    | 22 | 69  | $2,762 | −$42.55  | 20-2 |

Hybrid skipped every 15m catastrophe (−$412, −$403, −$208, −$60 — all
wrong-side banked_decided entries under range-avg) and the 5m tail
(eth −$463→−$75, sol −$200→−$26, btc −$125→$0), at the cost of ~2/3 of
5m volume and the 15m wins.

### Caveats

- The fill sim's ABSOLUTE pnl is not wallet truth: it shows range_avg 5m
  deeply negative over a span where the live fleet was near break-even —
  it fills clips live latency/matcher suppression would have missed and
  holds to settlement where live exits. Only the RELATIVE read is used.
- Hybrid barely banks anything at 15m because the minute-grain per_min
  feed sees the forming 60s settlement TWAP as ONE sample — the lock is
  invisible until the wire. Sub-minute RTDS feed is the unlock.

### Verdict

- 5m LIVE: stays range_avg. Proven live record beats a sim that trades
  3× less against the scale-up direction.
- 15m: stays PARKED. Hybrid is the proven-safe spec — re-arm gate is the
  RTDS-fed model (ref/spot/locked-frac from the settlement stream itself),
  then rerun this A/B expecting real 15m volume.
- Code ships DARK behind settle_rule (default range_avg unchanged).

**Round 2 note:** the "RTDS-fed model" re-arm gate above is now satisfied for
5m, and hybrid still loses there. The 15m re-run is still owed — round 2 only
covered 5m.
