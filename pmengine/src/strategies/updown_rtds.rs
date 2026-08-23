//! RTDS settlement-stream market data: the object these markets settle on,
//! wired straight into the model instead of proxied through Binance.
//!
//! Polymarket's crypto up/down markets settle on the Chainlink TWAP stream
//! relayed at `wss://ws-live-data.polymarket.com/` (1 Hz, free,
//! unauthenticated). Every arm until now read Binance and compared it to a
//! settlement print it could not see — the whole point of `basis_guard_bp`
//! was to price that cross-venue gap (docs/LESSONS.md#L2, #L32), and on
//! xrp/doge the gap was wide enough that no guard was both safe and
//! tradeable. Reading the settlement stream directly deletes the cross-venue
//! job entirely: reference, spot and TWAP marks all come off the same series
//! the resolver reads. What is left for the guard is the stream's own
//! sampling noise, which is a much smaller number.
//!
//! ONE supervisor for the whole strategy, not one per arm. The socket
//! carries all eight symbols on three topics for free, and a 15-arm fleet
//! opening 15 sockets is how you get rate-limited off a feed nobody is
//! charging for. Arms register as consumers and the supervisor fans each
//! sample out into their `FeedState`.
//!
//! Three protocol facts, learned the hard way by the Python recorder
//! (`pmtrader/polymarket/rtds.py`) and re-stated here because this client
//! has to obey them too:
//!
//!   - **The `PING` keepalive is not optional.** A plain-text `PING` frame
//!     every few seconds; without it the socket stays *open* while the flow
//!     silently stops. We also force a reconnect after `stall_s` of silence,
//!     because "open but dead" is this feed's signature failure.
//!   - **Subscribe per TOPIC, never per symbol.** One entry per topic with
//!     `filters` omitted delivers every symbol. Per-symbol entries at scale
//!     made the server quietly downgrade the whole subscription to Binance
//!     `crypto_prices` snapshots — wrong data, no error frame.
//!   - **The feed has no history.** No snapshot, no replay, no backfill after
//!     a disconnect. That is why the hub keeps its own per-symbol history and
//!     seeds a newly-armed window from it: a window armed at 12:05 needs the
//!     TWAP mark printed at 12:00, and nothing can re-fetch it.
//!
//! No plain `pub struct` in this file on purpose (same reason as
//! updown_model.rs / updown_oracle.rs, docs/LESSONS.md#L20): `pmstrat
//! transpile --all` registers the first `pub struct` it finds in every file
//! under strategies/. Every public item here is `pub(crate)`.

use super::updown_model::{lag1_autocorr, FeedState};
use std::collections::{BTreeMap, HashMap};
use std::sync::atomic::{AtomicBool, AtomicU64, Ordering};
use std::sync::{Arc, Mutex};

const RTDS_URL: &str = "wss://ws-live-data.polymarket.com/";

/// The 1 Hz Chainlink spot series — the model's `spot`.
pub(crate) const TOPIC_SPOT: &str = "crypto_prices_chainlink";
/// 30s TWAP: the settlement width for 5m windows.
pub(crate) const TOPIC_TWAP30: &str = "crypto_prices_twap_thirty";
/// 60s TWAP: the settlement width for everything longer.
pub(crate) const TOPIC_TWAP60: &str = "crypto_prices_twap_sixty";
/// All three are subscribed unconditionally: an arm's settlement width is
/// per-window, and a 5m arm rolling beside a 15m arm needs both.
const TOPICS: [&str; 3] = [TOPIC_SPOT, TOPIC_TWAP30, TOPIC_TWAP60];

/// What a no-filter subscription actually delivers (verified live
/// 2026-08-23 by the recorder). Used to refuse an arm whose symbol the
/// stream does not carry — a typo would otherwise arm a window that sits
/// gated on a stale feed forever and never says why.
pub(crate) const RTDS_SYMBOLS: [&str; 8] = [
    "btc/usd", "eth/usd", "sol/usd", "xrp/usd",
    "doge/usd", "bnb/usd", "hype/usd", "zec/usd",
];

/// A sample whose own observation clock is older than this is not evidence
/// of a live feed — the relay is lagging, or our clock is skewed. Dropping
/// it freezes `spot_ts`, so MAX_SPOT_AGE_S gates the arm a few seconds
/// later. Fail-closed: a lagging feed must stop trades, not price them.
const MAX_SAMPLE_LAG_S: f64 = 10.0;
/// How late a TWAP print may be and still count as "the value AT the minute
/// mark". The stream emits ~1/s, so the exact `:00` sample normally exists;
/// this only bounds the substitute taken when it was dropped.
const MARK_TOL_S: i64 = 2;
/// Trailing 1m closes kept per symbol. The estimators reach back 45
/// (`SIGMA_SLOW_WINDOW`) and 60 (`lag1_autocorr`) samples; 120 covers both
/// with room and costs nothing.
const CLOSES_CAP: usize = 120;
/// Per-symbol TWAP marks retained in the hub (minutes). Long enough that a
/// mid-window arm on a multi-hour market can still be seeded with its
/// range-start reference.
const HISTORY_MARKS: usize = 300;
/// An arm's own `per_min` only needs its range-start reference (start-60)
/// and the in-window marks; anything older is dead weight on every eval.
const ARM_PER_MIN_KEEP_S: f64 = 180.0;

/// Reconnect backoff bounds. Infinite retries: this is the only feed a
/// stream-fed arm has, and giving up means every arm on it gates forever.
const BACKOFF_START_S: f64 = 1.0;
const BACKOFF_MAX_S: f64 = 60.0;
/// PING cadence. The recorder uses 4s against a ~228s observed silent-death;
/// no reason to be less generous here.
const PING_S: f64 = 4.0;
/// Silence with the socket still open = reconnect. A subscription the
/// server quietly declined looks exactly like a healthy idle socket
/// otherwise.
const STALL_S: f64 = 30.0;
/// "RTDS health" INFO line cadence, matching the engine's "Book health".
const HEALTH_LOG_S: f64 = 60.0;
/// Read timeout on the socket — the loop's own pacing for pings, health
/// lines and the stop flag.
const READ_TIMEOUT_S: u64 = 1;

/// Which series a sample belongs to.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
enum Topic {
    Spot,
    Twap30,
    Twap60,
}

impl Topic {
    fn parse(name: &str) -> Option<Topic> {
        match name {
            TOPIC_SPOT => Some(Topic::Spot),
            TOPIC_TWAP30 => Some(Topic::Twap30),
            TOPIC_TWAP60 => Some(Topic::Twap60),
            _ => None,
        }
    }
}

/// The settlement-width TWAP topic for a window of `settle_tw_secs` — 30s
/// for 5m markets, 60s otherwise (`updown_model::settle_tw_secs` owns the
/// width rule; this only maps it onto a topic).
fn twap_topic_for(settle_tw_s: f64) -> Topic {
    if settle_tw_s <= 30.0 {
        Topic::Twap30
    } else {
        Topic::Twap60
    }
}

