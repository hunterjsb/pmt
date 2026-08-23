# Lessons

Numbered registry of the incidents that shaped this code. Every entry was
harvested from an inline war-story comment; the code now carries the
*constraint* (units, ordering, why-this-threshold) and points here for the
*story*.

Cite one as `see docs/LESSONS.md#L7`. Numbers are permanent — append, never
renumber. When you fix something the hard way, add an entry and shrink the
comment to a line.

---

### <a id="L1"></a>L1 — The -$318 window: a 99%-fair entry with no hands to act

**2026-08-22.** An arm bought into a window the model priced at 99% fair and
then had nothing to do as the TWAP flipped underneath it. There was no exit
path at all: entry logic existed, position management did not, so a losing
position simply rode to resolution. -$318 on one window.

**Changed:** an exit rule in `updown.rs` — dump a held side once its fair
collapses below `EXIT_FAIR`, but only into a bid within `EXIT_MAX_DISCOUNT`
of fair, because selling below that just donates to the panic.

### <a id="L2"></a>L2 — TWAP margins inside oracle-vs-venue basis noise are coin flips

**2026-08-22.** The first live night traded any window where the Binance TWAP
showed a margin over the strike. A margin smaller than the Chainlink-vs-Binance
settlement basis carries no information — the settlement print can land either
side of it — so those trades were coin flips priced as near-certainties.

**Changed:** the per-arm `basis_guard_bp` gate, and with it the whole gate
family in the `updown` module header: edge is net of the taker fee, a stale
spot feed means no trade, mean-reverting chop disables speculative clips, and
the full budget unlocks only late or banked-beyond-reversion.

### <a id="L3"></a>L3 — Hard-coded 2-decimal rounding snapped $0.9425 to $0.94

**2026-08-22.** Every order price was rounded to two decimals before signing,
regardless of the market's actual tick size. On a 0.001-tick market that threw
away a quarter-cent of quote precision per order and made carefully-priced
maker quotes land in the wrong place.

**Changed:** `client.rs` caches tick-size decimal places per token and rounds
each market's prices to its own tick (0.0001 / 0.001 / 0.01).

### <a id="L4"></a>L4 — Only SIGINT ran graceful shutdown; SIGTERM abandoned the book

**2026-08-23 config audit.** The engine handled `ctrl_c` and nothing else, so
`pmt engine kill` (SIGTERM) and the nightly systemd poweroff both skipped
shutdown entirely — resting orders and in-flight state were abandoned rather
than cancelled. At the fleet sizes then running, the hole was worth up to
`PMENGINE_MAX_TOTAL_EXPOSURE`, not the "one clip" the docs claimed.

**Changed:** shutdown is wired to SIGINT *and* SIGTERM. (Already-filled
inventory still rides to resolution unmanaged — that is a separate, documented
exposure, not a bug.)

### <a id="L5"></a>L5 — Unsubscribe left exposure in the ledger and froze the fleet

**2026-08-23.** Removing a strategy unsubscribed its tokens but never released
the risk manager's exposure entries for them. Every finished window kept
counting against the account, so accumulated ghost exposure eventually pinned
the whole fleet at the $1500 total-exposure cap and no arm could fire.

**Changed:** unsubscribe releases the exposure ledger for the token. `pmt
engine stop <strategy>` now unsubscribes its tokens as part of the same step.

### <a id="L6"></a>L6 — `?` in the cancel loop abandoned every order after the first failure

**2026-08-23 sweep.** The bulk-cancel path used `?` inside its loop, so a
single failing cancel returned early and left every remaining order live on
the book — while `pause`/`stop` reported success to the operator. A partial
cancel that reports "done" is worse than one that reports an error.

**Changed:** the loop keeps going past individual failures and reports what it
could not cancel.

### <a id="L7"></a>L7 — The 22k-rows-of-zeros bug: print flow sampled by window, not high-water mark

**2026-08-23.** Print-flow was counted as "trades in the last 6s". The data-api
indexes trades tens of seconds late, so that filter matched nothing, ever —
roughly 22,000 tape rows recorded a flow of zero and nobody noticed, because
the poller's failures were logged at `debug`.

