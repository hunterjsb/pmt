# Maker step 0, graded by the wallet — and the xrp autopsy

Snapshot 2026-08-23 18:52Z. Driver: `analysis/maker_grading.py`
(`cd pmtrader && uv run python ../analysis/maker_grading.py`). Inputs:
`~/.pmt/engine/order-latency-tape.jsonl`, `~/.pmt/engine/updown-tape.jsonl`,
the rotated engine logs, and the trading wallet's `/activity` feed. Every P&L
figure below is the wallet's; the model's own read grades nothing.

**Headline.** 52 post-only bids reached the wire across 14 windows. **2 filled
— a 3.8% fill rate — for +$1.20 of wallet P&L on $58.80 of notional.** Both
fills won; both earned exactly the 1-tick edge (2.00c/share) the design
predicted. Nothing about the experiment's economics is established at n=2, and
the sample contains no adverse selection at all. The two things it *did*
establish are structural and both are actionable: a re-quote bug that throws
away queue position on 56% of placements, and the fact that **xrp's −$151
hole has nothing to do with the maker path** — it is 117% explained by two
binance-fed windows at 01:40Z, before xrp moved to the settlement stream.

---

## 1. How a maker fill is told apart from a taker fire

The order tape's `post_only` flag records *intent*, and the wallet records what
happened, but neither alone answers the question. Three signals do, and a fill
must satisfy all three.

**1. The fee.** Polymarket bills the taker and not the maker, and the bill is
visible in the wallet: `usdcSize − price × size` is the taker fee and it is
exactly zero when we were the resting side of the match. The fee reconciles to
the cent against `0.07 × min(p, 1−p) × p × size` on every fee-bearing updown
buy in this wallet — e.g. 50 shares at 0.93 charge $0.22785, and
`0.07 × 0.07 × 0.93 × 50 = 0.22785`. So a fee-free fill is *necessary* for
maker origin.

It is not sufficient, and this is the trap. **An ordinary crossing GTC order
that only partly matches leaves its remainder resting, and that remainder
fills fee-free too.** Three of the day's five fee-free fills are exactly that.

**2. A post-only ack at that wire price.** The strategy prices maker bids on a
0.001 grid; `wire_shape` (pmengine/src/order.rs) then floors a post-only buy to
the market's 0.01 tick — *away* from the book, never toward it — so a 0.985
model price rests at 0.98. The wallet fill must sit on a tick a post-only order
of ours actually occupied, and that order must precede the fill.

**3. No crossing order standing ahead of it.** If a plain GTC order at the same
token and the same price was acked *later* than our post-only one, its
unmatched remainder is the simpler explanation and the fill is not credited to
the maker path.

Applied to 2026-08-23:

| time | market | side | fill | fee | verdict |
|---|---|---|---|---|---|
| 16:03:51 | sol-5m-1787500800 | up | 50sh @0.98 = $49.00 | 0 | **maker** — post-only ack 16:03:45, same size, same price, and the only crossing order at 0.98 was fully consumed at 16:03:36 |
| 16:33:04 | sol-5m-1787502600 | up | 36sh @0.98 = $35.28 | 0 | taker-rested — a 51-share crossing order acked 16:33:01 filled 15 as taker and rested 36; the first post-only ack is 9s LATER |
| 17:38:37 | sol-5m-1787506500 | up | 6sh @0.98 = $5.88 | 0 | taker-rested — crossing ack 17:38:34 for exactly 6sh; post-only came 12s later |
| 18:13:34 | xrp-5m-1787508600 | up | 10sh @0.98 = $9.80 | 0 | **maker** — post-only acks 18:13:00 and 18:13:05, exact size, and no crossing order ever stood at 0.98 on that token |
| 18:28:55 | xrp-5m-1787509500 | up | 5sh @0.98 = $4.90 | 0 | taker-rested — crossing ack 18:28:39 for exactly 5sh; post-only came at 18:29:04 |