/// `"XRPUSDT"` -> `"xrp/usd"`. None when the pair isn't a USD-quoted symbol
/// the stream carries — the caller turns that into an arm-time refusal.
pub(crate) fn rtds_symbol(binance_symbol: &str) -> Option<String> {
    let s = binance_symbol.to_lowercase();
    // Longest quote first: "usdt" must not match as "usd" + a stray "t".
    let base = ["usdt", "usdc", "busd", "usd"]
        .iter()
        .find_map(|q| s.strip_suffix(q))?;
    if base.is_empty() {
        return None;
    }
    let sym = format!("{}/usd", base);
    RTDS_SYMBOLS.contains(&sym.as_str()).then_some(sym)
}

/// One price print off the stream.
#[derive(Debug, Clone, PartialEq)]
struct Sample {
    topic: Topic,
    symbol: String,
    /// The payload's own observation timestamp (ms) — the Chainlink clock
    /// settlement compares on, not the relay's emit time.
    ts_ms: i64,
    value: f64,
}

impl Sample {
    fn ts_s(&self) -> i64 {
        self.ts_ms.div_euclid(1000)
    }
}

/// `full_accuracy_value` is an E18 fixed-point STRING (Chainlink Data
/// Streams scale). Parsing it as an integer and scaling keeps every digit
/// the wire carried; `value` is Polymarket's already-lossy display float and
/// is only the fallback.
fn parse_e18(raw: &str) -> Option<f64> {
    let s = raw.trim();
    let (sign, digits) = match s.strip_prefix('-') {
        Some(rest) => (-1.0, rest),
        None => (1.0, s.strip_prefix('+').unwrap_or(s)),
    };
    if digits.is_empty() || !digits.bytes().all(|b| b.is_ascii_digit()) {
        return None;
    }
    let n: i128 = digits.parse().ok()?;
    Some(sign * n as f64 / 1e18)
}

/// One RTDS envelope -> a `Sample`, or None if it isn't a price update we
/// track. Envelope: `{topic, type, timestamp, payload:{symbol, timestamp,
/// value, full_accuracy_value, window_s}}`.
fn parse_sample(raw: &str) -> Option<Sample> {
    let v: serde_json::Value = serde_json::from_str(raw).ok()?;
    let topic = Topic::parse(v.get("topic")?.as_str()?)?;
    let payload = v.get("payload")?;
    let symbol = payload.get("symbol")?.as_str()?;
    if symbol.is_empty() {
        return None;
    }
    let ts_ms = payload.get("timestamp").and_then(value_as_i64)?;
    let value = payload
        .get("full_accuracy_value")
        .and_then(|f| f.as_str())
        .and_then(parse_e18)
        .or_else(|| payload.get("value").and_then(|v| v.as_f64()))?;
    if !value.is_finite() || value <= 0.0 {
        return None;
    }
    Some(Sample { topic, symbol: symbol.to_string(), ts_ms, value })
}

/// The stream has sent `timestamp` as both a number and a string; accept
/// either rather than silently dropping every print on a format change.
fn value_as_i64(v: &serde_json::Value) -> Option<i64> {
    v.as_i64().or_else(|| v.as_str().and_then(|s| s.trim().parse().ok()))
}

/// The subscription frame: ONE entry per topic, `filters` omitted.
///
/// Do not "improve" this into per-symbol entries. Twenty-four of them made
/// the server downgrade the whole subscription to `crypto_prices` (Binance)
/// snapshots with no error frame — the exact data this feature exists to
/// stop reading.
fn subscribe_message() -> String {
    serde_json::json!({
        "action": "subscribe",
        "subscriptions": TOPICS
            .iter()
            .map(|t| serde_json::json!({"topic": t, "type": "update"}))
            .collect::<Vec<_>>(),
    })
    .to_string()
}

// ---------- health -------------------------------------------------------

/// What the supervisor knows about its own socket. Surfaced on the updown
/// status payload and on the periodic "RTDS health" log line.
#[derive(Debug, Clone, Default)]
pub(crate) struct RtdsHealth {
    pub(crate) started: bool,
    pub(crate) connected: bool,
    pub(crate) events: u64,
    pub(crate) events_per_s: f64,
    /// Wall clock of the last routed sample; 0.0 = never.
    pub(crate) last_event_at: f64,
    pub(crate) reconnects: u64,
    pub(crate) dropped_lagged: u64,
    pub(crate) last_err: Option<String>,
    pub(crate) consumers: usize,
    pub(crate) symbols: usize,
}

impl RtdsHealth {
    fn last_event_age_s(&self, now: f64) -> Option<f64> {
        (self.last_event_at > 0.0).then_some(now - self.last_event_at)
    }

    pub(crate) fn json(&self, now: f64) -> serde_json::Value {
        serde_json::json!({
            "started": self.started,
            "connected": self.connected,
            "events": self.events,
            "events_per_s": (self.events_per_s * 100.0).round() / 100.0,
            "last_event_age_s": self.last_event_age_s(now).map(|a| (a * 10.0).round() / 10.0),
            "reconnects": self.reconnects,
            "dropped_lagged": self.dropped_lagged,
            "consumers": self.consumers,
            "symbols": self.symbols,
            "err": self.last_err,
        })
    }
}

// ---------- the hub ------------------------------------------------------

/// Per-symbol accumulated history. Its whole reason to exist is that the
/// feed has no backfill: an arm registering now must be able to inherit the
/// marks and closes printed before it existed, or a rolling 5m chain would
/// restart its vol history from zero every five minutes and a mid-window arm
/// would never see its range-start reference at all.
#[derive(Default)]
struct SymbolHistory {
    spot: f64,
    spot_ts: f64,
    per_min_30: BTreeMap<i64, f64>,
    per_min_60: BTreeMap<i64, f64>,
    closes: Vec<f64>,
    last_close_min: i64,
    last_mark_30: i64,
    last_mark_60: i64,
}

impl SymbolHistory {
    fn marks(&mut self, topic: Topic) -> &mut BTreeMap<i64, f64> {
        match topic {
            Topic::Twap30 => &mut self.per_min_30,
            _ => &mut self.per_min_60,
        }
    }

    fn marks_ref(&self, topic: Topic) -> &BTreeMap<i64, f64> {
        match topic {
            Topic::Twap30 => &self.per_min_30,
            _ => &self.per_min_60,
        }
    }
}

/// One armed window's registration.
struct Consumer {
    id: u64,
    symbol: String,
    /// Which TWAP topic fills this arm's `per_min` — its settlement width.
    twap: Topic,
    /// Window start, so `per_min` can be pruned to what the model reads.
    start: f64,
    feed: Arc<Mutex<FeedState>>,
}

