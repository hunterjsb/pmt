# freq_funnel — where the fleet's trades die

corpus window   : 05:00:00Z -> 08:41:36Z  (3.69h, theta era)
windows armed   : 175   cadence ticks: 12235   armed window-minutes: 1020
fires on tape   : 164 clips across 42 windows (44.4 clips/h)
outcomes joined : 137/175 windows
clip notional   : median observed fire $24.50 (used for windows that never fired)
rerun           : cd pmtrader && uv run python ../analysis/freq_funnel.py --out ../analysis/freq_funnel_report.md

------------------------------------------------------------------------------
THE ANSWER, IN FIVE LINES
------------------------------------------------------------------------------

1. The BASIS GUARD is the binding gate on every single series. It eats 60% of
   armed time (614 of 1020 window-minutes) — more than every other gate combined.

2. But most of what it blocks is not a trade. Of its refused moments, only
   the theta-survivable slice is real: L1 = 61 episodes, 80% hit, NET $284.37,
   bootstrap 95% CI [-$56.03, $655.27] — positive but not yet significant.

3. The second-biggest killer is NOT a gate at all. On 10% of armed time (98
   window-minutes) the side our model wants has NO ASK ON THE BOOK — bid ~0.99,
   nothing offered. No taker parameter reaches that time; a maker quote does.

4. min_fair is a non-event (1 tick of 12,235), and the edge bar is not the
   problem either. In SAFE mode the median ask on edge-refused moments is
   ~0.99 — the market already priced it — and trimming min_edge 0.015 ->
   0.008 tests NET-NEGATIVE with a CI that clears $0 the wrong way. Only
   the SPEC bar (early_min_edge 0.08) has anything behind it, and it is
   small.

5. Was there money in the quiet? Unbiased, one clip per zero-fire window at
   our own best moment: 103 windows, 54% hit, NET $301.82 — a coin flip.
   Filtered to theta-survivable moments: 65 windows, 86% hit, NET $114.92.
   The edge lives entirely in the theta-survivable slice the basis guard is
   sitting on. That is the one place to spend an A/B.

Two facts that frame all of the above:

  * The fire rate stepped down at the theta deploy, not gradually: share of
    armed windows that fired anything went 71% -> 53% -> 30% -> 22% across
    pre-brake / brake / theta / theta+payup.

  * Tonight is NOT a calm tape. Realized 1m sigma sits at the 62-81 percentile band
    of the 90-day baseline on every symbol. Low volatility is not the
    explanation for the low fire count.

Highest-confidence single move: eth 5m guard 8 -> 6bp — 8 episodes, 100% hit,
NET $53.00, bootstrap CI [$18.57, $94.20] (clears $0). LOOSENS a gate -> needs the replay A/B:
  pmengine replay --mode full --slug eth-updown-5m --params ab_guard6.json --outcomes ~/.pmt/corpus/outcomes.jsonl

The three brakes (distrust / avg_down / latch) together cost $385.01 tonight.
They are named never-loosen in the ROADMAP operating rules and were built
for a violent night, not this one — priced here to SIZE them, not to
propose removing them.

Sample size warning: this is ONE night, 3.7h, 175 windows, 137 of them
graded. Every row below is a direction to test on the replay harness,
not a result.

Gate order is the engine's own (updown.rs decide): feed -> basis guard ->
book -> theta -> brakes -> chop -> min_fair -> min_edge -> max_price ->
budget/cooldown. Every refused moment is charged to the FIRST gate that
stopped it, so no gate is billed for work a gate upstream already did.

==============================================================================
1. THE FUNNEL
==============================================================================

--- POOLED (all series) ---
stage                                          ticks  win-min  %survive   %armed
armed (window-minutes on tape)                 12235   1019.6    100.0%   100.0%
live model (feed not stale)                    12028   1002.3     98.3%    98.3%
basis guard cleared                             4662    388.5     38.8%    38.1%
model side quoted (has an ask)                  3488    290.7     74.8%    28.5%
theta / safety gate cleared                     1852    154.3     53.1%    15.1%
no distrust / avg_down / latched brake           919     76.6     49.6%     7.5%
rho chop filter (spec mode only)                 919     76.6    100.0%     7.5%
fair >= min_fair                                 918     76.5     99.9%     7.5%
net >= min_edge                                  330     27.5     35.9%     2.7%
ask <= max_price                                 330     27.5    100.0%     2.7%
FIRED (budget/cooldown/inflight allowed it)      147     12.2     44.5%     1.2%
BINDING GATE: basis_guard  (7366 ticks = 60.2% of armed time, 614 window-minutes)

