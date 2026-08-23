//! Characterization fixtures — real, wallet-graded windows frozen into the
//! repo so the decision core is regression-tested against trades that
//! actually happened, forever, on a checkout that has never seen `~/.pmt`.
//!
//! One fixture is ONE window in ONE self-contained JSON file: the eval tape
//! slice, the book tape slice, the 1m klines the model reads, the params as
//! they were armed, the wallet's verdict, and an expectation block. Nothing
//! is resolved outside the file — no network, no corpus, no home directory.
//!
//! Three rules make the suite worth having:
//!
//! 1. **Wallet-graded only.** `outcome.source` must be `"wallet"`. A
//!    chainlink- or book-derived label is an inference, and L36 is the
//!    receipt for what happens when an inference is treated as ground
//!    truth — sub-1bp chainlink labels graded WORSE than a coin flip. A
//!    fixture is the thing other measurements are checked against, so it
//!    only ever carries the settlement Polymarket actually paid.
//! 2. **Expectations are characterization, not aspiration.** They are
//!    generated from the CURRENT engine's replay of the fixture at curation
//!    time (`--bless`) and then hand-checked against the wallet record. They
//!    describe what the engine does, not what it should do.
//! 3. **A failure is a finding.** A fixture that stops passing means the
//!    decision core changed behaviour on a real trade. Regenerating it is a
//!    deliberate act that has to be justified in the commit message; it is
//!    never the way to get CI green.
//!
//! The runner drives the SAME `decide()` path `--mode full` / `--mode evals`
//! drive — `replay_full_window_with` / `replay_evals_window_traced`, not a
//! lookalike — and asserts against the decision tape those emit, i.e. the
//! engine's own account of what it did.

use super::rtds::RtdsTimeline;
use super::{
    params_from_value, real_tally_from, replay_evals_window_traced, replay_full_window_with,
    FullFeed,
};
use crate::strategies::updown::FEED_RTDS;
use crate::strategies::updown_model::Kline;
use serde::{Deserialize, Serialize};
use serde_json::Value;
use std::collections::BTreeMap;
use std::path::{Path, PathBuf};

/// Bump only for a breaking change to the on-disk shape. A fixture whose
/// version this binary doesn't know fails to load rather than replaying as
/// something subtly different.
pub const FIXTURE_VERSION: u32 = 1;

/// Money comparisons are to the cent. Everything else — fire counts, sides,
/// modes, tick timestamps, gate counts — is exact: the sim is deterministic
/// against frozen input, so a drift there is a determinism bug, not a
/// tolerance problem.
const MONEY_EPS: f64 = 0.01;

// --- on-disk shape ----------------------------------------------------

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
pub struct KlineRow {
    pub t: i64,
    pub o: f64,
    pub c: f64,
}

/// The wallet's verdict plus its accounting. `winner` is what settlement
/// paid; the rest is what the money did, and neither is ever derived from
/// the sim.
#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
pub struct Outcome {
    pub winner: String,
    /// Must be "wallet". See rule 1 in the module header.
    pub source: String,
    #[serde(default)]
    pub buy: f64,
    #[serde(default)]
    pub buy_shares: f64,
    #[serde(default)]
    pub buy_side: Option<String>,
    #[serde(default)]
    pub sell: f64,
    #[serde(default)]
    pub redeem: f64,
    #[serde(default)]
    pub redeem_seen: bool,
    #[serde(default)]
    pub redeem_outcome: Option<String>,
    #[serde(default)]
    pub pnl: f64,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Default)]
pub struct FiresExpect {
    pub count: usize,
    pub by_side: BTreeMap<String, usize>,
    pub by_mode: BTreeMap<String, usize>,
    pub first_t: Option<f64>,
    pub notional: f64,
    pub max_committed: f64,
}

/// One gate the runner pins by name: the exact refusal sentence decide()
/// emitted at exactly this tick.
#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
pub struct GateTick {
    pub t: f64,
    pub reason: String,
}

/// Gate expectations in two registers. `by_class` buckets on the refusal's
/// leading phrase (everything before the first ':'), so it survives the
/// numbers moving inside a sentence and catches a whole class of gate
/// appearing or vanishing. `at` pins named ticks to the FULL sentence, so a
/// reworded reason or a drifted number fails loudly somewhere — L14's
/// lesson (prose crossing a boundary with nothing checking it) turned into
/// a test.
#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Default)]
pub struct GatesExpect {
    pub total: usize,
    pub by_class: BTreeMap<String, usize>,
    pub at: Vec<GateTick>,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Default)]
pub struct PnlExpect {
    /// "neg" | "pos" | "zero" — the sign is the assertion; `value` is
    /// context for a human reading a failure.
    pub sign: String,
    pub value: f64,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Default)]
