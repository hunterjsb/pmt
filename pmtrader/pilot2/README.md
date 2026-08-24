# pilot2 — the Strategy 2.0 interim pilot

A standalone service. **Not** part of pmengine, not a `pmt crypto` subcommand,
not sharing an arm store, a wallet path or a series with any running engine.

It is `strategy2/calibrated_model.md`'s shipping recommendation, live:

> `terminal_p_up` **blended with the de-vigged book, weight refit online** —
> never the raw model alone. On this corpus the blend is the only policy whose
> paper P&L excludes zero.

```
ref/spot/sigma/banked   RTDS, in memory    ->  terminal_p_up   (~20 flops, no state)
two asks + their sizes  CLOB REST, 2s      ->  devig
                                           ->  w*model + (1-w)*book
edge = p_side - ask - taker_fee(ask)       ->  fire iff edge >= 0.02
risk law                                   ->  a NAMED refusal, or one clip
```

`taker_fee` is `rate * p * (1-p)` per share — the schedule MEASURED off the
wallet (`polymarket/constants.py`), not the `rate * min(p, 1-p)` every path
used to subtract. It matters most here: the correction halves the charge at a
coin flip, and thesis B's whole claim is that the edge lives at mid price.
A resting fill pays zero, but nothing in pilot2 rests.

## What it does per decision

Shadow mode writes ONE line to `~/.pmt/pilot2/shadow-tape.jsonl` for each
would-be trade, carrying every input needed to grade it AND to re-fit the blend
weight later:

| field | why it is on the line |
| --- | --- |
| `slug` `series` `symbol` `start` `end` `elapsed_frac` | which window, and how far into it |
| `side` `token` | which outcome, and the tradeable handle |
| `ref` `spot` `spot_age_s` `sigma_s` `n_banked` | the four predictor inputs, so the p is reproducible offline |
| `model_p_up` | the model ALONE |
| `book_p_up` `book_up_ask` `book_dn_ask` `book_up_ask_sz` `book_dn_ask_sz` | the market's opinion and the depth behind it |
| `w` `w_source` `w_rows` | **the blend weight in force, on every line** |
| `blend_p_up` `p_side` | the estimator that actually decided |
| `ask` `ask_sz` `fee` `edge` `min_edge` | the cost model, term by term |
| `shares` `notional` `capped_by` | the clip, and WHICH cap shaped it |

The model and the book are stored **separately** as well as blended. A tape
that recorded only the blend could never re-fit the weight it was blended with,
and that weight is the one parameter the report says must not be frozen.

Three more record types on the same tape:

- `refused` — the EV gate passed and a risk law said no, with the law's name.
  The counterfactual is the point: a later study can price the law.
- `window` — a per-window summary at close (`polls`, `priced`, `two_sided`,
  `ev_pass`, `fired`, `refused` and `unpriced` counts, `best_edge`). There is
  deliberately no line per poll per token: at 2s across ~7 series that is 600k
  lines a day to answer a question two integers answer.

**Cold start is real and is not a fault.** The reference is the TWAP print AT
window start, so a pilot that comes up mid-window cannot price that window at
all and picks up at the next boundary — up to 5 minutes for a 5m series, 15 for
`bnb-updown-15m`. That shows up as `unpriced: {no_reference_print: N}` on the
window summary and in `pilot2 status`. Restarting the service costs one window
per series, which is why the kill file exists as an alternative to bouncing it.
- `calib` (in `calib.jsonl`) — ONE `(model_p_up, book_p_up)` sample per window,
  taken at the last moment an entry was still legal. Window-level, not
  clip-level, per L34: a clip-level calibration fit double-counts the windows
  that happened to be evaluated most often.

## The blend weight rule — implemented, not deferred

`calibrated_model.md` §5 specifies the update rule and this pilot implements
it exactly (`policy.fit_blend_weight`, mirroring `calfit/ev_policy.py::wf_blend`):

- **grid** `np.linspace(0, 1, 21)` — 0.00, 0.05 … 1.00.
- **objective** minimise Brier, `mean((w*model + (1-w)*book - y)^2)`.
- **floor** fewer than **400** usable rows and the fit does not run
  (`wf_blend` skips a fold under 400 training rows).
- **seed** `w = 0.55` — the LAST walk-forward fold's value. The trajectory was
  `fold 2 -> 0.00, fold 3 -> 0.20, fold 4 -> 0.40, fold 5 -> 0.55`, converging
  on the full-sample optimum from below. The report is explicit that "that
  trajectory is the finding; the endpoint is not yet earned", so the pilot
  starts there and refits — it never freezes.
- **walk-forward by construction** — a calibration row cannot exist until its
  window has RESOLVED, so no fit ever sees a row it is later scored on. The fit
  lives in `pilot2 grade`, not in the poll loop; the loop reads
  `blend-weight.json` and logs the weight in force on every decision.