struct HubInner {
    consumers: Mutex<Vec<Consumer>>,
    history: Mutex<HashMap<String, SymbolHistory>>,
    health: Mutex<RtdsHealth>,
    stop: AtomicBool,
    next_id: AtomicU64,
    supervisor: Mutex<Option<std::thread::JoinHandle<()>>>,
}

/// A handle to the one shared RTDS supervisor. Cheap to clone; the socket
/// and its thread live behind the `Arc`.
#[derive(Clone)]
pub(crate) struct RtdsHub {
    inner: Arc<HubInner>,
}

impl Default for RtdsHub {
    fn default() -> Self {
        Self::new()
    }
}

/// An arm's registration. Dropping it unregisters — so an arm that retires,
/// is replaced, or is dropped on any path at all stops being fed, with no
/// teardown call to forget.
pub(crate) struct RtdsSub {
    inner: Arc<HubInner>,
    id: u64,
}

impl Drop for RtdsSub {
    fn drop(&mut self) {
        let mut consumers = self.inner.consumers.lock().unwrap();
        consumers.retain(|c| c.id != self.id);
        let n = consumers.len();
        drop(consumers);
        self.inner.health.lock().unwrap().consumers = n;
    }
}

impl RtdsHub {
    pub(crate) fn new() -> Self {
        Self {
            inner: Arc::new(HubInner {
                consumers: Mutex::new(Vec::new()),
                history: Mutex::new(HashMap::new()),
                health: Mutex::new(RtdsHealth::default()),
                stop: AtomicBool::new(false),
                next_id: AtomicU64::new(1),
                supervisor: Mutex::new(None),
            }),
        }
    }

    /// Register one arm's `FeedState` for a symbol, seeding it from whatever
    /// history the hub already holds, and start the supervisor if this is
    /// the first consumer.
    pub(crate) fn register(
        &self,
        symbol: &str,
        settle_tw_s: f64,
        start: f64,
        feed: Arc<Mutex<FeedState>>,
    ) -> RtdsSub {
        let twap = twap_topic_for(settle_tw_s);
        seed_feed(&self.inner, symbol, twap, start, &feed);
        let id = self.inner.next_id.fetch_add(1, Ordering::SeqCst);
        {
            let mut consumers = self.inner.consumers.lock().unwrap();
            consumers.push(Consumer {
                id,
                symbol: symbol.to_string(),
                twap,
                start,
                feed,
            });
            let n = consumers.len();
            drop(consumers);
            let mut h = self.inner.health.lock().unwrap();
            h.consumers = n;
        }
        self.ensure_started();
        RtdsSub { inner: self.inner.clone(), id }
    }

    /// Start the supervisor thread once. Lazy on purpose: an engine whose
    /// arms are all Binance-fed never opens the socket.
    fn ensure_started(&self) {
        // Same house rule as ArmStore::live() and install_arm's feeds: a
        // unit test never reaches the network. The loop itself is driven
        // directly by the tests below, through `run_loop` with a scripted
        // transport, so nothing here goes untested for the sake of it.
        if cfg!(test) {
            return;
        }
        let mut sup = self.inner.supervisor.lock().unwrap();
        if sup.is_some() {
            return;
        }
        self.inner.stop.store(false, Ordering::SeqCst);
        self.inner.health.lock().unwrap().started = true;
        let inner = self.inner.clone();
        *sup = Some(std::thread::spawn(move || {
            let cfg = SupervisorCfg::default();
            run_loop(&inner, &cfg, live_connect);
        }));
    }

    /// Stop the supervisor and join it. Called from `on_shutdown`; the
    /// socket otherwise lives for the strategy's lifetime, because tearing
    /// it down between a window closing and its roll re-arming would flap
    /// the connection every five minutes and throw away the vol history
    /// that only accumulates in real time.
    pub(crate) fn stop(&self) {
        self.inner.stop.store(true, Ordering::SeqCst);
        let handle = self.inner.supervisor.lock().unwrap().take();
        if let Some(h) = handle {
            let _ = h.join();
        }
        self.inner.health.lock().unwrap().started = false;
    }

    pub(crate) fn health(&self) -> RtdsHealth {
        self.inner.health.lock().unwrap().clone()
    }

    /// Test-only: put one price through the real parse + route, exactly as
    /// a connected session would. Lets updown.rs's tests prove the whole
    /// path (wire frame -> FeedState -> eval_model) instead of hand-filling
    /// a FeedState and trusting the two agree.
    #[cfg(test)]
    pub(crate) fn ingest_price(
        &self,
        topic: &str,
        symbol: &str,
        ts_s: i64,
        value: f64,
        now: f64,
    ) {
        let raw = serde_json::json!({
            "topic": topic, "type": "update", "timestamp": ts_s * 1000,
            "payload": {
                "symbol": symbol, "timestamp": ts_s * 1000, "value": value,
                "full_accuracy_value": format!("{:.0}", value * 1e18),
            },
        })
        .to_string();
        let sample = parse_sample(&raw).expect("a well-formed price frame");
        route_sample(&self.inner, &sample, now);
    }
}

/// Copy the hub's accumulated history for `symbol` into a fresh arm's feed.
fn seed_feed(
    inner: &HubInner,
    symbol: &str,
    twap: Topic,
    start: f64,
    feed: &Arc<Mutex<FeedState>>,
) {
    let seed = {
        let history = inner.history.lock().unwrap();
        history.get(symbol).map(|h| {
            let marks: Vec<(i64, f64)> = h
                .marks_ref(twap)
                .iter()
                .filter(|(k, _)| **k as f64 >= start - ARM_PER_MIN_KEEP_S)
                .map(|(k, v)| (*k, *v))
                .collect();
            (h.spot, h.spot_ts, marks, h.closes.clone())
        })
    };
    let Some((spot, spot_ts, marks, closes)) = seed else { return };
    let mut f = feed.lock().unwrap();
    // spot_ts is a receive-time stamp, so a stale seed self-gates through
    // MAX_SPOT_AGE_S rather than needing a freshness check here.
    f.spot = spot;
    f.spot_ts = spot_ts;
    f.per_min.extend(marks);
    if !closes.is_empty() {
        f.rho = lag1_autocorr(&closes, 60);
        f.closes = closes;
    }
}