pub struct Expect {
    pub fires: FiresExpect,
    pub gates: GatesExpect,
    pub pnl: PnlExpect,
    pub outcome: String,
    #[serde(default)]
    pub generated_at: String,
    #[serde(default)]
    pub generated_by: String,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct Fixture {
    pub fixture_version: u32,
    pub slug: String,
    /// "evals" or "full". Load-bearing, not decoration: the book recorder
    /// only started at 02:45:20Z on 2026-08-23, so every window before it
    /// can be replayed in evals mode alone. A fixture that claims "full"
    /// and ships no book slice fails to load.
    pub mode: String,
    /// One line on why this window earns a permanent slot.
    pub teaches: String,
    #[serde(default)]
    pub lessons_ref: Option<String>,
    #[serde(default)]
    pub era: Vec<String>,
    pub window_utc: Vec<String>,
    /// Exactly one `--params` array entry: ArmParams, plus an optional
    /// `tunables` override for reproducing a pre-brake policy.
    pub params: Value,
    /// Where each param came from — tape-derived vs inherited from the arm
    /// store at freeze time. An inherited value is a reconstruction, and
    /// saying so beats pretending the tape knew.
    #[serde(default)]
    pub params_provenance: Value,
    pub outcome: Outcome,
    /// updown-tape slice for this slug: eval / fire / gated / roll /
    /// cleanup, verbatim.
    pub evals: Vec<Value>,
    /// book-tape slice, trimmed to the fields replay reads. Empty for a
    /// window that predates the recorder.
    #[serde(default)]
    pub book: Vec<Value>,
    /// 1m klines covering [start - KLINE_LOOKBACK_S, end). Full mode on a
    /// `feed = "binance"` arm only.
    #[serde(default)]
    pub klines: Vec<KlineRow>,
    /// RTDS recorder rows for this window's symbol — the market data a
    /// `feed = "rtds"` arm actually read, and the klines' counterpart on
    /// that feed. Additive: a Binance fixture has none and never will, and
    /// every fixture frozen before the stream existed loads unchanged.
    ///
    /// The settlement stream serves NO history, so unlike klines this
    /// cannot be re-fetched by anyone, ever. The slice in the file is the
    /// only surviving record that this window's market data existed.
    #[serde(default)]
    pub rtds: Vec<Value>,
    /// Named checks from the vocabulary in `check_invariant`. An unknown
    /// name is a hard error — never a silent pass.
    #[serde(default)]
    pub invariants: Vec<String>,
    /// `None` until blessed. A fixture with no expectations loads (so it can
    /// be inspected) but cannot pass a run.
    #[serde(default)]
    pub expect: Option<Expect>,
    #[serde(default)]
    pub provenance: Value,
}

// --- loading ----------------------------------------------------------

pub fn load_fixture(path: &Path) -> Result<Fixture, String> {
    let text = std::fs::read_to_string(path)
        .map_err(|e| format!("{}: {}", path.display(), e))?;
    let fx: Fixture = serde_json::from_str(&text)
        .map_err(|e| format!("{}: corrupt fixture: {}", path.display(), e))?;
    validate(&fx).map_err(|e| format!("{}: {}", path.display(), e))?;
    Ok(fx)
}

fn validate(fx: &Fixture) -> Result<(), String> {
    if fx.fixture_version != FIXTURE_VERSION {
        return Err(format!(
            "fixture_version {} — this binary reads version {}",
            fx.fixture_version, FIXTURE_VERSION
        ));
    }
    if fx.slug.is_empty() {
        return Err("empty slug".to_string());
    }
    // Ground truth or nothing (L36). A derived label would make the whole
    // suite an argument with itself.
    if fx.outcome.source != "wallet" {
        return Err(format!(
            "outcome.source is '{}' — characterization fixtures take wallet-graded \
             outcomes only (see docs/LESSONS.md#L36)",
            fx.outcome.source
        ));
    }
    if fx.outcome.winner != "up" && fx.outcome.winner != "down" {
        return Err(format!("outcome.winner '{}' is neither up nor down", fx.outcome.winner));
    }
    if fx.evals.is_empty() {
        return Err("no tape records — nothing to replay".to_string());
    }
    let (p, _) = params_from_value(&fx.params)?;
    match fx.mode.as_str() {
        "evals" => {}
        "full" => {
            if fx.book.is_empty() {
                return Err(
                    "mode 'full' with no book slice — this window predates the book \
                     recorder and can only be replayed in evals mode"
                        .to_string(),
                );
            }
            // Full mode rebuilds the model from the arm's OWN market data,
            // and which slice that is follows `feed` exactly as it does
            // live. A stream-fed fixture carrying klines instead would
            // replay against the venue the arm was moved off — the one
            // wrong answer that still looks like an answer.
            if p.feed == FEED_RTDS {
                if fx.rtds.is_empty() {
                    return Err(
                        "mode 'full' on a feed='rtds' arm with no rtds slice — the model \
                         has to be rebuilt from the settlement stream this window traded \
                         on, klines are a different venue, and the stream serves no \
                         history to fetch. Re-freeze with a corpus that covers the window."
                            .to_string(),
                    );
                }
            } else if fx.klines.is_empty() {
                return Err(
                    "mode 'full' with no klines — full mode rebuilds the model from \
                     them, and fetching is not an option offline"
                        .to_string(),
                );
            }
        }
        other => return Err(format!("unknown mode '{}' (want 'evals' or 'full')", other)),
    }
    for inv in &fx.invariants {
        known_invariant(inv)?;
    }
    Ok(())
}

/// Every `*.json` under a directory (sorted, non-recursive), or the single
/// file itself.
pub fn fixture_paths(path: &Path) -> Result<Vec<PathBuf>, String> {
    let meta = std::fs::metadata(path)
        .map_err(|e| format!("{}: {}", path.display(), e))?;
    if meta.is_file() {
        return Ok(vec![path.to_path_buf()]);
    }
    let mut out: Vec<PathBuf> = std::fs::read_dir(path)
        .map_err(|e| format!("{}: {}", path.display(), e))?
        .filter_map(Result::ok)
        .map(|e| e.path())
        .filter(|p| p.extension().and_then(|s| s.to_str()) == Some("json"))
        .collect();
    out.sort();
    if out.is_empty() {
        return Err(format!("{}: no *.json fixtures found", path.display()));
    }
    Ok(out)
}

// --- running ----------------------------------------------------------

/// What one fixture's replay produced, before it is judged.
pub struct FixtureRun {
    pub report: Value,
    pub observed: Expect,
    /// Every record decide() emitted, in order — the failure messages quote
    /// from it, and `--bless` reads its numbers.
    pub trace: Vec<Value>,
}

#[derive(Debug)]
pub struct FixtureResult {
    pub slug: String,
    pub passed: bool,
    pub failures: Vec<String>,
    pub report: Value,
    pub observed: Expect,
}

/// Replay one fixture through the same decide() path the operator's
/// `--mode evals` / `--mode full` runs drive.
pub fn replay_fixture(fx: &Fixture) -> Result<FixtureRun, String> {
    let (p, tun) = params_from_value(&fx.params)?;
    if p.slug != fx.slug {
        return Err(format!(
            "params slug '{}' does not match fixture slug '{}'",
            p.slug, fx.slug
        ));
    }
    let real = real_tally_from(&fx.evals);
    let winner = Some(fx.outcome.winner.clone());

    let (report, trace) = match fx.mode.as_str() {
        "evals" => replay_evals_window_traced(&p, tun, &fx.evals, winner, &real),
        "full" => {
            // The offline seam: the fixture hands in its own market data,
            // so neither branch can reach a network or a corpus.
            let mut feed_src = if p.feed == FEED_RTDS {
                FullFeed::Rtds(Box::new(RtdsTimeline::from_records(&p, &fx.rtds)?))
            } else {
                FullFeed::Klines(std::sync::Arc::new(
                    fx.klines
                        .iter()
                        .map(|k| (k.t, Kline { t: k.t, o: k.o, c: k.c }))
                        .collect::<BTreeMap<i64, Kline>>(),
                ))
            };
            replay_full_window_with(&mut feed_src, &p, tun, &fx.book, winner, &real)
        }
        other => return Err(format!("unknown mode '{}'", other)),
    };
    let observed = observe(&report, &trace, &fx.expect);
    Ok(FixtureRun { report, observed, trace })
}

/// Read the run's own decision tape into the expectation shape. `prior`
/// supplies which ticks are "named" — a re-run must pin the same ticks the
/// blessed expectation named, not whichever ticks this run happened to
/// produce.
fn observe(report: &Value, trace: &[Value], prior: &Option<Expect>) -> Expect {
    let mut fires = FiresExpect::default();
    let mut gates = GatesExpect::default();
    let mut gate_ticks: Vec<GateTick> = Vec::new();

    for rec in trace {
        match rec.get("ev").and_then(|v| v.as_str()).unwrap_or("") {
            "fire" => {
                fires.count += 1;
                let side = rec["side"].as_str().unwrap_or("?").to_string();
                let mode = rec["mode"].as_str().unwrap_or("?").to_string();
                *fires.by_side.entry(side).or_insert(0) += 1;
                *fires.by_mode.entry(mode).or_insert(0) += 1;
                let t = rec["t"].as_f64().unwrap_or(0.0);
                fires.first_t = Some(fires.first_t.map_or(t, |a: f64| a.min(t)));
            }
            "gated" => {
                gates.total += 1;
                let reason = rec["reason"].as_str().unwrap_or("gated").to_string();
                *gates.by_class.entry(gate_class(&reason)).or_insert(0) += 1;
                gate_ticks.push(GateTick { t: rec["t"].as_f64().unwrap_or(0.0), reason });
            }
            _ => {}
        }
    }
    // Money is stored to the cent, which is also how it is compared — an
    // expectation file full of 24.439999999999998 reads as precision it
    // does not have.
    fires.notional = round2(report["sim"]["notional"].as_f64().unwrap_or(0.0));
    fires.max_committed = round2(report["sim"]["max_committed"].as_f64().unwrap_or(0.0));

    gates.at = match prior {
        // Re-observe exactly the ticks the blessed expectation named. A
        // missing one reads as an empty reason and fails loudly, which is
        // the correct outcome: the gate that was there is gone.
        Some(e) => e
            .gates
            .at
            .iter()
            .map(|want| GateTick {
                t: want.t,
                reason: gate_ticks
                    .iter()
                    .find(|g| (g.t - want.t).abs() < 1e-6)
                    .map(|g| g.reason.clone())
                    .unwrap_or_default(),
            })
            .collect(),
        None => seed_named_gates(&gate_ticks, fires.first_t),
    };

    let pnl = report["sim"]["pnl"].as_f64().unwrap_or(0.0);
    Expect {
        fires,
        gates,
        pnl: PnlExpect { sign: sign_of(pnl).to_string(), value: round2(pnl) },
        outcome: report["sim"]["outcome"].as_str().unwrap_or("").to_string(),
        generated_at: String::new(),
        generated_by: String::new(),
    }
}

/// Which gates a fresh bless pins by name: the first, the last, and the one
/// immediately before the first fire — the three ticks a human reading a
/// window actually asks about (when did it start refusing, was it still
/// refusing at the end, what was it saying right before it changed its
/// mind).
fn seed_named_gates(ticks: &[GateTick], first_fire_t: Option<f64>) -> Vec<GateTick> {
    let mut out: Vec<GateTick> = Vec::new();
    let mut push = |g: Option<&GateTick>| {
        if let Some(g) = g {
            if !out.iter().any(|o| (o.t - g.t).abs() < 1e-6) {
                out.push(g.clone());
            }
        }
    };
    push(ticks.first());
    if let Some(ff) = first_fire_t {
        push(ticks.iter().rfind(|g| g.t < ff));
    }
    push(ticks.last());
    out.sort_by(|a, b| a.t.partial_cmp(&b.t).unwrap_or(std::cmp::Ordering::Equal));
    out
}

/// The refusal's leading phrase — "basis guard: projected margin -0.9bp
/// inside 3.0bp noise band" buckets as "basis guard".
fn gate_class(reason: &str) -> String {
    reason.split(':').next().unwrap_or(reason).trim().to_string()
}

fn sign_of(v: f64) -> &'static str {
    if v > MONEY_EPS {
        "pos"
    } else if v < -MONEY_EPS {
        "neg"
    } else {
        "zero"
    }
}

