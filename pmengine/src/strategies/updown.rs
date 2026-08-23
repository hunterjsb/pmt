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

// The replay harness (src/replay.rs) drives this module's decision core
// directly, so it must be visible crate-wide. The line below tells the
// registry generator to emit `pub(crate) mod updown;` in mod.rs.
// pmstrat: pub(crate)

use crate::position::Fill;
use crate::strategies::updown_model::{
    append_jsonl, avg_down_blocks, budget_unlocked, book_sample_due, distrust_blocks, eval_model,
    lag1_autocorr, maker_bid_price, pay_up_limit, safety_gate_blocks, settle_tw_secs, shape_klines,
    side_safety,
    FeedState, GateReason, ModelEval, Tunables, BINANCE_DATA, EV_BOOK, EV_CLEANUP, EV_EVAL,
    EV_EXIT, EV_FIRE, EV_GATED, EV_ROLL, KLINE_LOOKBACK_S,
};
#[cfg(test)]
use crate::strategies::updown_model::MAX_SPOT_AGE_S;
use crate::strategies::updown_oracle;
use crate::strategies::updown_rtds::{self, RtdsHub, RtdsSub};
use crate::strategies::updown_state::{
    plan_recovery, spawn_unmanaged_check, ArmStore, ArmsState, RollRecord,
};
use crate::strategy::{Signal, Strategy, StrategyContext, Urgency};
use rust_decimal::prelude::{FromPrimitive, ToPrimitive};
use rust_decimal::Decimal;
use serde::{Deserialize, Serialize};
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Arc, Mutex};

/// Exit rule (the -$318 lesson: a 99%-fair entry died with no hands to act
/// as it flipped). Dump a held side when its fair collapses below
/// EXIT_FAIR — but only into a bid within EXIT_MAX_DISCOUNT of fair;
/// selling below that donates to panic.
const EXIT_FAIR: f64 = 0.40;
const EXIT_MAX_DISCOUNT: f64 = 0.08;
/// One order in flight per token; assume dead if no fill inside this window.
/// Must outlive the engine's position-reconcile cadence (every 30 engine
/// ticks — ~1.5s at the launcher's 50ms tick): taker fills
/// are often MISSED by the realtime fill path and only show up via
/// reconcile (proven live), so freeing budget sooner buys fills twice.
const INFLIGHT_TTL_S: f64 = 12.0;
/// Speculative clips still need the model leaning clearly one way.
const EARLY_MIN_FAIR: f64 = 0.55;

// pub(crate), never plain pub, on purpose: the registry generator registers
// the first `pub struct` in the file, which must be Updown.
//
// Serialize as well as Deserialize: these params ARE the durable arm state
// (updown_state.rs) — everything a restart needs to rebuild a live arm,
// runtime-fetched token ids included, is already in here.
#[derive(Debug, Clone, Deserialize, Serialize)]
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
    /// Full budget unlocks with this many seconds left, regardless of
    /// banked-decidedness — absolute risk time, not window fraction: 60%
    /// elapsed leaves 2min on a 5m window but 6min on a 15m one.
    #[serde(default = "d_late_rem")]
    pub late_rem_s: f64,
    /// Lag-1 autocorrelation of 1m returns below which the tape counts as
    /// mean-reverting chop: speculative clips are disabled entirely.
    #[serde(default = "d_rho_block")]
    pub rho_block: f64,
    /// Fill-chasing fix (2026-08-23 audit: 32% of intended taker notional
    /// never crossed — the book ticks away between decision and arrival,
    /// and each re-quote chases it upward). A clip's limit may sit this
    /// many cents ABOVE the decision ask, funded only by surplus edge over
    /// the edge floor — a marketable limit fills at the book, so the
    /// buffer costs nothing unless the book actually moved. 0 = off.
    #[serde(default)]
    pub pay_up_max: f64,
    /// R6 tail honesty: the model's fair is capped here unless the TWAP is
    /// flip-proof — every symbol jumps >3σ roughly hourly, so a Gaussian
    /// p_up of 0.99+ is fiction exactly where being wrong costs 100%. With
    /// min_edge 1.5¢ a cap of 0.98 makes ~0.945 the highest ask a
    /// non-flip-proof clip can pay. 1.0 = off (wallet calibration check:
    /// stated fair ≥0.95 realizes ~92%).
    #[serde(default = "d_p_cap")]
    pub p_cap: f64,
    /// R9 safety gate: the FIRST clip of a window requires
    /// safety = signed_banked_bp / cushion_bp >= theta on the fired side.
    /// 0 disables (clock-gate-only entry, the pre-R9 behavior). θ=1 is
    /// equivalent to requiring banked_decided for entry. Tonight's first
    /// light (n=28): both losses entered at safety < 0.25, median winner
    /// at 0.57 — the clock says "go" long before the evidence does.
    #[serde(default)]
    pub theta: f64,
    /// Settlement rule the model prices. "range_avg" = the pre-2026-08-23
    /// belief (whole-range TWAP average); "terminal" = the empirically
    /// verified rule (the 60s-TWAP stream's value at range END vs start —
    /// see docs/LESSONS.md settlement-rule discovery; terminal matched
    /// 93-99% of 3,193 gamma resolutions vs 86-88% for range_avg).
    /// Default stays range_avg until the replay A/B promotes terminal.
    #[serde(default = "d_settle_rule")]
    pub settle_rule: String,
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
    /// Where this arm's market data comes from: "binance" (the trade
    /// stream + 1m klines, every arm until now) or "rtds" (the Chainlink
    /// TWAP stream the market actually settles on — see updown_rtds.rs).
    ///
    /// `serde(default)`, no version bump, exactly like the fleet cap: a
    /// pre-existing arms-state.json reads back as "binance" (today's
    /// behaviour) and an older binary ignores the field rather than
    /// refusing the whole state and stranding a live window.
    #[serde(default = "d_feed")]
    pub feed: String,
    /// Maker step 0 (docs/maker-design.md §6): rest ONE post-only bid on
    /// the side the model wants when the book has NOTHING to lift. That is
    /// 9.6% of armed time — bid pinned near 1.00 with no offer at any
    /// price, median 82% elapsed, exactly when the model is finally
    /// confident (analysis/freq_funnel_report.md). No taker parameter
    /// reaches that time; it is supply, not a gate.
    ///
    /// Deliberately NOT a general maker: measured early-window maker was
    /// negative everywhere (a 0.5c half-spread against 3.65c of drift per
    /// 5s). This is a narrow, late-window, theta-approved resting bid.
    ///
    /// `serde(default)` = off, no version bump — a pre-existing
    /// arms-state.json reads back as today's engine, and an older binary
    /// ignores the field rather than refusing the whole state and
    /// stranding a live window (same contract as `feed` and the fleet cap).
    #[serde(default)]
    pub maker_bid: bool,
}

/// Price grid the pure core floors a resting bid onto. These markets quote
/// at 0.001 or finer; `OrderManager` floors again to the token's real tick,
/// so a coarser market only ever makes the bid cheaper.
const MAKER_TICK: f64 = 0.001;

/// The Binance proxy feed — the default, and what every existing arm runs.
pub(crate) const FEED_BINANCE: &str = "binance";
/// The settlement stream itself (updown_rtds.rs).
pub(crate) const FEED_RTDS: &str = "rtds";

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
/// pub(crate) only for updown_model.rs's moved budget_unlocked tests, which
/// document the historical late_frac default (0.6) this value replaced.
pub(crate) fn d_late_rem() -> f64 { 120.0 }
fn d_rho_block() -> f64 { -0.25 }
fn d_manip_push() -> f64 { 25.0 }
fn d_p_cap() -> f64 { 1.0 }
fn d_settle_rule() -> String { "range_avg".to_string() }
fn d_feed() -> String { FEED_BINANCE.to_string() }
/// Flip-proof buys stay live until this close to resolution.
const FLIP_BUY_CUTOFF_S: f64 = 8.0;

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
    /// `post_only` distinguishes a resting maker bid from a crossing taker
    /// clip. Every consumer must dispatch on this flag rather than
    /// re-deriving intent downstream (docs/maker-design.md §5.3): the two
    /// are different orders on the wire and different events in a fill sim.
    Buy { token: String, price: f64, size: f64, post_only: bool },
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
    /// feed="rtds" arms hold their registration on the shared RTDS
    /// supervisor here instead of owning feed threads. Dropping it
    /// unregisters, so every teardown path is covered by `stop_feed`
    /// without a second lifecycle to remember.
    rtds_sub: Option<RtdsSub>,
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
    /// R7: last model read's banked-decidedness, cached so the fleet
    /// pre-pass can size the shared un-decided pool without running the
    /// model a second time per tick. Decidedness moves once per window and
    /// then stays, so a tick of staleness here costs nothing; the fast half
    /// of the sum (committed + inflight) is always read fresh.
    last_banked_decided: bool,
    pub(crate) last_eval: Option<serde_json::Value>,
    /// Throttle for eval/gated lines in the durable tape.
    last_tape_at: f64,
    /// Throttle for the book/spot recorder — its own cadence, see
    /// book_sample_due.
    last_book_at: f64,
    /// token -> newest trade timestamp already recorded. Print flow is
    /// counted by high-water mark, not sampling window: data-api indexes
    /// trades tens of seconds late, so a "trades in the last 6s" filter
    /// matches nothing ever (the 22k-rows-of-zeros bug, 2026-08-23).
    trade_hwm: std::collections::HashMap<String, i64>,
    /// Maker step 0: token -> (price, notional) of the ONE post-only bid
    /// resting on that side. No TTL, unlike `inflight` — a resting quote
    /// is meant to sit there. It is un-decided speculative exposure the
    /// whole time it rests, so its notional counts against the arm's
    /// budget and the R7 fleet pool exactly like a clip's does. Empty
    /// whenever `maker_bid` is off, which makes every sum below identical
    /// to the pre-maker engine.
    maker_rest: std::collections::HashMap<String, (f64, f64)>,
}

/// Safety write cadence for the arm store. Every mutation writes too — this
/// only bounds the damage from a mutation path nobody remembered to hook.
const PERSIST_INTERVAL_S: f64 = 30.0;

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
    /// Durable arm state. Without it a crash mid-window is terminal: the
    /// position rides to resolution with no exit rule, the roll chain dies,
    /// and past `end` nothing can recreate the arm.
    store: ArmStore,
    last_persist_at: f64,
    /// R7 correlation-aware fleet cap: ceiling (USDC) on the total
    /// un-decided committed+inflight notional the whole fleet may carry at
    /// once. 0 = off, and off is the default — at rho 0.7 the arms lose
    /// together, but the operator arms the cap deliberately, never by
    /// upgrade. Strategy-level on purpose: an ArmParams knob would be one
    /// budget per arm, which is the thing that already exists.
    fleet_undecided_cap: f64,
    /// ONE RTDS supervisor for the whole strategy. Lazily connected by the
    /// first feed="rtds" arm and shared by every one after it — the socket
    /// carries all symbols and all three topics, so a per-arm connection
    /// would be N sockets for one stream's worth of data.
    rtds: RtdsHub,
}

struct RollTask {
    params: ArmParams,
    next_slug: String,
    next_start: f64,
    next_end: f64,
    next_try_at: f64,
}

impl RollTask {
    fn record(&self) -> RollRecord {
        RollRecord {
            params: self.params.clone(),
            next_slug: self.next_slug.clone(),
            next_start: self.next_start,
            next_end: self.next_end,
        }
    }

    /// Restored tasks are due immediately — the retry clock is runtime-only.
    fn from_record(r: RollRecord, now: f64) -> Self {
        Self {
            params: r.params,
            next_slug: r.next_slug,
            next_start: r.next_start,
            next_end: r.next_end,
            next_try_at: now,
        }
    }
}

