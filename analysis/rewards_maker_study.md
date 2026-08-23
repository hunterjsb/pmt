# Do liquidity rewards invert the maker verdict?

Study date 2026-08-23, data 02:45Z–18:10Z. Driver: `analysis/rewards_maker_study.py`.
Inputs: `~/.pmt/engine/book-tape.jsonl` (15.3h, 800 windows, 61,022 two-sided
samples), `~/.pmt/engine/updown-tape.jsonl` (model `p_up`), plus a 45-minute live
full-depth sample (880 samples over 42 markets) taken for this study.

```
python3 analysis/rewards_maker_study.py config          # live, ~1 min
python3 analysis/rewards_maker_study.py depth           # offline
python3 analysis/rewards_maker_study.py adverse --size 200 --frac-lo 0 --frac-hi 0.9 --gates 0.2
python3 analysis/rewards_maker_study.py net --frac-lo 0 --frac-hi 0.9 --gates 0.2
# `live` needs a forward-collected depth tape; books are not backfillable:
python3 analysis/rewards_maker_study.py collect --collect-secs 2700 --depth-tape /tmp/d.jsonl
python3 analysis/rewards_maker_study.py live --depth-tape /tmp/d.jsonl
```

The 2.2MB depth tape behind §0.2 and the L1-sufficiency claim is not committed —
re-run `collect` to regenerate it. Everything else reproduces from `~/.pmt`.

**Verdict: no. Rewards are real, fully funded, and worth about 6–7% of the
adverse-selection bill at reward-qualifying size. They close less of the hole than
the maker rebate does.** `analysis/firsthalf_research.md`'s conclusion stands with
reward income added.

Three premises in the brief were wrong and each is load-bearing. They are corrected
below before the economics, because two of them cut in our favour and one of them is
the reason the trade still fails.

---

## 0. Three corrections

### 0.1 The program IS funded on 5m and 15m. gamma lies about it.

`analysis/firsthalf_research.md` concluded the reward program was dead because
`/rewards/markets/{cid}` returned an empty program. That was true when it was
measured and is **false now**. The confusion is that the two APIs disagree:

| source | field | says |
|---|---|---|
| gamma `/markets?slug=` | `clobRewards` | funded on **7 of 21** series (the 4h ones only) |
| CLOB `/rewards/markets/{cid}` | `rewards_config[].rate_per_day` | funded on **21 of 21** |

The CLOB is authoritative — it is what the reward engine scores against. Every one
of the 21 live rates matches the docs' August allocation ÷ 30 **exactly**:

```
btc/5m   $10000.00/day   docs $300k/30 = $10000.00   yes
btc/15m   $7500.00/day   docs $225k/30 =  $7500.00   yes
btc/4h    $1666.67/day   docs  $50k/30 =  $1666.67   yes
eth,sol,xrp,hype /5m  $1666.67  /15m $833.33  /4h $333.33   all yes
bnb,doge         /5m   $833.33  /15m $416.67  /4h $166.67   all yes
------------------------------------------------------------------
total across the 21 crypto-TWAP series: $33,333.33/day = $1M/30
```

The 5m/15m reward-config ids are `1579xxx`; the 4h ids are `1577xxx`. The 4h configs
carry `start_date: 2026-08-23` and the 5m/15m ones were created later the same day.
**The program was switched on today, in stages, 4h first.** A probe of
`btc-updown-5m-1787507700` at 17:53Z returned `count: 0`; the same probe against
`btc-updown-5m-1787508300` at 18:08Z returned `rate_per_day: 10000`.

Consequence for everything below: **the competing depth in our 15h book tape is
mostly PRE-reward depth.** Every share estimate here is an upper bound.

### 0.2 `rewardsMaxSpread` is 1.5c, not 4.5c

4.5c is a creation-time default. It flips to 1.5c one to two and a half minutes into
each new window, per symbol, measured directly in the live depth tape:

```
bnb-updown-5m-1787507700  [(40,4.5) (60,4.5) ... (140,4.5) (160,1.5) (180,1.5) ...]
eth-updown-5m-1787507700  [(40,4.5) (60,4.5) (80,4.5) (100,1.5) (120,1.5) ...]
btc-updown-5m-1787507700  [(40,1.5) ... already 1.5 by the first sample]
```