/// Fan one sample into the hub's history and every consumer on that symbol.
///
/// The FeedState contract is preserved field for field, so `eval_model`
/// needs no knowledge of where its numbers came from:
///   - `spot` / `spot_ts`: the latest chainlink print, stamped with receive
///     time exactly as the Binance trade-stream thread stamps its own.
///   - `per_min[m]`: the settlement-width TWAP's value AT wall time `m+60`.
///     The TWAP printed at `m+60` averages the window ending there, which is
///     the same span Binance's `(open+close)/2` for minute `m` proxied — and
///     it makes `per_min[start-60]`, the model's range-start reference,
///     literally the settlement print at the window's start instant.
///   - `closes`: one chainlink sample per minute, oldest first.
///   - `rho`: recomputed off those closes, same call the Binance poller makes.
///   - `candle_open`: never set — close_open arms are refused on this feed.
fn route_sample(inner: &HubInner, s: &Sample, now: f64) {
    if s.topic == Topic::Spot && now - (s.ts_s() as f64) > MAX_SAMPLE_LAG_S {
        // A lagging relay must not keep spot_ts fresh; let the staleness
        // gate bind and say why.
        let lag = now - s.ts_s() as f64;
        {
            let mut h = inner.health.lock().unwrap();
            h.dropped_lagged += 1;
            h.last_err = Some(format!("sample lag {:.1}s", lag));
        }
        // Name it on the arms too, or the gate line reads "feed stale" and
        // the operator goes looking at the market instead of the relay.
        mark_feeds_err(inner, Some(&s.symbol), &format!("sample lag {:.1}s", lag));
        return;
    }

    // Phase 1: history (no consumer locks held).
    enum Update {
        /// Latest spot, plus the full closes vec when a new minute banked.
        Spot(f64, Option<Vec<f64>>),
        /// A new settlement mark: (minute key, value).
        Mark(Topic, i64, f64),
    }
    let update = {
        let mut history = inner.history.lock().unwrap();
        let h = history.entry(s.symbol.clone()).or_default();
        match s.topic {
            Topic::Spot => {
                h.spot = s.value;
                h.spot_ts = now;
                let minute = s.ts_s().div_euclid(60) * 60;
                let banked = if minute > h.last_close_min {
                    h.last_close_min = minute;
                    h.closes.push(s.value);
                    if h.closes.len() > CLOSES_CAP {
                        let drop_n = h.closes.len() - CLOSES_CAP;
                        h.closes.drain(0..drop_n);
                    }
                    Some(h.closes.clone())
                } else {
                    None
                };
                Some(Update::Spot(s.value, banked))
            }
            topic => {
                let ts = s.ts_s();
                let mark = ts.div_euclid(60) * 60;
                let last = match topic {
                    Topic::Twap30 => h.last_mark_30,
                    _ => h.last_mark_60,
                };
                if ts - mark > MARK_TOL_S || mark <= last {
                    None
                } else {
                    match topic {
                        Topic::Twap30 => h.last_mark_30 = mark,
                        _ => h.last_mark_60 = mark,
                    }
                    // The print AT `mark` is the TWAP over the minute that
                    // just ended, so it keys as that minute.
                    let key = mark - 60;
                    let marks = h.marks(topic);
                    marks.insert(key, s.value);
                    while marks.len() > HISTORY_MARKS {
                        let oldest = *marks.keys().next().expect("non-empty");
                        marks.remove(&oldest);
                    }
                    Some(Update::Mark(topic, key, s.value))
                }
            }
        }
    };
    let Some(update) = update else { return };

    // Phase 2: fan out.
    let consumers = inner.consumers.lock().unwrap();
    for c in consumers.iter().filter(|c| c.symbol == s.symbol) {
        let mut f = c.feed.lock().unwrap();
        match &update {
            Update::Spot(px, banked) => {
                f.spot = *px;
                f.spot_ts = now;
                f.last_err = None;
                if let Some(closes) = banked {
                    f.rho = lag1_autocorr(closes, 60);
                    f.closes = closes.clone();
                }
            }
            Update::Mark(topic, key, px) => {
                if *topic != c.twap {
                    continue;
                }
                f.per_min.insert(*key, *px);
                f.per_min.retain(|k, _| *k as f64 >= c.start - ARM_PER_MIN_KEEP_S);
            }
        }
    }
    drop(consumers);

    let mut h = inner.health.lock().unwrap();
    h.events += 1;
    h.last_event_at = now;
    h.last_err = None;
}

/// Record a feed failure on the affected consumers (all of them for a dead
/// socket, one symbol's for a lagging series), so a gated arm's reason names
/// the feed rather than saying a bare "feed stale".
fn mark_feeds_err(inner: &HubInner, symbol: Option<&str>, err: &str) {
    let consumers = inner.consumers.lock().unwrap();
    for c in consumers.iter().filter(|c| symbol.is_none_or(|s| c.symbol == s)) {
        c.feed.lock().unwrap().last_err = Some(format!("rtds {}", err));
    }
}

// ---------- the socket loop ---------------------------------------------

/// The transport the supervisor drives. A trait so the reconnect/subscribe
/// behaviour is testable without a network — a stream that only reconnects
/// correctly in production is a stream nobody has tested.
trait RtdsSocket {
    fn send_text(&mut self, text: &str) -> Result<(), String>;
    /// Next text frame; `Ok(None)` = read timed out or a non-text frame,
    /// which is silence, not failure.
    fn read_text(&mut self) -> Result<Option<String>, String>;
}

#[derive(Debug, Clone, Copy)]
struct SupervisorCfg {
    ping_s: f64,
    stall_s: f64,
    backoff_start_s: f64,
    backoff_max_s: f64,
    health_log_s: f64,
}

impl Default for SupervisorCfg {
    fn default() -> Self {
        Self {
            ping_s: PING_S,
            stall_s: STALL_S,
            backoff_start_s: BACKOFF_START_S,
            backoff_max_s: BACKOFF_MAX_S,
            health_log_s: HEALTH_LOG_S,
        }
    }
}

struct TungSocket(
    tungstenite::WebSocket<tungstenite::stream::MaybeTlsStream<std::net::TcpStream>>,
);

impl RtdsSocket for TungSocket {
    fn send_text(&mut self, text: &str) -> Result<(), String> {
        self.0
            .send(tungstenite::Message::text(text.to_string()))
            .map_err(|e| format!("send: {}", e))
    }

    fn read_text(&mut self) -> Result<Option<String>, String> {
        match self.0.read() {
            Ok(tungstenite::Message::Text(t)) => Ok(Some(t.as_str().to_string())),
            Ok(_) => Ok(None),
            Err(tungstenite::Error::Io(e))
                if matches!(
                    e.kind(),
                    std::io::ErrorKind::WouldBlock | std::io::ErrorKind::TimedOut
                ) =>
            {
                Ok(None)
            }
            Err(e) => Err(format!("read: {}", e)),
        }
    }
}

fn live_connect() -> Result<TungSocket, String> {
    let (mut sock, _) = tungstenite::connect(RTDS_URL).map_err(|e| format!("connect: {}", e))?;
    // A read timeout is what turns the blocking read into the loop's clock —
    // pings, health lines and the stop flag all ride on it.
    let timeout = Some(std::time::Duration::from_secs(READ_TIMEOUT_S));
    match sock.get_mut() {
        tungstenite::stream::MaybeTlsStream::NativeTls(t) => {
            let _ = t.get_mut().set_read_timeout(timeout);
        }
        tungstenite::stream::MaybeTlsStream::Plain(t) => {
            let _ = t.set_read_timeout(timeout);
        }
        _ => {}
    }
    Ok(TungSocket(sock))
}

