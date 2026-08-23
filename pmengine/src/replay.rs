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
//!   - `full`: rebuilds FeedState from the book/spot recorder plus cached
//!     Binance 1m klines and calls `eval_model` itself, so it exercises
//!     the whole pipeline decide() sits behind. Only place in this module
//!     that touches the network (`--mode full` klines fetch), and it's
//!     cached to `~/.pmt/corpus/klines-1m-{SYMBOL}.jsonl` so a re-run of
//!     the same window doesn't refetch.
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

use crate::strategies::updown::{Action, ArmParams, ArmState, ArmView, DecideOut, TopOfBook};
use crate::strategies::updown_model::{
    eval_model, lag1_autocorr, shape_klines, FeedState, GateReason, Kline, ModelEval, Tunables,
    EV_EVAL, EV_FIRE, EV_GATED,
};
use serde::Deserialize;
use serde_json::Value;
use std::collections::{BTreeMap, HashMap};
use std::io::BufRead;
use std::path::{Path, PathBuf};

const BINANCE_DATA: &str = "https://data-api.binance.vision";
/// Matches poll_binance's live reach-back so the regime autocorr has real
/// history instead of pinning at 0.
const KLINE_LOOKBACK_S: i64 = 2700;

pub struct ReplayOpts {
    pub mode: String,
    pub tape: Option<PathBuf>,
    pub book_tape: Option<PathBuf>,
    pub slug: String,
    pub params: Option<PathBuf>,
    pub outcomes: Option<PathBuf>,
    pub out: Option<PathBuf>,
}

pub fn run(opts: ReplayOpts) -> Result<(), String> {
    match opts.mode.as_str() {
        "evals" => run_evals(&opts),
        "full" => run_full(&opts),
        other => Err(format!("unknown replay mode '{}' (want 'evals' or 'full')", other)),
    }
}

// --- params file -----------------------------------------------------

/// Replay-only policy override, deserialized separately since Tunables
/// itself has no serde impl (live arms never take it from JSON).
#[derive(Debug, Clone, Deserialize)]
struct TunablesOverride {
    #[serde(default = "default_distrust_net")]
    distrust_net: f64,
    #[serde(default = "default_avg_down_tol")]
    avg_down_tol: f64,
}
fn default_distrust_net() -> f64 {
    Tunables::default().distrust_net
}
fn default_avg_down_tol() -> f64 {
    Tunables::default().avg_down_tol
}
impl From<TunablesOverride> for Tunables {
    fn from(t: TunablesOverride) -> Self {
        Tunables { distrust_net: t.distrust_net, avg_down_tol: t.avg_down_tol }
    }
}

#[derive(Debug, Clone, Deserialize)]
struct ParamsFileEntry {
    #[serde(flatten)]
    p: ArmParams,
    #[serde(default)]
    tunables: Option<TunablesOverride>,
}

