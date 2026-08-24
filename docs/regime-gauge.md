# The regime gauge — leader persistence

**Status: MEASUREMENT ONLY.** Nothing in pmt reads this number to make a
decision. The estimator (`pmtrader/polymarket/regime.py`), the report
(`pmt crypto regime`), the watch header row and the corpus file
(`~/.pmt/corpus/regime.jsonl`) exist so the quantity is *visible and joinable*.
The sizing hook in §3 is written down and deliberately **not wired**.

---

## 1. What it measures

One number, on windows only, never on fills:

> Of the windows where the book had a leader at elapsed 0.25, how often did
> that leader go on to win?

The exact definition — the 0.25 mark, the 1000 ms two-sided freshness bound,
the `|de-vigged − 0.50| > 0.05` lead threshold, terminal-source grading, the
window-END ordering — is frozen in `regime.METHOD` and spelled out in that
module's docstring. Read it there; this file does not restate it, because two
copies of a definition is how they drift.

## 2. Why it is worth a gauge

`pmt-alpha/analysis/underdog_search.md` §5. Across the study's train/holdout
cut (2026-08-23 22:00Z) the quantity moved:

| | train (< 22:00Z) | holdout (≥ 22:00Z) |
|---|---:|---:|
| median \|terminal margin\| | 9.39 bp | **12.71 bp** (1.35×) |
| \|de-vigged book − 0.50\| at elapsed 0.25 | 0.2000 | 0.1850 |
| **the book's leader at 0.25 went on to win** | **79.7%** [76.1, 83.0] | **71.5%** [67.6, 75.2] |

Two-proportion z on lead persistence: **+3.12**.

Realized volatility rose by a third; the book's own priced confidence barely
moved. That matters because **a binary's price band IS a volatility position**
— buying the favourite is short volatility, buying the dog is long it. So when
the early leader stops holding, every favourite band gets worse and every dog
band gets better, *together*. That is exactly what the study's holdout shows:
in elapsed [0.00, 0.25) the favourite-longshot bias **inverts outright** — dog
bands 0.05–0.30 return +7.96% to +40.93% while favourite bands 0.60–0.95 return
−5.95% to −14.66% — and only there; every other elapsed band keeps the training
sign.

The honest statement of the problem the gauge addresses is the study's own:
*the dog/favourite axis is a volatility position, the fleet takes it blind, and
its sign flipped inside 24 hours.*

The study's recommendation was measurement first: "measure the regime before
betting on the band." That is this.

## 2b. What the first backfill says

633 resolved windows, 2026-08-23 10:50Z → 08-24 02:35Z, fleet-wide trailing-50
sampled hourly:

```
08-23 12:00Z  76.0%      08-23 19:05Z  78.0%      08-24 00:00Z  92.0%
08-23 13:00Z  76.0%      08-23 20:00Z  82.0%      08-24 01:00Z  92.0%
08-23 14:00Z  82.0%      08-23 21:00Z  82.0%      08-24 02:10Z  92.0%
08-23 15:00Z  80.0%      08-23 22:10Z  84.0%
08-23 16:00Z  84.0%      08-23 23:05Z  82.0%
08-23 17:00Z  82.0%
08-23 18:00Z  76.0%
```

The estimator reproduces the study's training figure **exactly** — 5m windows
before the 22:00Z cut read 417/523 = 79.7% [76.1, 83.0], the same k and n
`underdog_regime.txt` printed. That is the check that matters: the live gauge
and the vault's are one measurement, not two.

The rise to 92% after 00:00Z is **not** a clean "the regime recovered" — read
§4.1 before quoting it. Every observation in that stretch is wallet-graded.

## 3. The sizing hook — PROPOSED, DARK, NOT WIRED

What a wired version would look like, written down so the A/B has something
specific to test rather than a vibe:

```
gauge  = fleet leader-persistence, trailing 50 resolved windows
trigger: gauge < 0.75  (the study's holdout sat at 0.715; its train at 0.797)
action : SPECULATIVE size halves. Nothing else changes — no gate flips,
         no side is refused, no exit rule moves.
```