/// Sleep in slices so a stop request isn't held up by a 60s backoff.
fn wait(stop: &AtomicBool, secs: f64) {
    let mut left = secs;
    while left > 0.0 {
        if stop.load(Ordering::SeqCst) {
            return;
        }
        let slice = left.min(0.25);
        std::thread::sleep(std::time::Duration::from_secs_f64(slice));
        left -= slice;
    }
}

fn unix_now() -> f64 {
    std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map(|d| d.as_secs_f64())
        .unwrap_or(0.0)
}

/// Connect, subscribe, route, reconnect — forever, until the stop flag.
///
/// Generic over the transport and over how a connection is made, so the
/// reconnect path (does the NEW socket re-subscribe all three topics?) is a
/// unit test rather than an operational surprise.
fn run_loop<S: RtdsSocket>(
    inner: &HubInner,
    cfg: &SupervisorCfg,
    mut connect: impl FnMut() -> Result<S, String>,
) {
    let mut backoff = cfg.backoff_start_s;
    let mut rate_at = unix_now();
    let mut rate_events = 0u64;

    while !inner.stop.load(Ordering::SeqCst) {
        let session_err = match connect() {
            Ok(mut sock) => {
                let t_conn = unix_now();
                // The subscribe frame goes on EVERY connection, including
                // every reconnect: the server holds no state for us.
                match sock.send_text(&subscribe_message()) {
                    Ok(()) => {
                        inner.health.lock().unwrap().connected = true;
                        tracing::info!(
                            url = RTDS_URL,
                            topics = TOPICS.len(),
                            "RTDS connected — subscribed all topics, no symbol filters"
                        );
                        run_session(inner, cfg, &mut sock, t_conn, &mut rate_at, &mut rate_events)
                    }
                    Err(e) => Some(e),
                }
            }
            Err(e) => Some(e),
        };

        inner.health.lock().unwrap().connected = false;
        // Stop first, and only then record: a shutdown must not overwrite
        // the error that actually explains the last real failure.
        if inner.stop.load(Ordering::SeqCst) {
            break;
        }
        if let Some(e) = &session_err {
            inner.health.lock().unwrap().last_err = Some(e.clone());
            mark_feeds_err(inner, None, e);
            tracing::warn!(err = %e, backoff_s = backoff, "RTDS disconnected — reconnecting");
        }
        inner.health.lock().unwrap().reconnects += 1;
        wait(&inner.stop, backoff);
        backoff = (backoff * 2.0).min(cfg.backoff_max_s);
    }
    inner.health.lock().unwrap().connected = false;
}

/// One connected session. Returns the error that ended it, or None on a
/// clean stop.
fn run_session<S: RtdsSocket>(
    inner: &HubInner,
    cfg: &SupervisorCfg,
    sock: &mut S,
    t_conn: f64,
    rate_at: &mut f64,
    rate_events: &mut u64,
) -> Option<String> {
    let mut last_ping = t_conn;
    let mut last_msg: Option<f64> = None;
    let mut health_at = unix_now();

    loop {
        if inner.stop.load(Ordering::SeqCst) {
            return None;
        }
        let now = unix_now();
        if now - last_ping >= cfg.ping_s {
            last_ping = now;
            if let Err(e) = sock.send_text("PING") {
                return Some(e);
            }
        }
        if now - health_at >= cfg.health_log_s {
            log_health(inner, now, rate_at, rate_events);
            health_at = now;
        }
        match sock.read_text() {
            Ok(Some(text)) => {
                let text = text.trim();
                if text.is_empty() || text.eq_ignore_ascii_case("PONG") {
                    continue;
                }
                if let Some(sample) = parse_sample(text) {
                    route_sample(inner, &sample, unix_now());
                    last_msg = Some(unix_now());
                }
            }
            Ok(None) => {
                // Measure silence from the CONNECT when nothing has ever
                // arrived: a subscription the server quietly declined looks
                // exactly like a healthy idle socket.
                let quiet_since = last_msg.unwrap_or(t_conn);
                if now - quiet_since > cfg.stall_s {
                    return Some(format!("stalled {:.0}s with the socket open", now - quiet_since));
                }
            }
            Err(e) => return Some(e),
        }
    }
}

/// The periodic "RTDS health" INFO line, shaped like the engine's "Book
/// health" so one grep finds both feeds' state.
fn log_health(inner: &HubInner, now: f64, rate_at: &mut f64, rate_events: &mut u64) {
    let (events, rate, connected, reconnects, dropped, consumers, symbols, last_age) = {
        let history_len = inner.history.lock().unwrap().len();
        let mut h = inner.health.lock().unwrap();
        h.symbols = history_len;
        let secs = (now - *rate_at).max(0.001);
        h.events_per_s = (h.events - *rate_events) as f64 / secs;
        (
            h.events,
            h.events_per_s,
            h.connected,
            h.reconnects,
            h.dropped_lagged,
            h.consumers,
            h.symbols,
            h.last_event_age_s(now),
        )
    };
    *rate_at = now;
    *rate_events = events;
    tracing::info!(
        rtds_connected = connected,
        rtds_events = events,
        rtds_events_per_s = format!("{:.1}", rate),
        rtds_last_event_age_s = ?last_age.map(|a| (a * 10.0).round() / 10.0),
        rtds_reconnects = reconnects,
        rtds_dropped_lagged = dropped,
        rtds_consumers = consumers,
        rtds_symbols = symbols,
        "RTDS health"
    );
}

#[cfg(test)]
mod tests {
    use super::*;

    fn envelope(topic: &str, symbol: &str, ts_ms: i64, value: f64) -> String {
        // E18 the way the wire carries it, so the tests exercise the exact
        // parse the live feed hits.
        let fav = format!("{:.0}", value * 1e18);
        serde_json::json!({
            "connection_id": "c", "topic": topic, "type": "update",
            "timestamp": ts_ms,
            "payload": {
                "symbol": symbol, "timestamp": ts_ms,
                "value": value, "full_accuracy_value": fav,
            },
        })
        .to_string()
    }

    fn feed() -> Arc<Mutex<FeedState>> {
        Arc::new(Mutex::new(FeedState::default()))
    }

    // --- pure shapes -----------------------------------------------------