--- bnb 5m ---
stage                                          ticks  win-min  %survive   %armed
armed (window-minutes on tape)                   721     60.1    100.0%   100.0%
live model (feed not stale)                      706     58.8     97.9%    97.9%
basis guard cleared                              155     12.9     22.0%    21.5%
model side quoted (has an ask)                    66      5.5     42.6%     9.2%
theta / safety gate cleared                       30      2.5     45.5%     4.2%
no distrust / avg_down / latched brake            24      2.0     80.0%     3.3%
rho chop filter (spec mode only)                  24      2.0    100.0%     3.3%
fair >= min_fair                                  24      2.0    100.0%     3.3%
net >= min_edge                                   13      1.1     54.2%     1.8%
ask <= max_price                                  13      1.1    100.0%     1.8%
FIRED (budget/cooldown/inflight allowed it)        7      0.6     53.8%     1.0%
BINDING GATE: basis_guard  (551 ticks = 76.4% of armed time, 46 window-minutes)

--- btc 15m ---
stage                                          ticks  win-min  %survive   %armed
armed (window-minutes on tape)                  2152    179.3    100.0%   100.0%
live model (feed not stale)                     2132    177.7     99.1%    99.1%
basis guard cleared                             1047     87.2     49.1%    48.7%
model side quoted (has an ask)                   927     77.2     88.5%    43.1%
theta / safety gate cleared                      552     46.0     59.5%    25.7%
no distrust / avg_down / latched brake           243     20.2     44.0%    11.3%
rho chop filter (spec mode only)                 243     20.2    100.0%    11.3%
fair >= min_fair                                 243     20.2    100.0%    11.3%
net >= min_edge                                  133     11.1     54.7%     6.2%
ask <= max_price                                 133     11.1    100.0%     6.2%
FIRED (budget/cooldown/inflight allowed it)       58      4.8     43.6%     2.7%
BINDING GATE: basis_guard  (1085 ticks = 50.4% of armed time, 90 window-minutes)

--- btc 5m ---
stage                                          ticks  win-min  %survive   %armed
armed (window-minutes on tape)                  2410    200.8    100.0%   100.0%
live model (feed not stale)                     2359    196.6     97.9%    97.9%
basis guard cleared                              740     61.7     31.4%    30.7%
model side quoted (has an ask)                   465     38.8     62.8%    19.3%
theta / safety gate cleared                      203     16.9     43.7%     8.4%
no distrust / avg_down / latched brake           116      9.7     57.1%     4.8%
rho chop filter (spec mode only)                 116      9.7    100.0%     4.8%
fair >= min_fair                                 115      9.6     99.1%     4.8%
net >= min_edge                                   28      2.3     24.3%     1.2%
ask <= max_price                                  28      2.3    100.0%     1.2%
FIRED (budget/cooldown/inflight allowed it)       14      1.2     50.0%     0.6%
BINDING GATE: basis_guard  (1619 ticks = 67.2% of armed time, 135 window-minutes)

--- eth 15m ---
stage                                          ticks  win-min  %survive   %armed
armed (window-minutes on tape)                  2145    178.8    100.0%   100.0%
live model (feed not stale)                     2125    177.1     99.1%    99.1%
basis guard cleared                             1047     87.2     49.3%    48.8%
model side quoted (has an ask)                   856     71.3     81.8%    39.9%
theta / safety gate cleared                      494     41.2     57.7%    23.0%
no distrust / avg_down / latched brake           183     15.2     37.0%     8.5%
rho chop filter (spec mode only)                 183     15.2    100.0%     8.5%
fair >= min_fair                                 183     15.2    100.0%     8.5%
net >= min_edge                                   72      6.0     39.3%     3.4%
ask <= max_price                                  72      6.0    100.0%     3.4%
FIRED (budget/cooldown/inflight allowed it)       26      2.2     36.1%     1.2%
BINDING GATE: basis_guard  (1078 ticks = 50.3% of armed time, 90 window-minutes)

--- eth 5m ---
stage                                          ticks  win-min  %survive   %armed
armed (window-minutes on tape)                  2408    200.7    100.0%   100.0%
live model (feed not stale)                     2357    196.4     97.9%    97.9%
basis guard cleared                              753     62.8     31.9%    31.3%
model side quoted (has an ask)                   509     42.4     67.6%    21.1%
theta / safety gate cleared                      264     22.0     51.9%    11.0%
no distrust / avg_down / latched brake           156     13.0     59.1%     6.5%
rho chop filter (spec mode only)                 156     13.0    100.0%     6.5%
fair >= min_fair                                 156     13.0    100.0%     6.5%
net >= min_edge                                   55      4.6     35.3%     2.3%
ask <= max_price                                  55      4.6    100.0%     2.3%
FIRED (budget/cooldown/inflight allowed it)       28      2.3     50.9%     1.2%
BINDING GATE: basis_guard  (1604 ticks = 66.6% of armed time, 134 window-minutes)