The existing `updown_stats.maker_summary()` join — "a BUY on the same slug and
side at or below a price we rested at, after we started resting" — credits all
five, and its own docstring says so ("a taker clip that happened to cross cheap
satisfies that too"). It overstates maker fills 2.5x on today's tape. The fee
residual is the missing discriminator and it is free: it is already in every
activity row.

## 2. The rested bids

| | sol 5m | xrp 5m | all |
|---|---|---|---|
| post-only orders on the wire | 38 | 14 | **52** |
| distinct (slug, side, wire price) | 14 | 7 | 21 |
| windows carrying a bid | — | — | 14 |
| fills | 1 | 1 | **2** |
| fill rate per placement | 2.6% | 7.1% | **3.8%** |
| shares filled | 50 | 10 | 60 |
| notional filled | $49.00 | $9.80 | $58.80 |
| **wallet P&L** | **+$1.00** | **+$0.20** | **+$1.20** |
| per rested bid | +$0.03 | +$0.01 | +$0.02 |
| per filled bid | +$1.00 | +$0.20 | +$0.60 |
| c/share | 2.00 | 2.00 | 2.00 |

Clip sizes differ (`sol --clip 50`, `xrp --clip 10`), which is the whole of the
per-fill gap; per share the two are identical because both filled at 0.98 and
both won. The 5-minute windows resolve, so every fill is graded to $1 or $0 —
there is no mark-to-model anywhere in this table.

**The engine log undercounts placements and will keep doing so.** The log
carries 30 `maker bid resting` lines against the order tape's 52, because
`~/.pmt/engine/` had a rotation gap from 15:43Z to 17:38Z with no file at all.
The order tape is the placement record; the log is only good for the 0.001-grain
model price and the safety score.

### 2.1 The re-quote bug — 29 of 52 placements bought nothing

**29 of the 52 wire orders (56%) replaced an order at the identical wire
price.** One window, `sol-5m-1787502600`, burned 12 placements in 77 seconds,
every one of them 50 shares at 0.98.

The cause is a unit mismatch. `maker_slice` skips the re-quote when the new
price is within half a `MAKER_TICK` (0.0005) of the resting one:

```rust
if (resting_px - px).abs() < MAKER_TICK / 2.0 { ...keep the standing quote... }
```

but the order path then floors to the market's real tick, 0.01. A model move
from 0.984 to 0.985 clears the 0.0005 guard, cancels the resting order, and
posts a new one **at the same 0.98**. What it actually spends is queue
position — the only variable that determines whether a resting bid ever fills —
plus a placement token against the CLOB's rate limit.

Fix: compare wire prices, not model prices. The strategy core has no per-market
tick size (that is deliberate, see `maker_bid_price`'s docstring), so either the
tick has to reach the core or the delta matcher has to suppress a replace whose
wire price is unchanged. **Not implemented here** — it is a behaviour change on
a live armed path and wants its own gate. It is the single highest-value item on
the maker backlog: a 3.8% fill rate against a book we are re-queueing to the
back of on more than half our placements is not a measurement of the
opportunity, it is a measurement of the bug.

### 2.2 What the sample does and does not say

- It does **not** price adverse selection. Both fills won. The study's central
  claim — that the unpaired residual is worth about −27c/share against a 1c
  lock — is untouched by two winning fills.
- It does confirm the slice reaches its target class: every placement landed on
  a side with no ask, late in the window, exactly the supply gap
  `analysis/freq_funnel_report.md` charged at 9.6% of armed time.
- Fill rate is low for a structural reason as well as the bug: a resting bid at
  0.98 in a no-ask window is waiting for somebody to *sell* a near-certain
  winner. That counterparty is rare by construction.
- The maker path has never touched a losing window. Both fills, and every
  placement, sit in windows the wallet graded as wins.

---

## 3. xrp 5m autopsy

xrp 5m is **19W-4L, −$151.28** against sol 5m's 61W-2L, +$195.26 (wallet,
`score_activity`, all-time). All four losses are 100% wipeouts — no partial, no
salvage, a $0 redeem row each.

### 3.1 All four are taker-origin

| window | UTC | loss | notional | shares | avg entry | origin |
|---|---|---|---|---|---|---|
| xrp-5m-1787449200 | 01:40–01:45 | −$134.00 | $134.00 | 2615 | 0.0512 | taker (pre-maker era) |
| xrp-5m-1787449500 | 01:45–01:50 | −$43.23 | $43.23 | 58.7 | 0.7369 | taker (pre-maker era) |
| xrp-5m-1787505300 | 17:15–17:20 | −$28.44 | $28.44 | 31.0 | 0.9175 | taker |
| xrp-5m-1787508300 | 18:05–18:10 | −$23.19 | $23.19 | 32.0 | 0.7246 | taker |

The first two predate `maker_bid` being armed at all (first post-only ack
14:49Z). The last two are in the maker era but the strategy never rested a bid
on either slug — the maker-rest tape covers `xrp-5m-1787504700`, `-1787508600`,
`-1787508900` and `-1787509500`, and none of those lost. **Maker step 0 has zero
exposure to the xrp hole.**

### 3.2 The shape they share: the model is pinned and the book screams

Every loss is the same trade. `p_up` is pinned at ~0 or ~1, the model marks fair
at 0.99+, and the book is quoting the opposite side by 20 to 90 points. The
engine reads the gap as edge and buys into it.

```
1787449200  fair 0.9946 -> 1.0000 on DOWN, 8 fires, asks 0.95 0.81 0.90 0.98 0.90 0.95 0.01 0.01
            the last fire: 2500 shares at ONE CENT, "net 0.9893"       -> settled UP
1787449500  fair 0.9998 on UP, asks 0.75 and 0.71                      -> settled DOWN
1787505300  fair 0.9963 -> 1.0000 on DOWN, asks 0.89 0.93 0.97 0.90    -> settled UP
1787508300  fair 0.9985 -> 1.0000 on DOWN, asks 0.98 0.98 0.54         -> settled UP
```

The `distrust` brake exists for precisely this (`net > 0.15` means the book is
pricing in something we are not), and **`banked_decided` exempts it**
(`distrust_blocks(net, threshold, banked_decided)` returns false when banked).
Six of the fires inside these four windows carried `net > 0.15` and fired
anyway: ≈$103 of the $228.86 lost, **45% of the hole, is exposure the distrust
brake flagged and the banked-decided carve-out waved through.** The same
carve-out clears the `avg_down` brake and the `brake_latched` hold.

`1787508300` shows how thin that carve-out can be. Its last eval reads
`margin_bp = −12.95` against `cushion_bp = 12.91` — the window flipped to
"decided" on **0.04bp** of headroom, and three seconds later a fire crossed at
0.54 that the distrust brake had blocked on the previous tick.

### 3.3 Would the fleet's standard gates have caught them elsewhere? No — they are looser elsewhere

xrp runs the *tightest* basis guard on the board:

```
basis_guard_bp   btc 5m 6.0   eth 5m 6.0   bnb 5m 8.0   sol 5m 10.0   xrp 5m 12.0
```

Every other gate (`min_edge` 0.015, `min_fair` 0.97, `theta` 0.3, `p_cap` 1.0)
is identical across the 5m fleet. On btc or eth params these fires would have
passed *more* easily, and the fleet-wide count of net>0.15 fires that fired
anyway is btc 81 (~$1,558 notional), eth 51 (~$737), sol 16 (~$307), xrp 14
(~$172). **This is a fleet policy, not an xrp gate hole.** xrp is simply the
thinnest book, so its asks travel furthest from our fair before anyone lifts.

### 3.4 The stream is not the problem — it is the fix

xrp is the only `--feed rtds` arm. Splitting its windows at the cutover (first
xrp window on the stream: `xrp-5m-1787484600`, 11:30Z):

| era | record | P&L | notional | % of notional |
|---|---|---|---|---|
| binance-fed (before 11:30Z) | **0W-2L** | **−$177.23** | $177.23 | **−100.0%** |
| rtds-fed (11:30Z onward) | **19W-2L** | **+$25.94** | $368.30 | **+7.0%** |

**117% of the −$151.28 headline is two binance-fed windows.** The stream era is
profitable. As a control, sol 5m over the same clock is 34W-1L +$117.87 before
11:30Z and 27W-1L +$77.39 after — flat across the boundary, so this is not a
regime shift that lifted everything.

That result is exactly what the feed change was for: on `binance` the model
priced a Chainlink-settled market off a different venue, and the two losing
windows are what a cross-venue basis error looks like when the model is
certain. On `rtds` the reference, spot and TWAP marks all come off the series
that settles the market and the basis disappears.

Feed health, for completeness: **14 RTDS disconnects across today's logs** (not
6), thirteen of them `stalled 31s with the socket open` and one clean close.
The stall is silent — the socket stays up and events stop — and the 31s
watchdog is what catches it. The staleness gate did its job: `1787508300` logged
34 gated ticks, 11 of them `feed stale`, and no fire was priced off a stale
mark. What the stall *did* contribute is §3.2's 0.04bp decidedness flip on a
feed that had stalled twice inside that window. **One of four losses is
stream-adjacent, and via the decidedness margin, not via stale pricing.**
`1787505300` had zero gated ticks — a healthy feed and a genuine terminal-rule
miss (margin −27.4bp against a 17.5bp cushion, settled the other way).

