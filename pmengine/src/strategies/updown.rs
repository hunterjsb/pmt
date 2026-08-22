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
/// One order in flight per token; assume dead if no fill inside this window.
/// Must outlive the engine's ~5s position-reconcile cadence: taker fills
/// are often MISSED by the realtime fill path and only show up via
/// reconcile (proven live), so freeing budget sooner buys fills twice.
const INFLIGHT_TTL_S: f64 = 12.0;
/// Speculative clips still need the model leaning clearly one way.
const EARLY_MIN_FAIR: f64 = 0.55;

// Private on purpose: the registry generator registers the first `pub
// struct` in the file, which must be Updown.
#[derive(Debug, Clone, Deserialize)]
struct ArmParams {
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
    /// Elapsed fraction where the full budget unlocks regardless of
    /// banked-decidedness.
    #[serde(default = "d_late_frac")]
    pub late_frac: f64,
    /// Lag-1 autocorrelation of 1m returns below which the tape counts as
    /// mean-reverting chop: speculative clips are disabled entirely.
    #[serde(default = "d_rho_block")]
    pub rho_block: f64,
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
fn d_rho_block() -> f64 { -0.25 }
fn d_manip_push() -> f64 { 25.0 }
/// Flip-proof buys stay live until this close to resolution.
const FLIP_BUY_CUTOFF_S: f64 = 8.0;

/// Shared state an arm's Binance feed threads keep warm.
#[derive(Default)]
struct FeedState {
    spot: f64,
    spot_ts: f64,
    /// minute epoch -> (open+close)/2, proxying the Chainlink 60s TWAP.
    per_min: std::collections::BTreeMap<i64, f64>,
    /// Exact 1h candle open once the close_open window starts.
    candle_open: Option<f64>,
    /// Recent 1m closes, oldest first — feeds fast vol + regime autocorr.
    closes: Vec<f64>,
    /// Lag-1 autocorrelation of trailing 1m returns. Negative = the tape
    /// fades its own moves; momentum "locks" are mirages there.
    rho: f64,
    last_err: Option<String>,
}

/// One model read: everything the clip engine needs to pick a mode.
#[derive(Debug)]
struct ModelEval {
    p_up: f64,
    sig_bp: f64,
    /// twap only: the banked contribution alone decides the window even if
    /// the remaining path fully reverts, with basis + vol cushion. The one
    /// entry condition immune to mean reversion.
    banked_decided: bool,
    /// Stronger: the banked margin survives even an adversarial spot push
    /// (manip_push_bp) sustained for the whole remaining window. When true,
    /// the book's late panic/push prices are free money — nobody can flip
    /// this TWAP anymore.
    flip_proof: bool,
    rho: f64,
}

/// Everything one hunted window owns: params, feeds, budget, clip clocks.
struct ArmState {
    p: ArmParams,
    feed: Arc<Mutex<FeedState>>,
    feed_stop: Arc<AtomicBool>,
    feed_handles: Vec<std::thread::JoinHandle<()>>,
    subscribed: bool,
    cleaned: bool,
    filled_usdc: f64,
    /// token -> (notional, emitted_at) for the one order allowed in flight.
    inflight: std::collections::HashMap<String, (f64, f64)>,
    /// token -> last clip time, enforcing the per-side clip cadence.
    last_clip: std::collections::HashMap<String, f64>,
    last_eval: Option<serde_json::Value>,
    /// Throttle for eval/gated lines in the durable tape.
    last_tape_at: f64,
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

impl ArmState {
    /// Build without feeds (tests use this directly).
    fn with_params(p: ArmParams) -> Self {
        Self {
            p,
            feed: Arc::new(Mutex::new(FeedState::default())),
            feed_stop: Arc::new(AtomicBool::new(false)),
            feed_handles: Vec::new(),
            subscribed: false,
            cleaned: false,
            filled_usdc: 0.0,
            inflight: std::collections::HashMap::new(),
            last_clip: std::collections::HashMap::new(),
            last_eval: None,
            last_tape_at: 0.0,
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
        let p = &self.p;
        let f = self.feed.lock().unwrap();
        if now - f.spot_ts > MAX_SPOT_AGE_S {
            return Err(match &f.last_err {
                Some(e) => format!("feed stale: {}", e),
                None => "feed stale".to_string(),
            });
        }
        let spot = f.spot;
        // Vol only ratchets UP: the arm-time trailing sigma is a floor, the
        // fast window catches the storm the trailing estimate lags.
        let fast_bp = {
            let c = &f.closes;
            let n = c.len().min(VOL_FAST_WINDOW + 1);
            if n >= 4 {
                let rets: Vec<f64> =
                    c[c.len() - n..].windows(2).map(|w| (w[1] / w[0]).ln()).collect();
                let mu = rets.iter().sum::<f64>() / rets.len() as f64;
                let var =
                    rets.iter().map(|r| (r - mu).powi(2)).sum::<f64>() / (rets.len() - 1) as f64;
                var.sqrt() * 1e4
            } else {
                0.0
            }
        };
        let sig_bp = p.sigma_bp_per_min.max(fast_bp);
        let sig_frac = sig_bp / 1e4;
        let rho = f.rho;

        if p.kind == "close_open" {
            let open = f.candle_open.ok_or("candle open not printed yet")?;
            let t_min = ((p.end - now) / 60.0).max(0.005);
            let z = (spot / open).ln() / (sig_frac * t_min.sqrt());
            Ok(ModelEval { p_up: norm_cdf(z), sig_bp, banked_decided: false, flip_proof: false, rho })
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
                return Ok(ModelEval { p_up, sig_bp, banked_decided: true, flip_proof: true, rho });
            }
            let proj = (banked_avg * banked_s + spot * rem) / window;
            let margin_bp = (proj / ref_px - 1.0) * 1e4;
            if margin_bp.abs() < p.basis_guard_bp {
                return Err(format!(
                    "basis guard: projected margin {:+.1}bp inside {:.1}bp noise band",
                    margin_bp, p.basis_guard_bp
                ));
            }
            let breakeven = (ref_px * window - banked_avg * banked_s) / rem;
            let sig_avg = sig_frac * ((rem / 60.0).max(0.02) / 3.0).sqrt();
            let p_up = 1.0 - norm_cdf((breakeven / spot).ln() / sig_avg);
            // Banked-decided: the banked contribution alone survives a full
            // reversion of the remaining path to the reference, with basis
            // noise + one sigma of remaining-average cushion on top.
            let banked_margin_bp = (banked_avg / ref_px - 1.0) * 1e4 * (banked_s / window);
            let cushion_bp = p.basis_guard_bp
                + sig_bp * ((rem / 60.0).max(0.02) / 3.0).sqrt() * (rem / window);
            let banked_decided =
                banked_margin_bp.abs() > cushion_bp && (banked_margin_bp > 0.0) == (p_up > 0.5);
            // Flip-proof: survives basis noise PLUS a full-remaining-window
            // adversarial push. rem/window scales the push's TWAP influence.
            let flip_proof = banked_decided
                && banked_margin_bp.abs()
                    > p.basis_guard_bp + p.manip_push_bp * (rem / window);
            Ok(ModelEval { p_up, sig_bp, banked_decided, flip_proof, rho })
        }
    }

