# CLAUDE.md

## Project Overview

**pmt** (Polymarket Trading Toolkit) - prediction market trading ecosystem:

- **pmtrader** (Python) - Client SDK, CLI, and Streamlit UI
- **pmproxy** (Rust) - Reverse proxy for Polymarket APIs
- **pmengine** (Rust) - HFT trading engine
- **pmstrat** (Python) - Strategy DSL and transpiler

This repo is PUBLIC. The live strategies (updown\*) and the characterization
fixtures live in PRIVATE **pm-trade/pmt-strategies**, mounted as a git
submodule at `pmengine/src/strategies/private/` — see "Private strategies"
below. A clone without the submodule still builds and tests green as the
**public flavor** (in-tree `example` strategy only, no `replay` subcommand);
with it inited you get the **private flavor** the live engine runs.

## Build & Test

Use `uv` for Python (not pip). Use `uv run` to execute, `uv sync` to install.

```bash
# Python (pmtrader, pmstrat)
(cd pmtrader && uv sync && uv run pytest tests/ -v)
(cd pmstrat && uv sync && uv run pytest tests/ -v)

# Rust (pmproxy, pmengine)
(cd pmproxy && cargo test)
(cd pmengine && cargo build --features ec2 && cargo test)
```

**The merge bar for anything strategy-adjacent is the PRIVATE gate** (submodule
inited; `PMENGINE_EXPECT_PRIVATE=1` makes a silently-public build FAIL in
tests/flavor.rs instead of passing on zero tests):

```bash
cd pmengine
PMENGINE_EXPECT_PRIVATE=1 cargo test --features ec2
cargo clippy --features ec2 --all-targets
cargo build --release --features ec2
```

## Private strategies (pm-trade/pmt-strategies submodule)

- Mount: `pmengine/src/strategies/private/` (updown\*.rs + `fixtures/`).
  `.gitmodules` has `update = none`, so plain and `--recurse-submodules`
  clones skip it; the one-time init is
  `git submodule update --init --checkout pmengine/src/strategies/private`.
  build.rs probes `private/updown.rs` (the FILE — an uninited submodule
  leaves an empty dir) and sets `cfg(private_strategies)`; the generated
  `strategies/mod.rs` gates every private item on it, so ONE committed
  mod.rs compiles in both flavors and module names stay
  `crate::strategies::updown` etc.
- **Worktrees**: `git worktree add` NEVER populates submodules. First thing
  in any pmengine worktree: run the init command above (per-worktree gitdirs
  are independent and safe). Without it every gate runs the PUBLIC flavor
  and a private-breaking change sails through — hence the EXPECT guard in
  the merge-bar gate.
- **Agents never stage `pmengine/src/strategies/private`** — no `git add -A`
  gitlink bumps. The operator reviews any gitlink diff explicitly. To change
  private code use `scripts/strategies-push.sh "msg"`: it commits + pushes
  pmt-strategies FIRST (push-order invariant: the submodule commit must be
  on GitHub before any pmt commit records its gitlink, or fresh clones/CI
  404), then stages the gitlink for the operator. Never rewrite pushed
  pmt-strategies history.
- `pmstrat transpile --all` refuses when the submodule is declared but not
  inited (it would drop updown from mod.rs); `--public` knowingly emits the
  public form for local iteration — NEVER commit its output, and make sure
  pmstrat is current (a stale install predating the split emits ungated
  decls that break public clones).
- CI: public pmt CI never touches the submodule and holds zero secrets —
  **never add `submodules: true` to any workflow in this repo**, and never
  add submodule checkout to publish-pmengine.yml (public release artifacts
  stay public-flavor). The private net is pmt-strategies' own CI: it checks
  out public pmt at `PMT_REF` (default master; point it at a branch for
  coordinated changes), mounts itself, runs the full private gate + fixture
  replay on push + nightly cron.
- **Restart checklist**: before any operator `pmt engine restart`/rebuild,
  `scripts/preflight-private.sh` must pass in the live checkout (submodule
  inited + gitlinked sha reachable on the private remote). A rebuild from a
  submodule-less checkout starts an engine where `run updown` fails — arms
  never load and the roll chain is dead.

## Architecture

```
pmtrader → pmproxy (optional) → Polymarket APIs
                                 ├─ clob.polymarket.com
                                 ├─ gamma-api.polymarket.com
                                 └─ polygon-rpc.com

pmstrat (Python) → transpile → pmengine (Rust) → execute
```