- rows missing either estimator are **dropped, never repaired**. `devig`
  returns nan on a one-sided book on purpose; substituting `1 - other_side`
  would manufacture a market opinion that does not exist. With no book, the
  model stands alone — which is the correct behaviour on 60% of corpus rows.

## The risk law

Hard-coded constants in `risk.py`, no env override, no runtime knob.

| constant | value | why |
| --- | ---: | --- |
| `MAX_TOTAL_EXPOSURE_USDC` | **40** | the loss leg is exactly −100% of notional, and ρ≈0.77 / N_eff 1.2 means concurrent windows are ONE bet. This is the most a single correlated event can take. |
| `MAX_CLIP_USDC` | **5** | eight concurrent windows at a full clip still sit inside the total. |
| `MAX_SHARES_PER_WINDOW` | **25** | **the accelerator fix.** `size = clip/ask` buys unbounded shares as the price falls; 34 clips fired below $0.50 carried 13.9% of every share the fleet ever bought. At a $5 clip this binds below ask 0.20 — exactly where the dollar cap stops capping. RETROSPECTIVE.md's closing line is that no study has ever tested a share cap. |
| `MAX_CLIPS_PER_WINDOW_SIDE` | **1** | **the escalation ban.** §1.1: 1–4 clips = 95.5% win / +3.05% RoN; 5+ clips = 79.8% / −9.48%, Wilson intervals non-overlapping in both eras. The loss engine is a second clip into a falling book. "Ever", not "at a time": the fired mark survives the position being retired. |
| `NO_ENTRY_FINAL_S` | **30** | settlement averages [end−60, end]; an entry here buys an outcome already half-printed. |
| `HOLD_TO_RESOLUTION` | **true** | there are no exits, because the engine has none. A paper policy that gets to be smarter than the executor measures a strategy nobody runs. |
| paired-loss refusal | — | **found by the first live shadow run.** Both-sides IS the measured policy, but a whipsawing window bought btc DOWN at 0.53 and minutes later btc UP at 0.53: exactly one pays $1, so the overlapping shares cost 1.06 to collect 1.00 — a guaranteed loss with no opinion in it. The second side is refused when `ask + fee` on both legs sums to ≥ $1. It never blocks a first clip, and never blocks a genuinely cheap pair (asks summing under $1 lock a *profit* on the paired shares). This is the retro's "−27c unpaired residual" arriving by the front door. |
| `MIN_EDGE` (policy.py) | **0.02** | the report's replay value. Its sensitivity table is monotone in trade count and positive at every setting tested — no knife edge. |

Shadow and live keep **separate exposure ledgers**. A shadow window obeys every
law the live one does (that is what makes the tape faithful), but paper
inventory never spends live budget.

**The live book survives a restart.** `RiskBook` is in memory, so a fresh
process used to forget every `(slug, side)` it had fired — a still-open window
could be bought a *second* time, which is §1.1's −9.48% RoN shape rebuilt by a
systemd restart, and the $40 cap read as fully free with real positions open.
On startup the live book is **rehydrated from `live-tape.jsonl`'s `ev:order`
rows** (`Pilot.rehydrate`), which is the right authority because that row is
written *before* the send: a clip that reached the tape is a clip that was
spent, ack or no ack. Only windows still inside their settlement grace come
back as positions; anything older was already retired and queued. Shadow does
not rehydrate — paper exposure outliving the process would seize the shadow
budget at boot and stop the pilot producing the record it exists to produce.

**The kill file**: `touch ~/.pmt/pilot2/HALT`. Checked at the top of every poll
AND again immediately before any order leaves the process. Present → the pilot
writes a `halt` marker and exits 0. Filled positions ride to resolution, which
is what they do anyway. Remove the file before restarting.

## The series partition — a refusal, not a warning

Two participants quoting one market under one beneficial owner is wash-trade
shaped no matter what either intended.

- **Shadow, always**: `btc/eth/sol/xrp-updown-5m`. The desktop engine's book.
  Priced and logged; never touched with capital in any mode.