--- sol 5m ---
stage                                          ticks  win-min  %survive   %armed
armed (window-minutes on tape)                  2399    199.9    100.0%   100.0%
live model (feed not stale)                     2349    195.8     97.9%    97.9%
basis guard cleared                              920     76.7     39.2%    38.3%
model side quoted (has an ask)                   665     55.4     72.3%    27.7%
theta / safety gate cleared                      309     25.8     46.5%    12.9%
no distrust / avg_down / latched brake           197     16.4     63.8%     8.2%
rho chop filter (spec mode only)                 197     16.4    100.0%     8.2%
fair >= min_fair                                 197     16.4    100.0%     8.2%
net >= min_edge                                   29      2.4     14.7%     1.2%
ask <= max_price                                  29      2.4    100.0%     1.2%
FIRED (budget/cooldown/inflight allowed it)       14      1.2     48.3%     0.6%
BINDING GATE: basis_guard  (1429 ticks = 59.6% of armed time, 119 window-minutes)

Where the armed time goes, pooled (share of all cadence ticks):
  basis_guard        7366   60.2%      614 window-min
  theta              1636   13.4%      136 window-min
  book_quoted        1174    9.6%       98 window-min
  brakes              933    7.6%       78 window-min
  min_edge            588    4.8%       49 window-min
  live_model          207    1.7%       17 window-min
  last_mile           183    1.5%       15 window-min
  fired               147    1.2%       12 window-min
  min_fair              1    0.0%        0 window-min

==============================================================================
2. BINDING-GATE ECONOMICS (hindsight-priced, one clip per episode)
==============================================================================

Each row = the moments blocked ONLY by that gate (everything upstream
passed), collapsed into episodes on shadow.py's 20s-gap rule, priced as
one clip at the episode's best (lowest) recorded ask. WIN pays
shares*(1-ask-fee); LOSS forfeits the clip. NET = missed wins MINUS
avoided losses: NET>0 means the gate cost us money tonight.

gate            eps priced    hit         95% CI  med ask     missed    avoided        NET
------------------------------------------------------------------------------------------
basis_guard     535    425  58.1%       [53-63%]    0.520  $5,529.08  $4,540.69    $988.39
book_quoted     103      0      -              -        -      $0.00      $0.00      $0.00
theta           173    149  73.2%       [66-80%]    0.740  $1,083.07    $960.48    $122.59
brakes           53     47  85.1%       [72-93%]    0.870    $356.53    $136.78    $219.75
min_fair          1      1 100.0%      [21-100%]    0.960      $1.92      $0.00      $1.92
min_edge         80     67  89.6%       [80-95%]    0.990     $59.88    $136.94    -$77.06
last_mile        38     31  83.9%       [67-93%]    0.950     $55.16     $88.23    -$33.07

--- basis_guard: by series ---
series        eps priced    hit     missed    avoided        NET
bnb 5m         36     16    62%    $221.63    $146.97     $74.66
btc 15m        73     62    47%    $601.63    $764.26   -$162.63
btc 5m        130    106    60%  $1,248.71  $1,078.74    $169.97
eth 15m        65     61    52%  $1,107.39    $922.06    $185.33
eth 5m        115     93    63%  $1,189.56    $806.27    $383.28
sol 5m        116     87    61%  $1,160.17    $822.38    $337.78

--- brakes: by series ---
series        eps priced    hit     missed    avoided        NET
bnb 5m          1      0      -      $0.00      $0.00      $0.00
btc 15m         8      7    86%     $68.00     $13.60     $54.40
btc 5m          9      8    75%     $46.47     $48.99     -$2.52
eth 15m         9      8   100%    $154.47      $0.00    $154.47
eth 5m         12     11    82%     $47.37     $25.20     $22.17
sol 5m         14     13    85%     $40.22     $48.99     -$8.77

--- theta: by series ---
series        eps priced    hit     missed    avoided        NET
bnb 5m          7      2   100%      $9.72      $0.00      $9.72
btc 15m        26     22    59%    $125.90    $187.77    -$61.87
btc 5m         37     31    81%    $222.70    $171.94     $50.76
eth 15m        24     23    65%    $318.80    $242.61     $76.19
eth 5m         33     29    76%    $165.65    $113.77     $51.89
sol 5m         46     42    76%    $240.30    $244.39     -$4.09