Scope, precisely:

- **"Speculative" means the arm's `--size` budget**, the per-window ceiling —
  not `--clip`, which is a fill-shaping parameter, and not the number of
  windows. Halving the budget halves exposure without changing what the
  strategy believes.
- **Per-series or fleet-wide?** Fleet-wide. The gauge's per-series n is small
  (a trailing 50 on one series is most of a day) and L40 already says five arms
  are 1.21 independent bets — a per-series knob would be five noisy copies of
  one fact.
- **Not pilot2.** Its risk law is hard-coded with no knobs on purpose
  (RETROSPECTIVE §1.1). A regime multiplier there would be a second law.
- **Hysteresis is mandatory.** A raw threshold on a rate with a ±5pp Wilson
  interval will flap. The obvious form is to size down when the trailing-50
  Wilson *upper* bound falls below 0.75 and size back up when the *lower* bound
  rises above it — a band, not a line.

## 4. What has to be true before it is wired

1. **The gauge has to be trustworthy on live data**, which today it is not,
   and the report says so in two places rather than one:

   - **Corpus lag.** The outcomes corpus is refreshed by `pmt crypto outcomes`
     / `stats --gates`, not by the estimator. At the first backfill it ended at
     08-24 02:35Z while the book tape reached 11:15Z — 8h40m behind, with 612
     marked windows waiting on a grade, and only 37% of the trailing block's
     own span graded at all.
   - **Grade selection, which is the sharper problem.** A `wallet` grade exists
     because we *traded* the window; a `resolution` grade exists whether we did
     or not. On the first backfill those two populations do not agree:

     | grade source | leader persistence | n |
     |---|---:|---:|
     | wallet | **92.5%** [87.8, 95.5] | 186 |
     | resolution | **76.3%** [72.1, 80.0] | 447 |

     Two-proportion z = **+4.73**. That 16-point gap is the engine's own entry
     filter — it fires when the model agrees with the book's direction and the
     cushion holds, which is close to a definition of "the leader is going to
     hold". And wallet rows grade FIRST (a redeem posts in minutes; gamma
     resolution lands when the report walks it), so **the most recent slice of
     the gauge is its most selected slice**: every one of the 78 post-22:00Z
     observations in the first backfill was wallet-graded, and they read 93.6%.

   A hook fed by that is a hook fed by our own fills. The estimator prints both
   caveats beside the headline for exactly this reason; the fix is a corpus
   refresh, not a code change.
2. **An A/B, on windows, with a pre-registered cut.** The claim to test is not
   "persistence predicts the band's sign" (the study established that on its
   own holdout) but "halving size on the trigger beats not halving it, net of
   the windows it declines". Those are different claims and only the second one
   justifies the wiring.
3. **A latency answer.** The gauge is settlement-graded, so it can only ever
   describe the regime that *has already happened*. The A/B must measure the
   hook against the lag it actually runs at, not against a same-window oracle.

Until all three land, the number is a dial on the dashboard and nothing more.

## 5. Reading it

```bash
pmt crypto regime                 # fleet + per-series, appends new corpus rows
pmt crypto regime --tenor 5m      # the study's own scope
pmt crypto regime --dry-run       # print, write nothing
pmt crypto regime --rebuild       # re-cut ~/.pmt/corpus/regime.jsonl from scratch
pmt crypto regime --json          # the estimate, machine-readable
```

The corpus file carries one row per resolved window with the gauge state **as
of that window**, both series-scoped and fleet-scoped, stamped with `method`.
That is what makes it joinable: a study can attach the regime the fleet was
actually in to any window it is scoring, without re-deriving anything.

`pmt crypto watch` shows the fleet gauge as one header row and drops the row
entirely on a box that has never run the estimator — "we have not measured
this" and "the leader never holds" are opposite facts and must not share a
rendering. Its `h` modal explains the row.

The report always prints, under the headline: how many graded windows carried a
leader and why the rest didn't, how far the outcomes corpus trails the book
tape, and the wallet-vs-resolution split from §4.1. None of those is optional
decoration — each one is a way the headline can be wrong.
