# Guard/theta aggression sweep — 2026-08-23 (verdict: do not loosen)

Trigger: "we're still not trading more... maybe a tad more aggressive."
Frozen tapes (r7-*-frozen, 282 5m windows, 22:02→09:54Z), L36-cleaned
outcomes, replay --mode full, today's scaled sizes (btc 700/100, eth
600/75, sol 200/25, bnb 100/10), same driver for every variant — only
the deltas are read, never the sim's absolute pnl.

| variant | fired | clips | W-L | Δnet vs base | bootstrap CI95 of Δ |
|---|---|---|---|---|---|
| base (guards 6/6/10/8, θ.3) | 61/282 | 316 | 57-4 | — | — |
| guard −2bp everywhere | 100/282 | 639 | 93-7 | **−$104** | [−1368, +999] |
| θ .3→.2 | 68/282 | 359 | 63-5 | **−$66** | [−362, +137] |
| both | 103/282 | 673 | 96-7 | −$5 | [−1377, +1164] |

Loosening guards ~doubles fired windows and adds 36 wins — and 3 extra
tail losses eat all of it. Same shape as the min_edge trim (CI-negative,
freq_funnel report): the marginal window admitted by a looser gate is a
penny-win that occasionally detonates. Frequency is not the lever; the
edge lives in the windows the current gates already admit.

Where aggression goes instead: breadth (xrp on the RTDS stream feed —
see xrp_fit.py; doge deferred at 88-93% decidedness), the maker slice
for no-ask windows (~10% of armed time has nothing to lift), and 15m
reopening behind the stream-fed model.
