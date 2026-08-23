//! TWAP-gated clip trigger for Polymarket's recurring crypto up/down
//! markets — multi-arm: one strategy instance hunts several windows at
//! once (BTC 5m + BTC 15m + ETH 5m, ...), each arm with its own feed,
//! budget, and clip clock.
//!
//! The division of labor: the operator prices a market out-of-band
//! (`pmt crypto arm`) and feeds params via `POST /strategies/updown/command`.
//! Each arm owns the latency-critical part — a Binance trade-stream
//! websocket plus the live CLOB book — and buys in small clips only while
//! every gate holds. Human minutes decide *what* to hunt; engine
//! milliseconds decide *when*.
//!
//! Gates encode the lessons live trading taught us (2026-08-22):
//!   - edge is net of the crypto_fees_v2 taker fee, never gross
//!   - twap margins inside Chainlink-vs-Binance basis noise are coin flips
//!   - a stale spot feed means no trade, not "trade on the last print"
//!   - mean-reverting chop (negative return autocorr) disables speculative
//!     clips entirely — momentum "locks" are mirages there
//!   - the full budget unlocks only late or banked-beyond-reversion
//!   - quiesce pulls everything before resolution; exits stay live longer

use crate::position::Fill;
use crate::strategies::updown_oracle;
use crate::strategy::{Signal, Strategy, StrategyContext, Urgency};
use rust_decimal::prelude::{FromPrimitive, ToPrimitive};
use rust_decimal::Decimal;
use serde::Deserialize;
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Arc, Mutex};

const BINANCE_DATA: &str = "https://data-api.binance.vision";
/// Spot older than this is a dead feed — hold, never trade through it.
const MAX_SPOT_AGE_S: f64 = 5.0;
/// Exit rule (the -$318 lesson: a 99%-fair entry died with no hands to act
/// as it flipped). Dump a held side when its fair collapses below
/// EXIT_FAIR — but only into a bid within EXIT_MAX_DISCOUNT of fair;
/// selling below that donates to panic.
const EXIT_FAIR: f64 = 0.40;
const EXIT_MAX_DISCOUNT: f64 = 0.08;
/// Live vol floor: minutes of trailing 1m closes for the fast estimate.
const VOL_FAST_WINDOW: usize = 12;
/// Slow trailing window (minutes) that supersedes the arm-time sigma param —
/// roll chains otherwise freeze a floor measured hours ago in a dead regime.
const SIGMA_SLOW_WINDOW: usize = 45;
/// Minimum close samples before the live slow estimate is trusted over the
/// arm-time param.
const SIGMA_SLOW_MIN: usize = 30;
/// One order in flight per token; assume dead if no fill inside this window.
/// Must outlive the engine's ~5s position-reconcile cadence: taker fills
/// are often MISSED by the realtime fill path and only show up via
/// reconcile (proven live), so freeing budget sooner buys fills twice.
const INFLIGHT_TTL_S: f64 = 12.0;
/// Speculative clips still need the model leaning clearly one way.
const EARLY_MIN_FAIR: f64 = 0.55;
/// Book-distrust brake: net above this is the book pricing in something the
/// model hasn't caught, not free edge — every blown-up window tonight
/// entered on huge claimed edge into a collapsing book. Banked-decided
/// TWAPs are exempt: the edge there is math, not a book mispricing.
const BOOK_DISTRUST_NET: f64 = 0.15;
/// No-averaging-down brake: our side getting cheaper after a clip is the
/// market repricing against the thesis, not a discount to chase.
const AVG_DOWN_TOL: f64 = 0.02;

// pub(crate), never plain pub, on purpose: the registry generator registers
// the first `pub struct` in the file, which must be Updown.
#[derive(Debug, Clone, Deserialize)]
pub(crate) struct ArmParams {
    pub slug: String,
    /// "twap" or "close_open" — parsed from the market description upstream.
    pub kind: String,
    pub symbol: String,
    pub token_up: String,
    pub token_down: String,
    /// Window bounds, unix seconds.
    pub start: f64,
    pub end: f64,
    pub sigma_bp_per_min: f64,
    pub fee_rate: f64,
    /// Max total notional (USDC) this arm may spend across both sides.
    pub size_usdc: f64,
    #[serde(default = "d_min_edge")]
    pub min_edge: f64,
    #[serde(default = "d_max_price")]
    pub max_price: f64,
    /// No new orders in the final N seconds; standing orders are pulled.
    #[serde(default = "d_quiesce")]
    pub quiesce_secs: f64,
    /// twap only: |projected margin| below this many bp is basis noise.
    #[serde(default = "d_basis_guard")]
    pub basis_guard_bp: f64,
    /// Optional "up"/"down" to trade one side only.
    #[serde(default)]
    pub side_filter: Option<String>,
    /// Safe-bet mode only: buy a side the model prices at least this high.
    #[serde(default = "d_min_fair")]
    pub min_fair: f64,
    /// Hard floor before ANY fire (0 = the envelope governs timing).
    #[serde(default = "d_min_elapsed")]
    pub min_elapsed_frac: f64,
    /// Max notional per individual fire. Position builds in many small
    /// clips instead of one all-in trigger at a momentum extreme.
    #[serde(default = "d_clip")]
    pub clip_usdc: f64,
    /// Min seconds between clips on the same side.
    #[serde(default = "d_clip_cooldown")]
    pub clip_cooldown_s: f64,
    /// Budget fraction deployable before safe-bet mode unlocks. Early
    /// window = small speculative clips chasing outsized edge only.
    #[serde(default = "d_early_frac")]
    pub early_frac: f64,
    /// Net edge an early (speculative) clip must clear.
    #[serde(default = "d_early_min_edge")]
    pub early_min_edge: f64,
    /// DEPRECATED, unused: superseded by late_rem_s. Kept only so in-flight
    /// arm payloads still deserialize.
    #[allow(dead_code)]
    #[serde(default = "d_late_frac")]
    pub late_frac: f64,
    /// Full budget unlocks with this many seconds left, regardless of
    /// banked-decidedness — absolute risk time, not window fraction: 60%
    /// elapsed leaves 2min on a 5m window but 6min on a 15m one.
    #[serde(default = "d_late_rem")]
    pub late_rem_s: f64,
    /// Lag-1 autocorrelation of 1m returns below which the tape counts as
    /// mean-reverting chop: speculative clips are disabled entirely.
    #[serde(default = "d_rho_block")]
    pub rho_block: f64,
    /// R9 safety gate: the FIRST clip of a window requires
    /// safety = signed_banked_bp / cushion_bp >= theta on the fired side.
    /// 0 disables (clock-gate-only entry, the pre-R9 behavior). θ=1 is
    /// equivalent to requiring banked_decided for entry. Tonight's first
    /// light (n=28): both losses entered at safety < 0.25, median winner
    /// at 0.57 — the clock says "go" long before the evidence does.
    #[serde(default)]
    pub theta: f64,
    /// Assumed max adversarial spot push (bp) for the flip-proof test.
    /// Boundary manipulators shove Binance in the final seconds; when the
    /// banked margin exceeds even that push times the remaining weight,
    /// the TWAP is beyond anyone's reach and late buys are safe.
    #[serde(default = "d_manip_push")]
    pub manip_push_bp: f64,
    /// Re-arm the next window in this recurring series at window close —
    /// same gates and budget, fresh spend. The fleet keeps hunting with
    /// nobody at the keyboard; `disarm` breaks the chain.
    #[serde(default)]
    pub roll: bool,
}

// Two-mode tuning. Early: small clips, only on outsized mispricing, capped
// at early_frac of budget — a wrong "lock" costs a clip, not the stack.
// Late/banked-decided: ease the full budget into the safe bet at 1.5c+.
fn d_min_edge() -> f64 { 0.015 }
fn d_max_price() -> f64 { 0.985 }
fn d_quiesce() -> f64 { 20.0 }
fn d_basis_guard() -> f64 { 3.0 }
fn d_min_fair() -> f64 { 0.97 }
fn d_min_elapsed() -> f64 { 0.0 }
fn d_clip() -> f64 { 25.0 }
fn d_clip_cooldown() -> f64 { 2.0 }
fn d_early_frac() -> f64 { 0.2 }
fn d_early_min_edge() -> f64 { 0.08 }
fn d_late_frac() -> f64 { 0.6 }
fn d_late_rem() -> f64 { 120.0 }
fn d_rho_block() -> f64 { -0.25 }
fn d_manip_push() -> f64 { 25.0 }
/// Flip-proof buys stay live until this close to resolution.
const FLIP_BUY_CUTOFF_S: f64 = 8.0;

/// Shared state an arm's Binance feed threads keep warm. In replay the
/// corpus loader builds one of these per recorded sample instead.
#[derive(Default)]
pub(crate) struct FeedState {
    pub(crate) spot: f64,
    pub(crate) spot_ts: f64,
    /// minute epoch -> (open+close)/2, proxying the Chainlink 60s TWAP.
    pub(crate) per_min: std::collections::BTreeMap<i64, f64>,
    /// Exact 1h candle open once the close_open window starts.
    pub(crate) candle_open: Option<f64>,
    /// Recent 1m closes, oldest first — feeds fast vol + regime autocorr.
    pub(crate) closes: Vec<f64>,
    /// Lag-1 autocorrelation of trailing 1m returns. Negative = the tape
    /// fades its own moves; momentum "locks" are mirages there.
    pub(crate) rho: f64,
    pub(crate) last_err: Option<String>,
}

/// One model read: everything the clip engine needs to pick a mode.
#[derive(Debug, Clone)]
pub(crate) struct ModelEval {
    pub(crate) p_up: f64,
    pub(crate) sig_bp: f64,
    /// twap only: the banked contribution alone decides the window even if
    /// the remaining path fully reverts, with basis + vol cushion. The one
    /// entry condition immune to mean reversion.
    pub(crate) banked_decided: bool,
    /// Stronger: the banked margin survives even an adversarial spot push
    /// (manip_push_bp) sustained for the whole remaining window. When true,
    /// the book's late panic/push prices are free money — nobody can flip
    /// this TWAP anymore.
    pub(crate) flip_proof: bool,
    pub(crate) rho: f64,
    /// R9 safety-gate inputs, recorded on every tick (not yet gating):
    /// projected full-window margin, the locked banked contribution, and the
    /// 1σ-scale residual of the unlocked piece. safety = |banked|/cushion.
    pub(crate) margin_bp: f64,
    pub(crate) banked_margin_bp: f64,
    pub(crate) cushion_bp: f64,
    /// The basis guard actually enforced this eval — p.basis_guard_bp,
    /// possibly raised (never lowered) by the live Chainlink-vs-Binance
    /// oracle read. See `updown_oracle::live_guard_bp`. Recorded on every
    /// eval (not just twap gate checks) so the tape always shows what
    /// threshold was live, even for close_open evals that don't gate on it.
    pub(crate) guard_bp: f64,
}

