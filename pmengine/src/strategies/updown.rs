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
}

fn d_min_edge() -> f64 { 0.03 }
fn d_max_price() -> f64 { 0.97 }
fn d_quiesce() -> f64 { 20.0 }
fn d_basis_guard() -> f64 { 3.0 }

/// Shared state the Binance poller thread keeps warm.
#[derive(Default)]
struct FeedState {
    spot: f64,
    spot_ts: f64,
    /// minute epoch -> (open+close)/2, proxying the Chainlink 60s TWAP.
    per_min: std::collections::BTreeMap<i64, f64>,
    /// Exact 1h candle open once the close_open window starts.
    candle_open: Option<f64>,
    last_err: Option<String>,
}

pub struct Updown {
    id: String,
    tokens: Vec<String>,
    params: Option<ArmParams>,
    feed: Arc<Mutex<FeedState>>,
    feed_stop: Arc<AtomicBool>,
    feed_handle: Option<std::thread::JoinHandle<()>>,
    subscribed: bool,
    cleaned: bool,
    filled_usdc: f64,
    /// token -> (notional, emitted_at unix) for the one order we allow in flight.
    inflight: std::collections::HashMap<String, (f64, f64)>,
    /// Tokens whose resting orders still need pulling after a disarm —
    /// on_command can't emit signals, so the next tick does it.
    pending_cleanup: Vec<String>,
    last_eval: Option<serde_json::Value>,
}

impl Updown {
    pub fn new() -> Self {
        Self {
            id: "updown".to_string(),
            tokens: Vec::new(),
            params: None,
            feed: Arc::new(Mutex::new(FeedState::default())),
            feed_stop: Arc::new(AtomicBool::new(false)),
            feed_handle: None,
            subscribed: false,
            cleaned: false,
            filled_usdc: 0.0,
            inflight: std::collections::HashMap::new(),
            pending_cleanup: Vec::new(),
            last_eval: None,
        }
    }

    fn stop_feed(&mut self) {
        self.feed_stop.store(true, Ordering::SeqCst);
        if let Some(h) = self.feed_handle.take() {
            let _ = h.join();
        }
    }

    fn start_feed(&mut self, p: &ArmParams) {
        self.stop_feed();
        self.feed = Arc::new(Mutex::new(FeedState::default()));
        self.feed_stop = Arc::new(AtomicBool::new(false));
        let feed = self.feed.clone();
        let stop = self.feed_stop.clone();
        let symbol = p.symbol.clone();
        let kind = p.kind.clone();
        let (start, end) = (p.start, p.end);

        self.feed_handle = Some(std::thread::spawn(move || {
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
                        f.last_err = None;
                    }
                    Err(e) => {
                        feed.lock().unwrap().last_err = Some(e);
                    }
                }
                std::thread::sleep(std::time::Duration::from_millis(750));
            }
        }));
    }

    /// Model fair P(UP) from the current feed. None while gated.
    fn fair_p_up(&self, p: &ArmParams, now: f64) -> Result<f64, String> {
        let f = self.feed.lock().unwrap();
        if now - f.spot_ts > MAX_SPOT_AGE_S {
            return Err(match &f.last_err {
                Some(e) => format!("feed stale: {}", e),
                None => "feed stale".to_string(),
            });
        }
        let spot = f.spot;
        let sig_frac = p.sigma_bp_per_min / 1e4;

        if p.kind == "close_open" {
            let open = f.candle_open.ok_or("candle open not printed yet")?;
            let t_min = ((p.end - now) / 60.0).max(0.005);
            let z = (spot / open).ln() / (sig_frac * t_min.sqrt());
            Ok(norm_cdf(z))
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
                return Ok(if banked_avg >= ref_px { 1.0 } else { 0.0 });
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
            Ok(1.0 - norm_cdf((breakeven / spot).ln() / sig_avg))
        }
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
        250
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

        // Quiesce: standing orders pulled, nothing new until resolution.
        if now >= p.end - p.quiesce_secs {
            signals.push(Signal::Cancel { token_id: p.token_up.clone() });
            signals.push(Signal::Cancel { token_id: p.token_down.clone() });
            self.last_eval = Some(serde_json::json!({"state": "quiesce", "t": now}));
            return signals;
        }

        let p_up = match self.fair_p_up(&p, now) {
            Ok(v) => v,
            Err(gate) => {
                self.last_eval = Some(serde_json::json!({"state": "gated", "reason": gate, "t": now}));
                return vec![Signal::Hold];
            }
        };

        // Notional already committed. on_fill events are unreliable for taker
        // orders (the engine often only learns of the fill via its ~5s
        // position reconcile), so the authoritative floor is the position
        // tracker itself: shares held x entry price. Take the max of every
        // signal we have, then add anything still in flight.
        self.inflight.retain(|_, (_, at)| now - *at < INFLIGHT_TTL_S);
        let inflight_usdc: f64 = self.inflight.values().map(|(n, _)| n).sum();
        let position_usdc: f64 = [&p.token_up, &p.token_down]
            .iter()
            .filter_map(|t| ctx.positions.get(t))
            .map(|pos| {
                let size = pos.size.to_f64().unwrap_or(0.0);
                let avg = pos.avg_entry_price.to_f64().unwrap_or(0.0);
                // Reconcile-seeded positions can carry avg 0 briefly; price
                // those shares at our max_price so the budget errs tight.
                size * if avg > 0.0 { avg } else { p.max_price }
            })
            .sum();
        let committed = self.filled_usdc.max(position_usdc);
        let mut budget = p.size_usdc - committed - inflight_usdc;

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

            let firing = net >= p.min_edge
                && ask <= p.max_price
                && budget > 5.0
                && !self.inflight.contains_key(token);
            if !firing {
                continue;
            }
            let size = (budget / ask).min(ask_size).floor();
            if size < 5.0 {
                continue;
            }
            tracing::info!(
                side, ask, fair, net, size,
                slug = %p.slug,
                "updown trigger firing — taking the ask"
            );
            budget -= size * ask;
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
            "state": "armed", "t": now, "p_up": p_up,
            "filled_usdc": self.filled_usdc, "sides": evals,
        }));

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
                self.filled_usdc += notional;
                self.inflight.remove(&fill.token_id);
                tracing::info!(
                    token = %fill.token_id, notional,
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
            }
        }
    } else if now >= start {
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

    Ok(FeedUpdate { spot, per_min, candle_open })
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
        assert_eq!(p.min_edge, 0.03);
        assert_eq!(p.max_price, 0.97);
        assert_eq!(p.quiesce_secs, 20.0);
        assert_eq!(p.basis_guard_bp, 3.0);
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