--- brakes: which one ---
brake         ticks  eps priced    hit     missed    avoided        NET
latched         700   55     49    90%    $180.98     $87.79     $93.19
distrust        226   22     20    90%    $318.21     $48.99    $269.22
avg_down          7    5      5   100%     $22.60      $0.00     $22.60

--- the min_ask floor: is it the edge FLOOR or the PRICE? ---
Two different bars live behind one funnel stage: an unlocked (safe-mode)
side needs min_edge 0.015, a locked (spec-mode) side needs
early_min_edge 0.08. They are different knobs with different
answers, so they are split here.

  SAFE (unlocked): 259 ticks, bar = 0.015
    ask  p10 0.990  p25 0.990  median 0.994  p75 0.999  p90 0.999
    net  p10 +0.0009  p25 +0.0009  median +0.0056  p75 +0.0093  p90 +0.0093
    net >= 0.012 :     1 ticks (0% of them would be released)
    net >= 0.01  :     1 ticks (0% of them would be released)
    net >= 0.008 :   106 ticks (41% of them would be released)
    net >= 0.005 :   130 ticks (50% of them would be released)

  SPEC (locked): 329 ticks, bar = 0.08
    ask  p10 0.920  p25 0.950  median 0.960  p75 0.980  p90 0.990
    net  p10 +0.0093  p25 +0.0186  median +0.0279  p75 +0.0440  p90 +0.0590
    net >= 0.06  :    31 ticks (9% of them would be released)
    net >= 0.04  :    99 ticks (30% of them would be released)
    net >= 0.03  :   153 ticks (47% of them would be released)
    net >= 0.02  :   209 ticks (64% of them would be released)

  Effective max ask a safe-mode clip can pay: 0.951 at fair = min_fair 0.97, 0.984 at fair = 1.00.
  Edge-refused ticks already asking above 0.951: 471 of 588 (80%) — that is the
  min_ask floor biting: not our edge bar, the market's price.

--- no offer at any price: the book_quoted stage ---
eval ticks whose model side had NO ask on the book: 1174 (9.6% of armed time, 98 window-minutes)
  window elapsed-frac  p10 0.61  median 0.82  p90 0.93   (late-window, exactly when the model is finally confident)
  our side's BID at those moments: p25 0.990  median 0.990  p75 0.999
  Reading: the side we want is bid up near 1.00 and NOBODY IS OFFERING. No gate setting reaches this time — it is supply.

--- basis guard: counterfactual layers ---
A basis-gated tick never got a model, so L0 (the naive shadow-ledger
number) prices moments that the NEXT gates would have refused anyway.
The gate reason carries banked/cushion, which is exactly theta's input,
so we can carry the counterfactual one step further:
  L0  every basis-gated moment
  L1  + the theta gate would also have cleared (safety >= 0.3)
  L2  + the recorded ask is low enough to clear min_edge at min_fair
      (ask <= 0.951)

layer         eps priced    hit         95% CI     missed    avoided        NET          boot 95% CI on NET
L0 all        535    425  58.1%       [53-63%]  $5,529.08  $4,540.69    $988.39       [-$146.90, $2,195.07]
L1 +theta      91     61  80.3%       [69-88%]    $578.31    $293.94    $284.37          [-$56.03, $655.27]
L2 +edge       68     53  75.5%       [62-85%]    $575.95    $318.44    $257.51          [-$83.00, $632.29]

--- basis guard L1 (theta-survivable) by series: what a guard trim buys ---
series       guard  eps priced    hit     missed    avoided        NET
bnb 5m           8   10      3   100%      $7.52      $0.00      $7.52
btc 15m          6    5      3    67%     $16.78     $24.50     -$7.72
btc 5m           6   25     16    81%     $71.47     $73.48     -$2.01
eth 15m          8    2      2    50%     $81.60     $24.50     $57.11
eth 5m           8   21     14    86%    $193.12     $48.99    $144.13
sol 5m          10   28     23    78%    $207.81    $122.48     $85.34

--- guard trim sweep: episodes that a LOWER guard would have released ---
(basis-gated moments whose |margin| already exceeded the trial guard,
 theta-survivable, priced the same way. This is the A/B candidate set.)