/// Top-of-book for one token: (price, size) per side, None when the level
/// is missing.
#[derive(Debug, Clone, Copy, Default)]
pub(crate) struct TopOfBook {
    pub(crate) bid: Option<(f64, f64)>,
    pub(crate) ask: Option<(f64, f64)>,
}

/// Everything outside the arm that one decision pass reads. Built from the
/// live StrategyContext on real ticks and from the recorded corpus in
/// replay — the seam that makes the firing policy a pure function.
#[derive(Debug, Clone, Copy, Default)]
pub(crate) struct ArmView {
    pub(crate) up: TopOfBook,
    pub(crate) dn: TopOfBook,
    /// Shares held per side (position tracker), feeding exits.
    pub(crate) held_up: f64,
    pub(crate) held_dn: f64,
    /// Authoritative committed-notional floor: shares held x entry price.
    pub(crate) position_floor: f64,
}

/// One intended order/lifecycle step. The live adapter converts these to
/// engine Signals; the replay executor applies them to simulated fills.
#[derive(Debug, Clone, PartialEq)]
pub(crate) enum Action {
    Subscribe(String),
    Unsubscribe(String),
    Cancel(String),
    Buy { token: String, price: f64, size: f64 },
    Sell { token: String, price: f64, size: f64 },
}

/// Result of one pure decision pass: orders as data, durable-tape records
/// as data, and whether the arm just retired.
#[derive(Debug, Default)]
pub(crate) struct DecideOut {
    pub(crate) actions: Vec<Action>,
    pub(crate) tape: Vec<serde_json::Value>,
    pub(crate) finished: bool,
}

/// Replay-only policy knobs. Live arms always run the defaults — the brake
/// constants are the law in production and are NOT reachable from the arm
/// command. The A/B harness needs the pre-brake policy expressible to
/// reproduce recorded nights; that is this struct's only reason to exist.
#[derive(Debug, Clone, Copy)]
pub(crate) struct Tunables {
    pub(crate) distrust_net: f64,
    pub(crate) avg_down_tol: f64,
}

impl Default for Tunables {
    fn default() -> Self {
        Self { distrust_net: BOOK_DISTRUST_NET, avg_down_tol: AVG_DOWN_TOL }
    }
}

/// Everything one hunted window owns: params, feeds, budget, clip clocks.
pub(crate) struct ArmState {
    pub(crate) p: ArmParams,
    feed: Arc<Mutex<FeedState>>,
    /// Chainlink poller's rolling basis-sample window + poll diagnostics.
    /// Separate lock from `feed` — the poller only takes `feed`'s lock
    /// briefly to read the lag-aligned per_min mark, never holds both.
    oracle: Arc<Mutex<updown_oracle::OracleState>>,
    feed_stop: Arc<AtomicBool>,
    feed_handles: Vec<std::thread::JoinHandle<()>>,
    pub(crate) subscribed: bool,
    cleaned: bool,
    pub(crate) filled_usdc: f64,
    /// Replay-only overrides; always Default (= the live constants) on
    /// arms created through the command path.
    pub(crate) tunables: Tunables,
    /// token -> (notional, emitted_at) for the one order allowed in flight.
    pub(crate) inflight: std::collections::HashMap<String, (f64, f64)>,
    /// token -> last clip time, enforcing the per-side clip cadence.
    last_clip: std::collections::HashMap<String, f64>,
    /// token -> ask price of the last clip, feeding the no-averaging-down
    /// brake. New windows use new token ids so this resets per window.
    last_clip_ask: std::collections::HashMap<String, f64>,
    /// Latched on the first brake trip and held for the window: the
    /// 2026-08-23 audit showed brakes flagged all four losses but blocked
    /// only 10-51% of their exposure — later ticks slid through as the
    /// ask drifted back inside tolerance. Once the window looks wrong, it
    /// stays wrong for speculative entries; banked-decided still trades.
    brake_latched: bool,
    pub(crate) last_eval: Option<serde_json::Value>,
    /// Throttle for eval/gated lines in the durable tape.
    last_tape_at: f64,
    /// Throttle for the book/spot recorder — its own cadence, see
    /// book_sample_due.
    last_book_at: f64,
}

pub struct Updown {
    id: String,
    /// slug -> arm. Every armed window is hunted concurrently.
    arms: std::collections::BTreeMap<String, ArmState>,
    /// Tokens whose resting orders still need pulling after a disarm —
    /// on_command can't emit signals, so the next tick does it.
    pending_cleanup: Vec<String>,
    /// Successor windows waiting on gamma for their token ids. Retried
    /// on a slow clock; a task whose window expires hops to the next one,
    /// so the chain survives gamma outages.
    rolls: Vec<RollTask>,
}

struct RollTask {
    params: ArmParams,
    next_slug: String,
    next_start: f64,
    next_end: f64,
    next_try_at: f64,
}

/// btc-updown-5m-1787442000 + its bounds -> the following window.
/// Recurring series are contiguous: next start = this end.
fn next_window(slug: &str, start: f64, end: f64) -> Option<(String, f64, f64)> {
    let dur = end - start;
    if dur <= 0.0 {
        return None;
    }
    let (prefix, tail) = slug.rsplit_once('-')?;
    if tail.parse::<i64>().ok()? != start as i64 {
        return None;
    }
    Some((format!("{}-{}", prefix, end as i64), end, end + dur))
}

/// Gamma encodes outcomes and clobTokenIds as JSON-in-a-string; map the
/// Up/Down labels to their token ids by index.
fn parse_gamma_tokens(body: &serde_json::Value) -> Result<(String, String), String> {
    let m = body.get(0).ok_or("market not listed yet")?;
    let outcomes: Vec<String> =
        serde_json::from_str(m.get("outcomes").and_then(|v| v.as_str()).ok_or("no outcomes")?)
            .map_err(|e| format!("outcomes: {}", e))?;
    let tokens: Vec<String> = serde_json::from_str(
        m.get("clobTokenIds").and_then(|v| v.as_str()).ok_or("no clobTokenIds")?,
    )
    .map_err(|e| format!("clobTokenIds: {}", e))?;
    let mut up = None;
    let mut down = None;
    for (o, t) in outcomes.iter().zip(tokens.iter()) {
        match o.to_lowercase().as_str() {
            "up" => up = Some(t.clone()),
            "down" => down = Some(t.clone()),
            _ => {}
        }
    }
    match (up, down) {
        (Some(u), Some(d)) => Ok((u, d)),
        _ => Err("outcomes are not Up/Down".to_string()),
    }
}

fn fetch_gamma_tokens(slug: &str) -> Result<(String, String), String> {
    // Short timeout: this runs on the tick thread. One quick call per
    // window close; failures retry on RollTask's slow clock.
    let client = reqwest::blocking::Client::builder()
        .timeout(std::time::Duration::from_millis(1200))
        .build()
        .map_err(|e| format!("client: {}", e))?;
    let body: serde_json::Value = client
        .get("https://gamma-api.polymarket.com/markets")
        .query(&[("slug", slug)])
        .send()
        .map_err(|e| format!("gamma: {}", e))?
        .json()
        .map_err(|e| format!("gamma json: {}", e))?;
    parse_gamma_tokens(&body)
}

/// Append one JSONL record to the durable tape at ~/.pmt/engine/. The tape
/// is the cross-session dataset for calibrating gate parameters — every
/// fire, exit, and periodic eval survives reboots, unlike engine stdout.
fn tape(record: serde_json::Value) {
    use std::io::Write;
    let Ok(home) = std::env::var("HOME") else { return };
    let dir = std::path::PathBuf::from(home).join(".pmt/engine");
    let _ = std::fs::create_dir_all(&dir);
    if let Ok(mut f) = std::fs::OpenOptions::new()
        .create(true)
        .append(true)
        .open(dir.join("updown-tape.jsonl"))
    {
        let _ = writeln!(f, "{}", record);
    }
}

/// Append one JSONL record to the book/spot recorder at ~/.pmt/engine/.
/// Separate file from the eval tape — order-book history isn't
/// backfillable, so this is the raw feed the replay harness (R4) needs;
/// keeping it out of updown-tape.jsonl keeps that file's record types clean.
fn book_tape(record: serde_json::Value) {
    use std::io::Write;
    let Ok(home) = std::env::var("HOME") else { return };
    let dir = std::path::PathBuf::from(home).join(".pmt/engine");
    let _ = std::fs::create_dir_all(&dir);
    if let Ok(mut f) = std::fs::OpenOptions::new()
        .create(true)
        .append(true)
        .open(dir.join("book-tape.jsonl"))
    {
        let _ = writeln!(f, "{}", record);
    }
}

impl ArmState {
    /// Build without feeds (tests and the replay driver use this directly).
    pub(crate) fn with_params(p: ArmParams) -> Self {
        Self {
            p,
            feed: Arc::new(Mutex::new(FeedState::default())),
            oracle: Arc::new(Mutex::new(updown_oracle::OracleState::default())),
            feed_stop: Arc::new(AtomicBool::new(false)),
            feed_handles: Vec::new(),
            subscribed: false,
            cleaned: false,
            filled_usdc: 0.0,
            tunables: Tunables::default(),
            inflight: std::collections::HashMap::new(),
            last_clip: std::collections::HashMap::new(),
            last_clip_ask: std::collections::HashMap::new(),
            brake_latched: false,
            last_eval: None,
            last_tape_at: 0.0,
            last_book_at: 0.0,
        }
    }

    fn tokens(&self) -> [String; 2] {
        [self.p.token_up.clone(), self.p.token_down.clone()]
    }

    fn stop_feed(&mut self) {
        self.feed_stop.store(true, Ordering::SeqCst);
        for h in self.feed_handles.drain(..) {
            let _ = h.join();
        }
    }

    /// Push-based spot off the Binance trade stream (~100ms event age) —
    /// this is where the "ms not s" lives.
    fn spawn_ws_spot(&mut self) {
        let feed = self.feed.clone();
        let stop = self.feed_stop.clone();
        let end = self.p.end;
        let url = format!(
            "wss://data-stream.binance.vision/ws/{}@trade",
            self.p.symbol.to_lowercase()
        );
        self.feed_handles.push(std::thread::spawn(move || {
            while !stop.load(Ordering::SeqCst) && unix_now() < end + 30.0 {
                let Ok((mut sock, _)) = tungstenite::connect(&url) else {
                    feed.lock().unwrap().last_err = Some("ws connect failed".into());
                    std::thread::sleep(std::time::Duration::from_secs(1));
                    continue;
                };
                if let tungstenite::stream::MaybeTlsStream::NativeTls(t) = sock.get_mut() {
                    let _ = t
                        .get_mut()
                        .set_read_timeout(Some(std::time::Duration::from_secs(2)));
                }
                loop {
                    if stop.load(Ordering::SeqCst) || unix_now() > end + 30.0 {
                        return;
                    }
                    match sock.read() {
                        Ok(tungstenite::Message::Text(txt)) => {
                            if let Ok(v) = serde_json::from_str::<serde_json::Value>(&txt) {
                                if let Some(px) =
                                    v["p"].as_str().and_then(|s| s.parse::<f64>().ok())
                                {
                                    let mut f = feed.lock().unwrap();
                                    f.spot = px;
                                    f.spot_ts = unix_now();
                                }
                            }
                        }
                        Ok(_) => {}
                        // Read timeout: quiet tape, keep listening.
                        Err(tungstenite::Error::Io(e))
                            if matches!(
                                e.kind(),
                                std::io::ErrorKind::WouldBlock | std::io::ErrorKind::TimedOut
                            ) => {}
                        Err(_) => break, // reconnect
                    }
                }
            }
        }));
    }

