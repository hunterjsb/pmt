# Regression sweep — the 2026-08-23 ship day

Read-only audit of everything that landed on `master` / pmt-strategies `main`
after ~16:00Z on 2026-08-23. Nothing here was fixed; every line is a finding
with an evidence trail, or a verified-clean negative.

Baseline sanity, both suites green at `4dd28cd` + submodule `c8b0e53`:

| suite | command | result |
|---|---|---|
| pmengine (private flavor) | `cargo test` in `pmengine/` | **exit 0** — 277 + 5 + 6 + 5 + 1 + 1 + 1 passed, 0 failed, 2 ignored |
| pmtrader | `uv run pytest tests/ -q` in `pmtrader/` | **exit 0** — 772 passed, 7 deselected (`-m 'not network'`) |

> Running bare `pytest` at the repo ROOT collects **0 tests and exits 5** —
> root `pytest.ini` has `testpaths = tests`, and `tests/` now holds only
> `fixtures/gamma_response.json`. CI is unaffected (its `test` job sets
> `working-directory: pmtrader`), so this is a local-developer trap, not a
> CI hole. Listed under LATENT.

---

## The ranked table

| # | rank | finding | one-line evidence |
|---|---|---|---|
| 1 | **CRITICAL** | The scoreboard silently drops every `4h` window — the ledger of record understates all-time updown realized flow by **+$164.35** and 6 wins | `SLUG_RE` requires a `m` duration token, `is_updown()` does not; corrected parser reproduces the raw wallet walk exactly (−$401.40, 263W-23L) |
| 1b | **CRITICAL — cause unresolved** | 5m taker fire rate collapsed 11× post-restart (0.31/min → **0.027/min**, 1 fire vs 11.4 expected, Poisson P≤1 = 2.5e-4); the `safety` brake now catches **74%** of positive-EV sides vs 50%/52% before | evidence currently leans **regime, not regression** — `sol-5m`'s config did not change and it collapsed too (0/33 unbraked), and measured σ rose (eth-15m `sig_bp` p50 5.32→9.52) with `abs(margin_bp)` p50 2–3×. **Not resolvable on 37 min — run the replay A/B against pre-ship safety constants.** Do not disarm on this alone |
| 2 | DEGRADED | `pmt` master carries a permanently-red commit `6cd24a8` (settle-width shipped ahead of its fixtures) | `gh run view 32661239407` → job `test` failed, `assert 60 == 30 where 60 = ck_settlement_width_s(300)`; green again 2m17s later at `9fa85da` |
| 2b | DEGRADED | rtds reconnect rate is the worst of the three runs — 3 in 37 min (**0.081/min** vs 0.073 / 0.043); each blinds the RTDS health emitter for 26–55 s and gates all three rtds arms | all `err=stalled 31s with the socket open`, backoff 1→2→4 s; health-line gaps of 105.0 / 86.2 / 114.8 s land exactly on the three reconnects (the health task shares the thread) |
| 2c | DEGRADED | The restart landed **mid-window**, so all three rtds arms sat out the entire 20:45–20:50 market (4m43s) waiting on a range-start reference | 53 consecutive `range-start reference not printed yet` records per rtds arm, `rem_s=284.3` at re-arm; every later roll acquired in ≤5 s (1 record per boundary). Self-healing — but **restart on a boundary** |
| 3 | DEGRADED | `analysis/watch_load.md`'s reproduction appendix is now unrunnable — it greps a log line that no longer exists at any log level | `watch_load.md:435,438` grep `"Strategy command handled"`; `engine.rs:1651` is now `debug!` and dropped the `reply=` field, `main.rs:16` defaults to `info` |
| 4 | LATENT | Replay's `TunablesOverride` defaults `decided_k` to the FLAT 1.0, not `Tunables::law(dur)` — a partial override silently un-applies the new 15m law | `replay.rs:110` `default_decided_k() = Tunables::default().decided_k`; `carveout_ab.py` variants `m3/m5/m10/stale30/stale600` and `r7_fleet_ab.py:48 LIFTED` all omit `decided_k` |
| 5 | LATENT | pmt-strategies CI has not run against today's final pmt master — the public/private pairing is unverified by 4 commits | last private CI checked out pmt `8b3ae77` at 20:42:44Z; master HEAD is `4dd28cd` at 20:48:16Z |
| 6 | LATENT | `PMT_REF: master` floats, so a private-side push that beats its public counterpart red-lights on a stale pairing — it already fired today | run `32663104356` failed `cannot find series_guard in crate`; it checked out pmt `0882558`, and `series_guard.rs` landed 7s later in `8ae3ea2` |
| 7 | LATENT | Reconcile now swallows a failed fetch for the WHOLE pass, silently — blast radius went from 1 token to all 14, with no log either way | `engine.rs:971` `if let Ok(all) = …` has no `else`; a persistently failing positions endpoint disables the MAX_POSITION safety net invisibly |
| 8 | LATENT | 16 of today's master commits had CI cancelled by `cancel-in-progress` — "green on all pushes" is true at burst granularity, not per-commit | `ci.yml:9-11` `concurrency: group: pmproxy-${{ github.ref }}`; today's tally `{cancelled:16, failure:1, success:183}` |
| 9 | LATENT | Published `pmengine-v0.5.0` is already ~2h behind master — no `series_guard`, no POLY_1271 goldens, no `settle_tw_s` override, while `Cargo.toml` still says `0.5.0` | `git diff --stat 5b4bf34 origin/master -- pmengine/` → 12 files, 976 insertions |
| 10 | LATENT | Bare `pytest` at repo root collects 0 tests, exit 5 | root `pytest.ini` `testpaths = tests`; `tests/` holds only `fixtures/gamma_response.json` |
| 11 | LATENT | Orphaned `~/.pmt/engine/ledger-drift.jsonl` (37.9 KB) is backed up to S3 nightly forever; its writer `_log_ledger_drift` was deleted today | `scripts/pmt-backup.sh:79-81` globs `-name '*.jsonl'`; zero repo references to the file remain |
| 12 | LATENT | `analysis/latency_report.py:1457` reads `analysis/net_probe_raw.json`, deleted + gitignored today | guarded by `if npath.exists()`, degrades to "n/a"; but `latency_report.txt:496` ships measured rows a re-run can no longer reproduce |
| 13 | LATENT | ~70 surviving citations of `docs/LESSONS.md`, which went private today | resolves only in the live checkout via an untracked symlink to `pmt-alpha/docs/LESSONS.md`; dangles in every worktree |
| 14 | LATENT | A user-facing engine error names `pmt crypto rtds record`, a command that does not exist (pre-existing, not today) | `replay/rtds.rs:65`; `cli_crypto.py:41-46` registers 14 commands, `rtds` is not one — the real path is `python -m polymarket.rtds` |
| 15 | LATENT | **EU box:** the bnb arm is sized *above* its own risk cap — `size_usdc=50` against `MAX_POSITION_SIZE=30` | `arms-state.json` on `i-0426f1d5e68cdee60` vs `Risk limits configured max_position_size=30 …` at 20:46:08; clips are $10 so early fills pass, then the risk manager starts rejecting |
| 16 | LATENT | **EU box:** `pmengine.service` is `disabled` — and bnb now has exactly one home, so a reboot leaves it trading nowhere | `systemctl is-enabled pmengine` → `disabled`; deliberate per runbook, but its meaning changed when the fleet partitioned |
| 17 | LATENT | **EU box:** a dead balance poller would be invisible — failures log at `debug!` under an `info` engine, and success logs only on change | `engine.rs:648` (debug on failure), `:643` (`if *w != b`); closed here by reconciling to chain truth, but the observability hole is real |
| 18 | LATENT | **EU box:** no log rotation (~2.5 MB/day to a single file); desktop rotates, EU does not | unit appends to one `engine-systemd.log`, 59 KB in 34 min, 11 GB free |
| 19 | LATENT | `spot_age_s` in the updown tape is mostly null and sometimes carries an absolute timestamp instead of an age — **pre-existing, both boxes** | EU 319/325 null; desktop 4904/5000 null with non-null values mixing real ages (6.1–7.9 s) and unix timestamps (1787516405.25) |
| 20 | LATENT | Demoting `Strategy command handled` to `debug!` removed the exact metric `watch_load.md` used to *prove* a control-plane blackout — the next one will be invisible from the log | run C logs 0 of those lines vs 2298 / 1927 in the prior runs; the served/min + gap-percentile series is no longer computable. Needs an INFO-level counter to replace it |
| 21 | LATENT | The stale-cancel storm is 8× smaller but **not fixed** — same order id refused 3× in 0.14 s, each a CLOB round-trip inside the tick arm | 46 warns/order (run A) → 5.9 warns/order (run C); `watch_load.md` §2.4 already called this "a second, independent bug worth its own ticket" |
| 22 | LATENT | 15m arms have not fired since **07:40:35Z** — `min_fair=1.0` with `pay_up_max=0.0` makes them near-unfireable | **NOT** caused by `decided_k`: the last 15m fire predates the law by ~13 h. Their evals went *up* (145→436→712 matched-window) |
| 23 | LATENT | Sub-minimum order sizing: engine computed a **$0.80** clip against the CLOB's $1 minimum and the order was rejected | `20:24:13.927210Z` `Order execution failed … {"error":"invalid amount for a marketable BUY order ($0.8), min size: $1"}`; one occurrence, run B |
| 24 | LATENT | rtds subscribes 8 symbols to serve 3 consumers (~5/8 of 8.1 ev/s discarded) | **not a leak** — it was `rtds_symbols=8` at `rtds_consumers=1` too; fixed subscription set, wasted bandwidth only |