Every 15m and 4h market sampled was 1.5c throughout. **Read it per market, per
moment; do not hardcode either value.**

This correction is what makes the whole study tractable. At v=1.5c on a 1c tick grid,
only the touch can score — an order one tick behind the touch is already at s≥1.5c and
scores exactly zero. Measured on full depth: **93.3% of v=1.5c samples have ≤1
qualifying bid level**, the rest have 2. So the L1 book tape *is* the entire
qualifying book, and 15 hours of it can be used instead of 45 minutes of L2.

### 0.3 The complete-set "lock" is real, and it is capped at one cent

The brief's "buying up@0.49 + down@0.49 = $0.98 for a $1 redemption is a LOCK" is
true but the reward formula caps how much of it you can take.

A two-sided quote is a bid on UP at `b` and a bid on DOWN at `d`. The effective UP
ask is `1-d`, so the quote's width is `1-d-b` and each side sits `s = (1-d-b)/2`
from the mid. Qualifying requires `s < 1.5c`, i.e. **`b + d > 0.97`**. So:

| quote width | s | S(1.5, s) | complete-set lock if both fill |
|---|---|---|---|
| 1c (`b+d=0.99`) | 0.5c | **0.4444** | 1c |
| 2c (`b+d=0.98`) | 1.0c | 0.1111 | 2c |
| 3c (`b+d=0.97`) | 1.5c | **0.0000** | 3c |

Widening to double the lock costs **4x** the score; widening to triple it scores
nothing. The reward-maximising quote is the 1c-wide one, whose lock is exactly one
tick. And one tick is what the book already offers: median pair bid-sum is 0.9900 on
btc and eth (both 5m and 15m), 0.93–0.97 on the thin symbols. This is the mirror-book
identity `analysis/firsthalf_research.md` §1 established — UP and DOWN are two views
of one price, and the pair sum is `1.00 ± one tick` by exchange construction.

---

## 1. Q2 — the daily pot per market

`rate_per_day` is configured **per market**, and each individual 5-minute window is
its own market with its own condition_id and its own config row. A market live for
only part of the UTC day accrues its pro-rata slice:

```
pot_per_window = rate_per_day x (timeframe_seconds / 86400)
```

This is pinned by the 4h numbers, which reconcile exactly: $1666.67/day x (4/24) =
$277.78/window x 6 windows/day = $1666.67/day = $50k/30 = the docs' btc-4h line.

| symbol | 5m | 15m | 4h |
|---|---|---|---|
| btc | **$34.72** | **$78.12** | **$277.78** |
| eth, sol, xrp, hype | $5.79 | $8.68 | $55.56 |
| bnb, doge | $2.89 | $4.34 | $27.78 |

The operator's $33/window estimate for btc-5m was right; the exact figure is $34.72
(the program divides by 30, not by the 31-day calendar month).

**Distribution semantics.** Scoring samples once per minute. Within each sample every
maker's `Q_min` is normalised across all makers (`Q_normal`, eq. 5); those are summed
over the epoch (eq. 6) and normalised again (eq. 7). The net effect is that
**each sample distributes an equal slice of the market's pot**, and your take is the
time-average of your per-sample share. A 5m window gets ~5 samples. The docs' "10,080
samples" is a 7-day epoch and does not survive contact with a market that lives five
minutes; what matters is that eq. 7 renormalises, so the full pot is paid out as long
as at least one maker qualifies. Paid daily at 00:00 UTC to maker addresses, $1
minimum.

---

## 2. Q1 — competition

Rival qualifying liquidity at v=1.5c, minSize=50, from 15.3h of L1. `Q_min` is the
aggregate book's score under eq. 4. Rivals are collapsed into a single competitor,
which **overstates** their combined `Q_min` (min is superadditive) and therefore
understates our share — conservative.

