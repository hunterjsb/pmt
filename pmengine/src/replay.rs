//! Offline replay harness for the updown strategy's decision core
//! (`strategies::updown::ArmState::decide`). Drives the exact function
//! live ticks call — the sim never reimplements the firing policy, only
//! fills/PnL/settlement, which are replay-only math with no live analog.
//!
//! Two fidelity modes:
//!   - `evals`: replays the already-computed model reads off the eval
//!     tape. Fast, no network, but blind to a few things: no exits (the
//!     tape never records bids), no quiesce-window flip clips (the tape
//!     stops recording once quiesce starts), and the recorded
//!     p_up/rho/banked_decided/margin are trusted as-is rather than
//!     recomputed — a bad live model read replays as a bad read here too.
//!   - `full`: rebuilds FeedState from the book/spot recorder plus the
//!     arm's own market-data source and calls `eval_model` itself, so it
//!     exercises the whole pipeline decide() sits behind. Which source is
//!     the arm's `feed` param, exactly as it is live: `binance` reshapes
//!     cached 1m klines, `rtds` replays the settlement stream out of the
//!     recorder corpus (see `replay::rtds`). Only place in this module
//!     that touches the network (`--mode full` klines fetch), and it's
//!     cached to `~/.pmt/corpus/klines-1m-{SYMBOL}.jsonl` so a re-run of
//!     the same window doesn't refetch; the rtds path never fetches at all,
//!     because the stream serves no history to fetch.
//!
//! Settlement (which side actually won) needs a ground truth per window.
//! `full` mode already has the true klines on hand (cache is fetched with
//! no look-ahead restriction lifted — the window is over, scoring it
//! after the fact is fair) and applies the same TWAP-proxy math the model
//! banks on. `evals` mode has no klines and stays network-free by design,
//! so it uses the model's own last recorded p_up — by window close the
//! model is effectively certain (banked_decided, near-zero residual), so
//! that "trust the tape" p_up already read is a fine proxy for the truth.
//! `--outcomes` (wallet truth) overrides either proxy when given.
//!
//! No wall-clock reads: every `now` used for a decide() call comes from a
//! record's own `t` field, so a replay run is deterministic and repeatable.

use crate::fees::taker_fee;
use crate::strategies::updown::{
    Action, ArmParams, ArmState, ArmView, DecideOut, TopOfBook, FEED_RTDS,
};
use crate::strategies::updown_model::{
    eval_model, lag1_autocorr, shape_klines, FeedState, GateReason, GuardShape, Kline, ModelEval,
    Tunables,
    BINANCE_DATA, EV_EVAL, EV_FIRE, EV_GATED, KLINE_LOOKBACK_S,
};
use crate::strategies::updown_rtds;
use rtds::{RtdsCorpus, RtdsTimeline};
use serde::Deserialize;
use serde_json::Value;
use std::collections::{BTreeMap, BTreeSet, HashMap};
use std::io::BufRead;
use std::path::{Path, PathBuf};
use std::sync::Arc;

pub mod fixtures;
pub(crate) mod rtds;

pub struct ReplayOpts {
    pub mode: String,
    pub tape: Option<PathBuf>,
    pub book_tape: Option<PathBuf>,
    pub slug: String,
    pub params: Option<PathBuf>,
    pub outcomes: Option<PathBuf>,
    pub out: Option<PathBuf>,
    /// RTDS recorder corpus (directory of `rtds-YYYYMMDD.jsonl`, or one
    /// file) — the market data for `feed = "rtds"` arms in full mode.
    /// Defaults to `~/.pmt/corpus/rtds`; loaded only when a matched window
    /// is actually stream-fed.
    pub rtds_corpus: Option<PathBuf>,
    /// Replay-only decision trace: every tape record `decide()` produced,
    /// written as JSONL. Full mode only. The model reads a study needs to
    /// attribute a gate (`banked_bp`, `cushion_bp`, `term_bp`, the per-side
    /// `brake`) exist inside the run and reached nothing before this — a
    /// gate-attribution ladder could only bound them by relaxing knobs one
    /// at a time and reading fire counts. Costs nothing when unset.
    pub trace: Option<PathBuf>,
    /// R7: `Some(cap)` switches to the interleaved fleet driver (every
    /// matched window stepped in one global timestamp order, sharing one
    /// un-decided pool). `Some(0.0)` is that same driver with no cap — the
    /// only honest A/B baseline for a cap, since it differs from a capped
    /// run in the cap and nothing else. `None` is the per-window driver.
    pub fleet_cap: Option<f64>,
}

pub fn run(opts: ReplayOpts) -> Result<(), String> {
    match (opts.mode.as_str(), opts.fleet_cap) {
        ("evals", None) => run_evals(&opts),
        ("full", None) => run_full(&opts),
        ("evals", Some(cap)) => run_fleet(&opts, cap, false),
        ("full", Some(cap)) => run_fleet(&opts, cap, true),
        (other, _) => Err(format!("unknown replay mode '{}' (want 'evals' or 'full')", other)),
    }
}

// --- params file -----------------------------------------------------

/// Replay-only policy override, deserialized separately since Tunables
/// itself has no serde impl (live arms never take it from JSON).
///
/// **Every absent field falls back to `Tunables::default()`, NOT to
/// `Tunables::law(dur_s)`.** So the mere PRESENCE of a `tunables` object
/// drops the window off the production law — at 15m it reverts
/// `decided_k` 1.25 → 1.0, and at 5m it reverts `latch_release_on_proof`
/// true → false. That is long-standing behaviour and one committed fixture
/// (`btc-updown-15m-1787449500`, the pre-brake reproduction) depends on it,
/// so it is documented rather than fixed here.
///
/// The consequence for any A/B: **legs must emit the COMPLETE block,
/// baseline included**, carrying the law's values explicitly. A leg that
/// sends only its own knob is not being compared against the live engine,
/// it is being compared against `Tunables::default()`.
///
/// `deny_unknown_fields` because the failure it prevents is not a crash but
/// a FINDING: a leg whose knob name is misspelled deserializes to the
/// defaults, replays the baseline, and reports a delta of exactly zero —
/// which reads as "the knob does nothing" rather than "the config was
/// wrong". The A/B harness caught two such legs by their bit-identity to
/// the baseline and had to warn about it in prose; this makes it an error
/// at parse time instead. Same reasoning as `guard_shape_from`'s refusal to
/// fall back to `flat`.
#[derive(Debug, Clone, Deserialize)]
#[serde(deny_unknown_fields)]
struct TunablesOverride {
    #[serde(default = "default_distrust_net")]
    distrust_net: f64,
    #[serde(default = "default_avg_down_tol")]
    avg_down_tol: f64,
    #[serde(default = "default_decided_k")]
    decided_k: f64,
    #[serde(default = "default_decided_stale_s")]
    decided_stale_s: f64,
    #[serde(default = "default_late_clip_mult")]
    late_clip_mult: f64,
    #[serde(default = "default_late_terminal_agree")]
    late_terminal_agree: bool,
    #[serde(default = "default_latch_release_on_proof")]
    latch_release_on_proof: bool,
    #[serde(default = "default_disagreement_veto_c")]
    disagreement_veto_c: f64,
    #[serde(default = "default_disagreement_veto_late_only")]
    disagreement_veto_late_only: bool,
    #[serde(default = "default_decided_early_frac")]
    decided_early_frac: f64,
    #[serde(default = "default_decided_early_k")]
    decided_early_k: f64,
    /// `"flat"` (the live default) / `"sqrt_floor"` / `"linear_floor"` /
    /// `"quadrature"`. A name outside that set is a hard error, never a
    /// silent fallback to flat — a typo in a leg's config that quietly
    /// replayed the baseline would report "no effect" and look like a
    /// finding.
    #[serde(default = "default_guard_shape")]
    guard_shape: String,
}
fn default_distrust_net() -> f64 {
    Tunables::default().distrust_net
}
fn default_avg_down_tol() -> f64 {
    Tunables::default().avg_down_tol
}
fn default_decided_k() -> f64 {
    Tunables::default().decided_k
}
fn default_decided_stale_s() -> f64 {
    Tunables::default().decided_stale_s
}
fn default_late_clip_mult() -> f64 {
    Tunables::default().late_clip_mult
}
fn default_late_terminal_agree() -> bool {
    Tunables::default().late_terminal_agree
}
fn default_latch_release_on_proof() -> bool {
    Tunables::default().latch_release_on_proof
}
fn default_disagreement_veto_c() -> f64 {
    Tunables::default().disagreement_veto_c
}
fn default_disagreement_veto_late_only() -> bool {
    Tunables::default().disagreement_veto_late_only
}
fn default_decided_early_frac() -> f64 {
    Tunables::default().decided_early_frac
}
fn default_decided_early_k() -> f64 {
    Tunables::default().decided_early_k
}
fn default_guard_shape() -> String {
    "flat".to_string()
}

/// The `guard_shape` JSON contract. Unknown names are refused rather than
/// defaulted: a leg whose shape silently fell back to `flat` would replay
/// the baseline and report a Δ of exactly zero, which reads as "the knob
/// does nothing" instead of "the config was wrong".
fn guard_shape_from(name: &str) -> Result<GuardShape, String> {
    match name {
        "flat" => Ok(GuardShape::Flat),
        "sqrt_floor" => Ok(GuardShape::SqrtFloor),
        "linear_floor" => Ok(GuardShape::LinearFloor),
        "quadrature" => Ok(GuardShape::Quadrature),
        other => Err(format!(
            "unknown guard_shape '{}' — expected flat, sqrt_floor, linear_floor or quadrature",
            other
        )),
    }
}

impl TryFrom<TunablesOverride> for Tunables {
    type Error = String;
    fn try_from(t: TunablesOverride) -> Result<Self, String> {
        Ok(Tunables {
            distrust_net: t.distrust_net,
            avg_down_tol: t.avg_down_tol,
            decided_k: t.decided_k,
            decided_stale_s: t.decided_stale_s,
            late_clip_mult: t.late_clip_mult,
            late_terminal_agree: t.late_terminal_agree,
            latch_release_on_proof: t.latch_release_on_proof,
            disagreement_veto_c: t.disagreement_veto_c,
            disagreement_veto_late_only: t.disagreement_veto_late_only,
            decided_early_frac: t.decided_early_frac,
            decided_early_k: t.decided_early_k,
            guard_shape: guard_shape_from(&t.guard_shape)?,
        })
    }
}

#[derive(Debug, Clone, Deserialize)]
struct ParamsFileEntry {
    #[serde(flatten)]
    p: ArmParams,
    #[serde(default)]
    tunables: Option<TunablesOverride>,
}

/// One `--params` array entry as a value — the shape a fixture embeds so
/// that "as-armed params" mean the same thing in both entry points.
pub(crate) fn params_from_value(v: &Value) -> Result<(ArmParams, Option<Tunables>), String> {
    let e: ParamsFileEntry =
        serde_json::from_value(v.clone()).map_err(|e| format!("parse params: {}", e))?;
    let tun = e.tunables.map(Tunables::try_from).transpose()?;
    Ok((e.p, tun))
}

fn load_params_map(path: Option<&Path>) -> Result<HashMap<String, (ArmParams, Option<Tunables>)>, String> {
    let Some(path) = path else { return Ok(HashMap::new()) };
    let text = std::fs::read_to_string(path).map_err(|e| format!("read {}: {}", path.display(), e))?;
    let entries: Vec<ParamsFileEntry> =
        serde_json::from_str(&text).map_err(|e| format!("parse {}: {}", path.display(), e))?;
    entries
        .into_iter()
        .map(|e| {
            let tun = e
                .tunables
                .map(Tunables::try_from)
                .transpose()
                .map_err(|err| format!("{}: {}", e.p.slug, err))?;
            Ok((e.p.slug.clone(), (e.p, tun)))
        })
        .collect()
}

/// Minimal params from a bare slug — last resort for evals-mode ad-hoc
/// replay. A real run should always pass --params with the as-armed
/// values; this only exists so a tape record alone is still replayable.
fn synth_params(slug: &str) -> Result<(ArmParams, Option<Tunables>), String> {
    let parts: Vec<&str> = slug.split('-').collect();
    if parts.len() < 4 || parts[1] != "updown" {
        return Err(format!("cannot synthesize params from slug '{}': unrecognized shape", slug));
    }
    let coin = parts[0];
    let dur = parts[2];
    let start: f64 =
        parts[3].parse().map_err(|_| format!("bad start epoch in slug '{}'", slug))?;
    let mins: f64 = dur
        .strip_suffix('m')
        .and_then(|s| s.parse().ok())
        .ok_or_else(|| format!("bad duration '{}' in slug '{}'", dur, slug))?;
    let symbol = format!("{}USDT", coin.to_uppercase());
    let v = serde_json::json!({
        "slug": slug, "kind": "twap", "symbol": symbol,
        "token_up": format!("{}-up", slug), "token_down": format!("{}-down", slug),
        "start": start, "end": start + mins * 60.0,
        "sigma_bp_per_min": 3.0, "fee_rate": 0.07, "size_usdc": 100.0,
    });
    let p: ArmParams = serde_json::from_value(v).map_err(|e| format!("synth params: {}", e))?;
    Ok((p, None))
}

