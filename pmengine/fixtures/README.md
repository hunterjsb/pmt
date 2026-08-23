# Characterization fixtures

Real, wallet-graded windows frozen into this repo so the decision core is
regression-tested against trades that actually happened — forever, offline, on a
checkout that has never seen `~/.pmt`.

`~/.pmt` is a machine-local, append-only pile on a desktop that powers off nightly.
The lessons inside it were paid for in dollars. Every fixture here is one of those
lessons turned into committed test data that outlives the box.

```
pmengine replay --fixtures fixtures            # the whole suite, PASS/FAIL, non-zero on any FAIL
pmengine replay --fixtures fixtures --only <slug>
cargo test                                     # the same run, as tests/characterization.rs
```

---

## What a fixture is

One window, one self-contained JSON file, `<slug>.json`. Nothing is resolved
outside the file — no network, no corpus, no home directory.

| key | what it holds |
|---|---|
| `mode` | `evals` or `full`. Load-bearing: `full` needs `book` + `klines`, and a fixture that claims it without them fails to load. |
| `params` | Exactly one `--params` array entry: `ArmParams` as armed, plus an optional replay-only `tunables` override. |
| `params_provenance` | Per field: tape-derived, slug-derived, inherited from the arm store at freeze time, or an operator override. A reconstruction says so. |
| `outcome` | The wallet's verdict and accounting. `source` must be `wallet`. |
| `evals` | The `updown-tape.jsonl` slice for this slug, verbatim — eval / fire / gated / roll / cleanup. Drives `evals` mode; sources the live-fire comparison in either. |
| `book` | The `book-tape.jsonl` slice, trimmed to the fields replay reads (top-of-book per side + spot/spot age). Print flow and per-side source/age diagnostics are dropped: a fixture carries the decision's inputs, not the night's whole telemetry. |
| `klines` | 1m klines over `[start - 2700, end)` — the model's whole lookback. This is the offline seam: `--mode full`'s only network call is the kline fetch, and a fixture hands them in instead. |
| `invariants` | Curator intent (see the vocabulary below). **`--bless` never rewrites these**, which is what stops a careless regeneration from laundering a real behaviour change. |
| `expect` | Generated from the current engine's replay at curation time, then hand-checked against the wallet record. |
| `provenance` | sha256 + record count per slice, the freeze timestamp, the curator's note, and which tape schema generation this slice carries. |

Sizes: 16–154 KB each, 1.4 MB for the thirteen.

### Wallet-graded only

`outcome.source` must be `wallet` — enforced by the freezer AND by the loader.
A chainlink- or book-derived label is an inference, and L36 is the receipt for
treating an inference as ground truth: sub-1bp chainlink labels graded *worse
than a coin flip* against wallet truth, and 69 poisoned rows fed miss-rate
studies before anyone noticed. A fixture is what other measurements get checked
against, so it carries the settlement Polymarket actually paid, or it does not
exist.

**The cost of that rule, stated plainly:** a window nobody traded has no redeem,
so it can never be wallet-graded. The two most-wanted "the guard/θ correctly
refused everything" exhibits are exactly that class and are permanently outside
this suite. What stands in for them are the heavily-gated windows that still
fired something — `sol-updown-5m-1787468100` (39 gated ticks against 16 evals)
and `btc-updown-15m-1787464800` (88 gated ticks) — plus two fixtures that today's
engine refuses outright.

### What `expect` asserts

* **Fires** — count, by side, by mode, first-fire tick, intended notional, peak
  committed. Read off the decision tape `decide()` emits, i.e. the engine's own
  account of what it did, not re-derived from the fill sim.
* **Gates** — total, a histogram bucketed on the refusal's leading phrase
  (`basis guard`, `feed stale`), and named ticks pinned to the FULL sentence.
  Two registers on purpose: the histogram survives numbers moving inside a
  sentence and catches a whole class appearing or vanishing; the named ticks
  catch a reword or a drifted number. L14 (gate prose crossing a boundary with
  nothing checking it) as a test.
* **P&L sign** against the wallet-graded winner, with the value carried for a
  human reading a failure.
* **Invariants** the curator declared.