    /// Math-forced evacuation: dump a held side whose fair has collapsed,
    /// into a bid that still resembles fair. Runs every armed tick AND
    /// through quiesce (exits are most needed late; only new buys stop).
    fn exit_signals(&mut self, ctx: &StrategyContext, p_up: f64, now: f64) -> Vec<Signal> {
        let p = self.p.clone();
        let mut signals = Vec::new();
        for (side, token, fair) in
            [("up", &p.token_up, p_up), ("down", &p.token_down, 1.0 - p_up)]
        {
            if fair >= EXIT_FAIR || self.inflight.contains_key(token) {
                continue;
            }
            let held = ctx
                .positions
                .get(token)
                .map(|pos| pos.size.to_f64().unwrap_or(0.0))
                .unwrap_or(0.0);
            if held < 5.0 {
                continue;
            }
            let Some((bid, bid_size)) = ctx.order_books.get(token).and_then(|b| {
                b.best_bid()
                    .map(|l| (l.price.to_f64().unwrap_or(0.0), l.size.to_f64().unwrap_or(0.0)))
            }) else {
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
            tape(serde_json::json!({
                "t": now, "ev": "exit", "slug": p.slug, "side": side,
                "fair": fair, "bid": bid, "size": size,
            }));
            self.inflight.insert(token.clone(), (0.0, now));
            signals.push(Signal::Cancel { token_id: token.clone() });
            signals.push(Signal::Sell {
                token_id: token.clone(),
                price: Decimal::from_f64(bid).unwrap_or(Decimal::ONE),
                size: Decimal::from_f64(size).unwrap_or(Decimal::ZERO),
                urgency: Urgency::High,
            });
        }
        signals
    }

    /// One decision pass for this arm. Returns (signals, finished).
    fn tick(&mut self, ctx: &StrategyContext, now: f64) -> (Vec<Signal>, bool) {
        let p = self.p.clone();
        let mut signals = Vec::new();

        if !self.subscribed {
            self.subscribed = true;
            signals.push(Signal::Subscribe { token_id: p.token_up.clone() });
            signals.push(Signal::Subscribe { token_id: p.token_down.clone() });
            return (signals, false);
        }

        // Window over: pull everything, drop the market, retire the arm.
        if now >= p.end {
            if !self.cleaned {
                self.cleaned = true;
                tracing::info!(slug = %p.slug, filled_usdc = self.filled_usdc, "updown window closed — cleaning up");
                tape(serde_json::json!({"t": now, "ev": "cleanup", "slug": p.slug}));
                signals.push(Signal::Cancel { token_id: p.token_up.clone() });
                signals.push(Signal::Cancel { token_id: p.token_down.clone() });
                signals.push(Signal::Unsubscribe { token_id: p.token_up.clone() });
                signals.push(Signal::Unsubscribe { token_id: p.token_down.clone() });
            }
            return (signals, true);
        }

        // Quiesce: standing orders pulled, no new buys — with one carve-out.
        // When the TWAP is flip-proof (banked beyond even an adversarial
        // spot push), the book's late panic/push prices are free money and
        // clips stay live until FLIP_BUY_CUTOFF_S. Exits always stay live
        // until the final seconds.
        if now >= p.end - p.quiesce_secs {
            let model = self.fair_p_up(now).ok();
            let flip_live = model.as_ref().map(|m| m.flip_proof).unwrap_or(false)
                && now < p.end - FLIP_BUY_CUTOFF_S;
            if !flip_live {
                signals.push(Signal::Cancel { token_id: p.token_up.clone() });
                signals.push(Signal::Cancel { token_id: p.token_down.clone() });
            }
            if let Some(m) = model {
                if now < p.end - 5.0 {
                    let exits = self.exit_signals(ctx, m.p_up, now);
                    signals.extend(exits);
                }
                if flip_live {
                    self.inflight.retain(|_, (_, at)| now - *at < INFLIGHT_TTL_S);
                    let inflight_usdc: f64 = self.inflight.values().map(|(n, _)| n).sum();
                    let committed = self.filled_usdc.max(position_floor(ctx, &p));
                    let room = p.size_usdc - committed - inflight_usdc;
                    let (side, token, fair) = if m.p_up > 0.5 {
                        ("up", p.token_up.clone(), m.p_up)
                    } else {
                        ("down", p.token_down.clone(), 1.0 - m.p_up)
                    };
                    let allowed = p.side_filter.as_ref().map(|s| s == side).unwrap_or(true);
                    let cooled = now - self.last_clip.get(&token).copied().unwrap_or(0.0)
                        >= p.clip_cooldown_s;
                    if allowed && cooled && room > 5.0 && !self.inflight.contains_key(&token) {
                        if let Some((ask, ask_size)) = ctx.order_books.get(&token).and_then(|b| {
                            b.best_ask().map(|l| {
                                (l.price.to_f64().unwrap_or(1.0), l.size.to_f64().unwrap_or(0.0))
                            })
                        }) {
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
                                    tape(serde_json::json!({
                                        "t": now, "ev": "fire", "slug": p.slug, "side": side,
                                        "ask": ask, "fair": fair, "net": net, "size": size,
                                        "committed": committed, "mode": "flip", "rho": m.rho,
                                    }));
                                    self.last_clip.insert(token.clone(), now);
                                    self.inflight.insert(token.clone(), (size * ask, now));
                                    signals.push(Signal::Cancel { token_id: token.clone() });
                                    signals.push(Signal::Buy {
                                        token_id: token.clone(),
                                        price: Decimal::from_f64(ask).unwrap_or(Decimal::ONE),
                                        size: Decimal::from_f64(size).unwrap_or(Decimal::ZERO),
                                        urgency: Urgency::High,
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
            return (signals, false);
        }

        let elapsed_frac = (now - p.start) / (p.end - p.start).max(1.0);
        if elapsed_frac < p.min_elapsed_frac {
            self.last_eval = Some(serde_json::json!({
                "state": "gated",
                "reason": format!("window {:.0}% elapsed, firing opens at {:.0}%",
                                  elapsed_frac * 100.0, p.min_elapsed_frac * 100.0),
                "t": now,
            }));
            return (signals, false);
        }

        let m = match self.fair_p_up(now) {
            Ok(v) => v,
            Err(gate) => {
                self.last_eval =
                    Some(serde_json::json!({"state": "gated", "reason": gate, "t": now}));
                if now - self.last_tape_at >= 5.0 {
                    self.last_tape_at = now;
                    tape(serde_json::json!({
                        "t": now, "ev": "gated", "slug": p.slug, "reason": gate,
                    }));
                }
                return (signals, false);
            }
        };
        let (p_up, sig_bp) = (m.p_up, m.sig_bp);

        // Notional already committed. on_fill events are unreliable for
        // taker orders (fills often only surface via the ~5s position
        // reconcile), so the authoritative floor is the position tracker:
        // shares held x entry price. Take the max of every signal we have.
        self.inflight.retain(|_, (_, at)| now - *at < INFLIGHT_TTL_S);
        let inflight_usdc: f64 = self.inflight.values().map(|(n, _)| n).sum();
        let committed = self.filled_usdc.max(position_floor(ctx, &p));
        let budget = p.size_usdc - committed - inflight_usdc;

        let exits = self.exit_signals(ctx, p_up, now);
        signals.extend(exits);

        // Exposure envelope: small speculative clips until the window is
        // either late or banked-decided; then the full budget eases into
        // the safe bet clip by clip.
        let unlocked = elapsed_frac >= p.late_frac || m.banked_decided;
        let cap = p.size_usdc * if unlocked { 1.0 } else { p.early_frac };
        let mut room = (cap - committed - inflight_usdc).min(budget);
        let (edge_req, fair_req) = if unlocked {
            (p.min_edge, p.min_fair)
        } else {
            (p.early_min_edge, EARLY_MIN_FAIR)
        };
        let chop_blocked = !unlocked && m.rho < p.rho_block;

        let mut evals = Vec::new();
        for (side, token, fair) in
            [("up", &p.token_up, p_up), ("down", &p.token_down, 1.0 - p_up)]
        {
            if let Some(only) = &p.side_filter {
                if only != side {
                    continue;
                }
            }
            let book = match ctx.order_books.get(token) {
                Some(b) => b,
                None => continue,
            };
            let (ask, ask_size) = match book.best_ask() {
                Some(l) => (l.price.to_f64().unwrap_or(1.0), l.size.to_f64().unwrap_or(0.0)),
                None => continue,
            };
            let fee = p.fee_rate * ask.min(1.0 - ask);
            let net = fair - ask - fee;
            evals.push(serde_json::json!({"side": side, "fair": fair, "ask": ask, "net": net}));

            let cooled =
                now - self.last_clip.get(token).copied().unwrap_or(0.0) >= p.clip_cooldown_s;
            let firing = !chop_blocked
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
            tape(serde_json::json!({
                "t": now, "ev": "fire", "slug": p.slug, "side": side,
                "ask": ask, "fair": fair, "net": net, "size": size,
                "committed": committed, "elapsed_frac": elapsed_frac,
                "mode": if unlocked { "safe" } else { "spec" }, "rho": m.rho,
            }));
            room -= size * ask;
            self.last_clip.insert(token.clone(), now);
            self.inflight.insert(token.clone(), (size * ask, now));
            signals.push(Signal::Cancel { token_id: token.clone() });
            signals.push(Signal::Buy {
                token_id: token.clone(),
                price: Decimal::from_f64(ask).unwrap_or(Decimal::ONE),
                size: Decimal::from_f64(size).unwrap_or(Decimal::ZERO),
                urgency: Urgency::High,
            });
        }

        self.last_eval = Some(serde_json::json!({
            "state": "armed", "t": now, "p_up": p_up, "sig_bp": sig_bp,
            "rho": m.rho, "mode": if unlocked { "safe" } else { "spec" },
            "chop_blocked": chop_blocked, "banked_decided": m.banked_decided,
            "committed": committed, "budget": budget, "room": room,
            "inflight": inflight_usdc, "sides": evals,
        }));
        if now - self.last_tape_at >= 5.0 {
            self.last_tape_at = now;
            tape(serde_json::json!({
                "t": now, "ev": "eval", "slug": p.slug, "p_up": p_up,
                "sig_bp": sig_bp, "rho": m.rho, "banked_decided": m.banked_decided,
                "committed": committed, "sides": evals,
            }));
        }

        (signals, false)
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
                        (slug.clone(), serde_json::json!({
                            "filled_usdc": a.filled_usdc,
                            "roll": a.p.roll,
                            "eval": a.last_eval,
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

/// Lag-1 autocorrelation of log-returns over the last `n` closes.
fn lag1_autocorr(closes: &[f64], n: usize) -> f64 {
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
        assert_eq!(p.rho_block, -0.25);
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