// --- outcomes override -------------------------------------------------

fn load_outcomes(path: Option<&Path>) -> Result<HashMap<String, String>, String> {
    let Some(path) = path else { return Ok(HashMap::new()) };
    let records = load_jsonl(path)?;
    Ok(records
        .iter()
        .filter_map(|v| Some((v.get("slug")?.as_str()?.to_string(), v.get("winner")?.as_str()?.to_string())))
        .collect())
}

// --- tape loading / grouping -------------------------------------------

pub(crate) fn load_jsonl(path: &Path) -> Result<Vec<Value>, String> {
    let f = std::fs::File::open(path).map_err(|e| format!("open {}: {}", path.display(), e))?;
    let mut out = Vec::new();
    for line in std::io::BufReader::new(f).lines() {
        let line = line.map_err(|e| format!("read {}: {}", path.display(), e))?;
        if line.trim().is_empty() {
            continue;
        }
        // Tolerate a truncated final line (process killed mid-write).
        if let Ok(v) = serde_json::from_str::<Value>(&line) {
            out.push(v);
        }
    }
    Ok(out)
}

/// Trailing epoch in a rolling-series slug — the window's start time and
/// the true replay order key (string order happens to agree here since
/// the epoch digit count is fixed, but this is the honest sort).
fn window_epoch(slug: &str) -> i64 {
    slug.rsplit('-').next().and_then(|s| s.parse().ok()).unwrap_or(i64::MAX)
}

/// Group records by exact slug for every slug matching `query` (exact or
/// prefix), each group time-sorted, groups ordered by window start.
fn group_by_slug(records: &[Value], query: &str) -> Vec<(String, Vec<Value>)> {
    let mut groups: HashMap<String, Vec<Value>> = HashMap::new();
    for rec in records {
        let Some(slug) = rec.get("slug").and_then(|s| s.as_str()) else { continue };
        if slug == query || slug.starts_with(query) {
            groups.entry(slug.to_string()).or_default().push(rec.clone());
        }
    }
    for v in groups.values_mut() {
        v.sort_by(|a, b| {
            a["t"].as_f64().partial_cmp(&b["t"].as_f64()).unwrap_or(std::cmp::Ordering::Equal)
        });
    }
    let mut ordered: Vec<(String, Vec<Value>)> = groups.into_iter().collect();
    ordered.sort_by(|a, b| (window_epoch(&a.0), &a.0).cmp(&(window_epoch(&b.0), &b.0)));
    ordered
}

/// Real (live) fire tally per slug, sourced from the eval tape's "fire"
/// records — the sim-vs-real diff this whole harness exists to produce.
/// Missing/unreadable tape degrades to empty rather than failing the run:
/// the comparison is a bonus, not a requirement for replay to work.
#[derive(Default, Clone)]
pub(crate) struct RealTally {
    pub(crate) fires: usize,
    pub(crate) notional: f64,
    pub(crate) first_fire_t: Option<f64>,
}

/// One window's live fire tally, counted off records already in hand — the
/// fixture form of `load_real_tally`, which reads a whole tape file.
pub(crate) fn real_tally_from(records: &[Value]) -> RealTally {
    let mut tally = RealTally::default();
    for rec in records {
        if rec.get("ev").and_then(|v| v.as_str()) != Some(EV_FIRE) {
            continue;
        }
        let t = rec["t"].as_f64().unwrap_or(0.0);
        tally.fires += 1;
        tally.notional += rec["ask"].as_f64().unwrap_or(0.0) * rec["size"].as_f64().unwrap_or(0.0);
        tally.first_fire_t = Some(tally.first_fire_t.map_or(t, |a: f64| a.min(t)));
    }
    tally
}

fn load_real_tally(path: &Path) -> HashMap<String, RealTally> {
    let Ok(records) = load_jsonl(path) else {
        eprintln!(
            "[replay] note: no eval tape at {} — real-fire comparison will be empty",
            path.display()
        );
        return HashMap::new();
    };
    let mut out: HashMap<String, RealTally> = HashMap::new();
    for rec in &records {
        if rec.get("ev").and_then(|v| v.as_str()) != Some(EV_FIRE) {
            continue;
        }
        let Some(slug) = rec.get("slug").and_then(|v| v.as_str()) else { continue };
        let t = rec["t"].as_f64().unwrap_or(0.0);
        let notional = rec["ask"].as_f64().unwrap_or(0.0) * rec["size"].as_f64().unwrap_or(0.0);
        let tally = out.entry(slug.to_string()).or_default();
        tally.fires += 1;
        tally.notional += notional;
        tally.first_fire_t = Some(tally.first_fire_t.map_or(t, |a: f64| a.min(t)));
    }
    out
}

// --- fill sim ------------------------------------------------------------

/// Conservative taker fill sim: every Buy/Sell decide() emits fills
/// instantly at the quoted price — no partials beyond what decide()
/// already sized against room/ask_size. `cost`/`fees` never shrink (money
/// spent stays spent); `cost_basis` does shrink on a sell, since it feeds
/// the next tick's position_floor (currently-committed notional).
///
/// `fees` is the `crate::fees::taker_fee` schedule on every crossing fill.
/// A post-only bid never fills here at all, which is also the honest maker
/// fee: the wallet charges a resting fill exactly nothing.
#[derive(Default)]
struct FillSim {
    shares: HashMap<String, f64>,
    cost_basis: HashMap<String, f64>,
    cost: f64,
    fees: f64,
    proceeds: f64,
    fire_count: usize,
    first_fire_t: Option<f64>,
    max_committed: f64,
    /// Post-only ASKS the strategy has left resting, by token. A resting ask
    /// is not a fill — see `lift_resting_asks` for the convention that turns
    /// one into a fill, and why it is the mirror of the post-only BUY's
    /// "never fills here at all".
    resting_asks: HashMap<String, RestingAsk>,
}

/// One post-only ask standing on the simulated book.
#[derive(Clone, Copy, Debug)]
struct RestingAsk {
    px: f64,
    size: f64,
}

/// Lift any resting post-only ask this tick's book has come up to.
///
/// The convention is `analysis/bracket_exit.md` §0's, verbatim, because that
/// is the convention the entire exit study was scored under: an ask at `r` is
/// LIFTED iff a sampled book instant shows our side's best **BID >= r**. Not
/// "a trade printed at r" — the whole book has to come to us. A bid spike
/// between two book samples is invisible and scores as no-fill, so the sim
/// under-counts lifts, which is the same safe direction the post-only BUY
/// takes by never filling at all.
///
/// The interval is half-open on the placing tick: a rest placed by THIS
/// tick's actions is only tested against LATER samples. That is not a
/// convenience — a post-only order that would match at placement is rejected
/// by the exchange outright (docs/maker-design.md §2), so an ask cannot both
/// be placed and be lifted by the book that was standing when it went out.
///
/// A lifted ask is the MAKER side of the trade and pays `fees::maker_fee` —
/// zero, on 526 of 526 wallet rows. This is the one place in the sim where a
/// fill is booked with no fee at all, and it is honest.
///
/// What it deliberately does NOT model is depth: the ask sells its whole size
/// the instant the bid reaches it, ignoring what size stood there. That is the
/// headline convention of `bracket_exit.md` §2 and its known weak point —
/// §6's depth-constrained variant flipped the best candidate's sign — so a
/// maker-exit policy replays here as an UPPER bound on how well it filled.
fn lift_resting_asks(arm: &ArmState, sim: &mut FillSim, view: &ArmView, fee_rate: f64) {
    for (token, top) in [(&arm.p.token_up, view.up), (&arm.p.token_down, view.dn)] {
        let Some(rest) = sim.resting_asks.get(token).copied() else { continue };
        let held = sim.shares.get(token).copied().unwrap_or(0.0);
        if held <= 1e-9 {
            // Nothing left to sell — an evacuation took the inventory out
            // from under the quote, and the cancel that went with it is what
            // pulls the order. Drop the rest rather than sell shares twice.
            sim.resting_asks.remove(token);
            continue;
        }
        let Some((bid, _)) = top.bid else { continue };
        if bid < rest.px {
            continue; // the book has not come to us
        }
        let size = rest.size.min(held);
        sim.resting_asks.remove(token);
        let fee = size * crate::fees::maker_fee(rest.px, fee_rate);
        book_sell(sim, token, rest.px, size, fee);
    }
}

/// Book one SELL against the sim's inventory.
///
/// `proceeds` carries the sale net of `fee`; `cost_basis` walks down at the
/// average entry, because it feeds the next tick's `position_floor` (which is
/// currently-committed notional, not money ever spent). `cost` and `fees` are
/// deliberately untouched — money spent stays spent.
fn book_sell(sim: &mut FillSim, token: &str, price: f64, size: f64, fee: f64) {
    sim.proceeds += size * price - fee;
    let held = sim.shares.entry(token.to_string()).or_insert(0.0);
    let basis = sim.cost_basis.entry(token.to_string()).or_insert(0.0);
    if *held > 1e-9 {
        let avg = *basis / *held;
        *basis = (*basis - avg * size).max(0.0);
    }
    *held = (*held - size).max(0.0);
}

fn apply_fills(
    arm: &mut ArmState,
    sim: &mut FillSim,
    out: &DecideOut,
    now: f64,
    fee_rate: f64,
    view: &ArmView,
) {
    // Rests standing from EARLIER ticks are tested against this tick's book
    // before this tick's own actions are applied — the half-open interval
    // documented on `lift_resting_asks`.
    lift_resting_asks(arm, sim, view, fee_rate);
    for a in &out.actions {
        match a {
            Action::Buy { token, price, size, post_only } => {
                // A post-only bid RESTS; it is not a cross. Filling one
                // here would manufacture liquidity that never existed —
                // precisely the class of lie replay exists to catch. The
                // honest conservative queue-ahead model is maker step 2
                // (docs/maker-design.md §5.3); until it lands, a maker
                // policy replays as strictly under-filled, which is the
                // safe direction for a strategy whose whole edge depends
                // on not overstating how easily a thin book fills.
                if *post_only {
                    continue;
                }
                let (price, size) = (*price, *size);
                let fee = size * taker_fee(price, fee_rate);
                sim.cost += size * price;
                sim.fees += fee;
                *sim.shares.entry(token.clone()).or_insert(0.0) += size;
                *sim.cost_basis.entry(token.clone()).or_insert(0.0) += size * price;
                arm.filled_usdc += size * price;
                arm.inflight.remove(token);
                sim.fire_count += 1;
                if sim.first_fire_t.is_none() {
                    sim.first_fire_t = Some(now);
                }
                let committed: f64 = sim.cost_basis.values().sum();
                if committed > sim.max_committed {
                    sim.max_committed = committed;
                }
            }
            Action::Sell { token, price, size, post_only } => {
                let (price, size) = (*price, *size);
                if *post_only {
                    // A post-only ask RESTS; it is not a cross. Booking it as
                    // a fill here would manufacture the counterparty the whole
                    // exit study refused to assume — the exact mirror of the
                    // post-only BUY twenty lines up. It becomes a fill only
                    // when a LATER book sample brings the bid to it.
                    sim.resting_asks.insert(token.clone(), RestingAsk { px: price, size });
                    continue;
                }
                let fee = size * taker_fee(price, fee_rate);
                book_sell(sim, token, price, size, fee);
            }
            Action::Cancel(token) => {
                // A cancel is what takes a resting ask OFF the book. Without
                // this the sim would keep lifting an order the strategy had
                // already pulled — at quiesce, at window close, or on the
                // evacuation's own cancel/sell pair.
                sim.resting_asks.remove(token);
            }
            _ => {}
        }
    }
}

fn settle_pnl(sim: &FillSim, p: &ArmParams, winner: &str) -> f64 {
    let token = if winner == "up" { &p.token_up } else { &p.token_down };
    let winner_shares = sim.shares.get(token).copied().unwrap_or(0.0);
    winner_shares - sim.cost - sim.fees + sim.proceeds
}

// --- report shape --------------------------------------------------------

fn build_report(
    slug: &str,
    mode: &str,
    sim: &FillSim,
    real: &RealTally,
    outcome: Option<String>,
    pnl: Option<f64>,
) -> Value {
    serde_json::json!({
        "slug": slug,
        "mode": mode,
        "sim": {
            "fires": sim.fire_count,
            "notional": sim.cost,
            "fees": sim.fees,
            "max_committed": sim.max_committed,
            "first_fire_t": sim.first_fire_t,
            "pnl": pnl,
            "outcome": outcome,
        },
        "real": {
            "fires": real.fires,
            "notional": real.notional,
            "first_fire_t": real.first_fire_t,
        },
    })
}