### 3.5 Verdict

1. **Keep xrp.** The all-time number is a fossil. Since the stream cutover xrp
   is 19W-2L at +7.0% of notional. Nothing in the current configuration
   produced the −$177.
2. **Keep xrp maker bids.** They have zero exposure to every loss, the one xrp
   maker fill won, and xrp's thin book is where the no-ask supply gap is
   widest. n=1 is not evidence of edge either — this is "no reason to pull it",
   not "it works".
3. **Change one param, and it is not an xrp param.** The lever all four losses
   share is the `banked_decided` carve-out on the distrust brake — 45% of the
   loss by notional. The narrow version: keep the carve-out, but require the
   decidedness to have *cushion*, not just sign (`|margin| > k × cushion` for
   some k > 1 rather than k = 1), and refuse it outright on a window whose feed
   has gone stale. `1787508300` fails both tests; `1787505300` fails the first
   at k ≥ 1.6. This is a taker-path change on a live armed fleet and is written
   down here rather than shipped.
4. **Stop reporting xrp all-time without the split.** `pmt crypto stats --era`
   already has the vocabulary for this; the rtds cutover deserves an era
   boundary of its own.

---

## 4. What changed in the engine

Only the maker inventory brake, and it is dark. `maker_paired_credit`
(`pmengine/src/strategies/updown_model.rs`) returns the notional held in a
matched up/down stack, and `maker_slice` adds it to the headroom a resting bid
may size against — the study's §3.1 conclusion that the brake belongs on
`|shares_up − shares_dn|` and not on gross size.

