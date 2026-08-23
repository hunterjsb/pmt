# Resolution farmer — feasibility study

**Question**: is systematically buying high-certainty (93–99c) Polymarket markets in their final
hours, across all non-crypto-updown categories, net-positive after selection hazards — and at
what filters and caps?

**Verdict: NO-GO.** Not "go with tight filters" — no-go.

The short version, in four numbers:

1. **99.7%** of resolved markets have **no ask at all** on the winning side when they close. The
   post-event window the strategy is named after has a resting bid at 0.999 and nothing to lift.
2. **~61%** of favourites showing ≥0.90 three hours before endDate are unbuyable for the same
   reason — their "price" is a synthetic mid over an empty ask ladder.
3. On sports (**44%** of the daily-resolving universe) `endDate` **is the kickoff time**, so
   "the final hours" are the hours *before the match*. There is nothing settled to farm.
4. On what is left, the best band's whole edge is **+4 bp per trade at zero execution cost** and is
   already negative at half a tick of spread. One loss erases **~430 winning trades**; establishing
   an edge that small would take tens of thousands of observations — years. It is below the noise
   floor of any feasible measurement.

Across a filter grid of **1,609 cells with n ≥ 200, not one has a positive 95% lower bound.** The
family that came closest went 100% over 240 trades in-sample and then **reversed sign out of
sample** (+1.41% → −2.11%) — and turned out not to be a resolution farm at all, but a 4-hour
short-gamma bet on BTC correlated with the very fleet it was meant to diversify (§8b).

Everything else — fees, technicality risk, UMA delay — is second order next to these.

Date: 2026-08-23. Corpus: `~/.pmt/resfarm/`. Scripts: `analysis/rf_*.py`.

---

## 0. What was measured

| | |
|---|---|
| Window | endDate 2026-07-19 .. 2026-08-21 (34 days) |
| Metadata corpus | **61,052** resolved markets, volume ≥ $5k, gamma `/markets/keyset` |
| Price histories | CLOB `/prices-history`, 5-min fidelity, `[endDate−8h, endDate+2h]` |
| Entry definition | at endDate−Δ (Δ ∈ 6/4/3/2/1h) buy the favourite if its price is in band |
| Outcome | gamma `outcomePrices` on the resolved market |
| Post-event book | frozen `bestBid`/`bestAsk` on 1,260 resolved markets |
| Live book snapshot | every non-updown market with endDate inside 8h, real ask ladders |

Three methodology points that make the numbers mean something:

1. **`prices-history` returns the book MIDPOINT, not the last trade.** Verified against live
   `bestBid`/`bestAsk` on 7 markets — exact match every time. So the historical price is a mid,
   the taker pays the ask, and the ask premium is a *measured* input rather than a guessed
   "1c worse than last price".
2. **Survivorship bias is negligible.** Of markets with endDate in the window and volume ≥ $10k,
   35,069 are resolved and **52 (0.15%)** are still open. The `closed=true` corpus is not hiding
   a tail of stuck disputes — there barely is one.
3. **One observation per market per Δ.** Base rates are computed inside a single Δ slice, so a
   market never counts five times and the binomial intervals are honest.

---

## 1. The fee premise is wrong — and it barely matters

The brief assumed non-crypto markets charge no taker fee. **That is false as of 2026.** Live
`feeSchedule` on every market in the corpus:

| feeType | rate | takerOnly | rebate |
|---|---|---|---|
| `politics_fees` | 0.04 | yes | 25% |
| `sports_fees_v2` → `v3` | 0.03 → **0.05** | yes | 25%/15% |
| `weather_fees` | 0.05 | yes | 25% |
| `culture_fees`, `economics_fees`, `general_fees` | 0.05 | yes | 25% |
| `tech_fees`, `finance_prices_fees`, `mentions_fees` | 0.04 | yes | 25% |
| `crypto_fees_v2` | 0.07 | yes | 20% |
| geopolitics | **no fee** (`feesEnabled: false`) | — | — |