**Changed:** flow is counted by per-token newest-timestamp high-water mark
instead of a sampling window, and the poller's failures log at `warn` — its
silent failure is precisely how the zeros went unnoticed.

### <a id="L8"></a>L8 — Brakes flagged all four losses but blocked only 10-51% of the exposure

**2026-08-23 audit.** The per-tick brakes correctly flagged all four losing
windows, yet each window still accumulated most of its position: the brake
tripped, the ask drifted back inside tolerance a few ticks later, and the next
clip slid through. Per-tick brakes cannot hold a window that keeps
re-qualifying.

**Changed:** the brake latches on first trip and is held for the rest of the
window for speculative entries. Banked-decided trades still go through — the
edge there is arithmetic, not a book read.

### <a id="L9"></a>L9 — 32% of intended taker notional never crossed

**2026-08-23 audit.** A third of the notional the strategy decided to spend
never actually filled. The book ticks away between the decision and the
order's arrival, and each re-quote chases it upward, so the arm spent the
window paying more for less.

**Changed:** `pay_up_max` — a clip's limit may sit a configured number of
cents *above* the decision ask, funded only by surplus edge over the edge
floor. A marketable limit fills at the book, so the buffer costs nothing
unless the book actually moved.

### <a id="L10"></a>L10 — A pending BUY locked the whole position out of evacuation

**2026-08-23 adversarial sweep.** The in-flight guard treated any pending
order as a reason to block the next order. A BUY whose fill was missed
therefore held the *entire* position hostage for the in-flight TTL — exactly
while fair was collapsing and the exit was the only thing that mattered. The
safeguard re-enabled the loss it existed to guard against.

**Changed:** only a pending EXIT (the 0-notional sentinel) blocks another
exit. A pending BUY never does.

### <a id="L11"></a>L11 — NaN breakeven priced both sides at fair 1.0 simultaneously

**2026-08-23 adversarial sweep, compiled repro.** When banked mass sits so far
above the reference that no positive remaining path can pull the average back,
the breakeven goes negative and `ln(negative)` is NaN. NaN comparisons all read
false and `f64::min` eats NaN, so the model priced *both* outcomes at fair 1.0
at once and fired clips on both sides of the same window.

**Changed:** an explicit `breakeven <= 0` guard that resolves the side as
already-won instead of falling into the log. Repro pinned as a unit test
(13min banked at +5% vs reference, 35s left).

### <a id="L12"></a>L12 — Every blown-up window entered on huge claimed edge into a collapsing book

**2026-08-23.** The losing windows shared one signature: an enormous claimed
net edge. That is not free money on the table — it is the book pricing in
something the model has not caught, and the model buying the gap.

**Changed:** the book-distrust brake — net above a threshold blocks the fire.
Banked-decided TWAPs are exempt, since their edge comes from arithmetic on
already-elapsed time, not from disagreeing with the book.

### <a id="L13"></a>L13 — R9: both post-brake losses entered at safety < 0.25 while the clock said go

**2026-08-23.** After the brakes landed, the two remaining losses both entered
on the elapsed-time gate alone, with banked evidence at safety below 0.25;
the median winner entered at 0.57. The clock says "go" long before the
evidence does.

**Changed:** the `theta` entry gate — the FIRST clip of a TWAP window requires
`signed_banked_bp / cushion_bp >= theta` on the fired side. It applies only
until the first clip lands; position management after entry belongs to the
brakes. θ=0.3 is the deployed value.

### <a id="L14"></a>L14 — Gate refusals travelled as regex-scraped prose across the language boundary

**2026-08-23.** The basis guard formatted `margin`/`banked`/`cushion` into its
error *string*, and two Python consumers (`cli_crypto._MARGIN_RE`,
`shadow._MARGIN_RE`) regexed the numbers back out. Any reword of that sentence
broke both readers silently — no compiler, no test, nothing in the loop.

**Changed:** the numbers travel as structured fields on the eval; `reason`
stays the same human sentence so the durable tape and its existing consumers
keep working. The regex survives only as the legacy path for evals from an
engine built before the fields shipped.

### <a id="L15"></a>L15 — Fully-gated windows logged no book data, so the audit couldn't price the guard