    fn start_feeds(&mut self) {
        self.spawn_ws_spot();
        // Pushed into feed_handles like the other threads — stop_feed's
        // existing join loop covers its teardown, no separate lifecycle.
        self.feed_handles.push(updown_oracle::spawn_poller(
            self.oracle.clone(),
            self.feed.clone(),
            self.feed_stop.clone(),
            self.p.symbol.clone(),
            self.p.end,
        ));
        let feed = self.feed.clone();
        let stop = self.feed_stop.clone();
        let symbol = self.p.symbol.clone();
        let kind = self.p.kind.clone();
        let (start, end) = (self.p.start, self.p.end);

        self.feed_handles.push(std::thread::spawn(move || {
            let client = match reqwest::blocking::Client::builder()
                .timeout(std::time::Duration::from_secs(5))
                .build()
            {
                Ok(c) => c,
                Err(e) => {
                    feed.lock().unwrap().last_err = Some(format!("feed client: {}", e));
                    return;
                }
            };
            while !stop.load(Ordering::SeqCst) {
                let now = unix_now();
                if now > end + 30.0 {
                    break;
                }
                match poll_binance(&client, &symbol, &kind, start, now) {
                    Ok(update) => {
                        let mut f = feed.lock().unwrap();
                        f.spot = update.spot;
                        f.spot_ts = now;
                        f.per_min.extend(update.per_min);
                        if update.candle_open.is_some() {
                            f.candle_open = update.candle_open;
                        }
                        if !update.closes.is_empty() {
                            f.closes = update.closes;
                            f.rho = lag1_autocorr(&f.closes, 60);
                        }
                        f.last_err = None;
                    }
                    Err(e) => {
                        feed.lock().unwrap().last_err = Some(e);
                    }
                }
                // Candles/regime only — the WS thread owns spot freshness.
                std::thread::sleep(std::time::Duration::from_millis(2000));
            }
        }));
    }

    /// Model fair P(UP) plus regime/decidedness context. Errors = gated.
    fn fair_p_up(&self, now: f64) -> Result<ModelEval, String> {
        let f = self.feed.lock().unwrap();
        let effective_guard_bp = {
            let mut o = self.oracle.lock().unwrap();
            // make_contiguous avoids allocating a fresh Vec on every tick —
            // we already hold the exclusive lock.
            let samples = o.samples.make_contiguous();
            updown_oracle::live_guard_bp(samples, self.p.basis_guard_bp, updown_oracle::OBS_MIN_SAMPLES)
        };
        eval_model(&self.p, &f, now, effective_guard_bp)
    }
}

/// The pricing model as a pure function of (params, feed snapshot, t,
/// effective guard) — the live tick locks the feed, computes the live-
/// raised guard on the ArmState side, and delegates; replay hands in a
/// feed state reconstructed from the corpus and passes p.basis_guard_bp
/// unchanged (see src/replay.rs). `effective_guard_bp` is p.basis_guard_bp
/// itself, or higher if the live oracle poller has raised it — never
/// lower. Errors = gated.
pub(crate) fn eval_model(
    p: &ArmParams,
    f: &FeedState,
    now: f64,
    effective_guard_bp: f64,
) -> Result<ModelEval, String> {
    {
        if now - f.spot_ts > MAX_SPOT_AGE_S {
            return Err(match &f.last_err {
                Some(e) => format!("feed stale: {}", e),
                None => "feed stale".to_string(),
            });
        }
        let spot = f.spot;
        // Floor = live 45m trailing sigma once the feed holds real history
        // (the arm-time param is only the cold-start fallback); the fast
        // window still catches the storm either trailing estimate lags.
        let fast_bp = trailing_sigma_bp(&f.closes, VOL_FAST_WINDOW);
        let slow_bp = trailing_sigma_bp(&f.closes, SIGMA_SLOW_WINDOW);
        let sig_bp = vol_floor_bp(slow_bp, f.closes.len(), p.sigma_bp_per_min).max(fast_bp);
        let sig_frac = sig_bp / 1e4;
        let rho = f.rho;

        if p.kind == "close_open" {
            let open = f.candle_open.ok_or("candle open not printed yet")?;
            let t_min = ((p.end - now) / 60.0).max(0.005);
            let z = (spot / open).ln() / (sig_frac * t_min.sqrt());
            Ok(ModelEval {
                p_up: norm_cdf(z), sig_bp, banked_decided: false, flip_proof: false, rho,
                margin_bp: (spot / open - 1.0) * 1e4, banked_margin_bp: 0.0, cushion_bp: 0.0,
                guard_bp: effective_guard_bp,
            })
        } else {
            let ref_px = *f
                .per_min
                .get(&(p.start as i64 - 60))
                .ok_or("range-start reference not printed yet")?;
            let banked: Vec<f64> = f
                .per_min
                .iter()
                .filter(|(t, _)| **t as f64 >= p.start && (**t as f64) < (now - 30.0).min(p.end))
                .map(|(_, v)| *v)
                .collect();
            let banked_s = banked.len() as f64 * 60.0;
            let banked_avg = if banked.is_empty() {
                spot
            } else {
                banked.iter().sum::<f64>() / banked.len() as f64
            };
            let rem = (p.end - now).max(0.0);
            let window = banked_s + rem;
            if window <= 0.0 || rem <= 0.0 {
                let p_up = if banked_avg >= ref_px { 1.0 } else { 0.0 };
                let m = (banked_avg / ref_px - 1.0) * 1e4;
                return Ok(ModelEval {
                    p_up, sig_bp, banked_decided: true, flip_proof: true, rho,
                    margin_bp: m, banked_margin_bp: m, cushion_bp: effective_guard_bp,
                    guard_bp: effective_guard_bp,
                });
            }
            let proj = (banked_avg * banked_s + spot * rem) / window;
            let margin_bp = (proj / ref_px - 1.0) * 1e4;
            // Banked-decided inputs, computed before the guard so gated ticks
            // still record them — the R9 safety-gate sweep needs the corpus
            // to know what |banked|/cushion was while the flat guard held.
            let banked_margin_bp = (banked_avg / ref_px - 1.0) * 1e4 * (banked_s / window);
            let cushion_bp = effective_guard_bp
                + sig_bp * ((rem / 60.0).max(0.02) / 3.0).sqrt() * (rem / window);
            if margin_bp.abs() < effective_guard_bp {
                return Err(format!(
                    "basis guard: projected margin {:+.1}bp inside {:.1}bp noise band [banked {:+.1}bp cushion {:.1}bp]",
                    margin_bp, effective_guard_bp, banked_margin_bp, cushion_bp
                ));
            }
            let breakeven = (ref_px * window - banked_avg * banked_s) / rem;
            let sig_avg = sig_frac * ((rem / 60.0).max(0.02) / 3.0).sqrt();
            let p_up = 1.0 - norm_cdf((breakeven / spot).ln() / sig_avg);
            // Banked-decided: the banked contribution alone survives a full
            // reversion of the remaining path to the reference, with basis
            // noise + one sigma of remaining-average cushion on top.
            let banked_decided =
                banked_margin_bp.abs() > cushion_bp && (banked_margin_bp > 0.0) == (p_up > 0.5);
            // Flip-proof: survives basis noise PLUS a full-remaining-window
            // adversarial push. rem/window scales the push's TWAP influence.
            // Uses effective_guard_bp too (not just the design brief's named
            // three sites) — a fourth p.basis_guard_bp read lived here;
            // leaving it on the stale param would let a raised live guard
            // still wave flip clips through quiesce on noise it no longer
            // trusts.
            let flip_proof = banked_decided
                && banked_margin_bp.abs()
                    > effective_guard_bp + p.manip_push_bp * (rem / window);
            Ok(ModelEval {
                p_up, sig_bp, banked_decided, flip_proof, rho,
                margin_bp, banked_margin_bp, cushion_bp, guard_bp: effective_guard_bp,
            })
        }
    }
}

impl ArmState {
    /// Math-forced evacuation: dump a held side whose fair has collapsed,
    /// into a bid that still resembles fair. Runs every armed tick AND
    /// through quiesce (exits are most needed late; only new buys stop).
    fn exit_actions(
        &mut self,
        view: &ArmView,
        p_up: f64,
        now: f64,
        tape_out: &mut Vec<serde_json::Value>,
    ) -> Vec<Action> {
        let p = self.p.clone();
        let mut actions = Vec::new();
        for (side, token, fair, held, top) in [
            ("up", &p.token_up, p_up, view.held_up, &view.up),
            ("down", &p.token_down, 1.0 - p_up, view.held_dn, &view.dn),
        ] {
            if fair >= EXIT_FAIR || self.inflight.contains_key(token) {
                continue;
            }
            if held < 5.0 {
                continue;
            }
            let Some((bid, bid_size)) = top.bid else {
                continue;
            };
            if bid < fair - EXIT_MAX_DISCOUNT {
                continue; // bid already dead — holding beats donating
            }
            let size = held.min(bid_size).floor();
            if size < 5.0 {
                continue;
            }
            tracing::warn!(
                side, fair, bid, size, slug = %p.slug,
                "updown EXIT — side fair collapsed, evacuating at the bid"
            );
            tape_out.push(serde_json::json!({
                "t": now, "ev": "exit", "slug": p.slug, "side": side,
                "fair": fair, "bid": bid, "size": size,
            }));
            self.inflight.insert(token.clone(), (0.0, now));
            actions.push(Action::Cancel(token.clone()));
            actions.push(Action::Sell { token: token.clone(), price: bid, size });
        }
        actions
    }

    /// Book+spot snapshot for the replay corpus — book history isn't
    /// backfillable, so record now what R4's replay harness will need.
    /// Missing levels record as null rather than skipping the sample.
    fn record_book(&mut self, ctx: &StrategyContext, now: f64) {
        if now < self.p.start || !book_sample_due(now, self.last_book_at, self.p.end) {
            return;
        }
        self.last_book_at = now;
        let level = |token: &str, ask: bool| -> (Option<f64>, Option<f64>) {
            let l = ctx
                .order_books
                .get(token)
                .and_then(|b| if ask { b.best_ask() } else { b.best_bid() });
            (l.and_then(|l| l.price.to_f64()), l.and_then(|l| l.size.to_f64()))
        };
        let (up_bid, up_bid_sz) = level(&self.p.token_up, false);
        let (up_ask, up_ask_sz) = level(&self.p.token_up, true);
        let (dn_bid, dn_bid_sz) = level(&self.p.token_down, false);
        let (dn_ask, dn_ask_sz) = level(&self.p.token_down, true);
        // Tiny lock scope — copy the two floats out, don't hold the feed
        // mutex while formatting/writing JSON.
        let (spot, spot_ts) = {
            let f = self.feed.lock().unwrap();
            (f.spot, f.spot_ts)
        };
        book_tape(serde_json::json!({
            "t": now, "ev": "book", "slug": self.p.slug,
            "up_bid": up_bid, "up_bid_sz": up_bid_sz,
            "up_ask": up_ask, "up_ask_sz": up_ask_sz,
            "dn_bid": dn_bid, "dn_bid_sz": dn_bid_sz,
            "dn_ask": dn_ask, "dn_ask_sz": dn_ask_sz,
            "spot": spot, "spot_age_s": now - spot_ts,
        }));
    }