    #[test]
    fn subscribe_is_one_entry_per_topic_with_no_filters() {
        // The trap this pins: per-symbol entries make the server silently
        // downgrade the whole subscription to Binance snapshots.
        let v: serde_json::Value = serde_json::from_str(&subscribe_message()).unwrap();
        assert_eq!(v["action"], "subscribe");
        let subs = v["subscriptions"].as_array().unwrap();
        assert_eq!(subs.len(), 3);
        let topics: Vec<&str> = subs.iter().map(|s| s["topic"].as_str().unwrap()).collect();
        assert!(topics.contains(&TOPIC_SPOT));
        assert!(topics.contains(&TOPIC_TWAP30));
        assert!(topics.contains(&TOPIC_TWAP60));
        for s in subs {
            assert_eq!(s["type"], "update");
            assert!(s.get("filters").is_none(), "a symbol filter downgrades the stream");
            assert!(s.get("symbol").is_none());
        }
    }

    #[test]
    fn symbol_mapping_covers_the_stream_and_refuses_the_rest() {
        assert_eq!(rtds_symbol("XRPUSDT").as_deref(), Some("xrp/usd"));
        assert_eq!(rtds_symbol("btcusdt").as_deref(), Some("btc/usd"));
        assert_eq!(rtds_symbol("DOGEUSDC").as_deref(), Some("doge/usd"));
        assert_eq!(rtds_symbol("ETHUSD").as_deref(), Some("eth/usd"));
        // Not carried by the stream, and not a USD pair at all.
        assert_eq!(rtds_symbol("PEPEUSDT"), None);
        assert_eq!(rtds_symbol("ETHBTC"), None);
        assert_eq!(rtds_symbol("USDT"), None, "no base left");
    }

    #[test]
    fn twap_topic_follows_the_settlement_width() {
        assert_eq!(twap_topic_for(30.0), Topic::Twap30, "5m windows settle on the 30s TWAP");
        assert_eq!(twap_topic_for(60.0), Topic::Twap60);
    }

    #[test]
    fn parse_prefers_full_accuracy_over_the_display_float() {
        let raw = serde_json::json!({
            "topic": TOPIC_SPOT, "type": "update", "timestamp": 1_787_442_000_000i64,
            "payload": {
                "symbol": "xrp/usd", "timestamp": 1_787_442_000_000i64,
                "value": 2.34, "full_accuracy_value": "2345678901234567890",
            },
        })
        .to_string();
        let s = parse_sample(&raw).unwrap();
        assert_eq!(s.topic, Topic::Spot);
        assert_eq!(s.symbol, "xrp/usd");
        assert_eq!(s.ts_ms, 1_787_442_000_000);
        assert!((s.value - 2.345_678_901_234_568).abs() < 1e-12, "got {}", s.value);
        // Fallback when the exact value is absent.
        let lossy = serde_json::json!({
            "topic": TOPIC_SPOT, "payload": {
                "symbol": "xrp/usd", "timestamp": 1_787_442_000_000i64, "value": 2.34,
            },
        })
        .to_string();
        assert_eq!(parse_sample(&lossy).unwrap().value, 2.34);
    }

    #[test]
    fn parse_rejects_off_topic_and_malformed_frames() {
        // The Binance snapshot topic a downgraded subscription would send.
        assert!(parse_sample(&envelope("crypto_prices", "xrp/usd", 1000, 2.0)).is_none());
        assert!(parse_sample("PONG").is_none());
        assert!(parse_sample("{not json").is_none());
        assert!(parse_sample(&envelope(TOPIC_SPOT, "", 1000, 2.0)).is_none());
        let no_ts = serde_json::json!({
            "topic": TOPIC_SPOT, "payload": {"symbol": "xrp/usd", "value": 2.0},
        })
        .to_string();
        assert!(parse_sample(&no_ts).is_none());
        let zero = envelope(TOPIC_SPOT, "xrp/usd", 1000, 0.0);
        assert!(parse_sample(&zero).is_none(), "a zero price is never a price");
    }

    // --- routing ---------------------------------------------------------

    /// Drive one envelope through the hub at wall clock `now`.
    fn feed_msg(hub: &RtdsHub, topic: &str, symbol: &str, ts_s: i64, value: f64, now: f64) {
        let s = parse_sample(&envelope(topic, symbol, ts_s * 1000, value)).expect("parses");
        route_sample(&hub.inner, &s, now);
    }

    #[test]
    fn supervisor_routes_each_symbol_only_to_its_own_consumers() {
        let hub = RtdsHub::new();
        let (xrp, doge) = (feed(), feed());
        // Registrations only — ensure_started is not reached from register's
        // caller here, so no thread and no socket.
        let _sx = hub.register("xrp/usd", 30.0, 1_787_442_000.0, xrp.clone());
        let _sd = hub.register("doge/usd", 30.0, 1_787_442_000.0, doge.clone());

        feed_msg(&hub, TOPIC_SPOT, "xrp/usd", 1_787_442_100, 2.5, 1_787_442_100.0);
        feed_msg(&hub, TOPIC_SPOT, "doge/usd", 1_787_442_100, 0.31, 1_787_442_100.0);
        // A symbol nobody armed still lands in history, never in a feed.
        feed_msg(&hub, TOPIC_SPOT, "btc/usd", 1_787_442_100, 65000.0, 1_787_442_100.0);

        assert_eq!(xrp.lock().unwrap().spot, 2.5);
        assert_eq!(doge.lock().unwrap().spot, 0.31);
        assert_eq!(hub.health().events, 3);
        assert_eq!(hub.inner.history.lock().unwrap().len(), 3);

        // Two arms on the SAME symbol (a 5m and its roll successor) both get fed.
        let second = feed();
        let _s2 = hub.register("xrp/usd", 30.0, 1_787_442_300.0, second.clone());
        feed_msg(&hub, TOPIC_SPOT, "xrp/usd", 1_787_442_101, 2.6, 1_787_442_101.0);
        assert_eq!(xrp.lock().unwrap().spot, 2.6);
        assert_eq!(second.lock().unwrap().spot, 2.6);
    }

    #[test]
    fn per_min_holds_the_settlement_mark_at_the_window_start() {
        // The contract that makes eval_model's `per_min[start-60]` the real
        // settlement reference: the TWAP printed AT `start` keys as the
        // minute that just ended.
        let hub = RtdsHub::new();
        let start = 1_787_442_300.0; // a 5m boundary
        let f = feed();
        let _s = hub.register("xrp/usd", 30.0, start, f.clone());

        feed_msg(&hub, TOPIC_TWAP30, "xrp/usd", start as i64, 2.50, start);
        assert_eq!(
            f.lock().unwrap().per_min.get(&(start as i64 - 60)),
            Some(&2.50),
            "the print at the window start IS the range-start reference"
        );

        // In-window marks bank on each following minute.
        feed_msg(&hub, TOPIC_TWAP30, "xrp/usd", start as i64 + 60, 2.51, start + 60.0);
        assert_eq!(f.lock().unwrap().per_min.get(&(start as i64)), Some(&2.51));
    }