---

## 1. CRITICAL — the scoreboard is missing $164.35 and 6 wins

**This is the ledger-of-record question, and the raw wallet walk is the one
that is right.**

The operator's independent walk (data-api `/activity`, redeems + sells −
buys, strict updown filter) was reproduced exactly: **1,603 updown rows,
−$401.40**. The scoreboard on the *same frozen row set* returns **−$565.75,
257W-23L**. The whole $164.35 gap is one mechanism.

### The mechanism

`pmtrader/polymarket/updown_slugs.py` holds two membership tests that
disagree:

```python
SLUG_RE = re.compile(r"^([a-z]+)-updown-(\d+)m-(\d+)$")   # line 14 — requires 'm'
def is_updown(slug): return "-updown-" in slug             # line 61 — accepts anything
```

`score_activity` filters rows with the loose one and then details windows
with the strict one, dropping the difference on the floor with no counter,
no warning, and no drop list:

```python
parsed = updown_slugs.parse(slug)
if parsed is None:
    continue   # cli_crypto_stats.py:272 — "defensive; upstream already filtered"
```

For a `4h` slug the duration token is `4h`, `SLUG_RE` refuses it, and the
window vanishes from the money line.

### The six windows

All six are real, all six are settled with paying REDEEM rows, all six grade
as WINS (`redeemed_usd > 0.5` → `grade_window` returns at step 1):

