# Shipping pmengine to the EU box

The eu-west-1 box (t4g.micro, aarch64, SSM-only; instance id lives in the private runbook) runs
the **private** flavor of pmengine against the EU L0 wallet. This directory is
the whole ship path.

| file | what it is |
| --- | --- |
| `ship-eu.sh` | build → upload → install → smoke. Re-runnable. Never starts anything. |
| `pmengine.service` | the system unit, as installed at `/etc/systemd/system/` |
| `engine.env.example` | skeleton for `~/.pmt/engine.env` on the box. No secrets. |
| `redeem-sweeper.py` | turns settled winners back into collateral. See *Auto-redeem* below. |
| `pmt-redeem-sweeper.{service,timer}` | the sweeper's units. Ten-minute timer. |
| `fixtures/` | four frozen Polygon transactions the sweeper's tests are argued from. |

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

---

# Auto-redeem — `redeem-sweeper.py`

## Why the box needs it at all

A deposit-wallet (sig-type-3) account books a win as **CTF outcome tokens, not
cash**. Nothing spends those tokens. So the engine's collateral only ever goes
*down* while it trades, and a night of wins walks the balance under the clip
size and freezes it. That is not hypothetical: on 2026-08-24 the EU box won its
way to a frozen engine twice, and $133.66 + $50 came back by hand.

The sweeper closes that loop on a ten-minute timer. It redeems only settled,
resolved, on-chain-verified holdings, and — the part that matters — it **asserts
that the redemption paid**.

## The two paths, and the evidence for each

Both were read off real Polygon transactions before a line of this was written.
All five are frozen or cited below; the three receipts live in `fixtures/`.

**A — CTF direct, `redeemPositions(USDC.e, 0x0, conditionId, [1,2])` at
`0x4D97DCd97eC945f40cF65F87097ACe5EA0476045`.**

| tx | block | what it proves |
| --- | --- | --- |
| `0xe03d7e5c…0780` | 92553413 | nine `PayoutRedemption` events, collateral **pUSD**, **payout 0 each** — the L43 incident. The transaction *succeeded*. |
| `0xce642535…4b10` | 92553626 | the same nine conditions with collateral **USDC.e**: payout $133.66, and nine USDC.e `Transfer`s from the CTF into the wallet. |
| `0xc79ea1ff…b1a8` | 92554771 | three more, $50.00, same shape. |

Position ids are `keccak(collateral, collectionId)`; our tokens were minted
against USDC.e, so the pUSD parameter derives ids we hold none of and the CTF
happily pays out zero of them. The payout lands as **raw USDC.e** in the
deposit wallet, and Polymarket's own sweeper wraps it to pUSD 30–60 minutes
later. Until it does, the engine cannot see the money — at the time of writing
$50.00 of USDC.e from the recovery batch has sat unwrapped for over an hour
while the wallet shows $1.38 of pUSD.

**B — the adapter, `redeemPositions(pUSD, 0x0, conditionId, [1,2])` at
`0xAdA100Db00Ca00073811820692005400218FcE1f`.** Same selector `0x01b7037c`,
different target, and the pUSD parameter is correct *here* because the adapter
translates it.

| tx | block | what it proves |
| --- | --- | --- |
| `0x1c4618a2…ee71` | recent | the call shape, decoded out of a third party's relayer WALLET batch: target = adapter, `01b7037c` + pUSD + `0x00…0` + conditionId + `[1,2]`. |
| `0xc627b919…41d5` | 92558443 | the money. In ONE transaction: CTF `PayoutRedemption` with redeemer = **adapter**, collateral = **USDC.e**, payout 16.563372 → adapter sends 16.563372 USDC.e to the pUSD contract → pUSD **mints 16.563372 straight to the end user**. 1:1, no fee, no wrap wait. |

`0xc627b919…41d5` also shows the prerequisite: its log 0 is an
`ApprovalForAll(user → adapter)` on the CTF **in the same batch**, because the
adapter pulls the ERC-1155 to itself (`TransferBatch` user → adapter) before it
redeems. The EU wallet has not granted it: `isApprovedForAll(0x6da3e7Dd…,
0xAdA100Db…)` reads `0` today.

## What ships, and why

**Both, with the safe one as the default.** The path is chosen by
`isApprovedForAll` at runtime:

* no approval → **CTF + USDC.e**, the path already proven to pay *us*, with the
  wrap wait as its cost.
* approval present → **adapter**, one transaction, pUSD immediately spendable.

Granting a blanket ERC-1155 operator right over every position the wallet will
ever hold is a standing risk and a human's decision, not a timer's — so the
sweeper never grants it on its own. `--grant-approval` prepends the
`setApprovalForAll` to the *same batch* as the first redemption (exactly as the
third-party batch did), which makes it atomic: an approval can never outlive a
redemption that failed. After that one run, `auto` picks the adapter forever.

The recommendation is to run `--grant-approval` once during the first supervised
run and let the fast path take over. Until then the box still redeems, still
never strands, and just waits on the wrap.

Because the grant rides *with* a redemption, running `--grant-approval` on a
wallet with nothing to redeem grants nothing. The run says so
(`grant_deferred` in the idle record) rather than leaving you believing the
fast path is armed — repeat it on a run that has winners.

## Flow

1. **Enumerate.** `data-api /positions` for the wallet → keep rows flagged
   `redeemable` with `currentValue > 0`, group by `conditionId`, drop
   negative-risk (its redemption is a different call and would revert the
   batch). Held losers mark at 0 and are left alone on purpose — burning them
   would put a *legitimate* payout of zero into the same batch as the winners
   and make step 5's assertion unusable.
2. **Gate on settled.** `gamma /markets?condition_ids=…&closed=true`. The
   `closed=true` is mandatory, not decoration: without it gamma returns nothing
   at all for a settled market. Only `closed` markets go on.
3. **Verify on-chain — data-api is a candidate list, never truth.** Its
   `redeemable` flag went on showing the paid-zero conditions as redeemable
   after the fact. For each survivor the chain has to agree three times: CTF
   `payoutDenominator(conditionId) > 0` (resolved *at the CTF* — an unresolved
   condition reverts the whole atomic batch and strands every other winner in
   it), `balanceOf(wallet, tokenId) > 0` for a token from gamma's
   `clobTokenIds`, and `payoutNumerators(conditionId, index) > 0` for a token
   we actually hold. That last read is what makes the expected payout an exact
   number rather than a mark.
4. **Submit one batch.** Relayer nonce from `get_nonce(<L0 EOA>, "WALLET")` —
   note the *signer's* address, not the wallet's; the wallet's own address
   returns 0. Cross-checked against the wallet contract's on-chain `nonce()`
   and deferred one tick if they disagree (a batch of ours is in flight).
   Deadline is now + 900s; 240s was rejected "deadline too soon".
5. **Assert payment.** Pull the receipt from the chain, not the relayer's
   state machine. Decode `PayoutRedemption` keyed on `(redeemer, collateral)` —
   the redeemer of record is the wallet on the CTF path and the **adapter** on
   the adapter path — and read the ERC-20 credit into the wallet (USDC.e
   transferred from the CTF, or pUSD minted from `0x0`). A payout of zero, a
   missing event, a short payout, or a credit that does not match the payout is
   a **failure**, not a log line.
6. **Log one JSONL record** to `~/.pmt/engine/redeem-log.jsonl` — conditions,
   expected, paid, credited, tx hash, path, nonce.

Idempotent by construction: the candidate set is rebuilt from `balanceOf` every
run, so a redeemed condition cannot re-enter the next batch.

## What pages, and what does not

The unit is a `oneshot` with `Restart=no`. Its exit code IS the alarm.

| exit | status in the log | means | what it wants |
| --- | --- | --- | --- |
| 0 | `idle` | nothing verified redeemable | nothing. One line, quiet. |
| 0 | `redeemed` | batch landed and paid what was expected | nothing. |
| 0 | `dry_run` | `--dry-run` printed the plan | nothing. |
| 0 | `deferred` | relayer and chain nonces disagree | nothing; next tick. |
| 1 | `error` | data-api / RPC unreachable, missing creds, `--path adapter` without the approval | config or transport. Not money. |
| 2 | `error` | submit refused, no tx hash, receipt timeout, tx reverted | **the batch's fate is unknown.** Look at the tx. |
| 3 | `error` | `payout_assertion_failed` | **the L43 class**: it landed, it "succeeded", it paid nothing (or paid somewhere that is not us). Stop and read the receipt. |