It cannot loosen one-sided exposure, and that is arithmetic rather than a
promise: the unpaired residual is valued at `max_price`, the dearest entry any
path can take, so on a one-sided arm the residual's valuation is `≥`
`position_floor` and the clamp returns exactly zero. It is zero whenever
`maker_bid` is off. Spending draws `room` down as it always did, so two resting
sides share one pool rather than each claiming it, and the R7 fleet bound still
binds in full.

Pinned by `maker_brake_reads_net_inventory_not_gross`,
`the_net_brake_credits_only_the_paired_half`,
`the_net_brake_is_dark_while_maker_bid_is_off`, and two unit tests on the
helper itself.

## 5. Known weaknesses

- **n=2 on the fills.** Both won. This is not a measurement of maker
  economics; it is a measurement that the plumbing works.
- **The order tape starts at 10:28Z.** Fills before that cannot be attributed
  by order, only by fee, and the fee alone cannot separate maker from
  taker-rested. Every maker figure here is inside the tape's window.
- **The engine log has a 15:43–17:38Z hole**, so anything log-derived (the
  binance/rtds membership set, the 0.001 model prices) is incomplete in that
  range. The xrp split above uses the cutover *epoch* rather than log
  membership for exactly this reason.
- **The pre-stream xrp sample is two windows.** They are the entire hole, but
  two windows is not a distribution, and "binance-fed xrp loses" rests on them.
- **Outcome for lost windows is inferred**, not read: a losing window's redeem
  row names no winner, so the loser is taken to be the only side we held. Every
  window here held one side.
- **The 0.07 fee model is fitted to this wallet's rows**, not read from an API.
  It reconciles exactly on every fee-bearing row, but a fee-schedule change
  would break the discriminator silently — the check to keep is that fee-free
  and fee-bearing are the only two classes, with nothing in between.