fn aggregate(query: &str, mode: &str, reports: &[Value]) -> Value {
    let sum_f = |section: &str, key: &str| -> f64 {
        reports.iter().filter_map(|r| r[section][key].as_f64()).sum()
    };
    let sum_i = |section: &str, key: &str| -> i64 {
        reports.iter().filter_map(|r| r[section][key].as_i64()).sum()
    };
    let min_first_fire = |section: &str| -> Option<f64> {
        reports
            .iter()
            .filter_map(|r| r[section]["first_fire_t"].as_f64())
            .fold(None, |acc, v| Some(acc.map_or(v, |a: f64| a.min(v))))
    };
    let max_committed = reports
        .iter()
        .filter_map(|r| r["sim"]["max_committed"].as_f64())
        .fold(0.0_f64, f64::max);
    let pnl_vals: Vec<f64> = reports.iter().filter_map(|r| r["sim"]["pnl"].as_f64()).collect();
    let pnl = if pnl_vals.is_empty() { None } else { Some(pnl_vals.iter().sum::<f64>()) };
    serde_json::json!({
        "slug": format!("{} (aggregate x{})", query, reports.len()),
        "mode": mode,
        "sim": {
            "fires": sum_i("sim", "fires"),
            "notional": sum_f("sim", "notional"),
            "fees": sum_f("sim", "fees"),
            "max_committed": max_committed,
            "first_fire_t": min_first_fire("sim"),
            "pnl": pnl,
            "outcome": Value::Null,
        },
        "real": {
            "fires": sum_i("real", "fires"),
            "notional": sum_f("real", "notional"),
            "first_fire_t": min_first_fire("real"),
        },
    })
}

fn write_jsonl(path: &Path, values: &[Value]) -> Result<(), String> {
    use std::io::Write;
    let mut f = std::fs::File::create(path).map_err(|e| format!("create {}: {}", path.display(), e))?;
    for v in values {
        writeln!(f, "{}", v).map_err(|e| format!("write {}: {}", path.display(), e))?;
    }
    Ok(())
}

fn finalize(query: &str, mode: &str, reports: &[Value], out: Option<&Path>) -> Result<(), String> {
    if reports.is_empty() {
        return Err(format!("no window could be replayed for '{}'", query));
    }
    for r in reports {
        println!("{}", r);
    }
    let agg = aggregate(query, mode, reports);
    println!("{}", agg);
    if let Some(path) = out {
        let mut all = reports.to_vec();
        all.push(agg);
        write_jsonl(path, &all)?;
    }
    Ok(())
}

fn home_dir() -> PathBuf {
    std::env::var("HOME").map(PathBuf::from).unwrap_or_else(|_| PathBuf::from("."))
}
fn default_eval_tape_path() -> PathBuf {
    home_dir().join(".pmt/engine/updown-tape.jsonl")
}
fn default_book_tape_path() -> PathBuf {
    home_dir().join(".pmt/engine/book-tape.jsonl")
}

// --- evals mode ------------------------------------------------------

/// Evals-mode view: only ask prices survive the eval tape (no bids, no
/// ask size recorded) — the clip sizer caps by clip_usdc/room regardless,
/// so an unbounded size just means "no book-depth limit," matching what
/// actually gated real fires.
fn view_from_sides(sides: &Value, sim: &FillSim) -> ArmView {
    let mut up = TopOfBook::default();
    let mut dn = TopOfBook::default();
    if let Some(arr) = sides.as_array() {
        for s in arr {
            let Some(ask) = s.get("ask").and_then(|v| v.as_f64()) else { continue };
            match s.get("side").and_then(|v| v.as_str()) {
                Some("up") => up.ask = Some((ask, f64::INFINITY)),
                Some("down") => dn.ask = Some((ask, f64::INFINITY)),
                _ => {}
            }
        }
    }
    ArmView { up, dn, held_up: 0.0, held_dn: 0.0, position_floor: sim.cost_basis.values().sum() }
}

fn replay_evals_window(
    p: &ArmParams,
    tun: Option<Tunables>,
    recs: &[Value],
    outcome_override: Option<String>,
    real: &RealTally,
) -> Value {
    replay_evals_window_traced(p, tun, recs, outcome_override, real).0
}

/// `replay_evals_window` plus the decision tape decide() emitted along the
/// way — the SAME records the live engine appends to updown-tape.jsonl.
/// Characterization fixtures assert against those records rather than
/// re-deriving fire side/mode from the fill sim, so a fixture pins the
/// engine's own account of what it did.
pub(crate) fn replay_evals_window_traced(
    p: &ArmParams,
    tun: Option<Tunables>,
    recs: &[Value],
    outcome_override: Option<String>,
    real: &RealTally,
) -> (Value, Vec<Value>) {
    let mut arm = ArmState::with_params(p.clone());
    arm.subscribed = true;
    if let Some(t) = tun {
        arm.tunables = t;
    }
    let mut sim = FillSim::default();
    let mut trace: Vec<Value> = Vec::new();
    let mut last_p_up: Option<f64> = None;

    for rec in recs {
        let now = rec["t"].as_f64().unwrap_or(0.0);
        match rec.get("ev").and_then(|v| v.as_str()).unwrap_or("") {
            EV_EVAL => {
                let p_up = rec["p_up"].as_f64().unwrap_or(0.5);
                last_p_up = Some(p_up);
                let model = model_from_eval_record(rec, p_up, p, &arm.tunables);
                let view = view_from_sides(&rec["sides"], &sim);
                let out = arm.decide(&view, Ok(model), now);
                trace.extend(out.tape.iter().cloned());
                apply_fills(&mut arm, &mut sim, &out, now, p.fee_rate, &view);
            }
            EV_GATED => {
                // decide()'s Err branch never reads `view` — a default is
                // fine, we're only here to keep gate bookkeeping faithful.
                // A default view has no bid, so a gated tick can never lift a
                // resting ask: an evals-mode tape carries no bids anyway (see
                // `view_from_sides`), which makes this mode strictly
                // rest-and-never-fill for maker sells.
                let view = ArmView::default();
                let out = arm.decide(&view, Err(gate_from_record(rec)), now);
                trace.extend(out.tape.iter().cloned());
                apply_fills(&mut arm, &mut sim, &out, now, p.fee_rate, &view);
            }
            _ => {} // fire/roll/cleanup — real fires come from the shared tally
        }
    }

    let outcome = outcome_override.or_else(|| {
        last_p_up.map(|p_up| if p_up >= 0.5 { "up".to_string() } else { "down".to_string() })
    });
    let pnl = outcome.as_deref().map(|w| settle_pnl(&sim, p, w));
    (build_report(&p.slug, "evals", &sim, real, outcome, pnl), trace)
}

/// Rebuild the model read that a recorded `eval` line captured. One
/// implementation, shared by the per-window and the fleet driver — two
/// copies of this parse would let the two drivers judge different engines.
fn model_from_eval_record(rec: &Value, p_up: f64, p: &ArmParams, tun: &Tunables) -> ModelEval {
    // Evals mode replays the recorded decidedness as-is — it is the flag the
    // live engine actually acted on. The decidedness knobs may only SUBTRACT
    // from it, and only where the record carries the two numbers to judge by.
    //
    // The `k_eff > 1.0` guard is what makes the defaults provably inert: at
    // k = 1.0 the recomputation would just be the record's own inequality,
    // and on tape written before banked_bp/cushion_bp shipped it would read
    // 0 > 0 and wrongly un-decide the window. `decided_early_frac` routes
    // through the SAME guard rather than adding a second one — the early
    // knob raises k only on early ticks, so on a late tick `k_eff` collapses
    // to `decided_k` and the recomputation is correctly skipped.
    //
    // `elapsed_frac` is rebuilt from the record's own `t` against the arm's
    // window rather than read from a tape field: `elapsed_frac` is written
    // on `fire` records, not on `eval` records, so there is nothing to read.
    let now = rec["t"].as_f64().unwrap_or(p.start);
    let elapsed_frac = (now - p.start) / (p.end - p.start).max(1.0);
    let sig_bp = rec["sig_bp"].as_f64().unwrap_or(0.0);
    let banked_bp = rec.get("banked_bp").and_then(|v| v.as_f64()).unwrap_or(0.0);
    let recorded_cushion = rec.get("cushion_bp").and_then(|v| v.as_f64()).unwrap_or(0.0);
    // Tape written before the dynamic guard shipped has no "guard_bp" field
    // — fall back to the arm's static param, matching what the engine
    // actually enforced back then.
    let guard_bp = rec.get("guard_bp").and_then(|v| v.as_f64()).unwrap_or(p.basis_guard_bp);

    let reshaped = reshape_recorded_cushion(tun, rec, p, now, sig_bp, recorded_cushion);
    let cushion_bp = reshaped.unwrap_or(recorded_cushion);

    let mut banked_decided = rec["banked_decided"].as_bool().unwrap_or(false);
    if reshaped.is_some() {
        // The cushion itself moved, so the recorded flag is an answer to a
        // different inequality and cannot be trusted in EITHER direction —
        // a narrower cushion can newly decide a window as easily as a wider
        // one can un-decide it. Recompute outright. Safe because the
        // reshape only returns `Some` where the record carried every number
        // the live formula uses, so this is the live inequality on live
        // inputs, not a reconstruction of one.
        banked_decided = tun.decided(banked_bp, cushion_bp, p_up, elapsed_frac);
    } else if banked_decided && tun.decided_k_at(elapsed_frac) > 1.0 {
        if let (Some(b), Some(c)) = (
            rec.get("banked_bp").and_then(|v| v.as_f64()),
            rec.get("cushion_bp").and_then(|v| v.as_f64()),
        ) {
            banked_decided = tun.decided(b, c, p_up, elapsed_frac);
        }
    }
    ModelEval {
        p_up,
        sig_bp,
        rho: rec["rho"].as_f64().unwrap_or(0.0),
        banked_decided,
        margin_bp: rec.get("margin_bp").and_then(|v| v.as_f64()).unwrap_or(0.0),
        banked_margin_bp: banked_bp,
        cushion_bp,
        guard_bp,
        flip_proof: false,
        // Absent on every tape written before `term_bp` shipped, and on
        // every Binance arm forever. `None` is the honest reconstruction
        // either way, and it makes the `late_terminal_agree` knob a no-op
        // in evals mode rather than a gate acting on a guessed zero — this
        // knob's A/B belongs in `--mode full`, which recomputes the read.
        term_bp: rec.get("term_bp").and_then(|v| v.as_f64()),
    }
}

/// Re-shape a RECORDED cushion under a non-`Flat` `guard_shape`, or `None`
/// when this tick cannot be re-shaped honestly.
///
/// Evals mode has no feed, so a cushion cannot be recomputed from scratch —
/// but the live `range_avg` form inverts exactly, with no assumption about
/// feed gaps or banked-minute counts:
///
/// ```text
///   vol   = cushion - guard
///   denom = sig * sqrt(max(rem/60, 0.02) / 3)
///   rw    = vol / denom            (= rem / window)
///   window = rem / rw
/// ```
///
/// which is `analysis/cushion_calibration.md` §0's recovery, measured there
/// to reproduce the taped `cushion_bp` bit-exactly on 90.6% of ticks (the
/// 9.4% residual is feed gaps, concentrated in rtds arms). Every one of the
/// conditions below is a refusal to guess on the other 9.4%:
///
///   * `Flat` — nothing to do, and the caller keeps today's subtract-only
///     decidedness path rather than a full recomputation.
///   * a settle_rule other than `range_avg`, or a `close_open` arm — the
///     recorded cushion came from `terminal_lock` (or is identically zero)
///     and this algebra does not describe it. `GuardShape` deliberately
///     does not reach those cushions in full mode either, so refusing here
///     keeps the two conventions answering the same question.
///   * **no explicit `guard_bp` on the record** — the static-param fallback
///     is right for reading a cushion the engine already computed and wrong
///     for taking one apart, because `vol = cushion - guard` with the wrong
///     guard yields a `vol` that never existed.
///   * a `rw` outside `(0, 1]` — the inversion did not close, so the record
///     is one of the feed-gap ticks.
///
/// A refusal leaves the recorded cushion standing, i.e. that tick replays
/// FLAT. The bias is therefore always toward the baseline: an evals-mode
/// leg understates its own knob by whatever share of ticks refuse, and the
/// report has to carry that share.
fn reshape_recorded_cushion(
    tun: &Tunables,
    rec: &Value,
    p: &ArmParams,
    now: f64,
    sig_bp: f64,
    recorded_cushion: f64,
) -> Option<f64> {
    if tun.guard_shape == GuardShape::Flat {
        return None;
    }
    if p.kind == "close_open" || (p.settle_rule != "range_avg" && !p.settle_rule.is_empty()) {
        return None;
    }
    let guard_bp = rec.get("guard_bp").and_then(|v| v.as_f64())?;
    let rem = p.end - now;
    if rem <= 0.0 || sig_bp <= 0.0 {
        return None;
    }
    let vol_bp = recorded_cushion - guard_bp;
    let denom = sig_bp * ((rem / 60.0).max(0.02) / 3.0).sqrt();
    if vol_bp <= 0.0 || denom <= 0.0 {
        return None;
    }
    let rw = vol_bp / denom;
    if !(1e-6 < rw && rw <= 1.000_001) {
        return None;
    }
    Some(tun.guard_shape.cushion_bp(guard_bp, vol_bp, rem, rem / rw.min(1.0)))
}