**2026-08-23 audit.** Asks were only recorded on windows that got far enough to
trade. A window gated for its whole life wrote nothing, which meant there was
no way to ask the most important question about the basis guard: what did
refusing cost?

**Changed:** asks are recorded even while gated, so refusals can be
hindsight-priced (`pmt crypto shadow`).

### <a id="L16"></a>L16 — Basis measured against live spot inflated the guard where it hurt most

**2026-08-23, caught in review before it shipped.** Chainlink lags Binance
(deviation-threshold + heartbeat updates plus its own venue aggregation).
Comparing the oracle's answer against the *live* spot during a fast move
measures trend speed × oracle lag, not settlement basis — which would have
inflated the guard exactly in the regimes where banked-margin trades are best.

**Changed:** a basis sample compares the oracle answer against Binance's mark
for the minute containing the oracle's own `updatedAt`. Persistent venue basis
survives that alignment; the lag artifact mostly cancels.

### <a id="L17"></a>L17 — BTC's p95 basis halved overnight; a snapshot-fitted guard is a guess

**2026-08-23.** The measured basis distribution is non-stationary — BTC's p95
halved between two consecutive days. A guard set from a 48h-old snapshot
therefore under- or over-trusts depending on which regime happened to print it.

**Changed:** a per-arm Chainlink poller tracks live basis and may only RAISE
the arm's `basis_guard_bp` above the operator's offline-measured param, never
lower it. Loosening below the param needs a human and a replay A/B, never an
unattended live estimate.

### <a id="L18"></a>L18 — Three copies of the deployed basis guards, all drifted

**2026-08-23.** The live guard values existed in `chainlink.py`, in the `pmt
crypto arm` default, and again inside `analysis/r1_aligned_basis.py`. They had
drifted — the analysis script still graded btc at 3bp against a p95 while the
live arm ran 6bp — so the study was quietly measuring a guard nobody used.

**Changed:** `chainlink.py` is THE source. `pmt crypto arm` resolves its
`--basis-guard` default from it and the analysis scripts read it rather than
keeping copies.

### <a id="L19"></a>L19 — Duplicated parsers would have de-synced replay from the code it judges

**2026-08-23.** The live feed poller and the replay corpus fetcher each parsed
Binance klines inline — same field offsets, same zero filter, two copies. The
durable tape's event-type strings were likewise written in one file and matched
in another. A drift in either would have made replay silently grade something
other than what ran live, which is the one failure mode replay cannot survive.

**Changed:** one shared `klines` shaper called by both paths, and shared
event-type constants between the tape writer and the replay matcher.

### <a id="L20"></a>L20 — A stray `pub struct` in strategies/ registers as a bogus strategy

**2026-08-23.** `pmstrat transpile --all` registers the first `pub struct` it
finds in every file under `strategies/`. Helper modules that live there for
organizational reasons are therefore load-bearing in a way nothing in the file
says: add a plain `pub struct` and the transpiler either skips it (harmless) or
registers a strategy that does not exist.

**Changed:** `updown_model.rs` and `updown_oracle.rs` keep every public item at
`pub(crate)`, with a header note saying why.

### <a id="L21"></a>L21 — A bare `pmengine run updown` runs the model 20x slower than measured

**2026-08-23 config audit.** `updown` declares a 50ms cadence because its whole
latency model — quiesce windows, flip cutoffs, in-flight TTLs — was fitted at
that rate. The engine's OUTER loop is the real ceiling and defaults to 1000ms.
The `pmt` CLI always passes `PMENGINE_TICK_INTERVAL_MS`, so launching the
binary directly silently ran the model at 1/20th its calibration rate with
nothing in the log saying so.

**Changed:** a startup warning when the loop interval throttles a strategy's
declared cadence. Diagnostic only — no clamping, no refusal; the operator's env
var stays the law, it just stops being invisible.

### <a id="L22"></a>L22 — The size-0 dust redeem: a -$265 loss recorded as a win