fn round2(v: f64) -> f64 {
    (v * 100.0).round() / 100.0
}

/// Judge one fixture. Every divergence is reported, not just the first —
/// a fixture that moved usually moved in several places at once, and the
/// shape of the whole move is the diagnosis.
pub fn run_fixture(fx: &Fixture) -> Result<FixtureResult, String> {
    let run = replay_fixture(fx)?;
    let Some(want) = fx.expect.as_ref() else {
        return Err(format!(
            "{}: no expectations — run `pmt crypto fixture {} --regen` to bless it",
            fx.slug, fx.slug
        ));
    };
    let got = &run.observed;
    let mut f: Vec<String> = Vec::new();

    if got.fires.count != want.fires.count {
        f.push(format!(
            "fires: expected {}, got {} ({:+})",
            want.fires.count,
            got.fires.count,
            got.fires.count as i64 - want.fires.count as i64
        ));
    }
    if got.fires.by_side != want.fires.by_side {
        f.push(format!(
            "fires by side: expected {}, got {}",
            fmt_counts(&want.fires.by_side),
            fmt_counts(&got.fires.by_side)
        ));
    }
    if got.fires.by_mode != want.fires.by_mode {
        f.push(format!(
            "fires by mode: expected {}, got {}",
            fmt_counts(&want.fires.by_mode),
            fmt_counts(&got.fires.by_mode)
        ));
    }
    match (want.fires.first_t, got.fires.first_t) {
        (Some(w), Some(g)) if (w - g).abs() > 1e-6 => f.push(format!(
            "first fire tick: expected {:.6}, got {:.6} ({:+.2}s)",
            w,
            g,
            g - w
        )),
        (Some(w), None) => f.push(format!("first fire tick: expected {:.6}, got no fire", w)),
        (None, Some(g)) => {
            f.push(format!("first fire tick: expected no fire, got one at {:.6}", g))
        }
        _ => {}
    }
    if (got.fires.notional - want.fires.notional).abs() > MONEY_EPS {
        f.push(format!(
            "notional: expected ${:.2}, got ${:.2} ({:+.2})",
            want.fires.notional,
            got.fires.notional,
            got.fires.notional - want.fires.notional
        ));
    }
    if (got.fires.max_committed - want.fires.max_committed).abs() > MONEY_EPS {
        f.push(format!(
            "max committed: expected ${:.2}, got ${:.2} ({:+.2})",
            want.fires.max_committed,
            got.fires.max_committed,
            got.fires.max_committed - want.fires.max_committed
        ));
    }
    if got.gates.total != want.gates.total {
        f.push(format!(
            "gated ticks: expected {}, got {} ({:+})",
            want.gates.total,
            got.gates.total,
            got.gates.total as i64 - want.gates.total as i64
        ));
    }
    if got.gates.by_class != want.gates.by_class {
        f.push(format!(
            "gate classes: expected {}, got {}",
            fmt_counts(&want.gates.by_class),
            fmt_counts(&got.gates.by_class)
        ));
    }
    for (w, g) in want.gates.at.iter().zip(got.gates.at.iter()) {
        if w.reason != g.reason {
            f.push(format!(
                "gate @{:.3}: expected {:?}, got {:?}",
                w.t,
                w.reason,
                if g.reason.is_empty() { "<no gate at this tick>" } else { &g.reason }
            ));
        }
    }
    if got.pnl.sign != want.pnl.sign {
        f.push(format!(
            "pnl sign: expected {} (${:+.2}), got {} (${:+.2})",
            want.pnl.sign, want.pnl.value, got.pnl.sign, got.pnl.value
        ));
    }
    if got.outcome != want.outcome {
        f.push(format!("settled outcome: expected {}, got {}", want.outcome, got.outcome));
    }
    for inv in &fx.invariants {
        if let Some(msg) = check_invariant(inv, fx, got)? {
            f.push(format!("invariant {} violated: {}", inv, msg));
        }
    }

    Ok(FixtureResult {
        slug: fx.slug.clone(),
        passed: f.is_empty(),
        failures: f,
        report: run.report,
        observed: run.observed,
    })
}