/// Rebuild a `GateReason` from a recorded `gated` tape line.
///
/// Structured fields when the line has them; `None` when it doesn't —
/// every line written before those fields shipped, which is most of the
/// existing corpus. Never parses the prose: an old line simply replays as
/// a numberless gate, which is exactly what decide() does with it anyway.
fn gate_from_record(rec: &Value) -> GateReason {
    let num = |k: &str| rec.get(k).and_then(|v| v.as_f64());
    GateReason {
        reason: rec["reason"].as_str().unwrap_or("gated").to_string(),
        margin_bp: num("margin_bp"),
        banked_bp: num("banked_bp"),
        cushion_bp: num("cushion_bp"),
        guard_bp: num("guard_bp"),
        spot_age_s: num("spot_age_s"),
    }
}

fn run_evals(opts: &ReplayOpts) -> Result<(), String> {
    let tape_path = opts.tape.clone().unwrap_or_else(default_eval_tape_path);
    let records = load_jsonl(&tape_path)?;
    let windows = group_by_slug(&records, &opts.slug);
    if windows.is_empty() {
        return Err(format!("no records for slug '{}' in {}", opts.slug, tape_path.display()));
    }
    let params_map = load_params_map(opts.params.as_deref())?;
    let outcomes = load_outcomes(opts.outcomes.as_deref())?;
    let real_by_slug = load_real_tally(&tape_path);

    let mut reports = Vec::new();
    for (slug, recs) in &windows {
        let (p, tun) = match params_map.get(slug) {
            Some(v) => v.clone(),
            None => {
                eprintln!(
                    "[replay] warning: no --params entry for '{}' — synthesizing minimal params; \
                     pass --params with the as-run values for a faithful replay",
                    slug
                );
                synth_params(slug)?
            }
        };
        let real = real_by_slug.get(slug).cloned().unwrap_or_default();
        reports.push(replay_evals_window(&p, tun, recs, outcomes.get(slug).cloned(), &real));
    }
    finalize(&opts.slug, "evals", &reports, opts.out.as_deref())
}

// --- full mode -------------------------------------------------------

fn kline_cache_path(symbol: &str) -> Result<PathBuf, String> {
    let dir = home_dir().join(".pmt/corpus");
    std::fs::create_dir_all(&dir).map_err(|e| format!("corpus dir: {}", e))?;
    Ok(dir.join(format!("klines-1m-{}.jsonl", symbol)))
}

fn load_kline_cache(path: &Path) -> BTreeMap<i64, Kline> {
    let mut rows = BTreeMap::new();
    let Ok(f) = std::fs::File::open(path) else { return rows };
    for line in std::io::BufReader::new(f).lines().map_while(Result::ok) {
        if let Ok(v) = serde_json::from_str::<Value>(&line) {
            if let (Some(t), Some(o), Some(c)) = (v["t"].as_i64(), v["o"].as_f64(), v["c"].as_f64())
            {
                rows.insert(t, Kline { t, o, c });
            }
        }
    }
    rows
}

fn append_kline_rows(path: &Path, rows: &[Kline]) -> Result<(), String> {
    use std::io::Write;
    let mut f = std::fs::OpenOptions::new()
        .create(true)
        .append(true)
        .open(path)
        .map_err(|e| format!("kline cache open: {}", e))?;
    for r in rows {
        writeln!(f, "{}", serde_json::json!({"t": r.t, "o": r.o, "c": r.c}))
            .map_err(|e| format!("kline cache write: {}", e))?;
    }
    Ok(())
}

fn cache_covers(rows: &BTreeMap<i64, Kline>, lo: i64, hi: i64) -> bool {
    let mut m = lo;
    while m < hi {
        if !rows.contains_key(&m) {
            return false;
        }
        m += 60;
    }
    true
}

fn fetch_klines(
    client: &reqwest::blocking::Client,
    symbol: &str,
    start_epoch_s: i64,
) -> Result<Vec<Kline>, String> {
    let start_ms = (start_epoch_s * 1000).to_string();
    let v: Value = client
        .get(format!("{}/api/v3/klines", BINANCE_DATA))
        .query(&[("symbol", symbol), ("interval", "1m"), ("startTime", &start_ms), ("limit", "500")])
        .send()
        .and_then(|r| r.error_for_status())
        .map_err(|e| format!("klines fetch: {}", e))?
        .json()
        .map_err(|e| format!("klines json: {}", e))?;
    // Same shaper the live feed poller runs — replay must reconstruct the
    // feed from the identical parse, not a lookalike.
    Ok(shape_klines(&v))
}

/// Cached 1m klines for `symbol` covering [start-2700, end). Fetches and
/// appends only the missing range (`ensure_klines`, the module's only
/// network path) and is only ever reached from `--mode full` — never from
/// tests, never from evals mode. The fleet driver calls `ensure_klines`
/// itself, once per symbol over the union of that symbol's windows: this
/// re-reads a multi-megabyte cache file, which is the entire runtime of a
/// several-hundred-window run if done per window.
fn klines_for_window(symbol: &str, start: i64, end: i64) -> Result<BTreeMap<i64, Kline>, String> {
    let mut rows = load_kline_cache(&kline_cache_path(symbol)?);
    ensure_klines(symbol, start - KLINE_LOOKBACK_S, end, &mut rows)?;
    Ok(rows)
}

/// Top `rows` up so it covers [lo, hi) at minute resolution, fetching only
/// the missing stretch and appending it to the on-disk cache.
///
/// Loops rather than fetching once: Binance caps a klines page at 500
/// minutes, so a single call covers ~8h. A fleet run spans a whole night
/// across several symbols, and a one-shot fetch would leave a silent hole
/// in the middle of the feed rather than an error.
fn ensure_klines(
    symbol: &str,
    lo: i64,
    hi: i64,
    rows: &mut BTreeMap<i64, Kline>,
) -> Result<(), String> {
    let lo = lo - lo.rem_euclid(60);
    let hi = hi - hi.rem_euclid(60);
    if cache_covers(rows, lo, hi) {
        return Ok(());
    }
    let path = kline_cache_path(symbol)?;
    let client = reqwest::blocking::Client::builder()
        .timeout(std::time::Duration::from_secs(10))
        .build()
        .map_err(|e| format!("client: {}", e))?;
    let mut cursor = lo;
    while cursor < hi {
        if rows.contains_key(&cursor) {
            cursor += 60;
            continue;
        }
        let fetched = fetch_klines(&client, symbol, cursor)?;
        let new_rows: Vec<Kline> =
            fetched.into_iter().filter(|r| !rows.contains_key(&r.t)).collect();
        if new_rows.is_empty() {
            // Binance has nothing there (delisted minute, or we asked past
            // the tape's own edge). Stepping past it beats spinning.
            cursor += 60;
            continue;
        }
        append_kline_rows(&path, &new_rows)?;
        for r in &new_rows {
            rows.insert(r.t, *r);
        }
    }
    Ok(())
}

/// Reconstruct the per_min/closes/rho feed at decision time `now`,
/// honoring the same look-ahead the live feed thread has: a minute counts
/// only once fully closed; the forming minute uses (open + latest
/// spot)/2, since the kline open prints at minute start but its true
/// close isn't known yet — mirrors poll_binance's live (o+c)/2 for closed
/// bars and the model's own forming-bar approximation.
fn feed_state_at(
    rows: &BTreeMap<i64, Kline>,
    start: i64,
    spot: f64,
    spot_ts: f64,
    now: f64,
) -> FeedState {
    let m0 = (now / 60.0).floor() as i64 * 60;
    let mut per_min = BTreeMap::new();
    let mut closes = Vec::new();
    for (&t, r) in rows.range(start - KLINE_LOOKBACK_S..=m0) {
        if (t + 60) as f64 <= now {
            per_min.insert(t, r.mid());
            closes.push(r.c);
        } else if t == m0 {
            per_min.insert(t, (r.o + spot) / 2.0);
        }
    }
    closes.push(spot);
    let rho = lag1_autocorr(&closes, 60);
    // `spot_hist` stays empty on this path and that is the contract, not an
    // omission: it is the settlement stream's 1 Hz buffer, and a Binance
    // arm has no settlement stream. `terminal_bank` falls back to its spot
    // proxy when the buffer is empty, so a kline-fed window prices exactly
    // as it did before the buffer existed.
    FeedState {
        spot, spot_ts, per_min, candle_open: None, closes, rho, last_err: None,
        spot_hist: Vec::new(),
    }
}

/// True TWAP-proxy settlement, scored after the window closed — no
/// look-ahead concern here, the outcome already happened. Same math the
/// model banks on: avg of the window's per-minute marks vs the range-start
/// reference.
///
/// Takes the marks rather than the klines so both feeds settle through one
/// function: a kline mid IS the per-minute mark on a Binance-fed arm, and
/// on a stream-fed one the marks are the settlement TWAP prints themselves.
fn settle_from_marks(marks: &BTreeMap<i64, f64>, start: i64, end: i64) -> Option<String> {
    let ref_px = *marks.get(&(start - 60))?;
    let (mut sum, mut n) = (0.0, 0usize);
    for v in marks.range(start..end).map(|(_, v)| *v) {
        sum += v;
        n += 1;
    }
    if n == 0 {
        return None;
    }
    Some(if sum / n as f64 >= ref_px { "up".to_string() } else { "down".to_string() })
}

fn settle_winner(rows: &BTreeMap<i64, Kline>, start: i64, end: i64) -> Option<String> {
    let marks: BTreeMap<i64, f64> = rows.iter().map(|(t, r)| (*t, r.mid())).collect();
    settle_from_marks(&marks, start, end)
}

// --- full mode's market-data source --------------------------------------

/// Where `--mode full` gets the FeedState it hands to `eval_model`.
///
/// The arm's own `feed` param picks it, exactly as it picks which threads
/// `start_feeds` spawns live. That is the whole discipline: a window is
/// replayed off the series it traded on, never off a convenient stand-in.
/// Replaying a stream-fed arm against Binance klines reconstructs a model
/// read that never happened — different venue, different basis, and the
/// cross-venue gap this feed exists to delete put right back in.
pub(crate) enum FullFeed {
    /// `feed = "binance"`: 1m klines reshaped per tick by `feed_state_at`.
    /// `Arc` because a fleet run shares one symbol's cache across every
    /// window on it, and the cache is megabytes.
    Klines(Arc<BTreeMap<i64, Kline>>),
    /// `feed = "rtds"`: the settlement stream replayed through the live hub.
    Rtds(Box<RtdsTimeline>),
}

impl FullFeed {
    /// The model's inputs as of `now`. `spot`/`spot_ts` come off the book
    /// record for a Binance arm — the recorder stamped them from that arm's
    /// own feed thread — and are ignored for a stream-fed one, whose spot
    /// is a chainlink print like every other number it reads.
    fn state_at(&mut self, start: i64, spot: f64, spot_ts: f64, now: f64) -> FeedState {
        match self {
            FullFeed::Klines(rows) => feed_state_at(rows, start, spot, spot_ts, now),
            FullFeed::Rtds(tl) => tl.state_at(now),
        }
    }

    fn settle(&mut self, start: i64, end: i64) -> Option<String> {
        match self {
            FullFeed::Klines(rows) => settle_winner(rows, start, end),
            FullFeed::Rtds(tl) => settle_from_marks(&tl.settle_marks(end as f64), start, end),
        }
    }
}

/// The rtds symbols a set of matched windows needs, or empty when every one
/// of them is Binance-fed — which is the signal not to load the corpus at
/// all. A hundred megabytes read for a run that has no use for it is the
/// difference between a replay that feels instant and one that doesn't.
fn rtds_symbols_needed<'a>(
    windows: impl Iterator<Item = &'a ArmParams>,
) -> BTreeSet<String> {
    windows
        .filter(|p| p.feed == FEED_RTDS)
        // A symbol the stream never carried yields nothing here; the
        // timeline refuses it by name a moment later, which is the message
        // that actually helps.
        .filter_map(|p| updown_rtds::rtds_symbol(&p.symbol))
        .collect()
}

/// Load the corpus iff some matched window is stream-fed.
fn load_rtds_corpus(
    opts: &ReplayOpts,
    wanted: &BTreeSet<String>,
) -> Result<Option<RtdsCorpus>, String> {
    if wanted.is_empty() {
        return Ok(None);
    }
    let path = opts.rtds_corpus.clone().unwrap_or_else(rtds::default_corpus_dir);
    RtdsCorpus::load(&path, wanted).map(Some)
}

/// This window's feed source. Fails closed on a stream-fed arm with no
/// corpus behind it rather than falling back to klines: a silent fallback
/// would replay the window off the very venue the arm was moved away from.
fn full_feed_for(p: &ArmParams, corpus: Option<&RtdsCorpus>) -> Result<FullFeed, String> {
    if p.feed != FEED_RTDS {
        return Ok(FullFeed::Klines(Arc::new(klines_for_window(
            &p.symbol,
            p.start as i64,
            p.end as i64,
        )?)));
    }
    let corpus = corpus.ok_or_else(|| {
        format!("{}: feed 'rtds' needs an RTDS recorder corpus (--rtds-corpus)", p.slug)
    })?;
    RtdsTimeline::new(p, corpus).map(|tl| FullFeed::Rtds(Box::new(tl)))
}