**2026-08-23.** Grading read the wallet: if every redeem on a slug paid $0, we
held the loser, so flip the outcome label. But a size-0 $0 "dust" redeem can
carry the WINNER's label — on `btc-15m-1787457600` the dust row said "Up" while
the real position was Down. The blind flip booked a -$265 loss into the corpus
as a win, and the fleet's headline P&L was wrong until the operator caught it.

**Changed:** only a redeem row with real size is trusted to name the side we
held. With no sized row there is no guess — the chainlink/gamma fallback grades
the window instead.

### <a id="L23"></a>L23 — ~17 windows carry a spurious $0 dust redeem beside the real one

**2026-08-23 audit.** A partial fill on the losing token leaves a $0 redeem row
sitting next to the genuine paying one. Any grading rule keyed on the *presence*
of a $0 row therefore mis-graded roughly 17 windows in the corpus.

**Changed:** the summed *paying* amount decides the grade, and it wins the
priority check over `redeem_seen`. An actual $0 redemption is still ground
truth from the wallet — it just has to be the total, not a row.

### <a id="L24"></a>L24 — Silence past the grace window used to mean LOSS

**2026-08-23.** If no redeem had posted by the end of the grace period, the
grader recorded a loss. A slow redeem, an indexer lag, or an unreachable gamma
were all indistinguishable from a real loss, and they were all booked as one.

**Changed:** gamma's resolution is consulted first. With no gamma reachable the
old heuristic still runs, but the result is flagged `estimated` rather than
presented as confirmed — and a gamma-confirmed win whose redeem hasn't posted
yet gets an imputed P&L under the same flag.

### <a id="L25"></a>L25 — Offset pagination over a live feed drifted the all-time scoreboard

**2026-08-23.** Wallet history is walked with offset pagination over a feed that
is still being written. Whenever the fleet traded mid-walk, rows shifted down
and boundary rows were duplicated (or, on deletions, skipped). The operator
noticed the all-time P&L moving run-to-run while nothing had actually settled.

**Changed:** pages advance by less than their size (`PAGE_STEP` 400 against
`PAGE_SIZE` 500) so the seam is re-read, and a `row_key` dedupe collapses
whatever duplicates remain.

### <a id="L26"></a>L26 — An unset funder address reported a clean "0W-0L"

**2026-08-23.** With no funder address configured, the scoreboard fell through
its address lookup silently and printed a perfectly healthy `0W-0L` — visually
identical to a genuinely empty trading history. Every sibling command
(`activity`, `window`, `outcomes`) already raised for this case.

**Changed:** the scoreboard raises a usage error like its siblings.

### <a id="L27"></a>L27 — The 92% win rate flatters: bounded up, unbounded down

**2026-08-23.** The updown book's count win rate sat around 92% while the money
was going the other way. The payoff is bounded up (+2-8% of stake, buying at
0.92-0.98 to collect $1) and unbounded down (-100%), so a $10 win and a $265
loss are one tally mark each — and the count ignores time entirely. Eleven $10
wins against one $265 loss is 92% of the marks and 29% of the money.

**Changed:** `polymarket/effectiveness.py` — money-weighted win rate, the
break-even win rate this payoff shape actually requires, profit factor, return
on risk capital (per dollar-hour), and bankroll growth. `pmt crypto stats`
renders each corrected number beside what it means.

### <a id="L28"></a>L28 — The watch dashboard polled keys on the thread that walked the wallet

**2026-08-23.** The dashboard was one loop: poll for a keypress once a second,
then go walk the entire wallet history inline. A keypress could sit unread
behind a multi-second HTTP walk, so `q` and `h` felt dead.

**Changed:** a render/fetch split. The main thread does input and render with
zero network, polling the tty at 20Hz (the `select` timeout IS the loop's
pacing); one daemon worker owns every network call on its own cadences and
publishes whole result objects into a `WatchState`.

### <a id="L29"></a>L29 — `redirect_stdout` on the fetch thread blanked the whole dashboard

**2026-08-23.** `engine.post()` prints its own red error before exiting, and the
obvious fix for that noise inside the dashboard was to wrap the call in
`contextlib.redirect_stdout`. That swaps `sys.stdout` for the whole PROCESS;
Rich resolves `sys.stdout` at write time, so the render thread saw a non-tty
and stopped painting. The dashboard came up entirely blank.