fn fmt_counts(m: &BTreeMap<String, usize>) -> String {
    if m.is_empty() {
        return "{}".to_string();
    }
    let inner: Vec<String> = m.iter().map(|(k, v)| format!("{}={}", k, v)).collect();
    format!("{{{}}}", inner.join(" "))
}

// --- invariants -------------------------------------------------------

fn split_invariant(inv: &str) -> (&str, Option<&str>) {
    match inv.split_once(':') {
        Some((name, arg)) => (name, Some(arg)),
        None => (inv, None),
    }
}

/// The vocabulary, as a name check. Kept separate from evaluation so a
/// typo'd invariant is caught at LOAD time, not on the one run where it
/// would have mattered.
fn known_invariant(inv: &str) -> Result<(), String> {
    let (name, arg) = split_invariant(inv);
    let needs_arg = |ok: bool| {
        if ok {
            Ok(())
        } else {
            Err(format!("invariant '{}' needs a numeric argument", inv))
        }
    };
    match name {
        "no_fire" | "sim_notional_ge_wallet" => Ok(()),
        "no_fire_before_t" | "max_committed_le" | "gated_ticks_ge" | "fires_eq" => {
            needs_arg(arg.and_then(|a| a.parse::<f64>().ok()).is_some())
        }
        "all_fires_side" => match arg {
            Some("up") | Some("down") => Ok(()),
            _ => Err(format!("invariant '{}' wants side up|down", inv)),
        },
        "all_fires_mode" => match arg {
            Some("safe") | Some("spec") | Some("flip") => Ok(()),
            _ => Err(format!("invariant '{}' wants mode safe|spec|flip", inv)),
        },
        "pnl_sign" => match arg {
            Some("neg") | Some("pos") | Some("zero") => Ok(()),
            _ => Err(format!("invariant '{}' wants sign neg|pos|zero", inv)),
        },
        _ => Err(format!(
            "unknown invariant '{}' — a fixture may only declare checks the runner \
             implements, never ones it silently ignores",
            inv
        )),
    }
}

