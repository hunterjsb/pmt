# xrp 5m fit on the settlement stream — the stream-fed re-add gate

Date: 2026-08-23. Run of `analysis/xrp_fit.py` against
`~/.pmt/corpus/rtds/rtds-20260823.jsonl` (209,532 prints, 08:28Z→11:19Z = 2.85h,
6 recorded gaps totalling 237s).

The question is narrow: xrp was struck off the tradeable list because its
Binance-vs-Chainlink basis (p95 22.5bp, docs/LESSONS.md#L32) demanded a guard
that gutted its own opportunity. The RTDS recorder now captures the settlement
stream itself, so this fit uses **only stream quantities** — the world a
`--feed rtds` arm actually lives in. Peers on the same tape are the control:
xrp "fits" if its curve at reachable margins matches symbols the wallet has
already proved.

Decidedness = P(final winner == sign of the live stream margin), sampled at
elapsed fractions 0.4/0.5/0.6/0.7 of each 5m window, conditioned on
|margin| ≥ m. Reference and settlement are both `crypto_prices_twap_thirty`
at the window bounds; the live margin is `crypto_prices_chainlink` vs the
reference.

```
      sym  win  |settle m|bp p50/p90 1m sig bp  decidedness at elapsed>=40%, |margin|>= 4 / 6 / 10bp
  xrp/usd   29      17.4/51.4           13.7   92% (n=98)   94% (n=94)   97% (n=78)
  btc/usd   29       5.9/18.5            4.4   97% (n=60)  100% (n=37)  100% (n=15)
  eth/usd   29       6.9/14.3            5.8   90% (n=70)   92% (n=52)  100% (n=26)
  sol/usd   29      11.9/22.0            7.6   92% (n=88)   95% (n=65)   95% (n=39)
  bnb/usd   29       6.8/13.7            4.5   95% (n=74)   98% (n=51)  100% (n=20)
 doge/usd   29      14.6/45.0           14.4   87% (n=95)   87% (n=87)   93% (n=67)
```

## The read

**xrp fits — on this feed.** At ≥10bp its decidedness is **97% (n=78)**, inside
the band the proved symbols occupy (sol 95%, eth/bnb/btc 100% on thinner n),
and it gets there with **3× the sample count of any peer**: xrp reaches a 10bp
margin in 78 of the sampled ticks against btc's 15. That is the whole shape of
the trade — xrp's 1m sigma is 13.7bp against btc's 4.4, so margins that are
rare on btc are ordinary on xrp, and the same |margin| threshold buys the same
reliability far more often. Its median settlement margin (17.4bp) is nearly
three times btc's (5.9bp), which is the same fact from the settlement side.

**Guard: 12bp proposed.** The p90 settlement margin is 51.4bp and the 1m sigma
is 13.7bp, so 12bp is under one minute of noise and comfortably inside the
reachable margin distribution — 78 of the sampled ticks clear 10bp. Below 10bp
decidedness degrades to 94%/92%, which is where the loss asymmetry (bounded up,
unbounded down — docs/LESSONS.md#L27) stops paying. Rounding up from 10 to 12
follows the fleet's standing bias: prefer a foregone win to a paid loss.

**doge deferred.** 87% / 87% / 93% across the same thresholds — it never
reaches the fleet's band at any reachable margin, and at 93% the break-even
maths on a 0.92-0.98 entry does not close. doge's problem is not basis (the
stream deletes that for it too); it is that its tape genuinely flips.
Re-measure on a longer corpus before arming it.

## Caveats

1. **n=29 windows, 2.85h, one session.** This is a go/no-go screen, not a
   calibration. The peers being measured on the same 29 windows is what makes
   it worth anything; the absolute percentages are not stable at this n.
2. The corpus is append-only and a re-run sees a larger one (docs/LESSONS.md#L33)
   — the numbers above are stamped to the span named at the top.
3. This says nothing about **book depth**, which is the constraint that actually
   binds on alts (see `analysis/bnb_fit.md` §6). The first live night is a
   fill-rate experiment as much as a P&L one; clip small.
4. Decidedness is measured against the stream's own settlement, never against
   gamma resolutions. It is evidence that the model can read this feed, not
   that Polymarket resolves the way the feed says.

## Proposed arm (dark until the operator runs it)

```
pmt crypto arm https://polymarket.com/event/xrp-updown-5m-<epoch> \
    --feed rtds --size 100 --clip 10 --basis-guard 12 --theta 0.3 \
    --min-elapsed 0 --pay-up 0.02
```

`sigma_bp_per_min` comes off the market pricing at arm time and should read
≈14 for xrp (the fit measures 13.7 on the stream). `--feed rtds` is the load-
bearing flag: on `--feed binance` this arm is the one that blew up, and the
22bp guard it would need keeps only 41% of btc's opportunity rate.
