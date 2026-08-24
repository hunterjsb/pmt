# Where the EU box's order latency actually goes

Measured 2026-08-24 against the live `order-latency-tape.jsonl` on both nodes.
Read-only: no orders were placed for this, and no auth headers were printed.

The question was "the EU box acks at p50 166ms but `curl https://clob.polymarket.com/ok`
from the same box returns in 36ms — where is the missing 130ms?"

**It is the CLOB's matching engine. It is not ours and it is not removable.**

## The measurement that settles it

`post_only` orders and taker orders are the SAME request: same POST to
`/order`, same client, same connection, same L2 headers, same signed payload
shape. The only difference is what the exchange does after it reads them — a
post-only order is booked, a taker order is matched. So the difference in ack
time is the matching engine, with transport and signing cancelled out.

| node    | post_only (booked) | taker (matched) | **difference** |
|---------|-------------------:|----------------:|---------------:|
| EU      | p50 **30.68**ms (n=48)  | p50 **155.43**ms (n=68)  | **124.75ms** |
| desktop | p50 **153.42**ms (n=451)| p50 **284.01**ms (n=1011)| **130.59ms** |

The two nodes sit on different continents with a 122.74ms RTT gap between them
(153.42 − 30.68, read off the booked path, which is pure transport). Yet the
matching cost is the same ~125–131ms on both. **A cost that does not move when
the network moves is server-side.** That is the whole proof.

## Full decomposition of the EU taker path (p50 155ms)

| stage                       | cost      | whose |
|-----------------------------|----------:|-------|
| build + EIP-712 sign        | 0.28 ms   | ours  |
| L2 headers + serialize      | 0.03 ms   | ours  |
| transport (warm h2, EU→CLOB)| ~30 ms    | physics |
| CLOB matching engine        | ~125 ms   | theirs |

`/ok` was a misleading baseline: it is a 4-byte GET that does no order work.
The correct transport baseline is a POST `/order` the exchange *books* without
matching — that is the post_only path, and it costs 30.68ms, which agrees with
`curl` (`/ok` 36ms cold, 19–22ms warm; `/book` 24–28ms warm). Transport was
never unaccounted for.

## Transport facts (EU box, `curl -w`)

- `clob.polymarket.com` is Cloudflare, edge `DUB` (Dublin). TCP connect
  **1.6–1.9ms**, TLS **16ms**, so a cold handshake is **~18ms**.
- HTTP/2 is already negotiated end to end. There is no h2 win left to take.
- Warm keep-alive `/ok` TTFB is 17–22ms vs 34–44ms cold — the handshake is
  real and worth never paying.

## What was actually ours (and is now fixed)

p50 held nothing. Both findings are tails:

1. **Cold-connection risk.** Ack time is flat against idle gap today (EU
   post_only: `<1s` p50 30.26 vs `>300s` p50 30.77) — the pool never goes cold
   only because the REST book poller runs every 10s, under reqwest's 90s
   `pool_idle_timeout`. That is an accident of `PMENGINE_BOOK_POLL_SLOW_MS`,
   not a property of the order path. Pinned: explicit `tcp_nodelay`, a 300s
   pool idle timeout, and HTTP/2 keep-alive pings so a half-dead connection is
   found by a ping instead of by a live order.
   Evidence for the pings: 2.66% of desktop post_only acks exceed 2× median on
   a path whose floor is 141ms, worst 4166ms.

2. **Cold token metadata inside `sign`.** `sign_done_ms` is p50 0.16ms but
   20 of 1462 desktop orders paid 100–175ms there. **20 of those 21 slow signs
   were the first order on their token** — 3.9% of first-on-token fires meet a
   `prewarm_token` that failed or lost the race, and pay the tick-size /
   neg-risk round trip on the order path. Prewarm now retries.

## What the fix measured (desktop, 2026-08-24)

`pmengine/tests/order_transport_live.rs`, legacy client (what shipped) vs
current, both against `clob.polymarket.com/ok`.

Warm loop, N=50 — deliberately unchanged, this is the control:

| client  | p50       | p90       |
|---------|----------:|----------:|
| legacy  | 114.45ms  | 117.00ms  |
| current | 114.74ms  | 115.80ms  |

Idle gap, one request after the pool has gone quiet — the shape of a window's
first fire. Three runs:

| idle  | legacy   | current  |
|-------|---------:|---------:|
| 100s  | +28.55ms | −0.29ms  |
| 100s  | +45.39ms | −1.56ms  |
| 280s  | +48.29ms | +2.20ms  |

The legacy client pays 28–48ms to rebuild TCP+TLS; the current one pays
nothing because it still holds the connection. The 280s run is also the
keep-alive safety check: ~9 pings at 30s drew no `GOAWAY` and no
`ENHANCE_YOUR_CALM` from Cloudflare's edge, and the connection was still
reused at the end.

The desktop handshake is dearer than the EU box's (~18ms there, measured by
`curl`), so the EU saving is the smaller number — but it lands on the first
fire of every window that follows a quiet stretch, which is the fire that
matters most.

## Verdict

166ms is near the floor. About 125ms of it is Polymarket's matching engine,
proven invariant across two continents. Roughly 30ms is transport that is
already optimal (Cloudflare's Dublin edge, warm HTTP/2). Signing is 0.3ms.
**There is no p50 win left in the EU order path** — the only remaining lever
on ack time is order semantics (a booked post-only quote acks 125ms faster
than a matched taker order), which is a strategy decision, not a transport one.