fn view_from_book_record(rec: &Value, sim: &FillSim, p: &ArmParams) -> ArmView {
    let level = |bid_key: &str, sz_key: &str| -> Option<(f64, f64)> {
        rec[bid_key].as_f64().zip(rec[sz_key].as_f64())
    };
    ArmView {
        up: TopOfBook { bid: level("up_bid", "up_bid_sz"), ask: level("up_ask", "up_ask_sz") },
        dn: TopOfBook { bid: level("dn_bid", "dn_bid_sz"), ask: level("dn_ask", "dn_ask_sz") },
        held_up: sim.shares.get(&p.token_up).copied().unwrap_or(0.0),
        held_dn: sim.shares.get(&p.token_down).copied().unwrap_or(0.0),
        position_floor: sim.cost_basis.values().sum(),
    }
}

/// Full-mode replay over a feed source the caller already holds — the
/// offline seam.
///
/// `klines_for_window` is this module's ONLY network path; a fixture run
/// never reaches it, because the fixture carries its own kline (or rtds)
/// slice and hands the built source in here. That is a structural
/// guarantee, not an env var CI might forget to set (issue #5, Phase 3).
pub(crate) fn replay_full_window_with(
    feed_src: &mut FullFeed,
    p: &ArmParams,
    tun: Option<Tunables>,
    recs: &[Value],
    outcome_override: Option<String>,
    real: &RealTally,
) -> (Value, Vec<Value>) {
    let mut arm = ArmState::with_params(p.clone());
    arm.subscribed = true;
    if let Some(t) = tun {
        arm.tunables = t;
    }
    let mut sim = FillSim::default();
    let mut trace: Vec<Value> = Vec::new();

    for rec in recs {
        let now = rec["t"].as_f64().unwrap_or(0.0);
        let spot = rec["spot"].as_f64().unwrap_or(0.0);
        let spot_age = rec["spot_age_s"].as_f64().unwrap_or(0.0);
        let feed = feed_src.state_at(p.start as i64, spot, now - spot_age, now);
        // Static guard only — replay has no recorded oracle-sample corpus
        // (yet; oracle-tape.jsonl starts accumulating once the dynamic
        // guard runs live), so pass the operator's param unchanged: replay
        // output must match the exact static-guard behavior this window
        // ran under live.
        let model = eval_model(p, &feed, now, p.basis_guard_bp, &arm.tunables);
        let view = view_from_book_record(rec, &sim, p);
        let out = arm.decide(&view, model, now);
        trace.extend(out.tape.iter().cloned());
        apply_fills(&mut arm, &mut sim, &out, now, p.fee_rate, &view);
    }

    let outcome =
        outcome_override.or_else(|| feed_src.settle(p.start as i64, p.end as i64));
    let pnl = outcome.as_deref().map(|w| settle_pnl(&sim, p, w));
    (build_report(&p.slug, "full", &sim, real, outcome, pnl), trace)
}

/// Dump the decision trace, one JSON record per line. `None` writes nothing
/// and costs nothing — a study opts in.
fn write_trace(path: Option<&Path>, trace: &[Value]) -> Result<(), String> {
    let Some(path) = path else { return Ok(()) };
    let mut out = String::new();
    for r in trace {
        out.push_str(&r.to_string());
        out.push('\n');
    }
    std::fs::write(path, out).map_err(|e| format!("write {}: {}", path.display(), e))?;
    eprintln!("[replay] trace: {} record(s) -> {}", trace.len(), path.display());
    Ok(())
}

fn run_full(opts: &ReplayOpts) -> Result<(), String> {
    let book_path = opts.book_tape.clone().unwrap_or_else(default_book_tape_path);
    let tape_path = opts.tape.clone().unwrap_or_else(default_eval_tape_path);
    let book_records = load_jsonl(&book_path)?;
    let windows = group_by_slug(&book_records, &opts.slug);
    if windows.is_empty() {
        return Err(format!("no book records for slug '{}' in {}", opts.slug, book_path.display()));
    }
    let params_map = load_params_map(opts.params.as_deref())?;
    if params_map.is_empty() {
        return Err("--params is required for --mode full".to_string());
    }
    let outcomes = load_outcomes(opts.outcomes.as_deref())?;
    let real_by_slug = load_real_tally(&tape_path);
    let matched = || windows.iter().filter_map(|(s, _)| params_map.get(s)).map(|(p, _)| p);
    let corpus = load_rtds_corpus(opts, &rtds_symbols_needed(matched()))?;

    let mut reports = Vec::new();
    let mut traced: Vec<Value> = Vec::new();
    for (slug, recs) in &windows {
        let Some((p, tun)) = params_map.get(slug).cloned() else {
            eprintln!("[replay] skipping '{}': no --params entry for this slug", slug);
            continue;
        };
        let real = real_by_slug.get(slug).cloned().unwrap_or_default();
        // A feed the window cannot be replayed against skips that window
        // loudly and leaves the rest of the run alone — same as a kline
        // hole does. Silence is what the refusal messages exist to avoid.
        let mut feed_src = match full_feed_for(&p, corpus.as_ref()) {
            Ok(f) => f,
            Err(e) => {
                eprintln!("[replay] skipping '{}': {}", slug, e);
                continue;
            }
        };
        let (report, trace) = replay_full_window_with(
            &mut feed_src,
            &p,
            tun,
            recs,
            outcomes.get(slug).cloned(),
            &real,
        );
        traced.extend(trace);
        reports.push(report);
    }
    write_trace(opts.trace.as_deref(), &traced)?;
    finalize(&opts.slug, "full", &reports, opts.out.as_deref())
}

// --- fleet mode (R7) -------------------------------------------------
//
// The per-window drivers above run each slug to completion before starting
// the next, which is fine when arms are independent — and useless for a
// fleet cap, whose whole subject is what several arms are doing to each
// other AT THE SAME MOMENT. A per-slug "fleet" cap would cap nothing.
//
// So this driver does the one thing that makes the number honest: it
// merges every matched window's records into a single global timestamp
// order and steps them through their own `decide_fleet` against one shared
// un-decided pool, exactly the way `Updown::on_tick` hands one `&mut f64`
// to every arm in a tick.
//
// The pool is re-summed from live sim state before each record rather than
// carried, mirroring the engine's per-tick pre-pass (and for the same
// reason: committed notional is a position-tracker read, never a running
// counter — the engine's oldest gotcha). Within one record the `&mut`
// still does its job across the arm's two sides.
//
// What it is NOT: fills are still the per-window conservative FillSim, and
// each window's book/model records are exactly the ones that window
// recorded live. The interleaving is in time and in the shared budget, not
// in liquidity.

/// One window's live state inside an interleaved fleet run.
struct FleetArm {
    p: ArmParams,
    arm: ArmState,
    sim: FillSim,
    last_p_up: Option<f64>,
    real: RealTally,
    /// Decide passes where the fleet cap — not this arm's own budget or a
    /// window brake — is what stopped a clip.
    fleet_blocks: usize,
    /// Full mode only. Per arm, never per symbol: a stream-fed timeline
    /// carries its own hub consumer and its own cursor through the corpus,
    /// so two windows on one symbol are two timelines. The Binance side
    /// shares its cache through the `Arc` instead.
    feed_src: Option<FullFeed>,
}

impl FleetArm {
    /// This arm's live contribution to the shared pool. `undecided_committed`
    /// is the engine's own function: replay hands it the fill sim's cost
    /// basis where the live tick hands it the position tracker, so there is
    /// one definition of the pool and not two (L18).
    fn undecided(&self, now: f64) -> f64 {
        self.arm.undecided_committed(self.sim.cost_basis.values().sum(), now)
    }

    fn fleet_braked(&self) -> bool {
        self.arm
            .last_eval
            .as_ref()
            .and_then(|e| e["sides"].as_array())
            .map(|sides| sides.iter().any(|s| s["brake"] == "fleet"))
            .unwrap_or(false)
    }
}

/// Every arm's records merged into one global timestamp order — the whole
/// difference between a fleet run and a stack of independent window runs.
///
/// Sorted by `(t, arm index)`, never by t alone: records that share a
/// timestamp have to break their tie the same way on every run, or the
/// pool is spent in a different order and the A/B stops being repeatable
/// (L33's complaint about non-reproducible studies, one layer down).
fn interleave<'a>(recs_by_arm: &[&'a Vec<Value>]) -> Vec<(f64, usize, &'a Value)> {
    let mut ordered: Vec<(f64, usize, &Value)> = Vec::new();
    for (i, recs) in recs_by_arm.iter().enumerate() {
        for rec in recs.iter() {
            ordered.push((rec["t"].as_f64().unwrap_or(0.0), i, rec));
        }
    }
    ordered.sort_by(|a, b| {
        a.0.partial_cmp(&b.0).unwrap_or(std::cmp::Ordering::Equal).then(a.1.cmp(&b.1))
    });
    ordered
}