/// `Ok(None)` = held. `Ok(Some(msg))` = violated, msg says how.
fn check_invariant(
    inv: &str,
    fx: &Fixture,
    got: &Expect,
) -> Result<Option<String>, String> {
    known_invariant(inv)?;
    let (name, arg) = split_invariant(inv);
    let num = || arg.and_then(|a| a.parse::<f64>().ok()).unwrap_or(0.0);
    let out = match name {
        "no_fire" => (got.fires.count > 0).then(|| {
            format!("{} clip(s) fired, first at {:?}", got.fires.count, got.fires.first_t)
        }),
        "no_fire_before_t" => got
            .fires
            .first_t
            .filter(|t| *t < num())
            .map(|t| format!("first fire at {:.3}, before {:.3}", t, num())),
        "fires_eq" => (got.fires.count as f64 != num())
            .then(|| format!("{} clip(s) fired, wanted {:.0}", got.fires.count, num())),
        "all_fires_side" => {
            let want = arg.unwrap_or("");
            got.fires
                .by_side
                .keys()
                .any(|s| s != want)
                .then(|| format!("sides fired: {}", fmt_counts(&got.fires.by_side)))
        }
        "all_fires_mode" => {
            let want = arg.unwrap_or("");
            got.fires
                .by_mode
                .keys()
                .any(|s| s != want)
                .then(|| format!("modes fired: {}", fmt_counts(&got.fires.by_mode)))
        }
        "pnl_sign" => (got.pnl.sign != arg.unwrap_or(""))
            .then(|| format!("pnl {} (${:+.2})", got.pnl.sign, got.pnl.value)),
        "max_committed_le" => (got.fires.max_committed > num() + MONEY_EPS)
            .then(|| format!("max committed ${:.2} > ${:.2}", got.fires.max_committed, num())),
        "gated_ticks_ge" => ((got.gates.total as f64) < num())
            .then(|| format!("{} gated tick(s) < {:.0}", got.gates.total, num())),
        // The instant-fill sim has no partials and no queue, so it can only
        // ever spend MORE than the wallet did. Under-spending means the sim
        // got quietly optimistic about a book that was actually thin —
        // exactly the "improvement" that would hide the gap this suite
        // documents on purpose.
        "sim_notional_ge_wallet" => (got.fires.notional < fx.outcome.buy - MONEY_EPS).then(|| {
            format!(
                "sim spent ${:.2} against the wallet's ${:.2} — the instant-fill sim \
                 must over-state exposure, never under-state it",
                got.fires.notional, fx.outcome.buy
            )
        }),
        _ => None,
    };
    Ok(out)
}

// --- entry point ------------------------------------------------------

pub struct FixtureOpts {
    pub path: PathBuf,
    /// Restrict to one slug (exact match).
    pub only: Option<String>,
    /// Rewrite the fixture's expectations from this run. Refuses to touch
    /// more than one fixture per invocation and prints the diff first.
    pub bless: bool,
}

pub fn run(opts: FixtureOpts) -> Result<(), String> {
    let mut paths = fixture_paths(&opts.path)?;
    if let Some(only) = &opts.only {
        paths.retain(|p| {
            p.file_stem().and_then(|s| s.to_str()).map(|s| s == only).unwrap_or(false)
        });
        if paths.is_empty() {
            return Err(format!("no fixture named '{}' under {}", only, opts.path.display()));
        }
    }
    if opts.bless && paths.len() != 1 {
        // Mass regeneration is the exact failure mode this suite exists to
        // prevent: it turns "the engine changed on 9 real trades" into a
        // one-line diff nobody reads.
        return Err(format!(
            "--bless matched {} fixtures — bless exactly one at a time (use --only <slug>)",
            paths.len()
        ));
    }

    let mut failed = 0usize;
    for path in &paths {
        let mut fx = load_fixture(path)?;
        if opts.bless {
            return bless(&mut fx, path);
        }
        let res = run_fixture(&fx)?;
        println!("{}", res.report);
        if res.passed {
            println!("PASS {}", res.slug);
        } else {
            failed += 1;
            println!("FAIL {}  ({})", res.slug, fx.teaches);
            for msg in &res.failures {
                println!("  - {}", msg);
            }
        }
    }
    if failed > 0 {
        return Err(format!(
            "{}/{} characterization fixture(s) FAILED — the decision core changed \
             behaviour on a real trade. Triage before merging; regenerating an \
             expectation is a deliberate act that belongs in the commit message.",
            failed,
            paths.len()
        ));
    }
    eprintln!("[fixtures] {} fixture(s) PASS", paths.len());
    Ok(())
}

fn bless(fx: &mut Fixture, path: &Path) -> Result<(), String> {
    let run = replay_fixture(fx)?;
    let mut next = run.observed;
    next.generated_at = utc_now();
    next.generated_by = format!("pmengine {} replay --fixtures --bless", env!("CARGO_PKG_VERSION"));
    // Print what is about to change before changing it — a bless that
    // silently rewrites six numbers is indistinguishable from a bug.
    match &fx.expect {
        None => println!("[bless] {}: first expectations", fx.slug),
        Some(prev) => {
            let diffs = diff_expect(prev, &next);
            if diffs.is_empty() {
                println!("[bless] {}: expectations already match this engine", fx.slug);
            } else {
                println!("[bless] {}: rewriting {} expectation(s)", fx.slug, diffs.len());
                for d in &diffs {
                    println!("  - {}", d);
                }
            }
        }
    }
    fx.expect = Some(next);
    write_fixture(fx, path)
}