| sym/tf | n | Q_min=0 | med Q_min | p90 Q_min | med touch | 1c-book |
|---|---|---|---|---|---|---|
| btc/5m | 11,679 | 0.09 | 106.5 | 269.4 | 166 sh | 0.95 |
| btc/15m | 4,229 | 0.22 | 39.9 | 134.0 | 66 sh | 0.92 |
| eth/5m | 12,777 | 0.60 | 0.0 | 30.4 | 20 sh | 0.80 |
| eth/15m | 4,134 | 0.29 | 12.0 | 48.3 | 50 sh | 0.65 |
| sol/5m | 12,755 | 0.65 | 0.0 | 24.3 | 50 sh | 0.24 |
| bnb/5m | 9,046 | 0.90 | 0.0 | 0.0 | 18 sh | 0.13 |
| xrp/5m | 5,415 | 0.95 | 0.0 | 0.0 | 30 sh | 0.10 |

On btc the touch is genuinely contested (median 166 shares). Everywhere else,
qualifying liquidity is **absent the majority of the time** — the 50-share minimum
alone disqualifies most of the book, and on bnb/xrp the spread is 6c wide so nothing
is within 1.5c of the mid at all.

Score share if we join the touch two-sided with X shares a side:

| sym/tf | X=50 | X=100 | X=200 | X=500 |
|---|---|---|---|---|
| btc/5m | 0.283 | 0.406 | 0.554 | 0.736 |
| btc/15m | 0.532 | 0.678 | 0.797 | 0.882 |
| eth/5m | 0.882 | 0.931 | 0.955 | 0.976 |
| sol/5m | 0.892 | 0.924 | 0.949 | 0.973 |
| bnb/5m | 0.977 | 0.983 | 0.989 | 0.994 |
| xrp/5m | 0.980 | 0.986 | 0.990 | 0.995 |

Gross reward per day at those shares — the number that makes the trade look good:

| sym/tf | pot/win | X=50 | X=100 | X=200 | X=500 |
|---|---|---|---|---|---|
| btc/5m | $34.72 | $2,827 | $4,057 | $5,543 | $7,362 |
| btc/15m | $78.12 | $3,992 | $5,087 | $5,974 | $6,613 |
| eth/5m | $5.79 | $1,470 | $1,551 | $1,591 | $1,627 |
| xrp/5m | $5.79 | $1,633 | $1,643 | $1,651 | $1,659 |

**The thin symbols are where the shares are, but the pots are 6–12x smaller there,
which is presumably why they are thin.** The one place a large pot meets a thin book
is btc/15m.

**Honest unknown — rival elasticity.** These books were rewarded for at most part of
today. $10,000/day on btc-5m against a 166-share touch is not an equilibrium; if it
were, professional makers would already be there. Either depth rises sharply over the
coming days, or there is a reason they are staying away that this study has not found.
Both readings argue against sizing into it now. Our books also only cover markets we
trade (btc/eth/sol/bnb/xrp 5m, btc/eth/sol 15m since ~17:45Z) — no doge, no hype, and
4h only from the 45-minute live sample.

---

## 3. Q3 — adverse selection at qualifying quotes

Simulation: join the touch on both books, requote every 5s, size X a side, maker fee
0, mark to window resolution (windows that do not resolve decisively are dropped).
Two fill models bracket the truth:

- **queue-front** — we are first at the level, filled when the opposing ask touches
  our price.
- **sweep-only** (default) — we sat behind the displayed size, filled only when a
  sweep carries the ask strictly through our price. This is the conservative
  queue-ahead convention of `firsthalf_q3_maker.py`.

**Validation against the prior study.** At queue-front, 100 shares: btc/15m
**−2.56c/share**, eth/15m **−2.22c/share**. `firsthalf_research.md` measured −1.9 to
−2.5c/share on the same books with a print-driven fill model. The models agree.
Sweep-only is 2–3x worse (btc/5m −5.87c/share) because it conditions fills on the
most toxic subset — the aggressor that cleared the whole level.

Full window (frac 0.0–0.9), 200 shares a side, sweep-only:

| sym/tf | wins | act.f | fills/w | reward/w | adverse/w | net/w | c/share | b/e pot/w | pot mult |
|---|---|---|---|---|---|---|---|---|---|
| btc/5m | 179 | 0.71 | 25.5 | $12.37 | **−$188.94** | −$176.57 | −5.87 | $530 | 0.065 |
| btc/15m | 25 | 0.80 | 60.8 | $47.99 | **−$197.76** | −$149.78 | −3.79 | $321 | **0.243** |
| eth/5m | 178 | 0.73 | 26.2 | $3.69 | −$78.73 | −$75.04 | −5.45 | $124 | 0.047 |
| eth/15m | 25 | 0.78 | 55.6 | $5.22 | −$160.62 | −$155.41 | −4.55 | $267 | 0.032 |
| sol/5m | 177 | 0.73 | 14.6 | $1.68 | −$54.59 | −$52.91 | −6.67 | $188 | 0.031 |
| bnb/5m | 115 | 0.75 | 8.0 | $0.46 | −$34.80 | −$34.35 | −8.35 | $221 | 0.013 |
| xrp/5m | 74 | 0.75 | 10.2 | $0.66 | −$49.20 | −$48.54 | −9.49 | $431 | 0.013 |

`b/e pot/w` is the pot this series would need before rewards cover the measured
adverse selection; `pot mult` is the actual pot divided by that. **The best market on
the board needs its pot to be 4x bigger. Most need 15–75x.**

Aggregated across the seven series at 200 shares: **≈$10,500/day of reward income
against ≈$151,000/day of adverse selection — rewards cover 6.9% of the hole.** The
20% maker rebate covers 13–16%. Rewards are worth *less* than the rebate, because the
rebate scales with fills while the reward is capped by a fixed pot.

### 3.1 Where the loss actually lives — the unpaired residual

Fills pair up most of the time: **70–88% of filled shares end up in complete sets**
(btc/15m 0.845, eth/15m 0.875, btc/5m 0.768, xrp/5m 0.731). Each pair earns exactly
the 1c lock. Back out the arithmetic on btc/5m: 76.8% of shares paying +0.5c/share
gives +0.38c/share, against a measured ≈−5.9c/share overall, so the unpaired 23.2%
must be losing ≈**27c/share**.

That is the whole story, and it is a binary-market story rather than a
market-making one. **An unpaired maker fill on a 5m updown is not a 3c markout — it
is a coin flip on 50c, held to resolution with no exit.** The complete-set pairing is
what makes passive quoting survivable at all; 77% pairing is not enough, because the
23% tail is worth 27c against a 1c lock.

**Design consequence: the inventory brake must target the unpaired residual
`|shares_up − shares_dn|`, not gross size.** Gross size is nearly harmless; the
imbalance is the entire risk. This is `maker-design.md` §4's `q_net` vs `q_paired`
split, and this study says `q_net` is not merely the right skew input — it is the only
quantity that matters.

### 3.2 The model gate does not rescue it

The brief's proposed edge: quote only while our own model says the coin is still fair,
pull once banked evidence forms. Measured, `|p_up − 0.5|` by window decile:

| decile | 5m median | 5m frac ≤0.2 | 15m median | 15m frac ≤0.2 |
|---|---|---|---|---|
| 0.0–0.1 | 0.305 | 0.24 | 0.282 | 0.26 |
| 0.2–0.3 | 0.437 | 0.07 | 0.401 | 0.07 |
| 0.4–0.5 | 0.496 | 0.03 | 0.489 | 0.02 |
| 0.5–1.0 | **0.500** | **0.00** | **0.500** | **0.00** |

The model is *decided* — `p_up` pinned at 0 or 1 — for the entire second half of every
window, and mostly decided from decile 2 onward. Inside frac 0.2–0.8 the gate
`|p_up−0.5| ≤ 0.2` passes on **0.6%** of samples with model coverage. The window in
which our model and the reward program both want us quoting is roughly **the first
minute**, which is exactly the early window `firsthalf_research.md` already condemned.

Running it anyway (fail-closed: no fresh model signal, no quote) collapses the duty
cycle from 0.71–0.80 to 0.10–0.16 — and moves the ratio almost not at all:

| sym/tf | pot mult, always-on | pot mult, gated |
|---|---|---|
| eth/5m | 0.047 | 0.026 |
| sol/5m | 0.031 | 0.034 |
| bnb/5m | 0.013 | 0.010 |
| xrp/5m | 0.013 | 0.005 |

**The gate is a size control, not an edge.** Reward accrues per unit of quoting time
and so does the loss, so gating scales both and leaves the ratio where it was. It
turns a large negative into a small negative by trading less — the same shape as every
non-losing row in `firsthalf_research.md` §3.

---

## 4. Q4 — net, swept, with CIs