fn run_fleet(opts: &ReplayOpts, cap: f64, full: bool) -> Result<(), String> {
    let tape_path = opts.tape.clone().unwrap_or_else(default_eval_tape_path);
    let source = if full {
        opts.book_tape.clone().unwrap_or_else(default_book_tape_path)
    } else {
        tape_path.clone()
    };
    let records = load_jsonl(&source)?;
    let windows = group_by_slug(&records, &opts.slug);
    if windows.is_empty() {
        return Err(format!("no records for slug '{}' in {}", opts.slug, source.display()));
    }
    let params_map = load_params_map(opts.params.as_deref())?;
    if full && params_map.is_empty() {
        return Err("--params is required for --mode full".to_string());
    }
    let outcomes = load_outcomes(opts.outcomes.as_deref())?;
    let real_by_slug = load_real_tally(&tape_path);

    // One arm per matched window, with its params resolved up front so the
    // shared feed sources below can be sized off the real fleet.
    let mut pending: Vec<(&String, &Vec<Value>, ArmParams, Option<Tunables>)> = Vec::new();
    for (slug, recs) in &windows {
        let (p, tun) = match params_map.get(slug).cloned() {
            Some(v) => v,
            None if full => {
                eprintln!("[replay] skipping '{}': no --params entry for this slug", slug);
                continue;
            }
            None => {
                eprintln!(
                    "[replay] warning: no --params entry for '{}' — synthesizing minimal params",
                    slug
                );
                synth_params(slug)?
            }
        };
        pending.push((slug, recs, p, tun));
    }

    // Full mode needs its feeds before the interleave starts, and the
    // Binance side wants each symbol's cache read ONCE over the union of
    // its windows: the cache file is megabytes and re-parsing it a few
    // hundred times is the whole runtime otherwise. The corpus is read once
    // too, and only if some arm on it is stream-fed.
    let mut klines: HashMap<String, Arc<BTreeMap<i64, Kline>>> = HashMap::new();
    let mut corpus: Option<RtdsCorpus> = None;
    if full {
        corpus = load_rtds_corpus(opts, &rtds_symbols_needed(pending.iter().map(|e| &e.2)))?;
        let mut spans: HashMap<String, (i64, i64)> = HashMap::new();
        for (_, _, p, _) in pending.iter().filter(|e| e.2.feed != FEED_RTDS) {
            let e = spans.entry(p.symbol.clone()).or_insert((i64::MAX, i64::MIN));
            e.0 = e.0.min(p.start as i64);
            e.1 = e.1.max(p.end as i64);
        }
        for (symbol, (lo, hi)) in spans {
            let mut rows = load_kline_cache(&kline_cache_path(&symbol)?);
            ensure_klines(&symbol, lo - KLINE_LOOKBACK_S, hi, &mut rows)?;
            klines.insert(symbol, Arc::new(rows));
        }
    }

    // Drop any window we cannot run BEFORE the interleave — a half-built
    // fleet would misprice the pool every arm is rationed against.
    let mut arms: Vec<FleetArm> = Vec::new();
    let mut recs_by_arm: Vec<&Vec<Value>> = Vec::new();
    for (slug, recs, p, tun) in pending {
        let feed_src = if !full {
            None
        } else {
            let built = if p.feed == FEED_RTDS {
                full_feed_for(&p, corpus.as_ref())
            } else {
                klines
                    .get(&p.symbol)
                    .cloned()
                    .map(FullFeed::Klines)
                    .ok_or_else(|| format!("{}: no kline cache for {}", p.slug, p.symbol))
            };
            match built {
                Ok(f) => Some(f),
                Err(e) => {
                    eprintln!("[replay] skipping '{}': {}", slug, e);
                    continue;
                }
            }
        };
        let mut arm = ArmState::with_params(p.clone());
        arm.subscribed = true;
        if let Some(t) = tun {
            arm.tunables = t;
        }
        arms.push(FleetArm {
            p,
            arm,
            sim: FillSim::default(),
            last_p_up: None,
            real: real_by_slug.get(slug).cloned().unwrap_or_default(),
            fleet_blocks: 0,
            feed_src,
        });
        recs_by_arm.push(recs);
    }
    if arms.is_empty() {
        return Err(format!("no window could be replayed for '{}'", opts.slug));
    }

    let ordered = interleave(&recs_by_arm);
    let mut room_low = f64::INFINITY;
    let mut traced: Vec<Value> = Vec::new();
    for (now, i, rec) in ordered {
        let mut fleet_room = if cap > 0.0 {
            let undecided: f64 = arms.iter().map(|a| a.undecided(now)).sum();
            (cap - undecided).max(0.0)
        } else {
            f64::INFINITY
        };
        room_low = room_low.min(fleet_room);

        let a = &mut arms[i];
        // The book this tick decided on, kept for the fill sim: a resting
        // post-only ask is lifted by the SAME sample decide() just read.
        let mut tick_view = ArmView::default();
        let out = if full {
            let spot = rec["spot"].as_f64().unwrap_or(0.0);
            let spot_age = rec["spot_age_s"].as_f64().unwrap_or(0.0);
            let start = a.p.start as i64;
            // Copied out before the feed's &mut borrow so the model read and
            // the decide() that consumes it run on ONE tunables value.
            let tun = a.arm.tunables;
            let Some(src) = a.feed_src.as_mut() else { continue };
            let feed = src.state_at(start, spot, now - spot_age, now);
            let model = eval_model(&a.p, &feed, now, a.p.basis_guard_bp, &tun);
            let view = view_from_book_record(rec, &a.sim, &a.p);
            tick_view = view;
            Some(a.arm.decide_fleet(&view, model, now, &mut fleet_room))
        } else {
            match rec.get("ev").and_then(|v| v.as_str()).unwrap_or("") {
                EV_EVAL => {
                    let p_up = rec["p_up"].as_f64().unwrap_or(0.5);
                    a.last_p_up = Some(p_up);
                    let model = model_from_eval_record(rec, p_up, &a.p, &a.arm.tunables);
                    let view = view_from_sides(&rec["sides"], &a.sim);
                    tick_view = view;
                    Some(a.arm.decide_fleet(&view, Ok(model), now, &mut fleet_room))
                }
                EV_GATED => Some(a.arm.decide_fleet(
                    &ArmView::default(),
                    Err(gate_from_record(rec)),
                    now,
                    &mut fleet_room,
                )),
                _ => None,
            }
        };
        let Some(out) = out else { continue };
        if opts.trace.is_some() {
            traced.extend(out.tape.iter().cloned());
        }
        let fee_rate = a.p.fee_rate;
        apply_fills(&mut a.arm, &mut a.sim, &out, now, fee_rate, &tick_view);
        if a.fleet_braked() {
            a.fleet_blocks += 1;
        }
    }

    write_trace(opts.trace.as_deref(), &traced)?;
    let mode = if full { "full" } else { "evals" };
    let mut reports = Vec::new();
    for a in &mut arms {
        let (start, end) = (a.p.start as i64, a.p.end as i64);
        let outcome = outcomes.get(&a.p.slug).cloned().or_else(|| match &mut a.feed_src {
            // Full mode settles off the arm's own feed — kline mids for a
            // Binance arm, the settlement TWAP prints for a stream-fed one.
            Some(src) => src.settle(start, end),
            None => a.last_p_up.map(|p| if p >= 0.5 { "up".into() } else { "down".into() }),
        });
        let pnl = outcome.as_deref().map(|w| settle_pnl(&a.sim, &a.p, w));
        let mut r = build_report(&a.p.slug, mode, &a.sim, &a.real, outcome, pnl);
        r["fleet"] = serde_json::json!({"cap": cap, "blocks": a.fleet_blocks});
        reports.push(r);
    }
    let windows_hit = reports.iter().filter(|r| r["fleet"]["blocks"].as_i64() != Some(0)).count();
    // `peak un-decided` is the headline diagnostic even on a run that never
    // blocks: it says how close the fleet ever came to the cap, which is
    // what decides whether a cap is a constraint or a decoration.
    let peak = if room_low.is_finite() {
        format!("${:.0}", cap - room_low)
    } else {
        "n/a (uncapped)".to_string()
    };
    eprintln!(
        "[replay] fleet cap ${:.0} — {} window(s), {} block(s) across {} window(s), \
         peak un-decided {}",
        cap,
        reports.len(),
        reports.iter().filter_map(|r| r["fleet"]["blocks"].as_i64()).sum::<i64>(),
        windows_hit,
        peak,
    );
    finalize(&opts.slug, mode, &reports, opts.out.as_deref())
}

#[cfg(test)]
mod tests {
    use super::*;

    fn test_params(slug: &str) -> ArmParams {
        serde_json::from_value(serde_json::json!({
            "slug": slug, "kind": "twap", "symbol": "BTCUSDT",
            "token_up": format!("{}-u", slug), "token_down": format!("{}-d", slug),
            "start": 600.0, "end": 1500.0,
            "sigma_bp_per_min": 3.0, "fee_rate": 0.07, "size_usdc": 100.0,
        }))
        .unwrap()
    }

    #[test]
    fn evals_record_reproduces_one_fire() {
        // Mirrors updown.rs's decide_fires_a_clip_when_all_gates_hold
        // fixture: rem=100s <= late_rem 120 -> unlocked, banked_decided,
        // ask cheap enough to clear min_edge. Older-shape record: no
        // margin_bp/banked_bp/cushion_bp fields, must default to 0.0.
        let p = test_params("s");
        let rec = serde_json::json!({
            "t": 1400.0, "ev": "eval", "slug": "s",
            "p_up": 1.0, "sig_bp": 3.0, "rho": 0.0, "banked_decided": true,
            "sides": [
                {"side": "up", "fair": 1.0, "ask": 0.94, "net": 0.05},
                {"side": "down", "fair": 0.0, "ask": 0.05, "net": -0.05},
            ],
        });
        let report = replay_evals_window(&p, None, &[rec], None, &RealTally::default());
        assert_eq!(report["sim"]["fires"], 1);
        assert!((report["sim"]["notional"].as_f64().unwrap() - 26.0 * 0.94).abs() < 1e-9);
        assert_eq!(report["sim"]["outcome"], "up");
    }

    // ---------- the post-only SELL convention ----------------------------

    /// Drive the fill sim directly with hand-built actions and books. The
    /// decision core never emits a post-only sell today (the incumbent's only
    /// sell is L1's crossing evacuation), so this is the seam that proves the
    /// SIM half of the capability on its own.
    fn sim_with(shares: f64, basis: f64, p: &ArmParams) -> (ArmState, FillSim) {
        let mut arm = ArmState::with_params(p.clone());
        arm.subscribed = true;
        let mut sim = FillSim::default();
        sim.shares.insert(p.token_up.clone(), shares);
        sim.cost_basis.insert(p.token_up.clone(), basis);
        sim.cost = basis;
        (arm, sim)
    }

    fn up_book(bid: Option<f64>) -> ArmView {
        ArmView {
            up: TopOfBook { bid: bid.map(|b| (b, 500.0)), ask: None },
            ..ArmView::default()
        }
    }

    fn actions(a: Vec<Action>) -> DecideOut {
        DecideOut { actions: a, ..DecideOut::default() }
    }

    /// The convention, verbatim from `analysis/bracket_exit.md` §0: an ask at
    /// `r` is lifted iff a sampled book instant shows our side's best BID at
    /// or above `r`. A bid one tick short is NOT a fill, however close, and
    /// the placing tick's own book can never lift it — a post-only order that
    /// would match at placement is rejected, not filled.
    #[test]
    fn a_resting_ask_fills_only_when_the_bid_comes_through_it() {
        let p = test_params("s");
        let (mut arm, mut sim) = sim_with(50.0, 30.0, &p);
        let rest = actions(vec![Action::Sell {
            token: p.token_up.clone(),
            price: 0.70,
            size: 50.0,
            post_only: true,
        }]);

        // Placed against a book already bid at 0.70. It does not fill on the
        // tick it goes out on, at any price.
        apply_fills(&mut arm, &mut sim, &rest, 1400.0, p.fee_rate, &up_book(Some(0.70)));
        assert_eq!(sim.proceeds, 0.0, "a rest is not a fill");
        assert_eq!(sim.shares[&p.token_up], 50.0);

        // A LATER sample one tick short: still no fill. The whole book has to
        // come to us — "a trade printed at r" is not the test.
        apply_fills(&mut arm, &mut sim, &actions(vec![]), 1405.0, p.fee_rate, &up_book(Some(0.69)));
        assert_eq!(sim.proceeds, 0.0, "0.69 does not lift a 0.70 ask");
        assert_eq!(sim.shares[&p.token_up], 50.0);

        // …and no bid at all is no fill, not a fill at the last price seen.
        apply_fills(&mut arm, &mut sim, &actions(vec![]), 1410.0, p.fee_rate, &up_book(None));
        assert_eq!(sim.proceeds, 0.0);

        // The bid crosses it: lifted, at the ASK's price, not the bid's.
        apply_fills(&mut arm, &mut sim, &actions(vec![]), 1415.0, p.fee_rate, &up_book(Some(0.72)));
        assert!((sim.proceeds - 35.0).abs() < 1e-9, "50 shares at the resting 0.70");
        assert_eq!(sim.shares[&p.token_up], 0.0);
        // And it is gone — a lifted ask cannot be lifted twice.
        apply_fills(&mut arm, &mut sim, &actions(vec![]), 1420.0, p.fee_rate, &up_book(Some(0.99)));
        assert!((sim.proceeds - 35.0).abs() < 1e-9);
    }

    /// A lifted resting ask is the MAKER side and pays nothing — 526 of 526
    /// wallet rows. Running it through the taker curve is how the ledger
    /// drifts off the scoreboard (`bracket_exit.md` §9.4).
    #[test]
    fn a_lifted_ask_pays_the_maker_fee_and_a_crossing_sell_does_not() {
        let p = test_params("s");
        let (mut arm, mut sim) = sim_with(50.0, 30.0, &p);
        let rest = actions(vec![Action::Sell {
            token: p.token_up.clone(),
            price: 0.50,
            size: 50.0,
            post_only: true,
        }]);
        apply_fills(&mut arm, &mut sim, &rest, 1400.0, p.fee_rate, &up_book(None));
        apply_fills(&mut arm, &mut sim, &actions(vec![]), 1405.0, p.fee_rate, &up_book(Some(0.50)));
        assert!((sim.proceeds - 25.0).abs() < 1e-9, "50 x 0.50, no fee taken out");
        assert_eq!(sim.fees, 0.0, "a maker sell is charged exactly nothing");

        // The same size and price crossing, for contrast: 0.50 is the widest
        // point of the taker curve, 1.75 cents a share.
        let (mut arm, mut sim) = sim_with(50.0, 30.0, &p);
        let cross = actions(vec![Action::Sell {
            token: p.token_up.clone(),
            price: 0.50,
            size: 50.0,
            post_only: false,
        }]);
        apply_fills(&mut arm, &mut sim, &cross, 1400.0, p.fee_rate, &up_book(Some(0.50)));
        let fee = 50.0 * taker_fee(0.50, p.fee_rate);
        assert!(fee > 0.87, "the taker curve at mid: {fee}");
        assert!((sim.proceeds - (25.0 - fee)).abs() < 1e-9);
    }

    /// A cancel is what takes a resting ask off the book — at quiesce, at
    /// window close, or on the evacuation's own cancel/sell pair. Without it
    /// the sim would keep lifting an order the strategy had already pulled.
    #[test]
    fn a_cancel_takes_a_resting_ask_off_the_simulated_book() {
        let p = test_params("s");
        let (mut arm, mut sim) = sim_with(50.0, 30.0, &p);
        let rest_then_cancel = actions(vec![
            Action::Sell {
                token: p.token_up.clone(),
                price: 0.70,
                size: 50.0,
                post_only: true,
            },
            Action::Cancel(p.token_up.clone()),
        ]);
        apply_fills(&mut arm, &mut sim, &rest_then_cancel, 1400.0, p.fee_rate, &up_book(None));
        apply_fills(&mut arm, &mut sim, &actions(vec![]), 1405.0, p.fee_rate, &up_book(Some(0.99)));
        assert_eq!(sim.proceeds, 0.0, "a cancelled ask is not on the book to lift");
        assert_eq!(sim.shares[&p.token_up], 50.0);
    }

    /// An ask cannot sell inventory that is no longer there. The evacuation
    /// takes the shares AND cancels; if the two ever arrive out of order the
    /// sim must not book the same shares twice.
    #[test]
    fn a_resting_ask_over_empty_inventory_is_dropped_not_filled() {
        let p = test_params("s");
        let (mut arm, mut sim) = sim_with(50.0, 30.0, &p);
        let both = actions(vec![
            Action::Sell {
                token: p.token_up.clone(),
                price: 0.70,
                size: 50.0,
                post_only: true,
            },
        ]);
        apply_fills(&mut arm, &mut sim, &both, 1400.0, p.fee_rate, &up_book(None));
        // Everything sold out from under it by a crossing exit.
        let evac = actions(vec![Action::Sell {
            token: p.token_up.clone(),
            price: 0.30,
            size: 50.0,
            post_only: false,
        }]);
        apply_fills(&mut arm, &mut sim, &evac, 1405.0, p.fee_rate, &up_book(Some(0.30)));
        let after_evac = sim.proceeds;
        apply_fills(&mut arm, &mut sim, &actions(vec![]), 1410.0, p.fee_rate, &up_book(Some(0.99)));
        assert!((sim.proceeds - after_evac).abs() < 1e-12, "no second sale of the same shares");
        assert_eq!(sim.shares[&p.token_up], 0.0);
    }