fn diff_expect(a: &Expect, b: &Expect) -> Vec<String> {
    let mut out = Vec::new();
    let mut cmp = |label: &str, x: String, y: String| {
        if x != y {
            out.push(format!("{}: {} -> {}", label, x, y));
        }
    };
    cmp("fires", a.fires.count.to_string(), b.fires.count.to_string());
    cmp("by_side", fmt_counts(&a.fires.by_side), fmt_counts(&b.fires.by_side));
    cmp("by_mode", fmt_counts(&a.fires.by_mode), fmt_counts(&b.fires.by_mode));
    cmp(
        "first_t",
        format!("{:?}", a.fires.first_t),
        format!("{:?}", b.fires.first_t),
    );
    cmp("notional", format!("{:.2}", a.fires.notional), format!("{:.2}", b.fires.notional));
    cmp(
        "max_committed",
        format!("{:.2}", a.fires.max_committed),
        format!("{:.2}", b.fires.max_committed),
    );
    cmp("gates", a.gates.total.to_string(), b.gates.total.to_string());
    cmp("gate_classes", fmt_counts(&a.gates.by_class), fmt_counts(&b.gates.by_class));
    cmp(
        "pnl",
        format!("{} {:+.2}", a.pnl.sign, a.pnl.value),
        format!("{} {:+.2}", b.pnl.sign, b.pnl.value),
    );
    cmp("outcome", a.outcome.clone(), b.outcome.clone());
    for (x, y) in a.gates.at.iter().zip(b.gates.at.iter()) {
        cmp(&format!("gate@{:.3}", x.t), x.reason.clone(), y.reason.clone());
    }
    out
}

/// Rewrite the fixture in place, keeping the record arrays one-per-line so
/// a diff of a blessed fixture reads as a diff and not as one 120KB line.
fn write_fixture(fx: &Fixture, path: &Path) -> Result<(), String> {
    let v = serde_json::to_value(fx).map_err(|e| format!("serialize {}: {}", fx.slug, e))?;
    std::fs::write(path, render_fixture(&v))
        .map_err(|e| format!("write {}: {}", path.display(), e))
}

/// Top-level keys pretty, arrays of records compact one per line.
pub fn render_fixture(v: &Value) -> String {
    let obj = match v.as_object() {
        Some(o) => o,
        None => return format!("{}\n", v),
    };
    let mut s = String::from("{\n");
    let n = obj.len();
    for (i, (k, val)) in obj.iter().enumerate() {
        let comma = if i + 1 == n { "" } else { "," };
        match val.as_array() {
            Some(arr) if arr.iter().all(|e| e.is_object() || e.is_number()) && !arr.is_empty() => {
                s.push_str(&format!(" {}: [\n", serde_json::to_string(k).unwrap()));
                for (j, e) in arr.iter().enumerate() {
                    let c = if j + 1 == arr.len() { "" } else { "," };
                    s.push_str(&format!("  {}{}\n", e, c));
                }
                s.push_str(&format!(" ]{}\n", comma));
            }
            _ => {
                let body = serde_json::to_string_pretty(val).unwrap_or_else(|_| val.to_string());
                let indented = body.replace('\n', "\n ");
                s.push_str(&format!(
                    " {}: {}{}\n",
                    serde_json::to_string(k).unwrap(),
                    indented,
                    comma
                ));
            }
        }
    }
    s.push_str("}\n");
    s
}

fn utc_now() -> String {
    chrono::Utc::now().format("%Y-%m-%dT%H:%M:%SZ").to_string()
}

#[cfg(test)]
mod tests {
    use super::*;

    fn scratch(name: &str) -> PathBuf {
        let dir = std::env::temp_dir()
            .join(format!("pmengine-fixture-{}-{}", name, std::process::id()));
        let _ = std::fs::remove_dir_all(&dir);
        std::fs::create_dir_all(&dir).unwrap();
        dir
    }

    /// A one-tick evals fixture that fires exactly one safe clip: the same
    /// shape updown.rs's decide_fires_a_clip_when_all_gates_hold uses, so a
    /// change to the firing policy moves this and the unit test together.
    fn synthetic(slug: &str) -> Fixture {
        Fixture {
            fixture_version: FIXTURE_VERSION,
            slug: slug.to_string(),
            mode: "evals".to_string(),
            teaches: "synthetic: one banked-decided clip".to_string(),
            lessons_ref: None,
            era: vec![],
            window_utc: vec!["1970-01-01T00:10:00Z".into(), "1970-01-01T00:25:00Z".into()],
            params: serde_json::json!({
                "slug": slug, "kind": "twap", "symbol": "BTCUSDT",
                "token_up": format!("{}-up", slug), "token_down": format!("{}-down", slug),
                "start": 600.0, "end": 1500.0,
                "sigma_bp_per_min": 3.0, "fee_rate": 0.07, "size_usdc": 100.0,
            }),
            params_provenance: Value::Null,
            outcome: Outcome {
                winner: "up".to_string(),
                source: "wallet".to_string(),
                buy: 10.0,
                buy_shares: 11.0,
                buy_side: Some("up".to_string()),
                sell: 0.0,
                redeem: 11.0,
                redeem_seen: true,
                redeem_outcome: Some("up".to_string()),
                pnl: 1.0,
            },
            evals: vec![
                serde_json::json!({
                    "t": 1390.0, "ev": "gated", "slug": slug, "reason": "feed stale",
                }),
                serde_json::json!({
                    "t": 1400.0, "ev": "eval", "slug": slug,
                    "p_up": 1.0, "sig_bp": 3.0, "rho": 0.0, "banked_decided": true,
                    "sides": [
                        {"side": "up", "fair": 1.0, "ask": 0.94, "net": 0.05},
                        {"side": "down", "fair": 0.0, "ask": 0.05, "net": -0.05},
                    ],
                }),
            ],
            book: vec![],
            klines: vec![],
            rtds: vec![],
            invariants: vec![
                "fires_eq:1".to_string(),
                "all_fires_side:up".to_string(),
                "all_fires_mode:safe".to_string(),
                "no_fire_before_t:1395".to_string(),
            ],
            expect: None,
            provenance: Value::Null,
        }
    }