series       guard  trial  eps priced    hit     missed    avoided        NET
bnb 5m           8      7    8      2   100%      $1.90      $0.00      $1.90
bnb 5m           8      6   10      4   100%      $5.36      $0.00      $5.36
bnb 5m           8      5   10      3   100%      $7.52      $0.00      $7.52
btc 15m          6      5    3      2     0%      $0.00     $48.99    -$48.99
btc 15m          6      4    3      2     0%      $0.00     $48.99    -$48.99
btc 15m          6      3    6      4    50%     $16.78     $48.99    -$32.21
btc 5m           6      5   18      9    89%     $23.20     $24.50     -$1.30
btc 5m           6      4   21     14    93%     $38.98     $24.50     $14.48
btc 5m           6      3   23     14    86%     $66.06     $48.99     $17.07
eth 15m          8      7    3      3    67%     $80.81     $24.50     $56.32
eth 15m          8      6    4      4    50%    $129.06     $48.99     $80.07
eth 15m          8      5    3      3    33%     $81.60     $48.99     $32.61
eth 5m           8      7   11      4   100%     $14.87      $0.00     $14.87
eth 5m           8      6   14      8   100%     $53.00      $0.00     $53.00
eth 5m           8      5   17     11    91%    $158.12     $24.50    $133.62
sol 5m          10      9   15     12    75%     $41.24     $73.48    -$32.25
sol 5m          10      8   20     17    76%     $76.37     $97.98    -$21.61
sol 5m          10      7   19     16    81%     $90.11     $73.48     $16.63