    /// The mirror the whole convention rests on: a post-only BUY never fills
    /// in the sim at all, and a post-only SELL never fills on the tick it is
    /// placed. Both under-count, which is the safe direction for a strategy
    /// whose edge depends on not overstating how easily a thin book fills.
    #[test]
    fn post_only_buys_and_sells_are_the_same_conservative_convention() {
        let p = test_params("s");
        let (mut arm, mut sim) = sim_with(0.0, 0.0, &p);
        let maker_bid = actions(vec![Action::Buy {
            token: p.token_up.clone(),
            price: 0.98,
            size: 25.0,
            post_only: true,
        }]);
        for t in [1400.0, 1405.0, 1410.0] {
            apply_fills(&mut arm, &mut sim, &maker_bid, t, p.fee_rate, &up_book(Some(0.99)));
        }
        assert_eq!(sim.fire_count, 0, "a post-only bid never fills here, ever");
        assert_eq!(sim.cost, 0.0);
    }

    #[test]
    fn evals_gated_record_fires_nothing() {
        let p = test_params("s");
        let rec = serde_json::json!({
            "t": 1400.0, "ev": "gated", "slug": "s", "reason": "feed stale",
        });
        let report = replay_evals_window(&p, None, &[rec], None, &RealTally::default());
        assert_eq!(report["sim"]["fires"], 0);
    }

    #[test]
    fn gate_from_record_prefers_fields_and_tolerates_old_lines() {
        let modern = serde_json::json!({
            "ev": "gated", "reason": "basis guard: projected margin -4.9bp inside 6.0bp noise band",
            "margin_bp": -4.9, "banked_bp": 1.0, "cushion_bp": 9.0, "guard_bp": 6.0,
        });
        let g = gate_from_record(&modern);
        assert_eq!(g.margin_bp, Some(-4.9));
        assert_eq!(g.guard_bp, Some(6.0));
        assert_eq!(g.spot_age_s, None, "a basis gate has no feed age");
        // A stale line from the current generation round-trips its age.
        let stale = serde_json::json!({
            "ev": "gated", "reason": "feed stale: rtds sample lag 12.3s", "spot_age_s": 12.3,
        });
        assert_eq!(
            gate_from_record(&stale),
            GateReason::stale("feed stale: rtds sample lag 12.3s", 12.3)
        );
        // Pre-structured line: reason survives, numbers are simply absent —
        // no regex, no guessing, including on the age the sentence carries.
        let old = serde_json::json!({"ev": "gated", "reason": "feed stale: rtds sample lag 12.3s"});
        assert_eq!(gate_from_record(&old), GateReason::plain("feed stale: rtds sample lag 12.3s"));
    }

    #[test]
    fn evals_tunables_override_reproduces_pre_brake_policy() {
        // Same signature as updown.rs's replay-tunables test: an
        // undecided moonshot the live brake blocks, the old (infinite
        // threshold) policy fires.
        let p = test_params("s");
        let rec = serde_json::json!({
            "t": 1400.0, "ev": "eval", "slug": "s",
            "p_up": 0.99, "sig_bp": 3.0, "rho": 0.0, "banked_decided": false,
            "sides": [
                {"side": "up", "fair": 0.99, "ask": 0.50, "net": 0.45},
                {"side": "down", "fair": 0.01, "ask": 0.92, "net": -0.98},
            ],
        });
        let blocked =
            replay_evals_window(&p, None, std::slice::from_ref(&rec), None, &RealTally::default());
        assert_eq!(blocked["sim"]["fires"], 0, "distrust brake holds under default tunables");

        let old_policy = Tunables {
            distrust_net: f64::INFINITY,
            avg_down_tol: f64::INFINITY,
            ..Tunables::default()
        };
        let fired = replay_evals_window(&p, Some(old_policy), &[rec], None, &RealTally::default());
        assert_eq!(fired["sim"]["fires"], 1, "lifted tunables reproduce the old policy's fire");
    }

    #[test]
    fn lookahead_cutoff_excludes_the_forming_minute_true_close() {
        // now sits 20s into minute 660 (start=600): minute 600 is fully
        // closed, 660 is still forming. The forming bucket must use
        // (open+spot)/2, never its true close, even though the cache has it.
        let mut rows = BTreeMap::new();
        rows.insert(600, Kline { t: 600, o: 100.0, c: 100.5 });
        rows.insert(660, Kline { t: 660, o: 100.5, c: 101.0 }); // future — true close must not leak
        let now = 680.0;
        let spot = 100.6;
        let feed = feed_state_at(&rows, 600, spot, now, now);

        assert_eq!(feed.per_min.get(&600), Some(&100.25), "closed minute uses (o+c)/2");
        assert_eq!(
            feed.per_min.get(&660),
            Some(&((100.5 + spot) / 2.0)),
            "forming minute uses (open+spot)/2, not the true close"
        );
        // closes: closed-minute true closes, then spot as the forming proxy.
        assert_eq!(feed.closes, vec![100.5, spot]);
    }

    #[test]
    fn lookahead_cutoff_never_sees_a_minute_that_has_not_started() {
        let mut rows = BTreeMap::new();
        rows.insert(600, Kline { t: 600, o: 100.0, c: 100.5 });
        rows.insert(660, Kline { t: 660, o: 100.5, c: 101.0 }); // hasn't started at now=650
        let now = 650.0; // still inside minute 600
        let feed = feed_state_at(&rows, 600, 100.3, now, now);
        assert!(!feed.per_min.contains_key(&660), "minute 660 hasn't opened yet at t=650");
        assert_eq!(feed.per_min.get(&600), Some(&((100.0 + 100.3) / 2.0)), "600 is the forming bar");
    }

    #[test]
    fn settle_winner_picks_the_higher_side() {
        let mut rows = BTreeMap::new();
        rows.insert(540, Kline { t: 540, o: 100.0, c: 100.0 }); // ref (start-60)
        rows.insert(600, Kline { t: 600, o: 100.0, c: 101.0 });
        rows.insert(660, Kline { t: 660, o: 101.0, c: 102.0 });
        assert_eq!(settle_winner(&rows, 600, 720).as_deref(), Some("up"));

        let mut down_rows = BTreeMap::new();
        down_rows.insert(540, Kline { t: 540, o: 100.0, c: 100.0 });
        down_rows.insert(600, Kline { t: 600, o: 100.0, c: 99.0 });
        assert_eq!(settle_winner(&down_rows, 600, 660).as_deref(), Some("down"));

        assert_eq!(settle_winner(&BTreeMap::new(), 600, 660), None, "no ref, no verdict");
    }

    #[test]
    fn kline_cache_round_trips_and_dedupes() {
        let path = std::env::temp_dir().join(format!(
            "pmengine-replay-test-kline-cache-{}.jsonl",
            std::process::id()
        ));
        let _ = std::fs::remove_file(&path);

        let rows = vec![
            Kline { t: 600, o: 100.0, c: 100.5 },
            Kline { t: 660, o: 100.5, c: 101.0 },
        ];
        append_kline_rows(&path, &rows).unwrap();
        // Re-appending the same t=600 row (simulating an overlapping
        // fetch range) must not duplicate it once loaded into a map.
        append_kline_rows(&path, &[Kline { t: 600, o: 100.0, c: 100.5 }]).unwrap();

        let loaded = load_kline_cache(&path);
        assert_eq!(loaded.len(), 2, "dedupes by t on load despite the duplicate line");
        assert_eq!(loaded[&660].c, 101.0);
        assert!(cache_covers(&loaded, 600, 720));
        assert!(!cache_covers(&loaded, 600, 780), "gap past the cached range");

        let _ = std::fs::remove_file(&path);
    }

    #[test]
    fn synth_params_parses_slug_shape() {
        let (p, tun) = synth_params("btc-updown-15m-1787446800").unwrap();
        assert_eq!(p.symbol, "BTCUSDT");
        assert_eq!(p.kind, "twap");
        assert_eq!(p.start, 1787446800.0);
        assert_eq!(p.end, 1787446800.0 + 900.0);
        assert!(tun.is_none());
        assert!(synth_params("not-a-recognized-slug").is_err());
    }

    // --- mixed-cadence book tapes ---------------------------------------
    //
    // The recorder's coarse leg moved 5s -> 2s (book_sample_due), so every
    // live tape from the change onward is one file with 5s rows followed by
    // 2s rows, and every committed fixture still carries a pure-5s slice.
    // Nothing here may notice: the readers are row-driven, and the ONLY
    // clock they have is each row's own `t`.

    /// A book slice at an explicit list of offsets from `start` — the caller
    /// supplies the cadence, which is the whole point.
    fn book_rows_at(slug: &str, start: f64, offsets: &[f64], ask: f64) -> Vec<Value> {
        offsets
            .iter()
            .map(|off| {
                let t = start + off;
                serde_json::json!({
                    "t": t, "ev": "book", "slug": slug,
                    "up_bid": ask - 0.02, "up_bid_sz": 500.0,
                    "up_ask": ask, "up_ask_sz": 500.0,
                    "dn_bid": 1.0 - ask - 0.02, "dn_bid_sz": 500.0,
                    "dn_ask": 1.0 - ask + 0.02, "dn_ask_sz": 500.0,
                    "spot": 101.0, "spot_age_s": 0.1,
                })
            })
            .collect()
    }

    /// 5s for the first `switch` seconds, 2s after it — a tape that spans
    /// the recorder change.
    fn mixed_offsets(span: f64, switch: f64) -> Vec<f64> {
        let mut v = Vec::new();
        let mut t = 0.0;
        while t < switch {
            v.push(t);
            t += 5.0;
        }
        while t <= span {
            v.push(t);
            t += 2.0;
        }
        v
    }

    #[test]
    fn mixed_cadence_book_tape_loads_row_for_row_in_time_order() {
        let dir = scratch("mixed-cadence");
        let slug = "btc-updown-15m-600";
        let offsets = mixed_offsets(600.0, 300.0);
        let rows = book_rows_at(slug, 600.0, &offsets, 0.94);

        // The realistic file shape is append-only: the old 5s rows, then the
        // new 2s rows. The pathological one is a concatenation that got them
        // backwards (two archives glued together). Both must group the same.
        let (old_rows, new_rows) = rows.split_at(offsets.iter().filter(|o| **o < 300.0).count());
        let appended = dir.join("appended.jsonl");
        let backwards = dir.join("backwards.jsonl");
        write_jsonl(&appended, &rows).unwrap();
        let mut swapped = new_rows.to_vec();
        swapped.extend_from_slice(old_rows);
        write_jsonl(&backwards, &swapped).unwrap();

        let group = |p: &Path| {
            let recs = load_jsonl(p).unwrap();
            let mut g = group_by_slug(&recs, slug);
            assert_eq!(g.len(), 1);
            g.pop().unwrap().1
        };
        let a = group(&appended);
        let b = group(&backwards);

        assert_eq!(a.len(), rows.len(), "every row survives — no dedup, no gap-filling");
        assert_eq!(a, b, "file order is not information; `t` is");

        // The mixed gap structure comes back intact. A reader that had
        // baked in a fixed step would have to normalize this away.
        let gaps: Vec<f64> = a
            .windows(2)
            .map(|w| w[1]["t"].as_f64().unwrap() - w[0]["t"].as_f64().unwrap())
            .collect();
        assert!(gaps.iter().take(59).all(|g| (*g - 5.0).abs() < 1e-9), "5s prefix preserved");
        assert!(gaps.iter().skip(60).all(|g| (*g - 2.0).abs() < 1e-9), "2s suffix preserved");
        assert!(gaps.windows(2).any(|w| w[0] != w[1]), "the tape really does change cadence");

        let _ = std::fs::remove_dir_all(&dir);
    }

    /// Permissive params: the gate family is exercised everywhere else, and
    /// this test is about the tape grid, so let the arm actually trade.
    fn open_arm(slug: &str, start: f64, end: f64) -> ArmParams {
        serde_json::from_value(serde_json::json!({
            "slug": slug, "kind": "twap", "symbol": "BTCUSDT",
            "token_up": format!("{}-up", slug), "token_down": format!("{}-down", slug),
            "start": start, "end": end,
            "sigma_bp_per_min": 3.0, "fee_rate": 0.0, "size_usdc": 10_000.0,
            "min_edge": 0.0, "early_min_edge": 0.0, "min_fair": 0.0,
            "min_elapsed_frac": 0.0, "basis_guard_bp": 0.0, "theta": 0.0,
            "clip_usdc": 20.0, "early_frac": 1.0, "p_cap": 1.0, "rho_block": -1.0,
        }))
        .unwrap()
    }