    fn blessed(slug: &str) -> Fixture {
        let mut fx = synthetic(slug);
        let run = replay_fixture(&fx).unwrap();
        fx.expect = Some(run.observed);
        fx
    }

    #[test]
    fn fixture_round_trips_through_serde() {
        let fx = blessed("s");
        let rendered = render_fixture(&serde_json::to_value(&fx).unwrap());
        // The custom renderer must still emit valid JSON — it is what every
        // committed fixture is written with.
        let back: Fixture = serde_json::from_str(&rendered).unwrap();
        validate(&back).unwrap();
        assert_eq!(back.slug, fx.slug);
        assert_eq!(back.mode, fx.mode);
        assert_eq!(back.outcome, fx.outcome);
        assert_eq!(back.evals, fx.evals);
        assert_eq!(back.invariants, fx.invariants);
        assert_eq!(back.expect, fx.expect);
        // One record per line keeps a blessed diff readable.
        assert!(rendered.contains("\"evals\": [\n"), "records render one per line");
    }

    #[test]
    fn blessed_fixture_passes_against_itself() {
        let fx = blessed("s");
        let res = run_fixture(&fx).unwrap();
        assert!(res.passed, "failures: {:?}", res.failures);
        assert_eq!(res.observed.fires.count, 1);
        assert_eq!(res.observed.fires.by_mode.get("safe"), Some(&1));
        assert_eq!(res.observed.gates.total, 1);
        assert_eq!(res.observed.pnl.sign, "pos");
    }

    #[test]
    fn a_drifted_decision_core_fails_and_names_the_divergence() {
        // Bless against today's policy, then move the edge floor — a real
        // clip stops happening on a frozen window. The suite's whole job is
        // that this cannot pass quietly, and that the failure reads.
        let mut fx = blessed("s");
        fx.params["min_edge"] = serde_json::json!(0.10);
        let res = run_fixture(&fx).unwrap();
        assert!(!res.passed);
        let joined = res.failures.join("\n");
        assert!(joined.contains("fires: expected 1, got 0 (-1)"), "got: {}", joined);
        assert!(
            joined.contains("first fire tick: expected 1400.000000, got no fire"),
            "got: {}",
            joined
        );
        assert!(joined.contains("notional: expected $24.44, got $0.00 (-24.44)"), "got: {}", joined);
        assert!(joined.contains("invariant fires_eq:1 violated: 0 clip(s) fired"), "got: {}", joined);

        // A careless re-bless rewrites the generated expectations — and
        // still cannot make this green, because `invariants` are curator
        // intent and bless never touches them.
        let mut regenerated = fx.clone();
        regenerated.expect = Some(replay_fixture(&fx).unwrap().observed);
        let after = run_fixture(&regenerated).unwrap();
        assert!(!after.passed, "re-blessing must not launder a real behaviour change");
        assert!(
            after.failures.iter().any(|m| m.contains("invariant fires_eq:1")),
            "got: {:?}",
            after.failures
        );
    }

    #[test]
    fn a_moved_gate_reason_fails_at_the_named_tick() {
        let mut fx = blessed("s");
        // The engine's refusal sentence changed; the tick is still gated.
        fx.evals[0]["reason"] = serde_json::json!("feed stale: connection reset");
        let res = run_fixture(&fx).unwrap();
        assert!(!res.passed);
        let joined = res.failures.join("\n");
        assert!(joined.contains("gate @1390.000"), "got: {}", joined);
        // Same class, so the histogram holds — only the named tick fails.
        assert!(!joined.contains("gate classes"), "got: {}", joined);
    }

    #[test]
    fn a_wallet_pnl_sign_flip_fails() {
        let mut fx = blessed("s");
        fx.outcome.winner = "down".to_string();
        let res = run_fixture(&fx).unwrap();
        assert!(!res.passed);
        let joined = res.failures.join("\n");
        assert!(joined.contains("pnl sign: expected pos"), "got: {}", joined);
        assert!(joined.contains("settled outcome: expected up, got down"), "got: {}", joined);
    }

    #[test]
    fn instant_fill_sim_must_not_under_state_the_wallet() {
        let mut fx = blessed("s");
        fx.invariants.push("sim_notional_ge_wallet".to_string());
        assert!(run_fixture(&fx).unwrap().passed, "sim spends $24.44 vs the wallet's $10");
        // A wallet that spent more than the sim = the sim got optimistic
        // about a thin book.
        fx.outcome.buy = 500.0;
        let res = run_fixture(&fx).unwrap();
        assert!(res.failures.iter().any(|m| m.contains("must over-state exposure")),
                "got: {:?}", res.failures);
    }

    #[test]
    fn a_chainlink_graded_outcome_is_refused_at_load() {
        let mut fx = blessed("s");
        fx.outcome.source = "chainlink".to_string();
        let err = validate(&fx).unwrap_err();
        assert!(err.contains("wallet-graded outcomes only"), "got: {}", err);
    }

    #[test]
    fn full_mode_without_book_or_klines_is_refused_at_load() {
        let mut fx = blessed("s");
        fx.mode = "full".to_string();
        assert!(validate(&fx).unwrap_err().contains("predates the book recorder"));
        fx.book = vec![serde_json::json!({"t": 1400.0, "ev": "book", "slug": "s"})];
        assert!(validate(&fx).unwrap_err().contains("no klines"));
        fx.klines = vec![KlineRow { t: 1380, o: 1.0, c: 1.0 }];
        validate(&fx).unwrap();
    }

