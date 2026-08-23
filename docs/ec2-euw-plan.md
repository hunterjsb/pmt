# Running pmengine on EC2 in eu-west-1 — without pmproxy, with websockets

Operator's question, verbatim: *"what would it take to run the engine on an EC2
in euw without the proxy and with websockets."*

Answer: about **$13/month**, a handful of env-var changes, two config constants
that are currently hardcoded, and one piece of engineering that is not
optional — an interlock so the desktop engine and the EC2 engine can never both
be armed on the same wallet.

Everything numeric below was measured on 2026-08-23, not estimated. A t3.micro
was launched in eu-west-1a for 4m29s purely to run the probes, and terminated.

---

## 1. The evidence

### Method

`analysis/net_probe.py` was run **simultaneously** from both locations — 20
passes, 3s apart, same script (sha256 `826c22f0…`), same endpoints, same
wall-clock window — so the two columns are not separated by market conditions.
Each pass does DNS → TCP → TLS → one cold HTTP → three warm HTTP on the pooled
connection; websockets get TCP+TLS+the Upgrade round trip up to the 101.

`http_warm` is the number that matters. pmengine keeps a connection pool, so a
steady-state order pays warm-HTTP-on-an-open-socket, not the setup cost.
Conflating the two is how a 20ms order path gets described as 250ms.

**Probe host:** `i-0bae1e59a226d2bdd`, t3.micro, eu-west-1a, public IP
`54.78.128.11`, IMDSv2 required, default VPC SG (no internet inbound), no key
pair, no IAM role. Results returned over the serial console. Terminated;
verified below.

### The verdict table

RTT in ms. `warm` = warm HTTP round trip; `ws` = handshake to the 101.
"Desktop" is the residential US box (Cloudflare colo **ATL**); "EC2" is
eu-west-1a (Cloudflare colo **DUB**).

| Endpoint | Stage | Desktop p50 | Desktop p95¹ | EC2 p50 | EC2 p95¹ | Δ | Status |
|---|---|---|---|---|---|---|---|
| `clob.polymarket.com` /book | **warm** | **121.0** | 124.4 | **18.8** | 20.9 | **−102.2** | 404² both |
| `clob.polymarket.com` /time | **warm** | **117.6** | 122.9 | **17.9** | 20.8 | **−99.7** | 200 both |
| `clob…` — via pmproxy Lambda | **warm** | **148.4** | 162.9 | n/a | | **−129.6** vs EC2 | 200 both |
| `gamma-api.polymarket.com` | warm | 26.0 | 26.5 | 7.1 | 9.4 | −18.9 | 200 both |
| `data-api.polymarket.com` | warm | 26.0 | 28.9 | 7.4 | 9.6 | −18.6 | 200 both |
| `wss://ws-subscriptions-clob…/ws/market` | ws | **225.6** | 229.4 | **33.0** | 41.3 | **−192.6** | **101 both** |
| `wss://ws-live-data.polymarket.com` (RTDS) | ws | **348.5** | 355.6 | **69.0** | 77.2 | **−279.5** | **101 both** |
| `api.binance.com` (the real one) | warm | 10.0³ | 11.0 | 240.5 | 244.8 | — | **451 desktop / 200 EC2** |
| `wss://stream.binance.com:9443` | ws | 184.1³ | 187.5 | 237.5 | 240.7 | — | **451 desktop / 101 EC2** |
| `data-api.binance.vision` (mirror) | warm | 183.8 | 187.0 | 238.6 | 242.4 | **+54.8** | 200 both |
| `wss://data-stream.binance.vision` | ws | 188.2 | 190.9 | 237.3 | 243.1 | **+49.1** | 101 both |

¹ the probe reports p90 and max; the p90 column is shown. n=20 per cell, sd ≤ 8ms
on every Polymarket row, so p90 and p95 are within noise of each other here.
² the sample token is a settled 5m window — a 404 is still a complete round trip,
so the timing is valid; `/time` is the 200 control and agrees to 1ms.
³ the desktop's fast `api.binance.com` numbers are a CloudFront edge returning
the 451 locally. Fast, and useless.

### Four things this proves

**1. The CLOB origin is in or beside eu-west-1.** From Dublin, TCP to Cloudflare
is 1.4ms and warm HTTP is 18.8ms; from Atlanta, TCP is 19.8ms and warm HTTP is
121.0ms. TCP terminates at the anycast edge in both cases, so the ~100ms spread
is edge→origin, and it collapses to near nothing from Ireland. This corroborates
the third-party "sub-ms RTT to Polymarket from Dublin" report in issue #4 — that
claim is about the edge (we measure 1.4ms TCP); the origin is ~17ms behind it.

**2. Today's live order path is the slowest of the three available.** The engine
runs with `PMPROXY_URL` set, so orders go desktop → Lambda (eu-west-1) → CLOB at
**148.4ms**. Direct from the desktop is 116.6ms. Direct from EC2 is 17.9ms. The
proxy is currently costing **+31.8ms against** the desktop's own direct path — it
is not buying latency, it is buying a non-US egress IP.