    /// One pure decision pass — the entire firing policy with I/O stripped:
    /// market state arrives as `view` + `model`, orders leave as Actions,
    /// durable-tape records leave as data. Live ticks and the replay
    /// harness run this exact path — the simulator judges the code that
    /// trades, not a copy of it.
    pub(crate) fn decide(
        &mut self,
        view: &ArmView,
        model: Result<ModelEval, String>,
        now: f64,
    ) -> DecideOut {
        let p = self.p.clone();
        let mut actions = Vec::new();
        let mut tape_out = Vec::new();

        if !self.subscribed {
            self.subscribed = true;
            actions.push(Action::Subscribe(p.token_up.clone()));
            actions.push(Action::Subscribe(p.token_down.clone()));
            return DecideOut { actions, tape: tape_out, finished: false };
        }

        // Window over: pull everything, drop the market, retire the arm.
        if now >= p.end {
            if !self.cleaned {
                self.cleaned = true;
                tracing::info!(slug = %p.slug, filled_usdc = self.filled_usdc, "updown window closed — cleaning up");
                tape_out.push(serde_json::json!({"t": now, "ev": "cleanup", "slug": p.slug}));
                actions.push(Action::Cancel(p.token_up.clone()));
                actions.push(Action::Cancel(p.token_down.clone()));
                actions.push(Action::Unsubscribe(p.token_up.clone()));
                actions.push(Action::Unsubscribe(p.token_down.clone()));
            }
            return DecideOut { actions, tape: tape_out, finished: true };
        }

        // Quiesce: standing orders pulled, no new buys — with one carve-out.
        // When the TWAP is flip-proof (banked beyond even an adversarial
        // spot push), the book's late panic/push prices are free money and
        // clips stay live until FLIP_BUY_CUTOFF_S. Exits always stay live
        // until the final seconds.
        if now >= p.end - p.quiesce_secs {
            let model = model.ok();
            let flip_live = model.as_ref().map(|m| m.flip_proof).unwrap_or(false)
                && now < p.end - FLIP_BUY_CUTOFF_S;
            if !flip_live {
                actions.push(Action::Cancel(p.token_up.clone()));
                actions.push(Action::Cancel(p.token_down.clone()));
            }
            if let Some(m) = model {
                if now < p.end - 5.0 {
                    let exits = self.exit_actions(view, m.p_up, now, &mut tape_out);
                    actions.extend(exits);
                }
                if flip_live {
                    self.inflight.retain(|_, (_, at)| now - *at < INFLIGHT_TTL_S);
                    let inflight_usdc: f64 = self.inflight.values().map(|(n, _)| n).sum();
                    let committed = self.filled_usdc.max(view.position_floor);
                    let room = p.size_usdc - committed - inflight_usdc;
                    let (side, token, fair, top) = if m.p_up > 0.5 {
                        ("up", p.token_up.clone(), m.p_up, view.up)
                    } else {
                        ("down", p.token_down.clone(), 1.0 - m.p_up, view.dn)
                    };
                    let allowed = p.side_filter.as_ref().map(|s| s == side).unwrap_or(true);
                    let cooled = now - self.last_clip.get(&token).copied().unwrap_or(0.0)
                        >= p.clip_cooldown_s;
                    if allowed && cooled && room > 5.0 && !self.inflight.contains_key(&token) {
                        if let Some((ask, ask_size)) = top.ask {
                            let fee = p.fee_rate * ask.min(1.0 - ask);
                            let net = fair - ask - fee;
                            if net >= p.min_edge && ask <= p.max_price {
                                let size =
                                    (p.clip_usdc / ask).min(ask_size).min(room / ask).floor();
                                if size >= 5.0 {
                                    tracing::info!(
                                        side, ask, fair, net, size, slug = %p.slug,
                                        "updown FLIP clip — TWAP beyond reach, book still trading the print"
                                    );
                                    tape_out.push(serde_json::json!({
                                        "t": now, "ev": "fire", "slug": p.slug, "side": side,
                                        "ask": ask, "fair": fair, "net": net, "size": size,
                                        "committed": committed, "mode": "flip", "rho": m.rho,
                                    }));
                                    self.last_clip.insert(token.clone(), now);
                                    self.inflight.insert(token.clone(), (size * ask, now));
                                    actions.push(Action::Cancel(token.clone()));
                                    actions.push(Action::Buy {
                                        token: token.clone(),
                                        price: ask,
                                        size,
                                    });
                                }
                            }
                        }
                    }
                }
            }
            self.last_eval = Some(serde_json::json!({
                "state": if flip_live { "flip" } else { "quiesce" }, "t": now,
            }));
            return DecideOut { actions, tape: tape_out, finished: false };
        }

        let elapsed_frac = (now - p.start) / (p.end - p.start).max(1.0);
        if elapsed_frac < p.min_elapsed_frac {
            self.last_eval = Some(serde_json::json!({
                "state": "gated",
                "reason": format!("window {:.0}% elapsed, firing opens at {:.0}%",
                                  elapsed_frac * 100.0, p.min_elapsed_frac * 100.0),
                "t": now,
            }));
            return DecideOut { actions, tape: tape_out, finished: false };
        }

        let m = match model {
            Ok(v) => v,
            Err(gate) => {
                self.last_eval =
                    Some(serde_json::json!({"state": "gated", "reason": gate, "t": now}));
                if now - self.last_tape_at >= 5.0 {
                    self.last_tape_at = now;
                    // Asks recorded even while gated: the 2026-08-23 audit
                    // couldn't price what the basis guard cost because
                    // fully-gated windows logged no book data at all.
                    tape_out.push(serde_json::json!({
                        "t": now, "ev": "gated", "slug": p.slug, "reason": gate,
                        "up_ask": view.up.ask.map(|(px, _)| px),
                        "dn_ask": view.dn.ask.map(|(px, _)| px),
                    }));
                }
                return DecideOut { actions, tape: tape_out, finished: false };
            }
        };
        let (p_up, sig_bp) = (m.p_up, m.sig_bp);

        // Notional already committed. on_fill events are unreliable for
        // taker orders (fills often only surface via the ~5s position
        // reconcile), so the authoritative floor is the position tracker:
        // shares held x entry price. Take the max of every signal we have.
        self.inflight.retain(|_, (_, at)| now - *at < INFLIGHT_TTL_S);
        let inflight_usdc: f64 = self.inflight.values().map(|(n, _)| n).sum();
        let committed = self.filled_usdc.max(view.position_floor);
        let budget = p.size_usdc - committed - inflight_usdc;

        let exits = self.exit_actions(view, p_up, now, &mut tape_out);
        actions.extend(exits);

        // Exposure envelope: small speculative clips until the window is
        // either late or banked-decided; then the full budget eases into
        // the safe bet clip by clip.
        let unlocked = budget_unlocked(now, p.end, p.late_rem_s, m.banked_decided);
        let cap = p.size_usdc * if unlocked { 1.0 } else { p.early_frac };
        let mut room = (cap - committed - inflight_usdc).min(budget);
        let (edge_req, fair_req) = if unlocked {
            (p.min_edge, p.min_fair)
        } else {
            (p.early_min_edge, EARLY_MIN_FAIR)
        };
        let chop_blocked = !unlocked && m.rho < p.rho_block;

        let mut evals = Vec::new();
        for (side, token, fair, top) in [
            ("up", &p.token_up, p_up, view.up),
            ("down", &p.token_down, 1.0 - p_up, view.dn),
        ] {
            if let Some(only) = &p.side_filter {
                if only != side {
                    continue;
                }
            }
            let Some((ask, ask_size)) = top.ask else {
                continue;
            };
            let fee = p.fee_rate * ask.min(1.0 - ask);
            let net = fair - ask - fee;
            let safety = side_safety(side == "up", m.banked_margin_bp, m.cushion_bp);
            let raw_brake = if distrust_blocks(net, self.tunables.distrust_net, m.banked_decided) {
                Some("distrust")
            } else if avg_down_blocks(
                ask,
                self.last_clip_ask.get(token).copied(),
                self.tunables.avg_down_tol,
                m.banked_decided,
            ) {
                Some("avg_down")
            } else {
                None
            };
            if raw_brake.is_some() {
                self.brake_latched = true;
            }
            let brake = if safety_gate_blocks(
                p.theta,
                &p.kind,
                self.last_clip.is_empty(),
                safety,
            ) {
                Some("safety")
            } else if raw_brake.is_some() {
                raw_brake
            } else if self.brake_latched && !m.banked_decided {
                Some("latched")
            } else {
                None
            };
            let mut eval = serde_json::json!({
                "side": side, "fair": fair, "ask": ask, "net": net,
                "safety": (safety * 100.0).round() / 100.0,
            });
            if let Some(b) = brake {
                eval["brake"] = serde_json::json!(b);
            }
            evals.push(eval);

            let cooled =
                now - self.last_clip.get(token).copied().unwrap_or(0.0) >= p.clip_cooldown_s;
            let firing = brake.is_none()
                && !chop_blocked
                && fair >= fair_req
                && net >= edge_req
                && ask <= p.max_price
                && room > 5.0
                && cooled
                && !self.inflight.contains_key(token);
            if !firing {
                continue;
            }
            let size = (p.clip_usdc / ask).min(ask_size).min(room / ask).floor();
            if size < 5.0 {
                continue;
            }
            tracing::info!(
                side, ask, fair, net, size, unlocked,
                slug = %p.slug,
                "updown clip firing"
            );
            tape_out.push(serde_json::json!({
                "t": now, "ev": "fire", "slug": p.slug, "side": side,
                "ask": ask, "fair": fair, "net": net, "size": size,
                "committed": committed, "elapsed_frac": elapsed_frac,
                "mode": if unlocked { "safe" } else { "spec" }, "rho": m.rho,
            }));
            room -= size * ask;
            self.last_clip.insert(token.clone(), now);
            self.last_clip_ask.insert(token.clone(), ask);
            self.inflight.insert(token.clone(), (size * ask, now));
            actions.push(Action::Cancel(token.clone()));
            actions.push(Action::Buy { token: token.clone(), price: ask, size });
        }

        self.last_eval = Some(serde_json::json!({
            "state": "armed", "t": now, "p_up": p_up, "sig_bp": sig_bp,
            "rho": m.rho, "mode": if unlocked { "safe" } else { "spec" },
            "chop_blocked": chop_blocked, "banked_decided": m.banked_decided,
            "margin_bp": m.margin_bp, "banked_bp": m.banked_margin_bp,
            "cushion_bp": m.cushion_bp, "guard_bp": m.guard_bp,
            "committed": committed, "budget": budget, "room": room,
            "inflight": inflight_usdc, "sides": evals,
        }));
        if now - self.last_tape_at >= 5.0 {
            self.last_tape_at = now;
            tape_out.push(serde_json::json!({
                "t": now, "ev": "eval", "slug": p.slug, "p_up": p_up,
                "sig_bp": sig_bp, "rho": m.rho, "banked_decided": m.banked_decided,
                "margin_bp": m.margin_bp, "banked_bp": m.banked_margin_bp,
                "cushion_bp": m.cushion_bp, "guard_bp": m.guard_bp,
                "committed": committed, "sides": evals,
            }));
        }

        DecideOut { actions, tape: tape_out, finished: false }
    }

