# Fleet orchestrator — series leases and automatic failover

**Status:** phase 0 (this document) and phase 1 (the doctor) are built. Phase 2
— actually failing over — is specified at the end and is NOT built. It does not
get built until you have read this.

The charge: *"look at all engine nodes: if this one is offline, EUW should cover
its series. if both online, respect what each node tells it. if EUW is offline,
this engine picks up BNB."*

## 0. The constraint, and why the obvious design is wrong

Two engines under one operator must never trade the same series at the same
instant. Their orders sit on the same book under the same beneficial owner, so
one can match the other's resting quote — wash-trade shaped, no matter that
neither meant it (CLAUDE.md, "Series partition"). That is not a preference. It
is the thing that makes this whole feature dangerous to build.

The obvious design — *"watch the other node's heartbeat; if it goes stale, take
its series"* — fails on the one scenario that matters. A **network partition**
leaves both nodes alive, both trading, and each seeing a dead peer. Both take
over. Both quote. The failure mode of the safety feature is the exact hazard the
safety feature exists to prevent, and it fires precisely when the network is
already having a bad day.

So liveness is never the authority here. The authority is a **lease**, and the
lease intervals are non-overlapping *by construction* rather than by anyone's
opinion about who is alive.

## 1. The protocol

### 1.1 Objects

One DynamoDB table, `pmt-fleet` (eu-west-1), single-table, `pk`/`sk` strings.

| item | key | what it is |
| --- | --- | --- |
| lease | `series#<series>` / `lease` | **the only authority on who may trade a series** |
| heartbeat | `node#<node>` / `hb` | observability. Nothing in the protocol reads it. |
| config | `fleet` / `config` | the kill switch |

The lease item:

```
holder            "desktop" | "euw"
epoch             int, strictly increasing — the version counter and the linearisation point
expires_at        float, epoch seconds, written on the holder's clock
released          bool — the holder has confirmed it has no live orders
holder_is_home    bool  — was this holder the home node, as of acquire time
grace_s           float — copied from the map at acquire time
home_extension_s  float — copied from the map at acquire time
```

`grace_s`, `home_extension_s` and `holder_is_home` are **copied onto the item**
rather than looked up. A claimant must never compute a deadline from its own
copy of the map: if the operator edits the map mid-flight, the in-flight lease
still resolves under the terms it was taken out on, and both sides compute one
function of one recorded fact.

### 1.2 The three rules

> **R1 — authority.** A node may have live orders on S only while it holds a
> lease on S, and it stops at its own `fence` deadline whether or not it can
> reach anything.
>
> **R2 — the claim bar.** A claimant may acquire only after `fence + grace(S)`,
> where `grace` is sized so the previous holder is provably quiet by then.
>
> **R3 — the CAS.** Every mutation bumps `epoch` and is conditional on the epoch
> the writer read.

R3 is one line of DynamoDB and it kills the double-claim race outright. R1 and
R2 are the arithmetic that closes the gap *between* epochs.

### 1.3 State machine

```
                       acquire (CAS: attribute_not_exists OR epoch = seen)
       ┌───────────┐  ────────────────────────────────────────────►  ┌──────────┐
       │  UNHELD   │                                                 │   HELD   │
       └───────────┘  ◄────────────────────────────────────────────  └──────────┘
             ▲          claim bar passed, someone else acquires        │   │   ▲
             │                                                        │   │   │
             │                            renew (CAS: holder=me AND ───┘   │   └── renew
             │                             epoch=mine; bumps epoch)        │
             │                                                             │
             │                    ┌────────────────────────────────────────┤
             │                    ▼                                        ▼
             │            ┌───────────────┐                        ┌──────────────┐
             │            │   EXTENDING   │ (home holder only)     │   RELEASED   │
             │            │ past expiry,  │                        │ orders proven│
             │            │ inside home_  │                        │ gone; claim  │
             │            │ extension_s   │                        │ bar = now    │
             │            └───────────────┘                        └──────────────┘
             │                    │                                        │
             │                    ▼  fence                                 │
             │            ┌───────────────┐                                │
             └────────────│    LAPSED     │◄───────────────────────────────┘
              claim bar   │ holder quiet, │
              = fence     │ nobody trades │
              + grace     └───────────────┘
```

