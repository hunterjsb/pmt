# Hybrid settle-rule A/B — momentum evidence, terminal risk (2026-08-23)

`settle_rule="hybrid"`: p_up / margin / banked evidence from the range-avg
momentum proxy (what the wallet proved at 5m), cushion + banked_decided +
flip_proof from TERMINAL-rule arithmetic (what actually settles). Replay
`--mode full`, live-policy params (theta .3, pay-up .02, per-symbol guards),
outcomes = the L36-cleaned corpus (wallet/chainlink≥5bp/terminal-book).

## Results (graded windows only, aggregate rows excluded)

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

## Caveats

- The fill sim's ABSOLUTE pnl is not wallet truth: it shows range_avg 5m
  deeply negative over a span where the live fleet was near break-even —
  it fills clips live latency/matcher suppression would have missed and
  holds to settlement where live exits. Only the RELATIVE read is used.
- Hybrid barely banks anything at 15m because the minute-grain per_min
  feed sees the forming 60s settlement TWAP as ONE sample — the lock is
  invisible until the wire. Sub-minute RTDS feed is the unlock.

## Verdict

- 5m LIVE: stays range_avg. Proven live record beats a sim that trades
  3× less against the scale-up direction.
- 15m: stays PARKED. Hybrid is the proven-safe spec — re-arm gate is the
  RTDS-fed model (ref/spot/locked-frac from the settlement stream itself),
  then rerun this A/B expecting real 15m volume.
- Code ships DARK behind settle_rule (default range_avg unchanged).