Counts, sides, modes and tick timestamps are compared **exactly** — the sim is
deterministic against frozen input, so a drift there is a determinism bug, not a
tolerance problem. Money is compared to the cent.

### Invariant vocabulary

`no_fire` · `no_fire_before_t:<epoch>` · `fires_eq:<n>` · `all_fires_side:up|down`
· `all_fires_mode:safe|spec|flip` · `pnl_sign:neg|pos|zero` ·
`max_committed_le:<usd>` · `gated_ticks_ge:<n>` · `sim_notional_ge_wallet`

An unknown name is a **load error**, never a silent pass — a typo'd invariant
would otherwise be caught on the one run where it mattered.

---

## The catalog

`sim` is today's engine replaying the frozen window; `wallet` is what the money
actually did. They are not supposed to match: the fill sim crosses every clip
instantly at the recorded ask, with no partials and no queue, so it over-states
exposure by design (see *Known gaps*).

| slug | UTC | mode | why it earns a permanent slot | sim | wallet |
|---|---|---|---|---|---|
| `btc-updown-15m-1787449500` | 01:45 | evals | **The −$370 anatomy.** 13 speculative UP clips into a book the model outran; 49 ticks gated at the 3.0bp band; winner DOWN. ROADMAP Phase 1's acceptance test, frozen. Predates the book recorder, so evals-only. | 10 fires, $495.05, −$504.15, first fire on the **identical tick** as reality | $370.14 bought, $0 redeemed, −$370.14 |
| `btc-updown-15m-1787454000` | 03:00 | full | **Wrong-side banked btc15.** 11 DOWN clips entered on the clock gate with the evidence at safety −0.11, winner UP. R9's exhibit, one window before θ existed. | 18 fires, $299.86, −$304.88 | $169.84 bought, $0 redeemed, −$169.84 |
| `btc-updown-5m-1787456100` | 03:35 | evals | **What the brake latch costs.** A clean 3-clip win with zero gated ticks that today's engine refuses outright: the distrust brake trips on the early ticks, L8's latch — which shipped ~90 min after this window — holds the rest of it, and `banked_decided` never arrives in time to exempt a clip. | **0 fires** | $148.39 → $167.99, **+$19.60** |
| `sol-updown-15m-1787457600` | 04:00 | full | **The terminal-rule loss, filed as a basis event.** 7 DOWN clips, winner UP. R6 called it >p99 SOL basis; `analysis/fourh_fit.md` re-reads it as the model pricing the range average against a market that settles on the terminal TWAP. The tripwire for any `eval_model` re-spec — its verdict here is *supposed* to move, and the PR that moves it has to say so. | 6 fires, $148.22, −$144.59 | $142.45 bought, $0 redeemed, −$142.45 |
| `btc-updown-15m-1787458500` | 04:15 | full | **The night's biggest wallet win.** 21 DOWN clips out of a $350 budget. Don't-break-what-works at scale: a gate change that stops this window firing justifies itself in the same PR. | 15 fires, $347.54, +$36.78 | $308.05 → $390.97, **+$82.92** |
| `eth-updown-15m-1787461200` | 05:00 | full | **The θ boundary.** 54 ticks gated at the 8bp band and the safety brake live for the first time in this corpus; 10 safe DOWN clips still landed. | 7 fires, $299.87, +$24.30 | $354.54 → $380.50, **+$25.96** |
| `eth-updown-15m-1787463000` | 05:30 | full | **The eth guard trim, priced.** 72 ticks refused at the 8bp band, 3 clips survived. What the guard costs and what it keeps, on one window. | 4 fires, $187.26, +$11.85 | $149.34 → $162.00, **+$12.66** |
| `btc-updown-15m-1787464800` | 06:00 | full | **θ live and still −$100.** 88 gated ticks, 8 DOWN clips including the corpus's only `flip`-mode clip, winner UP. An entry gate is not a loss-proof. | 25 fires, $346.21, −$359.99 | $100.39 bought, $0 redeemed, −$100.39 |
| `btc-updown-15m-1787465700` | 06:15 | full | **The first 15m fully inside the theta+payup era.** 20 safe UP clips. The closest sim-to-reality window in the suite — within two clips and 50 cents of notional. | 18 fires, $349.53, +$28.34 | $352.02 → $378.00, **+$25.98** |
| `eth-updown-15m-1787465700` | 06:15 | full | **The latch holds speculation, never arithmetic.** Brake latched for 31 of its reads and 8 safe UP clips still got through; the sim reproduces the fire count exactly. | 8 fires, $349.14, +$20.33 | $351.16 → $370.00, **+$18.84** |
| `sol-updown-5m-1787468100` | 06:55 | full | **The quiet, guard-dominated 5m** — the shape of most of the fleet's night: 39 gated ticks against 16 evals at the 10bp sol band, $4.86 of risk actually taken. Also the suite's clearest instance of the instant-fill sim over-stating a thin book. | 5 fires, $103.25, +$3.49 | $4.86 → $5.00, **+$0.14** |
| `eth-updown-15m-1787468400` | 07:00 | full | **The brake-latch save, and its edge.** 52 latched reads let exactly ONE $45 clip through on a window the model kept wanting more of. Today's engine lets none through — the latch is one tick from refusing this class entirely. | **0 fires** | $45.14 → $46.00, **+$0.86** |
| `btc-updown-15m-1787470200` | 07:30 | full | **The disciplined end of the same policy that lost $370 at 01:45Z.** Latch on for 50 reads, 28 gated ticks, 4 safe DOWN clips, winner DOWN. | 8 fires, $152.12, +$4.54 | $54.59 → $56.00, **+$1.41** |