    /// Live-tick adapter around `decide`: snapshot the StrategyContext into
    /// an ArmView, run the pure pass, then do the I/O it prescribed — tape
    /// records to disk here, orders upstream as Signals.
    fn tick(&mut self, ctx: &StrategyContext, now: f64) -> (Vec<Signal>, bool) {
        if self.subscribed && now < self.p.end {
            self.record_book(ctx, now);
        }
        let view = arm_view(ctx, &self.p);
        let model = self.fair_p_up(now);
        let out = self.decide(&view, model, now);
        for rec in out.tape {
            tape(rec);
        }
        (out.actions.into_iter().map(to_signal).collect(), out.finished)
    }
}

/// Snapshot the live StrategyContext into the pure view `decide` consumes.
fn arm_view(ctx: &StrategyContext, p: &ArmParams) -> ArmView {
    let top = |token: &str| -> TopOfBook {
        let book = ctx.order_books.get(token);
        TopOfBook {
            bid: book.and_then(|b| b.best_bid()).map(|l| {
                (l.price.to_f64().unwrap_or(0.0), l.size.to_f64().unwrap_or(0.0))
            }),
            ask: book.and_then(|b| b.best_ask()).map(|l| {
                (l.price.to_f64().unwrap_or(1.0), l.size.to_f64().unwrap_or(0.0))
            }),
        }
    };
    let held = |token: &str| -> f64 {
        ctx.positions
            .get(token)
            .map(|pos| pos.size.to_f64().unwrap_or(0.0))
            .unwrap_or(0.0)
    };
    ArmView {
        up: top(&p.token_up),
        dn: top(&p.token_down),
        held_up: held(&p.token_up),
        held_dn: held(&p.token_down),
        position_floor: position_floor(ctx, p),
    }
}

/// Action -> engine Signal. The Decimal conversion lives here so the pure
/// core stays in f64 like the model math. All clip orders are High urgency.
fn to_signal(a: Action) -> Signal {
    match a {
        Action::Subscribe(t) => Signal::Subscribe { token_id: t },
        Action::Unsubscribe(t) => Signal::Unsubscribe { token_id: t },
        Action::Cancel(t) => Signal::Cancel { token_id: t },
        Action::Buy { token, price, size } => Signal::Buy {
            token_id: token,
            price: Decimal::from_f64(price).unwrap_or(Decimal::ONE),
            size: Decimal::from_f64(size).unwrap_or(Decimal::ZERO),
            urgency: Urgency::High,
        },
        Action::Sell { token, price, size } => Signal::Sell {
            token_id: token,
            price: Decimal::from_f64(price).unwrap_or(Decimal::ONE),
            size: Decimal::from_f64(size).unwrap_or(Decimal::ZERO),
            urgency: Urgency::High,
        },
    }
}

impl Updown {
    pub fn new() -> Self {
        Self {
            id: "updown".to_string(),
            arms: std::collections::BTreeMap::new(),
            pending_cleanup: Vec::new(),
            rolls: Vec::new(),
        }
    }

    /// Arm any due successor windows. Gamma hiccups retry every 10s; a
    /// task whose target window has already ended hops forward instead
    /// of dying, so an unattended fleet self-heals.
    fn process_rolls(&mut self, now: f64) {
        if self.rolls.is_empty() {
            return;
        }
        let mut keep = Vec::new();
        for mut task in std::mem::take(&mut self.rolls) {
            if now < task.next_try_at {
                keep.push(task);
                continue;
            }
            if now >= task.next_end {
                if let Some((ns, s, e)) =
                    next_window(&task.next_slug, task.next_start, task.next_end)
                {
                    task.next_slug = ns;
                    task.next_start = s;
                    task.next_end = e;
                    keep.push(task);
                }
                continue;
            }
            match fetch_gamma_tokens(&task.next_slug) {
                Ok((up, down)) => {
                    let mut p = task.params;
                    p.slug = task.next_slug;
                    p.start = task.next_start;
                    p.end = task.next_end;
                    p.token_up = up;
                    p.token_down = down;
                    tracing::info!(slug = %p.slug, size = p.size_usdc, "updown roll — next window armed");
                    tape(serde_json::json!({
                        "t": now, "ev": "roll", "slug": p.slug, "size": p.size_usdc,
                    }));
                    if let Some(mut old) = self.arms.remove(&p.slug) {
                        old.stop_feed();
                    }
                    let slug = p.slug.clone();
                    let mut arm = ArmState::with_params(p);
                    arm.start_feeds();
                    self.arms.insert(slug, arm);
                }
                Err(e) => {
                    tracing::warn!(slug = %task.next_slug, err = %e, "updown roll retry");
                    task.next_try_at = now + 10.0;
                    keep.push(task);
                }
            }
        }
        self.rolls = keep;
    }
}

impl Default for Updown {
    fn default() -> Self {
        Self::new()
    }
}

impl Strategy for Updown {
    fn id(&self) -> &str {
        &self.id
    }

    fn subscriptions(&self) -> Vec<String> {
        self.arms.values().flat_map(|a| a.tokens()).collect()
    }

    fn tick_interval_ms(&self) -> u64 {
        50
    }

    fn on_tick(&mut self, ctx: &StrategyContext) -> Vec<Signal> {
        let now = unix_now();
        let mut signals: Vec<Signal> = self
            .pending_cleanup
            .drain(..)
            .flat_map(|t| {
                [Signal::Cancel { token_id: t.clone() }, Signal::Unsubscribe { token_id: t }]
            })
            .collect();

        let mut finished = Vec::new();
        for (slug, arm) in self.arms.iter_mut() {
            let (mut s, done) = arm.tick(ctx, now);
            signals.append(&mut s);
            if done {
                finished.push(slug.clone());
            }
        }
        for slug in finished {
            if let Some(mut arm) = self.arms.remove(&slug) {
                arm.stop_feed();
                if arm.p.roll {
                    if let Some((ns, s, e)) = next_window(&arm.p.slug, arm.p.start, arm.p.end) {
                        tracing::info!(from = %slug, to = %ns, "updown roll scheduled");
                        self.rolls.push(RollTask {
                            params: arm.p.clone(),
                            next_slug: ns,
                            next_start: s,
                            next_end: e,
                            next_try_at: now,
                        });
                    }
                }
            }
        }
        self.process_rolls(now);

        if signals.is_empty() {
            vec![Signal::Hold]
        } else {
            signals
        }
    }

    fn on_fill(&mut self, fill: &Fill) {
        for arm in self.arms.values_mut() {
            if fill.token_id == arm.p.token_up || fill.token_id == arm.p.token_down {
                let notional = (fill.price * fill.size).to_f64().unwrap_or(0.0);
                // Only buys consume budget; exit sells free shares but the
                // gross-buys number stays (evacuated capital stays out).
                if fill.is_buy {
                    arm.filled_usdc += notional;
                }
                arm.inflight.remove(&fill.token_id);
                tracing::info!(
                    token = %fill.token_id, notional, is_buy = fill.is_buy,
                    slug = %arm.p.slug, total = arm.filled_usdc,
                    "updown fill"
                );
            }
        }
    }

    fn on_shutdown(&mut self) {
        for arm in self.arms.values_mut() {
            arm.stop_feed();
        }
    }

    fn on_command(&mut self, cmd: &serde_json::Value) -> Result<serde_json::Value, String> {
        match cmd.get("action").and_then(|a| a.as_str()) {
            Some("arm") => {
                let p: ArmParams =
                    serde_json::from_value(cmd.clone()).map_err(|e| format!("bad params: {}", e))?;
                if p.kind != "twap" && p.kind != "close_open" {
                    return Err(format!("unknown kind '{}'", p.kind));
                }
                if unix_now() >= p.end {
                    return Err("window already over".to_string());
                }
                let slug = p.slug.clone();
                // Re-arming a slug replaces its arm (fresh budget + feeds).
                if let Some(mut old) = self.arms.remove(&slug) {
                    old.stop_feed();
                }
                let mut arm = ArmState::with_params(p);
                arm.start_feeds();
                self.arms.insert(slug.clone(), arm);
                Ok(serde_json::json!({"armed": slug, "arms": self.arms.len()}))
            }
            Some("disarm") => {
                let target = cmd.get("slug").and_then(|s| s.as_str()).map(str::to_string);
                let slugs: Vec<String> = match &target {
                    Some(s) => self.arms.keys().filter(|k| *k == s).cloned().collect(),
                    None => self.arms.keys().cloned().collect(),
                };
                // Break roll chains too — a disarm means STOP, including the
                // successor a closed window queued up.
                let rolls_before = self.rolls.len();
                match &target {
                    Some(s) => self
                        .rolls
                        .retain(|t| t.next_slug != *s && t.params.slug != *s),
                    None => self.rolls.clear(),
                }
                for slug in &slugs {
                    if let Some(mut arm) = self.arms.remove(slug) {
                        arm.stop_feed();
                        self.pending_cleanup.extend(arm.tokens());
                    }
                }
                Ok(serde_json::json!({
                    "disarmed": slugs, "arms": self.arms.len(),
                    "rolls_cancelled": rolls_before - self.rolls.len(),
                    "cleanup": "next tick",
                }))
            }
            Some("status") => {
                let arms: serde_json::Map<String, serde_json::Value> = self
                    .arms
                    .iter()
                    .map(|(slug, a)| {
                        let o = a.oracle.lock().unwrap();
                        (slug.clone(), serde_json::json!({
                            "filled_usdc": a.filled_usdc,
                            "roll": a.p.roll,
                            "eval": a.last_eval,
                            "oracle": {
                                "samples": o.samples.len(),
                                "last_chainlink": o.last_chainlink,
                                "last_updated_at": o.last_updated_at,
                                "last_poll_at": o.last_poll_at,
                                "err": o.last_err,
                            },
                        }))
                    })
                    .collect();
                let rolls: Vec<&str> =
                    self.rolls.iter().map(|t| t.next_slug.as_str()).collect();
                Ok(serde_json::json!({
                    "arms": arms, "count": self.arms.len(), "pending_rolls": rolls,
                }))
            }
            _ => Err("unknown action (arm | disarm | status)".to_string()),
        }
    }
}