    /// A decisively-up kline series, so the model prices `up` and the arm
    /// has a reason to keep buying it.
    fn rising_klines(start: i64, end: i64) -> Arc<BTreeMap<i64, Kline>> {
        let mut rows = BTreeMap::new();
        let mut px = 100.0;
        let mut t = start - 60;
        while t <= end + 60 {
            rows.insert(t, Kline { t, o: px, c: px + 0.5 });
            px += 0.5;
            t += 60;
        }
        Arc::new(rows)
    }

    #[test]
    fn a_mixed_cadence_tape_replays_and_only_ever_acts_on_recorded_instants() {
        let (slug, start, end) = ("btc-updown-15m-600", 600.0, 1500.0);
        let p = open_arm(slug, start, end);
        let offsets = mixed_offsets(880.0, 300.0);
        let rows = book_rows_at(slug, start, &offsets, 0.60);
        let stamps: Vec<f64> = rows.iter().map(|r| r["t"].as_f64().unwrap()).collect();

        let mut feed = FullFeed::Klines(rising_klines(start as i64, end as i64));
        let (report, trace) = replay_full_window_with(
            &mut feed,
            &p,
            None,
            &rows,
            Some("up".to_string()),
            &RealTally::default(),
        );

        // Guard against the test rotting into a vacuous one: it only proves
        // something if the arm actually did something across BOTH cadences.
        assert!(!trace.is_empty(), "the arm emitted no tape at all — nothing is being proved");
        let fires = report["sim"]["fires"].as_i64().unwrap();
        assert!(fires > 0, "expected the permissive arm to trade; got {fires} fires");

        // The cadence-agnosticism claim, stated so it can fail: replay has
        // no clock of its own, so every instant it acts at must be a row it
        // was handed. A reader that stepped a fixed interval, interpolated
        // between samples, or resampled a mixed file onto one grid would
        // land between these stamps.
        for rec in &trace {
            let t = rec["t"].as_f64().unwrap();
            assert!(
                stamps.iter().any(|s| (s - t).abs() < 1e-9),
                "record at t={t} is not a recorded book instant — replay invented a tick"
            );
        }

        // And it really did work across the seam, not just in the 5s half.
        let seam = start + 300.0;
        assert!(trace.iter().any(|r| r["t"].as_f64().unwrap() < seam), "acted in the 5s prefix");
        assert!(trace.iter().any(|r| r["t"].as_f64().unwrap() > seam), "acted in the 2s suffix");
    }

    #[test]
    fn the_tape_grid_bounds_replay_and_2s_is_the_first_that_can_express_a_clip_cooldown() {
        // L46's residue, end to end. Replay can only act on a recorded row,
        // so the tape grid is a FLOOR under every timing gate the arm has —
        // and a 5s grid sits above the finest one, `clip_cooldown_s` (2.0):
        // an arm free to re-fire at +2s replays as re-firing at +5s, and
        // there is no replay-side change that can recover the difference.
        // A 2s grid is the coarsest that can express it.
        //
        // The gate this ultimately has to resolve is the 12s INFLIGHT_TTL_S
        // lock — live's real firing metronome, same-side p50 12.07s. It is
        // not the binding gate HERE, because `apply_fills` still deletes
        // the lock on fill (the branch-`sim-fidelity` repair L46 describes
        // is not on master yet), which is why this test measures the
        // cooldown instead. The 2s grid answers both for the same reason:
        // 2 divides both 12 and 2, where 5 divides neither. The sampler-side
        // arithmetic for the 12s case is pinned in updown_model.rs's
        // `book_grid_resolves_the_12s_inflight_lock_without_rounding_up`.
        let (slug, start, end) = ("btc-updown-15m-600", 600.0, 1500.0);
        let p = open_arm(slug, start, end);
        let span = 600.0;

        let same_side_gaps = |step: f64| -> Vec<f64> {
            let offsets: Vec<f64> =
                std::iter::successors(Some(0.0), |t| Some(t + step)).take_while(|t| *t <= span).collect();
            let rows = book_rows_at(slug, start, &offsets, 0.60);
            let mut feed = FullFeed::Klines(rising_klines(start as i64, end as i64));
            let (_, trace) = replay_full_window_with(
                &mut feed,
                &p,
                None,
                &rows,
                Some("up".to_string()),
                &RealTally::default(),
            );
            let fires: Vec<f64> = trace
                .iter()
                .filter(|r| r["ev"] == "fire" && r["side"] == "up")
                .filter_map(|r| r["t"].as_f64())
                .collect();
            fires.windows(2).map(|w| w[1] - w[0]).collect()
        };

        let five = same_side_gaps(5.0);
        let two = same_side_gaps(2.0);
        assert!(five.len() >= 3 && two.len() >= 3, "need several re-fires to compare");

        let median = |mut v: Vec<f64>| {
            v.sort_by(|a, b| a.partial_cmp(b).unwrap());
            v[v.len() / 2]
        };
        let (m5, m2) = (median(five), median(two));
        assert!(
            (m5 - 5.0).abs() < 1e-6,
            "on a 5s grid the re-fire gap should be the GRID, not the arm's 2s cooldown; got {m5}s"
        );
        assert!(
            (m2 - p.clip_cooldown_s).abs() < 1e-6,
            "a 2s grid should resolve clip_cooldown_s ({}s) exactly; got {m2}s",
            p.clip_cooldown_s
        );
        assert!(m5 > m2, "the coarse grid can only ever under-fire relative to the fine one");
    }

    // --- R7 fleet mode ------------------------------------------------

    /// Scratch dir for the file-driven fleet tests. Named per test so two
    /// running in parallel never share a tape.
    fn scratch(name: &str) -> PathBuf {
        let dir = std::env::temp_dir()
            .join(format!("pmengine-replay-fleet-{}-{}", name, std::process::id()));
        let _ = std::fs::remove_dir_all(&dir);
        std::fs::create_dir_all(&dir).unwrap();
        dir
    }

    /// Two 15m windows on the same clock, their eval ticks alternating.
    fn two_arm_tape(dir: &Path) -> (PathBuf, PathBuf) {
        let side_pair = |ask: f64| {
            serde_json::json!([
                {"side": "up", "fair": 1.0, "ask": ask, "net": 1.0 - ask},
                {"side": "down", "fair": 0.0, "ask": 1.0 - ask, "net": -1.0},
            ])
        };
        let ev = |slug: &str, t: f64| {
            serde_json::json!({
                "t": t, "ev": "eval", "slug": slug, "p_up": 1.0, "sig_bp": 3.0,
                "rho": 0.0, "banked_decided": false, "sides": side_pair(0.94),
            })
        };
        // btc first at each timestamp; eth 0.1s behind it.
        let tape_path = dir.join("tape.jsonl");
        write_jsonl(
            &tape_path,
            &[
                ev("btc-updown-15m-600", 1400.0),
                ev("eth-updown-15m-600", 1400.1),
                ev("btc-updown-15m-600", 1410.0),
                ev("eth-updown-15m-600", 1410.1),
            ],
        )
        .unwrap();

        let entry = |slug: &str, sym: &str| {
            serde_json::json!({
                "slug": slug, "kind": "twap", "symbol": sym,
                "token_up": format!("{}-u", slug), "token_down": format!("{}-d", slug),
                "start": 600.0, "end": 1500.0,
                "sigma_bp_per_min": 3.0, "fee_rate": 0.07, "size_usdc": 100.0,
            })
        };
        let params_path = dir.join("params.json");
        std::fs::write(
            &params_path,
            serde_json::json!([
                entry("btc-updown-15m-600", "BTCUSDT"),
                entry("eth-updown-15m-600", "ETHUSDT"),
            ])
            .to_string(),
        )
        .unwrap();
        (tape_path, params_path)
    }

    fn fleet_opts(tape: &Path, params: &Path, out: &Path, cap: f64) -> ReplayOpts {
        ReplayOpts {
            mode: "evals".to_string(),
            tape: Some(tape.to_path_buf()),
            book_tape: None,
            slug: "".to_string(), // every slug is a prefix-match on ""
            params: Some(params.to_path_buf()),
            outcomes: None,
            out: Some(out.to_path_buf()),
            trace: None,
            fleet_cap: Some(cap),
            rtds_corpus: None,
        }
    }

    /// Per-window rows from a fleet run's `--out` file (the trailing
    /// aggregate row is dropped).
    fn fleet_rows(out: &Path) -> Vec<Value> {
        let mut rows = load_jsonl(out).unwrap();
        rows.pop();
        rows
    }

    #[test]
    fn interleave_orders_by_time_then_arm() {
        let a: Vec<Value> = vec![serde_json::json!({"t": 2.0}), serde_json::json!({"t": 1.0})];
        let b: Vec<Value> = vec![serde_json::json!({"t": 1.0}), serde_json::json!({"t": 3.0})];
        let ordered = interleave(&[&a, &b]);
        let keys: Vec<(f64, usize)> = ordered.iter().map(|(t, i, _)| (*t, *i)).collect();
        assert_eq!(
            keys,
            vec![(1.0, 0), (1.0, 1), (2.0, 0), (3.0, 1)],
            "global time order, ties broken by arm so a re-run is identical"
        );
    }

    #[test]
    fn fleet_cap_binds_across_windows_a_per_slug_run_could_not_see() {
        // The point of the whole driver: two windows that never overlap in
        // a per-slug run are competing for one pool here. Room for one
        // $24.44 clip, two arms that both want it.
        let dir = scratch("binds");
        let (tape, params) = two_arm_tape(&dir);

        let out = dir.join("capped.jsonl");
        run(fleet_opts(&tape, &params, &out, 26.0)).unwrap();
        let rows = fleet_rows(&out);
        assert_eq!(rows.len(), 2);
        let fires: i64 = rows.iter().map(|r| r["sim"]["fires"].as_i64().unwrap()).sum();
        assert_eq!(fires, 1, "the pool funded exactly one clip across the fleet");

        // btc leads eth by 0.1s at every tick, so btc is the one that eats
        // the pool — order in, order out. eth never gets a look-in, and
        // btc's own second tick is refused by the exposure it just built.
        let btc = rows.iter().find(|r| r["slug"] == "btc-updown-15m-600").unwrap();
        let eth = rows.iter().find(|r| r["slug"] == "eth-updown-15m-600").unwrap();
        assert_eq!(btc["sim"]["fires"], 1);
        assert_eq!(btc["fleet"]["blocks"], 1, "its own committed fills the pool on tick two");
        assert_eq!(eth["sim"]["fires"], 0);
        assert_eq!(eth["fleet"]["blocks"], 2, "both of eth's ticks name the fleet");
    }

    #[test]
    fn fleet_cap_zero_is_the_uncapped_baseline() {
        // `--fleet-cap 0` runs the SAME interleaved driver with no cap, so
        // an A/B differs in the cap and in nothing else.
        let dir = scratch("baseline");
        let (tape, params) = two_arm_tape(&dir);

        let out = dir.join("uncapped.jsonl");
        run(fleet_opts(&tape, &params, &out, 0.0)).unwrap();
        let rows = fleet_rows(&out);
        for r in &rows {
            assert_eq!(r["sim"]["fires"], 2, "every arm fires every tick when nothing rations it");
            assert_eq!(r["fleet"]["blocks"], 0);
        }
        assert_eq!(rows.len(), 2);
    }

    #[test]
    fn fleet_cap_is_reproducible() {
        // L33's lesson at the harness level: the same frozen input twice
        // has to give the same number, or an A/B proves nothing.
        let dir = scratch("repro");
        let (tape, params) = two_arm_tape(&dir);
        let (a, b) = (dir.join("a.jsonl"), dir.join("b.jsonl"));
        run(fleet_opts(&tape, &params, &a, 26.0)).unwrap();
        run(fleet_opts(&tape, &params, &b, 26.0)).unwrap();
        assert_eq!(std::fs::read_to_string(&a).unwrap(), std::fs::read_to_string(&b).unwrap());
    }

    #[test]
    fn aggregate_sums_across_windows() {
        let a = build_report(
            "s1", "evals",
            &FillSim { fire_count: 2, cost: 50.0, fees: 1.0, max_committed: 50.0, first_fire_t: Some(10.0), ..Default::default() },
            &RealTally { fires: 3, notional: 60.0, first_fire_t: Some(9.0) },
            Some("up".to_string()), Some(5.0),
        );
        let b = build_report(
            "s2", "evals",
            &FillSim { fire_count: 1, cost: 20.0, fees: 0.5, max_committed: 20.0, first_fire_t: Some(15.0), ..Default::default() },
            &RealTally { fires: 1, notional: 20.0, first_fire_t: Some(14.0) },
            Some("down".to_string()), Some(-2.0),
        );
        let agg = aggregate("s", "evals", &[a, b]);
        assert_eq!(agg["sim"]["fires"], 3);
        assert!((agg["sim"]["notional"].as_f64().unwrap() - 70.0).abs() < 1e-9);
        assert_eq!(agg["sim"]["first_fire_t"], 10.0);
        assert!((agg["sim"]["pnl"].as_f64().unwrap() - 3.0).abs() < 1e-9);
        assert_eq!(agg["real"]["fires"], 4);
        assert_eq!(agg["real"]["first_fire_t"], 9.0);
    }
}
