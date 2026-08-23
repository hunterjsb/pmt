# Control-plane load audit — is `pmt crypto watch` too heavy for the engine?

Measured 2026-08-23 ~19:35–19:50Z against the live engine (pid 2046609,
`127.0.0.1:7531`, `PMENGINE_TICK_INTERVAL_MS=50`, 8 arms / 16 subscribed
tokens), plus a forensic read of `~/.pmt/engine/engine-systemd.log` covering
the two instances that ran 17:31:11Z–19:34:40Z and 19:35:14Z onward.

Read-only endpoints only. No mutation was issued during this audit.

---

## Verdict, up front

**Watch is not the problem.** Its whole control-plane footprint is **30
requests/min carrying ~4.0 KB each (~121 KB/min)**, and each request costs the
engine's event loop **~0.69 ms** — about **0.03 % of wall clock**. It cannot
delay a tick, an order or a roll by any amount that matters.

**The engine's own tick arm is the problem.** The periodic position reconcile
(`engine.rs:964`) issues **16 sequential full-account HTTP fetches** — the same
URL, 30 KB each, 16 times — inline in the `tokio::select!` arm that also serves
the control plane. That is ~8.7 req/s and ~15.7 MB/min against
`data-api.polymarket.com`, sustained, to read 16 numbers that are all present in
the *first* response. While a pass runs, the control plane is **completely
dark**. Watch's only real contribution to the 19:23:44Z event was *indirect*:
its 10-second scoreboard is a 6-page / 2.41 MB wallet walk against the same
upstream, and upstream contention is what stretched a normally-330 ms reconcile
pass into the 9.6-second blackout the monitor fell into.

---

## 1. Consumer map

### 1.1 The control plane is a single-consumer queue behind the trading loop

`control.rs` is a thin axum server. Handlers never touch engine state; they
push a typed `EngineCommand` onto a bounded mpsc (capacity **64**,
`engine.rs:431`) and block on a oneshot reply. The engine receives it in the
`Some(cmd) = cmd_rx.recv()` arm of its main `tokio::select!`
(`engine.rs:1456`) — **the same `select!` that owns the tick timer**
(`engine.rs:942`).

`select!` resolves to exactly one ready branch and then runs that branch's body
to completion before re-entering the select. `.await` points inside a branch
yield to the *executor* — other spawned tasks (WS feed, REST book poller,
balance poller, trades poller) keep running — but the select's **other branches
are not re-polled**. So:

- the tick arm and the command arm are **strictly mutually exclusive**;
- a command's cost delays the next tick by exactly the command's own duration;
- a tick arm that blocks for N seconds makes the control plane unreachable for
  N seconds, with no queue drain and **no timeout anywhere in the handler**
  (`control.rs:359` etc. `rx.await` unbounded) — the client's own timeout is
  the only bound.

There is **no lock shared with the eval path**; the `select!` is the
synchronization point, exactly as `control.rs:9-16` claims. The one lock the
status reply does take is each arm's `Arc<Mutex<OracleState>>`
(`updown.rs:1984`), and the oracle poller only takes it *after* its RPC
returns (`updown_oracle.rs:271`), so it is held for microseconds and is not a
contention hazard. The oracle poller also ships dark by default.

### 1.2 Who calls what

| Consumer | Endpoint | Cadence | Wire bytes | Cost to the loop |
|---|---|---|---|---|
| `crypto watch` worker | `POST /strategies/updown/command {"action":"status"}` (`cli_crypto_watch.py:137`) | **2.0 s** (`ENGINE_EVERY_S`) | **4031 B** measured (8 arms) | ~0.69 ms |
| `crypto watch` worker | scoreboard (`cli_crypto_stats._tape_scoreboard`) | 10.0 s | — | **zero** — data-api, not the engine |
| `crypto watch` worker | USDC balance | 60.0 s | — | **zero** — not the engine |
| `crypto watch` main loop | tape file seek+read | 1.0 s | — | **zero** — local file |
| `crypto stats` | same `{"action":"status"}` POST (`cli_crypto_stats.py:531`) | **once per run** | 4031 B | ~0.69 ms, once |
| `crypto trigger` / `arm` / `disarm` | `{"action":"status"}` and mutations (`cli_crypto_arm.py:202-256`) | operator-driven | 4031 B | ~0.69 ms, once |
| health monitor | `GET /status` (or `pmt engine status`, `cli.py:1294`) | unknown, ≥3 retries | **499 B** | ~0.69 ms |
| `pmt orders/subs/trades/alerts` | assorted GETs (`cli.py:1412-1570`) | operator-driven | 566–2100 B | ~0.69 ms |
| **engine itself** | `PlaceOrder` / `CancelOrderById` from `pmt buy/sell` | operator-driven | — | **hundreds of ms** — these make CLOB round-trips *inside the command arm* (`engine.rs:1580`, `:1593`) |

