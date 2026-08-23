# 15m reopen — preliminary read on the RTDS settlement stream (2026-08-23)

Run of `analysis/fifteen_stream_fit.py` against
`~/.pmt/corpus/rtds/rtds-20260823.jsonl`, **08:28:54Z → 17:02:46Z = 8.56h**,
660,258 lines. 15m windows formed: **30 per symbol** (of ~34 in span; 4 lost to
a missing boundary mark). 5m control on the same tape: 92 per symbol.

Recorder health, stamped because it bounds everything below: **46 gaps > 5s,
1,842s = 6.0% of the span**, worst 722s @08:44:14Z (the seam between the
converted legacy `.rtds4/.rtds5` files and the current recorder, which started
08:56:16Z). The recorder is a SECOND subscriber to the stream, so these drops
are not the engine's (CLAUDE.md, replay caveat).

Settlement is the stream's own terminal rule throughout: the settlement-width
TWAP at range end vs the same topic at range start. 15m uses the **sixty**-TWAP
topic (`updown_model::settle_tw_secs`), 5m the thirty. Reference is keyed the
way `updown_rtds` keys it — `per_min[start-60]` is the mark printed AT `start`.

## §1 Decidedness — 15m, and the 5m control on the same tape

P(final winner == sign of the live stream margin) at elapsed fraction f,
conditioned on |margin| ≥ m. Margin is `crypto_prices_chainlink` vs the
window's reference mark.

```
      sym  win  |settle m|bp p50/p90  sig1m  elapsed          >=6bp        >=10bp        >=15bp
--- 15m ---
  btc/usd   30      11.3/31.2           5.4      40%   85% (n= 20)   86% (n= 14)  100% (n=  8)
                                                 60%   95% (n= 22)  100% (n= 14)  100% (n=  7)
                                                 80%  100% (n= 20)  100% (n= 13)  100% (n=  7)
  eth/usd   30      17.0/78.1           7.2      40%   85% (n= 26)   90% (n= 21)   86% (n= 14)
                                                 60%   91% (n= 22)   94% (n= 17)  100% (n= 12)
                                                 80%  100% (n= 26)  100% (n= 21)  100% (n= 16)
  sol/usd   30      17.0/46.3           8.7      40%   88% (n= 26)   88% (n= 24)   82% (n= 17)
                                                 60%   92% (n= 24)   95% (n= 20)  100% (n= 15)
                                                 80%  100% (n= 28)  100% (n= 23)  100% (n= 17)
  xrp/usd   30      41.1/112.3         16.8      40%   85% (n= 27)   92% (n= 25)   91% (n= 23)
                                                 60%   93% (n= 28)   93% (n= 27)   92% (n= 24)
                                                 80%   97% (n= 29)   97% (n= 29)   96% (n= 27)
--- 5m CONTROL ---
  btc/usd   92       6.9/19.2           4.9      40%   94% (n= 33)  100% (n= 14)  100% (n=  5)
                                                 60%  100% (n= 38)  100% (n= 15)  100% (n=  8)
                                                 80%  100% (n= 45)  100% (n= 25)  100% (n= 14)
  eth/usd   92      10.1/35.8           7.1      40%   88% (n= 43)  100% (n= 27)  100% (n= 12)
                                                 60%   93% (n= 56)   97% (n= 35)  100% (n= 13)
                                                 80%  100% (n= 59)  100% (n= 44)  100% (n= 20)
  sol/usd   92      13.9/29.6           8.6      40%   91% (n= 55)   95% (n= 38)  100% (n= 23)
                                                 60%  100% (n= 62)  100% (n= 42)  100% (n= 28)
                                                 80%  100% (n= 69)  100% (n= 53)  100% (n= 33)
  xrp/usd   92      22.0/66.1          15.5      40%   83% (n= 70)   87% (n= 54)   95% (n= 42)
                                                 60%   96% (n= 68)   98% (n= 61)   98% (n= 52)
                                                 80%   99% (n= 76)  100% (n= 69)  100% (n= 63)
```

**15m is a LATER trade than 5m, not a worse one.** At 40% elapsed and ≥10bp,
15m sits at 86-92% where 5m is 87-100%; by 80% elapsed both saturate at
96-100%. That is the duration doing exactly what it should: 40% of a 15m window
leaves **9 minutes** of diffusion, against 3 on a 5m one. Same threshold, more
remaining noise, worse read. The margin distribution moves the same way — 15m
p50 |settle margin| is 1.2-1.9× the 5m one on every symbol, so the bigger
margins are available, they just are not yet decisive at the same clock reading.