## Environment Variables

Stored in `.env` at repo root. Key variables:
```
PM_PRIVATE_KEY=0x...
PM_FUNDER_ADDRESS=0x...
PM_SIGNATURE_TYPE=0|1|2|3  # 0=EOA, 1=Poly Proxy, 2=GnosisSafe, 3=deposit wallet
PMENGINE_SERIES_ALLOWLIST=  # optional; see "Series partition" below
```

**Deposit wallets** (`3` / POLY_1271, the post-2026-05-04 account class): funder
is a CONTRACT that validates orders via EIP-1271, and the order signature is an
ERC-7739 wrapper rather than a bare ECDSA blob. Auth (L1 + L2) is unchanged.
See `docs/deposit-wallet.md`; the signing shape is pinned to the Python
reference by `pmengine/tests/poly1271_golden.rs`.

**Series partition** (`PMENGINE_SERIES_ALLOWLIST`): comma-separated slug
PREFIXES this engine may trade, e.g. `xrp-updown-5m,sol-updown-5m`. Two engines
under one operator account must never share a series — their orders sit on the
same book under the same wallet, so one can match the other's resting quote,
which is wash-trade shaped. Arms outside the list are refused by
`StrategyRuntime`; rolls and recovered arms are refused inside `updown` (a roll
chain re-arms itself without passing through the control plane). **Unset =
unpartitioned**, byte-identical to an engine that predates the guard, so the
desktop needs no change.

## Strategy Workflow

```
pmstrat (Python DSL) → transpile → Rust code → pmengine (execution)
```

Write strategies in Python, transpile to Rust for HFT performance.

## Infra & Deployment

AWS infra is *described* in `infra/pulumi/` (Python Pulumi, eu-west-1 pinned).
Current live state and decisions are tracked in `.infra/INFRA.md` (gitignored).

- **NEVER run `pulumi up`.** The state bucket was destroyed in the 2026 teardown;
  the stack has no state, so an apply tries to *create* resources that already
  exist. `pulumi preview` only, until the import described in
  `infra/pulumi/README.md` is done. Config changes go through the AWS CLI today.
- **pmproxy** — Lambda + Function URL in eu-west-1. The URL is `AuthType=AWS_IAM`:
  callers SigV4-sign (`pmtrader/polymarket/sigv4.py`) and must send
  `x-amz-content-sha256`. `PMPROXY_AUTH_ENABLED=false` — Cognito Bearer is retired.
  Code deploys via GitHub Actions (`.github/workflows/deploy-pmproxy.yml`) using
  OIDC-federated role `pmproxy-ci-deploy`: builds the Lambda zip, then
  `aws lambda update-function-code`. Trigger: `workflow_dispatch` or auto on
  `pmproxy-v*` release publish. CI only swaps the binary, never the config.
- **pmengine** — no live host. Releases via tag `pmengine-v*` produce GH artifacts only.
- **pmtrader** — Python package, no AWS deployment.

Tag-triggered workflows (`publish-pmproxy.yml`, `publish-pmengine.yml`, `publish-pmtrader.yml`) build + cut GitHub releases. `deploy-pmproxy.yml` handles Lambda deploys via `aws lambda update-function-code` — not Pulumi.

## Live trading ops (crypto up/down trigger)