struct FeedUpdate {
    spot: f64,
    per_min: Vec<(i64, f64)>,
    candle_open: Option<f64>,
    closes: Vec<f64>,
}

fn poll_binance(
    client: &reqwest::blocking::Client,
    symbol: &str,
    kind: &str,
    start: f64,
    now: f64,
) -> Result<FeedUpdate, String> {
    let spot: f64 = {
        let v: serde_json::Value = client
            .get(format!("{}/api/v3/ticker/price", BINANCE_DATA))
            .query(&[("symbol", symbol)])
            .send()
            .and_then(|r| r.error_for_status())
            .map_err(|e| format!("ticker: {}", e))?
            .json()
            .map_err(|e| format!("ticker json: {}", e))?;
        v["price"]
            .as_str()
            .and_then(|s| s.parse().ok())
            .ok_or("ticker price parse")?
    };

    let mut per_min = Vec::new();
    let mut candle_open = None;
    let mut closes = Vec::new();
    if kind == "twap" {
        // Reach back 45min so the regime autocorr has real history — a
        // window-only fetch left rho pinned at 0 and the chop gate dead.
        let start_ms = ((start as i64 - 2700) * 1000).to_string();
        let v: serde_json::Value = client
            .get(format!("{}/api/v3/klines", BINANCE_DATA))
            .query(&[("symbol", symbol), ("interval", "1m"), ("startTime", &start_ms), ("limit", "500")])
            .send()
            .and_then(|r| r.error_for_status())
            .map_err(|e| format!("klines: {}", e))?
            .json()
            .map_err(|e| format!("klines json: {}", e))?;
        for k in v.as_array().unwrap_or(&Vec::new()) {
            let t = k[0].as_i64().unwrap_or(0) / 1000;
            let o: f64 = k[1].as_str().and_then(|s| s.parse().ok()).unwrap_or(0.0);
            let c: f64 = k[4].as_str().and_then(|s| s.parse().ok()).unwrap_or(0.0);
            if o > 0.0 && c > 0.0 {
                per_min.push((t, (o + c) / 2.0));
                closes.push(c);
            }
        }
    } else {
        // Fast-vol input for close_open markets (twap reuses its klines).
        let v: serde_json::Value = client
            .get(format!("{}/api/v3/klines", BINANCE_DATA))
            .query(&[("symbol", symbol), ("interval", "1m"), ("limit", "30")])
            .send()
            .and_then(|r| r.error_for_status())
            .map_err(|e| format!("vol klines: {}", e))?
            .json()
            .map_err(|e| format!("vol klines json: {}", e))?;
        for k in v.as_array().unwrap_or(&Vec::new()) {
            if let Some(c) = k[4].as_str().and_then(|s| s.parse::<f64>().ok()) {
                closes.push(c);
            }
        }
    }
    if kind != "twap" && now >= start {
        let start_ms = ((start as i64) * 1000).to_string();
        let v: serde_json::Value = client
            .get(format!("{}/api/v3/klines", BINANCE_DATA))
            .query(&[("symbol", symbol), ("interval", "1h"), ("startTime", &start_ms), ("limit", "1")])
            .send()
            .and_then(|r| r.error_for_status())
            .map_err(|e| format!("1h kline: {}", e))?
            .json()
            .map_err(|e| format!("1h kline json: {}", e))?;
        candle_open = v
            .as_array()
            .and_then(|a| a.first())
            .and_then(|k| k[1].as_str())
            .and_then(|s| s.parse().ok());
    }

    Ok(FeedUpdate { spot, per_min, candle_open, closes })
}

/// Notional the position tracker proves is already spent on an arm's pair.
/// on_fill misses taker fills (reconcile catches them ~5s later), so this
/// is the authoritative budget floor: shares held x entry price, with the
/// live ask as the honest estimate while reconcile-seeded avg is still 0.
fn position_floor(ctx: &StrategyContext, p: &ArmParams) -> f64 {
    [&p.token_up, &p.token_down]
        .iter()
        .filter_map(|t| ctx.positions.get(*t).map(|pos| (*t, pos)))
        .map(|(t, pos)| {
            let size = pos.size.to_f64().unwrap_or(0.0);
            let avg = pos.avg_entry_price.to_f64().unwrap_or(0.0);
            let fallback = ctx
                .order_books
                .get(t)
                .and_then(|b| b.best_ask())
                .and_then(|l| l.price.to_f64())
                .unwrap_or(p.max_price);
            size * if avg > 0.0 { avg } else { fallback }
        })
        .sum()
}

/// Signed safety for one side: banked evidence divided by the residual
/// noise cushion, positive only when the banked margin points the side's
/// way. safety >= 1 on the fired side ≈ banked_decided.
fn side_safety(is_up: bool, banked_margin_bp: f64, cushion_bp: f64) -> f64 {
    let signed = if is_up { banked_margin_bp } else { -banked_margin_bp };
    signed / cushion_bp.max(1e-9)
}

/// R9 entry gate: the FIRST clip of a window needs banked evidence, not a
/// clock reading — both 2026-08-23 post-brake losses entered at safety
/// < 0.25 while the 50% clock said go. Applies to twap arms only
/// (close_open has no banked mass to measure), and only until the first
/// clip lands; position management after entry belongs to the brakes.
fn safety_gate_blocks(theta: f64, kind: &str, no_clips_yet: bool, safety: f64) -> bool {
    theta > 0.0 && kind == "twap" && no_clips_yet && safety < theta
}

/// Book-distrust brake predicate: a book handing over more than
/// `threshold` net (BOOK_DISTRUST_NET live) is pricing in something the
/// model missed, unless the TWAP math itself has already decided the
/// window. The threshold is a parameter only so replay can express the
/// pre-brake policy — live arms always pass the constant.
fn distrust_blocks(net: f64, threshold: f64, banked_decided: bool) -> bool {
    net > threshold && !banked_decided
}

/// No-averaging-down brake predicate: the ask dropping more than `tol`
/// (AVG_DOWN_TOL live) below our last clip on this token means the market
/// is repricing against the thesis, unless the TWAP math has already
/// decided.
fn avg_down_blocks(ask: f64, last_clip_ask: Option<f64>, tol: f64, banked_decided: bool) -> bool {
    match last_clip_ask {
        Some(prev) => ask < prev - tol && !banked_decided,
        None => false,
    }
}

/// Full-budget unlock: absolute seconds left, not window fraction — a 15m
/// window's 60%-elapsed mark still leaves 6 minutes of risk on the table.
fn budget_unlocked(now: f64, end: f64, late_rem_s: f64, banked_decided: bool) -> bool {
    (end - now) <= late_rem_s || banked_decided
}

/// Book recorder cadence: 5s normally, 1s inside the final 90s before
/// settlement — manipulation-signature research (R4) needs the book
/// fine-grained right where it matters and can afford coarse elsewhere.
fn book_sample_due(now: f64, last: f64, end: f64) -> bool {
    let interval = if end - now <= 90.0 { 1.0 } else { 5.0 };
    now - last >= interval
}

/// Realized sigma (bp per 1m bar) over the last `window` returns.
fn trailing_sigma_bp(closes: &[f64], window: usize) -> f64 {
    let n = closes.len().min(window + 1);
    if n < 4 {
        return 0.0;
    }
    let rets: Vec<f64> =
        closes[closes.len() - n..].windows(2).map(|w| (w[1] / w[0]).ln()).collect();
    let mu = rets.iter().sum::<f64>() / rets.len() as f64;
    let var = rets.iter().map(|r| (r - mu).powi(2)).sum::<f64>() / (rets.len() - 1) as f64;
    var.sqrt() * 1e4
}

/// Roll chains clone params forever, so the arm-time sigma goes stale within
/// hours — the live slow estimate supersedes it once enough history printed.
fn vol_floor_bp(slow_bp: f64, samples: usize, param_bp: f64) -> f64 {
    if samples >= SIGMA_SLOW_MIN && slow_bp > 0.0 {
        slow_bp
    } else {
        param_bp
    }
}

/// Lag-1 autocorrelation of log-returns over the last `n` closes.
/// pub(crate): the replay harness's full-mode feed reconstruction needs
/// the exact same regime signal the live feed thread computes.
pub(crate) fn lag1_autocorr(closes: &[f64], n: usize) -> f64 {
    let m = closes.len().min(n + 1);
    if m < 10 {
        return 0.0;
    }
    let rets: Vec<f64> = closes[closes.len() - m..]
        .windows(2)
        .map(|w| (w[1] / w[0]).ln())
        .collect();
    let mu = rets.iter().sum::<f64>() / rets.len() as f64;
    let var = rets.iter().map(|r| (r - mu).powi(2)).sum::<f64>();
    if var <= 0.0 {
        return 0.0;
    }
    let cov: f64 = rets.windows(2).map(|w| (w[0] - mu) * (w[1] - mu)).sum();
    cov / var
}

fn unix_now() -> f64 {
    std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map(|d| d.as_secs_f64())
        .unwrap_or(0.0)
}

/// Abramowitz & Stegun 7.1.26 — |error| < 1.5e-7, plenty for pricing.
fn erf(x: f64) -> f64 {
    let sign = if x < 0.0 { -1.0 } else { 1.0 };
    let x = x.abs();
    let t = 1.0 / (1.0 + 0.3275911 * x);
    let y = 1.0
        - (((((1.061405429 * t - 1.453152027) * t) + 1.421413741) * t - 0.284496736) * t
            + 0.254829592)
            * t
            * (-x * x).exp();
    sign * y
}