Renewal is deliberately **not** conditioned on expiry — only on `holder=me AND
epoch=mine`. A holder that lost the store for eight minutes and comes back to
find nobody claimed simply renews and carries on. If someone *did* claim, the
epoch moved and the renewal fails, which is the correct answer.

### 1.4 Fence and claim bar

```
fence(lease)       = expires_at + (home_extension_s if holder_is_home else 0)
claimable_at(lease)= expires_at                       if released
                     fence(lease) + grace_s           otherwise
```

The holder stops at `fence`. The claimant starts no earlier than `fence +
grace`. The `grace`-wide hole between them is where safety lives.

### 1.5 Grace, sized

```
grace_floor(S) = window_dur(S) + FENCE_CHECK_S + STOP_LATENCY_S + 2·MAX_SKEW_S
               = window_dur    + 5             + 30             + 10
```

| series | window | floor | shipped | your stated minimum |
| --- | --- | --- | --- | --- |
| 5m | 300 | 345 | **420** | ≥ 360 |
| 15m | 900 | 945 | **1200** | ≥ 1200 |

The `window_dur` term is the one that deserves defending, because it dominates
the budget. The other three terms are small and mechanical. This one encodes an
assumption about **how weak the departing holder's stopping mechanism might
be.**

`pmt crypto disarm` does pull orders mid-window. But the enforcement point we
can lean on hardest is the **in-strategy roll refusal** — the check inside
`updown` that fires when a roll chain tries to re-arm the next window, which
exists precisely because a roll chain re-arms itself without passing through the
control plane. That check acts at a *window boundary*. Budgeting a whole window
means the non-overlap argument survives even if roll refusal is the only thing
that works — which is exactly the assumption to make about a node sick enough to
have lost its lease.

Refusing a too-small grace is a hard failure in `assign.validate`, not a
round-up. A grace that does not cover the old holder's stop is the one bug in
this system that costs money quietly.

### 1.6 The clock bound

Every deadline is written on one node's clock and enforced on another's, so a
silent clock fault is the single input that could make two provably-disjoint
intervals overlap in real time.

This is **measured, not assumed**. Every DynamoDB response carries an HTTP
`Date` header, so each round trip samples the store's clock; the store is the
natural reference because it is the one clock both nodes provably share a view
of. `MAX_SKEW_S = 5.0`.

- **Max tolerable skew: 5s per node, 10s between any two participating nodes.**
- Measured reality on NTP-disciplined hosts is <100ms (chronyd on the desktop,
  the Amazon Time Sync Service on the EU box), so the bound carries ~50x
  headroom and only fires on a clock that is *wrong*, not one that is imprecise.
- HTTP `Date` is whole seconds, so a single sample carries ~1s of quantisation.
  That is why the bound is 5s and not 500ms.
- A node past the bound **refuses to acquire** and **fences itself out of any
  lease it holds**. An *unmeasured* offset (`None`) fails the same way. A node
  that cannot place itself in time cannot promise to stop on time.

### 1.7 Release, and the nightly poweroff

The desktop powers off every night on purpose. That is a designed-for event,
not an incident.

On SIGTERM the engine already runs graceful shutdown and cancels its resting
orders. The lease is then **released**: `released=true`, epoch bumped. A
released lease is claimable immediately, so handover is instant rather than
`grace`-long.

Two disciplines make that safe:

1. **Stop first, release second.** `released=true` is a claim that the holder
   has no live orders. It is written only after that is confirmed.