The operational consequence is a parameter, not a verdict: `late_rem_s` is
120s, which is 40% of a 5m window and **13%** of a 15m one. A 15m arm inherits
a budget-unlock rule calibrated for a window three times shorter.

## §2 The terminal lock — what the hybrid brake actually prices

Inside the forming sixty-TWAP (rem ≤ 60s), P(final winner == locked side), by
`locked_frac = (tw-rem)/tw`, two estimators:

- `spot` — `sign(spot/ref - 1)`, what `updown_model::terminal_lock` banks today
- `ptwap` — `sign(mean(chainlink over [end-tw, now])/ref - 1)`, the partial
  settlement TWAP itself, which only a sub-minute feed can form

Restricted to **contested** windows (|settle margin| ≤ 15bp) — on a wide-margin
window every estimator is right and the average hides the only cases the brake
exists for:

```
  -- 15m (tw=60s) — CONTESTED --
      sym  win    est        0.00-0.25        0.25-0.50        0.50-0.75        0.75-1.00
  btc/usd   21   spot      99% (n=315)      94% (n=315)      91% (n=313)      89% (n=291)
                ptwap      99% (n=315)     100% (n=315)      99% (n=315)     100% (n=294)
  eth/usd   14   spot     100% (n=210)     100% (n=210)      93% (n=208)      93% (n=193)
                ptwap     100% (n=210)     100% (n=210)     100% (n=210)     100% (n=196)
  sol/usd   13   spot      98% (n=195)      97% (n=195)      93% (n=193)      99% (n=179)
                ptwap     100% (n=195)     100% (n=195)     100% (n=195)     100% (n=182)
```

**The spot proxy gets WORSE as the lock forms; the partial TWAP does not.** On
contested 15m windows btc runs 99% → 89% and eth 100% → 93% across the buckets,
because late in the window the instantaneous price is the *last* input to a 60s
average, not a summary of it. The partial TWAP holds 99-100% in every bucket.

This is a concrete engine finding, not a statistic: `terminal_lock()` takes
`margin_bp` from spot because the comment says "the minute-grain feed can't
resolve finer; spot is the live stream's best proxy" — and on `feed=rtds` that
premise is **false**. The stream carries 1Hz prints, so the formed share of the
settlement TWAP is directly computable, and it is the strictly better estimator
of the quantity the brake waives on. Caveat: `ptwap` here is the arithmetic mean
of the 1Hz chainlink prints, not the oracle's own time-weighting, and it is
closer to the settlement quantity by construction — it should be read as "the
right shape of estimator", not as a calibrated number.

## §3 Bankability — does hybrid have any volume at 15m on this feed?

The question that parked 15m. `guard` = windows where the range-avg projected
margin ever clears the static guard; `banked` = terminal `banked_decided` ever
fires; `entry` = guard AND banked AND the θ=0.3 safety gate on ONE tick;
`ra-bank` = range_avg's own `banked_decided`, for contrast.

```
 set       sym   guard   win     guard    banked   P(win|bk)     entry   P(win|en)  med rem@bk   ra-bank   P(win|ra)
 15m   btc/usd      6bp    30        25        21 100% (n= 21)        14 100% (n= 14)         27s        15 100% (n= 15)
 15m   eth/usd      6bp    30        29        22 100% (n= 22)        20 100% (n= 20)         40s        27  93% (n= 27)
 15m   sol/usd     10bp    30        29        22 100% (n= 22)        17 100% (n= 17)         29s        21  95% (n= 21)
 15m   xrp/usd     12bp    30        30        28 100% (n= 28)        25 100% (n= 25)         37s        26  96% (n= 26)
  5m   btc/usd      6bp    92        52        48 100% (n= 48)        34 100% (n= 34)         12s        36 100% (n= 36)
  5m   eth/usd      6bp    92        71        68 100% (n= 68)        52 100% (n= 52)         12s        55  96% (n= 55)
  5m   sol/usd     10bp    92        53        60 100% (n= 60)        35 100% (n= 35)         11s        36 100% (n= 36)
  5m   xrp/usd     12bp    92        81        66 100% (n= 66)        51 100% (n= 51)         15s        60 100% (n= 60)
```

**The stream restores the lock.** Hybrid reaches `banked_decided` in 21-28 of 30
15m windows and clears the full model-side entry chain in 14-25 — against
`hybrid_ab.md`'s **2 fires over 47 windows** on the minute-grain feed. The
diagnosis in that document was right and the fix is mechanical: the lock was
never absent, it was unobservable.