--- theta trim sweep: eval moments the safety gate refused ---
(each row = the moments a LOWER theta would have released, priced the
 same way. NET>0 = the trim would have made money on tonight's tape.)
trial theta     eps priced    hit         95% CI     missed    avoided        NET          boot 95% CI on NET
0.25             47     36    81%       [65-90%]    $112.83    $159.29    -$46.46          [-$189.19, $75.46]
0.2              68     53    85%       [73-92%]    $265.46    $172.89     $92.57          [-$73.44, $245.24]
0.15             91     71    86%       [76-92%]    $364.18    $221.60    $142.58          [-$40.96, $318.20]
0.1             114     93    84%       [75-90%]    $533.45    $332.18    $201.27          [-$32.32, $425.01]
0.0             171    147    73%       [66-80%]  $1,065.56    $952.94    $112.62         [-$322.06, $516.31]

--- edge-bar trim sweeps (the two bars, separately) ---
bar / trial                     eps priced    hit         95% CI     missed    avoided        NET          boot 95% CI on NET
min_edge (safe) -> 0.012          1      1   100%      [21-100%]      $0.42      $0.00      $0.42                           -
min_edge (safe) -> 0.01           1      1   100%      [21-100%]      $0.42      $0.00      $0.42                           -
min_edge (safe) -> 0.008         34     31    84%       [67-93%]      $7.18     $99.84    -$92.66         [-$182.59, -$16.08]
min_edge (safe) -> 0.005         38     35    86%       [71-94%]      $9.84     $99.84    -$90.01         [-$180.52, -$13.70]
early_min_edge (spec) -> 0.06    17     12    92%       [65-99%]     $20.66     $24.21     -$3.56           [-$59.34, $29.17]
early_min_edge (spec) -> 0.04    26     19    95%       [75-99%]     $48.18     $24.21     $23.96           [-$36.72, $66.56]
early_min_edge (spec) -> 0.03    32     24    96%       [80-99%]     $50.83     $24.21     $26.61           [-$37.66, $70.26]
early_min_edge (spec) -> 0.02    35     26    96%       [81-99%]     $49.14     $24.21     $24.93           [-$36.72, $68.26]

--- spec mode: does it still exist post-theta? ---
cadence ticks in spec mode (locked budget) : 8342 (68.2% of armed)
  ...of which reached the side gates       : 2916
cadence ticks in safe mode (unlocked)      : 3686
  spec-mode side-gate deaths: {'theta': 1626, 'brakes': 805, 'min_edge': 329, 'book_quoted': 137, 'fired': 12, 'last_mile': 7}
fires by mode: spec=20  safe=143  flip=1
  last spec fire at 08:07:59Z
spec-mode deaths at EARLY_MIN_FAIR 0.55: 0   at early_min_edge 0.08: 329

VERDICT: spec mode is NOT dead post-theta — 20 of 164 clips tonight (12%)
fired in spec mode, the last at 08:07:59Z. What killed spec
moments is theta (1626 ticks) and the brakes (805), both UPSTREAM of the
spec bars — EARLY_MIN_FAIR 0.55 refused 0 moments all night. Re-tuning the
spec bars is re-tuning a gate that is barely reached.

==============================================================================
3. THE QUIET-MARKET QUESTION
==============================================================================

For every window that never fired, three progressively honest measures of
what was actually on the table, all on the eventual winner's side and all
capped by the size really resting at that ask (uncapped, a $24 clip at ask
0.01 'wins' $2,400 on depth that was never there):

  A  ANY  — cheapest offer at any point in the window. Pure hindsight: a
           binary opens near 0.50, so this is almost always < 0.90 and
           mostly measures reversals nobody could have known. Ceiling only.
  B  LATE — cheapest offer in the final 120s (the unlocked / safe-bet
           window where min_fair 0.97 applies). This is the safe-bet
           opportunity the fleet is actually built to take.
  C  KNEW — cheapest offer at a moment when OUR OWN read already pointed
           at the winner (eval p_up side, or a gated tick's margin sign).
           This is the only one that is a verdict on our gates: the market
           was selling the winner cheap AND we already knew which side.

'No edge existed' = that measure never got below 0.90.

--- ZERO-FIRE windows (n=133) ---
  windows with no outcome or no book: 30 (23%)

  measure     n  no edge  edge, gated   % of n  $ ceiling (depth-capped)
  A ANY     103        0          103     100%                 $8,239.45
  B LATE     97       31           66      68%                 $6,252.70
  C KNEW    101        0          101     100%                 $1,668.01

  A ANY winner min-ask: p10 0.090  p25 0.260  median 0.350  p75 0.480  p90 0.610
  B LATE winner min-ask: p10 0.150  p25 0.470  median 0.780  p75 0.950  p90 0.980
  C KNEW winner min-ask: p10 0.390  p25 0.460  median 0.520  p75 0.600  p90 0.670

  deepest C-KNEW misses (we already pointed at the winner and the
  book was offering it this cheap):
    sol-updown-5m-1787461200         ask 0.090 x47 sh @ 86% elapsed -> $42.46
    eth-updown-5m-1787463900         ask 0.150 x5 sh @ 85% elapsed -> $4.20
    eth-updown-5m-1787466300         ask 0.190 x10 sh @ 90% elapsed -> $7.97
    btc-updown-15m-1787463900        ask 0.340 x20 sh @ 68% elapsed -> $12.60
    btc-updown-5m-1787471100         ask 0.360 x360 sh @ 66% elapsed -> $41.83
    sol-updown-5m-1787463000         ask 0.370 x50 sh @ 48% elapsed -> $30.20
    sol-updown-5m-1787463300         ask 0.370 x50 sh @ 16% elapsed -> $30.20
    btc-updown-5m-1787468100         ask 0.380 x94 sh @  3% elapsed -> $38.25
    sol-updown-5m-1787467800         ask 0.380 x100 sh @ 20% elapsed -> $38.25
    sol-updown-5m-1787469000         ask 0.380 x50 sh @  6% elapsed -> $29.67

--- basis guard NEVER cleared (n=50) ---
  windows with no outcome or no book: 16 (32%)

  measure     n  no edge  edge, gated   % of n  $ ceiling (depth-capped)
  A ANY      34        0           34     100%                 $3,396.81
  B LATE     34        7           27      79%                 $2,731.98
  C KNEW     34        0           34     100%                   $618.33

  A ANY winner min-ask: p10 0.090  p25 0.290  median 0.340  p75 0.440  p90 0.510
  B LATE winner min-ask: p10 0.200  p25 0.440  median 0.660  p75 0.840  p90 0.950
  C KNEW winner min-ask: p10 0.420  p25 0.460  median 0.510  p75 0.600  p90 0.620

  deepest C-KNEW misses (we already pointed at the winner and the
  book was offering it this cheap):
    eth-updown-5m-1787463900         ask 0.150 x5 sh @ 85% elapsed -> $4.20
    btc-updown-5m-1787471100         ask 0.360 x360 sh @ 66% elapsed -> $41.83
    eth-updown-15m-1787463900        ask 0.410 x100 sh @ 68% elapsed -> $33.53
    btc-updown-5m-1787467800         ask 0.420 x462 sh @ 47% elapsed -> $32.11
    bnb-updown-5m-1787470800         ask 0.450 x50 sh @ 19% elapsed -> $25.93
    eth-updown-5m-1787464500         ask 0.450 x30 sh @ 27% elapsed -> $15.56
    sol-updown-5m-1787469900         ask 0.450 x110 sh @ 45% elapsed -> $28.22
    bnb-updown-5m-1787472000         ask 0.460 x50 sh @ 32% elapsed -> $25.39
    sol-updown-5m-1787467200         ask 0.460 x50 sh @ 78% elapsed -> $25.39
    bnb-updown-5m-1787470500         ask 0.470 x50 sh @ 24% elapsed -> $24.86

--- D UNBIASED: one clip per zero-fire window, our side, real outcome ---
A/B/C above are conditioned on the eventual winner, so they only ever
show upside — they are ceilings, not verdicts. D takes the cheapest
moment on whatever side OUR read favoured at the time and grades it on
the real outcome, losers included. This is the honest 'was there money
in the quiet' number.

  variant                   n    hit        95% CI     missed    avoided        NET
  D-best (any tick)       103    54%      [45-64%]    $992.26    $690.44    $301.82
  D-theta (safety>=0.3)    65    86%      [76-93%]    $224.33    $109.41    $114.92

--- zero-fire rate by series (C-KNEW basis) ---
series        windows  fired   zero   zero%  edge existed   $ ceiling
bnb 5m             14      2     12     86%             6     $114.96
btc 15m            13      5      8     62%             6      $79.11
btc 5m             45     10     35     78%            30     $610.01
eth 15m            13      5      8     62%             7     $130.49
eth 5m             45     10     35     78%            27     $252.52
sol 5m             45     10     35     78%            25     $480.92

==============================================================================
4. REGIME CONTEXT
==============================================================================

--- |projected margin| vs the guard ---
series       guard      n    p25    p50    p75    p90  <guard  within 1bp  within 2bp
bnb 5m           8    706    3.2    5.6    7.7   13.3     78%         11%         29%
btc 15m          6   2132    2.4    5.9   13.0   16.3     50%         10%         18%
btc 5m           6   2359    1.7    3.9    7.3   12.4     68%         11%         25%
eth 15m          8   2125    2.8    7.8   14.0   26.2     51%          6%         15%
eth 5m           8   2357    2.4    4.8   10.3   16.3     68%          5%         14%
sol 5m          10   2349    4.0    7.8   15.1   21.8     61%          8%         17%
  (%<guard is over ticks with a known margin; 'within Nbp' is the share
   of BLOCKED ticks that a guard N bp lower would have released.)

--- realized sigma tonight vs the 90-day 1m baseline (bp/min, 45m window) ---
symbol   tonight p50  90d p10  90d p25  90d p50  90d p75   pctile
bnb              6.7      2.2      2.8      3.9      5.9      81%
btc              5.1      1.8      2.6      3.9      5.8      68%
eth              6.1      2.5      3.6      5.1      7.5      62%
sol              9.9      3.2      4.2      5.9      8.7      81%

  VERDICT: NOT CALM. Tonight sits ABOVE the 90-day median on every symbol, so
  low volatility is NOT the explanation for the low fire count. The
  fleet is quiet in a normal-to-busy tape.

--- engine-reported sig_bp (eval tape) by series, tonight ---
series            n     p25     p50     p75
bnb 5m          155     7.6     8.5    10.0
btc 15m        1047     4.8     5.4     6.2
btc 5m          740     5.0     5.5     6.5
eth 15m        1047     6.1     6.7    15.6
eth 5m          753     6.2     7.0    15.2
sol 5m          920     9.3    10.6    14.7

--- fires/hour by policy era (whole tape, not just the theta era) ---
era                  from         to  hours  clips  clips/h  armed w  fired w  fired w %
pre-brake       22:02:17Z  02:18:46Z   4.27    397     92.9       82       58        71%
brake           02:18:46Z  05:00:00Z   2.69    378    140.7      123       65        53%
theta           05:00:00Z  06:00:00Z   1.00     56     56.0       44       13        30%
theta+payup     06:00:00Z  08:42:27Z   2.71    108     39.9      131       29        22%
  ('armed w' counts every window with a tape record in the era, so a
   window spanning two eras is counted in both — read the % as a rate,
   not a ledger.)

==============================================================================
5. RECOMMENDATIONS, RANKED BY NET SHADOW $
==============================================================================

Every row is: move this knob this far, and tonight's tape says you would
have taken these episodes, at this hit rate, for this NET. The bootstrap
CI is over episode P&L — where it straddles $0 the number is a direction,
not a result.

LOOSEN = the change relaxes a gate. ROADMAP operating rule: a loosened
gate ships ONLY on a replay A/B win, then one small-size night, then full
size. The A/B command is on the row.

#  knob                   move                 n   hit      hit CI       NET        boot 95% CI on NET  flag
------------------------------------------------------------------------------------------------------------------
1  brake: distrust        disable brake       20   90%    [70-97%]   $269.22        [$119.18, $417.72]  LOOSEN  *CI clears $0 POSITIVE
2  theta (fleet)          0.3 -> 0.1          93   84%    [75-90%]   $201.27        [-$32.32, $425.01]  LOOSEN
3  theta (fleet)          0.3 -> 0.15         71   86%    [76-92%]   $142.58        [-$40.96, $318.20]  LOOSEN
4  eth 5m guard           8 -> 5bp            11   91%    [62-98%]   $133.62        [-$21.07, $359.60]  LOOSEN
5  brake: latched         disable brake       49   90%    [78-96%]    $93.19        [-$14.57, $186.70]  LOOSEN
6  theta (fleet)          0.3 -> 0.2          53   85%    [73-92%]    $92.57        [-$73.44, $245.24]  LOOSEN
7  eth 15m guard          8 -> 6bp             4   50%    [15-85%]    $80.07        [-$97.98, $258.12]  LOOSEN
8  eth 15m guard          8 -> 7bp             3   67%    [21-94%]    $56.32        [-$73.48, $123.66]  LOOSEN
9  eth 5m guard           8 -> 6bp             8  100%   [68-100%]    $53.00          [$18.57, $94.20]  LOOSEN  *CI clears $0 POSITIVE
10 eth 15m guard          8 -> 5bp             3   33%     [6-79%]    $32.61        [-$73.48, $244.81]  LOOSEN
11 early_min_edge (fleet) 0.08 -> 0.03        24   96%    [80-99%]    $26.61         [-$37.66, $70.26]  LOOSEN
12 early_min_edge (fleet) 0.08 -> 0.02        26   96%    [81-99%]    $24.93         [-$36.72, $68.26]  LOOSEN
13 early_min_edge (fleet) 0.08 -> 0.04        19   95%    [75-99%]    $23.96         [-$36.72, $66.56]  LOOSEN
14 brake: avg_down        disable brake        5  100%   [57-100%]    $22.60           [$9.97, $39.04]  LOOSEN  *CI clears $0 POSITIVE
15 btc 5m guard           6 -> 3bp            14   86%    [60-96%]    $17.07        [-$74.25, $100.96]  LOOSEN
16 sol 5m guard           10 -> 7bp           16   81%    [57-93%]    $16.63        [-$93.57, $115.20]  LOOSEN
17 eth 5m guard           8 -> 7bp             4  100%   [51-100%]    $14.87           [$1.67, $34.13]  LOOSEN  *CI clears $0 POSITIVE
18 btc 5m guard           6 -> 4bp            14   93%    [69-99%]    $14.48         [-$45.78, $54.55]  LOOSEN
19 bnb 5m guard           8 -> 5bp             3  100%   [44-100%]     $7.52           [$3.60, $13.02]  LOOSEN  *CI clears $0 POSITIVE
20 bnb 5m guard           8 -> 6bp             4  100%   [51-100%]     $5.36            [$3.11, $7.39]  LOOSEN  *CI clears $0 POSITIVE
21 btc 5m guard           6 -> 5bp             9   89%    [56-98%]    -$1.30         [-$59.80, $36.12]  LOOSEN
22 early_min_edge (fleet) 0.08 -> 0.06        12   92%    [65-99%]    -$3.56         [-$59.34, $29.17]  LOOSEN
23 sol 5m guard           10 -> 8bp           17   76%    [53-90%]   -$21.61        [-$138.48, $83.43]  LOOSEN
24 btc 15m guard          6 -> 3bp             4   50%    [15-85%]   -$32.21         [-$97.98, $33.56]  LOOSEN
25 sol 5m guard           10 -> 9bp           12   75%    [47-91%]   -$32.25        [-$127.96, $52.48]  LOOSEN
26 theta (fleet)          0.3 -> 0.25         36   81%    [65-90%]   -$46.46        [-$189.19, $75.46]  LOOSEN
27 min_edge (fleet)       0.015 -> 0.005      35   86%    [71-94%]   -$90.01       [-$180.52, -$13.70]  LOOSEN  *CI clears $0 NEGATIVE - the gate is earning its keep
28 min_edge (fleet)       0.015 -> 0.008      31   84%    [67-93%]   -$92.66       [-$182.59, -$16.08]  LOOSEN  *CI clears $0 NEGATIVE - the gate is earning its keep

Notes:
  rows 1,5,14:
    ROADMAP operating rule names the three brakes as never-loosen; they are
    priced here to SIZE the cost, not to propose removal. Only replay-only
    Tunables can express them, and only on one night of tape — the night
    they were built for is a different night.
  rows 4,7,8,9,10,15,16,17,18,19,20,21,23,24,25:
    full mode REQUIRED: evals mode has no model on a gated tick

NOT KNOBS — the two blockers no parameter reaches:
  no offer on our side    1174 ticks (9.6% of armed time). The book is bid ~0.99 with
                         nothing offered. Only a MAKER quote reaches this
                         time (ROADMAP Phase 3.1), never a taker gate.
  budget/cooldown          183 ticks — every gate passed and no clip went out.
                         That is a sizing/cadence question, not a gate one.

A/B params files: copy the arm's as-run params, change ONE field
(basis_guard_bp / theta / min_edge / early_min_edge), keep everything
else identical, and
run baseline and candidate over the SAME --slug and --outcomes.

