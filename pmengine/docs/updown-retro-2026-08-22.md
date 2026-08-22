# updown trigger — first live night retro (2026-08-22)

Eight 5m/15m windows armed between 21:15Z and 22:20Z. Full per-fire tape in
`~/.pmt/engine/updown-tape.jsonl` (fires with stated fair from 22:00Z on).

## Results

| window | side/entry | stated fair at fire | result |
|---|---|---|---|
| 21:15 5m  | UP @ 0.937 avg  | ~0.99  | **WIN** +9.37 |
| 21:15 15m | UP @ 0.889 avg  | ~1.0   | **WIN** +22.91 |
| 21:35 5m  | no trade (asks ≥ 0.98 over gates) | — | correctly flat |
| 21:40 5m  | DOWN @ 0.79     | 0.9947 | **WIN** +53.13 |
| 21:45 5m  | DOWN @ 0.901 avg (7-fire ladder) | 0.90→0.999 | **WIN** +33.24 |
| 22:00 5m  | DOWN @ 0.921 avg | 0.9923 | **LOSS** −317.76 |
| 22:15 5m  | UP @ 0.782 avg  | 0.945–0.957 | photo finish, likely LOSS −194 |
| 22:20 5m  | disarmed pre-fire | — | flat |

Aggregate ≈ −$390 net. Model log-loss is the real verdict: two losses at
stated p of 0.99 and 0.95 inside 20 minutes is not variance, it's bias.

## The pattern

Wins 21:15–21:45 came during a sustained BTC downtrend: moves persisted, so
"banked margin + spot momentum" locks stayed locked. Both losses came after
~22:00 when the tape turned violently mean-reverting — every 30–50bp move
was faded inside 2–3 minutes, which is exactly one 5m-window timescale.

The model assumes the remaining window path is a driftless random walk
centered at CURRENT spot. In a reverting tape the conditional expectation
after a sharp move is *behind* spot, not at it. Worse, the trigger's fires
are structurally timed at momentum extremes — that is when the projected
margin clears the gates and asks lag — so in chop it systematically buys
the top of micro-moves. The same behavior that printed five wins in trend
is the behavior that lost twice in chop. The strategy as shipped is an
implicit bet on move persistence; it has no idea which regime it is in.

Secondary finding: spike-aware vol (added mid-night) helped honesty
(0.945 stated vs the earlier 0.99-style claims) but vol ratchets measure
*amplitude*, not *serial correlation* — they cannot see reversion.

The exit rule behaved correctly in the 22:15 window: fair rode ~0.5 into
the close and never crossed the 0.40 evacuation line, and selling a coin
flip at a coin-flip bid is EV-neutral. Exits are for true sign flips; the
22:00-style collapse (0.99 → 0.05) remains its target case.

## Fixes before the next live arm (in order)

1. **Regime gate** — rolling lag-1 autocorrelation ρ of 1m returns
   (trailing 60m + fast 15m read) from the feed's existing closes.
   ρ < −0.25 ⇒ no mid-window momentum entries at all.
2. **Banked-decided entries in chop** — when reverting, only fire if the
   banked contribution alone decides the window (outcome safe even if the
   remaining path fully reverts to the reference). True resolution snipe,
   immune to reversion.
3. **Reversion-adjusted projection** — E[remaining avg] pulled from spot
   toward the trailing mean with weight |ρ|; widens breakeven distance in
   chop instead of pretending spot is the anchor.
4. **Kelly sizing** — quarter-Kelly on post-haircut p. Stated-0.94 entries
   must not take the full stack (22:15 did).
5. **Calibration ledger** — tape already records stated fair per fire;
   maintain realized-vs-stated by p-bucket and refuse size increases until
   ≥20 decided windows show calibration. Same validation gate as sports.
6. **Prefer 15m+ windows** — banked fraction dominates the reversion
   timescale sooner; 5m windows are the most reversion-hostile.

Trading paused until 1, 2, and 5 are live. 4 rides with the z-gated EV
rework (sketched 2026-08-22, this doc is its requirements input).