`systemctl status pmt-redeem-sweeper` shows failed for 1, 2 and 3. The one that
must never be quiet is 3 — that is the exact shape of the incident this exists
to prevent, and a sweeper that logs it as a win is worse than no sweeper.

### The unwrapped-USDC.e watch

Every run reads the wallet's raw USDC.e balance. If more than $10 sits
unwrapped for more than 45 minutes — Polymarket's sweeper has not run, so the
engine cannot spend its own winnings — the run's log record carries an
`unwrapped` note with the amount and the age. It does **not** try to wrap it:
the wallet→EOA leg needs a human go. On the adapter path this should never fire.

## Failure modes worth knowing before you enable it

* **One open window reverts nine winners.** Batches are atomic. Steps 2 and 3
  both guard it, from two independent sources, for that reason.
* **A stale relayer nonce.** Deferred, not forced. The candidates do not expire.
* **A duplicate batch.** Cannot arise from the timer — `balanceOf` is zero after
  a successful redeem. If one ever did, it would exit 3 (payout zero on a
  holding it thought it had), which is the correct place for it to land.
* **negRisk markets are skipped entirely.** The updown book has none. If one
  ever appears it shows up in the log's `skipped` list, redeemed by hand.

## Testing

`pmtrader/tests/test_redeem_sweeper.py` — 30 tests, no network, run by the
repo's own gate:

```sh
cd pmtrader && uv run pytest tests/ -q
```

The calldata encoder is pinned byte-for-byte against inner calls copied out of
`0xce642535…4b10` (ours, paying) and `0x1c4618a2…ee71` (the adapter form). The
payout decoder is argued from the three frozen receipts in `fixtures/`:
`receipt_pusd_payout_zero.json` is nine payouts of exactly zero and is the
negative fixture the whole assertion exists for, `receipt_ctf_usdce_paid.json`
is the $133.66 that actually landed, and `receipt_adapter_pusd_paid.json` is a
third party's $16.563372 minted as pUSD in one transaction.
`positions_eu_wallet.json` is a real data-api blob whose two dropped rows are
held losers, not open windows.

The submit path has no unit test and cannot have one — it needs the box's key
and the builder credentials. `--dry-run` is its substitute: it reads every
public feed, does every chain verification, prints the exact calldata it would
send, and touches no key file at all.

## Deploying it (orchestrator, via SSM)

The sweeper is **not** installed or enabled by `ship-eu.sh`. It needs its own
venv (which the box already has from the manual recovery) and its own decision.

```sh
# 1. upload redeem-sweeper.py to /home/ec2-user/pmt/bin/ (presigned URL, same
#    pattern as ship-eu.sh — the box has no S3 credentials and keeps none)
# 2. install the units:
sudo install -m 0644 pmt-redeem-sweeper.service /etc/systemd/system/
sudo install -m 0644 pmt-redeem-sweeper.timer   /etc/systemd/system/
sudo systemctl daemon-reload

# 3. confirm the venv can run it and the plan looks right. Reads no key.
/home/ec2-user/redeem-venv/bin/python \
    /home/ec2-user/pmt/bin/redeem-sweeper.py --dry-run

# 4. confirm the env var names — the sweeper accepts several spellings and
#    says which it tried if it finds none. This is the one thing that cannot
#    be verified from the desktop:
grep -o '^[A-Z_]*=' /home/ec2-user/.pmt/l0.env /home/ec2-user/.pmt/builder.env

# 5. ONE supervised run, in the foreground, on the CTF path:
/home/ec2-user/redeem-venv/bin/python \
    /home/ec2-user/pmt/bin/redeem-sweeper.py ; echo "exit=$?"
#    …or, to take the adapter path from the start (one-time grant, atomic):
#    …redeem-sweeper.py --grant-approval ; echo "exit=$?"

# 6. only then hand it the timer:
sudo systemctl enable --now pmt-redeem-sweeper.timer
systemctl list-timers pmt-redeem-sweeper.timer
tail -f /home/ec2-user/.pmt/engine/redeem-log.jsonl
```

Step 5 before step 6 is the whole ceremony. An unattended process that signs
wallet batches earns that by redeeming once where someone is watching.