/// btc-updown-5m-1787442000 + its bounds -> the following window.
/// Recurring series are contiguous: next start = this end.
/// pub(crate): recovery re-derives a downed arm's successor with it, so the
/// gap-hopping rule has exactly one implementation.
pub(crate) fn next_window(slug: &str, start: f64, end: f64) -> Option<(String, f64, f64)> {
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

/// Arm-time validation of the market-data source. Refusals here are loud on
/// purpose: an unsupported feed/market pairing that armed anyway would sit
/// gated on a stale feed for the life of the window and never say why.
///
/// close_open is refused on rtds because the model reads `candle_open` — the
/// exact 1h Binance candle open — and the settlement stream has no candles.
/// A close_open market is priced against a venue's OHLC, so it belongs on
/// the venue's feed.
pub(crate) fn check_feed(p: &ArmParams) -> Result<(), String> {
    match p.feed.as_str() {
        FEED_BINANCE => Ok(()),
        FEED_RTDS => {
            if p.kind != "twap" {
                return Err(format!(
                    "feed 'rtds' does not support kind '{}' — the settlement stream has no \
                     candle opens; arm close_open markets with --feed binance",
                    p.kind
                ));
            }
            if updown_rtds::rtds_symbol(&p.symbol).is_none() {
                return Err(format!(
                    "feed 'rtds' does not carry {} — the stream serves {}",
                    p.symbol,
                    updown_rtds::RTDS_SYMBOLS.join(", ")
                ));
            }
            Ok(())
        }
        other => Err(format!("unknown feed '{}' (binance | rtds)", other)),
    }
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

/// Order a token's book pulled, unless this pass already did.
///
/// The engine batches cancels per token and asks the CLOB to retire every
/// non-matching open order on it, so a second Cancel for the same token
/// puts the same order id in one request twice. Harmless before maker step
/// 0 (nothing emitted two cancels for one token in a pass); now the
/// maker-pull and a taker fire on the same side can both want one.
fn cancel_once(actions: &mut Vec<Action>, token: &str) {
    if !actions.iter().any(|a| matches!(a, Action::Cancel(t) if t == token)) {
        actions.push(Action::Cancel(token.to_string()));
    }
}

/// Append one JSONL record to the durable tape at ~/.pmt/engine/. The tape
/// is the cross-session dataset for calibrating gate parameters — every
/// fire, exit, and periodic eval survives reboots, unlike engine stdout.
fn tape(record: serde_json::Value) {
    append_jsonl("updown-tape.jsonl", record);
}

/// Append one JSONL record to the book/spot recorder at ~/.pmt/engine/.
/// Separate file from the eval tape — order-book history isn't
/// backfillable, so this is the raw feed the replay harness (R4) needs;
/// keeping it out of updown-tape.jsonl keeps that file's record types clean.
fn book_tape(record: serde_json::Value) {
    append_jsonl("book-tape.jsonl", record);
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
            rtds_sub: None,
            subscribed: false,
            cleaned: false,
            filled_usdc: 0.0,
            tunables: Tunables::default(),
            inflight: std::collections::HashMap::new(),
            last_clip: std::collections::HashMap::new(),
            last_clip_ask: std::collections::HashMap::new(),
            brake_latched: false,
            last_banked_decided: false,
            last_eval: None,
            last_tape_at: 0.0,
            last_book_at: 0.0,
            trade_hwm: std::collections::HashMap::new(),
            maker_rest: std::collections::HashMap::new(),
        }
    }

    fn tokens(&self) -> [String; 2] {
        [self.p.token_up.clone(), self.p.token_down.clone()]
    }

    /// R7: this arm's contribution to the fleet's shared un-decided pool.
    ///
    /// Committed + still-inflight notional, but only while the window is
    /// still a bet. Banked-decided capital left the pool the moment the
    /// model banked it: R9's theta gate owns entry into that state, and
    /// once there the exposure is no longer correlated with every other
    /// speculative arm — which is the only thing this cap rations. A
    /// closed window contributes nothing; its capital is resolved, not
    /// speculative (the study's `cleanup` zeroing, analysis/r7_fleet_cap.py).
    ///
    /// `position_floor` is the caller's authoritative committed floor —
    /// the live position tracker on real ticks, the fill sim's cost basis
    /// in replay — so both drivers share one definition of the pool
    /// (L18: three drifted copies of the same guard is how this goes wrong).
    pub(crate) fn undecided_committed(&self, position_floor: f64, now: f64) -> f64 {
        if self.last_banked_decided || now >= self.p.end {
            return 0.0;
        }
        let inflight: f64 = self
            .inflight
            .values()
            .filter(|(_, at)| now - *at < INFLIGHT_TTL_S)
            .map(|(n, _)| *n)
            .sum();
        self.filled_usdc.max(position_floor) + inflight + self.resting_usdc()
    }

    /// Notional tied up by resting post-only bids. Zero unless `maker_bid`
    /// is armed, so every budget sum that adds it is bit-identical to the
    /// pre-maker engine while the knob is off.
    fn resting_usdc(&self) -> f64 {
        self.maker_rest.values().map(|(_, n)| *n).sum()
    }

    /// Pull every resting maker bid and say which tokens need cancelling.
    /// Used by the paths that sweep the book wholesale — quiesce and window
    /// close — so a resting quote has no way to outlive the arm's own
    /// lifecycle rules.
    fn drop_maker_rests(&mut self) -> Vec<String> {
        let mut tokens: Vec<String> = self.maker_rest.drain().map(|(t, _)| t).collect();
        tokens.sort(); // HashMap drain order is arbitrary; actions are compared
        tokens
    }

    fn stop_feed(&mut self) {
        self.feed_stop.store(true, Ordering::SeqCst);
        for h in self.feed_handles.drain(..) {
            let _ = h.join();
        }
        // Unregisters on drop. The shared RTDS socket itself stays up for
        // the strategy's lifetime — it is one connection for the whole
        // fleet, and tearing it down between a window closing and its roll
        // re-arming would flap it every five minutes.
        self.rtds_sub = None;
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

    fn start_feeds(&mut self, rtds: &RtdsHub) {
        match self.p.feed.as_str() {
            FEED_BINANCE => {}
            FEED_RTDS => {
                // The settlement stream fills the SAME FeedState contract
                // the Binance threads do, so nothing downstream changes.
                // No Binance threads at all, and no Chainlink oracle
                // poller: that poller measures oracle-vs-Binance basis, and
                // on this feed both sides of that comparison are the same
                // series. live_guard_bp then floors to the arm's own param,
                // which is what the operator measured for the stream.
                let Some(symbol) = updown_rtds::rtds_symbol(&self.p.symbol) else {
                    return self.refuse_feed(format!(
                        "rtds does not carry {}", self.p.symbol
                    ));
                };
                tracing::info!(
                    slug = %self.p.slug, symbol = %symbol,
                    tw_s = settle_tw_secs(self.p.end - self.p.start),
                    "updown arm feeding off the RTDS settlement stream"
                );
                self.rtds_sub = Some(rtds.register(
                    &symbol,
                    settle_tw_secs(self.p.end - self.p.start),
                    self.p.start,
                    self.feed.clone(),
                ));
                return;
            }
            // Fail closed, never fall through to Binance. `on_command`
            // refuses an unknown feed, so this is the recovery path with a
            // hand-edited or corrupted state file — and an arm whose guard
            // was sized for one feed must not quietly trade another.
            other => return self.refuse_feed(format!("unknown feed '{}'", other)),
        }
        self.spawn_ws_spot();
        // Ships dark: the dynamic guard's oracle poller runs only when
        // PMENGINE_DYNAMIC_GUARD=1 — a routine engine restart must never
        // be the thing that activates an unproven subsystem. Without the
        // poller, live_guard_bp floors to the static param (no samples).
        // Pushed into feed_handles like the other threads — stop_feed's
        // existing join loop covers its teardown, no separate lifecycle.
        if std::env::var("PMENGINE_DYNAMIC_GUARD").ok().as_deref() == Some("1") {
            self.feed_handles.push(updown_oracle::spawn_poller(
                self.oracle.clone(),
                self.feed.clone(),
                self.feed_stop.clone(),
                self.p.symbol.clone(),
                self.p.end,
            ));
        }
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

    /// Start no feed at all and say why. spot_ts stays where it is, so the
    /// arm gates on a stale feed for its whole life — the safe end of the
    /// failure, and the reason travels into the gate line.
    fn refuse_feed(&mut self, why: String) {
        tracing::error!(
            slug = %self.p.slug, symbol = %self.p.symbol, feed = %self.p.feed,
            "updown arm has no usable feed — it will gate, never trade: {}", why
        );
        self.feed.lock().unwrap().last_err = Some(why);
    }

    /// Model fair P(UP) plus regime/decidedness context. Errors = gated.
    fn fair_p_up(&self, now: f64) -> Result<ModelEval, GateReason> {
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

/// One side's inputs to the maker slice — a struct rather than ten
/// positional f64s, most of which would be interchangeable at the call site.
struct MakerSlice<'a> {
    side: &'a str,
    token: &'a str,
    /// The p_cap'd fair for this side, same value the taker gates use.
    fair: f64,
    fair_req: f64,
    edge_req: f64,
    safety: f64,
    chop_blocked: bool,
    top: &'a TopOfBook,
    model: &'a ModelEval,
    now: f64,
}

impl ArmState {
    /// Maker step 0 (docs/maker-design.md §6 step 0): decide whether ONE
    /// post-only bid should be resting on a side the book is not offering.
    ///
    /// This is deliberately narrow. Early-window maker measured NEGATIVE
    /// everywhere — a 0.5c half-spread against 3.65c of drift per 5s — so
    /// this is not a market maker. It reaches exactly one miss class: the
    /// 9.6% of armed time where our side is bid near 1.00 and NOBODY is
    /// offering, median 82% through the window
    /// (analysis/freq_funnel_report.md). No taker parameter touches that
    /// time; it is supply, not a gate.
    ///
    /// Runs whether or not `maker_bid` is armed: with the knob off it only
    /// stamps `maker_candidate` on the eval so the operator can count the
    /// opportunity before any capital rides on it.
    fn maker_slice(
        &mut self,
        s: MakerSlice<'_>,
        room: &mut f64,
        fleet_room: &mut f64,
        actions: &mut Vec<Action>,
        evals: &mut Vec<serde_json::Value>,
    ) {
        // A quote already resting here owns notional the pass subtracted
        // from `room` (and from the fleet pool) before the side loop
        // started. Refund it up front so the sizing below sees the room
        // this side actually controls, then re-book whatever we leave
        // standing — the alternative double-counts a quote against itself.
        let standing = self.maker_rest.remove(s.token);
        if let Some((_, n)) = standing {
            *room += n;
            if !s.model.banked_decided {
                *fleet_room += n;
            }
        }

        let px = s.top.bid.map(|(bid, _)| {
            maker_bid_price(s.fair, s.edge_req, bid, self.p.max_price, MAKER_TICK)
        });

        // The whole condition set, spelled out. Every one must hold for a
        // bid to rest, and the absence of any one pulls it on this tick.
        let qualified = px.is_some_and(|px| px > 0.0)
            // (a) nothing to lift — the miss class this slice exists for.
            && s.top.ask.is_none()
            // (b) theta-approved, the same banked-evidence score entry
            //     uses (docs/LESSONS.md#L13). Entry tests it only on a
            //     window's FIRST clip; a resting bid re-tests it on every
            //     quote, because no ask exists to price the thesis against
            //     and nothing else stands between the model and un-decided
            //     exposure.
            // theta 0 would make the evidence test vacuous — a resting bid
            // with NO evidence gate is precisely the exposure this slice
            // must never take, so the knob demands a real theta. The live
            // fleet runs 0.3; a theta-0 arm simply never rests.
            && self.p.theta > 0.0
            && s.safety >= self.p.theta
            // (c) outside the quiesce sweep. Structurally true here (the
            //     sweep returns before the side loop) and stated anyway,
            //     so the condition set reads complete at the one place it
            //     is enforced.
            && s.now < self.p.end - self.p.quiesce_secs
            // (d) the window has not already gone wrong. Flat, unlike the
            //     taker latch: banked-decided arithmetic earns a clip its
            //     carve-out because a real ask prices it, and a resting
            //     bid has no such witness (docs/LESSONS.md#L8).
            && !self.brake_latched
            && !s.chop_blocked
            && s.fair >= s.fair_req
            // Averaging down is the same brake it always was, measured at
            // the price we would actually pay.
            && !avg_down_blocks(
                px.unwrap_or(0.0),
                self.last_clip_ask.get(s.token).copied(),
                self.tunables.avg_down_tol,
                s.model.banked_decided,
            )
            && s.now - self.last_clip.get(s.token).copied().unwrap_or(0.0)
                >= self.p.clip_cooldown_s
            && !self.inflight.contains_key(s.token);

        // Budget and the R7 fleet pool bind a resting bid exactly as they
        // bind a clip — it is un-decided speculative exposure from the
        // moment it rests, not from the moment it fills.
        let fleet_bound = if s.model.banked_decided {
            f64::INFINITY
        } else {
            (*fleet_room).max(0.0)
        };
        let clip_room = (*room).min(fleet_bound);
        let size = match px {
            Some(px) if qualified && clip_room > 5.0 => {
                (self.p.clip_usdc / px).min(clip_room / px).floor()
            }
            _ => 0.0,
        };

        if size < 5.0 {
            if standing.is_some() {
                cancel_once(actions, s.token);
            }
            return;
        }
        let px = px.expect("a sized quote always has a price");

        let mut eval = serde_json::json!({
            "side": s.side, "fair": s.fair, "ask": serde_json::Value::Null,
            "safety": (s.safety * 100.0).round() / 100.0,
            "maker_px": px, "maker_size": size,
        });

        if !self.p.maker_bid {
            // The shadow measurement, and it has to work with the knob
            // OFF: this is how the operator prices what the slice reaches
            // before arming it. `ask` stays null so every existing tape
            // consumer still charges the moment to the same funnel stage.
            eval["maker_candidate"] = serde_json::json!(true);
            evals.push(eval);
            return;
        }

        // Re-quote only when the price actually moved off the grid point
        // we are already resting on. A cancel/replace restarts the
        // order-age clock and spends placement tokens for nothing
        // (docs/maker-design.md §3).
        if let Some((resting_px, resting_n)) = standing {
            if (resting_px - px).abs() < MAKER_TICK / 2.0 {
                *room -= resting_n;
                if !s.model.banked_decided {
                    *fleet_room -= resting_n;
                }
                self.maker_rest.insert(s.token.to_string(), (resting_px, resting_n));
                eval["maker_rest"] = serde_json::json!(resting_px);
                evals.push(eval);
                return;
            }
        }

        let notional = size * px;
        *room -= notional;
        if !s.model.banked_decided {
            *fleet_room -= notional;
        }
        self.maker_rest.insert(s.token.to_string(), (px, notional));
        eval["maker_rest"] = serde_json::json!(px);
        evals.push(eval);
        tracing::info!(
            side = s.side, px, size, safety = s.safety, slug = %self.p.slug,
            "updown maker bid resting — nothing offered on this side at any price"
        );
        cancel_once(actions, s.token);
        actions.push(Action::Buy {
            token: s.token.to_string(),
            price: px,
            size,
            post_only: true,
        });
    }

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
            // Only a pending EXIT (the 0-notional sentinel) blocks another
            // exit — a pending BUY must not: its missed fill used to lock
            // the ENTIRE held position out of evacuation for the inflight
            // TTL, exactly while fair was collapsing (adversarial sweep
            // 2026-08-23 — the safeguard re-enabling the loss it guards).
            let exit_pending =
                self.inflight.get(token).map(|(n, _)| *n == 0.0).unwrap_or(false);
            if fair >= EXIT_FAIR || exit_pending {
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
                "t": now, "ev": EV_EXIT, "slug": p.slug, "side": side,
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
        // Signed print flow — the VPIN/R8 input, NOT backfillable. Counted
        // by per-token high-water mark on trade timestamps over the FULL
        // hub buffer (1h prune bound): any recency filter here re-creates
        // the arrival-lag hole — data-api indexes prints minutes late, so
        // a trade can enter the hub already older than a short window.
        // The HWM alone dedupes; lag shifts a print's sample attribution
        // but never drops it.
        let start_floor = self.p.start as i64 - 1;
        let (tok_up, tok_dn) = (self.p.token_up.clone(), self.p.token_down.clone());
        let hwm_map = &mut self.trade_hwm;
        let empty: Vec<crate::orderbook::TradeRecord> = Vec::new();
        let mut flow = |token: &str| -> (i64, f64, f64) {
            let hwm = hwm_map.entry(token.to_string()).or_insert(start_floor);
            let mut n = 0i64;
            let (mut buys, mut sells) = (0.0, 0.0);
            let mut newest = *hwm;
            for tr in ctx.trade_history.get(token).unwrap_or(&empty) {
                if tr.timestamp <= *hwm {
                    continue;
                }
                let sz = tr.size.to_f64().unwrap_or(0.0);
                n += 1;
                if tr.side.eq_ignore_ascii_case("buy") {
                    buys += sz;
                } else {
                    sells += sz;
                }
                newest = newest.max(tr.timestamp);
            }
            *hwm = newest;
            (n, buys, sells)
        };
        let (up_tn, up_tbuy, up_tsell) = flow(&tok_up);
        let (dn_tn, dn_tbuy, dn_tsell) = flow(&tok_dn);
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
        // Which feed this sample came off, and how stale it was when read.
        // Replay/analysis has to be able to tell a streamed book from a
        // polled one — a "byte-identical consecutive samples" finding means
        // something completely different on each.
        let now_ms = crate::orderbook::now_ms();
        let meta = |token: &str| -> (&'static str, Option<i64>) {
            ctx.order_books
                .get(token)
                .map(|b| (b.source.as_str(), b.age_ms(now_ms)))
                .unwrap_or(("none", None))
        };
        let (up_src, up_age_ms) = meta(&self.p.token_up);
        let (dn_src, dn_age_ms) = meta(&self.p.token_down);
        let src = if up_src == dn_src { up_src } else { "mixed" };
        // Tiny lock scope — copy the two floats out, don't hold the feed
        // mutex while formatting/writing JSON.
        let (spot, spot_ts) = {
            let f = self.feed.lock().unwrap();
            (f.spot, f.spot_ts)
        };
        book_tape(serde_json::json!({
            "t": now, "ev": EV_BOOK, "slug": self.p.slug,
            "up_bid": up_bid, "up_bid_sz": up_bid_sz,
            "up_ask": up_ask, "up_ask_sz": up_ask_sz,
            "dn_bid": dn_bid, "dn_bid_sz": dn_bid_sz,
            "dn_ask": dn_ask, "dn_ask_sz": dn_ask_sz,
            "up_tn": up_tn, "up_tbuy": up_tbuy, "up_tsell": up_tsell,
            "dn_tn": dn_tn, "dn_tbuy": dn_tbuy, "dn_tsell": dn_tsell,
            "spot": spot, "spot_age_s": now - spot_ts,
            "src": src, "up_src": up_src, "dn_src": dn_src,
            "up_age_ms": up_age_ms, "dn_age_ms": dn_age_ms,
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
        model: Result<ModelEval, GateReason>,
        now: f64,
    ) -> DecideOut {
        let mut uncapped = f64::INFINITY;
        self.decide_fleet(view, model, now, &mut uncapped)
    }

    /// `decide` with R7's shared fleet budget threaded through.
    ///
    /// `fleet_room` is the whole fleet's remaining un-decided headroom for
    /// this tick, decremented in place as arms consume it. It has to be
    /// shared and mutable rather than recomputed per arm: `on_tick` walks
    /// `arms` sequentially, so two arms in the SAME pass would otherwise
    /// both read the same stale headroom and both fire, overshooting the
    /// cap by as many arms as happen to want a clip. `f64::INFINITY` is the
    /// cap-off case and makes every clamp below a no-op.
    pub(crate) fn decide_fleet(
        &mut self,
        view: &ArmView,
        model: Result<ModelEval, GateReason>,
        now: f64,
        fleet_room: &mut f64,
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
                self.maker_rest.clear(); // the two Cancels below pull them
                tracing::info!(slug = %p.slug, filled_usdc = self.filled_usdc, "updown window closed — cleaning up");
                tape_out.push(serde_json::json!({"t": now, "ev": EV_CLEANUP, "slug": p.slug}));
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
            // TTL-prune unconditionally: this only ran inside the flip_live
            // branch before, so a stale entry from a missed fill froze the
            // exit rule for the whole non-flip quiesce window.
            self.inflight.retain(|_, (_, at)| now - *at < INFLIGHT_TTL_S);
            let model = model.ok();
            let flip_live = model.as_ref().map(|m| m.flip_proof).unwrap_or(false)
                && now < p.end - FLIP_BUY_CUTOFF_S;
            if !flip_live {
                actions.push(Action::Cancel(p.token_up.clone()));
                actions.push(Action::Cancel(p.token_down.clone()));
            }
            // The quiesce sweep pulls a resting maker bid like any other
            // order — including through the flip-proof carve-out, which
            // exempts CLIPS from the sweep, not standing quotes. A flip
            // clip needs an ask; a maker bid exists precisely because there
            // is none, so the two never contend for the same token anyway.
            for token in self.drop_maker_rests() {
                cancel_once(&mut actions, &token);
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
                                        "t": now, "ev": EV_FIRE, "slug": p.slug, "side": side,
                                        "ask": ask, "fair": fair, "net": net, "size": size,
                                        "committed": committed, "mode": "flip", "rho": m.rho,
                                    }));
                                    self.last_clip.insert(token.clone(), now);
                                    self.inflight.insert(token.clone(), (size * ask, now));
                                    let limit = pay_up_limit(
                                        ask, net, p.min_edge, p.pay_up_max, p.max_price,
                                    );
                                    cancel_once(&mut actions, &token);
                                    actions.push(Action::Buy {
                                        token: token.clone(),
                                        price: limit,
                                        size,
                                        post_only: false,
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
                // A gate means "no trade" (docs/LESSONS.md#L2), and that has
                // to reach a quote already ON the book, not just the next
                // one. A stale feed cannot tell us a resting bid is still
                // priced right, so it comes off.
                for token in self.drop_maker_rests() {
                    cancel_once(&mut actions, &token);
                }
                // The numbers ride ALONGSIDE the prose, never instead of it:
                // `reason` reads exactly as it always has for old consumers,
                // while margin/banked/cushion/guard arrive as fields so
                // nobody has to regex a sentence apart. Non-basis gates
                // (stale feed) write them as null.
                self.last_eval = Some(serde_json::json!({
                    "state": "gated", "reason": gate.reason, "t": now,
                    "margin_bp": gate.margin_bp, "banked_bp": gate.banked_bp,
                    "cushion_bp": gate.cushion_bp, "guard_bp": gate.guard_bp,
                }));
                if now - self.last_tape_at >= 5.0 {
                    self.last_tape_at = now;
                    // Asks recorded even while gated: the 2026-08-23 audit
                    // couldn't price what the basis guard cost because
                    // fully-gated windows logged no book data at all.
                    tape_out.push(serde_json::json!({
                        "t": now, "ev": EV_GATED, "slug": p.slug, "reason": gate.reason,
                        "margin_bp": gate.margin_bp, "banked_bp": gate.banked_bp,
                        "cushion_bp": gate.cushion_bp, "guard_bp": gate.guard_bp,
                        "up_ask": view.up.ask.map(|(px, _)| px),
                        "dn_ask": view.dn.ask.map(|(px, _)| px),
                    }));
                }
                return DecideOut { actions, tape: tape_out, finished: false };
            }
        };
        let (p_up, sig_bp) = (m.p_up, m.sig_bp);
        // Cache for the fleet pre-pass. A gate (stale feed) leaves the last
        // known value standing: a feed hiccup doesn't un-bank a window.
        self.last_banked_decided = m.banked_decided;

        // Notional already committed. on_fill events are unreliable for
        // taker orders (fills often only surface via the periodic position
        // reconcile), so the authoritative floor is the position tracker:
        // shares held x entry price. Take the max of every signal we have.
        self.inflight.retain(|_, (_, at)| now - *at < INFLIGHT_TTL_S);
        let inflight_usdc: f64 = self.inflight.values().map(|(n, _)| n).sum();
        // A resting post-only bid is spend that has not landed yet, exactly
        // like an inflight clip — it just has no TTL. Zero while maker_bid
        // is off, so this subtraction is a no-op on today's engine.
        let resting_usdc = self.resting_usdc();
        let committed = self.filled_usdc.max(view.position_floor);
        let budget = p.size_usdc - committed - inflight_usdc - resting_usdc;

        let exits = self.exit_actions(view, p_up, now, &mut tape_out);
        actions.extend(exits);

        // Exposure envelope: small speculative clips until the window is
        // either late or banked-decided; then the full budget eases into
        // the safe bet clip by clip.
        let unlocked = budget_unlocked(now, p.end, p.late_rem_s, m.banked_decided);
        let cap = p.size_usdc * if unlocked { 1.0 } else { p.early_frac };
        let mut room = (cap - committed - inflight_usdc - resting_usdc).min(budget);
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
            // R6 cap and the side's banked-evidence score are both
            // ask-independent, so they are computed before the book read —
            // the maker slice below needs them on a side with no ask at all.
            let fair_raw = fair;
            let fair = if m.flip_proof { fair } else { fair.min(p.p_cap) };
            let safety = side_safety(side == "up", m.banked_margin_bp, m.cushion_bp);

            let Some((ask, ask_size)) = top.ask else {
                // --- maker step 0: nobody is offering this side ----------
                // The book is bid up near 1.00 with NO ask at any price.
                // 9.6% of armed time lives here and no taker knob reaches
                // it — it is supply (analysis/freq_funnel_report.md). The
                // one thing that does reach it is a resting bid.
                self.maker_slice(
                    MakerSlice {
                        side, token, fair, fair_req, edge_req, safety,
                        chop_blocked, top: &top, model: &m, now,
                    },
                    &mut room,
                    fleet_room,
                    &mut actions,
                    &mut evals,
                );
                continue;
            };
            // An ask exists: whatever we were resting on this side no
            // longer meets the slice's one precondition, so it comes off
            // the book on this tick.
            if self.maker_rest.remove(token).is_some() {
                cancel_once(&mut actions, token);
            }
            let fee = p.fee_rate * ask.min(1.0 - ask);
            let net = fair - ask - fee;
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
            // R7 fleet cap: the fleet's shared un-decided headroom is one
            // more ceiling on this clip's room — the arm's own budget/cap
            // machinery above is untouched. Banked-decided clips are never
            // clamped: that capital was never in the pool this rations.
            let fleet_bound =
                if m.banked_decided { f64::INFINITY } else { fleet_room.max(0.0) };
            let clip_room = room.min(fleet_bound);
            let sized = |r: f64| -> f64 {
                if r > 5.0 {
                    (p.clip_usdc / ask).min(ask_size).min(r / ask).floor()
                } else {
                    0.0
                }
            };
            let size = sized(clip_room);
            let cooled =
                now - self.last_clip.get(token).copied().unwrap_or(0.0) >= p.clip_cooldown_s;
            let gates_ok = brake.is_none()
                && !chop_blocked
                && fair >= fair_req
                && net >= edge_req
                && ask <= p.max_price
                && cooled
                && !self.inflight.contains_key(token);
            // Label the fleet only when it is what stopped a clip that
            // every other gate cleared — otherwise "fleet" would shadow the
            // real reason. Unlike distrust/avg_down it never latches the
            // window: a full fleet says nothing about THIS window's
            // evidence, and the arm must be free to fire once room returns.
            let fleet_stopped = gates_ok && size < 5.0 && sized(room) >= 5.0;
            let mut eval = serde_json::json!({
                "side": side, "fair": fair, "ask": ask, "net": net,
                "safety": (safety * 100.0).round() / 100.0,
            });
            if fair < fair_raw {
                eval["fair_raw"] = serde_json::json!(fair_raw);
            }
            if let Some(b) = brake {
                eval["brake"] = serde_json::json!(b);
            } else if fleet_stopped {
                eval["brake"] = serde_json::json!("fleet");
                // What the fleet cost this side, so R7's own effect is
                // priceable from the tape the way the basis guard's is.
                eval["fleet_blocked"] = serde_json::json!(sized(room) * ask);
            }
            evals.push(eval);

            if !(gates_ok && size >= 5.0) {
                continue;
            }
            tracing::info!(
                side, ask, fair, net, size, unlocked,
                slug = %p.slug,
                "updown clip firing"
            );
            tape_out.push(serde_json::json!({
                "t": now, "ev": EV_FIRE, "slug": p.slug, "side": side,
                "ask": ask, "fair": fair, "net": net, "size": size,
                "committed": committed, "elapsed_frac": elapsed_frac,
                "mode": if unlocked { "safe" } else { "spec" }, "rho": m.rho,
            }));
            room -= size * ask;
            // Only speculative notional draws down the shared pool, and it
            // draws down NOW so the next arm in this tick's loop — and this
            // arm's other side — see the headroom this clip just spent.
            if !m.banked_decided {
                *fleet_room -= size * ask;
            }
            self.last_clip.insert(token.clone(), now);
            self.last_clip_ask.insert(token.clone(), ask);
            self.inflight.insert(token.clone(), (size * ask, now));
            let limit = pay_up_limit(ask, net, edge_req, p.pay_up_max, p.max_price);
            cancel_once(&mut actions, token);
            actions.push(Action::Buy {
                token: token.clone(),
                price: limit,
                size,
                post_only: false,
            });
        }

        // Only carried when a cap is actually set — an uncapped fleet has
        // infinite room, which is not a number any consumer should see.
        let fleet_left =
            if fleet_room.is_finite() { Some(fleet_room.max(0.0)) } else { None };
        let mut eval_rec = serde_json::json!({
            "state": "armed", "t": now, "p_up": p_up, "sig_bp": sig_bp,
            "rho": m.rho, "mode": if unlocked { "safe" } else { "spec" },
            "chop_blocked": chop_blocked, "banked_decided": m.banked_decided,
            "margin_bp": m.margin_bp, "banked_bp": m.banked_margin_bp,
            "cushion_bp": m.cushion_bp, "guard_bp": m.guard_bp,
            "committed": committed, "budget": budget, "room": room,
            "inflight": inflight_usdc, "sides": evals,
        });
        if let Some(fr) = fleet_left {
            eval_rec["fleet_room"] = serde_json::json!(fr);
        }
        // Only when a bid is actually resting — an arm with maker_bid off
        // never carries the field, so its records are the old shape.
        let resting_now = self.resting_usdc();
        if resting_now > 0.0 {
            eval_rec["resting"] = serde_json::json!(resting_now);
        }
        self.last_eval = Some(eval_rec);
        if now - self.last_tape_at >= 5.0 {
            self.last_tape_at = now;
            let mut rec = serde_json::json!({
                "t": now, "ev": EV_EVAL, "slug": p.slug, "p_up": p_up,
                "sig_bp": sig_bp, "rho": m.rho, "banked_decided": m.banked_decided,
                "margin_bp": m.margin_bp, "banked_bp": m.banked_margin_bp,
                "cushion_bp": m.cushion_bp, "guard_bp": m.guard_bp,
                "committed": committed, "sides": evals,
            });
            if let Some(fr) = fleet_left {
                rec["fleet_room"] = serde_json::json!(fr);
            }
            if resting_now > 0.0 {
                rec["resting"] = serde_json::json!(resting_now);
            }
            tape_out.push(rec);
        }

        DecideOut { actions, tape: tape_out, finished: false }
    }

    /// Live-tick adapter around `decide`: snapshot the StrategyContext into
    /// an ArmView, run the pure pass, then do the I/O it prescribed — tape
    /// records to disk here, orders upstream as Signals.
    fn tick(
        &mut self,
        ctx: &StrategyContext,
        now: f64,
        fleet_room: &mut f64,
    ) -> (Vec<Signal>, bool) {
        if self.subscribed && now < self.p.end {
            self.record_book(ctx, now);
        }
        let view = arm_view(ctx, &self.p);
        let model = self.fair_p_up(now);
        let out = self.decide_fleet(&view, model, now, fleet_room);
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
/// core stays in f64 like the model math.
///
/// Urgency is the ONLY channel the engine has for post-only: `Low` means
/// add liquidity and never take it, everything else is a plain crossing
/// GTC limit. Taker clips stay High, as they always were.
fn to_signal(a: Action) -> Signal {
    match a {
        Action::Subscribe(t) => Signal::Subscribe { token_id: t },
        Action::Unsubscribe(t) => Signal::Unsubscribe { token_id: t },
        Action::Cancel(t) => Signal::Cancel { token_id: t },
        Action::Buy { token, price, size, post_only } => Signal::Buy {
            token_id: token,
            price: Decimal::from_f64(price).unwrap_or(Decimal::ONE),
            size: Decimal::from_f64(size).unwrap_or(Decimal::ZERO),
            urgency: if post_only { Urgency::Low } else { Urgency::High },
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
        let mut s = Self::with_store(ArmStore::live());
        s.recover(unix_now());
        s
    }

    /// Bare construction against an explicit store, no recovery pass. The
    /// seam the persistence tests drive.
    pub(crate) fn with_store(store: ArmStore) -> Self {
        Self {
            id: "updown".to_string(),
            arms: std::collections::BTreeMap::new(),
            pending_cleanup: Vec::new(),
            rolls: Vec::new(),
            store,
            last_persist_at: 0.0,
            fleet_undecided_cap: 0.0,
            rtds: RtdsHub::new(),
        }
    }

    /// R7: the fleet's remaining shared un-decided headroom for one tick.
    ///
    /// Recomputed from live exposure every tick rather than carried: fills
    /// land through the position reconcile, not through `on_fill` (the
    /// engine's oldest gotcha), so a running counter would drift off the
    /// truth within a window. `INFINITY` when no cap is set, which makes
    /// every clamp downstream a no-op — cap-off is exactly today's engine.
    fn fleet_room(&self, ctx: &StrategyContext, now: f64) -> f64 {
        if self.fleet_undecided_cap <= 0.0 {
            return f64::INFINITY;
        }
        let undecided: f64 = self
            .arms
            .values()
            .map(|a| a.undecided_committed(position_floor(ctx, &a.p), now))
            .sum();
        (self.fleet_undecided_cap - undecided).max(0.0)
    }

    /// Rebuild from the durable store, then rewrite it clean.
    ///
    /// Still-open windows re-arm from their persisted params alone — token
    /// ids were fetched at arm time and live in there, so this needs no
    /// gamma call. Spend is NOT persisted and doesn't need to be: `decide`
    /// floors `committed` at the position tracker's shares x entry, and
    /// re-armed tokens are back in `subscriptions()` before the engine's
    /// startup position seed runs, so the budget comes back with the arm.
    pub(crate) fn recover(&mut self, now: f64) {
        let Some(state) = self.store.load() else { return };
        // R7's cap comes back before any arm does: a restart that re-armed
        // an uncapped fleet would quietly undo the operator's ration.
        self.fleet_undecided_cap = state.fleet_undecided_cap.max(0.0);
        if self.fleet_undecided_cap > 0.0 {
            tracing::info!(
                cap = self.fleet_undecided_cap,
                "updown recovery — fleet un-decided cap restored"
            );
        }
        let plan = plan_recovery(&state, now);
        for p in plan.rearm {
            tracing::info!(
                slug = %p.slug, size = p.size_usdc, rem_s = p.end - now,
                "updown recovery — window still open, re-arming from durable state"
            );
            self.install_arm(p);
        }
        for r in plan.rolls {
            tracing::info!(
                slug = %r.next_slug, from = %r.params.slug,
                "updown recovery — resuming roll chain (hops forward if this window also passed)"
            );
            self.rolls.push(RollTask::from_record(r, now));
        }
        for p in &plan.ended {
            tracing::info!(
                slug = %p.slug, end = p.end, roll = p.roll,
                "updown recovery — window closed while the engine was down, dropping the arm"
            );
        }
        // Detection only: anything still held on those tokens rode to
        // resolution with no exit rule running. Fires off-thread.
        spawn_unmanaged_check(plan.ended);
        self.persist(now);
    }

    /// Build, start feeds, and register an arm, replacing any existing one
    /// for the slug. The single path an arm enters the fleet by — command,
    /// roll, or recovery.
    fn install_arm(&mut self, p: ArmParams) {
        let slug = p.slug.clone();
        if let Some(mut old) = self.arms.remove(&slug) {
            old.stop_feed();
        }
        let mut arm = ArmState::with_params(p);
        // Unit tests get the arm without the sockets: `cfg!` (not `#[cfg]`)
        // so the feed path still compiles and lints under `cargo test`.
        if cfg!(not(test)) {
            arm.start_feeds(&self.rtds);
        }
        self.arms.insert(slug, arm);
    }

    /// Snapshot arms + roll chain to the store. A full snapshot, never an
    /// incremental edit: a disarm that left a resurrectable entry behind
    /// would re-buy a market the operator retired, so "gone from the map"
    /// has to mean "gone from disk" with no bookkeeping in between.
    fn persist(&mut self, now: f64) {
        self.last_persist_at = now;
        let arms: Vec<ArmParams> = self.arms.values().map(|a| a.p.clone()).collect();
        let rolls: Vec<RollRecord> = self.rolls.iter().map(|t| t.record()).collect();
        self.store
            .save(&ArmsState::new(arms, rolls, now).with_fleet_cap(self.fleet_undecided_cap));
    }

    /// Arm any due successor windows. Gamma hiccups retry every 10s; a
    /// task whose target window has already ended hops forward instead
    /// of dying, so an unattended fleet self-heals. Returns true when the
    /// roll set changed and the store owes a write.
    fn process_rolls(&mut self, now: f64) -> bool {
        if self.rolls.is_empty() {
            return false;
        }
        let mut changed = false;
        let mut keep = Vec::new();
        for mut task in std::mem::take(&mut self.rolls) {
            if now < task.next_try_at {
                keep.push(task);
                continue;
            }
            if now >= task.next_end {
                changed = true;
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
                        "t": now, "ev": EV_ROLL, "slug": p.slug, "size": p.size_usdc,
                    }));
                    self.install_arm(p);
                    changed = true;
                }
                Err(e) => {
                    tracing::warn!(slug = %task.next_slug, err = %e, "updown roll retry");
                    task.next_try_at = now + 10.0;
                    keep.push(task);
                }
            }
        }
        self.rolls = keep;
        changed
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

        // R7 pre-pass: one shared budget for the whole sweep below, spent
        // down as arms consume it. Sizing it once per tick and handing out
        // a &mut is what keeps two arms in the same pass from both seeing
        // the same headroom and both firing into it.
        let mut fleet_room = self.fleet_room(ctx, now);

        let mut finished = Vec::new();
        for (slug, arm) in self.arms.iter_mut() {
            let (mut s, done) = arm.tick(ctx, now, &mut fleet_room);
            signals.append(&mut s);
            if done {
                finished.push(slug.clone());
            }
        }
        let retired = !finished.is_empty();
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
        let rolled = self.process_rolls(now);
        // Write on every mutation, so a window the engine actually saw close
        // is off disk immediately — an entry still on disk past its end is
        // exactly what the unmanaged-position check reads as "we were down".
        if retired || rolled || now - self.last_persist_at >= PERSIST_INTERVAL_S {
            self.persist(now);
        }

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
        self.rtds.stop();
    }

    fn on_command(&mut self, cmd: &serde_json::Value) -> Result<serde_json::Value, String> {
        match cmd.get("action").and_then(|a| a.as_str()) {
            Some("arm") => {
                let p: ArmParams =
                    serde_json::from_value(cmd.clone()).map_err(|e| format!("bad params: {}", e))?;
                if p.kind != "twap" && p.kind != "close_open" {
                    return Err(format!("unknown kind '{}'", p.kind));
                }
                check_feed(&p)?;
                if unix_now() >= p.end {
                    return Err("window already over".to_string());
                }
                let slug = p.slug.clone();
                // Re-arming a slug replaces its arm (fresh budget + feeds).
                self.install_arm(p);
                self.persist(unix_now());
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
                // Before the reply, not on the next tick: a disarm that
                // survived on disk would re-arm itself on the next restart
                // and buy a market the operator deliberately retired.
                self.persist(unix_now());
                Ok(serde_json::json!({
                    "disarmed": slugs, "arms": self.arms.len(),
                    "rolls_cancelled": rolls_before - self.rolls.len(),
                    "cleanup": "next tick",
                }))
            }
            // R7 fleet cap. Strategy-level, not per-arm: the quantity being
            // rationed is the SUM across arms, so there is no arm that owns
            // it. Absent/0/negative all mean off, which is the default and
            // is byte-for-byte today's behavior.
            Some("fleet") => {
                let cap = cmd
                    .get("undecided_cap_usdc")
                    .map(|v| v.as_f64().ok_or("undecided_cap_usdc must be a number"))
                    .transpose()?
                    .unwrap_or(0.0)
                    .max(0.0);
                self.fleet_undecided_cap = cap;
                tracing::info!(cap, arms = self.arms.len(), "updown fleet un-decided cap set");
                self.persist(unix_now());
                Ok(serde_json::json!({
                    "undecided_cap_usdc": cap,
                    "enabled": cap > 0.0,
                    "arms": self.arms.len(),
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
                            "feed": a.p.feed,
                            "maker_bid": a.p.maker_bid,
                            "resting_usdc": a.resting_usdc(),
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
                    "fleet_undecided_cap": self.fleet_undecided_cap,
                    "rtds": self.rtds.health().json(unix_now()),
                }))
            }
            _ => Err("unknown action (arm | disarm | fleet | status)".to_string()),
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
        let start_ms = ((start as i64 - KLINE_LOOKBACK_S) * 1000).to_string();
        let v: serde_json::Value = client
            .get(format!("{}/api/v3/klines", BINANCE_DATA))
            .query(&[("symbol", symbol), ("interval", "1m"), ("startTime", &start_ms), ("limit", "500")])
            .send()
            .and_then(|r| r.error_for_status())
            .map_err(|e| format!("klines: {}", e))?
            .json()
            .map_err(|e| format!("klines json: {}", e))?;
        for k in shape_klines(&v) {
            per_min.push((k.t, k.mid()));
            closes.push(k.c);
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
/// on_fill misses taker fills (the periodic reconcile catches them), so this
/// is the authoritative budget floor: shares held x entry price, with the
/// live ask as the honest estimate while reconcile-seeded avg is still 0.
fn position_floor(ctx: &StrategyContext, p: &ArmParams) -> f64 {
    [&p.token_up, &p.token_down]
        .iter()
        .filter_map(|t| ctx.positions.get(t).map(|pos| (*t, pos)))
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

fn unix_now() -> f64 {
    std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map(|d| d.as_secs_f64())
        .unwrap_or(0.0)
}

#[cfg(test)]
mod tests {
    use super::*;

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
        assert_eq!(p.late_rem_s, 120.0);
        assert_eq!(p.rho_block, -0.25);
        assert!(!p.maker_bid, "maker step 0 ships dark");
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
        let Action::Buy { token, price, size, post_only } = b[0] else { unreachable!() };
        assert_eq!(token, "s-u");
        assert_eq!(*price, 0.94);
        assert_eq!(*size, 26.0, "clip_usdc 25 / 0.94 floored");
        assert!(!*post_only, "a clip crosses the spread — it is never post-only");
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
    fn decide_gated_record_emits_numbers_alongside_the_reason() {
        // Both halves of the contract in one test: the durable tape and the
        // status `last_eval` carry margin/banked/cushion/guard as FIELDS,
        // and `reason` still reads exactly as it always did so old
        // consumers (and old tape lines) are unaffected.
        let mut arm = armed(params("s"));
        let gate = GateReason {
            reason: "basis guard: projected margin -4.9bp inside 6.0bp noise band \
                     [banked +1.0bp cushion 9.0bp]"
                .to_string(),
            margin_bp: Some(-4.9),
            banked_bp: Some(1.0),
            cushion_bp: Some(9.0),
            guard_bp: Some(6.0),
        };
        let out = arm.decide(&view_with_up_ask(0.94, 500.0), Err(gate), 1400.0);
        assert!(out.actions.is_empty(), "a gate never trades");
        let rec = out.tape.iter().find(|r| r["ev"] == EV_GATED).expect("gated tape record");
        assert!(rec["reason"].as_str().unwrap().starts_with("basis guard"));
        assert_eq!(rec["margin_bp"], -4.9);
        assert_eq!(rec["banked_bp"], 1.0);
        assert_eq!(rec["cushion_bp"], 9.0);
        assert_eq!(rec["guard_bp"], 6.0);
        assert_eq!(rec["up_ask"], 0.94, "asks still recorded while gated");
        let e = arm.last_eval.as_ref().unwrap();
        assert_eq!(e["state"], "gated");
        assert_eq!(e["margin_bp"], -4.9);
        assert_eq!(e["guard_bp"], 6.0);
    }

    #[test]
    fn decide_gated_record_nulls_the_numbers_for_a_non_basis_gate() {
        let mut arm = armed(params("s"));
        let out = arm.decide(&ArmView::default(), Err(GateReason::plain("feed stale")), 1400.0);
        let rec = out.tape.iter().find(|r| r["ev"] == EV_GATED).expect("gated tape record");
        assert_eq!(rec["reason"], "feed stale");
        assert!(rec["margin_bp"].is_null(), "no margin behind a stale feed");
        assert!(rec["guard_bp"].is_null());
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
    fn decide_p_cap_blocks_sure_things_unless_flip_proof() {
        let mut p = params("s");
        p.p_cap = 0.98;
        let mut arm = armed(p);
        // fair 1.0 at ask 0.97: uncapped net ~0.028 fires; capped net
        // ~0.008 sits under min_edge 0.015 — the "sure thing" is refused.
        let m = ModelEval { flip_proof: false, ..locked_up_model() };
        let out = arm.decide(&view_with_up_ask(0.97, 500.0), Ok(m), 1400.0);
        assert!(buys(&out).is_empty());
        let eval = &arm.last_eval.as_ref().unwrap()["sides"][0];
        assert_eq!(eval["fair"], 0.98);
        assert_eq!(eval["fair_raw"], 1.0);
        // flip-proof is exempt: the TWAP is beyond reach, the price is real.
        let m = ModelEval { flip_proof: true, ..locked_up_model() };
        let out = arm.decide(&view_with_up_ask(0.97, 500.0), Ok(m), 1400.1);
        assert_eq!(buys(&out).len(), 1);
    }

    #[test]
    fn p_cap_default_is_inert() {
        let mut arm = armed(params("s"));
        let out = arm.decide(&view_with_up_ask(0.94, 500.0), Ok(locked_up_model()), 1400.0);
        assert_eq!(buys(&out).len(), 1);
        assert!(arm.last_eval.as_ref().unwrap()["sides"][0].get("fair_raw").is_none());
    }

    #[test]
    fn decide_pending_buy_does_not_block_exits() {
        let mut arm = armed(params("s"));
        // A buy is in flight (missed fill, notional > 0) while fair has
        // collapsed on 50 held shares with a live bid near fair.
        arm.inflight.insert("s-u".into(), (24.0, 1399.0));
        let view = ArmView {
            up: TopOfBook { bid: Some((0.30, 200.0)), ask: Some((0.32, 200.0)) },
            held_up: 50.0,
            position_floor: 40.0,
            ..ArmView::default()
        };
        let collapsed = ModelEval { p_up: 0.30, banked_decided: false, ..locked_up_model() };
        let out = arm.decide(&view, Ok(collapsed), 1400.0);
        assert!(
            out.actions.iter().any(|a| matches!(a, Action::Sell { .. })),
            "held position must evacuate even with a buy pending"
        );
        // But a pending EXIT sentinel (notional 0.0) still blocks re-selling.
        let mut arm2 = armed(params("s"));
        arm2.inflight.insert("s-u".into(), (0.0, 1399.9));
        let view2 = ArmView {
            up: TopOfBook { bid: Some((0.30, 200.0)), ask: Some((0.32, 200.0)) },
            held_up: 50.0,
            ..ArmView::default()
        };
        let collapsed2 = ModelEval { p_up: 0.30, banked_decided: false, ..locked_up_model() };
        let out2 = arm2.decide(&view2, Ok(collapsed2), 1400.0);
        assert!(!out2.actions.iter().any(|a| matches!(a, Action::Sell { .. })));
    }

    #[test]
    fn decide_pay_up_raises_the_limit_not_the_sizing() {
        let mut p = params("s");
        p.pay_up_max = 0.02;
        let mut arm = armed(p);
        let out = arm.decide(&view_with_up_ask(0.90, 500.0), Ok(locked_up_model()), 1400.0);
        let b = buys(&out);
        assert_eq!(b.len(), 1);
        let Action::Buy { price, size, .. } = b[0] else { unreachable!() };
        assert!(*price > 0.90 + 1e-9, "limit chases above the ask");
        assert_eq!(*size, 27.0, "sizing still on the decision ask (25/0.90)");
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

    // --- R7 correlation-aware fleet cap ---------------------------------
    //
    // The cap rations ONE quantity across the whole fleet: un-decided
    // committed notional. These tests pin the three things that make that
    // real — the shared budget survives an arm-to-arm handoff inside a
    // single tick, banked-decided capital is outside the pool entirely,
    // and an unset cap is byte-for-byte the old engine.

    /// An undecided model that still clears every gate at t=1400 (rem=100s
    /// unlocks the budget regardless of decidedness, and net ~0.056 sits
    /// under the 0.15 distrust threshold).
    fn undecided_up_model() -> ModelEval {
        ModelEval { banked_decided: false, ..locked_up_model() }
    }

    #[test]
    fn fleet_cap_rations_one_pool_across_arms_in_the_same_tick() {
        // Two arms, both undecided, both wanting a $24.44 clip, into a pool
        // with room for exactly one. This is the same-tick race: `on_tick`
        // walks the arms sequentially, so the second arm MUST see what the
        // first just spent, not the headroom the tick opened with.
        let (mut a, mut b) = (armed(params("a")), armed(params("b")));
        let mut room = 26.0;

        let out_a = a.decide_fleet(&view_with_up_ask(0.94, 500.0), Ok(undecided_up_model()), 1400.0, &mut room);
        assert_eq!(buys(&out_a).len(), 1, "first arm takes the pool");
        assert!((room - (26.0 - 26.0 * 0.94)).abs() < 1e-9, "the clip drew the pool down in place");

        let out_b = b.decide_fleet(&view_with_up_ask(0.94, 500.0), Ok(undecided_up_model()), 1400.0, &mut room);
        assert!(buys(&out_b).is_empty(), "second arm finds the pool spent");
        let side = &b.last_eval.as_ref().unwrap()["sides"][0];
        assert_eq!(side["brake"], "fleet", "and says so — not 'budget', not silence");
        assert!(
            (side["fleet_blocked"].as_f64().unwrap() - 26.0 * 0.94).abs() < 1e-9,
            "the record prices what the cap cost, the way the basis guard does"
        );

        // The control: hand each arm its own fresh headroom and both fire.
        // That is precisely the overshoot a per-arm budget would allow.
        let (mut a2, mut b2) = (armed(params("a")), armed(params("b")));
        let (mut r1, mut r2) = (26.0, 26.0);
        assert_eq!(buys(&a2.decide_fleet(&view_with_up_ask(0.94, 500.0), Ok(undecided_up_model()), 1400.0, &mut r1)).len(), 1);
        assert_eq!(buys(&b2.decide_fleet(&view_with_up_ask(0.94, 500.0), Ok(undecided_up_model()), 1400.0, &mut r2)).len(), 1);
    }

    #[test]
    fn fleet_cap_trims_a_clip_to_the_room_that_is_left() {
        // Partial room is not a block: the clip shrinks to what the pool
        // can still fund, mirroring the counterfactual's trimmed deltas.
        let mut arm = armed(params("s"));
        let mut room = 12.0;
        let out = arm.decide_fleet(&view_with_up_ask(0.94, 500.0), Ok(undecided_up_model()), 1400.0, &mut room);
        let b = buys(&out);
        assert_eq!(b.len(), 1);
        let Action::Buy { size, .. } = b[0] else { unreachable!() };
        assert_eq!(*size, 12.0, "12.00 room / 0.94 floored, not the full 26-share clip");
    }

    #[test]
    fn fleet_cap_exempts_banked_decided_fires() {
        // A fully-spent pool must not stop banked-decided capital: it was
        // never in the pool (R9 owns entry into that state), and blocking
        // it would ration exactly the exposure this cap is not about.
        let mut arm = armed(params("s"));
        let mut room = 0.0;
        let out = arm.decide_fleet(&view_with_up_ask(0.94, 500.0), Ok(locked_up_model()), 1400.0, &mut room);
        assert_eq!(buys(&out).len(), 1, "banked-decided fires through an empty fleet");
        assert_eq!(room, 0.0, "and never draws the pool down further");
        assert!(arm.last_eval.as_ref().unwrap()["sides"][0].get("brake").is_none());
    }

    #[test]
    fn fleet_cap_off_is_inert() {
        // The default path: `decide` is `decide_fleet` with an infinite
        // pool, so an unset cap changes neither the fire nor the record.
        let mut plain = armed(params("s"));
        let out = plain.decide(&view_with_up_ask(0.94, 500.0), Ok(undecided_up_model()), 1400.0);
        assert_eq!(buys(&out).len(), 1);
        let eval = plain.last_eval.as_ref().unwrap();
        assert!(eval.get("fleet_room").is_none(), "no cap, no fleet numbers in the tape");
        assert!(eval["sides"][0].get("brake").is_none());

        let mut capped = armed(params("s"));
        let mut room = f64::INFINITY;
        let out2 = capped.decide_fleet(&view_with_up_ask(0.94, 500.0), Ok(undecided_up_model()), 1400.0, &mut room);
        assert_eq!(buys(&out2), buys(&out), "an infinite pool is the same engine");
    }

    #[test]
    fn fleet_room_carries_into_the_eval_record_when_a_cap_is_set() {
        let mut arm = armed(params("s"));
        let mut room = 100.0;
        arm.decide_fleet(&view_with_up_ask(0.94, 500.0), Ok(undecided_up_model()), 1400.0, &mut room);
        let left = arm.last_eval.as_ref().unwrap()["fleet_room"].as_f64().unwrap();
        assert!((left - (100.0 - 26.0 * 0.94)).abs() < 1e-9, "post-clip headroom, so the tape can price the cap");
    }

    #[test]
    fn undecided_committed_counts_only_live_speculation() {
        let mut arm = armed(params("s"));
        arm.filled_usdc = 40.0;
        arm.inflight.insert("s-u".into(), (10.0, 1400.0));
        // Position floor wins when the tracker knows more than fills do.
        assert_eq!(arm.undecided_committed(55.0, 1400.0), 65.0);
        assert_eq!(arm.undecided_committed(10.0, 1400.0), 50.0);
        // A dead inflight entry is not exposure — same TTL decide() uses.
        assert_eq!(arm.undecided_committed(0.0, 1400.0 + INFLIGHT_TTL_S), 40.0);
        // Banked-decided capital left the pool.
        arm.last_banked_decided = true;
        assert_eq!(arm.undecided_committed(55.0, 1400.0), 0.0);
        // So did a closed window's — resolved, not speculative.
        arm.last_banked_decided = false;
        assert_eq!(arm.undecided_committed(55.0, 1500.0), 0.0);
    }

    #[test]
    fn decide_caches_banked_decidedness_for_the_fleet_pre_pass() {
        // The pre-pass reads this instead of running the model a second
        // time per tick; a gate must leave the last known value standing,
        // since a stale feed does not un-bank a window.
        let mut arm = armed(params("s"));
        assert!(!arm.last_banked_decided, "an unevaluated arm is speculative");
        arm.decide(&view_with_up_ask(0.94, 500.0), Ok(locked_up_model()), 1400.0);
        assert!(arm.last_banked_decided);
        arm.decide(&ArmView::default(), Err(GateReason::plain("feed stale")), 1400.1);
        assert!(arm.last_banked_decided, "a feed hiccup does not un-bank the window");
    }

    // --- maker step 0: the optimistic resting bid ------------------------
    //
    // The miss class: our side is bid near 1.00 and NOBODY is offering, on
    // 9.6% of armed time, median 82% through the window
    // (analysis/freq_funnel_report.md). No taker knob reaches it. These
    // tests pin the four things that keep the slice narrow — it needs the
    // missing ask, the theta evidence, an un-latched window and room inside
    // the sweep — plus the shadow record that has to work with the knob OFF.

    /// A book with a fat bid and NO offer on the up side: the exact shape
    /// the funnel charges to `book_quoted`.
    fn view_no_ask_up(bid: f64) -> ArmView {
        ArmView {
            up: TopOfBook { bid: Some((bid, 500.0)), ask: None },
            ..ArmView::default()
        }
    }

    fn maker_arm(bid: bool) -> ArmState {
        let mut p = params("s");
        p.maker_bid = bid;
        p.theta = 0.3; // resting demands a real theta; locked_up_model's safety 3.0 clears it
        armed(p)
    }

    fn maker_buys(out: &DecideOut) -> Vec<(&str, f64, f64)> {
        out.actions
            .iter()
            .filter_map(|a| match a {
                Action::Buy { token, price, size, post_only: true } => {
                    Some((token.as_str(), *price, *size))
                }
                _ => None,
            })
            .collect()
    }

    fn up_side(arm: &ArmState) -> serde_json::Value {
        arm.last_eval.as_ref().unwrap()["sides"][0].clone()
    }

    #[test]
    fn maker_rests_one_post_only_bid_when_nothing_is_offered() {
        let mut arm = maker_arm(true);
        // Bid 0.99 with no ask: fair 1.0 - min_edge 0.015 = 0.985, one tick
        // over the bid is 0.991, max_price is 0.985 — the cap binds.
        let out = arm.decide(&view_no_ask_up(0.99), Ok(locked_up_model()), 1400.0);
        let m = maker_buys(&out);
        assert_eq!(m.len(), 1, "exactly one resting bid, never a ladder");
        assert_eq!(m[0], ("s-u", 0.985, 25.0), "clip_usdc 25 / 0.985 floored");
        assert!(
            out.actions.iter().any(|a| matches!(a, Action::Cancel(t) if t == "s-u")),
            "re-quoting goes through the delta matcher, so the pass orders the cancel"
        );
        assert_eq!(arm.maker_rest.get("s-u").map(|(px, _)| *px), Some(0.985));
        let side = up_side(&arm);
        assert!(side["ask"].is_null(), "there was no ask — the record must say so");
        assert_eq!(side["maker_rest"], 0.985);
        assert!(
            side.get("maker_candidate").is_none(),
            "candidate is the shadow label; an armed slice reports what it DID"
        );
    }

    #[test]
    fn maker_bid_prices_one_tick_over_the_book_when_the_bid_is_thin() {
        let mut arm = maker_arm(true);
        let out = arm.decide(&view_no_ask_up(0.50), Ok(locked_up_model()), 1400.0);
        let m = maker_buys(&out);
        assert_eq!(m.len(), 1);
        assert_eq!(m[0].1, 0.501, "join the book +1 tick, never leap the queue");
        assert_eq!(m[0].2, 49.0, "25 / 0.501 floored");
    }

    #[test]
    fn maker_needs_the_missing_ask() {
        // The precondition IS the miss class. An offer at any price means
        // the taker path owns this moment.
        let mut arm = maker_arm(true);
        let out = arm.decide(&view_with_up_ask(0.94, 500.0), Ok(locked_up_model()), 1400.0);
        assert!(maker_buys(&out).is_empty(), "an ask exists — take it, don't quote");
        assert_eq!(buys(&out).len(), 1, "and the taker clip is untouched");
        assert!(arm.maker_rest.is_empty());
    }

    #[test]
    fn an_ask_appearing_pulls_the_resting_bid() {
        let mut arm = maker_arm(true);
        arm.decide(&view_no_ask_up(0.99), Ok(locked_up_model()), 1400.0);
        assert!(!arm.maker_rest.is_empty());
        let out = arm.decide(&view_with_up_ask(0.94, 500.0), Ok(locked_up_model()), 1400.1);
        assert!(arm.maker_rest.is_empty(), "the slice's one precondition broke");
        assert_eq!(
            out.actions.iter().filter(|a| matches!(a, Action::Cancel(t) if t == "s-u")).count(),
            1,
            "one cancel, not two — the engine batches per token and would \
             ask the CLOB to retire the same order id twice"
        );
    }

    #[test]
    fn maker_never_rests_on_a_theta_zero_arm() {
        // theta 0 makes the evidence test vacuous, so the knob demands a
        // real theta: an evidence-free resting bid is exactly the exposure
        // this slice exists to forbid.
        let mut p = params("s");
        p.maker_bid = true;
        p.theta = 0.0;
        let mut arm = armed(p);
        let out = arm.decide(&view_no_ask_up(0.99), Ok(locked_up_model()), 1400.0);
        assert!(maker_buys(&out).is_empty());
        assert!(arm.maker_rest.is_empty());
    }

    #[test]
    fn maker_needs_theta_evidence_on_that_side() {
        let mut p = params("s");
        p.maker_bid = true;
        p.theta = 0.3;
        let mut arm = armed(p);
        // safety 0.5/5.0 = 0.1: the clock says go, the evidence does not
        // (docs/LESSONS.md#L13). Unlike entry's first-clip-only gate, a
        // resting bid re-tests this on every quote.
        let weak = ModelEval { banked_margin_bp: 0.5, cushion_bp: 5.0, ..locked_up_model() };
        let out = arm.decide(&view_no_ask_up(0.99), Ok(weak), 1400.0);
        assert!(maker_buys(&out).is_empty());
        assert!(arm.maker_rest.is_empty());
        assert!(
            arm.last_eval.as_ref().unwrap()["sides"].as_array().unwrap().is_empty(),
            "a blocked slice is not even a candidate"
        );
        // Evidence arrives: 2.0/5.0 = 0.4 clears theta 0.3.
        let strong = ModelEval { banked_margin_bp: 2.0, cushion_bp: 5.0, ..locked_up_model() };
        let out = arm.decide(&view_no_ask_up(0.99), Ok(strong), 1400.1);
        assert_eq!(maker_buys(&out).len(), 1);
    }

    #[test]
    fn maker_is_blocked_by_the_brake_latch() {
        // Flat, unlike the taker latch: banked-decided arithmetic earns a
        // clip its carve-out because a real ask prices it, and a resting
        // bid has no such witness.
        let mut arm = maker_arm(true);
        arm.brake_latched = true;
        let out = arm.decide(&view_no_ask_up(0.99), Ok(locked_up_model()), 1400.0);
        assert!(maker_buys(&out).is_empty(), "a window that went wrong stays wrong");
        assert!(arm.maker_rest.is_empty());
    }

    #[test]
    fn maker_is_blocked_by_the_avg_down_brake() {
        let mut arm = maker_arm(true);
        // Last clip on this token paid 0.60; a 0.501 bid is the market
        // repricing against the thesis, not a discount.
        arm.last_clip_ask.insert("s-u".into(), 0.60);
        let undecided = ModelEval { banked_decided: false, ..locked_up_model() };
        let out = arm.decide(&view_no_ask_up(0.50), Ok(undecided), 1400.0);
        assert!(maker_buys(&out).is_empty());
    }

    #[test]
    fn the_quiesce_sweep_pulls_a_resting_bid_like_any_other_order() {
        let mut arm = maker_arm(true);
        arm.decide(&view_no_ask_up(0.99), Ok(locked_up_model()), 1400.0);
        assert!(!arm.maker_rest.is_empty());
        // end 1500, quiesce 20 -> the sweep owns everything past 1480.
        let out = arm.decide(&view_no_ask_up(0.99), Ok(locked_up_model()), 1490.0);
        assert!(arm.maker_rest.is_empty(), "quiesce leaves nothing standing");
        assert!(maker_buys(&out).is_empty());
        assert!(out.actions.iter().any(|a| matches!(a, Action::Cancel(t) if t == "s-u")));
    }

    #[test]
    fn a_gate_pulls_a_resting_bid_off_the_book() {
        // "No trade" has to reach the quote already standing, not just the
        // next one — a stale feed cannot say the bid is still priced right.
        let mut arm = maker_arm(true);
        arm.decide(&view_no_ask_up(0.99), Ok(locked_up_model()), 1400.0);
        let out = arm.decide(
            &view_no_ask_up(0.99),
            Err(GateReason::plain("feed stale")),
            1400.1,
        );
        assert!(arm.maker_rest.is_empty());
        assert!(out.actions.iter().any(|a| matches!(a, Action::Cancel(t) if t == "s-u")));
    }

    #[test]
    fn a_resting_bid_is_requoted_only_when_its_price_moves() {
        let mut arm = maker_arm(true);
        arm.decide(&view_no_ask_up(0.50), Ok(locked_up_model()), 1400.0);
        // Same book: no cancel, no replace. A re-quote restarts the
        // order-age clock and spends a placement token for nothing.
        let held = arm.decide(&view_no_ask_up(0.50), Ok(locked_up_model()), 1400.1);
        assert!(held.actions.is_empty(), "an unchanged quote is left alone");
        assert_eq!(arm.maker_rest.get("s-u").map(|(px, _)| *px), Some(0.501));
        // The book moves: re-quote through the delta matcher.
        let moved = arm.decide(&view_no_ask_up(0.60), Ok(locked_up_model()), 1400.2);
        assert_eq!(maker_buys(&moved)[0].1, 0.601);
        assert!(moved.actions.iter().any(|a| matches!(a, Action::Cancel(t) if t == "s-u")));
    }

    #[test]
    fn a_resting_bid_is_committed_notional_like_a_clip() {
        // The whole point of the accounting: a post-only bid is un-decided
        // speculative exposure from the moment it RESTS, not from the
        // moment it fills. R7's pool and the arm's own budget both see it.
        let mut arm = maker_arm(true);
        let undecided = ModelEval { banked_decided: false, ..locked_up_model() };
        arm.decide(&view_no_ask_up(0.50), Ok(undecided.clone()), 1400.0);
        let notional = 49.0 * 0.501;
        assert!((arm.resting_usdc() - notional).abs() < 1e-9);
        assert!(
            (arm.undecided_committed(0.0, 1400.0) - notional).abs() < 1e-9,
            "the fleet pre-pass counts it"
        );
        // And the arm's own budget shrank by exactly that much.
        let out = arm.decide(&view_no_ask_up(0.50), Ok(undecided), 1400.1);
        assert!(out.actions.is_empty());
        let eval = arm.last_eval.as_ref().unwrap();
        assert!((eval["budget"].as_f64().unwrap() - (100.0 - notional)).abs() < 1e-9);
        assert!((eval["resting"].as_f64().unwrap() - notional).abs() < 1e-9);
    }

    #[test]
    fn the_fleet_cap_rations_resting_bids_and_clips_from_one_pool() {
        // Arm A rests a bid; arm B in the SAME tick finds the pool spent.
        let (mut a, mut b) = (maker_arm(true), maker_arm(true));
        b.p.slug = "b".into();
        let mut room = 26.0;
        let undecided = ModelEval { banked_decided: false, ..locked_up_model() };
        let out_a = a.decide_fleet(&view_no_ask_up(0.99), Ok(undecided.clone()), 1400.0, &mut room);
        assert_eq!(maker_buys(&out_a).len(), 1);
        assert!((room - (26.0 - 25.0 * 0.985)).abs() < 1e-9, "the quote drew the pool down in place");
        let out_b = b.decide_fleet(&view_no_ask_up(0.99), Ok(undecided), 1400.0, &mut room);
        assert!(maker_buys(&out_b).is_empty(), "a resting bid is rationed like a clip");
    }

    #[test]
    fn maker_bid_off_is_inert() {
        // The R7 wrapper contract: with the knob absent, the pass emits
        // byte-identical actions and holds no maker state. Only the shadow
        // label lands, and only on the eval.
        let mut dark = maker_arm(false);
        let out = dark.decide(&view_no_ask_up(0.99), Ok(locked_up_model()), 1400.0);
        assert!(out.actions.is_empty(), "a dark slice never touches the book");
        assert!(dark.maker_rest.is_empty());
        assert!(
            dark.last_eval.as_ref().unwrap().get("resting").is_none(),
            "no quote, no resting notional in the record"
        );

        // And the taker path is identical either way.
        let (mut lit, mut off) = (maker_arm(true), maker_arm(false));
        let a = lit.decide(&view_with_up_ask(0.94, 500.0), Ok(locked_up_model()), 1400.0);
        let b = off.decide(&view_with_up_ask(0.94, 500.0), Ok(locked_up_model()), 1400.0);
        assert_eq!(a.actions, b.actions, "the knob must not perturb a clip");
    }

    #[test]
    fn the_shadow_record_prices_the_slice_before_it_is_armed() {
        // This is the measurement the operator arms on: with maker_bid OFF,
        // a moment the slice WOULD have quoted still writes a record, so
        // the opportunity can be counted before any capital rides on it.
        let mut dark = maker_arm(false);
        let out = dark.decide(&view_no_ask_up(0.99), Ok(locked_up_model()), 1400.0);
        assert!(out.actions.is_empty());
        let side = up_side(&dark);
        assert_eq!(side["side"], "up");
        assert_eq!(side["maker_candidate"], true);
        assert_eq!(side["maker_px"], 0.985);
        assert_eq!(side["maker_size"], 25.0);
        assert!(
            side["ask"].is_null(),
            "ask stays null so the funnel still charges this to book_quoted"
        );
        assert!(side.get("maker_rest").is_none(), "nothing rested");
    }

    #[test]
    fn maker_bid_round_trips_through_arm_state_and_an_old_file_reads_as_off() {
        // Same forward-compat contract as `feed` and the fleet cap: no
        // version bump, an old file recovers with the slice dark, and an
        // older binary ignores the field instead of stranding a live window.
        let (store, path) = tmp_store("makerbid");
        let mut s = Updown::with_store(store);
        let (slug, mut cmd) = arm_cmd(-30.0, true);
        cmd["maker_bid"] = serde_json::json!(true);
        s.on_command(&cmd).unwrap();

        let mut fresh = Updown::with_store(ArmStore::at(path));
        fresh.recover(unix_now());
        assert!(fresh.arms.get(&slug).expect("recovered").p.maker_bid);

        let (_store2, old_path) = tmp_store("makerbid-oldfile");
        let start = (unix_now() - 30.0) as i64;
        let old_slug = format!("btc-updown-5m-{}", start);
        std::fs::write(&old_path, serde_json::json!({
            "version": 1, "written_at": 0.0, "rolls": [],
            "arms": [{
                "slug": old_slug, "kind": "twap", "symbol": "BTCUSDT",
                "token_up": format!("{}-u", old_slug), "token_down": format!("{}-d", old_slug),
                "start": start as f64, "end": (start + 300) as f64,
                "sigma_bp_per_min": 3.0, "fee_rate": 0.07, "size_usdc": 250.0,
            }],
        }).to_string()).unwrap();
        let mut old = Updown::with_store(ArmStore::at(old_path));
        old.recover(unix_now());
        assert!(!old.arms.get(&old_slug).expect("still recovers").p.maker_bid);
    }

    #[test]
    fn a_resting_bid_becomes_a_low_urgency_signal_and_a_clip_stays_high() {
        // Urgency is the only channel the engine has for post-only.
        let rest = to_signal(Action::Buy {
            token: "t".into(), price: 0.985, size: 25.0, post_only: true,
        });
        assert!(matches!(rest, Signal::Buy { urgency: Urgency::Low, .. }));
        let clip = to_signal(Action::Buy {
            token: "t".into(), price: 0.94, size: 26.0, post_only: false,
        });
        assert!(matches!(clip, Signal::Buy { urgency: Urgency::High, .. }));
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
        assert!(err.reason.contains("basis guard"), "{}", err.reason);
        assert!(err.margin_bp.is_some(), "the gate carries its margin as a field");
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
        assert!(err.reason.contains("basis guard"), "{}", err.reason);
        assert_eq!(err.guard_bp, Some(9.0), "the raised guard is reported structurally");
    }

    #[test]
    fn twap_stale_feed_refuses() {
        let (arm, _) = armed_with_feed(101.0, 101.0);
        let err = arm.fair_p_up(1400.0 + MAX_SPOT_AGE_S + 1.0).unwrap_err();
        assert!(err.reason.contains("stale"), "{}", err.reason);
        assert_eq!(err.margin_bp, None, "a stale feed has no margin to report");
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

    // --- arm-state survivorship (updown_state.rs): the strategy-level half.
    // The store, the recovery plan, and the unmanaged-position matching are
    // unit-tested there; these drive the whole loop through `Updown`.

    fn tmp_store(name: &str) -> (ArmStore, std::path::PathBuf) {
        let dir = std::env::temp_dir()
            .join(format!("pmengine-updown-{}-{}", name, std::process::id()));
        std::fs::create_dir_all(&dir).unwrap();
        let path = dir.join("arms-state.json");
        let _ = std::fs::remove_file(&path);
        (ArmStore::at(path.clone()), path)
    }

    /// A live-shaped arm command on a window opening `offset_s` from now.
    /// Negative offsets build a window that already closed.
    fn arm_cmd(offset_s: f64, roll: bool) -> (String, serde_json::Value) {
        let start = (unix_now() + offset_s) as i64;
        let slug = format!("btc-updown-5m-{}", start);
        let cmd = serde_json::json!({
            "action": "arm", "slug": slug, "kind": "twap", "symbol": "BTCUSDT",
            "token_up": format!("{}-u", slug), "token_down": format!("{}-d", slug),
            "start": start as f64, "end": (start + 300) as f64,
            "sigma_bp_per_min": 3.0, "fee_rate": 0.07, "size_usdc": 250.0,
            "roll": roll,
        });
        (slug, cmd)
    }

    #[test]
    fn arm_persists_and_a_fresh_strategy_recovers_it() {
        let (store, path) = tmp_store("roundtrip");
        let mut s = Updown::with_store(store);
        let (slug, cmd) = arm_cmd(-30.0, true);
        s.on_command(&cmd).unwrap();
        assert!(path.exists(), "arming writes durable state");

        // The crash: everything in memory is gone, the file is not.
        let mut fresh = Updown::with_store(ArmStore::at(path));
        fresh.recover(unix_now());
        assert_eq!(fresh.arms.len(), 1);
        let arm = fresh.arms.get(&slug).expect("the open window came back");
        assert_eq!(arm.p.token_up, format!("{}-u", slug), "token ids survive — no gamma call");
        assert_eq!(arm.p.size_usdc, 250.0);
        assert!(arm.p.roll);
        assert_eq!(fresh.subscriptions().len(), 2, "recovered tokens are re-subscribed");
    }

    #[test]
    fn disarm_deletes_the_persisted_arm() {
        // The money bug this guards: a disarmed market that resurrects on
        // the next restart and starts buying again.
        let (store, path) = tmp_store("disarm");
        let mut s = Updown::with_store(store);
        let (slug, cmd) = arm_cmd(-30.0, true);
        s.on_command(&cmd).unwrap();
        s.on_command(&serde_json::json!({"action": "disarm", "slug": slug}))
            .unwrap();
        assert!(!path.exists(), "the last arm out clears the file");

        let mut fresh = Updown::with_store(ArmStore::at(path));
        fresh.recover(unix_now());
        assert!(fresh.arms.is_empty(), "a disarm must never resurrect");
        assert!(fresh.rolls.is_empty(), "nor its roll chain");
    }

    #[test]
    fn disarming_one_arm_leaves_the_others_recoverable() {
        let (store, path) = tmp_store("disarm-one");
        let mut s = Updown::with_store(store);
        let (kept, keep_cmd) = arm_cmd(-30.0, true);
        let (dropped, drop_cmd) = arm_cmd(-90.0, true);
        s.on_command(&keep_cmd).unwrap();
        s.on_command(&drop_cmd).unwrap();
        s.on_command(&serde_json::json!({"action": "disarm", "slug": dropped}))
            .unwrap();

        let mut fresh = Updown::with_store(ArmStore::at(path));
        fresh.recover(unix_now());
        assert_eq!(fresh.arms.len(), 1);
        assert!(fresh.arms.contains_key(&kept));
    }

    #[test]
    fn recovery_hops_a_closed_roll_window_and_drops_a_stale_one() {
        let (store, path) = tmp_store("hop");
        let mut s = Updown::with_store(store);
        // Both windows closed while we were down: one rolls, one doesn't.
        let (rolling, roll_cmd) = arm_cmd(-600.0, true);
        let (_stale, stale_cmd) = arm_cmd(-900.0, false);
        // on_command refuses a dead window, so seed the file the way a
        // mid-window crash would have left it.
        let rolling_p: ArmParams = serde_json::from_value(roll_cmd).unwrap();
        let stale_p: ArmParams = serde_json::from_value(stale_cmd).unwrap();
        s.arms.insert(rolling.clone(), ArmState::with_params(rolling_p.clone()));
        s.arms.insert(stale_p.slug.clone(), ArmState::with_params(stale_p));
        s.persist(unix_now());

        let mut fresh = Updown::with_store(ArmStore::at(path.clone()));
        fresh.recover(unix_now());
        assert!(fresh.arms.is_empty(), "no dead window is ever re-armed");
        assert_eq!(fresh.rolls.len(), 1, "only the rolling arm leaves a successor");
        assert_eq!(
            fresh.rolls[0].next_slug,
            format!("btc-updown-5m-{}", rolling_p.end as i64),
            "the chain resumes at the immediate successor; process_rolls walks it forward"
        );
        // The rewrite dropped both stale entries and kept the live roll task.
        let back = ArmStore::at(path).load().expect("state rewritten, not deleted");
        assert!(back.arms.is_empty());
        assert_eq!(back.rolls.len(), 1);
    }

    #[test]
    fn recovery_is_inert_without_a_file() {
        let (store, path) = tmp_store("inert");
        assert!(!path.exists());
        let mut s = Updown::with_store(store);
        s.recover(unix_now());
        assert!(s.arms.is_empty());
        assert!(s.rolls.is_empty());
        assert!(!path.exists(), "an empty strategy writes nothing");
    }

    #[test]
    fn scheduled_rolls_persist_across_a_restart() {
        let (store, path) = tmp_store("rollpersist");
        let mut s = Updown::with_store(store);
        let (_slug, cmd) = arm_cmd(-30.0, true);
        let p: ArmParams = serde_json::from_value(cmd).unwrap();
        s.rolls.push(RollTask {
            params: p.clone(),
            next_slug: format!("btc-updown-5m-{}", p.end as i64),
            next_start: p.end,
            next_end: p.end + 300.0,
            next_try_at: 0.0,
        });
        s.persist(unix_now());

        let mut fresh = Updown::with_store(ArmStore::at(path));
        fresh.recover(unix_now());
        assert_eq!(fresh.rolls.len(), 1, "a pending roll survives the restart");
        assert_eq!(fresh.rolls[0].next_slug, format!("btc-updown-5m-{}", p.end as i64));
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

    #[test]
    fn fleet_cap_round_trips_through_arm_state() {
        // The cap is a fleet-level ration, so it has to survive a restart
        // the way the arms do — a reboot that came back uncapped would
        // quietly undo it right when the roll chain re-arms everything.
        let (store, path) = tmp_store("fleetcap");
        let mut s = Updown::with_store(store);
        let (_slug, cmd) = arm_cmd(-30.0, true);
        s.on_command(&cmd).unwrap();
        let r = s
            .on_command(&serde_json::json!({"action": "fleet", "undecided_cap_usdc": 250.0}))
            .unwrap();
        assert_eq!(r["undecided_cap_usdc"], 250.0);
        assert_eq!(r["enabled"], true);

        let mut fresh = Updown::with_store(ArmStore::at(path.clone()));
        fresh.recover(unix_now());
        assert_eq!(fresh.fleet_undecided_cap, 250.0);
        assert_eq!(fresh.arms.len(), 1, "the arms came back with it");
        assert_eq!(fresh.on_command(&serde_json::json!({"action": "status"})).unwrap()
                       ["fleet_undecided_cap"], 250.0);

        // Turning it off is a real state too: the file must not hand the
        // old cap back on the next restart.
        fresh.on_command(&serde_json::json!({"action": "fleet", "undecided_cap_usdc": 0})).unwrap();
        let mut off = Updown::with_store(ArmStore::at(path));
        off.recover(unix_now());
        assert_eq!(off.fleet_undecided_cap, 0.0);
    }

    #[test]
    fn a_bare_fleet_cap_persists_without_any_arms() {
        // "absent = inert" still holds, but a cap alone is not nothing —
        // the operator may ration the fleet before arming it.
        let (store, path) = tmp_store("fleetcap-bare");
        let mut s = Updown::with_store(store);
        s.on_command(&serde_json::json!({"action": "fleet", "undecided_cap_usdc": 300.0}))
            .unwrap();
        assert!(path.exists(), "a cap with no arms is still state worth keeping");

        let mut fresh = Updown::with_store(ArmStore::at(path.clone()));
        fresh.recover(unix_now());
        assert_eq!(fresh.fleet_undecided_cap, 300.0);
        assert!(fresh.arms.is_empty());

        fresh.on_command(&serde_json::json!({"action": "fleet"})).unwrap();
        assert!(!path.exists(), "cap off and no arms clears the file again");
    }

    #[test]
    fn a_pre_r7_state_file_recovers_as_uncapped() {
        // Forward compat both ways: no version bump, so an old file loads
        // and simply means "no cap" rather than being refused (which would
        // strand a live window on the way back up).
        let (_store, path) = tmp_store("fleetcap-oldfile");
        let start = (unix_now() - 30.0) as i64;
        let slug = format!("btc-updown-5m-{}", start);
        std::fs::write(&path, serde_json::json!({
            "version": 1, "written_at": 0.0, "rolls": [],
            "arms": [{
                "slug": slug, "kind": "twap", "symbol": "BTCUSDT",
                "token_up": format!("{}-u", slug), "token_down": format!("{}-d", slug),
                "start": start as f64, "end": (start + 300) as f64,
                "sigma_bp_per_min": 3.0, "fee_rate": 0.07, "size_usdc": 250.0,
            }],
        }).to_string()).unwrap();

        let mut s = Updown::with_store(ArmStore::at(path));
        s.recover(unix_now());
        assert_eq!(s.arms.len(), 1, "a pre-R7 file still recovers its arms");
        assert_eq!(s.fleet_undecided_cap, 0.0);
    }

    // --- feed selection (updown_rtds.rs): the strategy-level half. The
    // supervisor, its routing and its reconnect are unit-tested there;
    // these pin the seam — what an arm accepts, what it persists, and that
    // a stream-fed FeedState is the same object every gate already knows.

    /// A twap arm on a 5m window (so the settlement width is 30s) fed by
    /// the stream, wired to a hub the test drives by hand.
    fn rtds_armed(hub: &RtdsHub) -> (ArmState, f64, f64) {
        let (start, end) = (600.0, 900.0);
        let mut p = params("s");
        p.symbol = "XRPUSDT".into();
        p.feed = FEED_RTDS.into();
        p.start = start;
        p.end = end;
        let mut arm = ArmState::with_params(p);
        arm.subscribed = true;
        arm.rtds_sub = Some(hub.register(
            "xrp/usd",
            settle_tw_secs(end - start),
            start,
            arm.feed.clone(),
        ));
        (arm, start, end)
    }

    #[test]
    fn feed_defaults_to_binance_and_only_knows_two_names() {
        assert_eq!(params("s").feed, FEED_BINANCE, "absent = today's engine");
        let mut p = params("s");
        p.feed = FEED_RTDS.into();
        p.symbol = "XRPUSDT".into();
        assert!(check_feed(&p).is_ok());
        p.feed = "coinbase".into();
        assert!(check_feed(&p).unwrap_err().contains("unknown feed"));
    }

    #[test]
    fn rtds_refuses_close_open_and_symbols_the_stream_does_not_carry() {
        // close_open prices off a venue's 1h candle open; the settlement
        // stream has no candles, so the model would have nothing to read.
        let mut p = params("s");
        p.feed = FEED_RTDS.into();
        p.symbol = "XRPUSDT".into();
        p.kind = "close_open".into();
        let err = check_feed(&p).unwrap_err();
        assert!(err.contains("close_open"), "{err}");
        assert!(err.contains("--feed binance"), "the error says what to do instead: {err}");
        // And a symbol the stream doesn't serve is refused loudly rather
        // than arming a window that would sit gated forever.
        p.kind = "twap".into();
        p.symbol = "PEPEUSDT".into();
        let err = check_feed(&p).unwrap_err();
        assert!(err.contains("does not carry PEPEUSDT"), "{err}");
        assert!(err.contains("xrp/usd"), "and lists what it does carry: {err}");
    }

    #[test]
    fn arm_command_rejects_close_open_on_rtds() {
        let mut s = Updown::new();
        let start = (unix_now() + 60.0) as i64;
        let res = s.on_command(&serde_json::json!({
            "action": "arm", "slug": format!("btc-updown-1h-{}", start),
            "kind": "close_open", "symbol": "BTCUSDT",
            "token_up": "1", "token_down": "2",
            "start": start as f64, "end": (start + 3600) as f64,
            "sigma_bp_per_min": 3.0, "fee_rate": 0.07, "size_usdc": 100.0,
            "feed": "rtds",
        }));
        assert!(res.is_err(), "close_open on the settlement stream must not arm");
        assert!(s.arms.is_empty());
    }

    #[test]
    fn feed_round_trips_through_arm_state_and_an_old_file_reads_as_binance() {
        // Forward compat both ways, exactly like the fleet cap: no version
        // bump, so a file written before this feature recovers its arms and
        // simply means "binance", and an older binary ignores the new field
        // instead of refusing the state and stranding a live window.
        let (store, path) = tmp_store("feedfield");
        let mut s = Updown::with_store(store);
        let (slug, mut cmd) = arm_cmd(-30.0, true);
        cmd["symbol"] = serde_json::json!("XRPUSDT");
        cmd["feed"] = serde_json::json!("rtds");
        s.on_command(&cmd).unwrap();

        let mut fresh = Updown::with_store(ArmStore::at(path));
        fresh.recover(unix_now());
        assert_eq!(fresh.arms.get(&slug).expect("recovered").p.feed, FEED_RTDS);
        assert_eq!(
            fresh.on_command(&serde_json::json!({"action": "status"})).unwrap()["arms"][&slug]
                ["feed"],
            "rtds",
            "and status says which feed an arm is on"
        );

        // A pre-feature file: no `feed` key at all.
        let (_store2, old_path) = tmp_store("feedfield-oldfile");
        let start = (unix_now() - 30.0) as i64;
        let old_slug = format!("btc-updown-5m-{}", start);
        std::fs::write(&old_path, serde_json::json!({
            "version": 1, "written_at": 0.0, "rolls": [],
            "arms": [{
                "slug": old_slug, "kind": "twap", "symbol": "BTCUSDT",
                "token_up": format!("{}-u", old_slug), "token_down": format!("{}-d", old_slug),
                "start": start as f64, "end": (start + 300) as f64,
                "sigma_bp_per_min": 3.0, "fee_rate": 0.07, "size_usdc": 250.0,
            }],
        }).to_string()).unwrap();
        let mut old = Updown::with_store(ArmStore::at(old_path));
        old.recover(unix_now());
        assert_eq!(old.arms.get(&old_slug).expect("still recovers").p.feed, FEED_BINANCE);
    }

    #[test]
    fn an_unusable_feed_starts_nothing_and_gates_forever() {
        // The recovery path takes no commands, so a hand-edited or
        // corrupted state file is the one way an unknown feed reaches an
        // arm. Falling through to Binance would trade a guard sized for
        // some other feed; starting nothing gates on a stale spot instead.
        let mut p = params("s");
        p.feed = "coinbase".into();
        let mut arm = ArmState::with_params(p);
        arm.start_feeds(&RtdsHub::new());
        assert!(arm.feed_handles.is_empty(), "no feed threads on an unknown feed");
        assert!(arm.rtds_sub.is_none());
        let err = arm.fair_p_up(1400.0).unwrap_err();
        assert!(err.reason.contains("stale"), "{}", err.reason);
        assert!(err.reason.contains("coinbase"), "and names the cause: {}", err.reason);

        // Same for rtds on a symbol the stream doesn't carry.
        let mut p = params("s");
        p.feed = FEED_RTDS.into();
        p.symbol = "PEPEUSDT".into();
        let mut arm = ArmState::with_params(p);
        arm.start_feeds(&RtdsHub::new());
        assert!(arm.rtds_sub.is_none());
        assert!(arm.fair_p_up(1400.0).unwrap_err().reason.contains("PEPEUSDT"));
    }

    #[test]
    fn a_roll_keeps_the_arms_feed() {
        // Roll chains clone params forward; an rtds arm that silently
        // rolled onto Binance would trade xrp through the basis this
        // feature exists to stop reading.
        let mut p = params("btc-updown-5m-1787442000");
        p.feed = FEED_RTDS.into();
        p.start = 1787442000.0;
        p.end = 1787442300.0;
        let task = RollTask {
            params: p.clone(),
            next_slug: "btc-updown-5m-1787442300".into(),
            next_start: 1787442300.0,
            next_end: 1787442600.0,
            next_try_at: 0.0,
        };
        assert_eq!(task.record().params.feed, FEED_RTDS);
    }

    #[test]
    fn rtds_fills_the_same_feedstate_contract_the_model_already_reads() {
        // The point of the whole feature: eval_model needs no knowledge of
        // where its numbers came from. The stream supplies the range-start
        // reference (the settlement print AT the window start, not a
        // Binance proxy for it), spot, and the closes behind sigma/rho.
        let hub = RtdsHub::new();
        let (arm, start, _end) = rtds_armed(&hub);

        // The 30s-TWAP print at the window's start instant IS the reference.
        hub.ingest_price(updown_rtds::TOPIC_TWAP30, "xrp/usd", start as i64, 2.50, start);
        // 1Hz spot, +20bp above the reference.
        hub.ingest_price(updown_rtds::TOPIC_SPOT, "xrp/usd", 700, 2.505, 700.0);

        let m = arm.fair_p_up(700.0).expect("prices off the stream");
        assert!((m.margin_bp - 20.0).abs() < 0.5, "margin {:.1}bp", m.margin_bp);
        assert!(m.p_up > 0.99, "+20bp with 100s left and 3bp/min vol: {}", m.p_up);
        assert_eq!(
            m.guard_bp, arm.p.basis_guard_bp,
            "no oracle poller on this feed — the operator's param governs"
        );
        // The 60s topic belongs to longer windows and must not touch a 5m
        // arm's reference.
        hub.ingest_price(updown_rtds::TOPIC_TWAP60, "xrp/usd", start as i64, 9.99, start);
        assert_eq!(arm.feed.lock().unwrap().per_min.get(&(start as i64 - 60)), Some(&2.50));
    }

    #[test]
    fn a_dead_stream_gates_an_rtds_arm_exactly_like_a_dead_binance_feed() {
        // The basis guard's cross-venue job is gone on this feed; what is
        // left is staleness, and MAX_SPOT_AGE_S must bind here unchanged.
        // A dropped socket stops refreshing spot_ts, so the arm gates
        // within seconds and quiesce still pulls its orders.
        let hub = RtdsHub::new();
        let (mut arm, start, end) = rtds_armed(&hub);
        hub.ingest_price(updown_rtds::TOPIC_TWAP30, "xrp/usd", start as i64, 2.50, start);
        hub.ingest_price(updown_rtds::TOPIC_SPOT, "xrp/usd", 700, 2.505, 700.0);
        assert!(arm.fair_p_up(700.0).is_ok());

        // ... and then the socket dies. Nothing arrives; the clock moves.
        let dead_at = 700.0 + MAX_SPOT_AGE_S + 1.0;
        let gate = arm.fair_p_up(dead_at).unwrap_err();
        assert!(gate.reason.contains("stale"), "{}", gate.reason);
        assert_eq!(gate.margin_bp, None, "a stale feed has no margin to report");

        // The supervisor's error rides along so the gate line names the
        // feed instead of leaving the operator guessing.
        arm.feed.lock().unwrap().last_err = Some("rtds read: connection reset".into());
        let named = arm.fair_p_up(dead_at).unwrap_err();
        assert!(named.reason.contains("rtds"), "{}", named.reason);

        // No trade on a dead feed, and quiesce still pulls the book.
        let out = arm.decide(&view_with_up_ask(0.94, 500.0), Err(named), dead_at);
        assert!(buys(&out).is_empty());
        let quiesce = arm.fair_p_up(end - 5.0).unwrap_err();
        let out = arm.decide(&view_with_up_ask(0.94, 500.0), Err(quiesce), end - 5.0);
        assert!(buys(&out).is_empty(), "no buys through quiesce on a dead feed");
        assert_eq!(
            out.actions.iter().filter(|a| matches!(a, Action::Cancel(_))).count(),
            2,
            "both sides pulled"
        );
    }

    #[test]
    fn fleet_command_rejects_a_non_numeric_cap() {
        let mut s = Updown::new();
        assert!(s
            .on_command(&serde_json::json!({"action": "fleet", "undecided_cap_usdc": "250"}))
            .is_err());
        assert_eq!(s.fleet_undecided_cap, 0.0, "a bad command leaves the fleet as it was");
    }
}