2. **Release is an optimisation; expiry is the guarantee.** If the daemon cannot
   confirm the engine stopped — SIGKILL after `TimeoutStopSec`, a wedged
   shutdown — it does **not** release. The lease simply lapses, costing `grace`
   of downtime and never correctness.

Filled inventory riding to resolution is not an obstacle to release. The hazard
is two nodes with *live order flow* on one book; a position with no orders
behind it cannot self-match, and each node's inventory sits on its own wallet.

### 1.8 The home/failover asymmetry — availability vs safety when the store is down

This is the decision you asked me to make explicitly and defend.

> **Home holder:** on renewal failure, keeps trading for `home_extension_s` past
> expiry, then stops.
> **Failover holder:** on renewal failure, stops at expiry. No extension.

**Why this cannot double-trade.** A claim is a store **write**. If the store is
unreachable, no acquire can succeed anywhere, so no new epoch can arise and the
set of nodes trading S can only *shrink*. The one node that continues past
expiry is the home holder — and the claimant budgets `home_extension_s` too,
because it reads `holder_is_home` off the same lease item. Two nodes can never
both take the availability branch for one series, because there is exactly one
lease item per series and it names exactly one holder.

Note what this does **not** rest on: it does not rest on the two boxes holding
identical copies of the assignment map. The map decides who is allowed to *try*.
The lease item decides who actually holds. A diverged map costs a refused
acquire and a confusing log line, never a self-match.

**Why not put home series under a strict lease too** (stop at expiry, no
extension)? Because it buys zero safety and costs full availability. The home
partition is *already* safe without the store — it is exactly today's static
`PMENGINE_SERIES_ALLOWLIST`, which is safe by construction because the map gives
each series one home. Putting it under a strict lease would convert a DynamoDB
blip into a total trading outage and make the store a single point of failure
for something that does not need it. The store's failure mode is deliberately
"no failover", and "no failover" is the fleet exactly as it runs today.

**`home_extension_s` is the dial**, and it is the one number here with a real
trade-off:

| value | store-outage tolerance | failover after a hard crash (5m series) |
| --- | --- | --- |
| 0 | none — a blip stops the fleet | ~8.5 min |
| **600 (shipped)** | 10 min | ~18.5 min |
| 1800 | 30 min | ~38.5 min |

600s is the recommendation: it covers every realistic DynamoDB event (throttles,
transient 5xx, an SDK retry storm) against a table doing under one write per
second, while keeping crash failover inside twenty minutes. It costs nothing at
all in the common case, because a **graceful release short-circuits every
deadline here** — and the nightly poweroff is graceful.

Could failover be made faster by claiming early when the peer's heartbeat is
*also* stale? No, and this is worth stating because it is the tempting bug: a
node partitioned from the store cannot write its heartbeat either, so "lease
stale AND heartbeat stale" is exactly what a live-but-partitioned node looks
like. Any shortcut that a claimant cannot *verify* re-imports the liveness
judgement the whole design exists to eliminate.

### 1.9 Kill switch

`pk="fleet", sk="config"`, attribute `disabled` (bool). One flag, fleet-wide.

- It freezes **new failover claims**. It does not freeze a home node reclaiming
  its own series (that is how the fleet returns to rest — freezing it would
  strand a series on the covering box with no way back), and it does not freeze
  renewals by a node already holding (yanking a live takeover mid-window is a
  much larger action than "stop failing over", and a separate decision).
- **Unreadable reads as disabled.** A safety control that fails open is
  decoration. A *missing* item is different from an unreadable one and reads as
  enabled — that is the table's resting state.

## 2. Failure matrix

`H` = home node, `F` = failover node, `S` = a series homed on H.

