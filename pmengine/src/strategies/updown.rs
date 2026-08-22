//! TWAP-gated trigger for Polymarket's recurring crypto up/down markets.
//!
//! The division of labor: the operator prices a market out-of-band
//! (`pmt crypto updown --json`) and arms this strategy with the semantics
//! and thresholds via `POST /strategies/updown/command`. The strategy then
//! owns the latency-critical part — a background Binance feed plus the live
//! CLOB book — and takes the ask only while every gate holds. Human minutes
//! decide *whether* to hunt a market; engine milliseconds decide *when*.
//!
//! Gates encode the lessons hand-trading these markets taught us:
//!   - edge is net of the crypto_fees_v2 taker fee, never gross
//!   - twap margins inside the Chainlink-vs-Binance basis band are coin
//!     flips regardless of what the proxy model says — no trade
//!   - a stale spot feed means no trade, not "trade on the last print"
//!   - the final seconds are quiesce: cancel everything, place nothing
//!     (a bid resting into resolution only ever fills against a winner)

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
/// Exit rule (the 2026-08-22 -$318 lesson: a 99%-fair entry died with no
/// hands to act as it flipped). Dump a held side when its fair collapses
/// below EXIT_FAIR — but only into a bid that isn't already dead, i.e.
/// within EXIT_MAX_DISCOUNT of fair. Selling below that donates to panic.
const EXIT_FAIR: f64 = 0.40;
const EXIT_MAX_DISCOUNT: f64 = 0.08;
/// Live vol floor: minutes of trailing 1m closes for the fast estimate.
const VOL_FAST_WINDOW: usize = 12;
/// One order in flight per token; assume dead if no fill inside this window.
/// Must comfortably outlive the engine's ~5s position-reconcile cadence:
/// taker fills are often MISSED by the realtime fill path and only show up
/// via reconcile (proven live 2026-08-22), so freeing the budget sooner
/// than reconcile lands means buying the same fill twice.
const INFLIGHT_TTL_S: f64 = 12.0;

// Private on purpose: the registry generator registers the first `pub struct`
// in the file, which must be Updown.
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
    /// mean-reverting chop: speculative clips are disabled entirely (the
    /// 2026-08-22 regime that ate two "locks").
    #[serde(default = "d_rho_block")]
    pub rho_block: f64,
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

/// Speculative clips still need the model leaning clearly one way.
const EARLY_MIN_FAIR: f64 = 0.55;

/// Shared state the Binance poller thread keeps warm.
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
    rho: f64,
}

pub struct Updown {
    id: String,
    tokens: Vec<String>,
    params: Option<ArmParams>,
    feed: Arc<Mutex<FeedState>>,
    feed_stop: Arc<AtomicBool>,
    feed_handles: Vec<std::thread::JoinHandle<()>>,
    subscribed: bool,
    cleaned: bool,
    filled_usdc: f64,
    /// token -> (notional, emitted_at unix) for the one order we allow in flight.
    inflight: std::collections::HashMap<String, (f64, f64)>,
    /// token -> last clip time, enforcing the per-side clip cadence.
    last_clip: std::collections::HashMap<String, f64>,
    /// Tokens whose resting orders still need pulling after a disarm —
    /// on_command can't emit signals, so the next tick does it.
    pending_cleanup: Vec<String>,
    last_eval: Option<serde_json::Value>,
    /// Throttle for eval lines in the durable tape.
    last_tape_at: f64,
}

/// Append one JSONL record to the durable tape at ~/.pmt/engine/. The tape
/// is the cross-session dataset for calibrating gate parameters — every
/// fire and periodic eval survives reboots, unlike engine stdout logs.
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

impl Updown {
    pub fn new() -> Self {
        Self {
            id: "updown".to_string(),
            tokens: Vec::new(),
            params: None,
            feed: Arc::new(Mutex::new(FeedState::default())),
            feed_stop: Arc::new(AtomicBool::new(false)),
            feed_handles: Vec::new(),
            subscribed: false,
            cleaned: false,
            filled_usdc: 0.0,
            inflight: std::collections::HashMap::new(),
            last_clip: std::collections::HashMap::new(),
            pending_cleanup: Vec::new(),
            last_eval: None,
            last_tape_at: 0.0,
        }
    }

    fn stop_feed(&mut self) {
        self.feed_stop.store(true, Ordering::SeqCst);
        for h in self.feed_handles.drain(..) {
            let _ = h.join();
        }
    }