    #[test]
    fn per_min_takes_the_mark_once_and_only_near_the_minute() {
        let hub = RtdsHub::new();
        let start = 1_787_442_300.0;
        let f = feed();
        let _s = hub.register("xrp/usd", 30.0, start, f.clone());

        // Mid-minute prints are not marks — 1Hz would otherwise overwrite
        // the settlement reference sixty times with drifting values.
        feed_msg(&hub, TOPIC_TWAP30, "xrp/usd", start as i64 + 31, 9.99, start + 31.0);
        assert!(f.lock().unwrap().per_min.is_empty());

        // The exact print lands; a later one in the same minute cannot
        // overwrite it.
        feed_msg(&hub, TOPIC_TWAP30, "xrp/usd", start as i64 + 60, 2.51, start + 60.0);
        feed_msg(&hub, TOPIC_TWAP30, "xrp/usd", start as i64 + 61, 2.99, start + 61.0);
        assert_eq!(f.lock().unwrap().per_min.get(&(start as i64)), Some(&2.51));

        // A dropped `:00` sample falls back to the next print inside tolerance.
        feed_msg(&hub, TOPIC_TWAP30, "xrp/usd", start as i64 + 121, 2.52, start + 121.0);
        assert_eq!(f.lock().unwrap().per_min.get(&(start as i64 + 60)), Some(&2.52));
    }

    #[test]
    fn per_min_is_filled_by_the_arms_own_settlement_width() {
        // A 5m arm reads the 30s TWAP, a 15m arm the 60s one, off the same
        // socket at the same instant.
        let hub = RtdsHub::new();
        let start = 1_787_442_300.0;
        let (five, fifteen) = (feed(), feed());
        let _a = hub.register("xrp/usd", 30.0, start, five.clone());
        let _b = hub.register("xrp/usd", 60.0, start, fifteen.clone());

        feed_msg(&hub, TOPIC_TWAP30, "xrp/usd", start as i64, 2.50, start);
        feed_msg(&hub, TOPIC_TWAP60, "xrp/usd", start as i64, 2.60, start);

        let key = start as i64 - 60;
        assert_eq!(five.lock().unwrap().per_min.get(&key), Some(&2.50));
        assert_eq!(fifteen.lock().unwrap().per_min.get(&key), Some(&2.60));
    }

    #[test]
    fn closes_bank_once_a_minute_and_carry_rho() {
        let hub = RtdsHub::new();
        let f = feed();
        let _s = hub.register("xrp/usd", 30.0, 1_787_442_000.0, f.clone());

        // 1Hz inside one minute: one close, not sixty.
        for i in 0..5 {
            let ts = 1_787_442_000 + i;
            feed_msg(&hub, TOPIC_SPOT, "xrp/usd", ts, 2.50, ts as f64);
        }
        assert_eq!(f.lock().unwrap().closes, vec![2.50]);

        // A mean-reverting minute tape must read as one to the regime signal.
        for m in 1..40 {
            let ts = 1_787_442_000 + m * 60;
            let px = if m % 2 == 0 { 2.50 } else { 2.53 };
            feed_msg(&hub, TOPIC_SPOT, "xrp/usd", ts, px, ts as f64);
        }
        let g = f.lock().unwrap();
        assert_eq!(g.closes.len(), 40, "one per minute");
        assert!(g.rho < -0.8, "alternating minutes are chop: rho {}", g.rho);
        assert_eq!(g.spot, 2.53, "spot still tracks the last print");
    }

    #[test]
    fn spot_freshness_is_receive_time_and_a_lagging_relay_stops_refreshing_it() {
        let hub = RtdsHub::new();
        let f = feed();
        let _s = hub.register("xrp/usd", 30.0, 1_787_442_000.0, f.clone());

        feed_msg(&hub, TOPIC_SPOT, "xrp/usd", 1_787_442_100, 2.50, 1_787_442_100.5);
        assert_eq!(f.lock().unwrap().spot_ts, 1_787_442_100.5, "stamped on arrival");

        // A print whose own clock is far behind is not evidence of a live
        // feed: spot_ts must NOT move, so MAX_SPOT_AGE_S gates the arm.
        feed_msg(&hub, TOPIC_SPOT, "xrp/usd", 1_787_442_090, 9.99, 1_787_442_130.0);
        let g = f.lock().unwrap();
        assert_eq!(g.spot, 2.50, "the stale print is not priced");
        assert_eq!(g.spot_ts, 1_787_442_100.5, "and does not refresh freshness");
        assert!(
            g.last_err.as_deref().unwrap_or("").contains("sample lag"),
            "and the arm's gate line names the relay: {:?}",
            g.last_err
        );
        drop(g);
        assert_eq!(hub.health().dropped_lagged, 1);
        assert!(hub.health().last_err.unwrap().contains("sample lag"));
    }

    #[test]
    fn a_new_arm_inherits_the_history_it_could_never_refetch() {
        // The feed has no backfill. A window armed after its start still
        // needs the reference printed at that start, and a roll chain must
        // not restart its vol history every five minutes.
        let hub = RtdsHub::new();
        let start = 1_787_442_300.0;
        let warm = feed();
        let _w = hub.register("xrp/usd", 30.0, start - 300.0, warm);

        feed_msg(&hub, TOPIC_TWAP30, "xrp/usd", start as i64, 2.50, start);
        for m in 0..10 {
            let ts = start as i64 + m * 60;
            feed_msg(&hub, TOPIC_SPOT, "xrp/usd", ts, 2.5 + m as f64 * 0.001, ts as f64);
        }

        let late = feed();
        let _l = hub.register("xrp/usd", 30.0, start, late.clone());
        let g = late.lock().unwrap();
        assert_eq!(
            g.per_min.get(&(start as i64 - 60)),
            Some(&2.50),
            "the range-start reference came off the hub, not the wire"
        );
        assert_eq!(g.closes.len(), 10, "and so did the vol history");
        assert!(g.spot > 0.0 && g.spot_ts > 0.0);
    }

    #[test]
    fn an_arm_stops_being_fed_the_moment_its_registration_drops() {
        let hub = RtdsHub::new();
        let f = feed();
        let sub = hub.register("xrp/usd", 30.0, 1_787_442_000.0, f.clone());
        feed_msg(&hub, TOPIC_SPOT, "xrp/usd", 1_787_442_100, 2.50, 1_787_442_100.0);
        assert_eq!(hub.health().consumers, 1);

        drop(sub);
        assert_eq!(hub.health().consumers, 0, "a retired arm leaves no consumer behind");
        feed_msg(&hub, TOPIC_SPOT, "xrp/usd", 1_787_442_101, 9.99, 1_787_442_101.0);
        assert_eq!(f.lock().unwrap().spot, 2.50, "and is not written to again");
    }