    #[test]
    fn a_stream_fed_full_fixture_without_its_rtds_slice_is_refused_at_load() {
        // The trap: a `feed = "rtds"` window frozen with klines would load,
        // replay, and produce a confident answer — off the Binance series
        // the arm was deliberately moved away from. The refusal has to
        // happen at LOAD, before anyone reads a number out of it.
        let mut fx = blessed("s");
        fx.mode = "full".to_string();
        fx.params["feed"] = serde_json::json!("rtds");
        fx.params["symbol"] = serde_json::json!("XRPUSDT");
        fx.book = vec![serde_json::json!({"t": 1400.0, "ev": "book", "slug": "s"})];
        // Klines do not stand in for the stream, however many there are.
        fx.klines = vec![KlineRow { t: 1380, o: 1.0, c: 1.0 }];
        let err = validate(&fx).unwrap_err();
        assert!(err.contains("no rtds slice"), "got: {}", err);
        assert!(err.contains("klines are a different venue"), "got: {}", err);

        fx.rtds = vec![serde_json::json!({
            "t_recv": 1380.2, "topic": "crypto_prices_chainlink", "symbol": "xrp/usd",
            "ts": 1_380_000i64, "value": 2.5,
        })];
        validate(&fx).unwrap();

        // And the mirror stays true: a Binance arm still needs its klines.
        fx.params["feed"] = serde_json::json!("binance");
        fx.klines = vec![];
        assert!(validate(&fx).unwrap_err().contains("no klines"));
    }

    #[test]
    fn an_unknown_invariant_is_refused_rather_than_ignored() {
        let mut fx = blessed("s");
        fx.invariants.push("definitely_not_a_check:3".to_string());
        let err = validate(&fx).unwrap_err();
        assert!(err.contains("unknown invariant"), "got: {}", err);
        // A typo'd argument is caught the same way.
        fx.invariants.pop();
        fx.invariants.push("all_fires_side:sideways".to_string());
        assert!(validate(&fx).unwrap_err().contains("wants side up|down"));
    }

    #[test]
    fn a_missing_or_corrupt_fixture_file_errors_by_name() {
        let dir = scratch("errors");
        let missing = dir.join("nope.json");
        assert!(load_fixture(&missing).unwrap_err().contains("nope.json"));

        let corrupt = dir.join("corrupt.json");
        std::fs::write(&corrupt, "{\"fixture_version\": 1, \"slug\":").unwrap();
        let err = load_fixture(&corrupt).unwrap_err();
        assert!(err.contains("corrupt fixture"), "got: {}", err);

        let wrong_version = dir.join("v99.json");
        let mut fx = blessed("s");
        fx.fixture_version = 99;
        std::fs::write(&wrong_version, serde_json::to_string(&fx).unwrap()).unwrap();
        assert!(load_fixture(&wrong_version).unwrap_err().contains("fixture_version 99"));

        // An empty directory is an error, not a vacuous pass.
        assert!(fixture_paths(&dir.join("empty")).is_err());
        std::fs::create_dir_all(dir.join("empty")).unwrap();
        assert!(fixture_paths(&dir.join("empty")).unwrap_err().contains("no *.json"));
    }

    #[test]
    fn an_unblessed_fixture_cannot_pass() {
        let fx = synthetic("s");
        let err = run_fixture(&fx).unwrap_err();
        assert!(err.contains("no expectations"), "got: {}", err);
    }

    #[test]
    fn bless_writes_expectations_and_the_rerun_passes() {
        let dir = scratch("bless");
        let path = dir.join("s.json");
        let fx = synthetic("s");
        std::fs::write(&path, render_fixture(&serde_json::to_value(&fx).unwrap())).unwrap();

        run(FixtureOpts { path: path.clone(), only: None, bless: true }).unwrap();
        let reloaded = load_fixture(&path).unwrap();
        assert!(reloaded.expect.is_some(), "bless wrote the expectation block");
        assert!(run_fixture(&reloaded).unwrap().passed);
        // And the whole-directory run is green.
        run(FixtureOpts { path: dir.clone(), only: None, bless: false }).unwrap();
    }

    #[test]
    fn bless_refuses_to_regenerate_the_whole_suite_at_once() {
        let dir = scratch("massbless");
        for slug in ["a", "b"] {
            let fx = blessed(slug);
            std::fs::write(
                dir.join(format!("{}.json", slug)),
                render_fixture(&serde_json::to_value(&fx).unwrap()),
            )
            .unwrap();
        }
        let err = run(FixtureOpts { path: dir.clone(), only: None, bless: true }).unwrap_err();
        assert!(err.contains("bless exactly one at a time"), "got: {}", err);
        // --only narrows it to one, which is allowed.
        run(FixtureOpts { path: dir, only: Some("a".to_string()), bless: true }).unwrap();
    }

    #[test]
    fn a_failing_fixture_makes_the_run_exit_non_zero() {
        let dir = scratch("failing");
        let mut fx = blessed("s");
        fx.expect.as_mut().unwrap().fires.count = 99;
        std::fs::write(
            dir.join("s.json"),
            render_fixture(&serde_json::to_value(&fx).unwrap()),
        )
        .unwrap();
        let err = run(FixtureOpts { path: dir, only: None, bless: false }).unwrap_err();
        assert!(err.contains("1/1 characterization fixture(s) FAILED"), "got: {}", err);
    }

    #[test]
    fn gate_class_buckets_on_the_leading_phrase() {
        assert_eq!(
            gate_class("basis guard: projected margin -0.9bp inside 3.0bp noise band"),
            "basis guard"
        );
        assert_eq!(gate_class("feed stale"), "feed stale");
        assert_eq!(gate_class("feed stale: connection reset"), "feed stale");
    }
}
