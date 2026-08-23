# Shipping pmengine to the EU box

The eu-west-1 box (t4g.micro, aarch64, SSM-only; instance id lives in the private runbook) runs
the **private** flavor of pmengine against the EU L0 wallet. This directory is
the whole ship path.

| file | what it is |
| --- | --- |
| `ship-eu.sh` | build → upload → install → smoke. Re-runnable. Never starts anything. |
| `pmengine.service` | the system unit, as installed at `/etc/systemd/system/` |
| `engine.env.example` | skeleton for `~/.pmt/engine.env` on the box. No secrets. |

```sh
git submodule update --init --checkout pmengine/src/strategies/private
./deploy/eu/ship-eu.sh
```

## The four things that make this non-obvious

**1. The binary is always cross-compiled, never built on the box.** The target
has 1GB of RAM; `rustc` OOMs on it. `ship-eu.sh` drives `cross` over podman
with `AWS_LC_SYS_CMAKE_BUILDER=1` (a GCC memcmp bug in `aws-lc-sys`), mirroring
`.github/workflows/publish-pmengine.yml`. The flag has to be passed *twice* —
once for the host and once through `CROSS_CONTAINER_OPTS` into the container.

**2. A worktree does not populate submodules, and the failure is silent.**
`build.rs` probes the *file* `src/strategies/private/updown.rs`, because an
uninitialized submodule leaves an empty directory behind that would otherwise
read as present. Build without it and you get a working engine that only knows
`example` — no `updown`, no strategy, useless. `ship-eu.sh` refuses to start in
that state, and refuses again if the build log carries build.rs's
`private strategies absent` warning.

**3. `pmengine list` is the flavor proof, and it is the only one that counts.**
Host-side checks tell you what you *built*; `list` on the box tells you what
actually landed and actually runs there. It starts no strategy and places no
order. If it prints `updown`, the aarch64 build runs and the private strategies
are aboard. If it prints only `example`, stop — the public engine shipped.

**4. The build image's glibc must be no newer than the box's.** Amazon Linux
2023 has glibc 2.34. cross's default `main` image is a recent Ubuntu and links
against 2.38/2.39, producing a binary that builds perfectly here and dies on
the box with `GLIBC_2.39 not found` — glibc symbol versioning is backward but
not forward compatible. `ship-eu.sh` pins
`ghcr.io/cross-rs/aarch64-unknown-linux-gnu:0.2.5` (Ubuntu 20.04, glibc 2.31)
and then re-checks the built binary's highest required `GLIBC_` symbol against
`BOX_GLIBC` before it ships anything.

Note this means the **public release artifacts from
`.github/workflows/publish-pmengine.yml` will not run on AL2023** — that
workflow installs cross from git and takes the `main` image. Nothing consumes
those artifacts today, but do not assume a GitHub release binary is
box-compatible.

**5. The box has no S3 access, deliberately.** The instance role
(`pmt-eu-ssm`) is `AmazonSSMManagedInstanceCore` and nothing else. `ship-eu.sh`
uploads from the desktop and hands the box a **15-minute presigned URL** rather
than granting a trading host standing bucket credentials. Do not "fix" this by
attaching an S3 policy to the role.

SSM payloads are base64'd into the parameter JSON and passed as `file://`.
Inline `--parameters commands=[...]` mangles anything containing a quote or a
`$`, and it fails in ways that look like the script is wrong.

## Secrets

`~/.pmt/l0.env` holds the L0 EOA private key. It was generated **on the box**
and has never left it. Nothing in this directory reads it, prints it, or copies
it — the unit references it by path only. `~/.pmt/engine.env` is non-secret
knobs and is safe to read; the split is the point.

No proxy vars in `engine.env`. The box talks to `clob.polymarket.com` directly.
Routing it through pmproxy would put a US egress back in front of the EU box
and undo the only reason the box exists.

## Operator ceremony — everything `ship-eu.sh` deliberately does NOT do