Deliberately included: one window with **no book tape**, three **wallet losses**,
two windows today's engine **refuses entirely**, and one where the sim is **known
to be wrong** in a documented direction. A suite made only of clean wins teaches
nothing.

### Windows that could NOT be frozen

| window | why not |
|---|---|
| `xrp-updown-5m-1787485200` (11:40Z) — the RTDS stream-fed win | Not wallet-graded. `outcomes.jsonl`'s newest wallet row is 09:40Z; the grader has not run since this window settled. Re-run `pmt crypto outcomes`, then it can be frozen unchanged. |
| `btc-updown-15m-1787446800` (01:00Z) — the no-fill ghost (32 fires, $698 of intent, $0 bought) | No graded outcome row at all. |
| `btc-updown-15m-1787457600` (04:00Z) — the corpus-corruption window (L22's size-0 dust redeem) | **chainlink**-graded. With no *sized* redeem row the wallet cannot name the side we held, which is precisely L22's fix — so the window that taught the grader its lesson is the window the rule now excludes. Its grading behaviour belongs in a `pmtrader` test over `outcomes.wallet_outcomes`, not here. |
| `btc-updown-5m-1787461800` (05:10Z) — basis guard SAVED, 43 ticks gated, zero fires | Zero fires ⇒ no redeem ⇒ no wallet grade. Structural, not fixable. |
| `eth-updown-15m-1787462100` (05:15Z) — θ-blocked and correctly so, zero fires | Same. |
| `eth-updown-5m-1787462100` + `sol-updown-5m-1787462100` — R7's same-minute correlated loss pair | Both chainlink-graded. A fleet-cap fixture also needs the interleaved multi-window driver, which this single-window format does not carry. |

---

## Known gaps (deliberate, documented, do not "fix" quietly)

1. **The fill sim over-states exposure.** Every clip crosses instantly at the
   recorded ask — no partials, no queue, no re-quote. So sim notional ≥ wallet
   notional, always, and `sol-updown-5m-1787468100` shows the extreme
   ($103 sim against $4.86 filled). That over-statement is conservative and
   intentional. `sim_notional_ge_wallet` pins the direction on the fixtures where
   it matters; an "improvement" that makes the sim fill less has to move those
   expectations and explain itself.
2. **No recorded window can prove a pay-up chase.** The fire record carries `ask`
   but never the limit `pay_up_limit()` submitted, so `pay_up_max` is frozen at
   `0` in every fixture and the two payup-era fixtures are era markers, not chase
   evidence. Landing the one-line `limit` field on the fire record unblocks it.
3. **Old gated tape lines carry no structured numbers.** `margin_bp` /
   `banked_bp` / `cushion_bp` / `guard_bp` only reach the tape in the last era;
   before that the numbers survive only inside the reason prose, which
   `gate_from_record` refuses to parse. Every fixture's
   `provenance.tape_schema` records which generation its slice carries — a
   gate-heavy fixture cut before those fields shipped pins less than it looks
   like it does.
4. **`evals` mode is blind to exits and to quiesce-window flip clips** (the eval
   tape records no bids and stops at quiesce), and it trusts the recorded model
   read as-is. `full` mode rebuilds the model from the book tape and klines and
   therefore reconstructs — not replays — the model's numbers. Two fixtures are
   frozen in `evals` mode on purpose; the mode is in the manifest.

---

## Adding a fixture

```
pmt crypto fixture <slug> \
  --teaches 'one line on why this window earns a permanent slot' \
  --era post-theta --era guard-6bp \
  --lesson 'docs/LESSONS.md#L13' \
  --invariant all_fires_side:down --invariant pnl_sign:neg \
  --note 'where any --param came from'
```

Reads the local corpus only, never writes to a tape, refuses a non-wallet-graded
window, refuses to write if the slice carries anything address- or key-shaped,
then blesses the fixture through `pmengine replay --fixtures <file> --bless` and
prints the sim-vs-wallet reconciliation for you to hand-check.

**The incident pipeline:** every wallet-realized loss ≥ $100, and every window
where the sim and the wallet disagree materially, gets a fixture within 24 hours
— before the postmortem closes. The fixture is part of the fix, not a follow-up
ticket. Same for a *win* that reveals a policy surprise. **Fixtures are never
deleted**; a superseded lesson gets a note appended and stays.

### Params as armed

The freezer reconstructs the arm from what the tape can prove and says so per
field:

| field | source |
|---|---|
| `size_usdc` | the window's own `roll` record, else the series' first roll |
| `basis_guard_bp` | the lowest `guard_bp` the window recorded — the static param the live dynamic guard raises *from* (L17) |
| `clip_usdc` | the largest clip that actually fired, rounded up to $5. The sizer caps at `clip_usdc`, so the biggest fire is a tight lower bound; every window in this catalog lands exactly on 25 or 50. |
| `theta` | `0` unless the tape shows a `safety` brake, which only exists when θ > 0 — that is how a pre-R9 window stays pre-R9 |
| `pay_up_max` | `0`, always. Not recoverable (gap 2). |
| everything else | inherited from the arm store at freeze time — a reconstruction, marked as one |

`--param KEY=VALUE` pins a value the tape cannot prove; it is recorded as an
operator override, and `--note` says where it came from. Four fixtures use it,
each with the band read off that window's own gate prose or cited to ROADMAP.md.

### Changing an expectation

`expect` is source code. Regenerating it is `pmt crypto fixture <slug> --regen`,
and it is a **deliberate act**:

* The bless path **refuses more than one fixture per invocation** and prints the
  diff before writing. Mass regeneration is the exact failure mode this suite
  exists to prevent.
* It never runs in CI.
* It rewrites the generated expectations and **never the declared invariants** —
  so a re-bless cannot make a genuine behaviour change go green on its own.
* The commit message names which fixtures moved, the before/after numbers, and
  the mechanism that moved them. "CI was red" is not a mechanism.

A fixture that starts failing is a **finding**, not a chore. It means the
decision core changed behaviour on a real trade; triage it before the PR merges.

---

## Acceptance, verified at build time

* The −$370 fixture reproduces ROADMAP Phase 1's recorded acceptance numbers
  **from committed data**: first sim fire at `1787449950.03049` — the identical
  tick as reality — $495.05 of $500 committed, −$504 sim against −$370 wallet.
* Reverting a known brake breaks the suite loudly. Stubbing `safety_gate_blocks`
  to `false` (θ off) fails four fixtures, and the output names each move:
  `fires: expected 18, got 19 (+1)`,
  `fires by mode: expected {safe=18}, got {safe=16 spec=3}`,
  `first fire tick: expected 1787466185.749730, got 1787465719.750063 (-466.00s)`.
* `tests/characterization_offline.rs` runs the whole suite with `$HOME` pointed
  at an empty directory and asserts nothing was written under it.