`watch_ui.py` is pure render — no network at all. Confirmed by inspection.

Client-side timeouts (`pmtrader/engine.py`): `get()` **5 s**, `post()` **10 s**,
`notify()` **3 s**. A 9.6 s dark window fails all three.

---

## 2. Measurements

### 2.1 Payload sizes (live, 8 arms / 16 tokens)

| Endpoint | Bytes |
|---|---|
| `POST /strategies/updown/command {"action":"status"}` | **4021–4037 B** (~503 B/arm) |
| `GET /status` | 499 B |
| `GET /strategies` | 1405 B |
| `GET /subscriptions` | 1282 B |
| `GET /orders` | 566 B |
| `GET /orders/all` | 2100 B |
| `GET /alerts` | 2 B |

The observed "~10 KB strategy reply" is **~2.4× high**. Measured on the wire it
is 4.0 KB at 8 arms; the *log line* it produces is bigger (p50 4624 B, max
6922 B over 2354 samples) because tracing prefixes it. It would reach 10 KB at
roughly 20 arms.

### 2.2 Request cost to the loop

Six spaced `{"action":"status"}` POSTs against an idle-ish loop:

```
0.690 ms  0.727 ms  92.212 ms  0.516 ms  0.691 ms  0.656 ms
```

Five of six complete in **0.52–0.73 ms end-to-end** — HTTP parse, channel hop,
JSON build over 8 arms, the 4.2 KB INFO log write, and the reply. The 92 ms
outlier is queueing, not work. **~0.69 ms is the true cost of one watch poll to
the trading path.**

### 2.3 Control-plane availability — the number that actually matters

`GET /subscriptions` is a bare `Vec<String>` clone, so its latency **is** the
wait for the loop, with the reply build subtracted out.

**120 samples @ ~1.15 s spacing:**

```
min 0.3 ms   p50 1.2 ms   p75 175 ms   p90 353 ms   p95 462 ms   p99 833 ms   max 4865 ms
```

**140 samples @ 5 Hz for 30 s (duty cycle):**

```
p50 1.4 ms   p75 1.7 ms   p90 246 ms   p99 442 ms   max 455 ms
14.3 % of samples > 100 ms;  0 % > 500 ms
dark runs: never two consecutive 200 ms samples  →  blackouts are < 400 ms
loop unavailable ~20 % of wall clock, in ~250–450 ms blocks
```

Read that carefully: the *median* reply is **1.2 ms** and the *p90* is **353
ms**. The distribution is bimodal — the loop is either instantly free or
blocked in a multi-hundred-millisecond block. Payload size is irrelevant; the 2
B `/alerts` reply took 147–319 ms in the same window that the 4031 B status
POST took 0.5 ms.

### 2.4 What is blocking it: the position reconcile

`engine.rs:964-976`:

```rust
if tick_count.is_multiple_of(30) {
    for token_id in self.subscribed_tokens.clone() {
        if let Ok(Some((size, avg))) = self.client.get_position(&token_id).await {
```

and `client.rs:717-757` — `get_position` fetches
`data-api.polymarket.com/positions?user={funder}&sizeThreshold=0`, the **entire
account**, then scans the array client-side for one asset. It is **not cached
and not batched**, so 16 subscribed tokens = **16 identical 30 KB fetches**,
serially, inside the select arm.