Only **272 of 61,052** markets (0.4%) are fee-free. The formula
([docs.polymarket.com/trading/fees](https://docs.polymarket.com/trading/fees)) is

```
fee = C × feeRate × p × (1 − p)          # C = shares, p = price; takers only, charged at match time
```

*not* `min(p, 1−p)` — but at 93–99c the two nearly coincide, so the brief's strategic instinct was
right even though the formula was wrong. Breakeven becomes:

```
w* = p × (1 + feeRate × (1 − p))
```

At p=0.97 / rate 0.04 that is **97.12%** vs 97.00% — fees add 12 bp to breakeven. Real, but an
order of magnitude smaller than the half-spread, and two orders smaller than the effects in §2.
Fees are not what kills this.

*(The Taker Rebate Program does not help: its weighted volume term is `size × (1 − entry price)`,
so buying at 97c earns almost no rebate credit.)*

---

## 2. Four structural facts that decide it before any base rate

### 2a. On sports, `endDate` is the KICKOFF, not the finish

Market descriptions state the scheduled start in ET. Parsing them out of the corpus and
differencing against `endDate`:

| category | n | endDate − scheduled start = **exactly 0** | typical offset otherwise |
|---|---|---|---|
| soccer | 8,928 | **8,747 (98.0%)** | — |
| baseball | 2,913 | **2,729 (93.7%)** | +7d (series markets) |
| basketball | 536 | **536 (100%)** | — |
| football | 76 | **76 (100%)** | — |
| esports | 1,130 | 8 (0.7%) | **+4h to +6h** |

Sports is **44%** of the daily-resolving universe. For nearly all of it, "T-minus 1–6 hours before
endDate" is *1–6 hours before the match kicks off*. There is no settled outcome waiting to be
collected — you are buying a pre-game favourite at pre-game odds. The premise ("the outcome is
known, only the oracle is slow") does not apply to the largest slice of the universe.

Two exceptions, and they matter:

- **Weather (13%)**: endDate 12:00 UTC on a daily-temperature market lands late in the local day,
  so most of the day's maximum really has already happened. This is a genuine post-event window.
- **Esports**: endDate is deliberately padded to start + 4–6h, so T-3h really is mid- or
  post-match. Also a genuine post-event window.

Keep those two in mind — §2d returns to them, because they are precisely where the outcome really
is settled *and* precisely where the ask disappears.

### 2b. 99.7% of winning sides have **no ask at all** when the market closes

Frozen final book on 1,260 resolved markets across every category:

```
winning side NO ASK at close        1,256   (99.7%)
winning side buyable at close           4   ( 0.3%)
winning-side ask price:  1.000 × 1,256   |   0.999 × 2   |   0.99 × 1
winning-side bid price:  0.999 × 1,242   (98.6%)
```

Buyable share by category: soccer 0/491, tennis 0/175, esports 0/157, weather 0/124,
baseball 0/82, politics 2/69, crypto-other 0/45. It is not category-specific — it is universal.

The window the strategy is named after — *after the real world has decided, before UMA posts the
payout* — has **a resting bid at 0.999 and nothing on the offer**. Nobody sells a certain dollar
for 99.9 cents. The free money is already fully bid by resting makers, and a taker cannot reach it.

The obvious objection is that this is the book at the *instant* of close, and an ask might have
existed earlier and been lifted. Two live snapshots taken at arbitrary moments say otherwise:
of 40 live markets whose favourite showed a mid ≥ 0.90 with endDate inside the next 8–12h,
**13 (33%) had a literally empty ask ladder** mid-window; and 8 of 8 markets probed specifically
because gamma reported `bestAsk = 1.0` returned zero asks from the CLOB book endpoint. The
condition is not an artefact of the closing instant — it is the resting state of a decided market.

### 2c. 61% of "sure things" at T-3h cannot be bought at any price

With tick size *t*, an ask can never be quoted above `1−t`, so the richest mid that can have a real
offer behind it is `(1−2t + 1−t)/2 = 1 − 1.5t`. Anything above that is a synthetic ask of 1.000
standing in for an **empty ask ladder**.

This inference is load-bearing — it is made from historical mids with no recorded book — so it was
validated against live CLOB books in both directions (`analysis/rf_validate_noask.py`, 1,500 live
markets):

| prediction from the mid alone | live book has asks | live book EMPTY |
|---|---|---|
| unbuyable (`mid > 1 − 1.5t`) | 0 | **15** |
| buyable (`mid ≤ 1 − 1.5t`) | **25** | 0 |

40/40. It is not a heuristic — it is tick arithmetic.

```
Δ=6h: 5365 favourites >=0.90; 1708 (31.8%) have an EMPTY ask ladder (mid > 1-1.5*tick) and cannot be bought at any price
Δ=3h: 8904 favourites >=0.90; 5402 (60.7%) have an EMPTY ask ladder (mid > 1-1.5*tick) and cannot be bought at any price
Δ=1h: 7570 favourites >=0.90; 4632 (61.2%) have an EMPTY ask ladder (mid > 1-1.5*tick) and cannot be bought at any price
```

The bulk of the unbuyable population sits at a mid of **exactly 0.9995** — bid 0.999 against a
synthetic ask of 1.000. Any scanner reading a price feed (including the dormant `sure_bets`
strategy) sees a large population of 99c "opportunities" that are pure display artefact.

### 2d. The one-line version: the ask exists only where the outcome is still uncertain

Split the Δ=3h population by whether there was a real offer behind the mid:

```
population                              n  realized win%   mean mid     edge
mid>=0.90  BUYABLE (real ask)        3502         93.58%     0.9668   -3.11%
mid>=0.90  UNBUYABLE (no ask)        5402         99.94%     0.9995   -0.00%
mid>=0.99  BUYABLE (real ask)        1260         99.76%     0.9956   +0.20%
mid>=0.99  UNBUYABLE (no ask)        5402         99.94%     0.9995   -0.00%
```

The markets nobody will sell you are the ones that really are decided (~99.98% realized). The
markets you *can* buy are the ones still in doubt — and there the realized rate lands **below** the
mid you would have paid. That is not a filter problem; it is the selection mechanism of the
strategy itself. A resolution farmer is, by construction, buying the residue that the people
holding the certainty declined to sell.

The same fact seen across categories, using §2a's finding about what each category's Δ=3h window
actually contains:

```
    esports            205/3297   buyable ( 6.2%)
    weather           1132/2394   buyable (47.3%)
    soccer            1134/1150   buyable (98.6%)
    crypto-other       448/960    buyable (46.7%)
    politics           273/606    buyable (45.0%)
    culture             52/106    buyable (49.1%)
    geopolitics         73/96     buyable (76.0%)
    finance             60/84     buyable (71.4%)
    tech                33/72     buyable (45.8%)
    sports-other        32/54     buyable (59.3%)
```

Soccer at T-3h is pre-kickoff — nothing is settled, so almost everything is offered. Esports at
T-3h is mid- or post-match — the outcome is largely known, and 93% of it has no offer. The
availability of an ask is an almost perfect inverse indicator of whether the question has been
answered.

**Everything below is computed only on the entries that could actually be bought.**

---

## 3. Realized base rates

Of buyable favourites priced in each band at T-minus Δ, how often did the favoured side actually
win? Breakeven is the entry price itself (grossed up by the fee), *not* 1/price — a win returns
$1 on a cost of `p + fee`, so `w* = p × (1 + feeRate × (1−p))`.

```
### ALL CATEGORIES by price bucket (Δ=3h)
key                             n    win%        wilson95  mean px   ROI/tr      be    edge  medhold
----------------------------------------------------------------------------------------------------
0.995-0.999                   837  99.76% [ 99.1, 99.9]   0.9970   -0.09%  0.9970  +0.06%      6.5
0.985-0.995                   699  98.71% [ 97.6, 99.3]   0.9905   -0.58%  0.9905  -0.34%      7.6
0.900-0.930                   630  82.86% [ 79.7, 85.6]   0.9145   -9.88%  0.9145  -8.59%      7.1
0.950-0.970                   503  89.07% [ 86.0, 91.5]   0.9596   -7.56%  0.9596  -6.90%      7.1
0.970-0.985                   452  96.24% [ 94.1, 97.6]   0.9776   -1.85%  0.9776  -1.52%      7.4
0.930-0.950                   381  91.08% [ 87.8, 93.5]   0.9401   -3.60%  0.9401  -2.93%      7.0

### ALL CATEGORIES (Δ=3h) -- ROI per trade vs assumed half-spread
bucket              n    win%  mean mid  slip 0.0000  slip 0.0005  slip 0.0020  slip 0.0050  slip 0.0100
0.900-0.930       630  82.86%    0.9145       -9.69%       -9.73%       -9.88%      -10.16%      -10.62%
0.930-0.950       381  91.08%    0.9401       -3.40%       -3.45%       -3.60%       -3.89%       -4.37%
0.950-0.970       503  89.07%    0.9596       -7.37%       -7.42%       -7.56%       -7.83%       -8.28%
0.970-0.985       452  96.24%    0.9776       -1.66%       -1.71%       -1.85%       -2.14%       -2.61%
0.985-0.995       699  98.71%    0.9905       -0.39%       -0.44%       -0.58%       -0.86%       -1.11%
0.995-0.999       837  99.76%    0.9970        0.04%       -0.00%       -0.09%       -0.14%       -0.14%

### by category (Δ=3h, px>=0.93)
key                             n    win%        wilson95  mean px   ROI/tr      be    edge  medhold
----------------------------------------------------------------------------------------------------
weather                      1005  97.11% [ 95.9, 98.0]   0.9829   -1.45%  0.9829  -1.17%     13.4
soccer                        744  91.40% [ 89.2, 93.2]   0.9623   -5.38%  0.9623  -4.83%      7.0
crypto-other                  431  98.38% [ 96.7, 99.2]   0.9882   -0.68%  0.9882  -0.44%      3.2
politics                      244  95.49% [ 92.1, 97.5]   0.9839   -3.17%  0.9839  -2.90%      9.2
esports                       165 100.00% [ 97.7,100.0]   0.9793    1.82%  0.9793  +2.07%      2.3
geopolitics                    70  97.14% [ 90.2, 99.2]   0.9876   -1.82%  0.9876  -1.62%     73.6
finance                        56 100.00% [ 93.6,100.0]   0.9797    1.80%  0.9797  +2.03%      4.1
culture                        47 100.00% [ 92.4,100.0]   0.9857    1.21%  0.9857  +1.43%     24.4
tech                           31 100.00% [ 89.0,100.0]   0.9885    0.94%  0.9885  +1.15%     21.1
```

---

## 4. What actually kills the trades

### 4a. Forty-five failures, hand-classified: zero technicalities

The corpus contains **117 failures** at Δ=3h, mid ≥ 0.93, out of 2,872 buyable entries (4.1%).
The 30 largest by volume were pulled with their resolution rules, their full event comment thread,
and their trade tape (`analysis/rf_inspect.py`) and read individually. Classification:

| Failure mode | n | Examples |
|---|---|---|
| Genuine sports variance — pre-game odds that lost | 13 | O/U 0.5 that finished 0-0 (Qarabağ–CSKA, Botafogo–Vitória, Häcken–AIK, Örgryte–Djurgården, NE–Toronto); O/U 4.5/5.5 that went Over; FK Auda winning at 4.5%; Messi recording 0 shots |
| Weather — max temp landed in a neighbouring bucket | 13 | Seoul 26°C, London 22°C/27°C, Chengdu 40°C, Chicago 84-85°F, Wuhan 33°C, Ankara 26°C, Kuala Lumpur 32°C, Istanbul 34°C, SF 90-91°F |
| Macro surprise | 2 | South African Reserve Bank July meeting — market gave 95% to a 25bp hike, got a hold (both legs of the event failed) |
| Exact-score / spread props | 2 | Nacional 0-3 Tigre, Orlando City −2.5 |
| **Resolution technicality / wording** | **0** | — |
| **UMA dispute or 50/50 split** | **0** | zero SPLIT outcomes in the whole corpus |

The hazard the brief was built to fear — *a market resolving on wording rather than reality* — **is
not the failure mode here**, and the reason is structural: this universe is dominated by
mechanically-resolved sports and weather markets with a named objective source (uefa.com,
wunderground.com, a league's own stats page) and `automaticallyResolved: true`. Technicality risk
lives in bespoke, long-dated, human-judgement markets — which by construction are *not* the
markets that resolve every day and therefore are not the farmer's universe.

The losses are ordinary re-pricing on news, not surprises at settlement. Confirming this at
corpus scale:

```
DID THE MARKET DISCOVER IT FIRST? (last trade on the other side of 0.50 from entry)
  of 117 FAILURES: 117 (100.0%) had already flipped
  of 2755 WINNERS : 5 (0.2%) had flipped
  -> losses are ordinary re-pricing on news, not surprises at settlement
```

A random draw of 15 further failures from the full corpus (`rf_inspect.py --sample 15`) was read
to check the by-volume top-30 was representative. It is: soccer O/U 0.5 and 5.5, two spread props,
an exact-score prop, three weather buckets, an MMA fight, and one politics primary (Wisconsin
Secretary of State Republican nominee). Every one carried **0 comments** and a single
`umaResolutionStatuses: ["proposed"]` round — no dispute, no re-proposal. **45 failures read by
hand in total; zero technicalities, zero disputes.**

### 4b. `pmt scan`'s signals have almost no coverage here

| `pmt scan` signal | Coverage in this universe |
|---|---|
| Comment complaint ratio | **92.0% of qualifying markets have ZERO event comments; 97.4% of failures (114/117) have zero.** The signal is undefined on essentially the entire population. |
| Thinness (volume) | Real but weak — see the volume sweep in §5. Raising the floor to $250k improves ROI substantially and cuts supply ~90%, but never reaches positive. |
| Sibling-resolution precedent | Undefined. These are same-day markets created fresh each day; there are no already-closed siblings at entry time. |
| Smart-money side asymmetry | Not measurable at this cadence — `scan` costs ~20 wallet lookups per market and the pool is 77 markets/day. |

`pmt scan` was built for the near-miss it was built for: one bespoke, contested, comment-heavy
event. It is the right tool for that and **the wrong tool for this**, because the farmer's universe
has no comments, no siblings, and no technicalities to detect.

### 4c. Correlated failure is real and it lives inside negRisk events

The tail scenario the brief asked about does exist, in a specific shape: **negRisk bucket events
guarantee that exactly one leg resolves against the crowd.** Two clean examples in the corpus:

- **Chengdu, 25 July**: "highest temp = 40°C" was 0.98 YES and lost; "= 41°C" was 0.969 NO and
  lost. A farmer buying both favourites in one event loses both.
- **SARB July meeting**: "hike 25bps" at 0.95 lost and "hold" at 0.964 (as NO) lost — same event,
  same instant, both legs.

```
=== WORST DAYS: failures per calendar day (buyable, mid>=0.93) ===
  2026-08-19: 11 failures across 9 events (worst event contributed 2)
  2026-07-23: 10 failures across 8 events (worst event contributed 2)
  2026-08-11: 9 failures across 7 events (worst event contributed 2)
  2026-08-16: 6 failures across 6 events (worst event contributed 1)
  2026-07-21: 5 failures across 5 events (worst event contributed 1)
  2026-08-01: 5 failures across 4 events (worst event contributed 2)
```

The good news is that it is **bounded**: across 34 days no single event ever contributed more than
2 failures, and the worst day spread 11 failures over 9 distinct events. The feared scenario —
one news event resolving a dozen markets wrong-way at once — does not appear in this universe,
because the universe is mostly independent football matches and independent city temperatures.

The bad news is that the bound only holds if you enforce it. Event clustering in the raw
qualifying pool runs to 20 legs on a single event (World Cup halftime show), 18 (The Open
Championship), 17 (an Israel/Iran ceasefire ladder), and 13/10/10 across one World Cup fixture's
prop markets. **A per-event cap of 1 is mandatory**; without it a single World Cup match or one
geopolitical ladder is 10-20 correlated clips.

---

## 5. Economics

Simulated over the recorded corpus at **$75 per market** (the brief's $50–100 range), entering at
the ask (mid + half-spread), paying the live per-market taker fee, and holding to `closedTime`.
Capital employed is integrated over wall-clock time — every clip locks its cost basis from entry
until the market actually resolves — so slow UMA resolution shows up as a cost rather than being
annualised away.

```
=== price band sweep (Δ=3h, all non-updown cats, vol>=$10k) ===
px [0.930,1.001)                             n= 2872 wr= 95.93% pnl=$   -4,879 roi= -2.26% avgcap=$   3,295 $/day=$ -141.8 $/1k/day=$ -43.05 dd=$  -4,935 worst=$    -699 hold=7.3h
px [0.950,1.001)                             n= 2491 wr= 96.67% pnl=$   -3,851 roi= -2.06% avgcap=$   2,960 $/day=$ -112.0 $/1k/day=$ -37.84 dd=$  -3,941 worst=$    -606 hold=7.4h
px [0.970,1.001)                             n= 1988 wr= 98.59% pnl=$     -993 roi= -0.67% avgcap=$   2,421 $/day=$  -28.9 $/1k/day=$ -11.92 dd=$  -1,026 worst=$    -344 hold=7.4h
px [0.985,1.001)                             n= 1536 wr= 99.28% pnl=$     -365 roi= -0.32% avgcap=$   1,860 $/day=$  -10.6 $/1k/day=$  -5.71 dd=$    -461 worst=$    -209 hold=7.4h
px [0.995,1.001)                             n=  837 wr= 99.76% pnl=$      -58 roi= -0.09% avgcap=$     939 $/day=$   -1.7 $/1k/day=$  -1.80 dd=$     -75 worst=$     -75 hold=6.5h
px [0.999,1.001)                             (no trades)
px [0.930,0.970)                             n=  884 wr= 89.93% pnl=$   -3,886 roi= -5.86% avgcap=$     875 $/day=$ -113.0 $/1k/day=$-129.19 dd=$  -3,927 worst=$    -594 hold=7.0h
px [0.970,0.995)                             n= 1151 wr= 97.74% pnl=$     -934 roi= -1.08% avgcap=$   1,482 $/day=$  -27.2 $/1k/day=$ -18.34 dd=$  -1,028 worst=$    -348 hold=7.5h

=== per-category (Δ=3h, px>=0.93) ===
crypto-other                                 n=  431 wr= 98.38% pnl=$     -225 roi= -0.70% avgcap=$     137 $/day=$   -6.7 $/1k/day=$ -48.71 dd=$    -511 worst=$    -438 hold=3.2h
culture                                      n=   47 wr=100.00% pnl=$       43 roi=  1.23% avgcap=$     112 $/day=$    1.4 $/1k/day=$  12.80 dd=$       0 worst=$       0 hold=24.4h
esports                                      n=  165 wr=100.00% pnl=$      231 roi=  1.86% avgcap=$      68 $/day=$    6.9 $/1k/day=$ 102.63 dd=$       0 worst=$       0 hold=2.3h
finance                                      n=   56 wr=100.00% pnl=$       77 roi=  1.82% avgcap=$      45 $/day=$    2.5 $/1k/day=$  54.64 dd=$       0 worst=$       2 hold=4.1h
geopolitics                                  n=   70 wr= 97.14% pnl=$      -94 roi= -1.79% avgcap=$     407 $/day=$   -2.8 $/1k/day=$  -6.94 dd=$    -140 worst=$     -72 hold=73.6h
politics                                     n=  244 wr= 95.49% pnl=$     -587 roi= -3.21% avgcap=$     550 $/day=$  -17.9 $/1k/day=$ -32.64 dd=$    -624 worst=$    -214 hold=9.2h
soccer                                       n=  744 wr= 91.40% pnl=$   -3,026 roi= -5.42% avgcap=$     554 $/day=$  -88.9 $/1k/day=$-160.52 dd=$  -3,039 worst=$    -365 hold=7.0h
weather                                      n= 1005 wr= 97.11% pnl=$   -1,119 roi= -1.49% avgcap=$   1,329 $/day=$  -33.0 $/1k/day=$ -24.85 dd=$  -1,194 worst=$    -257 hold=13.4h

=== Δ sweep (px>=0.93) ===
Δ=6h                                         n= 2916 wr= 95.51% pnl=$   -5,451 roi= -2.49% avgcap=$   4,061 $/day=$ -158.0 $/1k/day=$ -38.89 dd=$  -5,451 worst=$    -483 hold=10.1h
Δ=4h                                         n= 3064 wr= 96.08% pnl=$   -4,700 roi= -2.05% avgcap=$   3,591 $/day=$ -136.5 $/1k/day=$ -38.00 dd=$  -4,700 worst=$    -618 hold=8.1h
Δ=3h                                         n= 2872 wr= 95.93% pnl=$   -4,879 roi= -2.26% avgcap=$   3,295 $/day=$ -141.8 $/1k/day=$ -43.05 dd=$  -4,935 worst=$    -699 hold=7.3h
Δ=2h                                         n= 2689 wr= 95.80% pnl=$   -4,819 roi= -2.39% avgcap=$   3,033 $/day=$ -140.3 $/1k/day=$ -46.26 dd=$  -4,869 worst=$    -780 hold=6.4h
Δ=1h                                         n= 2386 wr= 95.77% pnl=$   -4,218 roi= -2.36% avgcap=$   2,645 $/day=$ -123.0 $/1k/day=$ -46.49 dd=$  -4,346 worst=$    -415 hold=5.5h

=== volume floor sweep (Δ=3h, px>=0.93) ===
vol>=$10,000                                 n= 2872 wr= 95.93% pnl=$   -4,879 roi= -2.26% avgcap=$   3,295 $/day=$ -141.8 $/1k/day=$ -43.05 dd=$  -4,935 worst=$    -699 hold=7.3h
vol>=$25,000                                 n= 1145 wr= 95.55% pnl=$   -2,417 roi= -2.81% avgcap=$   1,410 $/day=$  -70.4 $/1k/day=$ -49.91 dd=$  -2,479 worst=$    -399 hold=6.3h
vol>=$50,000                                 n=  651 wr= 96.47% pnl=$     -978 roi= -2.00% avgcap=$     775 $/day=$  -28.7 $/1k/day=$ -37.03 dd=$  -1,042 worst=$    -343 hold=5.1h
vol>=$100,000                                n=  382 wr= 96.86% pnl=$     -507 roi= -1.77% avgcap=$     468 $/day=$  -14.9 $/1k/day=$ -31.77 dd=$    -593 worst=$    -287 hold=5.1h
vol>=$250,000                                n=  220 wr= 96.82% pnl=$     -314 roi= -1.90% avgcap=$     328 $/day=$   -9.2 $/1k/day=$ -28.14 dd=$    -426 worst=$    -219 hold=5.1h
```

**The number the brief asked for: −$43 per $1,000 of capital employed per day** at the base filter
(Δ=3h, mid ≥ 0.93, vol ≥ $10k, $75 clips, 0.002 half-spread). Over 34 days that is −$4,879 on an
average $3,295 employed, a −$4,935 max drawdown (the equity curve never recovers a peak — it just
descends), and a −$699 worst day. Nothing in the sweep turns it positive:

- **Price band**: monotonically less bad as you go up-price, from −$129/$1k/day at [0.93,0.97) to
  −$1.80 at [0.995,1.0). The best case is a slow bleed, not a profit.
- **Δ**: flat and negative across 6h/4h/3h/2h/1h (−$38.9 / −$38.0 / −$43.1 / −$46.3 / −$46.5 per
  $1k/day). There is no timing that helps.
- **Volume floor**: raising it to $250k improves ROI/trade from −2.26% to −1.90% and cuts n from
  2,872 to 220. It buys a 36 bp improvement for 92% of the supply, and stays negative.
- **Category**: only esports (+$103/$1k/day, n=165), finance (n=56) and culture (n=47) are
  positive, all on 100% win rates over small samples — and esports is 94% unbuyable (§2d), so its
  n=165 is the residue, not the opportunity.

**Execution capacity is not the constraint.** Two live book snapshots over every non-updown market
with endDate inside 8–12h: at $100 per clip, the top ask level alone absorbs the order in
essentially every case (median walk beyond best ask = 0.0000); at $500 it still fills in ~90%.
Liquid sports books (soccer O/U, spreads) show 1-tick spreads and $100k–$360k of resting ask.
The thin books are weather — spreads of 0.007–0.030 and as little as $10–$55 of total ask, which
is where a half-spread assumption of 0.002 becomes optimistic by 5×.

---

## 6. Operational shape

Supply, capital turnover, and the redemption lag — the numbers that would decide the build even if
the edge were real.

```
=== SUPPLY: qualifying markets per day (Δ=3h, non-updown) ===
  favourite mid >= 0.90 (any)                total=  8904   261.9/day  p10=220  med=252  p90=295
    of which BUYABLE (has an ask)            total=  3502   103.0/day  p10= 74  med=101  p90=124
    buyable and mid in [0.93, 0.9985]        total=  2872    84.5/day  p10= 63  med= 80  p90=104
    buyable, [0.93,0.9985], vol >= $25k      total=  1145    33.7/day  p10= 23  med= 32  p90= 43
    buyable, [0.93,0.9985], vol >= $100k     total=   382    11.2/day  p10=  5  med= 10  p90= 14

=== category mix of the buyable [0.93,0.9985] pool ===
  weather           1005  (35.0%)   29.6/day
  soccer             744  (25.9%)   21.9/day
  crypto-other       431  (15.0%)   12.7/day
  politics           244  ( 8.5%)    7.2/day
  esports            165  ( 5.7%)    4.9/day
  geopolitics         70  ( 2.4%)    2.1/day
  finance             56  ( 1.9%)    1.6/day
  culture             47  ( 1.6%)    1.4/day
  tech                31  ( 1.1%)    0.9/day
  sports-other        29  ( 1.0%)    0.9/day
  other               24  ( 0.8%)    0.7/day
  economics           13  ( 0.5%)    0.4/day
  football             5  ( 0.2%)    0.1/day
  basketball           3  ( 0.1%)    0.1/day
  baseball             2  ( 0.1%)    0.1/day
  mma                  2  ( 0.1%)    0.1/day
  mentions             1  ( 0.0%)    0.0/day

=== REDEMPTION LAG: closedTime - endDate, by category (hours) ===
  category              n     p10     med     p75     p90     p99      max
  esports            3297    -2.4    -1.4    -0.6     0.6     4.5    164.4
  weather            2394     3.3     4.6    11.7    17.2    62.8    281.0
  soccer             1150     2.2     4.0     4.2     5.7    24.6    227.8
  crypto-other        960     0.2     0.2     0.3     0.4     3.0     15.2
  politics            606     2.0     2.3    24.0    42.7   153.6    396.3
  culture             106    -2.1    21.4    27.2    27.3    38.3     71.3
  geopolitics          96     2.1    53.7    71.4    97.2   282.1    282.1
  finance              84    -1.9     0.4     2.0    22.2    92.5     92.5
  tech                 72     2.1    18.0    18.1    21.6    21.6     21.6
  sports-other         54     3.3    19.6    20.1    24.4    69.2     69.2
  other                36     1.9    13.1    15.3    50.9    51.0     51.0
  economics            25    10.5    10.6    10.6    10.7    21.9     21.9
  ALL                8904    -2.0     2.0     4.5    14.8    67.4    396.3

=== TOTAL HOLD: entry (endDate-3h) to redeemable, hours ===
  n=2872  p10=3.2  med=7.3  p75=13.7  p90=23.2  p99=87.2  max=399.3
  implied turnover at the median: 3.29 deployments/day per dollar
```

Three things to take from this:

- **Redemption lag is not the killer the brief feared, but it is not free either.** Median
  `closedTime − endDate` is ~2h and 90% resolve inside ~15h — no multi-day UMA purgatory, because
  this universe is `automaticallyResolved` sports/weather with named data sources, not
  human-adjudicated questions. But the tail is long: politics reaches ~10 days at p99. Combined
  with a 3h pre-entry, the *median* clip is tied up 7.3h and the p90 clip 23.2h, so realistic
  turnover is ~3.3 deployments/day per dollar at best — and the p99 is 87h, so a fraction of the
  book is always stuck.
- **Supply is adequate but not large.** ~85 buyable markets/day in [0.93, 0.9985], falling to
  ~34/day above $25k volume and ~11/day above $100k. At $75 a clip that is $6.3k/day of gross
  turnover at the loose filter and under $1k/day at the tight one.
- **Correlated failure exists but is bounded.** The worst single day produced 11 failures spread
  across 9 distinct events; across the whole corpus no event ever contributed more than 2
  failures. The real correlated exposure is the negRisk bucket structure (§4c), which
  *guarantees* one leg per event resolves against the crowd — a per-event cap of 1 is mandatory,
  not optional.

---

## 7. Does an exit rule rescue the payoff shape?

Since every failure re-prices before resolution, a stop-loss is the obvious repair. It makes
things **strictly worse** at every level tested — measured optimistically (exit at the recorded
mid minus 1c; real exit liquidity in a collapsing book is worse, and a soccer goal gaps through
the stop rather than walking to it):

```
  stop      n  stopped  stop-outs that   ROI/trade  ROI no-stop
                        would have won
  None   2872        0               0      -2.22%             
   0.9   2872      707             607      -4.78%             
  0.85   2872      589             490      -4.97%             
   0.8   2872      523             426      -5.13%             
   0.7   2872      429             334      -5.29%             
   0.5   2872      216             122      -3.49%             
   0.3   2872      102              18      -2.26%             
```

The reason is the third column: at a 0.90 stop, **607 of 707 stop-outs would have gone on to
win**. Favourites dip below 0.90 constantly and recover; the stop just pays the round-trip on
noise. Soccer is the worst case by far — a 0.85 stop turns −5.38% into −15.95%, because a
pre-kickoff favourite that concedes early is precisely the position that most often comes back.
Only a 0.30 stop is roughly neutral, and a 0.30 stop is not risk management, it is a slower way
to lose the same money.

---

## 7b. The measurement problem: the edge is below the noise floor

This is the argument that would still stand even if every structural finding above were wrong.

At an entry of *E* the breakeven **loss** rate is `1 − E − fee`. Buying at 99.75c, that is one
loss in 424. Measuring whether your true loss rate is 1-in-424 or 1-in-300 requires counting rare
events, and the count is Poisson.

```
band                n  losses  mean ask*  breakeven   realized   95% upper  wins per   n needed
                              (mid+½tick)  loss rate  loss rate   loss rate    1 loss     for 2σ
--------------------------------------------------------------------------------------------------------
[0.930,0.9500)    381      34     0.9406    0.05667    0.08924     0.11985        17      never
[0.950,0.9700)    503      55     0.9601    0.03795    0.10934     0.13883        25      never
[0.970,0.9850)    452      17     0.9781    0.02087    0.03761     0.05585        47      never
[0.985,0.9950)    699       9     0.9910    0.00852    0.01288     0.02443       116      never
[0.995,0.9986)    837       2     0.9975    0.00235    0.00239     0.00863       424      never
```

Read the last two columns. **Every band, including the richest, reads "never"** — the realized loss
rate is at or above its own breakeven, so the point estimate is on the wrong side and no amount of
extra data rescues it. The top band is the closest call and it still misses: breakeven needs one
loss in 424, and the corpus delivered one in 419.

Now read the 95% upper bound. In the top band it is 0.00863 — **3.7× the breakeven loss rate**. Even
if the point estimate had come out favourable, the data would be equally consistent with losing
nearly four times faster than the strategy can afford.

The practical consequence: **one failure erases 424 winning trades**, which at the observed supply
of ~25 qualifying markets/day in that band is seventeen days of profit. Distinguishing a real
+4 bp edge from zero at a 99.8% win rate needs observations in the tens of thousands — years of
live trading. Under the roadmap's own operating rule ("no change touches full-size capital until a
replay of recorded reality says it's better"), **this strategy can never be approved**, because
recorded reality cannot resolve an edge this small before the capital has been at risk for years.
§8b is what happens when you try anyway: 240 trades of perfect results, then the tail.

---

## 8. Filter search with a date holdout

A grid over Δ × price band × volume floor × category × late-activity, scored on the first half of
the corpus and re-scored on the second:

```
=== top 12 IN-SAMPLE by ROI/trade ===
   Δ          px band     vol>= cat             dl |     n    win%      ROI  ROI lo95
   4 [0.900,0.9986)    10,000 crypto-other     3 |   240 100.00%    1.41%    -0.19%
   4 [0.900,0.9986)    10,000 crypto-other     0 |   252 100.00%    1.35%    -0.17%
   3 [0.930,0.9986)    10,000 crypto-other     3 |   216 100.00%    1.03%    -0.74%
   4 [0.930,0.9986)    10,000 crypto-other     3 |   226 100.00%    1.01%    -0.68%
   3 [0.930,0.9986)    10,000 crypto-other     0 |   223 100.00%    1.00%    -0.71%
   4 [0.930,0.9986)    10,000 crypto-other     0 |   238 100.00%    0.97%    -0.64%
   3 [0.950,0.9986)    10,000 crypto-other     3 |   209 100.00%    0.89%    -0.93%
   3 [0.900,0.9986)    10,000 crypto-other     3 |   225  99.56%    0.87%    -1.19%
   3 [0.950,0.9986)    10,000 crypto-other     0 |   216 100.00%    0.87%    -0.90%
   6 [0.930,0.9986)    10,000 crypto-other     3 |   244  99.59%    0.86%    -1.04%
   4 [0.950,0.9986)    10,000 crypto-other     3 |   218 100.00%    0.85%    -0.89%
   3 [0.900,0.9986)    10,000 crypto-other     0 |   232  99.57%    0.85%    -1.15%

=== the same 12, re-scored OUT OF SAMPLE ===
   Δ          px band     vol>= cat            |     n    win%      ROI
   4 [0.900,0.9986)    10,000 crypto-other   |   229  96.51%   -2.11%
   4 [0.900,0.9986)    10,000 crypto-other   |   238  96.64%   -2.02%
   3 [0.930,0.9986)    10,000 crypto-other   |   202  96.53%   -2.56%
   4 [0.930,0.9986)    10,000 crypto-other   |   213  97.18%   -1.92%
   3 [0.930,0.9986)    10,000 crypto-other   |   208  96.63%   -2.49%
   4 [0.930,0.9986)    10,000 crypto-other   |   222  97.30%   -1.84%
   3 [0.950,0.9986)    10,000 crypto-other   |   193  97.41%   -1.88%
   3 [0.900,0.9986)    10,000 crypto-other   |   210  96.19%   -2.66%
   3 [0.950,0.9986)    10,000 crypto-other   |   199  97.49%   -1.82%
   6 [0.930,0.9986)    10,000 crypto-other   |   218  96.33%   -2.61%
   4 [0.950,0.9986)    10,000 crypto-other   |   208  97.12%   -2.09%
   3 [0.900,0.9986)    10,000 crypto-other   |   216  96.30%   -2.58%

in-sample positive-ROI filter sets: 30/902
in-sample sets whose 95% LOWER bound is positive: 0/902
```

**Nothing survives, and the way it fails is the most instructive result in the study.**

- In-sample: **902 cells with n ≥ 200; 30 have a positive ROI; ZERO have a positive 95% lower
  bound.**
- Whole corpus: **1,609 cells with n ≥ 200; 39 positive ROI; ZERO with a positive lower bound.**
- Every one of the top 12 in-sample cells is `crypto-other`, every one shows a **100% in-sample
  win rate**, and **every single one reverses sign out of sample** — +1.41% → −2.11%, +1.03% →
  −2.56%, and so on down the list. Not one degrades gracefully; they all flip.

That is what a manufactured edge looks like. A grid this wide produces positive point estimates by
construction; the lower bound and the holdout are what separate them from noise, and here nothing
passes either. §8b dissects the family that got closest, because *why* it flipped is worth more
than the fact that it did.

---

## 8b. The near-miss, dissected: how a 100% win rate became a loss

`crypto-other` at Δ=4h is the family that came closest, and watching it fail is the study's most
transferable lesson. On the first half of the corpus it went **100% over 240 trades, +1.41% per
trade**. On the second half it went **96.5%, −2.11%**. Over the full 34 days:

```
=== what it actually returned, once the sample was long enough ===
  losses: 8/490 (1.63%)  vs breakeven loss rate 0.0146 (69:1)
  REALIZED ROI per trade: -0.18%
  each loss costs 68 wins; 8 losses cost 542 winning trades out of 482 available
```

**What the markets are.** Not resolution plays at all — BTC/ETH *price-level* ladders: "Will the
price of Bitcoin be above $X on August N?", "Will Bitcoin dip to $X?", "Will Bitcoin reach $X?".
Median hold **4.2 hours**; **374 of 490** entries take the **NO** side. Stripped of the question
text the position is: *sell a 4-hour digital option on BTC struck far from spot*. It is a
short-gamma crypto trade wearing a prediction-market costume.

**The tell was visible before the losses arrived.** A win pays 1.48% on a 4.2h hold — 5.7
turns/day, **+8.4% per day on deployed capital**, 1.5 × 10^13 × per year compounded. No market
leaves that on the table. The counterparty is not confused; they are collecting a premium for a
tail. A 100% win rate over 240 observations is not evidence the tail is absent — it is exactly
what a short-vol book looks like right up until it isn't. **When a "sure thing" strategy's implied
annual return is absurd, the correct inference is that the sample is too short, not that the edge
is real.**

**And the tail is correlated by construction.** All legs of a ladder settle off a single price
print at a single instant: the busiest resolution hour in the corpus holds **15 legs**, and the
top five hours hold 14% of all positions. One 4-hour BTC move large enough to breach half the legs
in that hour costs **~$517 at $75/clip against a best-case daily profit of $15.96 — 32 days of
profit erased in one hour**. The roadmap's own standing truth predicted exactly this: *"Every
symbol throws a >3σ 1-minute jump about once an hour. Gaussian p_up ≥ 0.99 is fiction in exactly
the region we trade."*

**It also fails the premise it was proposed under.** The brief asked for a strategy *uncorrelated*
with the crypto fleet. This is short-dated BTC/ETH directional risk on a 4-hour horizon — the same
underlying, the same horizon class, and the same left tail as the fleet already running. It does
not diversify that book; it levers it.

If anyone wants to revisit this, it belongs in the crypto research track under R6/R7 (fat tails,
correlation-aware fleet cap), sized as additional crypto exposure — **not** as a "resolution
farmer", and not as diversification.

---

## 9. Verdict

**NO-GO.** Do not build it.

The strategy has four independent failure points, any one of which is fatal:

1. **The window doesn't exist where it's needed.** On 44% of the universe (sports) `endDate` is
   kickoff, so "final hours" means pre-game and there is nothing settled to farm.
2. **Where the window does exist, there's nothing to buy.** 99.7% of winning sides close with a
   0.999 bid and an empty offer. 61% of ≥0.90 favourites at T-3h are unbuyable for the same
   reason. The market has already priced and *bid* the certainty; the residual is claimed by
   resting makers, not available to takers.
3. **On what remains, realized base rates sit at or below their own breakeven in EVERY band.**
   At zero assumed slippage the best band returns +4 bp/trade and goes negative at half a tick of
   spread; its realized loss rate (1 in 419) is already worse than its breakeven (1 in 424). One
   loss erases 424 wins. Separating a +4 bp edge from zero at a 99.8% win rate needs tens of
   thousands of trades — it cannot clear the roadmap's evidence bar before years of capital risk.
4. **Nothing survives the filter grid.** 1,609 cells at n ≥ 200; zero with a positive 95% lower
   bound. The family that came closest reversed sign out of sample and turned out to be
   short-dated BTC/ETH digital options (§8b) — a 4-hour short-gamma trade whose tail is
   correlated by construction and which stacks the crypto fleet's exposure instead of
   diversifying it.

This is what an efficient market looks like from the taker side, and the result is unsurprising in
hindsight: the resolution-lag premium is worth roughly one tick, and one tick is exactly what the
resting bid at 0.999 has already taken.

It also fails the diversification premise it was proposed under, twice over. Where the returns are
≤ 0, "uncorrelated" is worthless — an uncorrelated zero adds no Sharpe, it just consumes capital
and attention the fleet's measured edge could use. And where the returns *looked* positive, they
were not uncorrelated at all: §8b is the same underlying, the same horizon, and the same left tail
as the book already running.

Note this **agrees with roadmap R3** ("test a max entry price (~0.70) for non-banked entries…
external research says high-price 'sure things' are poor risk/reward after fees"). Our own tape
now confirms it on a second, independent universe: high-price entries are poor risk/reward on
non-crypto Polymarket too, and for a sharper reason than fees.

### What is actually there, if anything

The one real observation worth keeping: **the last tick is captured by resting a bid, not by
taking an ask.** 1,242 of 1,260 resolved markets show a 0.999 bid on the winning side — someone is
already doing this, as a maker. That trade is:

- maker-side, so it earns the rebate instead of paying the fee;
- queue-competitive against existing size (the live snapshot showed $755 resting at 0.999);
- adversely selected by construction — you get filled when someone needs liquidity or is wrong,
  which is precisely when you don't want the fill.

That is Phase 3.1 (maker mode) applied to a new market class, and it should be evaluated *there*,
against the same fill-economics work — not as a separate strategy. It is not a taker strategy and
cannot be made into one.

### If it were a go, where would it live? (it isn't, but the answer generalises)

**Not the engine.** Three reasons, in order of weight:

1. **Latency is irrelevant by construction.** The entire premise is that nothing happens for
   hours. Decisions are made once per market; holds are 7–23h. The engine exists for sub-second
   book reactions on a handful of subscribed tokens.
2. **The shape is wrong.** This needs to scan ~800 markets/day across every category, hold
   position state for days, and drive redemption — none of which the engine's subscribe-and-react
   model does. It would need a second, unrelated subsystem bolted into a latency-critical process.
3. **The nightly poweroff makes the engine actively unsafe for multi-hour holds.** Per CLAUDE.md,
   graceful shutdown cancels resting orders but *already-filled inventory rides to resolution
   unmanaged*. For the updown fleet that is bounded at one window's arm size; for a farmer holding
   dozens of overnight positions with median 7h and p90 23h holds, every single night would strand
   the entire book.

A `pmtrader` scheduled command — scan → filter → place → redeem, run every 15 minutes from
cron/systemd, state on disk in `~/.pmt/` — is the right home, and would have been the right home
even if the numbers had come out positive. That conclusion generalises: **the engine is for
strategies whose edge decays in seconds; anything whose edge decays in hours belongs in
`pmtrader`.**

**The dormant `sure_bets` strategy should be deleted, not rebuilt.** Beyond being superseded by
this study, it has specific defects worth recording:

- It reads `book.best_ask()` and buys at `ask_price` if it is in `[0.95, 0.99]`. Per §2b/§2c, in
  the post-event window there *is* no ask, so it would never fire on the trades it was written
  for — and in the pre-event window it would fire on pre-game favourites at fair odds.
- Its `EXCLUDE_KEYWORDS` blocklist (~80 terms: every league, `" vs "`, all the O/U lines) removes
  sports entirely — 44% of the universe — leaving it to trade the thin residue.
- `MAX_HOURS_TO_EXPIRY = 48.0` with no lower bound means it would also buy things two days out,
  where §3's base rates are worst and the capital is locked longest.
- `MIN_LIQUIDITY = 500.0` is two orders of magnitude below the $25k–$100k volume floors that §5
  shows are needed before the numbers even approach breakeven.

Deleting it also removes a live foot-gun: it is a resident strategy that fires on any subscribed
token matching a price band, with no per-event cap (§4c) and no notion of the empty-ask artefact.

---

## 10. Reproduction

```bash
python3 analysis/rf_fetch_markets.py 2026-07-19 2026-08-21 5000   # metadata corpus  (~7 min)
python3 analysis/rf_fetch_prices.py 10000                          # price histories  (~50 min)
python3 analysis/rf_analyze.py                                     # base rates + hazards
python3 analysis/rf_sim.py                                         # portfolio economics
python3 analysis/rf_ops.py                                         # supply / hold / redemption lag
python3 analysis/rf_postevent.py 2500 10000                        # the no-ask finding
python3 analysis/rf_filter_search.py                               # grid + date holdout
python3 analysis/rf_stop.py                                        # exit-rule variant
python3 analysis/rf_probe_unresolved.py 2026-07-19 2026-08-15      # survivorship check
python3 analysis/rf_book_snapshot.py 8 10000                       # live ask ladders (forward-only)
python3 analysis/rf_noise_floor.py                                 # can the best pocket be measured
python3 analysis/rf_validate_noask.py 25                           # empty-ask heuristic vs live books
python3 analysis/rf_inspect.py                                     # failure dossiers for hand review
bash    analysis/rf_report.sh                                      # all offline stages -> one text dump
```

Cache is `~/.pmt/resfarm/` (durable — scratchpads are tmpfs and die on the nightly poweroff).
All fetchers are rate-limited by a shared token bucket and resume from cache.

### API notes worth keeping

- gamma `/markets` **hard-caps `offset` at 2100** — use `/markets/keyset` with `after_cursor`.
  `limit` is silently capped at 100 whatever you ask for.
- `include_tag=true` is what returns the real category tags; the embedded `events` object does not
  carry them.
- `/markets` defaults to `closed=false`, so fetching a resolved market by id returns nothing
  unless you also pass `closed=true`.
- `closedTime` is the resolution timestamp and is **not ISO** (`"2026-08-08 20:33:13+00"`).
- `prices-history` `startTs`/`endTs` span is capped at 15 days; `fidelity` is integer minutes.
- `prices-history` returns the **book midpoint**, not the last trade — do not treat it as a print.
- `bestAsk = 1.0` on a gamma market is a **placeholder for an empty ask ladder**, not a price.
  Any code that reads `bestAsk` as tradable must special-case it (`sure_bets` does not).
- `feeSchedule` / `feeType` / `feesEnabled` are per-market and authoritative — read them rather
  than hardcoding the docs table, which is ahead of production for sports.
