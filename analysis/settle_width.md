# Settlement TWAP width at 5m — 30s or 60s? (2026-08-23)

**Verdict: 60s. The 5m assumption of 30s is wrong.** Every window that can
tell the two apart says 60s, 6–0.

Reproduce: `uv run --project pmtrader python analysis/settle_width.py`

## Where the width lives

| site | today |
|---|---|
| `pmengine/src/strategies/updown_model.rs:448` `settle_tw_secs()` | `30.0` if window ≤ 300s else `60.0` |
| `pmengine/src/strategies/updown_rtds.rs:136` `twap_topic_for()` | picks `crypto_prices_twap_thirty` when width ≤ 30 |
| `pmtrader/polymarket/outcomes.py:73` `ck_settlement_width_s()` | same 30/60 split, for the Chainlink fallback grader |

`settle_tw_secs` is the single source: it drives `terminal_lock`'s
`locked_frac` / cushion, `eval_terminal`'s reference, and — through
`twap_topic_for` — *which stream topic fills `per_min`* for an rtds arm. One
constant, three consequences.

## Method

The RTDS recorder corpus (`~/.pmt/corpus/rtds/rtds-20260823.jsonl`, 776k rows,
08:28:55–18:30:26Z, 8 symbols, zero recorder gaps) carries all three topics at
1 Hz: `crypto_prices_chainlink` (spot), `crypto_prices_twap_thirty`,
`crypto_prices_twap_sixty`. So the width question needs no reconstruction —
both candidate settlement series are recorded verbatim.

For each graded window, the terminal rule is applied directly: winner is `up`
iff the settlement-width TWAP print AT range end exceeds the print AT range
start. A print may be up to 3s stale to stand in for "the value AT ts", and
the lookup never looks forward.

## The wallet cannot answer this question

Wallet redemptions are ground truth, and 72 of them fall entirely inside the
corpus. Both widths grade **72/72**. That is not a tie between two right
answers — it is a sample with no discriminating windows in it at all:

- Across all 880 5m windows the corpus covers, the two widths pick different
  winners on only **27 (3.1%)**.
- The wallet population is worse than that: we only hold windows we *filled*,
  and the arms fire on momentum, so our windows are systematically far from
  the near-tie region where width matters.

72 windows at a 3% base rate expects ~2 discriminating cases; observing 0 is
unremarkable. The wallet is silent here, not supportive.

## The terminal book can answer it — and it is a validated proxy

`outcomes.book_outcome` grades a window from the market's own book pinned
≥0.95 in the final 15s. It is independent of our model and of the Chainlink
stream. Before leaning on it, it was checked against the wallet on every
window the wallet graded:

> **book vs wallet: agree 168, disagree 0, ungradable 54.**

Zero observed error against ground truth. On the 284 book-graded 5m windows
inside corpus coverage:

| width | agrees with truth | misses |
|---|---|---|
| 30s | 277/284 (97.5%) | 7 |
| **60s** | **283/284 (99.6%)** | **1** |

### The six windows that flip

| window | truth | 30s says | 60s says | right |
|---|---|---|---|---|
| `bnb-updown-5m-1787475900` | up | down −0.76bp | up +0.21bp | 60s |
| `bnb-updown-5m-1787478900` | up | down −1.19bp | up +0.75bp | 60s |
| `eth-updown-5m-1787482800` | up | down −0.19bp | up +0.11bp | 60s |
| `bnb-updown-5m-1787484300` | up | down **−6.64bp** | up +0.18bp | 60s |
| `bnb-updown-5m-1787484600` | down | up +0.08bp | down −0.71bp | 60s |
| `xrp-updown-5m-1787493900` | up | down **−2.40bp** | up +5.83bp | 60s |

**6 of 6 to 60s.** Under a fair coin that is p = 1/64 ≈ 1.6%. Two of them are
not near-ties for the 30s reading either: bnb at −6.64bp and xrp at −2.40bp
are confident 30s calls that the market resolved the other way.

The single 60s miss, `eth-updown-5m-1787495700` (truth up, 60s says down at
**−0.05bp**), is inside any noise floor — 30s missed it too, at −1.42bp.

## Ruling out the boring explanation

If `twap_thirty` were simply a mislabeled or lagged relay, the above would say
nothing about the settlement rule. It is not. Trailing W-second means rebuilt
from the 1 Hz chainlink topic match the topic that claims that width, by a
factor of ~6:

| reconstruction | \|err\| vs `twap_thirty` | \|err\| vs `twap_sixty` |
|---|---|---|
| btc trailing 30s | **0.118bp** | 0.703bp |
| btc trailing 60s | 0.515bp | **0.095bp** |
| eth trailing 30s | **0.173bp** | 1.004bp |
| eth trailing 60s | 0.688bp | **0.139bp** |

Both topics carry exactly the width they advertise. The 30s series is real and
correctly stamped — it is just not what settles these markets.

## A note on the older corpus hint

Regrading off the on-chain round corpus (`~/.pmt/corpus/chainlink-*.jsonl`)
instead of the stream gives the *opposite*, weaker read (5m: 30s 101/104, 60s
100/104). That corpus is not usable for this question: its median round cadence
is **33s**, so a 30s TWAP window usually contains a single round and the "30s
TWAP" degenerates into a point-in-time price. Simulating that cadence against
the recorded stream measures the damage — at 33s sampling, a reconstructed
width recovers its own true winner only ~95% of the time, an error rate larger
than the entire effect being measured. Whichever direction that corpus leans,
it leans on noise. Only the recorded stream settles this.

## Independent corroboration

`analysis/correlation_study.md` Result 0 reached the same place from the other
direction, grading settlement *rules* rather than widths over its own (shorter,
17:39Z-truncated) slice of the same stream:

| rule | book (unselected) |
|---|---|
| `terminal` (30s at 5m) | 282/289 — 97.6% |
| `terminal_t60` | 288/289 — **99.7%** |

97.6% vs 99.7% there; 97.5% vs 99.6% here. Two studies, different window sets,
same gap. That study did not name the width as the finding — it was chasing the
range_avg-vs-terminal error — so this is a genuinely separate confirmation
rather than a restatement.

## Consequence

`settle_tw_secs` should return `60.0` for every window, not just windows wider
than 5m — which collapses it to a constant and makes `twap_topic_for` always
choose `crypto_prices_twap_sixty`. Three things move with it:

1. **Grading.** `ck_settlement_width_s` should follow, or the Chainlink
   fallback grader keeps writing 30s verdicts into the outcomes corpus.
2. **`per_min` for rtds arms.** An rtds 5m arm's range-start reference becomes
   the 60s print, i.e. the actual settlement reference.
3. **`terminal_lock`.** `locked_frac` at `rem` seconds out halves: at 15s
   remaining a 5m window is 75% locked under 60s, not 50% as priced today. The
   terminal rule banks evidence *earlier* and with a smaller residual cushion
   than the current constant implies.

Item 3 is why this matters beyond bookkeeping: it is a direct input to
`eval_hybrid`'s `banked_decided` / `flip_proof`, so the hybrid A/B has to be
re-run at 60s before its verdict means anything. That is the next task.