| # | failure | what happens | can two nodes quote S? |
| --- | --- | --- | --- |
| 1 | **H crashes hard** (panic, power cut, OOM) | H's process is gone, so its orders are gone. Lease lapses at `expires_at + home_extension_s`. F acquires at `+ grace`. | **No.** H places nothing once dead. |
| 2 | **H shuts down gracefully** (nightly poweroff, `systemctl stop`) | Engine cancels resting orders, daemon writes `released=true`. F acquires immediately. | **No.** Release is written only after orders are confirmed gone (§1.7). |
| 3 | **H wedges** (alive, not trading, not renewing — the axum-panic class) | Same as 1. The engine is up but headless; it holds no orders it can act on. Lease lapses, F takes over after `fence + grace`. | **No.** |
| 4 | **H partitioned from the store, still trading** | H cannot renew. It rides `home_extension_s`, then fences itself and stops. F's claim bar is `expires_at + home_extension_s + grace` — it budgets H's full extension because `holder_is_home` is on the item. | **No.** The two deadlines are `grace` apart by construction. |
| 5 | **F partitioned from the store while holding S** | F gets no extension: it fences at `expires_at` exactly. H reclaims at `+ grace`. | **No.** And handover is fast, which is why the asymmetry points this way. |
| 6 | **Store outage (whole table down)** | Nobody can acquire — an acquire is a write. H keeps trading its home series for `home_extension_s`; any failover holder stops at expiry. The trader set shrinks monotonically toward the static home map. | **No.** A store outage can only *remove* a trader, never add one. |
| 7 | **Store outage longer than `home_extension_s`** | H stops too. The fleet is dark and safe. | **No.** |
| 8 | **H ↔ F network partition** | **Irrelevant.** The nodes never talk to each other. There is no node-to-node path in this design, deliberately: it is the channel whose failure produces split-brain. | **No.** |
| 9 | **Clock skew inside 5s** | Absorbed by the `2·MAX_SKEW_S` term in the grace budget. | **No.** |
| 10 | **Clock skew beyond 5s** | Detected against the store's `Date` header. The offending node refuses to acquire and fences out of anything it holds. | **No.** It removes itself. |
| 11 | **Clock unmeasurable** (no `Date`, unparseable) | Treated as skew-out-of-bound. Fails closed. | **No.** |
| 12 | **Double-claim race** — two nodes read the same epoch, both decide "claimable", both CAS | DynamoDB serialises conditional writes on one item. First bumps to `epoch+1`; second's `epoch = seen` no longer holds and it fails. | **No.** Exactly one winner (tested with 12 threads on a barrier). |
| 13 | **Stale read** — claimant reads, sleeps, holder renews meanwhile | The CAS refuses the write. Every write that lands was decided on an image proven current at write time. | **No.** |
| 14 | **Assignment map diverges between boxes** | The lease item is the authority; the map only says who may *try*. Worst case is a refused acquire. | **No.** |
| 15 | **Both boxes wrongly believe they are home for S** | Only one can hold the single lease item, and only the holder trades. | **No.** |
| 16 | **Throughput cap throttles the table** | Renewals start failing → holders fence → trading stops. The cost guard fails **safe**. | **No.** |
| 17 | **Lease item deleted by hand** | Reads as unheld, instantly claimable — the one genuinely dangerous manual action. Mitigated by giving leases **no TTL** so nothing deletes them automatically, and by `DeletionProtectionEnabled` on the table. | **Yes, if done while a holder is live.** Operator hazard, documented, not defended against in code. |
| 18 | **Two daemons for one node** (unit + hand-started) | Heartbeat writes are conditional on `ts <= :ts`, so the row is monotonic and cannot flap backwards. | n/a (heartbeats are not authority) |

Row 17 is the honest gap. Everything else is closed by the protocol; that one is
closed by not doing it.

## 3. The non-overlap argument

**Claim.** For any series S, at no instant do two distinct nodes both have live
order flow on S.

**Setup.** By R3, every mutation of S's lease item bumps `epoch` and is
conditional on the epoch its writer read. DynamoDB conditional writes on a
single item are linearizable. Therefore the history of S's lease is a **totally
ordered chain** `L₁ → L₂ → …`, each link with exactly one `holder`.