Measured cost of that URL: **29,960 B**, 55–334 ms per call over three cold
samples (the engine's pooled/keep-alive client gets ~21 ms — derived below).

`tokio::time::interval` defaults to `MissedTickBehavior::Burst`, so a blocked
arm does not lose ticks — the timer fires the backlog immediately afterwards.
That preserves the long-run average and makes tick slippage directly
measurable from the 600-tick heartbeats:

| Instance | Ticks | Wall | Effective | vs 50 ms scheduled |
|---|---|---|---|---|
| 17:31:11Z–19:34:40Z (**the one that went dark**) | 120,600 | 7375 s | **61.2 ms/tick** | **+22 % behind** |
| 19:35:14Z onward (current) | 9,600 | 480 s | 50.0 ms/tick | on schedule |

The pre-restart engine lost **1345 s of wall clock in ~2 h** to blocking work
inside `select!` arms. Spread over its 4020 reconcile passes
(120,600 / 30) that is **~330 ms per pass ≈ 21 ms per `get_position`** — which
matches the 5 Hz duty-cycle probe exactly (14 % of samples slow, blocks capped
at ~450 ms, never two consecutive).

Volume that implies for the pre-restart instance:

> **64,320 `/positions` calls in 2 hours — 8.7 req/s sustained, ~1.93 GB of
> JSON downloaded — to read 16 numbers that were all in the first response.**

Two other in-arm blockers show up in the same log, both CLOB round-trips inside
the tick arm (`engine.rs:1173`, `:1292`): **1176** `Orders cancelled` calls and
**3514** `Stale order refused cancellation` warns, arriving in storms (10+
cancels in 3 s at 19:24:40–43, each ~230–400 ms, all failing on the same two
stale order ids). That is a second, independent bug worth its own ticket.

### 2.5 The engine log is 83 % watch's own polls echoed back

`engine.rs:1636-1642` logs **the entire strategy reply** at INFO on every
command:

```rust
EngineCommand::StrategyCommand { id, body, reply } => {
    let res = self.strategy_runtime.command(&id, &body);
    if let Ok(ref v) = res {
        tracing::info!(strategy_id = %id, reply = %v, "Strategy command handled");
```

`%v` on a `serde_json::Value` is a full compact serialization. Measured over
the live log:

```
2354 such lines, 10.90 MB — 83.3 % of the 12.89 MB engine log
p50 4624 B/line, max 6922 B
```

At watch's 2 s cadence that is **~126 KB/min → 7.6 MB/h → 181 MB/day** of log
written for no reason other than that a dashboard asked for a status. The
subscriber is a plain `FmtSubscriber` (`main.rs:191`) — a synchronous,
mutex-guarded stdout writer on the loop's own thread. Cheap today (tens of µs,
already inside the 0.69 ms measurement) but it is real I/O on the trading
thread and it is the reason `engine.log` needs rotating at all.

### 2.6 Requests/min, and what overlap actually does

**Control plane, watch alone:**

| | req/min | bytes/min | loop time/min |
|---|---|---|---|
| watch status poll (2 s) | **30** | **~121 KB** | **~21 ms (0.03 %)** |

**Control plane, watch + a stats run + a 1 Hz monitor, all overlapping:**

| | req/min | bytes/min | loop time/min |
|---|---|---|---|
| watch | 30 | 121 KB | 21 ms |
| stats (one run) | 1 | 4 KB | 0.7 ms |
| monitor @ 1 Hz | 60 | 30 KB | 41 ms |
| **total** | **91** | **~155 KB** | **~63 ms — 0.1 % of wall clock** |

Nothing queues. The mpsc holds 64; peak depth here is 1–2. The p99 latency
those consumers *see* is 833 ms, and every millisecond of it is the reconcile,
not each other.

**The shared upstream — where the real overlap lives.** All of these hit
`data-api.polymarket.com` from one IP:

| | req/min | bytes/min |
|---|---|---|
| **engine position reconcile** | **~520** (8.7/s) | **~15.7 MB** |
| watch scoreboard (6-page walk / 10 s, measured 2.41 MB / 2880 rows / 0.50 s) | **36** | **~14.5 MB** |
| one `crypto stats` run | 6 | 2.41 MB |
| `crypto outcomes` refresh | bursty | — |

The engine is its own worst customer by request count, but watch's scoreboard
adds **+7 % request volume and roughly doubles the byte volume**, and it does
so as a **6-page back-to-back burst every 10 s** — the shape most likely to
trip a burst limiter. CLAUDE.md itself puts the account-wide budget at "~5
req/sec"; the reconcile alone is 8.7.

Note also that watch's scoreboard walk goes back to the **beginning of wallet
history** every 10 s (`_tape_scoreboard(0.0, ...)`, `cli_crypto_watch.py:145`),
so its page count grows without bound as the wallet accrues rows — 6 pages
today, more every week. It also re-parses the 19.3 MB / 58k-line
`updown-tape.jsonl` on each refresh (local CPU only; the watch process sits at
~11 % CPU).

---

## 3. The 19:23:44Z blackout, explained

The monitor logged `ENGINE CONTROL PLANE UNREACHABLE 3x 19:23:44Z`. The
engine's own log shows why. Gaps between consecutive `Strategy command handled`
lines — i.e. the intervals during which the loop served *no* command at all —
around that moment:

```
19:22:50.443 → 19:22:54.978   4.53 s
19:22:54.978 → 19:23:03.758   8.78 s
19:23:03.758 → 19:23:12.474   8.72 s
19:23:12.474 → 19:23:16.952   4.48 s
19:23:16.952 → 19:23:26.894   9.94 s
19:23:26.894 → 19:23:35.927   9.03 s
19:23:35.927 → 19:23:40.320   4.39 s
19:23:40.320 → 19:23:49.936   9.62 s   ←  19:23:44Z lands HERE
19:23:49.936 → 19:23:59.303   9.37 s
```

A clean ~9 s / ~4.5 s sawtooth. Across that whole instance:

```
n=2353 gaps   p50 2.07 s   p90 8.52 s   p95 9.24 s   p99 11.83 s   max 47.6 s
272 gaps > 5 s      28 > 10 s      8 > 20 s
```

Watch asks every 2 s but the engine answered only **18.7×/min** — it was
serving barely 62 % of the polls it was offered. (Post-restart, on the same 2 s
cadence: **27.5×/min**, p50 gap 2.01 s, p90 3.00 s.)

**The chain:**

1. 16 subscribed tokens → each reconcile pass = 16 serial 30 KB `/positions`
   fetches inside the tick arm. Nominally ~330 ms.
2. Concurrent wallet walks — watch's 10 s scoreboard, plus the operator's
   `crypto stats` run, plus an `outcomes` refresh — hammered the same
   `data-api` host from the same IP, pushing per-call latency from ~21 ms to
   ~0.55 s.
3. A pass therefore stretched to 16 × 0.55 s ≈ **8.8 s**, matching the observed
   9.0–9.9 s gaps.
4. For that whole stretch `cmd_rx` was never polled. The monitor's request —
   5 s timeout on `GET /status`, 10 s on a POST — landed inside the
   19:23:40.320 → 19:23:49.936 window and timed out, three times.
5. The engine was **entirely healthy**: it fired a clip at 19:23:49.382 and
   placed the order at 19:23:49.936 — the same instant the reconcile released
   the loop and the queued status command was finally served. That 5 s-later
   "full strategy reply" in the logs is the *end* of the blackout, not evidence
   against it.

So: **saturated and serialized, not down** — and the thing doing the
serializing was the engine's own reconcile, amplified by upstream contention
that watch's scoreboard participates in.

One aggravating factor visible in the same window: the cancel storm
(§2.4) — 3514 stale-cancel failures, each a CLOB round-trip in the tick arm —
was running through 19:24:31–19:24:43 and produced the 17.27 s gap at
19:24:57.758.

---

## 4. Verdict

**Is watch's load material to the trading path?** **No.** 30 req/min × 0.69 ms
= 21 ms of loop time per minute, 0.03 % of wall clock. It cannot meaningfully
delay a tick (50 ms budget), an order, or a roll. Even watch + stats + a 1 Hz
monitor together are 0.1 %.

**Is it material to the control plane's own responsiveness?** **Also no —
watch is the victim, not the cause.** Control-plane latency is bimodal (p50
1.2 ms, p90 353 ms, p99 833 ms) and every millisecond of the tail is the tick
arm blocking on serial HTTP. Payload size and request rate are not in the
picture: a 2 B reply and a 4031 B reply queue identically.

**Is watch material *anywhere*?** Yes, in two places, neither of which is the
control plane:

1. **Shared data-api budget** — the 10 s full-history wallet walk is a 6-page,
   2.41 MB burst that contends with the engine's reconcile on the same host and
   IP, and it grows unboundedly with wallet history. This is the one real
   coupling between watch and the 19:23:44Z event, and it is *indirect*.
2. **Engine log volume** — 126 KB/min, 181 MB/day, 83 % of the log file,
   written synchronously on the trading thread, purely to echo watch's polls
   back.

---

## 5. Ranked recommendations

Recommendations only — nothing here is implemented, and none of it touches
watch/stats code that is currently being rewritten.

### Tier 1 — fix the actual blocker (engine side, high value, low risk)

**R1. Make the position reconcile one fetch, not N.** `get_position` already
downloads the whole account (`client.rs:724`). Add a `get_all_positions()` that
fetches once and returns a map, and have the reconcile loop read from it.
*Effect:* 16 calls → 1 per pass; ~330 ms of blocking → ~21 ms; data-api load
from 8.7 req/s → 0.55 req/s and from 15.7 MB/min → 1.0 MB/min. This single
change removes ~94 % of the control plane's tail latency and ~20 % of the tick
slippage, and it makes the 19:23-class blackout arithmetically impossible
(one 0.55 s call cannot produce a 9 s gap). **Do this one first; everything
below is secondary.**