The script installs a stopped engine. Arming it is a sequence, and the order is
load-bearing.

### Step 1 — partition the desktop FIRST, and restart it

`PMENGINE_SERIES_ALLOWLIST` is an allowlist of slug prefixes with **no deny
form**. Unset means *unpartitioned* — every series passes. The desktop's `.env`
currently has no allowlist line, so the desktop will happily trade
`bnb-updown-5m` at the same moment the EU box does.

Two engines quoting one market cross each other. Different wallets do not make
that safe: it is the same beneficial owner on both sides of the trade, which is
wash-trade shaped no matter what either engine intended.

So, on the desktop, before the EU engine ever starts:

1. Enumerate what the desktop currently has armed.
2. Set `PMENGINE_SERIES_ALLOWLIST` in the desktop `.env` to **exactly those
   series minus `bnb-updown-5m`**. It has to be an explicit list; there is no
   way to say "everything except".
3. Restart the desktop engine so it reads the new partition, and confirm from
   its logs that the allowlist is in force.

Only once the desktop is provably off `bnb-updown-5m` does the EU box get it.

### Step 2 — fund the deposit wallet (the wrap)

`PM_SIGNATURE_TYPE=3` means orders are signed by the L0 EOA but collateralised
by the **deposit wallet** `0x6da3e7Dd76cE67B32ae7911e19da0c00550F1D71`. The
engine's caps are enforced against collateral that must actually be sitting
there.

The wallet is **already deployed** (beacon proxy, verified on-chain) — the
sequencing hazard from the onboarding research, where wrapping into an
undeployed address strands the funds, is behind us.

Collateral is pUSD, not USDC.e, and the conversion is permissionless:

```
approve(CollateralOnramp, amount)              on USDC.e 0x2791Bca1f2de4661eD88A30C99A7a9449Aa84174
CollateralOnramp.wrap(USDC.e, 0x6da3e7Dd…, amount)   at 0x93070a847efEf7F70739046A929D47a521F5B8ee
```

Use USDC.e. Native USDC is paused on the onramp (`OnlyUnpaused()`).

Size it against the caps in `engine.env`: `PMENGINE_MAX_TOTAL_EXPOSURE=60`
cannot be reached on a wallet holding less than that in pUSD.

### Step 2.5 — decide the tick interval (OPEN ITEM)

`PMENGINE_TICK_INTERVAL_MS` is **not** in the shipped `engine.env`, and the
engine's own default is `1000`. The desktop never runs at that default: `pmt
engine start` injects `--tick-ms 50`, and the desktop's proposed unit hard-codes
`Environment=PMENGINE_TICK_INTERVAL_MS=50`, because updown's latency model is
built around 50ms — at 1000ms it runs 20x slow and silently mis-prices.

As installed, the EU unit will therefore start updown at **1000ms**. That is a
deliberate non-decision, not an endorsement: the env skeleton was specified as
an exact six-line file and this var was not in it. Before Step 3, either

```sh
echo 'PMENGINE_TICK_INTERVAL_MS=50' | sudo tee -a /home/ec2-user/.pmt/engine.env
```

or add `Environment=PMENGINE_TICK_INTERVAL_MS=50` to the unit — matching the
desktop — **or** consciously decide the EU pilot runs slow-tick.

### Step 3 — start, and only then

```sh
sudo systemctl start pmengine        # NOT enable, until it has proven itself
sudo systemctl status pmengine
sudo tail -f /home/ec2-user/.pmt/engine/engine-systemd.log
```

`enable` is a separate decision and a later one. Read
`deploy/systemd/README.md` first: `Restart=on-failure` does not merely restart a
process, it brings **positions** back under management unattended, because the
engine recovers its arms from `~/.pmt/engine/arms-state.json` on startup.

### Stopping

`sudo systemctl stop pmengine` — SIGTERM, 30s grace, so the engine can cancel
its resting orders. Killing it harder leaves live orders on the book. Arms
survive a stop; `pmt crypto disarm` is the actual off switch.