That immediately disposes of the *same-epoch* case: two nodes cannot both hold
`Lₖ`. What remains is temporal overlap *across* links — holder(`Lₖ`) still
trading when holder(`Lₖ₊₁`) begins.

Fix `k`, write `A = holder(Lₖ)`, `B = holder(Lₖ₊₁)`, `A ≠ B`.

**Case A — `Lₖ₊₁` arose from a release.**
By §1.7 R6, `released=true` on `Lₖ` is written only after A confirmed it has no
live orders on S. That write precedes B's acquire in the chain order. So A's
trading interval closed before B's opened. ∎

**Case B — `Lₖ₊₁` arose from expiry.**
Let `E` = `Lₖ.expires_at` (written on A's clock) and
`F = E + home_extension_s·[Lₖ.holder_is_home]` — A's fence.

*Upper bound on A.* By R1, A stops trading at `F` on its own clock. It checks
its fence every `FENCE_CHECK_S`, so it notices at most `FENCE_CHECK_S` late; its
orders then take at most `STOP_LATENCY_S` to die, and — under the weakest
assumption about its stopping mechanism (§1.5) — it may only be able to stop at
a window boundary, at most `window_dur` away. So A's last live order dies by
`F + window_dur + FENCE_CHECK_S + STOP_LATENCY_S`, **on A's clock**.

*Lower bound on B.* By R2, B acquired at a local time `> F + grace_s`. Crucially
B computes `F` from the **same** `home_extension_s·[holder_is_home]` recorded on
the item that A applied to itself — one function of one fact, so the two cannot
disagree about where the fence is.

*Reconciling the clocks.* By R4 both nodes are within `MAX_SKEW_S` of the
store's clock, so `|clock_A − clock_B| ≤ 2·MAX_SKEW_S`. B's start, expressed on
A's clock, is therefore `≥ F + grace_s − 2·MAX_SKEW_S`.

*Closing.* By R5,
`grace_s ≥ window_dur + FENCE_CHECK_S + STOP_LATENCY_S + 2·MAX_SKEW_S`, hence

```
B_start(on A's clock) ≥ F + grace_s − 2·MAX_SKEW_S
                      ≥ F + window_dur + FENCE_CHECK_S + STOP_LATENCY_S
                      ≥ A_last_order_dies
```

So B begins no earlier than A ends. ∎

**Case C — store outage.** No acquire can succeed, so no `Lₖ₊₁` exists and the
chain does not advance. The only node trading past `E` is A, and only if
`A = home(S)`, which is unique. Cardinality of the trader set is ≤ 1. ∎

**Case D — network partition (node ↔ store).** From the partitioned node's view
this is Case C. From the connected node's view it is Case B — and Case B's bound
already budgets the partitioned node's *full* fence `F`, including the home
extension, without needing to observe anything about it. ∎

**Case E — node ↔ node partition.** Vacuous: no node-to-node channel exists.
The design has no such path on purpose. ∎

**What the argument depends on**, stated plainly so it can be attacked:

1. DynamoDB conditional writes on one item are linearizable. *(AWS's documented
   guarantee.)*
2. `MAX_SKEW_S` genuinely bounds clock disagreement. *(Enforced, not assumed —
   §1.6 — and a node that cannot verify it removes itself.)*
3. `STOP_LATENCY_S = 30s` genuinely bounds cancel-to-quiet. *(Should be measured
   against the order-latency tape before phase 2 goes live. This is the weakest
   link and it is a measurement, not a proof.)*
4. A released lease really implies no live orders. *(Ordering discipline in the
   shutdown path — §1.7 — and the fallback is to not release.)*

The property is also driven directly in code:
`tests/test_fleet_lease.py::test_no_interleaving_ever_puts_two_nodes_on_one_book`
runs 25 seeded simulations of two nodes through random crashes, per-node
partitions, store outages and in-bound clock skew, asserting `len(live) ≤ 1` at
every tick — with a companion test proving the simulation is not vacuous.

## 4. The assignment map

`orchestrator/assignments.json`, version 1. Repo-resident so a change to who
owns what is a reviewable diff. Validated by `pmt-fleet map`.

```json
"btc-updown-5m": {
  "home": "desktop",
  "failover": "euw",
  "grace_s": 420,
  "home_extension_s": 600,
  "failover_priority": 1,
  "arm_template": {"size": 25, "clip": 10, "roll": true, "feed": "binance"}
}
```

`window_dur_s` is derived from the series key itself and only needs declaring
for a coarse key the slug parser cannot read; declaring one that contradicts the
key is refused.

**Refusals** (all fatal, exit 2): a grace below the floor; a failover equal to
its home; an undeclared node; a failover with no `arm_template`; a declared
duration contradicting the key; a series key that is a **prefix** of another
(the allowlist matches by prefix, so `btc-updown` silently claims
`btc-updown-15m-…` — a partition that does not partition).

**`arm_template` is deliberately not a copy of the home node's arm.** A box
covering for another box is the wrong moment to discover the size was tuned for
different capital. Shipped templates are `size 25 / clip 10` against home arms
of 100–400.

**`failover_priority` exists because of a hard capital constraint you should
know about.** The EU box holds ~182 pUSD against `PMENGINE_MAX_TOTAL_EXPOSURE=60`.
At template sizes it can meaningfully cover **two** of the desktop's seven
failover-eligible series, not seven. Without a priority order, which two it gets
is whichever the loop happened to reach first. Phase 2 claims in priority order
up to `nodes.euw.failover_budget_usdc`. Priorities as shipped: btc-5m, eth-5m,
sol-5m, xrp-5m, then the 15m tier.

`hype-updown-5m` has **no failover**: pilot2's live default names hype, and a
covering engine on a series the pilot may also trade is a third participant on
one book.

## 5. Why this store

The brief specified DynamoDB. **The account's own EC2 plan argued the opposite**
and specified S3 conditional writes for the analogous wallet-owner lease
(`pmt-alpha/docs/ec2-euw-plan.md`: *"S3 over DynamoDB: no table to manage, same
region, and the account has no DynamoDB footprint to grow"*). This is a genuine
reversal and it is recorded here rather than glossed.

The case for DynamoDB: purpose-built conditional writes with a clean
`ConditionExpression`, strongly-consistent reads, and single-digit-ms latency on
a path a trading node blocks on. S3 can do the same CAS via `If-Match` on PUT
(GA since late 2024), so the alternative is real, not theoretical.

Cost, honestly — the brief said "pennies", which is off by about an order of
magnitude:

```
~1M write request units/mo  @ $1.4256/M (eu-west-1)  = $1.43
~1M read  request units/mo  @ $0.2851/M              = $0.29
storage (tens of items)                              ~ $0.00
                                                     --------
                                                       ~$1.72/mo
```

Against a t4g.micro line of $6.72 and an all-in EU box of ~$14/mo. Under two
dollars, not pennies, and worth stating precisely because the number that gets
waved at is the number nobody notices tripling.

`FleetStore` is an interface with two implementations already (`FleetStore`,
`FakeStore`), so an S3 `If-Match` backend is a swap and not a rewrite. If you
would rather keep the DynamoDB footprint at zero, that is a contained change.

**Guards on the table:** `OnDemandThroughput` at 5 read / 5 write request units
(~12x measured need; ceiling ~$22/mo if something pegs it 24/7),
`DeletionProtectionEnabled`, and TTL on `ttl` for heartbeats only.

**TTL is garbage collection, never correctness.** DynamoDB deletes TTL'd items
"typically within 48 hours" — a sweep, not a clock. Every deadline in this
system is decided by reading `expires_at` and comparing to a skew-checked local
clock. **Leases carry no TTL at all**: a lease that vanished mid-flight would
read as unheld and be instantly claimable, which is precisely the interval grace
exists to prevent.

## 6. Where alerts go

mubs, not a new channel. Pages route to the `mubs-attention` Lambda as a
fire-and-forget invoke:

```json
{"action": "raise", "service": "pmt-fleet", "detail": "...", "detected_by": "pmt-fleet-doctor@<node>"}
{"action": "resolve", "service": "pmt-fleet"}
```

The invoke is issued directly rather than by importing `mubslib`: the EU box has
no mubs checkout and no Discord token, and the pmtrader venv should not grow a
dependency on another project's library to report its own health. Mute is
applied by the attention Lambda, not by callers, so routing through it respects
mute mode by construction — a muted page reroutes to `#muted-reports` rather
than being dropped. `--notify-cmd` is the adapter seam for anything else.

**Correctness never touches mubs.** The lease CAS and the heartbeats live in
`pmt-fleet`; trading safety must not couple to mubs availability. mubs is the
voice, the table is the substrate.

**No-page-on-intentional-off**, mirroring the mubs `worker_shutdown_clean`
convention exactly:

- a lease **released** gracefully, or a heartbeat carrying the `shutdown` stamp,
  is expected — a note in the status output, never a page;
- a lease that **expires un-released**, or a node dark with no stamp, pages;
- the stamp self-clears, because `put_heartbeat` replaces the row whole;
- a **missing** heartbeat reads as *not* clean — page rather than silently skip
  a real wedge;
- the "already paged" latch is set **only on a page that was accepted**.
  Latching a failed send is how a fleet goes quietly dark.

Two things need a decision on the mubs side before this goes loud, both listed
in the handover: `pmt-fleet` is not in `attention.SERVICES` (so the card renders
the generic label rather than a real why/steps), and it is not in `mute.EXEMPT`
(so a quiet evening reroutes it). There is also **no external API to post to the
`#status` board** — it is one message the gateway Lambda edits in place from
state it reads out of the mubs table. Getting fleet health onto it is a
mubs-side change (a `_board_fleet_line`), not something to bolt on by writing
rows into someone else's table.

## 7. Phase 1 — what is built

Everything here is **read-only with respect to trading**. No lease is taken, no
order is placed, no arm is touched.

| piece | where |
| --- | --- |
| lease protocol + grace arithmetic | `pmtrader/orchestrator/lease.py` |
| store (DynamoDB + faithful fake) | `pmtrader/orchestrator/store.py` |
| clock-offset measurement | `pmtrader/orchestrator/clock.py` |
| assignment map + validation | `pmtrader/orchestrator/assign.py` |
| heartbeat construction | `pmtrader/orchestrator/heartbeat.py` |
| checker | `pmtrader/orchestrator/check.py` |
| mubs / `--notify-cmd` adapter | `pmtrader/orchestrator/notify.py` |
| table creation + cost guards | `pmtrader/orchestrator/table.py` |
| CLI | `pmtrader/orchestrator/cli.py` |
| unit (proposal, NOT installed) | `deploy/systemd/pmt-fleet-doctor.service` |

```sh
pmt-fleet map                      # print and validate the assignment map
pmt-fleet table --dry-run          # the CreateTable spec
pmt-fleet beat --once              # one heartbeat
pmt-fleet check                    # fleet status; exit 1 if it needs attention
pmt-fleet kill-switch --on         # freeze all failover claims
```

Exit codes: `0` nothing needs attention · `1` something does · `2` config
refused · `3` store unreachable (the doctor is blind — deliberately not the same
code as a sick fleet).

**Note on the brief:** it says the checker is "suitable for the existing monitor
to call". There is no monitor in this repo. The one referenced in
`pmt-alpha/analysis/watch_load.md` is external and documented only by its
output. The checker's exit-code contract is designed to be callable by whatever
ends up filling that role.

## 8. Phase 2 — specification only, not built

### 8.1 How a lease becomes a refusal the engine already enforces

The enforcement point exists. `PMENGINE_SERIES_ALLOWLIST` is read once at
`StrategyRuntime` construction (`series_guard.rs::SeriesAllowlist::from_env`),
refuses arms outside the list, and — critically — is re-checked *inside* `updown`
for rolls and recovered arms, because a roll chain re-arms itself without
passing through the control plane. The lease layer's job is to supply that list
**dynamically** instead of from the environment at startup.

Three options, in increasing order of invasiveness:

- **(a) Env + restart.** The claim daemon rewrites `engine.env` and restarts the
  engine at a window boundary. Zero engine changes; costs a restart per
  handover, which `ship-eu.sh --restart` already demonstrates is survivable.
  **Recommended for the first cut.**
- **(b) Control-plane setter.** Add `POST /series-allowlist` so the runtime's
  `series` field can be replaced live. Small, but it must also reach `updown`'s
  in-strategy check, and it introduces a way to *widen* the partition at runtime
  — which wants its own guard.
- **(c) Engine reads the lease itself.** Rejected. It puts an AWS SDK and a
  network dependency inside the hot path of a 50ms-tick trading engine, and it
  makes the store's latency a trading latency.

Whichever is chosen, **the allowlist stays the enforcement point.** The lease
layer must never become a second, parallel authority over what may trade — it
computes the list; `series_guard` enforces it.

### 8.2 The claim daemon

An extra mode on the same daemon (`pmt-fleet claim`), never a separate service:

```
every RENEW_INTERVAL_S (30s):
  offset := store.clock_offset_s;  if not skew_ok(offset): fence everything, stop
  for each series I hold:
      renew (CAS holder=me AND epoch=mine)
      on CASFailed  -> I am not the holder; fence immediately
      on Unavailable-> keep holding to fence(); home series ride the extension
  for each series I am home for and do not hold:
      acquire (priority order)
  if failover not disabled and I have budget left:
      for each failover series, in failover_priority order, while budget remains:
          acquire if can_acquire()
  reconcile: arms should equal leases held
      lease held, not armed  -> arm from arm_template
      armed, no lease        -> DISARM FIRST, always, before anything else
```

Disarm-before-arm is not an implementation detail; it is the ordering that keeps
R1 true during reconciliation.

On SIGTERM: stop claiming, wait for the engine to confirm shutdown, then release
every lease held. If the engine cannot be confirmed stopped, **do not release** —
let it lapse.

### 8.3 Rollout order

1. **Create the table, run the doctor on the desktop only.** Operator
   credentials, no IAM change, no EU exposure. Watch `check` for a week; confirm
   the clean-shutdown path stays quiet through nightly poweroffs.
2. **Approve and attach the EU IAM policy; run the doctor on the EU box.** Now
   both nodes beat. Still zero trading changes. This is the step that proves the
   clock bound and the store's reachability from both boxes.
3. **Measure `STOP_LATENCY_S`** against `order-latency-tape.jsonl` and, if it is
   worse than 30s at p99, raise it and re-derive the grace floors. Dependency 3
   of the proof is the one that is a measurement rather than an argument.
4. **Add mubs `SERVICES` entry + decide `mute.EXEMPT`.** Turn `--notify mubs` on
   for the checker. Alerts before autonomy.
5. **Claim daemon in shadow.** It computes what it *would* claim and logs it,
   claiming nothing. Compare against reality for a week.
6. **Leases live, arming still manual.** Nodes acquire, renew and release real
   leases; the reconcile step logs instead of arming. This exercises the whole
   protocol with zero order-path consequence, and it is the step where the
   nightly poweroff's instant handover can be observed end to end.
7. **One series live.** `bnb-updown-5m`, home euw, failover desktop —
   the smallest blast radius, the desktop has capital to spare, and it is the
   direction you named first.
8. **The rest, in `failover_priority` order**, one at a time.

Steps 1–2 are additive infrastructure. Step 6 is the first one that can stop
trading. Step 7 is the first one that can start it.