fn norm_cdf(x: f64) -> f64 {
    0.5 * (1.0 + erf(x / std::f64::consts::SQRT_2))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn norm_cdf_sane() {
        assert!((norm_cdf(0.0) - 0.5).abs() < 1e-7);
        assert!((norm_cdf(1.96) - 0.975).abs() < 1e-3);
        assert!((norm_cdf(-1.96) - 0.025).abs() < 1e-3);
    }

    fn params(slug: &str) -> ArmParams {
        serde_json::from_value(serde_json::json!({
            "slug": slug, "kind": "twap", "symbol": "BTCUSDT",
            "token_up": format!("{}-u", slug), "token_down": format!("{}-d", slug),
            "start": 600.0, "end": 1500.0,
            "sigma_bp_per_min": 3.0, "fee_rate": 0.07, "size_usdc": 100.0,
        }))
        .unwrap()
    }

    #[test]
    fn arm_params_defaults() {
        let p = params("s");
        assert_eq!(p.min_edge, 0.015);
        assert_eq!(p.max_price, 0.985);
        assert_eq!(p.quiesce_secs, 20.0);
        assert_eq!(p.basis_guard_bp, 3.0);
        assert_eq!(p.min_fair, 0.97);
        assert_eq!(p.min_elapsed_frac, 0.0);
        assert_eq!(p.clip_usdc, 25.0);
        assert_eq!(p.early_frac, 0.2);
        assert_eq!(p.early_min_edge, 0.08);
        assert_eq!(p.late_frac, 0.6);
        assert_eq!(p.late_rem_s, 120.0);
        assert_eq!(p.rho_block, -0.25);
    }

    #[test]
    fn distrust_brake_blocks_outsized_net_unless_banked() {
        let t = BOOK_DISTRUST_NET;
        assert!(distrust_blocks(0.16, t, false));
        assert!(!distrust_blocks(0.16, t, true), "banked-decided is exempt");
        assert!(!distrust_blocks(0.15, t, false), "at the threshold, not over it");
        assert!(!distrust_blocks(0.05, t, false));
    }

    #[test]
    fn avg_down_brake_blocks_cheapening_ask_unless_banked() {
        let t = AVG_DOWN_TOL;
        assert!(avg_down_blocks(0.50, Some(0.53), t, false), "3c cheaper clears tolerance");
        assert!(!avg_down_blocks(0.50, Some(0.53), t, true), "banked-decided is exempt");
        assert!(!avg_down_blocks(0.52, Some(0.53), t, false), "within tolerance");
        assert!(!avg_down_blocks(0.55, Some(0.53), t, false), "richer ask, not cheaper");
        assert!(!avg_down_blocks(0.50, None, t, false), "no prior clip on this token");
    }

    // --- decide()-level tests: the pure core makes the full firing policy
    // testable without a StrategyContext, which is half the point of the
    // extraction (the other half is replay running this exact path).

    fn armed(p: ArmParams) -> ArmState {
        let mut a = ArmState::with_params(p);
        a.subscribed = true;
        a
    }

    fn locked_up_model() -> ModelEval {
        ModelEval {
            p_up: 1.0, sig_bp: 3.0, banked_decided: true, flip_proof: false,
            rho: 0.0, margin_bp: 20.0, banked_margin_bp: 15.0, cushion_bp: 5.0,
            guard_bp: 3.0,
        }
    }

    fn view_with_up_ask(ask: f64, size: f64) -> ArmView {
        ArmView {
            up: TopOfBook { bid: None, ask: Some((ask, size)) },
            ..ArmView::default()
        }
    }

    fn buys(out: &DecideOut) -> Vec<&Action> {
        out.actions
            .iter()
            .filter(|a| matches!(a, Action::Buy { .. }))
            .collect()
    }

    #[test]
    fn decide_fires_a_clip_when_all_gates_hold() {
        let mut arm = armed(params("s"));
        // rem=100s <= late_rem 120 -> unlocked; before quiesce (end-20).
        let out = arm.decide(&view_with_up_ask(0.94, 500.0), Ok(locked_up_model()), 1400.0);
        let b = buys(&out);
        assert_eq!(b.len(), 1);
        let Action::Buy { token, price, size } = b[0] else { unreachable!() };
        assert_eq!(token, "s-u");
        assert_eq!(*price, 0.94);
        assert_eq!(*size, 26.0, "clip_usdc 25 / 0.94 floored");
        assert!(out.tape.iter().any(|r| r["ev"] == "fire" && r["mode"] == "safe"));
        assert!(arm.inflight.contains_key("s-u"), "one order in flight");
    }

    #[test]
    fn decide_inflight_and_cooldown_block_the_next_clip() {
        let mut arm = armed(params("s"));
        arm.decide(&view_with_up_ask(0.94, 500.0), Ok(locked_up_model()), 1400.0);
        let out = arm.decide(&view_with_up_ask(0.94, 500.0), Ok(locked_up_model()), 1400.1);
        assert!(buys(&out).is_empty());
    }

    #[test]
    fn decide_distrust_brake_blocks_undecided_moonshot() {
        let mut arm = armed(params("s"));
        let m = ModelEval { p_up: 0.99, banked_decided: false, ..locked_up_model() };
        // net ~0.45 into an undecided model: the -$370 signature.
        let out = arm.decide(&view_with_up_ask(0.50, 500.0), Ok(m), 1400.0);
        assert!(buys(&out).is_empty(), "distrust brake must hold");
        let eval = arm.last_eval.as_ref().unwrap();
        assert_eq!(eval["sides"][0]["brake"], "distrust");
    }

    #[test]
    fn decide_replay_tunables_express_the_pre_brake_policy() {
        // The A/B harness reproduces recorded pre-brake nights by lifting
        // the constants — live arms can never reach this path.
        let mut arm = armed(params("s"));
        arm.tunables.distrust_net = f64::INFINITY;
        let m = ModelEval { p_up: 0.99, banked_decided: false, ..locked_up_model() };
        let out = arm.decide(&view_with_up_ask(0.50, 500.0), Ok(m), 1400.0);
        assert_eq!(buys(&out).len(), 1, "old policy fires where the brake now holds");
    }

    #[test]
    fn decide_clock_gate_holds_before_min_elapsed() {
        let mut p = params("s");
        p.min_elapsed_frac = 0.5;
        let mut arm = armed(p);
        let out = arm.decide(&view_with_up_ask(0.94, 500.0), Ok(locked_up_model()), 960.0);
        assert!(out.actions.is_empty());
        assert_eq!(arm.last_eval.as_ref().unwrap()["state"], "gated");
    }

    #[test]
    fn decide_quiesce_pulls_orders_without_flip_proof() {
        let mut arm = armed(params("s"));
        let out = arm.decide(&view_with_up_ask(0.94, 500.0), Ok(locked_up_model()), 1490.0);
        assert!(buys(&out).is_empty(), "no new buys through quiesce unless flip-proof");
        assert_eq!(
            out.actions
                .iter()
                .filter(|a| matches!(a, Action::Cancel(_)))
                .count(),
            2
        );
        assert_eq!(arm.last_eval.as_ref().unwrap()["state"], "quiesce");
    }

    #[test]
    fn side_safety_signs_by_side_and_floors_cushion() {
        assert!((side_safety(true, 6.0, 12.0) - 0.5).abs() < 1e-9);
        assert!((side_safety(false, 6.0, 12.0) + 0.5).abs() < 1e-9, "banked-up hurts down");
        assert!((side_safety(false, -6.0, 12.0) - 0.5).abs() < 1e-9);
        assert!(side_safety(true, 5.0, 0.0) > 1e6, "zero cushion never divides by zero");
    }

    #[test]
    fn safety_gate_blocks_only_first_twap_clip_when_armed() {
        assert!(safety_gate_blocks(0.3, "twap", true, 0.2));
        assert!(!safety_gate_blocks(0.3, "twap", true, 0.31), "evidence clears it");
        assert!(!safety_gate_blocks(0.0, "twap", true, 0.0), "theta 0 = disabled");
        assert!(!safety_gate_blocks(0.3, "twap", false, 0.0), "post-entry belongs to brakes");
        assert!(!safety_gate_blocks(0.3, "close_open", true, 0.0), "no banked mass to measure");
    }

    #[test]
    fn decide_theta_gates_first_clip_until_banked_evidence() {
        let mut p = params("s");
        p.theta = 0.3;
        let mut arm = armed(p);
        // banked_decided model but banked/cushion say safety 0.1: the -$370
        // signature (model certain, evidence absent).
        let weak = ModelEval {
            banked_margin_bp: 0.5, cushion_bp: 5.0, banked_decided: false,
            ..locked_up_model()
        };
        let out = arm.decide(&view_with_up_ask(0.94, 500.0), Ok(weak), 1400.0);
        assert!(buys(&out).is_empty());
        assert_eq!(arm.last_eval.as_ref().unwrap()["sides"][0]["brake"], "safety");
        // Evidence arrives: same tick shape now fires.
        let strong = ModelEval { banked_margin_bp: 2.0, cushion_bp: 5.0, ..locked_up_model() };
        let out = arm.decide(&view_with_up_ask(0.94, 500.0), Ok(strong), 1400.1);
        assert_eq!(buys(&out).len(), 1);
    }

    #[test]
    fn decide_theta_ignores_wrong_sign_banked_mass() {
        let mut p = params("s");
        p.theta = 0.3;
        let mut arm = armed(p);
        // Big banked margin pointing DOWN while the model wants UP.
        let contra = ModelEval {
            banked_margin_bp: -10.0, cushion_bp: 5.0, banked_decided: false,
            ..locked_up_model()
        };
        let out = arm.decide(&view_with_up_ask(0.94, 500.0), Ok(contra), 1400.0);
        assert!(buys(&out).is_empty(), "|banked| alone is not evidence for this side");
    }

    #[test]
    fn decide_brake_latch_holds_for_the_window() {
        let mut arm = armed(params("s"));
        let undecided = ModelEval { p_up: 0.99, banked_decided: false, ..locked_up_model() };
        // Trip distrust (net ~0.45), then present a sane-looking book: the
        // audit's slide-through. Latch must still block.
        arm.decide(&view_with_up_ask(0.50, 500.0), Ok(undecided.clone()), 1400.0);
        let out = arm.decide(&view_with_up_ask(0.94, 500.0), Ok(undecided), 1400.1);
        assert!(buys(&out).is_empty());
        assert_eq!(arm.last_eval.as_ref().unwrap()["sides"][0]["brake"], "latched");
        // banked_decided math still trades through the latch.
        let out = arm.decide(&view_with_up_ask(0.94, 500.0), Ok(locked_up_model()), 1400.2);
        assert_eq!(buys(&out).len(), 1);
    }

    #[test]
    fn late_rem_s_default_matches_old_late_frac_on_a_300s_window() {
        // 300s window: old 60% mark and the new 120s-remaining default land
        // on the same instant.
        let (start, end) = (0.0, 300.0);
        let old_unlock_now = start + (end - start) * 0.6;
        assert!(budget_unlocked(old_unlock_now, end, d_late_rem(), false));
        assert!(!budget_unlocked(old_unlock_now - 1.0, end, d_late_rem(), false));
    }

    #[test]
    fn late_rem_s_unlocks_a_900s_window_at_two_minutes_left_not_six() {
        let end = 900.0;
        // old late_frac 0.6 would have unlocked at rem=360s (6min); the
        // absolute-time brake holds until rem=120s (2min).
        assert!(!budget_unlocked(end - 360.0, end, d_late_rem(), false));
        assert!(!budget_unlocked(end - 121.0, end, d_late_rem(), false));
        assert!(budget_unlocked(end - 120.0, end, d_late_rem(), false));
        assert!(budget_unlocked(end - 119.0, end, d_late_rem(), false));
    }

    #[test]
    fn late_rem_s_banked_decided_unlocks_regardless_of_time() {
        assert!(budget_unlocked(0.0, 900.0, 120.0, true));
    }

    #[test]
    fn book_sample_cadence_tightens_in_the_final_90s() {
        let end = 1000.0;
        // Mid-window: 5s cadence.
        assert!(!book_sample_due(603.0, 600.0, end), "3s in — not due yet");
        assert!(book_sample_due(605.0, 600.0, end), "5s in — due");
        // Inside the final 90s: 1s cadence.
        assert!(book_sample_due(end - 89.0, end - 90.0, end), "1s in — due");
        assert!(!book_sample_due(end - 89.5, end - 90.0, end), "0.5s in — not due yet");
    }

    #[test]
    fn trailing_sigma_needs_history_and_scales_with_moves() {
        assert_eq!(trailing_sigma_bp(&[100.0, 100.0, 100.0], 45), 0.0);
        let flat: Vec<f64> = vec![100.0; 40];
        assert!(trailing_sigma_bp(&flat, 45) < 1e-9);
        // ~10bp alternating moves → sigma near 10bp/min.
        let choppy: Vec<f64> =
            (0..40).map(|i| if i % 2 == 0 { 100.0 } else { 100.1 }).collect();
        let s = trailing_sigma_bp(&choppy, 45);
        assert!(s > 8.0 && s < 12.0, "got {s}");
    }

    #[test]
    fn live_slow_sigma_supersedes_stale_param_floor() {
        // Enough history: the live estimate wins in BOTH directions —
        // that's the point (stale-high floors under-fire, stale-low
        // floors overtrust).
        assert_eq!(vol_floor_bp(9.0, 40, 3.0), 9.0);
        assert_eq!(vol_floor_bp(2.0, 40, 8.0), 2.0);
        // Thin history or dead feed: fall back to the arm-time param.
        assert_eq!(vol_floor_bp(9.0, 10, 3.0), 3.0);
        assert_eq!(vol_floor_bp(0.0, 40, 3.0), 3.0);
    }

    fn armed_with_feed(banked_px: f64, spot: f64) -> (ArmState, f64) {
        let arm = ArmState::with_params(params("s"));
        let now = 1400.0;
        {
            let mut f = arm.feed.lock().unwrap();
            f.spot = spot;
            f.spot_ts = now;
            f.per_min.insert(540, 100.0); // range-start reference
            for t in (600..1380).step_by(60) {
                f.per_min.insert(t, banked_px);
            }
        }
        (arm, now)
    }

    #[test]
    fn twap_banked_above_ref_is_locked_up() {
        let (arm, now) = armed_with_feed(101.0, 101.0); // +100bp margin
        let m = arm.fair_p_up(now).unwrap();
        assert!(m.p_up > 0.999);
        assert!(m.banked_decided, "a +100bp banked margin survives full reversion");
    }

    #[test]
    fn twap_banked_below_ref_is_locked_down() {
        let (arm, now) = armed_with_feed(99.0, 99.0);
        assert!(arm.fair_p_up(now).unwrap().p_up < 0.001);
    }

    #[test]
    fn decided_but_pushable_is_not_flip_proof() {
        // +4.4bp banked contribution: survives natural reversion (decided)
        // but an adversarial 25bp push over the remaining window could
        // still flip it — no late-window flip clips.
        let (arm, now) = armed_with_feed(100.05, 100.05);
        let m = arm.fair_p_up(now).unwrap();
        assert!(m.banked_decided);
        assert!(!m.flip_proof);
        // +100bp is beyond any push.
        let (arm2, now2) = armed_with_feed(101.0, 101.0);
        assert!(arm2.fair_p_up(now2).unwrap().flip_proof);
    }

    #[test]
    fn thin_banked_margin_is_not_decided() {
        // +3.5bp banked contribution sits inside basis + vol cushion:
        // clears the basis guard for pricing, but is NOT safe-bet material
        let (arm, now) = armed_with_feed(100.035, 100.035);
        assert!(!arm.fair_p_up(now).unwrap().banked_decided);
    }

    #[test]
    fn autocorr_reads_reverting_tape() {
        let alternating: Vec<f64> =
            (0..40).map(|i| if i % 2 == 0 { 100.0 } else { 100.5 }).collect();
        assert!(lag1_autocorr(&alternating, 60) < -0.8);
        let trending: Vec<f64> = (0..40).map(|i| 100.0 + i as f64 * 0.5).collect();
        assert!(lag1_autocorr(&trending, 60) > -0.2);
        assert_eq!(lag1_autocorr(&[100.0; 5], 60), 0.0); // too short
    }

    #[test]
    fn fast_vol_ratchets_sigma_up_and_softens_p() {
        // +6bp margin: locked under calm vol, genuinely uncertain in a storm
        let (arm, now) = armed_with_feed(100.06, 100.06);
        let calm = arm.fair_p_up(now).unwrap();
        assert!((calm.sig_bp - 3.0).abs() < 1e-9); // no closes -> arm sigma
        assert!(calm.p_up > 0.99);
        {
            // violent recent tape: ±60bp swings per minute
            let mut f = arm.feed.lock().unwrap();
            f.closes = (0..14).map(|i| if i % 2 == 0 { 100.0 } else { 100.6 }).collect();
        }
        let storm = arm.fair_p_up(now).unwrap();
        assert!(storm.sig_bp > 30.0, "sig {}", storm.sig_bp);
        assert!(storm.p_up < calm.p_up - 0.01, "storm {} vs calm {}", storm.p_up, calm.p_up);
    }

    #[test]
    fn twap_thin_margin_trips_basis_guard() {
        let (arm, now) = armed_with_feed(100.001, 100.001); // ~0.1bp
        let err = arm.fair_p_up(now).unwrap_err();
        assert!(err.contains("basis guard"), "{}", err);
    }

    #[test]
    fn eval_model_explicit_effective_guard_can_exceed_the_static_param() {
        // margin_bp ~5bp here — clears the 3bp arm-time param comfortably.
        let (arm, now) = armed_with_feed(100.05, 100.05);
        let f = arm.feed.lock().unwrap();
        assert!(
            eval_model(&arm.p, &f, now, arm.p.basis_guard_bp).is_ok(),
            "static param alone passes"
        );
        // A higher effective guard (as a live-raised oracle read would
        // produce) gates the exact same margin — proves eval_model reads
        // the explicit argument, not p.basis_guard_bp directly.
        let err = eval_model(&arm.p, &f, now, 8.0).unwrap_err();
        assert!(err.contains("basis guard"), "{}", err);
        assert!(err.contains("8.0"), "reason string must print the effective value: {}", err);
    }

    #[test]
    fn eval_model_effective_equal_to_param_matches_old_behavior() {
        // Mechanical check that threading effective_guard_bp through
        // doesn't change anything when effective == param — the case every
        // live arm hits until the oracle poller has 30+ samples.
        let (arm, now) = armed_with_feed(100.05, 100.05);
        let f = arm.feed.lock().unwrap();
        let m = eval_model(&arm.p, &f, now, arm.p.basis_guard_bp).unwrap();
        assert_eq!(m.guard_bp, arm.p.basis_guard_bp);
    }

    #[test]
    fn fair_p_up_uses_live_raised_guard_from_oracle_samples() {
        let (arm, now) = armed_with_feed(100.05, 100.05); // ~5bp margin
        assert!(arm.fair_p_up(now).is_ok(), "empty oracle window: static 3bp param alone passes");
        {
            let mut o = arm.oracle.lock().unwrap();
            for _ in 0..40 {
                // p95 of a constant 9bp window is 9bp — above the 5bp margin.
                updown_oracle::push_sample(&mut o.samples, 9.0);
            }
        }
        let err = arm.fair_p_up(now).unwrap_err();
        assert!(err.contains("basis guard"), "{}", err);
    }

    #[test]
    fn twap_stale_feed_refuses() {
        let (arm, _) = armed_with_feed(101.0, 101.0);
        let err = arm.fair_p_up(1400.0 + MAX_SPOT_AGE_S + 1.0).unwrap_err();
        assert!(err.contains("stale"), "{}", err);
    }

    #[test]
    fn command_rejects_unknown_action() {
        let mut s = Updown::new();
        assert!(s.on_command(&serde_json::json!({"action": "explode"})).is_err());
    }

    #[test]
    fn arm_rejects_finished_window() {
        let mut s = Updown::new();
        let res = s.on_command(&serde_json::json!({
            "action": "arm", "slug": "s", "kind": "twap", "symbol": "BTCUSDT",
            "token_up": "1", "token_down": "2",
            "start": 0.0, "end": 900.0,
            "sigma_bp_per_min": 2.5, "fee_rate": 0.07, "size_usdc": 100.0,
        }));
        assert!(res.is_err());
        assert!(s.arms.is_empty());
    }

    #[test]
    fn multi_arm_status_and_selective_disarm() {
        let mut s = Updown::new();
        // Insert arms directly — arm() would spawn live feed threads.
        s.arms.insert("a".into(), ArmState::with_params(params("a")));
        s.arms.insert("b".into(), ArmState::with_params(params("b")));
        assert_eq!(s.subscriptions().len(), 4);

        let st = s.on_command(&serde_json::json!({"action": "status"})).unwrap();
        assert_eq!(st["count"], 2);

        let d = s
            .on_command(&serde_json::json!({"action": "disarm", "slug": "a"}))
            .unwrap();
        assert_eq!(d["arms"], 1);
        assert_eq!(s.pending_cleanup.len(), 2);
        assert!(s.arms.contains_key("b"));

        let d2 = s.on_command(&serde_json::json!({"action": "disarm"})).unwrap();
        assert_eq!(d2["arms"], 0);
    }

    #[test]
    fn next_window_rolls_the_series() {
        let (slug, s, e) =
            next_window("btc-updown-5m-1787442000", 1787442000.0, 1787442300.0).unwrap();
        assert_eq!(slug, "btc-updown-5m-1787442300");
        assert_eq!(s, 1787442300.0);
        assert_eq!(e, 1787442600.0);
        // Slug tail must match start — anything else is not a rolling series.
        assert!(next_window("some-market", 0.0, 300.0).is_none());
        assert!(next_window("btc-updown-5m-999", 1787442000.0, 1787442300.0).is_none());
    }

    #[test]
    fn gamma_tokens_parse_by_outcome_label() {
        let body = serde_json::json!([{
            "slug": "btc-updown-5m-1787443200",
            "outcomes": "[\"Up\", \"Down\"]",
            "clobTokenIds": "[\"111\", \"222\"]",
        }]);
        assert_eq!(parse_gamma_tokens(&body).unwrap(), ("111".into(), "222".into()));
        // Reversed order must still land on the right sides.
        let rev = serde_json::json!([{
            "outcomes": "[\"Down\", \"Up\"]",
            "clobTokenIds": "[\"111\", \"222\"]",
        }]);
        assert_eq!(parse_gamma_tokens(&rev).unwrap(), ("222".into(), "111".into()));
        assert!(parse_gamma_tokens(&serde_json::json!([])).is_err());
    }

    #[test]
    fn disarm_breaks_roll_chain() {
        let mut s = Updown::new();
        s.rolls.push(RollTask {
            params: params("btc-updown-5m-1787442000"),
            next_slug: "btc-updown-5m-1787442300".into(),
            next_start: 1787442300.0,
            next_end: 1787442600.0,
            next_try_at: 0.0,
        });
        let d = s.on_command(&serde_json::json!({"action": "disarm"})).unwrap();
        assert_eq!(d["rolls_cancelled"], 1);
        assert!(s.rolls.is_empty());
    }
}