fn load_params_map(path: Option<&Path>) -> Result<HashMap<String, (ArmParams, Option<Tunables>)>, String> {
    let Some(path) = path else { return Ok(HashMap::new()) };
    let text = std::fs::read_to_string(path).map_err(|e| format!("read {}: {}", path.display(), e))?;
    let entries: Vec<ParamsFileEntry> =
        serde_json::from_str(&text).map_err(|e| format!("parse {}: {}", path.display(), e))?;
    Ok(entries
        .into_iter()
        .map(|e| {
            let tun = e.tunables.map(Tunables::from);
            (e.p.slug.clone(), (e.p, tun))
        })
        .collect())
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

fn load_jsonl(path: &Path) -> Result<Vec<Value>, String> {
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
struct RealTally {
    fires: usize,
    notional: f64,
    first_fire_t: Option<f64>,
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
}

fn apply_fills(arm: &mut ArmState, sim: &mut FillSim, out: &DecideOut, now: f64, fee_rate: f64) {
    for a in &out.actions {
        match a {
            Action::Buy { token, price, size } => {
                let (price, size) = (*price, *size);
                let fee = size * fee_rate * price.min(1.0 - price);
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
            Action::Sell { token, price, size } => {
                let (price, size) = (*price, *size);
                let fee = size * fee_rate * price.min(1.0 - price);
                sim.proceeds += size * price - fee;
                let held = sim.shares.entry(token.clone()).or_insert(0.0);
                let basis = sim.cost_basis.entry(token.clone()).or_insert(0.0);
                if *held > 1e-9 {
                    let avg = *basis / *held;
                    *basis = (*basis - avg * size).max(0.0);
                }
                *held = (*held - size).max(0.0);
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
    let mut arm = ArmState::with_params(p.clone());
    arm.subscribed = true;
    if let Some(t) = tun {
        arm.tunables = t;
    }
    let mut sim = FillSim::default();
    let mut last_p_up: Option<f64> = None;

    for rec in recs {
        let now = rec["t"].as_f64().unwrap_or(0.0);
        match rec.get("ev").and_then(|v| v.as_str()).unwrap_or("") {
            EV_EVAL => {
                let p_up = rec["p_up"].as_f64().unwrap_or(0.5);
                last_p_up = Some(p_up);
                let model = ModelEval {
                    p_up,
                    sig_bp: rec["sig_bp"].as_f64().unwrap_or(0.0),
                    rho: rec["rho"].as_f64().unwrap_or(0.0),
                    banked_decided: rec["banked_decided"].as_bool().unwrap_or(false),
                    margin_bp: rec.get("margin_bp").and_then(|v| v.as_f64()).unwrap_or(0.0),
                    banked_margin_bp: rec.get("banked_bp").and_then(|v| v.as_f64()).unwrap_or(0.0),
                    cushion_bp: rec.get("cushion_bp").and_then(|v| v.as_f64()).unwrap_or(0.0),
                    // Tape written before the dynamic guard shipped has no
                    // "guard_bp" field — fall back to the arm's static param,
                    // matching what the engine actually enforced back then.
                    guard_bp: rec.get("guard_bp").and_then(|v| v.as_f64()).unwrap_or(p.basis_guard_bp),
                    flip_proof: false,
                };
                let view = view_from_sides(&rec["sides"], &sim);
                let out = arm.decide(&view, Ok(model), now);
                apply_fills(&mut arm, &mut sim, &out, now, p.fee_rate);
            }
            EV_GATED => {
                // decide()'s Err branch never reads `view` — a default is
                // fine, we're only here to keep gate bookkeeping faithful.
                let out = arm.decide(&ArmView::default(), Err(gate_from_record(rec)), now);
                apply_fills(&mut arm, &mut sim, &out, now, p.fee_rate);
            }
            _ => {} // fire/roll/cleanup — real fires come from the shared tally
        }
    }

    let outcome = outcome_override.or_else(|| {
        last_p_up.map(|p_up| if p_up >= 0.5 { "up".to_string() } else { "down".to_string() })
    });
    let pnl = outcome.as_deref().map(|w| settle_pnl(&sim, p, w));
    build_report(&p.slug, "evals", &sim, real, outcome, pnl)
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
/// appends only the missing range. The sole network call in this module,
/// and only ever reached from `--mode full` — never from tests, never
/// from evals mode.
fn klines_for_window(symbol: &str, start: i64, end: i64) -> Result<BTreeMap<i64, Kline>, String> {
    let path = kline_cache_path(symbol)?;
    let mut rows = load_kline_cache(&path);
    let lo = start - KLINE_LOOKBACK_S;
    let lo = lo - lo.rem_euclid(60);
    let hi = end - end.rem_euclid(60);
    if !cache_covers(&rows, lo, hi) {
        let client = reqwest::blocking::Client::builder()
            .timeout(std::time::Duration::from_secs(10))
            .build()
            .map_err(|e| format!("client: {}", e))?;
        let fetched = fetch_klines(&client, symbol, lo)?;
        let new_rows: Vec<Kline> = fetched.into_iter().filter(|r| !rows.contains_key(&r.t)).collect();
        append_kline_rows(&path, &new_rows)?;
        for r in &new_rows {
            rows.insert(r.t, *r);
        }
    }
    Ok(rows)
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
    FeedState { spot, spot_ts, per_min, candle_open: None, closes, rho, last_err: None }
}

/// True TWAP-proxy settlement, scored after the window closed — no
/// look-ahead concern here, the outcome already happened. Same math the
/// model banks on: avg of the window's final per-minute values vs the
/// range-start reference.
fn settle_winner(rows: &BTreeMap<i64, Kline>, start: i64, end: i64) -> Option<String> {
    let ref_px = rows.get(&(start - 60)).map(|r| r.mid())?;
    let (mut sum, mut n) = (0.0, 0usize);
    for r in rows.range(start..end).map(|(_, r)| r) {
        sum += r.mid();
        n += 1;
    }
    if n == 0 {
        return None;
    }
    Some(if sum / n as f64 >= ref_px { "up".to_string() } else { "down".to_string() })
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

fn replay_full_window(
    p: &ArmParams,
    tun: Option<Tunables>,
    recs: &[Value],
    outcome_override: Option<String>,
    real: &RealTally,
) -> Result<Value, String> {
    let rows = klines_for_window(&p.symbol, p.start as i64, p.end as i64)?;
    let mut arm = ArmState::with_params(p.clone());
    arm.subscribed = true;
    if let Some(t) = tun {
        arm.tunables = t;
    }
    let mut sim = FillSim::default();

    for rec in recs {
        let now = rec["t"].as_f64().unwrap_or(0.0);
        let spot = rec["spot"].as_f64().unwrap_or(0.0);
        let spot_age = rec["spot_age_s"].as_f64().unwrap_or(0.0);
        let feed = feed_state_at(&rows, p.start as i64, spot, now - spot_age, now);
        // Static guard only — replay has no recorded oracle-sample corpus
        // (yet; oracle-tape.jsonl starts accumulating once the dynamic
        // guard runs live), so pass the operator's param unchanged: replay
        // output must match the exact static-guard behavior this window
        // ran under live.
        let model = eval_model(p, &feed, now, p.basis_guard_bp);
        let view = view_from_book_record(rec, &sim, p);
        let out = arm.decide(&view, model, now);
        apply_fills(&mut arm, &mut sim, &out, now, p.fee_rate);
    }

    let outcome = outcome_override.or_else(|| settle_winner(&rows, p.start as i64, p.end as i64));
    let pnl = outcome.as_deref().map(|w| settle_pnl(&sim, p, w));
    Ok(build_report(&p.slug, "full", &sim, real, outcome, pnl))
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

    let mut reports = Vec::new();
    for (slug, recs) in &windows {
        let Some((p, tun)) = params_map.get(slug).cloned() else {
            eprintln!("[replay] skipping '{}': no --params entry for this slug", slug);
            continue;
        };
        let real = real_by_slug.get(slug).cloned().unwrap_or_default();
        match replay_full_window(&p, tun, recs, outcomes.get(slug).cloned(), &real) {
            Ok(report) => reports.push(report),
            Err(e) => eprintln!("[replay] skipping '{}': {}", slug, e),
        }
    }
    finalize(&opts.slug, "full", &reports, opts.out.as_deref())
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
        // Pre-structured line: reason survives, numbers are simply absent —
        // no regex, no guessing.
        let old = serde_json::json!({"ev": "gated", "reason": "feed stale"});
        assert_eq!(gate_from_record(&old), GateReason::plain("feed stale"));
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

        let old_policy = Tunables { distrust_net: f64::INFINITY, avg_down_tol: f64::INFINITY };
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