Bootstrap 2000x, per-window resample, seed 7. `net/w = reward − adverse`, maker fee 0.

```
--- btc/5m   pot/window $34.72 (181 windows)
  size     gate    act.f   reward/w      adv/w      net/w   95% CI net/w      net/day
    50      off     0.71     $5.535    $-79.97    $-74.44 [-85.65, -62.87]    $-21438
    50      0.2     0.10     $0.655      $5.36      $6.02 [-21.74,  31.56]      $1733
   100      off     0.71     $8.566   $-130.03   $-121.46 [-139.79,-103.29]   $-34981
   200      off     0.71    $12.378   $-188.94   $-176.56 [-206.95,-145.52]   $-50850
   200      0.2     0.10     $1.698     $19.54     $21.24 [-65.97, 109.05]      $6117
   500      off     0.71    $17.112   $-253.87   $-236.76 [-289.18,-185.24]   $-68187
   500      0.2     0.10     $2.399     $-9.45     $-7.06 [-165.75, 166.07]     $-2032

--- btc/15m  pot/window $78.12 (27 windows)     <-- the least-bad market
    50      off     0.79    $30.378   $-103.37    $-72.99 [-124.03, -13.61]    $-7007
   100      off     0.79    $40.008   $-137.70    $-97.69 [-167.85, -21.23]    $-9378
   200      off     0.79    $47.987   $-197.76   $-149.78 [-263.74, -40.85]   $-14378
   500      off     0.79    $53.715   $-285.28   $-231.57 [-383.28, -89.83]   $-22230

--- eth/5m   pot/window $5.79 (181 windows)
    50      off     0.73     $3.283    $-71.85    $-68.56 [-81.30, -55.39]    $-19746
    50      0.2     0.16     $0.784    $-28.72    $-27.94 [-44.43, -12.30]     $-8046
   500      off     0.73     $3.788    $-80.54    $-76.75 [-92.48, -60.12]    $-22105

--- sol/5m   pot/window $5.79 (181 windows)
    50      off     0.73     $1.233    $-51.90    $-50.66 [-60.42, -41.15]    $-14591
    50      0.2     0.10     $0.161     $-7.77     $-7.61 [-31.53,  15.70]     $-2191

--- xrp/5m   pot/window $5.79 (77 windows)
    50      off     0.74     $0.606    $-47.84    $-47.23 [-56.85, -37.72]    $-13603
    50      0.2     0.14     $0.060    $-13.18    $-13.12 [-23.24,  -3.13]     $-3780

--- bnb/5m   pot/window $2.89 (123 windows)
    50      off     0.75     $0.374    $-33.25    $-32.88 [-40.84, -24.92]     $-9470
```

**Every always-on configuration is negative with a 95% CI excluding zero.** The only
rows whose CI straddles zero are the gated ones at ~10% duty cycle, on 11–25 windows,
where the interval is so wide it contains nothing useful — and btc/5m gated flips
negative again at 500 shares. This is the same pattern the prior study reported: the
configurations that do not lose money are the ones that barely trade.

Size does not help. Reward share saturates (btc/5m 0.28→0.74 from 50 to 500 shares,
a 2.6x gain) while adverse selection scales faster (−$79.97→−$253.87, 3.2x). Net gets
monotonically worse with size in every series.

**15m markets.** Included above from the observer books that started ~17:45Z — only
25–27 graded windows each, so the CIs are wide and these rows are a direction, not a
result. btc/15m is the best market on the board on every metric (pot mult 0.243,
−3.79c/share, deepest pot relative to its book) and is the only one worth re-testing
on a full day of data.

**4h markets.** Not simulated — we have 45 minutes of book, no window outcomes, and no
history. Pots are the largest per window ($277.78 btc, $55.56 mid-tier) and the live
sample shows median `Q_min` of 11.1 on btc/4h against 81.6 on btc/5m, i.e. a *thinner*
qualifying book than the 5m one despite an 8x larger pot. That is the single most
interesting unexplored cell in the grid and it is the one this study cannot price:
a 4h window is 48 five-minute windows of drift, and adverse selection over that
horizon is unmeasured. **Do not extrapolate the 5m loss rates to it in either
direction.**

---

## 5. Q5 — verdict and what would change it