**3. Binance blocks the desktop, not AWS eu-west-1 — empirically.**
`api.binance.com` returns **HTTP 451** (`"Service unavailable from a restricted
location"`) to the desktop on every endpoint tried, and the raw
`stream.binance.com` websocket refuses the upgrade with 451 too. From
eu-west-1: **200 and 101**. The `.vision` mirror is what makes the current
desktop setup work at all.

**4. But moving to eu-west-1 costs ~27ms one-way on the Binance leg.** Binance's
matching engine is in Tokyo (`ap-northeast-1` — every resolved IP is
`13.x/18.x/35.x/54.x` in that region). Ireland is farther from Tokyo than the US
south-east: `.vision` warm RTT 238.6ms from EC2 vs 183.8ms from the desktop.
And `api.binance.com` from EC2 (240.5ms) is **no faster than the mirror**
(238.6ms) — CloudFront fronts it, but a price query is dynamic, so every request
still walks to Tokyo. Switching to the real API on EC2 buys endpoint surface and
rate limits, **not** latency.

### The net ledger for one updown decision

Halving RTT for a one-way estimate:

| Leg | Desktop today | EC2 direct | Δ |
|---|---|---|---|
| Binance trade → us (one way) | ~92ms | ~119ms | **+27ms worse** |
| decide (loop lateness p50, measured) | 50ms | 50ms | 0 |
| order → CLOB ack (full RTT) | 148.4ms | 17.9ms | **−130.5ms better** |
| **information → order acknowledged** | **~290ms** | **~187ms** | **−103ms** |

The Binance penalty is real and should be stated in any writeup. It is also
about a quarter of the order-path gain, and the CLOB book leg (−193ms on the WS
handshake) is not even counted in that table.

---

## 2. What `--features ec2` actually gates

It has never been EC2-specific. From `pmengine/Cargo.toml`:

```toml
[features]
default = ["ec2"]
ec2   = ["clap", "sigv4"]
sigv4 = ["aws-config", "aws-sigv4", "aws-credential-types", "aws-smithy-runtime-api"]

[[bin]]
required-features = ["ec2"]
```

So `ec2` means "build a CLI binary, and be able to SigV4-sign proxy requests."
It is `default`, so a plain `cargo build` already turns it on. The name is a
fossil of the pre-teardown host `pmt-ec2-euw1` (`34.250.56.199`, listed under
"Notably absent" in `.infra/INFRA.md`). `pmproxy` carries the same fossil, where
it means `clap + ws`.

The irony worth writing down: **on a real EC2 box the `sigv4` half becomes dead
weight**, because there is no proxy to sign for. But `required-features = ["ec2"]`
welds them together, so you cannot currently build the binary without dragging in
four `aws-*` crates and their transitive tree.

**Delta:** split the feature so the CLI and the signer are independent.

```toml
cli   = ["clap"]
ec2   = ["cli", "sigv4"]   # kept as an alias so nothing downstream breaks
[[bin]]
required-features = ["cli"]
```

Then the EC2 build is `cargo build --release --no-default-features --features cli`.
Leave `publish-pmengine.yml` building `--features ec2` — it already produces
**both** `x86_64-unknown-linux-gnu` and `aarch64-unknown-linux-gnu` artifacts, so
Graviton is supported today with no workflow change. The slim build is an
optimisation, not a prerequisite.

---

## 3. Code and config deltas

### 3a. Running without pmproxy — this part is already clean

Every proxy behaviour in pmengine is gated on `PMPROXY_URL` being **present**,
not on a build flag:

- `config.rs:42-45` — `clob_url` falls back to `https://clob.polymarket.com/`
  when `PMPROXY_URL` is unset.
- `client.rs:107` — `proxy_url: Option<String>` is just `env::var("PMPROXY_URL").ok()`.
- `client.rs:244`, `client.rs:613` — order POST and book GET pick the direct URL
  when it is `None`.
- `client.rs:255`, `client.rs:625` — SigV4 signing is inside `if self.proxy_url.is_some()`.
- `engine.rs:108-118` — the signer is only constructed when `PMPROXY_URL` is set.
- `client.rs:110` — L1 auth and `derive-api-key` **already** go direct to
  `https://clob.polymarket.com`, hardcoded, in every configuration.

**So the entire "drop the proxy" change is: unset two env vars.**

| Var | Desktop today | EC2 |
|---|---|---|
| `PMPROXY_URL` | the Lambda Function URL | **unset** |
| `PMPROXY_AWS_REGION` | `eu-west-1` | **unset** (dead without the above) |

Nothing hardcodes a proxy URL. No code change is required for this half.

Keep the Lambda deployed and unchanged — it costs ~$0 under its reserved
concurrency of 10, and it is the rollback path (§7).

### 3b. Binance endpoints — this part is hardcoded and must become config

Two constants, both baked in:

- `pmengine/src/strategies/updown_model.rs:74`
  `pub(crate) const BINANCE_DATA: &str = "https://data-api.binance.vision";`
  (also consumed by `replay.rs` and `updown.rs`'s `poll_binance`)
- `pmengine/src/strategies/updown.rs:445`
  `"wss://data-stream.binance.vision/ws/{}@trade"`

**Delta:** read both from env with the current values as defaults, so this is a
config change per location and never a rebuild:

```
PMENGINE_BINANCE_REST   default https://data-api.binance.vision
PMENGINE_BINANCE_WS     default wss://data-stream.binance.vision
```

On EC2 set them to `https://api.binance.com` / `wss://stream.binance.com:9443`.

**Be honest about why.** The measurement says this buys **~0ms** (240.5 vs
238.6ms warm). It buys: the full endpoint surface Phase 2 of issue #4 asks for
(`bookTicker`, depth, combined streams) which the mirror does not serve; a
documented rate limit (the probe read `REQUEST_WEIGHT 6000/min` off
`exchangeInfo`); and no dependency on a mirror that exists for the convenience of
people in our current situation and could be withdrawn. It is a robustness
change, not a speed change, and the plan should not be sold on it.

The corollary is the load-bearing one: **the `.vision` mirror keeps working from
eu-west-1** (200/101, measured), so this delta can be deferred past cutover
without blocking anything.

### 3c. Websockets — what actually changes

The engine already opens a **direct** SDK websocket to
`ws-subscriptions-clob.polymarket.com` (`engine.rs`, `WsClient::default()` in the reconnect loop),
alongside the REST poller. It has never gone through pmproxy — and it *cannot*:
per `pmproxy/README.md`, `/clob/ws/{chan}` is **"EC2 only — Lambda can't WS"**.

That is the single sharpest architectural point in this document. **The proxy
being a Lambda is why there is no proxied websocket path.** The moment the engine
runs on a box in eu-west-1, the proxy stops being needed *and* the WS stops
paying a transatlantic leg, in the same move.

What the probe proves and does not prove:
- **Proves:** the market WS accepts the upgrade (101) from **both** locations, so
  it is not geoblocked; and the handshake costs 225.6ms from the desktop vs
  33.0ms from EC2.
- **Does not prove:** that book diffs actually flow, or at what rate. Only the
  handshake was measured. The REST-poller comment in `engine.rs` ("so books stay current
  even when WS is unavailable, e.g. from a US IP without a WS-capable proxy")
  may therefore be stale. Open question #3.

Two consequences for `PMENGINE_BOOK_POLL_MS` (currently 2000):

1. **The poller gets faster for free.** The latency report measures the poller
   walking ~10 legs serially (median 5 slugs alive, two legs each). At the
   desktop's 120ms that is a 1.2s sweep on top of the 2s period — the report's
   3.2s effective book age. At EC2's 18.8ms the same sweep is 188ms. Effective
   book age drops from ~3.2s to ~2.2s **with no config change at all**.
2. **Then do Phase 4 properly.** Issue #4 Phase 4 wants the WS authoritative and
   REST demoted to health/fallback. That is worth **$337 per 11h corpus** by the
   latency report's own pricing — 25× the network fix — and it is the change the
   EC2 move makes cheap rather than the change the EC2 move performs. Do not
   conflate them: **ship the move, then ship Phase 4, and attribute separately.**

Leave `PMENGINE_BOOK_POLL_MS=2000` at cutover. Changing it in the same step
would make the A/B unreadable.

### 3d. Full env diff for the EC2 box

```diff
- PMPROXY_URL=https://gb5pjlcr2xxdheh622fgleorsa0whfwe.lambda-url.eu-west-1.on.aws
- PMPROXY_AWS_REGION=eu-west-1
  PM_SIGNATURE_TYPE=1
  PM_FUNDER_ADDRESS=0x…                 # see §6 — a DIFFERENT wallet during A/B
  PM_PRIVATE_KEY=…                      # see §5 — from SSM, never on disk
  PMENGINE_MAX_POSITION_SIZE=2500
  PMENGINE_MAX_TOTAL_EXPOSURE=2500
  PMENGINE_BOOK_POLL_MS=2000            # unchanged at cutover, on purpose
  PMENGINE_TICK_INTERVAL_MS=50          # the unit already sets this
  PMENGINE_RECONCILE_ON_STARTUP=true    # DANGEROUS with two engines — see §6
+ PMENGINE_CONTROL_BIND=<tailscale-ip>:7531
+ PMENGINE_BINANCE_REST=https://api.binance.com      # optional, after 3b
+ PMENGINE_BINANCE_WS=wss://stream.binance.com:9443  # optional, after 3b
```

And on the **desktop**, to drive the remote engine with the existing CLI:

```
PMENGINE_CONTROL_URL=http://<tailscale-ip>:7531
```

`pmtrader/engine.py:36` reads exactly that var and is the only module that talks
to the control plane, so every `pmt` subcommand follows it with no code change.

---

## 4. Ops architecture

### Instance

Bound from the binary and the feeds, not measured: the release binary is 20.5MB
stripped, the working set is a few hundred order-book levels and a handful of
websockets, and the process is IO-bound. RAM is a non-issue.

**The binding constraint is burst credits, not memory.** A t3/t4g `micro` earns
baseline 10% of 2 vCPU; a 50ms tick loop with book processing may exceed that.

| | vCPU/RAM | Baseline | eu-west-1 on-demand |
|---|---|---|---|
| **t4g.micro** ← start here | 2 / 1GB | 10% | $6.72/mo |
| t4g.small ← step up if credits drain | 2 / 2GB | 20% | $13.43/mo |
| t3.micro (x86_64) | 2 / 1GB | 10% | $8.32/mo |

Use **t4g.micro** (Graviton): `publish-pmengine.yml` already builds
`aarch64-unknown-linux-gnu`, so there is no toolchain work. Run in **standard**
credit mode, not unlimited — unlimited turns a runaway loop into a silent bill.
Watch `CPUCreditBalance` for a week and size up if it trends down.

AL2023, 30GB gp3, **encrypted**, `DeleteOnTermination=false` for the data volume
if you split it (tapes are the one thing here that cannot be recreated).

### systemd — reuse `deploy/systemd/`, do not duplicate

`deploy/systemd/pmengine.service` and `pmt-rtds-recorder.service` are merged
(f78bb20) and already encode the hard-won parts: `KillSignal=SIGTERM` +
`TimeoutStopSec=30` so graceful shutdown gets to cancel resting orders,
`StartLimitBurst=5` so a wedged engine cannot crash-loop into the book, and
`PMENGINE_TICK_INTERVAL_MS=50` so the unit does not silently run updown's 50ms
model 20× slow.

Changes for the cloud box only:

- **User units → system units.** No `loginctl enable-linger` dependency; the
  engine must come up on instance start with nobody logged in.
- `WorkingDirectory` / `ExecStart` paths `/var/home/hunter/Desktop/code/pmt` →
  `/opt/pmt`.
- `User=pmt` — a dedicated non-login account. Nothing here needs root.
- `EnvironmentFile=/run/pmt/engine.env` written by `ExecStartPre` from SSM (§5).
- **The restart caveat in `deploy/systemd/README.md` gets stronger, not weaker.**
  That README warns that `Restart=on-failure` plus arm-state recovery means "a
  crashed engine re-enters the market unattended." On a box that never powers
  off, in a region the operator is not in, with nobody watching — that is the
  behaviour you are explicitly buying. Read that README before enabling, not
  after.

### Health checks — judge liveness by the control plane, never by the process

Evidence, from this repo, from today: commit **133806c**. The axum 0.7→0.8 bump
changed route capture syntax (`/:id` → `/{id}`), and the old form panics **at
router construction**, which happens inside the spawned control task. Result:
`cargo test` stayed green, the process started, the process *ticked* — and every
engine on the new deps came up **headless and unarmable**. The fleet was dark
~09:25–09:33Z until a human noticed live.

`pidof pmengine` would have reported perfect health for all eight minutes.

On the desktop that was eight minutes and a person in the room. On an EC2 box in
Ireland, the control plane **is the only way in**, so the same bug is an engine
you can neither arm nor disarm nor inspect — while it holds inventory.

Therefore:

- **Liveness = `GET /status` returns 200 with a plausible uptime and a
  monotonically increasing tick count.** Never process existence.
- Add a systemd watchdog probing the control port (a `Type=notify` conversion, or
  simplest: a `pmengine-health.timer` every 30s that curls `/status` and
  `systemctl restart pmengine` on three consecutive failures — with the same
  StartLimit brake).
- Page on it. A headless engine must reach the operator without the operator
  asking.
- **Keep an out-of-band kill path that does not use the control plane**: SSM
  Session Manager → `systemctl stop pmengine`. That is SIGTERM, which is the
  engine's graceful path (it cancels resting orders), so the out-of-band stop is
  also a *safe* stop. This is the single most important operational property of
  the whole design, and it is why §5 chooses SSM over SSH.

### Arming and monitoring from the desktop

Tailscale on both boxes. `PMENGINE_CONTROL_BIND=<tailscale-ip>:7531` on EC2,
`PMENGINE_CONTROL_URL=http://<tailscale-ip>:7531` on the desktop. Then `pmt
crypto arm/trigger/disarm`, `pmt engine status/logs`, `pmt orders` all work
unchanged from the couch.

**Bind to the Tailscale address, never `0.0.0.0`.** `control.rs` has no
authentication of any kind — its own header says "All traffic is local." Its
endpoints cancel orders, stop strategies, approve alerts and place orders. On a
box with a public IP, `0.0.0.0:7531` is an unauthenticated remote control for the
wallet. If Tailscale is not wanted, the fallback is loopback bind + an SSH
tunnel, never a security-group hole.

### Tapes, corpus, and the poweroff hole

Data that must survive: `~/.pmt/engine/updown-tape.jsonl` (cross-session
calibration), `~/.pmt/corpus/rtds/*.jsonl` (the settlement stream — **no
backfill exists**), `~/.pmt/engine/arms-state.json` (live arms).

- Primary: the EBS volume. Durable across reboots, unlike the desktop's tmpfs
  scratchpads.
- Nightly `aws s3 sync` to `s3://pmt-corpus-euw1/` — same-region, so **egress is
  free**; ~$0.02/mo of storage at any plausible volume.
- The desktop pulls from S3 for analysis. Analysis stops needing the trading box
  to be reachable at all.

**The RTDS recorder belongs on this box, and this is where it pays for itself.**
Per `analysis/chainlink_stream_scout.md`, the settlement object is 1 Hz, free,
and has *no history, no replay, no on-chain shadow*. Every hour nobody is
connected is calibration data that is permanently gone. The desktop powers off
nightly by design — so today the recorder has a guaranteed nightly hole in a feed
that cannot be backfilled. An always-on box closes it. As a bonus the RTDS
handshake goes 348.5ms → 69.0ms.

**And it removes the poweroff exposure hole entirely.** CLAUDE.md is explicit:
the nightly poweroff runs graceful shutdown, so resting orders get cancelled —
but **already-filled inventory rides to resolution unmanaged, with no exits and
no evacuation, because the process that would have run them is gone.** The
ceiling is the arm's `--size` (100–400 typical). An EC2 box does not power off.
That is not a latency argument and it is probably the strongest single reason in
this document.

---

## 5. Security

### `PM_PRIVATE_KEY` custody — ranked

The key is an EOA that owns a `signature_type=1` Polymarket proxy wallet. It
signs orders and it is the wallet.

**1. SSM Parameter Store SecureString + a dedicated KMS CMK → tmpfs at unit start. ← recommended**

`ExecStartPre` fetches the parameter and writes `/run/pmt/engine.env` (tmpfs,
`0400 pmt:pmt`); the unit passes `--env-file /run/pmt/engine.env`;
`ExecStopPost` shreds it. Why it wins:

- **Never at rest on EBS**, so it is not in any snapshot or AMI you later create.
  (Note: encrypting the EBS volume does *not* solve this — at-rest encryption
  defends the physical disk, not a process on the instance.)
- Access is IAM-gated and every read is a **CloudTrail event**. You can alarm on
  a read that is not an instance start.
- Rotation is a parameter update plus a restart. No rebake, no redeploy.
- The instance role scopes to exactly one parameter path and one KMS key.
- Standard parameters are **free**; the CMK is $1/mo.

Residual risk: it is in the process environment, so root can read
`/proc/<pid>/environ`. Acceptable when the operator is the only root. Tightening
further means teaching pmengine to read a systemd credential
(`LoadCredentialEncrypted=`) instead of env — worth doing eventually, not a
blocker.

**2. KMS envelope-encrypted blob on the volume, decrypted at boot.** Same trust
model as (1) — the instance role can decrypt either way — but rotation means
re-encrypting and redeploying, and you lose the CloudTrail-per-read granularity.
No advantage over (1).

**3. AWS Secrets Manager.** Functionally (1) plus rotation machinery we do not
need, at $0.40/secret/mo. Fine; strictly costlier.

**4. Baked plaintext `.env` on the volume.** What the desktop does today. On a
cloud box: it is in every snapshot and every AMI, it survives into images you
forget you made, and any file-read bug hands over the wallet. **Reject.**

**5. Env vars in user-data or the launch template. Never.** User-data is
readable by *any* process on the instance via IMDS at `/latest/user-data`, with
no credential beyond an IMDSv2 token that any local process can mint. This is a
hard no, and it is worth writing down because it is the most convenient wrong
answer.

Also: **use a different EOA for the cloud box during A/B** (§6), which caps the
blast radius of a cloud-side mistake at that wallet's float.

### Security group — zero inbound

No inbound rules at all. Not "SSH from the home IP" — that is a residential
dynamic address, so it is both a maintenance burden and a hole that widens
whenever the ISP re-leases the block.

- **Tailscale is outbound-only** (UDP 41641 out, DERP over 443 out). It needs no
  ingress rule.
- **Administration is SSM Session Manager**, which is also outbound-only (the
  agent polls). It ships on AL2023.
- Result: **no key pairs, no port 22, no ingress.** The probe instance ran this
  way — it used the default VPC security group, whose only inbound rule is
  self-referential, and reached everything it needed.

Egress stays open. An egress allowlist sounds appealing until you notice
Polymarket is Cloudflare anycast and Binance rotates across a dozen Tokyo IPs per
resolution — the allowlist would be brittle in exactly the way that takes the
engine down at 3am.

### Instance hardening

- **IMDSv2 required**, `HttpPutResponseHopLimit=1`. Both were set on the probe
  instance. Hop limit 1 stops a container or an SSRF from reaching the metadata
  service and stealing the instance role.
- Instance role scoped to: `ssm:GetParameter` on the one path, `kms:Decrypt` on
  the one key, `s3:PutObject`/`GetObject` on the corpus prefix,
  `AmazonSSMManagedInstanceCore`. Nothing else. Notably **not** `lambda:*` —
  without the proxy the box has no reason to invoke anything.
- EBS encrypted (defends the snapshot, not the instance — see above).
- **Patching:** `dnf-automatic` in *download and install* mode for security
  updates, **but never automatic reboots.** An unattended reboot mid-window
  abandons filled inventory exactly the way the desktop poweroff does. Reboot
  manually, disarmed, between windows.
- Delete the two unused static AWS keys from the repo secrets while you are here
  (`.infra/INFRA.md` flags them; nothing reads them since deploys went OIDC).

---

## 6. Split-brain — the money risk

**Two engines armed on the same wallet is the failure mode that costs real
money, and nothing in the system currently prevents it across hosts.**

### Why it is worse than "two processes trading"

- Both engines authenticate the **same EOA**. `derive-api-key` is deterministic
  from the key, so they get the same L2 credentials and act as one API identity
  against the same funder wallet.
- **`PMENGINE_RECONCILE_ON_STARTUP=true` is the default** (`config.rs:67`). On
  startup each engine *cancels every pre-existing order on its subscribed
  tokens* — it is designed to treat itself as the sole order manager. Two engines
  on the same tokens therefore **cancel each other's resting orders**, on startup
  and on every restart. This is not a hypothetical race; it is the documented
  default behaviour of both processes.
- **The risk manager's exposure ledger is per-process.** `--size` and
  `PMENGINE_MAX_TOTAL_EXPOSURE` are enforced inside one engine. Two engines
  each honouring a $2500 cap put **$5000** at risk. Every risk limit in the
  system silently doubles.
- **The ~5s position reconcile reads the funder wallet from the data-api**, so
  each engine sees the *other's* fills as its own. Budget arithmetic then
  misfires in both directions — one engine thinks it is full and stops, the other
  thinks it has room it does not have.
- **`arms-state.json` recovery weaponises a file copy.** Since a7851ee the engine
  persists arms and **re-arms every still-open window on startup**, with token ids
  from the file and no operator in the loop. So `rsync`-ing the state file to the
  new box — the single most natural thing to do during a migration — makes the
  EC2 engine start hunting the windows the desktop is already hunting.
- The existing interlock does not help. `engine.rs:550-563` binds the control port
  synchronously and fails fast with *"Another engine is likely already running"* —
  which is exactly right, and **host-local only.** It cannot see a peer in
  Ireland.

### The interlock, in shipping order

**L0 — different wallets during A/B. Ship first, needs zero code.**

Give the EC2 engine its own EOA and its own Polymarket proxy wallet, funded with
a small float. Different `PM_PRIVATE_KEY` and `PM_FUNDER_ADDRESS` means: no
shared order state, no cross-cancellation, no shared exposure ledger, no
poisoned position reconcile. **The A/B becomes safe by construction rather than
by discipline.** The two engines are not trading the same capital, so PnL is not
directly comparable — which is fine, because Phase 13 is a *latency telemetry*
experiment, not a PnL bake-off.

**L1 — a wallet-owner lease. Ship with cutover.**

One authoritative marker; the engine refuses to place orders unless it holds it.

- **Store: S3 conditional write.** `s3://pmt-ops-euw1/wallet-lease/<funder>.json`
  holding `{owner, host, pid, acquired_at, expires_at}`. Acquire with
  `If-None-Match: *`; renew every 15s with a PUT guarded on the current ETag;
  TTL 60s. A challenger may only take over after `expires_at`.
- **The fail-closed half is the load-bearing half.** On a failed renewal the
  holder must *immediately* stop placing new orders, cancel resting orders, and
  alert. Correctness under partition comes from the loser stopping, not from the
  winner winning.
- **Hook it into `OrderManager`, not the strategy** — that way every order path
  is covered, including manual `pmt buy/sell` routed through the engine, and a
  future strategy cannot forget to check.
- S3 over DynamoDB: no table to manage, same region, and the account has no
  DynamoDB footprint to grow. Cost: a few PUTs per minute, ~$0.02/mo.

**L2 — make the human error loud. ~30 lines.**

`PMENGINE_PEER_CONTROL_URL`: before `pmt crypto arm` sends anything, GET the
peer's `/strategies`; if the peer reports live arms on the same funder, refuse
with a loud message. This catches the realistic accident (operator forgets the
desktop engine is up). It is **not** a safety mechanism — unreachable does not
mean safe, and the desktop is off every night by design — so it sits on top of
L1, never instead of it.

**L3 — an owner stamp in `arms-state.json`. Belt.**

Record the `owner_host` that wrote the file. On startup, if `owner_host` is not
this host and the file is younger than the lease TTL, **refuse to recover the
arms** and alert. This is the specific defence against the rsync accident above,
and it is cheap.

**L4 — the operational rule, written where it will be read.**

Exactly one box owns the wallet at a time. Put `PMT_WALLET_OWNER=ec2-euw1` in the
shared config and have `pmt engine start` refuse locally when it does not match
the local hostname. An operator who has to delete a line to start the second
engine is an operator who knows they are doing it.

---

## 7. Migration sequence

### Phase A — provision and verify, zero money at risk

Provision (CLI, per `.infra/INFRA.md` — see open question #7). Deploy the aarch64
release artifact and the two units. Then, **in dry-run**:

```
pmengine --env-file /run/pmt/engine.env run updown --dry-run --skip-warmup
```

Gates, all of which must pass before any order:

1. **L1 auth succeeds from an Irish IP** — `derive-api-key` returns credentials.
2. `GET /status` on the control plane returns 200 over Tailscale from the
   desktop. (Per 133806c, this is a real gate, not a formality.)
3. The market WS reaches 101 **and book diffs actually arrive** — resolves open
   question #3.
4. The RTDS recorder is writing to `~/.pmt/corpus/rtds/`.
5. `systemctl stop pmengine` over SSM produces a clean SIGTERM shutdown.

**Then the one gate that decides whether this plan is viable at all: place a
single 1-share order from the box and see whether it fills or 403s.** §9 explains
why nothing measured here can answer that. Do this before anything else is built
on top.

### Phase B — the Phase-13 A/B, one night

EC2 engine armed on **its own wallet** (L0) at `--size 10 --clip 5`, running
alongside the desktop's normal session. Both tapes collected. Compare, using the
machinery `analysis/latency_report.py` already provides: fire→fill, spot age at
decision, book age at decision, miss rate, pay-up on chases.

This is issue #4 Phase 13 as written — *"run the exact same strategy from two
environments… do not infer proximity from ping alone."* The probe table in §1 is
the ping. This is the measurement.

### Phase C — cutover

1. `pmt crypto disarm` on the desktop; confirm `pmt orders` is empty.
2. `pmt engine kill`; confirm the process is gone.
3. Move the live `PM_PRIVATE_KEY` / `PM_FUNDER_ADDRESS` into the EC2 SSM
   parameter.
4. Enable the L1 lease; confirm the EC2 engine acquires it.
5. Set `PMT_WALLET_OWNER=ec2-euw1` (L4).
6. Arm from the desktop over Tailscale. Watch one full window end to end.

### Rollback — about two minutes

Positions are on-chain and location-independent; **nothing about a position is
tied to the host that opened it.** So:

1. `systemctl stop pmengine` over SSM (graceful — cancels resting orders).
2. The lease expires in 60s.
3. Start the desktop engine with the original `.env`, `PMPROXY_URL` restored.
4. `arms-state.json` carries the live arms across, which is exactly what a7851ee
   built it for.

**Keep the pmproxy Lambda deployed and untouched throughout.** It is ~$0 under
reserved concurrency 10, and it is the thing that makes rollback possible — with
it gone, a desktop fallback has no non-US egress path for whatever the CLOB
actually enforces. Do not "clean it up" as part of this migration.

---

## 8. Cost

All figures from the AWS Pricing API for eu-west-1, read 2026-08-23.

### Steady state

| Line | Rate | Monthly |
|---|---|---|
| t4g.micro on-demand | $0.0092/hr | **$6.72** |
| Public IPv4 address (in-use) | $0.005/hr | **$3.65** |
| gp3 30 GB | $0.088/GB-mo | **$2.64** |
| KMS CMK for the key parameter | $1/key-mo | **$1.00** |
| Data transfer out | first 100 GB/mo free | **$0.00** |
| S3 corpus (same-region sync) | | **~$0.02** |
| **Total** | | **≈ $14.03/mo** |

- On **t4g.small** (if credits drain): **$20.74/mo**.
- A 1-year no-upfront Compute Savings Plan takes ~28% off the instance line:
  **≈ $12.15/mo**.
- Egress is genuinely ~zero: book diffs and market data are *inbound* (free);
  outbound is order POSTs (~1KB), WS subscribe frames, and the S3 sync, which is
  same-region and therefore free. Nowhere near the 100GB allowance.
- The public IPv4 line is unavoidable-in-practice: the alternative is a NAT
  gateway at $32.85/mo, which is worse than the whole rest of the bill.

### Against the latency value

`analysis/latency_report.txt` (fb4ea02, same day) prices the network fix on a
measured 11.0h corpus of 22,715 filled shares:

> **(a) NETWORK: 160ms order path → 50ms co-located — $13.66 total, +0.0601 c/share**

At that fill rate, $13.66/11h extrapolates to **~$894/mo**, against ~$14/mo of
infrastructure — roughly **64×**.

Refining with the numbers actually measured here rather than the round ones: the
live path is 148.4ms (not 160ms) and EC2 is 18.8ms (not 50ms). Using the report's
own model (λ = 0.0516 upward ask-moves/sec/leg × flight time × 10.64c mean
pay-up): 0.0815 c/share → 0.0103 c/share, a saving of **0.0712 c/share**, or
**$16.17 per 11h corpus ≈ $1,058/mo**.

**Three caveats, stated plainly.** That is one 11-hour corpus; it assumes volume
holds; and the sub-second scaling is a constant-rate jump model, not a
measurement. Any of those could be off by a factor of several. None of them
change the decision, because the ratio to $14/mo is ~64–75× and would survive an
order of magnitude of error.

**But do not let that reorder the priorities.** The same report prices the two
bigger fixes on the same corpus: the polling/WS fix at **$337/11h** and the
inflight-TTL fix at **$769/11h** — 25× and 56× the network fix. The report's
verdict is right: *latency is not the binding constraint.*

The correct reading is that these are **not competing**:

- The network fix is the only one that costs money, and at $14/mo it clears its
  own bar by ~64× — a rounding error against a wallet running $2500 exposure
  caps.
- The EC2 move is the **enabler** for the big one. Phase 4's WS-authoritative
  book is worth 25× more, and the proxy being a Lambda is precisely why there has
  never been a proxied WS path. Moving the engine is what makes that work cheap.
- And the largest non-latency win in the plan is not in the report at all: an
  always-on box closes the nightly unmanaged-inventory hole and the
  unbackfillable RTDS gap.

Buy the box for the exposure hole and the WS enablement. The 64× on latency is
the part you do not have to argue about.

### What this assessment cost

| | |
|---|---|
| Instance | `i-0bae1e59a226d2bdd`, t3.micro, eu-west-1a |
| Launched | 2026-08-23T09:19:50Z |
| Terminated | 2026-08-23T09:24:19Z |
| Runtime | **4m29s (269s)** |
| t3.micro | $0.0114/hr × 0.0747hr = $0.00085 |
| Public IPv4 | $0.005/hr × 0.0747hr = $0.00037 |
| gp3 8 GB | $0.088/GB-mo, prorated = $0.00007 |
| Egress | a few MB, within free allowance = $0 |
| **Total** | **≈ $0.0013 (about 0.13 US cents)** |

Verified after termination: instance `terminated`, **zero** volumes in the
region, **zero** non-default security groups, no instances in any other region.
No IAM role, instance profile, key pair, or security group was created — the
probe used the default VPC's default SG (self-referential ingress only) and
returned its results over the serial console, which is why it needed no inbound
access and left no residue.

---

## 9. Open questions

1. **Does the CLOB accept an authenticated order from an eu-west-1 IP?**
   Unproven, and it is the question the whole plan rests on. Resolvable only by
   placing one real share (Phase A). The proxy's existence implies yes, but
   `.infra/INFRA.md` shows the Lambda has only ever been smoke-tested on
   `/health`, `/clob`, and `/gamma` — **no order has been proven through it
   either.** If eu-west-1 is refused, the fallback is: keep the proxy for the
   order path, move the engine anyway for the read path and the WS and the
   always-on properties. Most of this document survives that outcome.

2. **`polymarket.com/api/geoblock` returns `blocked:true` from BOTH locations** —
   `{"country":"US","region":"SC"}` from the desktop, `{"country":"IE","region":"L"}`
   from eu-west-1. So it is *not* a discriminator, and since the desktop places
   filling orders today, it plainly does not gate the CLOB. It appears to be the
   website's UI gate. Do not use it as a geoblock oracle in either direction.

3. **Does the market WS actually deliver book diffs?** Only the 101 handshake was
   measured, from both locations. If the REST poller is silently carrying the
   book today, the Phase 4 win is larger than modelled and the REST-poller
   comment in `engine.rs` is stale. Cheap to settle: count `ws_update_count` in the logs.

4. **Binance rate limits from a dedicated EC2 IP.** `exchangeInfo` reported
   `REQUEST_WEIGHT 6000/min`, and limits are per-IP; a dedicated instance IP
   should be strictly better than a shared mirror. Unmeasured.

5. **Do t4g.micro burst credits hold at a 50ms tick?** Needs a week of
   `CPUCreditBalance`. The step-up is $6.71/mo, so this is a monitoring task, not
   a design risk.

6. **Should L1 be built, or is the operational rule enough?** Build it. The
   `PMENGINE_RECONCILE_ON_STARTUP` cross-cancellation and the doubled exposure
   ledger are not hazards you manage with discipline across two time zones.

7. **This adds resources to an account whose IaC has no state.** `pulumi up` is
   forbidden until the eleven live resources are imported
   (`infra/pulumi/README.md`). Either do the import first, or build by CLI and
   add these to the import list. Recommend the latter — do not block a $14/mo
   box on a state migration — but **record every resource in `.infra/INFRA.md`
   as it is created**, which is the discipline that made this assessment possible
   at all.

---

## Appendix — reproducing the probe

```bash
# Both locations, simultaneously, same script:
python3 analysis/net_probe.py -n 20 --gap 3 --timeout 8 \
    --env .env --out analysis/net_probe_raw.json

# Cloudflare edge identification (which colo is serving us):
curl -s https://clob.polymarket.com/cdn-cgi/trace   # colo=ATL desktop, colo=DUB eu-west-1

# The Binance geoblock, in one line:
curl -s -o /dev/null -w '%{http_code}\n' https://api.binance.com/api/v3/time
#   451 from the US desktop, 200 from eu-west-1
```

The EC2 side ran the identical script, gzip+base64-embedded in user-data
(verified byte-identical by sha256 before launch), with results written to
`/dev/console` and retrieved via `aws ec2 get-console-output --latest`. SSM was
deliberately *not* used: it would have required creating an IAM role and an
instance profile, which is more AWS mutation than this assessment was authorized
to make. Console output needs no inbound access, no key pair, and no IAM, and it
leaves nothing behind to clean up.