    #[test]
    fn arm_per_min_is_pruned_to_what_the_model_reads() {
        let hub = RtdsHub::new();
        let start = 1_787_442_300.0;
        let f = feed();
        let _s = hub.register("xrp/usd", 30.0, start, f.clone());
        // Marks from well before the window (oldest first — the stream is
        // monotonic), then one inside it.
        for m in (1..=5).rev() {
            let ts = start as i64 - m * 60;
            feed_msg(&hub, TOPIC_TWAP30, "xrp/usd", ts, 2.4, ts as f64);
        }
        feed_msg(&hub, TOPIC_TWAP30, "xrp/usd", start as i64, 2.5, start);
        let g = f.lock().unwrap();
        assert!(g.per_min.contains_key(&(start as i64 - 60)), "the reference survives");
        assert!(
            g.per_min.keys().all(|k| *k as f64 >= start - ARM_PER_MIN_KEEP_S),
            "older marks are dropped: {:?}",
            g.per_min.keys().collect::<Vec<_>>()
        );
    }

    // --- the socket loop -------------------------------------------------

    /// A scripted socket: yields its queued frames, then fails. Records
    /// every frame the supervisor sent it.
    struct FakeSocket {
        incoming: Vec<String>,
        sent: Frames,
        die_after: usize,
        read_count: usize,
    }

    impl RtdsSocket for FakeSocket {
        fn send_text(&mut self, text: &str) -> Result<(), String> {
            self.sent.lock().unwrap().push(text.to_string());
            Ok(())
        }

        fn read_text(&mut self) -> Result<Option<String>, String> {
            self.read_count += 1;
            if self.read_count > self.die_after {
                return Err("socket closed".to_string());
            }
            Ok(self.incoming.pop())
        }
    }

    /// Every clock disabled and no backoff, so a session ends exactly when
    /// the scripted socket says it does.
    fn fast_cfg() -> SupervisorCfg {
        SupervisorCfg {
            ping_s: f64::INFINITY,
            stall_s: f64::INFINITY,
            backoff_start_s: 0.0,
            backoff_max_s: 0.0,
            health_log_s: f64::INFINITY,
        }
    }

    /// The frames one scripted session received.
    type Frames = Arc<Mutex<Vec<String>>>;

    #[test]
    fn every_reconnect_resubscribes_all_three_topics() {
        // The failure this pins: a reconnect that routes samples again but
        // never re-sends the subscription. The server holds no state for us,
        // so that socket is open, silent, and looks perfectly healthy.
        let hub = RtdsHub::new();
        let f = feed();
        let _s = hub.register("xrp/usd", 30.0, 1_787_442_000.0, f.clone());

        let sessions: Arc<Mutex<Vec<Frames>>> = Arc::new(Mutex::new(Vec::new()));
        let inner = hub.inner.clone();
        let sessions_c = sessions.clone();
        let mut attempts = 0;
        run_loop(&hub.inner, &fast_cfg(), move || {
            attempts += 1;
            if attempts > 2 {
                // Third connection attempt: stop instead, so the loop ends.
                inner.stop.store(true, Ordering::SeqCst);
                return Err("stopping".to_string());
            }
            let sent = Arc::new(Mutex::new(Vec::new()));
            sessions_c.lock().unwrap().push(sent.clone());
            Ok(FakeSocket {
                incoming: vec![envelope(
                    TOPIC_SPOT,
                    "xrp/usd",
                    unix_now() as i64 * 1000,
                    2.5,
                )],
                sent,
                die_after: 2,
                read_count: 0,
            })
        });

        let sessions = sessions.lock().unwrap();
        assert_eq!(sessions.len(), 2, "it reconnected");
        for (i, sent) in sessions.iter().enumerate() {
            let frames = sent.lock().unwrap();
            let first = frames.first().unwrap_or_else(|| panic!("session {i} sent nothing"));
            let v: serde_json::Value = serde_json::from_str(first).unwrap();
            assert_eq!(v["action"], "subscribe", "session {i} must subscribe first");
            let topics: Vec<&str> = v["subscriptions"]
                .as_array()
                .unwrap()
                .iter()
                .map(|s| s["topic"].as_str().unwrap())
                .collect();
            assert_eq!(topics.len(), 3, "session {i} subscribed {topics:?}");
        }
        // And the samples that did arrive were routed, both sessions.
        assert_eq!(hub.health().events, 2);
        assert!(hub.health().reconnects >= 2);
        assert!(!hub.health().connected, "the loop leaves the socket marked down");
    }

    #[test]
    fn a_dead_session_names_the_feed_in_every_consumers_gate_reason() {
        let hub = RtdsHub::new();
        let f = feed();
        let _s = hub.register("xrp/usd", 30.0, 1_787_442_000.0, f.clone());

        let inner = hub.inner.clone();
        let mut attempts = 0;
        run_loop(&hub.inner, &fast_cfg(), move || {
            attempts += 1;
            if attempts > 1 {
                inner.stop.store(true, Ordering::SeqCst);
            }
            Err::<FakeSocket, String>("connect refused".to_string())
        });

        let err = f.lock().unwrap().last_err.clone().expect("the arm is told");
        assert!(err.starts_with("rtds "), "{err}");
        assert!(err.contains("connect refused"), "{err}");
        assert!(hub.health().last_err.unwrap().contains("connect refused"));
    }

    #[test]
    fn a_silent_socket_is_a_dead_socket() {
        // "Open but dead" is this feed's signature failure: the stall clock
        // runs from the CONNECT when nothing ever arrived.
        let hub = RtdsHub::new();
        let cfg = SupervisorCfg { stall_s: -1.0, ..fast_cfg() };
        let inner = hub.inner.clone();
        let mut attempts = 0;
        run_loop(&hub.inner, &cfg, move || {
            attempts += 1;
            if attempts > 1 {
                inner.stop.store(true, Ordering::SeqCst);
                return Err("stopping".to_string());
            }
            Ok(FakeSocket {
                incoming: Vec::new(), // silence
                sent: Arc::new(Mutex::new(Vec::new())),
                die_after: usize::MAX,
                read_count: 0,
            })
        });
        assert!(
            hub.health().last_err.unwrap().contains("stalled"),
            "a stalled session must reconnect, not sit there"
        );
    }

    #[test]
    fn health_starts_empty_and_tracks_the_stream() {
        let hub = RtdsHub::new();
        let h = hub.health();
        assert!(!h.started && !h.connected && h.events == 0);
        assert!(h.json(1_787_442_000.0)["last_event_age_s"].is_null(), "no events, no age");

        let f = feed();
        let _s = hub.register("xrp/usd", 30.0, 1_787_442_000.0, f);
        feed_msg(&hub, TOPIC_SPOT, "xrp/usd", 1_787_442_100, 2.5, 1_787_442_100.0);
        let j = hub.health().json(1_787_442_102.0);
        assert_eq!(j["events"], 1);
        assert_eq!(j["consumers"], 1);
        assert_eq!(j["last_event_age_s"], 2.0);
    }
}