**Do not build rewards-qualified quoting.** Rewards are real money — $33,333/day
across our markets — but at the sizes that qualify, they pay 6–7% of the adverse
selection they require you to eat. That is less than the maker rebate already offers
and roughly a fifth of what would be needed. `maker-design.md`'s decision to omit
reward income from the live edge math was correct, and now for a measured reason
rather than an absence of data.

The structural reason, stated once: **the reward pot is fixed and the adverse
selection is not.** Score share saturates in size while inventory risk scales with it,
so there is no size at which the pot catches up. Widening the quote to capture a
bigger complete-set lock destroys the score quadratically and hits zero at 3c. And the
one lever that would change the picture — quoting only when our model says the price
is fair — has a duty cycle of about one minute per five-minute window, because our
model is decided for the rest of it.

### What is worth doing

1. **Re-run `config` daily.** The program went live *today*, in stages. Rates,
   `rewardsMaxSpread` and `rewardsMinSize` are all server-side knobs that already
   moved once during this study. Read the CLOB, not gamma.
2. **Re-run `depth` in a week.** The competing-depth numbers here are pre-reward and
   are the load-bearing input. If rival depth has not risen by then, that is itself a
   finding worth understanding before dismissing the program.
3. **Price btc/15m properly on a full day.** It is the only cell where a large pot
   meets a thin book, its CI is the only one within reach of zero, and we now have
   observer books running.
4. **Leave `maker_bid` (maker-design.md step 0) exactly as it is.** It targets the
   one-sided late-window supply gap, not the reward program, and nothing here touches
   its economics.

### If it is ever revisited — the config the numbers point at

Not a recommendation to arm; a record of where the least-bad corner was.

- **Market**: btc/15m only. Nothing else clears 0.05 on pot multiple.
- **Size**: 50 shares a side — the reward minimum. Larger is monotonically worse.
- **Quote**: 1c wide, `b + d = 0.99`, joining the touch. Never wider: 2c costs 4x the
  score for 1c more lock, 3c scores zero. Never inside a wide spread without
  recomputing the mid — your own order moves the adjusted midpoint and can push you
  back out of the band.
- **Inventory brake on `|shares_up − shares_dn|`, not gross.** The paired stack is
  riskless and should not consume the brake. This is the one design change this study
  argues for on its own merits.
- **Never cross our own quotes.** The taker fleet arms the same slugs. A resting bid
  at the touch is exactly where `updown`'s taker path wants to lift, and the engine
  would pay itself the spread while booking two fills and a fee. A same-market
  conflict rule is a prerequisite, not a refinement: if an arm is live on a slug, the
  maker must not quote it.
- **Engine work required** (none of it built): a quoting loop on a 3–5s cadence with
  post-only cancel/replace (GTD's 2-minute floor rules it out), a per-market
  `rewardsMaxSpread`/`rewardsMinSize` poller since both move, an unpaired-residual
  brake, and reward telemetry reconciled against the daily 00:00 UTC on-chain payout —
  the payout is the only ground truth for whether any of the scoring model above is
  right.

---

## 6. Known weaknesses

- **One day, one regime, and a program that turned on mid-sample.** Every depth number
  predates or straddles the reward launch.
- **Rival elasticity is unmeasured.** The single largest unknown. Modelled as static —
  they move, and the direction they move is against us.
- **Rivals are aggregated into one competitor.** Conservative for share (understates
  it), but it means "how many *makers* compete" is unanswered.
- **Fill model, not print data.** The book tape has no prints, so fills are inferred
  from the opposing ask crossing our price. The two regimes bracket the prior study's
  print-driven result, which is reassuring, but they are not it.
- **Outcomes proxied by the last observed mid** (decisive windows only). The TWAP
  settlement rule is not re-derived here.
- **`b=1` assumed** (no in-game multiplier on crypto updown) and the size-cutoff
  adjustment to the midpoint is approximated from L1 — defensible at v=1.5c where
  93.3% of samples have ≤1 qualifying level, not at v=4.5c.
- **15m rows rest on 25–27 windows; 4h is not simulated at all.**
- **No reward payment has ever been observed.** `pmt rewards` has shown zero events.
  Every reward figure here is derived from the published formula and the live config,
  not from a credit that landed in the wallet. The first daily payout after any live
  test is the only thing that validates the model.