**And the parked model's failure mode is visible in the same table.**
`P(win|ra)` — range_avg calling a 15m window decided on its own momentum-proxy
banked mass — is 93% (eth), 95% (sol), 96% (xrp). Those are wrong-side
`banked_decided` entries with the brakes waived, which is precisely the shape of
the four catastrophes (−$412, −$403, −$208, −$60) in `hybrid_ab.md`. Under
hybrid the same column is 100%.

**This is a ceiling, not a fill count.** No book, no ask, no
`min_edge`/`min_fair`/`max_price`, no quiesce, no clip cooldown, no
`early_frac`. Depth is the constraint that actually binds on 15m markets and
none of it is modelled here.

## §4 The A/B cannot run today — 15m books and the stream do not overlap

`analysis/fifteen_stream_ab.sh` is written, wired and proven; its preflight
refuses, and the refusal is the finding:

```
book tape   ~/.pmt/engine/book-tape.jsonl
            47 15m windows, 02:45:20Z -> 08:08:57Z
rtds corpus ~/.pmt/corpus/rtds
            btc/usd   08:28:55Z -> 17:00:48Z  (82,201 samples)
runnable 15m windows (book AND rtds): 0
```

`replay --mode full` walks BOOK-TAPE windows, and a `feed=rtds` window
additionally needs the corpus to span `[start, end]` — `RtdsTimeline::build`
refuses anything shorter rather than replaying off klines. **15m book coverage
ends 08:08:57Z; the RTDS corpus begins 08:28:55Z. A 20-minute hole, zero
overlap, on every one of the 47 windows.** The widest 15m book coverage
anywhere on disk (`~/.pmt/corpus/r7-book-tape-frozen.jsonl`, 02:45:20Z →
08:08:57Z) is the same span. 15m is parked, so the engine stopped subscribing
15m books ~20 minutes before the recorder started, and nothing has subscribed
one since.

**What must happen to get books.** The engine has to SUBSCRIBE 15m tokens while
the recorder runs. It does not have to trade them: a **books-only observer arm**
on btc/eth/sol 15m — armed so that no clip can ever fire (`--size 0`, or
`--min-fair 1.0`) — subscribes the tokens and feeds `book-tape.jsonl`, which is
the only missing half. One night of that and the preflight goes green and the
harness runs unchanged. The alternative, degrading the A/B to a bookless
"would-have-fired" count, is the thing this study deliberately does not do:
`hybrid_ab.md` already established that the fill sim's absolute P&L is not
wallet truth even WITH a book, so a P&L table without one would be fiction.

The harness wiring is not taken on trust. Pointed at 5m — the duration whose two
tapes do overlap — `DUR=-5m- analysis/fifteen_stream_ab.sh` runs the complete
path (params → `replay --mode full --rtds-corpus` × 2 → Δnet + bootstrap CI)
over 303 graded stream-fed windows. That run is a wiring check, not a result.

## §5 Verdict — what tonight's 24h read can and cannot conclude

**Can:** it can settle the accuracy question. Decidedness at 15m and the
terminal-lock curve are measured against the stream's own settlement, need no
book, and roughly triple in sample overnight (30 → ~96 windows per symbol). At
n=30 a "100%" cell has a one-sided 95% lower bound near 87%; at n=96 that moves
to ~97%, which is the difference between "consistent with a coin that flips
often enough to matter" and a usable number. It can also confirm or kill the §2
finding — that on `feed=rtds` the partial TWAP is the right lock estimator and
instantaneous spot is not — which is a code change with a clear owner
(`updown_model::terminal_lock`) and does not need a single dollar traded.

**Cannot:** it cannot produce a P&L verdict on reopening 15m, tonight or ever,
on the tapes as they stand. There is no book. Every number in §3 is model-side
opportunity with the depth, the ask, the edge floor and the brakes' price
conditions all removed, and 15m book depth is exactly the constraint that
decides whether an arm that *wants* to fire 20 times a night can fill even once
at a price that clears `min_edge`. It also cannot generalise: this is one 8.56h
session on one regime, with 6% of the tape missing and 4 of 34 windows lost to
absent boundary marks, and the recorder's drops are its own rather than the
engine's — a window can replay as gated on a reference print the live arm did
receive.

**So the gate does not move tonight.** 15m stays parked. The unlock is now two
named, separable steps rather than one vague one: (1) a books-only 15m observer
arm running alongside the recorder, which costs nothing and is the entire
blocker on the A/B; (2) the `terminal_lock` partial-TWAP change on `feed=rtds`,
which §2 says is worth ~10pp of lock accuracy on contested windows at exactly
the moment the brake waives. Re-arming 15m for real money is downstream of both,
and of an A/B that has a book behind it.