**R2. Move the reconcile off the select arm entirely.** Spawn it as a
background task that writes into a shared snapshot the tick reads, the way the
balance and trades pollers already work (`engine.rs:636`, `:682`). Then no
amount of upstream slowness can darken the control plane. Slightly more work
than R1 and the two compose; R1 alone is probably enough.

**R3. Reconcile on wall-clock, not tick count.** `tick_count.is_multiple_of(30)`
means the cadence silently changes 20× if anyone passes `--tick-ms`, and
`MissedTickBehavior::Burst` makes a slow pass immediately eligible for the next
one. An `interval(Duration::from_secs(5))` branch says what it means and cannot
run away.

### Tier 2 — cheap, obvious, do them alongside

**R4. Stop logging the whole strategy reply at INFO** (`engine.rs:1639`). Log
the arm count and a hash, or demote the body to DEBUG/TRACE. Recovers 83 % of
the engine log — 181 MB/day — and takes a synchronous multi-KB write off the
trading thread on every dashboard poll. One line of code, zero risk.

**R5. Bound the handler waits.** `rx.await` in `control.rs` has no timeout, so
a wedged loop hangs every caller until *their* client gives up. A
`tokio::time::timeout(2s, rx)` returning 503 turns "unreachable" into a fast,
honest answer — and would have made the monitor's alert accurate rather than
ambiguous. Pair with monitor-side backoff so 3 retries do not all land inside
the same blackout.

### Tier 3 — watch/stats side, worth doing but not urgent

**R6. Slow or shrink the scoreboard walk.** Watch re-walks the *entire* wallet
history every 10 s (2.41 MB, 6 pages, growing forever). Options, in order of
preference: (a) walk to a bounded floor for the live view and full-walk only
on demand; (b) raise `SB_EVERY_S` from 10 s to 30 s — a 3× cut in data-api
burst pressure for a dashboard number that changes on window boundaries
anyway; (c) leave the walk alone but jitter it so it never lands in phase with
the reconcile. Do **not** reintroduce an incremental wallet cache —
`polymarket/wallet.py:70-89` is the autopsy of why that failed five times.

**R7. Add an engine-side `/strategies/{id}/summary`** returning arm count,
states and committed-$ without the per-arm `eval`/`oracle` blocks. Watch polls
that at 2 s and the full status only when the operator opens a detail view.
This is the "cheaper endpoint" idea and it is **last on purpose**: at 0.69 ms
and 4 KB per call, the payload is not costing anything today. Only worth it
once the arm count is large enough that 4 KB becomes 40 KB.

**R8. Do *not* cache the strategy reply between renders.** Watch already
separates fetch from render on its own thread (`cli_crypto_watch.py:116-190`)
and renders from a snapshot, so a cache would buy no render latency and would
put a stale committed-$ figure on a trading dashboard — the exact failure
`_status_failed` (`:153`) was written to avoid.

---

## Appendix — reproducing the measurements

```bash
# payload sizes + per-request cost (read-only)
curl -s -o /dev/null -w "%{size_download} %{time_total}\n" http://127.0.0.1:7531/status
curl -s -o /dev/null -w "%{size_download} %{time_total}\n" \
  -H 'content-type: application/json' -d '{"action":"status"}' \
  http://127.0.0.1:7531/strategies/updown/command

# loop occupancy — /subscriptions is a bare Vec<String> clone, so latency IS the wait
for i in $(seq 1 120); do
  curl -s -o /dev/null -w "%{time_total}\n" http://127.0.0.1:7531/subscriptions
  sleep 0.85
done

# tick slippage, from the 600-tick heartbeats
grep -a "Tick tick=" ~/.pmt/engine/engine-systemd.log

# control-plane dark windows
grep -a "Strategy command handled" ~/.pmt/engine/engine-systemd.log   # diff the timestamps

# log amplification
grep -a "Strategy command handled" ~/.pmt/engine/engine-systemd.log \
  | awk '{n++; b+=length($0)+1} END {print n, b, b/n}'
```

Cited code: `pmengine/src/engine.rs` (431, 942, 964-976, 1173, 1292, 1456,
1580, 1593, 1636-1642), `pmengine/src/control.rs` (9-16, 295-349, 359),
`pmengine/src/client.rs` (717-757), `pmengine/src/main.rs` (191),
`pmengine/src/strategies/private/updown.rs` (1979-2007),
`pmtrader/cli_crypto_watch.py` (50-53, 116-190), `pmtrader/cli_crypto_stats.py`
(531), `pmtrader/engine.py` (43, 59, 78), `pmtrader/polymarket/wallet.py`
(53-114).