**Changed:** the print is left alone — inside `Live`'s alternate screen it is
routed through Live's io redirect and painted over by the next frame, exactly
as it behaved on the main thread.

### <a id="L30"></a>L30 — `sys.stdin.read` blocked the dashboard on a single keypress

**2026-08-23, operator-reported.** Key polling read through `sys.stdin`, whose
`TextIOWrapper` buffering can demand more bytes than the tty has available and
block. `h` and `q` were dead in `watch` for exactly as long as nothing else was
typed.

**Changed:** `os.read` on the raw fd behind a `select`, never `sys.stdin.read`.

### <a id="L31"></a>L31 — The scanner unsubscribed a sibling strategy's static tokens

**2026-08-23.** Market-discovery mode unsubscribes tokens it no longer wants.
Tokens declared statically by another strategy looked identical to it, so the
scanner could pull the subscription out from under a strategy that does not
drive the scanner and would silently stop working.

**Changed:** tokens statically declared by ANY loaded strategy are held out of
the scanner's unsubscribe set.

### <a id="L32"></a>L32 — A flat 3bp basis band under-guards eth/sol by 2-3x

**2026-08-23.** The arm default was one flat basis band for every symbol. The
R1 aligned measurement put btc at 6bp, eth at 8bp and sol at 10bp — a flat 3bp
under-guarded the alts by two to three times, which is the shape the losses
took.

**Changed:** `--basis-guard` resolves per-symbol from `chainlink.py`, and an
unmeasured symbol falls back loudly rather than silently taking a number fitted
for something else. xrp/doge stay untradeable through the Binance proxy.

### <a id="L33"></a>L33 — The live tape is append-only, so a re-run is not a re-run

**2026-08-23.** Analysis scripts read the tape the engine is still writing to.
Re-running a study silently sees a larger corpus, and an inferred outcome can
even flip as a window's terminal book matures — so two runs of the same script
disagree and neither is wrong.

**Changed:** studies point `PMT_BOOK_TAPE` at a frozen copy when the number is
meant to be reproducible, and the analysis library says so at the top.

### <a id="L34"></a>L34 — The calibration fit weighted clips, not windows

**2026-08-23.** The first sizing simulation fitted its isotonic calibration at
clip level. A window that fired ten clips therefore counted ten times against a
window that fired one, weighting the curve toward whichever windows the arm
happened to be most active in.

**Changed:** the fit is window-level.

### <a id="L35"></a>L35 — Warmup could gate forever on a market with no book

**2026-08-23.** Warmup waited for order books to sync. A scanner that added an
illiquid market — no orders, therefore no book — could hold the engine in
warmup indefinitely, and the websocket condition never fires under US IPs where
Polymarket's WS is geoblocked.

**Changed:** three independent exit conditions, any one sufficient:
`--skip-warmup`, enough streamed WS book diffs, or a wall-clock deadline. A
still-missing book is handled by each strategy's own `if book is None` guard.

### <a id="L36"></a>L36 — Corpus TWAP labels near zero margin are interpolation noise, not outcomes

**2026-08-23.** The Chainlink round corpus grades untraded windows by flat-hold
TWAP. Rounds land ~30s apart, so any settlement decided by less than the
inter-round drift is invisible to it: measured against wallet + terminal-book
witnesses over 48h (n=344 graded), sub-1bp chainlink labels were WORSE than a
coin flip (1/6 vs wallet ground truth), and one 15m window was flat-out wrong
at 3.2bp. 69 poisoned rows (16% of the corpus) had been feeding miss-rate
studies. The tell that surfaced it: four windows where the entire market held
0.99 through the final 100ms while our corpus said the other side — when the
crowd and your derived label disagree, audit the label first.

**Changed:** `chainlink_outcome` refuses to grade below `CK_NOISE_FLOOR_BP`
(5bp). A new strictly-last `book` source grades from the market's own terminal
book (samples inside the final 15s only, ≥2 agreeing 0.95-pinned samples, zero
contradicting — a tape that died mid-window must not grade). True coin-flips
with unpinned books drop, never guess. Corpus purged and regraded.