- `pmt` works from ANY cwd — shim at `~/.local/bin/pmt` cds into pmtrader first (kills the "Failed to spawn pmt" class of error).
- Engine lifecycle: `pmt engine start|kill|restart|logs` — detached, pidfile + timestamped logs in `~/.pmt/engine/`, prefers `target/release`. Never launch it piped into `head` (SIGPIPE kills it).
- Two different "stops", two different exposure semantics: `pmt crypto disarm` retires the arms *inside* a still-running `updown` strategy (orders pulled next tick, roll chains dead, strategy stays loaded and re-armable). `pmt engine stop <strategy>` removes the strategy from the runtime entirely and now **unsubscribes its tokens too**, which releases the risk manager's exposure ledger — skipping that release is what used to freeze ghost exposure on a dead strategy. Neither one sells a position that already filled.
- Flow: `pmt crypto updown <url|slug>` prices a market (semantics auto-parsed from the description — TWAP vs close-vs-open); `pmt crypto arm <url> --size N [--side up|down]` hands params to the resident `updown` strategy; `pmt crypto trigger` shows its live eval (incl. committed/budget); `pmt crypto disarm` stops it and pulls its orders next tick.
- Arms ROLL by default: at window close the strategy re-arms the next window in the series itself (fresh budget, same gates; token ids fetched from gamma, retries hop windows on outage). `--no-roll` for one-shot. **Stopping the fleet**: `pmt crypto disarm` kills all arms AND their roll chains (orders pulled next tick); `pmt engine kill` stops the process (check `pmt orders` for strays after a hard kill).
- **What a poweroff actually leaves exposed**: the engine handles SIGINT *and* SIGTERM, so `pmt engine kill` and the nightly systemd poweroff both run graceful shutdown — resting orders get cancelled rather than left on the book. What that does NOT undo is **already-filled inventory**: any position the engine bought rides to resolution unmanaged, with no exits and no evacuation, because the process that would have run them is gone. Ceiling per window is the arm's `--size` (100–400 typical) built from `--clip`-sized fires (10–50); the exposure is whatever the arms had actually filled at the moment the box went down, not one clip. `pmt crypto activity` / `pmt crypto window <slug>` after a restart is how you find out.
- Manual momentum override: `--min-elapsed 0 --min-fair 0 --min-edge 0.005 --side X`.
- **Market-data source**: `pmt crypto arm ... --feed rtds` runs an arm off the Chainlink TWAP stream these markets *settle* on instead of the Binance proxy — reference, spot and TWAP marks all come off one series, so the cross-venue basis the guard was sized for disappears (twap markets only; close_open needs a venue's candle open, and is refused). One shared socket for the whole fleet, lazily opened by the first rtds arm; `pmt crypto trigger` / `watch` show its health and mark rtds arms `≈`. Default stays `binance` and nothing uses rtds until an arm asks for it. A dropped stream gates every rtds arm within `MAX_SPOT_AGE_S` (5s), same as a dead Binance feed.
- Durable eval/fire tape: `~/.pmt/engine/updown-tape.jsonl` — cross-session calibration data. Session scratchpads are tmpfs and die on the nightly poweroff; never leave data you want there.
- **Characterization fixtures** (`pmengine/src/strategies/private/fixtures/` — inside the pmt-strategies submodule since the private split, because a fixture embeds the arm's as-armed params; see its README): real wallet-graded windows frozen into the private repo — tape slice, book slice, the arm's own market data, as-armed params, the wallet's verdict — so `pmengine replay --fixtures src/strategies/private/fixtures` and `cargo test` (private flavor) regression-test the decision core offline, with no `~/.pmt`. Freeze a new one with `pmt crypto fixture <slug> --teaches '...'` (wallet-graded windows only; a chainlink/book label is refused). A fixture that starts failing is a FINDING: the engine changed behaviour on a real trade. Regenerating an expectation is `--regen`, one fixture at a time, justified in the commit message — never a way to get CI green.
- **Replaying a stream-fed arm**: `replay --mode full` follows the arm's `feed` param the same way `start_feeds` does. A `binance` arm reconstructs from cached 1m klines; a `feed=rtds` arm replays the RTDS recorder corpus (`--rtds-corpus`, default `~/.pmt/corpus/rtds`) back through `updown_rtds`'s own router into a real hub, so spot/per_min/closes/rho are shaped by the live code and cannot drift from it. Klines are never a stand-in: replay and the fixture loader both refuse a stream-fed window that has no corpus behind it rather than quietly answering off the wrong venue. Caveat worth knowing: the recorder is a SECOND subscriber to the stream, so its dropped samples are not the engine's — a window can replay as permanently gated on a missing reference print that the live arm did receive.
- Order-path tape: `~/.pmt/engine/order-latency-tape.jsonl` (Phase 7) — one line per decision, `stage` ∈ `ack` / `suppressed` / `fill`. Splits decision→ack into `sign_done_ms` / `send_ms` / `ack_ms` (cumulative offsets from build start) so the latency report names the stage instead of bounding it, and records the fires the delta matcher suppressed, which the engine log could only count by their absence. Joins to `updown-tape.jsonl` on `t` + `token`.
- **Policy eras** (`pmtrader/polymarket/eras.py`): the all-time record sums windows fired under policies that no longer exist, so `pmt crypto stats` cuts it at the deploy moments that actually moved it — pre-brake / brakes / theta / ws+scale / stream, each boundary an epoch cited to a commit, ROADMAP line or fixture `era` tag in a comment beside it. The "by era" table is in the DEFAULT view and lists every era, empty ones included; `--era <name>` scopes the rest of the report (per-symbol, effectiveness) to one era and still prints the whole table. All-time stays the headline ledger — an era is context, never a replacement. `--since` suppresses the table rather than showing a short one (it floors the wallet walk). Names deliberately match the fixture era vocabulary; boundaries are append-only and need a repo-citable moment.
- Engine gotcha: realtime taker fills are MISSED (`on_fill` doesn't run); the ~5s position reconcile is the truth. Any budget/PnL logic must read the position tracker, never fill events alone.
- **Trade journal**: `pmt crypto journal` appends the notable windows since the last run to `~/.pmt/journal/journal.md` — a PRIVATE location, never the repo. One terse line each for the day's biggest win and biggest loss, a `latched` brake that refused a side which then lost (priced through `polymarket.shadow`, one-clip materiality bar), the first window of a new symbol / the rtds feed / a resting maker bid / a maker fill, streak milestones (25/50/then every 50), and any size/clip change against `arms-state.json`. Grading reads the WHOLE book every run — a streak and a "first" are only true against all of it — and the floor only decides what gets WRITTEN. Idempotent by high-water mark + a set of emitted event keys, so a re-run or an overlapping `--since` backfill adds nothing it already said; `--since 0` is the full backfill, `--show` tails it, `--dry-run` prints without writing. It reads the outcomes corpus, never refreshes it (that's `pmt crypto outcomes` / `stats --gates`).
- **Print corpus** (`~/.pmt/corpus/prints/prints-YYYYMMDD.jsonl`): `python -m polymarket.prints` harvests Polymarket's public print tape for the windows in the engine's book tape, `--settle-lag` (180s) after each closes — data-api serves full history per market, so a post-close harvest keeps the exchange's own print timestamps at a fraction of a live poller's request budget. Rotated on PRINT time so a day joins `rtds-YYYYMMDD.jsonl` with no filter, and BOUNDED by `--retention-days` (30). Seeds from the daily files plus the legacy one-shot `prints.jsonl`, so a restart never re-fetches. Proposal unit `deploy/systemd/pmt-print-recorder.service` — a SIBLING of the rtds recorder, deliberately not folded into it (a blocking data-api call inside the stream reader's loop is the bug `analysis/watch_load.md` is about). `--once` runs one catch-up pass. The gap it closes: the old backfill stopped at 07:39:00Z on 2026-08-23, the rtds corpus starts 08:28:55Z, so print-vs-stream lead had zero overlapping data to measure on.
- **Spot corpus** (`~/.pmt/corpus/spot/spot-<venue>-YYYYMMDD.jsonl`): `python -m polymarket.spot` records the live exchange tick tape the Chainlink settlement feed is a *lagging function of*. The vault's `opponent_model.md` §1d splits the makers' ~3s lead over our feed into ~1.7s of our relay and ~1.3s of real information — they price the underlying spot, and no relay upgrade closes that half. `klines-1m-*.jsonl` cannot substitute: a 1m bar has no opinion about a 3s lead. **Both clocks on every row** (`t_recv` local, `t_exch` the venue's) and a message carrying no exchange stamp is DROPPED, not written with a null — a null there silently re-creates the ~1.7s lookahead that made `book_lead.md` report the lead backwards. Three venues, each its own thread + socket + file: **Binance via `data-stream.binance.vision`** (`stream.binance.com` answers HTTP 451 from this box; the `.vision` mirror is the same *global* book — Binance.US is a separate, thinner market and is not what the oracle follows), Kraken v2, and Hyperliquid for HYPE. Streams are `@trade` + `@ticker`, **never `@bookTicker`** — verified live to carry no exchange timestamp at all, and the stamped alternative (`@depth@100ms`) would need a REST snapshot and gap resync inside the reader loop, the blocking-call-in-a-stream-loop shape `analysis/watch_load.md` is about. `t_recv` is a **monotonic-anchored** wall clock so an NTP step mid-run cannot masquerade as market lead. Venue traps pinned in tests: Kraken v2 wants `BTC`/`DOGE` (its own REST reports the legacy `XBT`/`XDG` `wsname`s, which v2 rejects); Hyperliquid spot coins are `@{index}` (`@107` = HYPE/USDC, plain `HYPE` is the **perp**), and a readable spot name kills the whole connection with no close frame. `--minutes`/`--once` for bounded runs; `--kinds trade,book` selects the streams; **nonzero exit** if any venue got zero frames (1) or nothing parsed (2). Proposal unit `deploy/systemd/pmt-spot-recorder.service` runs **`--kinds book`**: the vault's §5b finds the 1 Hz quote correlates with the oracle as well as the full trade tape (btc r +0.9340 vs +0.9241) at ~1/130th of the rows, but peaking one second EARLIER in lag (k=+2 vs k=+3) because a 1 Hz snapshot is half a second stale. Equal information, one second less lead — the right trade for a corpus, the wrong one live, which is why the engine spec points at `@bookTicker` (fastest, and its missing stamp does not matter to something acting on arrival). That turns ~5 GB/day into under 100 MB/day; this recorder has no retention sweep, so the full trade tape should only ever be run BOUNDED. Analysis lives in the vault at `analysis/spot_lead.py`.
- **Wallet activity dump** (`~/.pmt/corpus/activity.jsonl`): `pmt crypto activity --refresh` is its ONLY writer — a full re-walk, never an append, because data-api rows mutate in place. The fixture freezer grades money off it and refuses (naming this command) a window the dump does not reach; a stale dump is what put $0 buy/redeem/pnl on seven fixtures of windows that really traded. Repair those with `pmt crypto fixture <slug> --accounting-only`, never `--regen` — a regen re-derives as-armed params from the LIVE arm store and will stamp today's config onto an old window.
- **pilot2** (`pmtrader/pilot2/`, `python -m pilot2`): the Strategy 2.0 interim pilot — a STANDALONE service, not part of pmengine and not a `pmt crypto` subcommand. Calibrated terminal physics (`predict.terminal_p_up`, a verbatim port of the vault's predictor spec, pinned bit-exact against frozen vectors) blended with the de-vigged book at a walk-forward weight, EV-gated at any price with no `min_fair`. **SHADOW by default**: prices btc/eth/sol/xrp-updown-5m and logs what it WOULD have traded to `~/.pmt/pilot2/shadow-tape.jsonl`; `pilot2 grade` scores those against gamma (`closed=true` pinned) and refits the blend weight. LIVE needs BOTH `--live` and `PILOT2_LIVE=1`, only on `PILOT2_SERIES` (default doge-5m/hype-5m/bnb-15m), and **refuses fatally (exit 2)** on any series an engine owns — the desktop's majors and the EU box's `bnb-updown-5m`. Risk law is hard-coded, no knobs: $40 total / $5 clip / 25 shares per window / **ONE clip per window-side ever** (RETROSPECTIVE §1.1 — 5+ clips is a −9.48% RoN business) / no entry in the final 30s / kill file `~/.pmt/pilot2/HALT` checked every loop. Positions ride to resolution and are queued in `redeem-queue.jsonl` for a MANUAL sweep (no relayer batch-redeem path exists). Proposal unit `deploy/systemd/pmt-pilot2.service`, shadow, not installed. See `pmtrader/pilot2/README.md`.
- **Fleet orchestrator** (`pmtrader/orchestrator/`, `python -m orchestrator` / `pmt-fleet`; design in `orchestrator/DESIGN.md`): cross-node health and the lease protocol behind automatic series failover between the desktop and the EU box. **Phase 1 is what is built and it places no orders, takes no lease and touches no arm** — `beat` writes a heartbeat (engine up, feed age, balance, series armed from `arms-state.json`) to the `pmt-fleet` DynamoDB table every 30s; `check` reads them back and exits 0/1/2/3 (nothing needs attention / something does / map refused / store unreachable-and-therefore-blind). The safety core: a series is traded only under an unexpired lease from that table, every mutation is a conditional `PutItem` bumping an `epoch` (so the double-claim race is decided by the CAS), a holder fences itself at `expires_at` and a claimant may not acquire until `fence + grace` where `grace ≥ one whole window + stop latency + 2× the 5s clock-skew bound. **Clock skew is measured, not assumed** — off the store's own `Date` header — and a node past the bound refuses to acquire and fences out of what it holds. The home/failover asymmetry is the store-outage answer: a HOME holder rides `home_extension_s` (600s) past expiry, a FAILOVER holder stops at expiry, and that cannot double-trade because a claim is a store WRITE — an outage can only remove a trader, never add one. A graceful SIGTERM releases the lease for instant handover and stamps the heartbeat `shutdown`, so **the nightly poweroff is a status note and never a page** (mirrors mubs' `worker_shutdown_clean`); a lease that expires un-released pages, through the `mubs-attention` Lambda or `--notify-cmd`. Kill switch: `disabled` on `pk=fleet` freezes failover claims (not renewals, not a home node's return) and reads as DISABLED when unreadable. Phase 2 (leases wired into `PMENGINE_SERIES_ALLOWLIST`'s existing refusal machinery) is specified in DESIGN.md §8 and NOT built. Proposal unit `deploy/systemd/pmt-fleet-doctor.service`; the EU box's IAM policy is `deploy/eu/pmt-fleet-doctor-policy.json`, **proposed and not attached**.
- **Regime gauge** (`pmtrader/polymarket/regime.py`, `pmt crypto regime`; the dark sizing hook is `docs/regime-gauge.md`): leader persistence — of the windows where the book had a leader at elapsed 0.25, how often it went on to win. The vault's `underdog_search.md` §5 found that number moved 79.7% → 71.5% (z 3.12) inside 24 hours and that when it moved, the dog/favourite bias INVERTED in elapsed [0.00, 0.25); a binary's price band IS a volatility position and the fleet takes it blind. **It is MEASUREMENT ONLY — no model, no fills, no ledger, and it gates and sizes NOTHING.** Definition frozen in `regime.METHOD` (first snapshot at elapsed ≥ 0.25; BOTH half-books quoted within 1000ms or the window is excluded and the mark does NOT slide forward; de-vigged mid; a leader needs `|dv − 0.50| > 0.05`; terminal grades only). Reads every book tape on the box (live + the frozen `*book-tape*.jsonl` archives) joined to the outcomes corpus — it never refreshes that corpus, which is `pmt crypto outcomes`' job. Writes ONE file, `~/.pmt/corpus/regime.jsonl`: one row per resolved window carrying the gauge as of that window, series- and fleet-scoped, idempotent by slug (`--rebuild` re-cuts it, the only correct move after a METHOD bump; `--out` redirects; `--dry-run` prints only). `pmt crypto watch` shows the fleet gauge as one header row and DROPS it on a box that has never run the estimator. **Two caveats the report always prints and you must always read**: the outcomes corpus lags the book tape (8h40m at the first backfill, 612 windows pending), and wallet-graded windows read 92.5% against resolution-graded 76.3% (z 4.73) — a wallet grade exists because we TRADED that window, and wallet rows grade FIRST, so the recent end of the gauge is its most selected end.
- **Swallowed-error log** (`pmtrader/polymarket/errlog.py`, `pmt crypto errors`): the belts stay, the silence goes. Most `except` handlers here are correct — a torn tape line must not take the dashboard down — but they were also mute, and `scoreboard: AttributeError` in the watch header was the ENTIRE record of a real failure: no site, no message, no traceback, no count. `errlog.note(site, exc, **ctx)` keeps the belt and leaves a mark: the FIRST occurrence of each (site, exception type) prints a full traceback to stderr and writes one JSONL line to `~/.pmt/engine/swallowed-errors.jsonl`; the rest are counted and written on a power-of-two schedule, so a storm escalates visibly and a site at n=4096 can never look like one that blinked. It NEVER raises (a read-only `~/.pmt` loses the mark, not the caller), the file is size-capped and rotated one generation, and `PMT_ERRLOG_PATH` / `PMT_ERRLOG_STDERR` redirect and mute it (`watch` mutes stderr — it owns the terminal, and the marks are for reading afterwards). ~30 sites are instrumented, prioritised money/grading, the tape readers and the watch fetch threads; genuinely-cosmetic silences carry a `LEGITIMATELY SILENT` comment instead. Read it with `pmt crypto errors` (aggregate, worst first), `--trace` for the kept frames, `--tail N` chronologically.
- **Corpus backup**: `scripts/pmt-backup.sh` ships `~/.pmt/corpus` + `~/.pmt/engine/*.jsonl` + `arms-state.json` to `s3://xanmc/pmt-backups/YYYY-MM-DD.tar.zst`, one object per day, skipped if the day is already up. Rotated engine logs are excluded on purpose. Proposal units in `deploy/systemd/` (`pmt-backup.service`/`.timer`, daily 03:30, `Persistent=true` so a missed run fires on the next boot) — see that README before installing. `--dry-run` shows the exact member list; the recorded stream is the one thing here that cannot be rebuilt.