| slug | window start (UTC) | buy | redeem | pnl |
|---|---|---|---|---|
| `eth-updown-4h-1767387600` | 2026-01-02 21:00Z | 37.35 | 45.00 | **+7.65** |
| `sol-updown-4h-1768856400` | 2026-01-19 21:00Z | 32.62 | 30.30 | **−2.32** |
| `sol-updown-4h-1779537600` | 2026-05-23 12:00Z | 134.96 | 139.00 | **+4.04** |
| `eth-updown-4h-1779753600` | 2026-05-26 00:00Z | 200.08 | 202.00 | **+1.92** |
| `btc-updown-4h-1779825600` | 2026-05-26 20:00Z | 903.35 | 1003.00 | **+99.65** |
| `btc-updown-4h-1787414400` | **2026-08-22 16:00Z** | 205.59 | 259.00 | **+53.41** |
| | | | | **+164.35** |

The last one is from **yesterday** — this is not purely ancient history.

### It is a regression, introduced today at 02:23Z

Before `92215b5` ("pmtrader: consolidate wallet/slug/tape duplication, split
crypto CLI", 2026-08-23 02:23:14 -0400) the scoreboard filtered on the loose
test only and parsed inline:

```python
if "-updown-" not in slug or _slug_window_start(slug) < floor:   # old cli.py:2002
    continue
...
parts = slug.split("-"); sym, dur = parts[0], parts[2]           # old cli.py:2036
end = int(slug.rsplit("-", 1)[1]) + int(dur[:-1]) * 60
```

The old code **counted these windows correctly**. Its `end` was wrong for
them (`4h` → `int("4")*60` = 240s instead of 14400s), but `end` is only
consulted by the grace/gamma branch, which a paying redeem short-circuits —
so the money and the W/L were right. Today's consolidation traded a wrong
`end` on a counted window for a correct `end` on a dropped one.

Corroborating that `4h` is a first-class series, not junk: the pre-split
CLI's own help text used `btc-updown-4h-1779825600` as its worked example
(old `cli.py:324`), and `analysis/fourh_fit.py` / `fourh_fit.md` /
`fourh_book_snapshot.py` are a whole research lane on it.

### Proof of the corrected number

Monkeypatching only the parser in-process (repo untouched) to accept
`(\d+)([mhd])`:

```
WITH 'h' DURATIONS PARSED:  net = $-401.40   W=263 L=23 riding=0 estimated=0
BASELINE (shipped parser):  net = $-565.75   W=257 L=23
DELTA: $164.35, +6 W, +0 L
RAW WALLET FLOW (independent walk): $-401.40      MATCH: True
```

The corrected scoreboard reproduces the independent wallet walk **to the
cent**. (Cosmetic follow-on: `series_key` does `dur_s // 60`, so the bucket
would label itself `btc 240m`.)

### Blast radius

`_tape_scoreboard` is documented as "THE acquisition path — every consumer of
the graded record calls this one: `stats`, `watch`, `journal`." All three
inherit the blind spot. `polymarket/positions.py:current_odds` filters with
`is_updown` only, so the watch **odds lane will happily mark a 4h position
the scoreboard refuses to count**.

### The itemized bridge

Every line named, zero residual:

| line | $ | how it was established |
|---|---|---|
| Raw wallet-flow walk — 286 updown windows, 1,603 rows | **−401.40** | frozen snapshot, `redeem + sell − buy` |
| (a) resolution-graded wins whose redeems have not posted | **0.00** | `riding_n = 0`, `estimated = 0` — there are no imputed windows at this instant |
| (b) CONVERT / MERGE / SPLIT rows a naive walk misses | **0.00** | updown row types are exactly `TRADE 1224` + `REDEEM 379`; zero others exist |
| (c) offset-pagination drops/dupes on the mutating feed | **0.00** | robust walk (limit 500 / step 400 + `row_key` dedupe) and naive walk (500/500) both returned **2,410** rows |
| (d) resolution-then-wallet double-count | **0.00** | `W + L = 257 + 23 = 280` = the exact count of gradable slugs, so every window contributes once; and pnl never comes from the corpus at all |
| **(e) 6 `4h` windows dropped by the `is_updown`/`parse` asymmetry** | **−164.35** | table above |
| Scoreboard net | **−565.75** | `score_activity(rows, 0.0)` |

On (a) specifically, since it was asked for explicitly: the set of windows
counted W with no redeem row is **empty right now** — `estimated = 0` means
`_impute_win_pnl` did not run on any window, so there are no imputed values
to enumerate. That is a point-in-time reading (21:2xZ, quiet tape, one fire
in the current run); the mechanism is live and will produce entries after
the next win lands.

On (d), the stronger structural statement: `score_activity` computes pnl
directly from wallet rows (`redeem + sell − buy`), never from
`outcomes.jsonl`. The corpus decides *nothing* about money. A
resolution-then-wallet double-count is not merely absent, it is
unrepresentable.

---

## 2. Class-by-class results

### Class 1 — semantics drift the fixture net cannot see (reconcile rewrite)

**CLEAN on the semantics, LATENT on the error path.**

`get_all_positions()` and `get_position()` build **byte-identical URLs**
(`https://data-api.polymarket.com/positions?user={funder}&sizeThreshold=0`)
and parse identically (`as_f64` → `Decimal::from_f64_retain` →
`unwrap_or(ZERO)` on both `size` and `avgPrice`). A token absent from the map
is skipped, exactly as `Ok(None)` was skipped. **An absent token cannot zero
a tracked position.**

Probed the real endpoint read-only to test the two ways a map could still
diverge from a scan:

```
HTTP 200, 30 rows
DUPLICATE asset keys (last-wins vs first-wins divergence): 0
rows with size == 0 (would ZERO a tracked position):       0
rows with negative size / missing asset field:             0 / 0
```

Both hazards are theoretical only on today's data: `out.insert` is last-wins
where the old scan was first-wins, and a `size: 0` row would reconcile a
tracked position to zero — but the endpoint emits neither.

**Drift-warning frequency is sane.** Raw counts collapse across the three
runs, but that is the tape, not the rewrite — normalizing against fires from
`updown-tape.jsonl` shows the rate per fill is unchanged:

| run | reconcile warns | fires (tape) | warns/fire |
|---|---|---|---|
| 17:31Z (pre-fix) | 47 | 42 | 1.12 |
| 19:35Z (pre-fix) | 18 | 21 | 0.86 |
| **20:45Z (post-fix)** | **1** | **1** | **1.00** |

**The reconcile demonstrably still runs post-fix** — `2026-08-23T21:17:08Z
WARN pmengine::engine: Position reconcile: corrected drift from missed
fill(s)`, on the current run's single fire.

The one real change is blast radius (finding #7): `if let Ok(all) = …` with
no `else` means one failed fetch skips all 14 tokens instead of one, and
neither version logs the failure.

### Class 2 — anything that depended on what we removed

**CLEAN, with one dead-documentation casualty.**

- `"Strategy command handled"` → **0 hits** in `scripts/` (all 3), `deploy/`
  (all 9 units/timers), `~/.pmt` (contains no scripts at all), CI workflows,
  and the private `pmt-alpha` runbooks. The only consumer anywhere is
  `analysis/watch_load.md`'s own reproduction appendix (finding #3).
  pmtrader never scrapes the engine log — it speaks HTTP to
  `127.0.0.1:7531` via `pmtrader/engine.py`. The one log-touching command,
  `pmt engine logs` (`cli.py:1339-1349`), filters `' Tick '`, and that
  format still matches the live log.
- `ActivityLedger`, `activity_ledger`, `_log_ledger_drift`, `RESYNC_S`,
  `redeem_identity`, `build_risk_header` → **0 hits** repo-wide.
  Survivors that merely share the word "ledger" are unrelated and live:
  `shadow.py`'s shadow P&L ledger, the risk-manager exposure ledger in
  `position.rs:139`, the era report vocabulary, and today's new trade
  journal.
- `pmt crypto shadow` / `oracle` / `spot` → **no caller anywhere**; verified
  against the real CLI (all three exit 2, "No such command"). The only three
  in-repo hits are negative tests asserting their absence.
- Chip strip → all surviving `chip` hits are design comments and tests
  naming behaviour absorbed into `build_windows_table`; no deleted render
  function is called.
- Whole-tree dangling-import sweep: an AST walk resolving every
  `from X import Y` across pmtrader and `analysis/` returns **0 unresolved**;
  all 12 modules import; `pyproject.toml` `py-modules` matches disk.
- The private-strategies split itself is sound: `build.rs` probes the FILE
  `src/strategies/private/updown.rs` (not the dir, which an uninitialized
  submodule leaves behind empty); every `crate::strategies::updown*`
  reference lives inside `cfg(private_strategies)`-gated trees; zero
  references to the old `pmengine/fixtures/` path survive.

### Class 3 — grading changes vs history

**CLEAN, and provably so.**

The 30→60 settle-width change cannot have rewritten a single historical row,
because `ck_settlement_width_s` only ever grades `source: "chainlink"` rows
and **the corpus contains none**:

```
corpus rows: 1025  {'wallet': 256, 'resolution': 767, 'book': 2}
corpus rows with source=chainlink: 0
```

Wallet rows were never demoted, three ways:

- `merge_outcomes` overwrites only on a strict `source_rank` increase, and
  `wallet` is rank 3, the maximum — a demotion is unrepresentable.
- Empirically: of the 275 windows the wallet can grade right now, **0** carry
  a non-wallet corpus row, and **0** conflict on the winner.
- Adversarially: feeding a deliberately *inverted* `resolution` row for all
  256 wallet rows changed **0** of them.

The intended −$272 correction is therefore the only ledger movement from the
loss-grading work; nothing else silently shifted. Note the corpus covers only
`{'5m': 934, '15m': 91}` — `4h` windows never enter it either, because
`window_universe` and `extract_updown_slugs` both gate on the same strict
`parse_updown_slug` (finding #1's root cause, same asymmetry).

### Class 5 — release / CI truth

**CLEAN on the release; three findings on CI hygiene (#2, #5, #6, #8, #9).**

`pmengine-v0.5.0` is the PUBLIC flavor, proven three independent ways:

1. **Workflow** — `publish-pmengine.yml` has a single `actions/checkout@v7`
   with no `submodules:` key, no `git submodule update` step, and an
   explicit guard comment at line 56 ("NEVER add submodule checkout to this
   workflow"). `.gitmodules` additionally sets `update = none`.
2. **Run log** — `gh run view 32659876193 --log` line 47 `submodules: false`;
   lines 1120 and 1710 carry `warning: pmengine@0.5.0: private strategies
   absent — building public engine (example only)`, once per cross target.
   `grep private_strategies` over all 1,841 log lines returns nothing, so
   `cargo:rustc-cfg=private_strategies` was never emitted — and per
   `build.rs` those are mutually exclusive branches.
3. **The shipped binary** — `strings` on the released tarball:
   `strategies/private|updown_rtds|banked_decided|decided_k` → **0 hits**;
   no `replay` subcommand, matching the documented public shape.

The release body states it: *"**Public flavor** — private strategies not
included… it is not the binary the live engine runs."*

Both tags dereference to the same commit `5b4bf34`, both are non-draft, both
are ancestors of master, and versions match (`Cargo.toml` 0.5.0 + `Cargo.lock`
0.5.0; `pyproject.toml` 0.8.0 + `uv.lock` 0.8.0).

Zero-secret public CI holds: `ci.yml` and `lint.yml` contain **zero**
`secrets.` expressions; the only real secret lives in `deploy-pmproxy.yml`,
which has no `pull_request` trigger, so no fork PR can reach it. Verified
live against a real fork-shaped PR run (`32663892820`, success).

Today's tally across 200 runs: `{cancelled: 16, failure: 1, success: 183}`.
The single failure is finding #2, already self-healed. Current gitlink
`c8b0e53` == pmt-strategies `main` HEAD, and that pairing's CI is green
(run `32665245178`, job `private-gate`: `cargo test` with
`PMENGINE_EXPECT_PRIVATE=1`, clippy `-D warnings`, release build, fixture
replay under a shadow HOME).

### Class 6 — cross-change interactions nobody tested together

| interaction | verdict | evidence |
|---|---|---|
| series allowlist × roll recovery × arms-state | **CLEAN, and self-clearing** | local `PMENGINE_SERIES_ALLOWLIST=btc-updown,eth-updown,sol-updown,xrp-updown` correctly omits bnb. Durable-state recovery replayed the stale bnb arm once at `20:45:15.703830Z` and the guard refused it (one ERROR, exactly 1 in the run) — **and the next state write dropped it**: `arms-state.json` (mtime 21:27Z) lists 7 arms, `grep -o bnb` → **0**, `rolls: []`. The refusal left no half-state and will **not** recur on the next restart. *(Corrects an initial read that the ERROR would repeat every restart — the prune already happened.)* |
| settle-tw auto-raise × non-rtds arms | **CLEAN** (not the money bug it looks like) | the auto-raise is gated `feed == "rtds"`, so `sol-updown-5m` (binance, $400, maker) sits at `settle_tw_s = 0.0` → `settle_tw_secs(300) = 30`, the width `settle_width.md` refuted 6–0. **But** all 7 live arms are `settle_rule = "range_avg"`, and `eval_range_avg` never reads `settle_tw` — grep of its body for `settle_tw`/`terminal_lock`/`tw` returns zero. `settle_tw_for` is consumed only by `eval_hybrid` (`updown_model.rs:569`) and `eval_terminal` (`:613`). On a binance-fed range_avg arm the knob is genuinely inert. It becomes live the moment that arm moves to `hybrid`/`terminal` or to the rtds feed. |
| decided_k law — did it land, correctly scoped? | **CLEAN, verified live** | the tape does not log `k` but logs all three terms, so `abs(banked_bp)/cushion_bp` brackets it at the `banked_decided` boundary. Run C: btc-15m min-decided 1.2762 / max-undecided 1.0871; **eth-15m brackets 1.2492 < k ≤ 1.2589**; sol-15m 1.3712 / 1.1816 — all consistent with 1.25. sol-**5m** brackets 1.0408 / 0.9513 → k = 1.0. Runs A/B bracket btc-15m at 1.0. The law landed and is correctly duration-scoped |
| decided_k law × replay tunables overrides | **override wins, but see #4** | explicit values are honoured; the hazard is *omitted* fields — `TunablesOverride`'s serde default is the flat `Tunables::default().decided_k` = 1.0, not `Tunables::law(dur)` = 1.25 above 300s. So on 15m windows `base` (no override → `ArmState` builds `Tunables::law` at `updown.rs:545`) runs k=1.25 while `m3`/`m5`/`m10`/`stale30`/`stale600` run k=1.0 — those five are not single-knob A/Bs any more. **Today's carve-out verdict is NOT retroactively invalidated**: `Tunables::law` landed in `c8b0e53` at 16:42:35-0400, after the analysis commits (`86a6eb4` 16:25, `07c9af9` 16:21, `69c9eec` 16:11). The confound bites the next re-run. `r7_fleet_ab.py:48 LIFTED` has the same shape. |
| watch odds lane × PM_FUNDER_ADDRESS parsing | **CLEAN** | `cli_crypto_watch.py:189` uses `wallet.funder_address()`, which raises on unset rather than falling through to a clean-looking empty; `_odds_failed` clears odds to `{}` on any lane failure; the live `.env` line is a bare unquoted 42-char address with no inline comment. The only wrinkle is the 4h asymmetry noted in finding #1's blast radius. |

### Class 4a — the local fleet since 20:45:15Z

**No arm is dead.** All 7 arms evaluate, gate, and roll. Only ~25 minutes of
history, so fire counts prove nothing either way; eval/gate structure does.

Per-arm `eval / gated / fire`, from `~/.pmt/engine/updown-tape.jsonl`, over
**matched 37.0-minute windows** measured from each run's start (the tape
throttles to ~1 record per arm per 5 s, so read eval:gate *share*, not
absolute counts):

| arm | A 17:31–18:08 | B 19:35–20:12 | **C 20:45–21:22** |
|---|---|---|---|
| btc-15m | 26 / 393 / 0 | 151 / 267 / 0 | **243 / 188 / 0** |
| btc-5m *(rtds)* | 33 / 366 / 0 | 23 / 375 / 1 | **86 / 325 / 0** |
| eth-15m | 59 / 360 / 0 | 202 / 216 / 0 | **238 / 193 / 0** |
| eth-5m *(rtds)* | 179 / 220 / 5 | 167 / 231 / 5 | **85 / 326 / 0** |
| sol-15m | 60 / 359 / 0 | 83 / 335 / 0 | **231 / 200 / 0** |
| sol-5m *(maker)* | 61 / 338 / 3 | 44 / 354 / 0 | **85 / 326 / 0** |
| xrp-5m *(rtds, maker)* | 80 / 319 / 8 | 103 / 295 / 0 | **103 / 308 / 1** |
| **5m total** | 353 / 1243 / **16** | 337 / 1255 / **6** | **359 / 1285 / 1** |
| **15m total** | 145 / 1112 / 0 | 436 / 818 / 0 | **712 / 581 / 0** |
| bnb-5m | 225 / 836 / 3 | 3 / 744 / 0 | *(partitioned to EU — expected)* |

5m eval counts are statistically identical across all three windows (353 /
337 / **359**), so the fire collapse in finding #1b is **not** eval
starvation — it is the `safety` brake. `fleet_room` never hit 0 in run C
(min 440.9 of 500), so it is not exposure-cap starvation either — it *did*
hit 0.0 in run A.

**Control-plane latency: the reconcile fix is measurably in the running
binary.** Tick cadence against the configured 50 ms interval, parsed from
`Tick tick=N elapsed_ms=M`:

| run | ms/tick p50 | p90 | max | elapsed_ms p50 / max |
|---|---|---|---|---|
| 17:31Z (pre-fix) | 50.0 | **151.3** | **230.3** | 49 / 126 |
| 19:35Z (pre-fix) | 50.0 | 50.0 | 87.6 | 50 / 59 |
| **20:45Z (post-fix)** | 50.0 | **50.0** | **50.0** | 49 / 53 |

Run 1 shows the pathology the commit message describes — the loop running up
to 4.6× behind schedule, corroborating the "22% behind" claim (independently
re-measured at **61.15 ms/tick, +22.3%**, against `watch_load.md` §2.4's
61.2 ms / +22%: the two methods agree exactly). Run 3 has **zero
excursions**: p90 and max both sit on 50.0 ms. Confirmed live from `/status`
— `uptime_secs: 2226, tick_count: 44488` → **0.07% cumulative drift over 37
minutes**.

A 200-sample live probe of `GET /subscriptions` at 5 Hz, same methodology as
`watch_load.md` §2.3, shows the fix on the wire:

| | watch_load baseline | **now** | |
|---|---|---|---|
| p50 | 1.4 ms | **0.67 ms** | 2.1× |
| **p90** | **246 ms** | **0.98 ms** | **251×** |
| p99 | 442 ms | 110.55 ms | 4.0× |
| max | 455 ms | 126.31 ms | 3.6× |
| samples > 100 ms | 14.3% | **1.0%** (2/200) | 14× |

**The 9.6 s blackout class is gone**, and the residual shape is the
signature of the fix: the two slow samples sit at ~110–126 ms — the cost of
**one** whole-account fetch, not 16 serialised ones.

**The rtds gating profile is CLEAN — it matches the EU box independently.**
The three newly-rtds arms each show ~55 `range-start reference not printed
yet` + 16 `feed stale` gates in run 3, where the binance-fed arms show
neither. Normalized per window that is 6.9 reference-waits and 2.0
feed-stales; EU's bnb (also rtds, different box, different wallet) runs 6.7
and 2.2. Two independent boxes agreeing this closely means this is a
property of the rtds feed path, not a desktop regression.

Worth watching, not yet a finding: **`sol-5m` has produced no fire since
19:35Z** (~95 min), where run 1 gave it 8 in 124 min. Its gate reasons are
ordinary basis-guard rejections (`projected margin -2.1bp inside 10bp`), so
it is refusing a quiet tape rather than failing — but it is the maker arm
that changed least today and the only 5m arm still on binance.

RTDS health at 21:10Z: `rtds_connected=true events_per_s=7.5
last_event_age_s=0.1 reconnects=3 dropped_lagged=0 consumers=3 symbols=8`.
Book health: `ws_connected=true ws_tokens=14 books=14 from_ws=14
from_rest=0 never_fed=0`, `book_age p50=5ms p90=36ms max=36ms`.

### Class 4b — the EU box (bnb), via SSM read-only

Box `i-0426f1d5e68cdee60`, t4g.micro aarch64, eu-west-1. **No CRITICAL, no
DEGRADED.** ~34 minutes and 6 completed 5-minute windows of history — enough
to prove the loop mechanics and reconcile the balance, not enough to say
anything about fill rate.

- **Up when expected**: `active (running) since 20:46:08 UTC`, `NRestarts=0`.
- **PRIVATE flavor confirmed**: `pmengine list` prints `example` + `updown`
  (⇒ submodule compiled in); `grep -ac 'private strategies absent'` on the
  binary = **0**.
- **Allowlist permits bnb**: `PMENGINE_SERIES_ALLOWLIST=bnb-updown-5m` in
  both `engine.env` and the live `/proc/60895/environ`; **zero** refusal
  lines on EU.
- **The partition is proven in both directions with no overlap** — the
  decisive pair: desktop `20:45:15.703830Z ERROR … series
  'bnb-updown-5m-1787517900' is outside this engine's
  PMENGINE_SERIES_ALLOWLIST=[btc-updown,eth-updown,sol-updown,xrp-updown] —
  refusing`, desktop's last bnb tape row `20:45:00.089Z`, EU picks bnb up at
  `20:46:47Z`. ~92 s unattended, **no window where both engines held it**.
  Different wallets and signers on each side.
- **Rolled 6 for 6**, every boundary within 36 ms.
- **Evaluates and acquires its reference**: 325 tape rows (41 eval, 279
  gated, 6 roll, 6 cleanup), 58 rows per full window at ~5 s cadence,
  identical across all five complete windows. Gate mix 226 basis-guard /
  40 early reference-wait (clears) / 13 feed-stale.
- **No error spam**: 2 WARN, 0 ERROR in 34 min, one class (`RTDS
  disconnected — reconnecting`) at ~3.5/hr vs the desktop's ~4/hr of the
  same class. **No message class on EU that is absent on the desktop.**
- **Balance stable and reconciled to chain truth**: engine reports
  `182.315381` at 20:46 and still at 21:17; independently, runbook pre-wrap
  175.5 USDC.e (EOA) + 6.82 pUSD (deposit) = **182.32**, and both on-chain
  USDC.e balances now read 0 (wrap completed). Agreement to under half a
  cent. Gas funded (94.76 POL).

Two things that look like failures and are not: **zero fills across six
windows** is the *most likely* outcome — the desktop baseline says only 6%
of `bnb-updown-5m` windows produce any fire (3 of 47), so 0.94⁶ ≈ 69%; and
a one-off `/status` reading `books_from_ws: 0, books_from_rest: 2` is a
sampling artifact of the REST health poller (book ages were 23 ms, and all
34 minutes of 60 s `Book health` samples read `from_ws=2 from_rest=0`).

Also noted: stale redeemable dust on the EU deposit wallet
(`btc-updown-5m-1787511000`, size 5.08, `currentValue: 0`) predating the EU
engine and unrelated to bnb.

---

## Verified clean — the absence-of-findings list

- Reconcile map rewrite preserves `get_position`'s skip-on-absent semantics
  exactly; identical URL, identical parsing; an absent token cannot zero a
  tracked position.
- Real positions endpoint carries no duplicate `asset` keys, no `size: 0`
  rows, no negative sizes, no missing `asset` fields — neither of the two
  ways a map could diverge from a scan is reachable on today's data.
- Reconcile still runs post-restart, at an unchanged warn-per-fill rate.
- Nothing anywhere parses `"Strategy command handled"` or `reply=` out of the
  engine log except one analysis appendix.
- No dangling import, reader, or caller of `ActivityLedger`, the chip strip,
  or `pmt crypto shadow`/`oracle`/`spot`. 0 unresolved imports tree-wide.
- The settle-width change moved zero historical corpus rows (zero
  chainlink-sourced rows exist).
- Wallet rows are never demoted — by construction, empirically, and
  adversarially.
- No resolution/wallet double-count is representable: pnl comes from wallet
  rows only, and every gradable window is counted exactly once
  (257 + 23 = 280).
- Wallet pagination is not dropping or duplicating rows right now: robust and
  naive walks agree at 2,410 rows.
- No CONVERT/MERGE/SPLIT rows exist on updown markets at all.
- `pmengine-v0.5.0` is the public flavor (workflow + build log + binary
  `strings`), the release body says so, tags/versions/lockfiles are
  consistent, and public CI needs no secrets.
- bnb's local refusal left no half-state in `arms-state.json`.
- `settle_tw` is inert on the live fleet's range_avg arms.
- Replay honours an explicit `decided_k` override.
- The watch odds lane fails loud on an unset funder address.
- No live arm is dead: all 7 evaluate, gate, and roll since the restart; 5m
  eval counts are statistically identical to both prior runs (353/337/359).
- Maker bids ARE still resting on both maker arms — 0.27 post_only orders/min
  in run C vs 0.27 (A) and 0.14 (B), ack 150–180 ms, resting notionals
  matching the clips (sol ~49.5, xrp ~9.8).
- `decided_k` is empirically 1.25 at 15m and 1.0 at 5m, measured off the tape.
- Book/WS health: 60 s cadence exact and phase-locked, `ws_connected=true`
  27/27, **`never_fed=0` in every sample**, `book_age` p50 17 ms / p90 132 ms
  — better than run A, on par with run B. `ws_tokens=14` is correct for 7 arms.
- `rtds_dropped_lagged=0` across all three runs — no consumer lag anywhere.
- `fleet_room` never hit 0 in run C (min 440.9 of 500); it *did* hit 0.0 in A.
- No message class disappeared: the stale-cancel and reconcile classes that
  read as "gone" at 21:13Z both reappeared the moment order flow resumed at
  21:19:59Z — the zeros were the fire drought, not a broken subsystem.
- The engine is transacting: `/status` 21:19:27Z `open_orders:2,
  total_exposure_usd:19.31, unrealized_pnl:4.90`; balance +$5.00 with
  exposure cleared by 21:22:18Z.
- The bnb allowlist refusal fired once and self-cleared from durable state.
- The reconcile fix is in the running binary and works — tick cadence p90/max
  went from 151/230 ms to exactly 50.0/50.0 ms against a 50 ms interval.
- The rtds arms' reference-wait / feed-stale rate matches the EU box's
  independently to within 3%, so it is the feed's shape, not a regression.
- The bnb partition has no overlap window: desktop refused it at 20:45:15Z
  (last desktop bnb tape row 20:45:00Z), EU picked it up at 20:46:47Z.
- EU box is the private flavor, allowlisted for bnb, rolled 6/6 on the
  boundary, and its balance reconciles to chain truth within half a cent.

---

## Method / provenance

- Worktree `pmt-wt-regress` at `4dd28cd`, submodule `c8b0e53`. Nothing in the
  repo, `~/.pmt`, `.env`, or systemd was modified; the EU box was touched
  only through read-only SSM commands.
- The wallet bridge uses ONE frozen activity snapshot so both sides of the
  arithmetic read identical rows; the parser fix was simulated by
  monkeypatching in-process, never by editing the tree.
- Live-fleet numbers are a ~25-minute post-restart window (desktop) and a
  ~34-minute window (EU). Fire counts are not conclusive at that length and
  are not treated as such anywhere above.