    /// Push-based spot off the Binance trade stream (~100ms event age vs
    /// the REST poll's 750ms cadence) — this is where the "ms not s" lives.
    fn spawn_ws_spot(&mut self, symbol: &str, end: f64) {
        let feed = self.feed.clone();
        let stop = self.feed_stop.clone();
        let url = format!(
            "wss://data-stream.binance.vision/ws/{}@trade",
            symbol.to_lowercase()
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

    fn start_feed(&mut self, p: &ArmParams) {
        self.stop_feed();
        self.feed = Arc::new(Mutex::new(FeedState::default()));
        self.feed_stop = Arc::new(AtomicBool::new(false));
        self.spawn_ws_spot(&p.symbol, p.end);
        let feed = self.feed.clone();
        let stop = self.feed_stop.clone();
        let symbol = p.symbol.clone();
        let kind = p.kind.clone();
        let (start, end) = (p.start, p.end);

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
    fn fair_p_up(&self, p: &ArmParams, now: f64) -> Result<ModelEval, String> {
        let f = self.feed.lock().unwrap();
        if now - f.spot_ts > MAX_SPOT_AGE_S {
            return Err(match &f.last_err {
                Some(e) => format!("feed stale: {}", e),
                None => "feed stale".to_string(),
            });
        }
        let spot = f.spot;
        // Vol only ratchets UP intraminute: the arm-time trailing sigma is a
        // floor, the fast window catches the storm the trailing estimate
        // lags (the -$318 window quoted 99% fair off calm-market vol).
        let fast_bp = {
            let c = &f.closes;
            let n = c.len().min(VOL_FAST_WINDOW + 1);
            if n >= 4 {
                let rets: Vec<f64> =
                    c[c.len() - n..].windows(2).map(|w| (w[1] / w[0]).ln()).collect();
                let mu = rets.iter().sum::<f64>() / rets.len() as f64;
                let var = rets.iter().map(|r| (r - mu).powi(2)).sum::<f64>() / (rets.len() - 1) as f64;
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
            Ok(ModelEval { p_up: norm_cdf(z), sig_bp, banked_decided: false, rho })
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
                return Ok(ModelEval { p_up, sig_bp, banked_decided: true, rho });
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
            let cushion_bp =
                p.basis_guard_bp + sig_bp * ((rem / 60.0).max(0.02) / 3.0).sqrt() * (rem / window);
            let banked_decided = banked_margin_bp.abs() > cushion_bp
                && (banked_margin_bp > 0.0) == (p_up > 0.5);
            Ok(ModelEval { p_up, sig_bp, banked_decided, rho })
        }
    }

    /// Math-forced evacuation: dump a held side whose fair has collapsed,
    /// into a bid that still resembles fair. Runs every armed tick AND
    /// through quiesce (exits are most needed late; only new buys stop).
    fn exit_signals(
        &mut self,
        ctx: &StrategyContext,
        p: &ArmParams,
        p_up: f64,
        now: f64,
    ) -> Vec<Signal> {
        let mut signals = Vec::new();
        for (side, token, fair) in [
            ("up", &p.token_up, p_up),
            ("down", &p.token_down, 1.0 - p_up),
        ] {
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
        self.tokens.clone()
    }

    fn tick_interval_ms(&self) -> u64 {
        50
    }

    fn on_tick(&mut self, ctx: &StrategyContext) -> Vec<Signal> {
        if !self.pending_cleanup.is_empty() {
            let signals = self
                .pending_cleanup
                .drain(..)
                .flat_map(|t| {
                    [Signal::Cancel { token_id: t.clone() }, Signal::Unsubscribe { token_id: t }]
                })
                .collect();
            return signals;
        }
        let p = match &self.params {
            Some(p) => p.clone(),
            None => return vec![Signal::Hold],
        };
        let now = unix_now();
        let mut signals = Vec::new();

        if !self.subscribed {
            self.subscribed = true;
            signals.push(Signal::Subscribe { token_id: p.token_up.clone() });
            signals.push(Signal::Subscribe { token_id: p.token_down.clone() });
            return signals;
        }

        // Window over: pull everything, drop the market, disarm.
        if now >= p.end {
            if !self.cleaned {
                self.cleaned = true;
                tracing::info!(slug = %p.slug, filled_usdc = self.filled_usdc, "updown window closed — cleaning up");
                tape(serde_json::json!({"t": now, "ev": "cleanup", "slug": p.slug}));
                signals.push(Signal::Cancel { token_id: p.token_up.clone() });
                signals.push(Signal::Cancel { token_id: p.token_down.clone() });
                signals.push(Signal::Unsubscribe { token_id: p.token_up.clone() });
                signals.push(Signal::Unsubscribe { token_id: p.token_down.clone() });
                self.stop_feed();
                self.params = None;
                self.tokens = Vec::new();
            }
            return signals;
        }

        // Quiesce: standing orders pulled, no new buys — but exits stay
        // live until the final seconds (they matter most late).
        if now >= p.end - p.quiesce_secs {
            signals.push(Signal::Cancel { token_id: p.token_up.clone() });
            signals.push(Signal::Cancel { token_id: p.token_down.clone() });
            if now < p.end - 5.0 {
                if let Ok(m) = self.fair_p_up(&p, now) {
                    let exits = self.exit_signals(ctx, &p, m.p_up, now);
                    signals.extend(exits);
                }
            }
            self.last_eval = Some(serde_json::json!({"state": "quiesce", "t": now}));
            return signals;
        }

        let elapsed_frac = (now - p.start) / (p.end - p.start).max(1.0);
        if elapsed_frac < p.min_elapsed_frac {
            self.last_eval = Some(serde_json::json!({
                "state": "gated",
                "reason": format!("window {:.0}% elapsed, firing opens at {:.0}%",
                                  elapsed_frac * 100.0, p.min_elapsed_frac * 100.0),
                "t": now,
            }));
            return vec![Signal::Hold];
        }

        let m = match self.fair_p_up(&p, now) {
            Ok(v) => v,
            Err(gate) => {
                self.last_eval = Some(serde_json::json!({"state": "gated", "reason": gate, "t": now}));
                // Gated windows must be reconstructable from the tape too —
                // v2's first outing sat out 5 straight minutes and left
                // nothing but a cleanup record to autopsy.
                if now - self.last_tape_at >= 5.0 {
                    self.last_tape_at = now;
                    tape(serde_json::json!({
                        "t": now, "ev": "gated", "slug": p.slug, "reason": gate,
                    }));
                }
                return vec![Signal::Hold];
            }
        };
        let (p_up, sig_bp) = (m.p_up, m.sig_bp);

        // Notional already committed. on_fill events are unreliable for taker
        // orders (the engine often only learns of the fill via its ~5s
        // position reconcile), so the authoritative floor is the position
        // tracker itself: shares held x entry price. Take the max of every
        // signal we have, then add anything still in flight.
        self.inflight.retain(|_, (_, at)| now - *at < INFLIGHT_TTL_S);
        let inflight_usdc: f64 = self.inflight.values().map(|(n, _)| n).sum();
        let position_usdc: f64 = [&p.token_up, &p.token_down]
            .iter()
            .filter_map(|t| ctx.positions.get(*t).map(|pos| (*t, pos)))
            .map(|(t, pos)| {
                let size = pos.size.to_f64().unwrap_or(0.0);
                let avg = pos.avg_entry_price.to_f64().unwrap_or(0.0);
                // Reconcile-seeded positions can carry avg 0 briefly. Pricing
                // them at max_price once blocked a legitimate top-up re-arm
                // (2026-08-22); the live ask is the honest estimate, with
                // max_price only as the last resort.
                let fallback = ctx
                    .order_books
                    .get(t)
                    .and_then(|b| b.best_ask())
                    .and_then(|l| l.price.to_f64())
                    .unwrap_or(p.max_price);
                size * if avg > 0.0 { avg } else { fallback }
            })
            .sum();
        let committed = self.filled_usdc.max(position_usdc);
        let budget = p.size_usdc - committed - inflight_usdc;

        let exits = self.exit_signals(ctx, &p, p_up, now);
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
        for (side, token, fair) in [
            ("up", &p.token_up, p_up),
            ("down", &p.token_down, 1.0 - p_up),
        ] {
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

            let cooled = now - self.last_clip.get(token).copied().unwrap_or(0.0)
                >= p.clip_cooldown_s;
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

        if signals.is_empty() {
            vec![Signal::Hold]
        } else {
            signals
        }
    }

    fn on_fill(&mut self, fill: &Fill) {
        if let Some(p) = &self.params {
            if fill.token_id == p.token_up || fill.token_id == p.token_down {
                let notional = (fill.price * fill.size).to_f64().unwrap_or(0.0);
                // Only buys consume budget; exit sells free shares but the
                // gross-buys number stays (no re-deploying after an exit in
                // the same window — evacuated capital stays evacuated).
                if fill.is_buy {
                    self.filled_usdc += notional;
                }
                self.inflight.remove(&fill.token_id);
                tracing::info!(
                    token = %fill.token_id, notional, is_buy = fill.is_buy,
                    total = self.filled_usdc,
                    "updown fill"
                );
            }
        }
    }

    fn on_shutdown(&mut self) {
        self.stop_feed();
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
                self.start_feed(&p);
                self.tokens = vec![p.token_up.clone(), p.token_down.clone()];
                self.subscribed = false;
                self.cleaned = false;
                self.filled_usdc = 0.0;
                self.inflight.clear();
                self.last_clip.clear();
                let slug = p.slug.clone();
                self.params = Some(p);
                Ok(serde_json::json!({"armed": slug}))
            }
            Some("disarm") => {
                self.stop_feed();
                if let Some(p) = &self.params {
                    self.pending_cleanup = vec![p.token_up.clone(), p.token_down.clone()];
                }
                let was = self.params.take().map(|p| p.slug);
                self.tokens = Vec::new();
                Ok(serde_json::json!({"disarmed": was, "cleanup": "next tick"}))
            }
            Some("status") => Ok(serde_json::json!({
                "armed": self.params.as_ref().map(|p| p.slug.clone()),
                "filled_usdc": self.filled_usdc,
                "eval": self.last_eval,
            })),
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
        let start_ms = ((start as i64 - 120) * 1000).to_string();
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

    #[test]
    fn arm_params_defaults() {
        let p: ArmParams = serde_json::from_value(serde_json::json!({
            "slug": "s", "kind": "twap", "symbol": "BTCUSDT",
            "token_up": "1", "token_down": "2",
            "start": 0.0, "end": 900.0,
            "sigma_bp_per_min": 2.5, "fee_rate": 0.07, "size_usdc": 100.0,
        }))
        .unwrap();
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

    fn armed_with_feed(banked_px: f64, spot: f64) -> (Updown, ArmParams, f64) {
        let s = Updown::new();
        let p: ArmParams = serde_json::from_value(serde_json::json!({
            "slug": "s", "kind": "twap", "symbol": "BTCUSDT",
            "token_up": "1", "token_down": "2",
            "start": 600.0, "end": 1500.0,
            "sigma_bp_per_min": 3.0, "fee_rate": 0.07, "size_usdc": 100.0,
        }))
        .unwrap();
        let now = 1400.0;
        {
            let mut f = s.feed.lock().unwrap();
            f.spot = spot;
            f.spot_ts = now;
            f.per_min.insert(540, 100.0); // range-start reference
            for t in (600..1380).step_by(60) {
                f.per_min.insert(t, banked_px);
            }
        }
        (s, p, now)
    }

    #[test]
    fn twap_banked_above_ref_is_locked_up() {
        let (s, p, now) = armed_with_feed(101.0, 101.0); // +100bp margin
        let m = s.fair_p_up(&p, now).unwrap();
        assert!(m.p_up > 0.999);
        assert!(m.banked_decided, "a +100bp banked margin survives full reversion");
    }

    #[test]
    fn twap_banked_below_ref_is_locked_down() {
        let (s, p, now) = armed_with_feed(99.0, 99.0);
        assert!(s.fair_p_up(&p, now).unwrap().p_up < 0.001);
    }

    #[test]
    fn thin_banked_margin_is_not_decided() {
        // +3.5bp banked contribution sits inside basis + vol cushion:
        // clears the basis guard for pricing, but is NOT safe-bet material
        let (s, p, now) = armed_with_feed(100.035, 100.035);
        let m = s.fair_p_up(&p, now).unwrap();
        assert!(!m.banked_decided);
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
        let (s, p, now) = armed_with_feed(100.06, 100.06);
        let calm = s.fair_p_up(&p, now).unwrap();
        let (p_calm, sig_calm) = (calm.p_up, calm.sig_bp);
        assert!((sig_calm - 3.0).abs() < 1e-9); // no closes -> arm sigma
        assert!(p_calm > 0.99);
        {
            // violent recent tape: ±60bp swings per minute
            let mut f = s.feed.lock().unwrap();
            f.closes = (0..14)
                .map(|i| if i % 2 == 0 { 100.0 } else { 100.6 })
                .collect();
        }
        let storm = s.fair_p_up(&p, now).unwrap();
        assert!(storm.sig_bp > 30.0, "sig {}", storm.sig_bp);
        assert!(storm.p_up < p_calm - 0.01, "storm p {} vs calm {}", storm.p_up, p_calm);
    }

    #[test]
    fn twap_thin_margin_trips_basis_guard() {
        let (s, p, now) = armed_with_feed(100.001, 100.001); // ~0.1bp
        let err = s.fair_p_up(&p, now).unwrap_err();
        assert!(err.contains("basis guard"), "{}", err);
    }

    #[test]
    fn twap_stale_feed_refuses() {
        let (s, p, _) = armed_with_feed(101.0, 101.0);
        let err = s.fair_p_up(&p, 1400.0 + MAX_SPOT_AGE_S + 1.0).unwrap_err();
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
        assert!(s.params.is_none());
    }
}