- **Live, only**: `PILOT2_SERIES`, default `doge-updown-5m,hype-updown-5m,bnb-updown-15m`.
- **Refused, fatally**: anything under `btc-updown` / `eth-updown` /
  `sol-updown` / `xrp-updown` (the desktop rolls every duration), plus exactly
  `bnb-updown-5m` (the EU engine's one series). `bnb-updown-15m` is free — the
  match is `s == owned or s.startswith(owned + "-")`, so a prefix on
  `bnb-updown` cannot swallow the duration this pilot was given.

A refusal exits **2** and the unit's `RestartPreventExitStatus=2` keeps it
visibly stopped. `pilot2 series` checks the partition without starting anything.

## Redeem: LOG FOR THE MANUAL SWEEP

**This is what was built.** There is no relayer batch-redeem path in pmtrader,
and inventing a money-moving code path nobody has reviewed for a $40 book is
the wrong trade. Instead the position is written to
`~/.pmt/pilot2/redeem-queue.jsonl` **twice**, and the first write is the one
that matters:

- `redeem_candidate` — the moment the clip is booked, *before* the order leaves
  the process. The queue used to be written only at settlement, by a `_retire`
  running 300s after the close, so a process that died in between left a filled
  position with nothing anywhere saying it needed sweeping. Writing early can
  only ever queue a position that turns out not to have filled — the sweep sees
  that and skips it; writing late can lose one, which is money left on chain.
- `redeem_due` — the same position when its settlement grace passes and its
  exposure is released.

`pilot2 status` reports the queue depth and notional under `MANUAL SWEEP`,
counting each `(slug, side)` **once** — the newest row wins, so a candidate
superseded by its `redeem_due` is one position, not two. The operator sweeps by
hand.

Note the EU wallet's redeem accounting quirk from RETROSPECTIVE.md §0: a bare
EOA's redeems post as $0/0-share rows, so wins are graded but not paid until
somebody sweeps. The queue is that reminder, in a file.

## Commands

```sh
uv run python -m pilot2 run                 # SHADOW. places nothing.
uv run python -m pilot2 run --live          # still shadow unless PILOT2_LIVE=1
uv run python -m pilot2 grade               # score settled decisions, refit w
uv run python -m pilot2 status              # what has been seen / would have traded / is held
uv run python -m pilot2 series              # validate the partition, exit 2 if refused
```

`status` shows: HALT state, the blend weight in force and its source, shadow
windows closed / polls / priced / EV opportunities seen / would-trade count /
refusals by law, the graded record (hit%, P&L, c/$), live orders and fills, the
redeem queue, and the risk law itself so the numbers are in the same view as
the record they produced.

## Arming live — the ceremony

Do not do this from a diff.

1. `pilot2 series` — confirm the partition is accepted and names nothing an
   engine owns. Cross-check the desktop's `PMENGINE_SERIES_ALLOWLIST` and the
   EU box's, by reading them, not by remembering them.
2. Point `PILOT2_ENV_FILES` at the credential files. On the EU box that is the
   non-secret knobs file plus the L0 key file that was generated on the box and
   has never left it. **Nothing in this package hardcodes either path**, and a
   test asserts that.
3. `PM_SIGNATURE_TYPE=3` with `PM_FUNDER_ADDRESS` set to the deposit wallet.
   Type 3 with no funder is refused at startup — a deposit wallet is a contract
   and has no derivation from the EOA.
4. Set `PILOT2_LIVE=1` **and** add `--live` to the ExecStart line. Two
   switches, two edits.
5. Watch `pilot2 status`. The first live clip is $5 or less.

Orders are **FAK marketable-limit BUYs at the quoted ask** — the live
equivalent of the replay's "fill at the quoted ask, capped by the quoted ask
size". No GTC remainder rests on the book, because a remainder that fills
minutes later is a different strategy than the one that was measured. There is
no SELL path in the module at all.

The pilot talks to `clob.polymarket.com` **directly**, not through pmproxy —
routing an EU box through the SigV4 Lambda would put a US egress back in front
of it and undo the only reason that box exists.

## What the first live shadow run showed (2026-08-24, ~6 min, 4 majors)

One 5m round after warm-up: **7 would-be clips, 71 refusals, and every single
refusal was `one_clip_per_window_side`.** The tape is the §1.1 loss engine in
plain sight — btc DOWN re-offered at 0.53 → 0.50 → 0.45 → 0.38, each one
reading as a *larger* edge than the last, and each one is a clip the incumbent
would have fired. The pilot took the first and refused eleven.

That run also produced the paired-loss law above. It is the only rule in this
package that was not in the approved plan, and it exists because the plan's own
policy, run live for six minutes, walked into a guaranteed loss.

## What this is not

No escalation. No averaging down. No exits. No size ladder. No `min_fair` — the
blend is profitable in every price bucket and best BELOW 0.6, which is the
region `min_fair` forbids and where the incumbent, when it went there, lost
7–57c on the dollar.

## Limits worth repeating before quoting any number this produces

The model behind it was fitted on **15 hours 50 minutes — ONE regime**, 191
cohorts. The +$1,459 / +15.6c per dollar rests on 199 windows. The replay
ignored queue position, the ~2.9s fire-to-fill delay, our own market impact and
the maker book; every one of those makes the real number worse. Book coverage
was 5 of 8 series. The blend weight's drift across folds is direct evidence
that the "converged" numbers have not converged.

That is the whole reason this exists in shadow first.
