//! Market-data WebSocket feed — the engine's authoritative book source.
//!
//! ## Why this exists
//!
//! The engine used to open the market WS inline in the main `select!`, inside
//! a `'reconnect` loop that only re-ran when the engine's blanket market
//! discovery (since deleted) found new tokens. Everything that subscribes at
//! RUNTIME — the updown arm/roll, the market scanner, `pmt engine subscribe`
//! — set a `ws_needs_reconnect` flag that nothing else ever read. With no tokens at
//! startup the engine logged "No subscriptions, running without WebSocket"
//! and stayed there for the whole session, so every book the strategies read
//! came from the 2s REST poller. That is the entire reason the measured book
//! age was REST-cadence bound.
//!
//! ## Shape
//!
//! One supervisor task owns one long-lived SDK client for the process. Token
//! subscriptions are incremental — a roll adds two assets and drops two, and
//! neither touches the socket. Per token we run one child task consuming BOTH
//! `book` snapshots and `price_change` deltas: the snapshots are rare (the
//! 40s live probe in `analysis/net_probe_raw-wslag.json` saw 902 of them
//! against 28,107 price_changes), so a book fed only by snapshots is barely
//! better than a poller.
//!
//! Reconnect/backoff, PING keepalive and resubscribe-on-reconnect all live in
//! the SDK's `ConnectionManager` (5s heartbeat, 15s pong timeout, exponential
//! backoff to 60s, infinite attempts) and its `SubscriptionManager`, which
//! replays every tracked asset id on connection recovery. What this module
//! adds is the engine-side half the SDK can't know about: which tokens are
//! currently wanted, and a health signal the REST poller and `/status` read.

use crate::orderbook::{now_ms, MarketDataHub};
use futures::StreamExt;
use polymarket_client_sdk_v2::clob::ws::{Client as WsClient, ChannelType};
use polymarket_client_sdk_v2::types::U256;
use std::collections::{HashMap, HashSet};
use std::str::FromStr;
use std::sync::atomic::{AtomicBool, AtomicI64, AtomicU64, AtomicUsize, Ordering};
use std::sync::Arc;
use std::time::Duration;
use tokio::sync::mpsc;
use tokio::task::JoinHandle;

/// How long the socket must stay down before we call the feed degraded, which
/// earns a warn and the `degraded` flag on `/status`. The REST poller does NOT
/// wait for this — it speeds back up the moment the socket drops, because a
/// 30s grace period at a 10s poll cadence would mean trading a 10s-old book.
const DEGRADE_AFTER: Duration = Duration::from_secs(30);

/// Cadence at which the supervisor samples SDK connection state and re-warns
/// about a sustained outage.
const STATE_POLL: Duration = Duration::from_secs(1);

/// Each token costs two `subscribe_*` calls (orderbook + prices), and the
/// SDK's asset refcount is per call — so tearing a token down takes the same
/// number of `unsubscribe_orderbook` calls to reach zero and actually emit
/// the server-side unsubscribe.
const SUBS_PER_TOKEN: usize = 2;

/// How long a dropped token's subscription is kept before the server-side
/// unsubscribe is actually sent.
///
/// The SDK drops the whole channel — and with it the socket — when the last
/// asset's refcount hits zero. A roll retires one window's two legs and arms
/// the next window's, and if the retirement lands first the set is briefly
/// empty, which would cost a full reconnect every five minutes. Holding the
/// dead subscription for a few seconds makes that flap impossible. It is free
/// on the book side: `unsubscribe_token` removes the hub entry first, and the
/// hub ignores events for tokens it isn't tracking.
const TEARDOWN_GRACE: Duration = Duration::from_secs(10);

/// Live health of the market WS, shared with the REST poller and the control
/// plane. Atomics, not a lock: the readers are on the hot path.
#[derive(Debug, Default)]
pub struct WsHealth {
    connected: AtomicBool,
    /// Unix ms of the last applied market event; 0 = none yet.
    last_event_ms: AtomicI64,
    /// Unix ms at which the socket last went down; 0 = up, or never opened.
    down_since_ms: AtomicI64,
    events: AtomicU64,
    tokens: AtomicUsize,
}

impl WsHealth {
    pub fn is_connected(&self) -> bool {
        self.connected.load(Ordering::Relaxed)
    }

    /// Market events applied since process start.
    pub fn events(&self) -> u64 {
        self.events.load(Ordering::Relaxed)
    }

    /// Tokens currently subscribed on the socket.
    pub fn tokens(&self) -> usize {
        self.tokens.load(Ordering::Relaxed)
    }

    /// Age of the last applied event in ms, None if none has arrived.
    pub fn last_event_age_ms(&self) -> Option<i64> {
        match self.last_event_ms.load(Ordering::Relaxed) {
            0 => None,
            ts => Some((now_ms() - ts).max(0)),
        }
    }

    /// How long the socket has been down, None while it is up (or while
    /// nothing is subscribed, which is not an outage).
    pub fn down_for_ms(&self) -> Option<i64> {
        match self.down_since_ms.load(Ordering::Relaxed) {
            0 => None,
            ts => Some((now_ms() - ts).max(0)),
        }
    }

    /// Sustained outage — the state that earns a warn and a `/status` flag.
    pub fn degraded(&self) -> bool {
        self.down_for_ms()
            .is_some_and(|ms| ms >= DEGRADE_AFTER.as_millis() as i64)
    }

    fn record_events(&self, n: u64) {
        if n == 0 {
            return;
        }
        self.events.fetch_add(n, Ordering::Relaxed);
        self.last_event_ms.store(now_ms(), Ordering::Relaxed);
    }

    /// Fold one connection-state sample in, returning the transition (if any)
    /// so the caller can log it once rather than every tick.
    fn observe(&self, connected: bool, has_tokens: bool) -> Option<bool> {
        let was = self.connected.swap(connected, Ordering::Relaxed);
        if connected || !has_tokens {
            self.down_since_ms.store(0, Ordering::Relaxed);
        } else if self.down_since_ms.load(Ordering::Relaxed) == 0 {
            self.down_since_ms.store(now_ms(), Ordering::Relaxed);
        }
        (was != connected).then_some(connected)
    }
}

/// What the engine asks the supervisor to do. Unbounded: these are rare
/// (a roll is four messages every five minutes) and the send sites are
/// `&mut self` engine methods we don't want to make fallible.
enum WsCommand {
    Subscribe(String),
    Unsubscribe(String),
}

/// Handle on the WS feed. Dropping it does not stop the feed; call `abort`.
pub struct WsFeed {
    cmd_tx: mpsc::UnboundedSender<WsCommand>,
    health: Arc<WsHealth>,
    task: JoinHandle<()>,
}

impl WsFeed {
    /// Start the supervisor. The socket itself is opened lazily by the SDK on
    /// the first subscription, so calling this with nothing subscribed costs
    /// one idle task.
    pub fn spawn(hub: Arc<MarketDataHub>) -> Self {
        let (cmd_tx, cmd_rx) = mpsc::unbounded_channel();
        let health = Arc::new(WsHealth::default());
        let task = tokio::spawn(supervise(hub, health.clone(), cmd_rx));
        Self {
            cmd_tx,
            health,
            task,
        }
    }

    /// Start streaming a token. Idempotent.
    pub fn subscribe(&self, token_id: &str) {
        let _ = self.cmd_tx.send(WsCommand::Subscribe(token_id.to_string()));
    }

    /// Stop streaming a token. Idempotent.
    pub fn unsubscribe(&self, token_id: &str) {
        let _ = self
            .cmd_tx
            .send(WsCommand::Unsubscribe(token_id.to_string()));
    }

    pub fn health(&self) -> Arc<WsHealth> {
        self.health.clone()
    }

    pub fn abort(&self) {
        self.task.abort();
    }
}

/// Which tokens to add and which to drop to move from `current` to `desired`.
///
/// Pulled out as a pure function because the roll cadence churns the token set
/// every five minutes and getting this wrong is invisible: subscribe-twice is
/// a silent refcount leak, drop-then-never-add is a silently dead book.
pub fn reconcile(
    current: &HashSet<String>,
    desired: &HashSet<String>,
) -> (Vec<String>, Vec<String>) {
    let mut to_add: Vec<String> = desired.difference(current).cloned().collect();
    let mut to_drop: Vec<String> = current.difference(desired).cloned().collect();
    to_add.sort();
    to_drop.sort();
    (to_add, to_drop)
}

/// One token's live subscription: the child task draining its merged stream,
/// plus what the SDK needs to tear the server-side subscription down.
struct TokenSub {
    asset_id: U256,
    task: JoinHandle<()>,
}

async fn supervise(
    hub: Arc<MarketDataHub>,
    health: Arc<WsHealth>,
    mut cmd_rx: mpsc::UnboundedReceiver<WsCommand>,
) {
    let client = WsClient::default();
    let mut subs: HashMap<String, TokenSub> = HashMap::new();
    // Dropped tokens still holding their subscription until TEARDOWN_GRACE.
    let mut retiring: HashMap<String, (TokenSub, std::time::Instant)> = HashMap::new();
    let mut state_timer = tokio::time::interval(STATE_POLL);
    state_timer.set_missed_tick_behavior(tokio::time::MissedTickBehavior::Delay);
    let mut warned_at: Option<std::time::Instant> = None;

    loop {
        tokio::select! {
            cmd = cmd_rx.recv() => {
                let Some(cmd) = cmd else { break };
                match cmd {
                    WsCommand::Subscribe(token_id) => {
                        if subs.contains_key(&token_id) {
                            continue;
                        }
                        // Still inside its grace window: take the live
                        // subscription back rather than churning the
                        // refcount, which would risk the empty-set teardown
                        // the grace exists to prevent.
                        if let Some((sub, _)) = retiring.remove(&token_id) {
                            subs.insert(token_id.clone(), sub);
                            tracing::info!(
                                token_id = %token_id,
                                tokens = subs.len(),
                                "WS re-subscribed within grace window"
                            );
                        } else {
                            match start_token(&client, &hub, &health, &token_id) {
                                Ok(sub) => {
                                    subs.insert(token_id.clone(), sub);
                                    tracing::info!(
                                        token_id = %token_id,
                                        tokens = subs.len(),
                                        "WS subscribed"
                                    );
                                }
                                Err(e) => tracing::warn!(
                                    token_id = %token_id,
                                    error = %e,
                                    "WS subscribe failed; this token stays on the REST poller"
                                ),
                            }
                        }
                    }
                    WsCommand::Unsubscribe(token_id) => {
                        if let Some(sub) = subs.remove(&token_id) {
                            retiring.insert(
                                token_id.clone(),
                                (sub, std::time::Instant::now() + TEARDOWN_GRACE),
                            );
                            tracing::info!(
                                token_id = %token_id,
                                tokens = subs.len(),
                                "WS unsubscribed (teardown pending)"
                            );
                        }
                    }
                }
                health.tokens.store(subs.len(), Ordering::Relaxed);
            }

            _ = state_timer.tick() => {
                // Retire anything past its grace window.
                let now = std::time::Instant::now();
                let expired: Vec<String> = retiring
                    .iter()
                    .filter(|(_, (_, at))| *at <= now)
                    .map(|(t, _)| t.clone())
                    .collect();
                for token_id in expired {
                    let Some((sub, _)) = retiring.remove(&token_id) else {
                        continue;
                    };
                    sub.task.abort();
                    for _ in 0..SUBS_PER_TOKEN {
                        if let Err(e) = client.unsubscribe_orderbook(&[sub.asset_id]) {
                            tracing::debug!(
                                token_id = %token_id,
                                error = %e,
                                "WS unsubscribe request failed"
                            );
                            break;
                        }
                    }
                    tracing::debug!(token_id = %token_id, "WS subscription torn down");
                }

                let has_tokens = !subs.is_empty();
                let connected = has_tokens
                    && client.connection_state(ChannelType::Market).is_connected();

                if let Some(now_connected) = health.observe(connected, has_tokens) {
                    if now_connected {
                        warned_at = None;
                        tracing::info!(tokens = subs.len(), "WS market feed connected");
                    } else {
                        tracing::warn!(
                            tokens = subs.len(),
                            "WS market feed dropped; REST poller back to fast cadence"
                        );
                    }
                }

                // Sustained outage: repeat the warn on the degrade interval so
                // a dark feed can't hide behind one line at the top of a log.
                if health.degraded()
                    && warned_at.is_none_or(|t| t.elapsed() >= DEGRADE_AFTER)
                {
                    warned_at = Some(std::time::Instant::now());
                    tracing::warn!(
                        down_for_s = health.down_for_ms().unwrap_or(0) / 1000,
                        tokens = subs.len(),
                        "WS market feed still down — books are REST-cadence bound"
                    );
                }
            }
        }
    }

    for (_, sub) in subs.drain() {
        sub.task.abort();
    }
    for (_, (sub, _)) in retiring.drain() {
        sub.task.abort();
    }
}

/// Open one token's book + price streams and spawn the task that drains them
/// into the hub.
fn start_token(
    client: &WsClient,
    hub: &Arc<MarketDataHub>,
    health: &Arc<WsHealth>,
    token_id: &str,
) -> Result<TokenSub, String> {
    let asset_id = U256::from_str(token_id).map_err(|e| format!("bad token id: {}", e))?;

    let books = client
        .subscribe_orderbook(vec![asset_id])
        .map_err(|e| format!("subscribe_orderbook: {}", e))?;
    let prices = client
        .subscribe_prices(vec![asset_id])
        .map_err(|e| format!("subscribe_prices: {}", e))?;

    let hub = hub.clone();
    let health = health.clone();
    let token = token_id.to_string();

    // One merged stream rather than a select! over two: a completed
    // async-stream must not be polled again, and `stream::select` ends
    // cleanly only when both halves are done.
    let task = tokio::spawn(async move {
        let books = books.map(FeedItem::Book);
        let prices = prices.map(FeedItem::Price);
        let mut merged = Box::pin(futures::stream::select(books, prices));

        while let Some(item) = merged.next().await {
            match item {
                FeedItem::Book(Ok(update)) => {
                    if hub.apply_ws_book(&update).await {
                        health.record_events(1);
                    }
                }
                FeedItem::Price(Ok(pc)) => {
                    let n = hub.apply_ws_price_change(&pc).await;
                    health.record_events(n as u64);
                }
                FeedItem::Book(Err(e)) | FeedItem::Price(Err(e)) => {
                    // The SDK reconnects underneath us; a parse/transport
                    // error on one message is not a reason to drop the token.
                    tracing::debug!(token_id = %token, error = %e, "WS market message error");
                }
            }
        }
        tracing::warn!(token_id = %token, "WS token stream ended");
    });

    Ok(TokenSub { asset_id, task })
}

/// Tagged union so the two typed streams can be merged into one.
enum FeedItem<B, P> {
    Book(B),
    Price(P),
}

#[cfg(test)]
mod tests {
    use super::*;

    fn set(items: &[&str]) -> HashSet<String> {
        items.iter().map(|s| (*s).to_string()).collect()
    }

    #[test]
    fn reconcile_adds_and_drops_on_a_roll() {
        // A roll retires one window's two legs and arms the next window's.
        let current = set(&["up_1", "dn_1"]);
        let desired = set(&["up_2", "dn_2"]);
        let (add, drop) = reconcile(&current, &desired);
        assert_eq!(add, vec!["dn_2", "up_2"]);
        assert_eq!(drop, vec!["dn_1", "up_1"]);
    }

    #[test]
    fn reconcile_keeps_overlap_untouched() {
        // The overlap must NOT be re-subscribed: a duplicate subscribe leaks
        // an SDK refcount and the token then survives its own unsubscribe.
        let current = set(&["a", "b", "c"]);
        let desired = set(&["b", "c", "d"]);
        let (add, drop) = reconcile(&current, &desired);
        assert_eq!(add, vec!["d"]);
        assert_eq!(drop, vec!["a"]);
    }

    #[test]
    fn reconcile_noop_when_sets_match() {
        let s = set(&["a", "b"]);
        let (add, drop) = reconcile(&s, &s);
        assert!(add.is_empty() && drop.is_empty());
    }

    #[test]
    fn reconcile_from_empty_subscribes_everything() {
        let (add, drop) = reconcile(&set(&[]), &set(&["a", "b"]));
        assert_eq!(add, vec!["a", "b"]);
        assert!(drop.is_empty());
    }

    #[test]
    fn health_starts_disconnected_but_not_down() {
        // No subscriptions is not an outage — otherwise a freshly started
        // engine with nothing armed would page as a dead feed.
        let h = WsHealth::default();
        assert!(!h.is_connected());
        assert_eq!(h.down_for_ms(), None);
        assert!(!h.degraded());
        assert_eq!(h.last_event_age_ms(), None);
    }

    #[test]
    fn health_tracks_connect_and_drop_transitions() {
        let h = WsHealth::default();
        assert_eq!(h.observe(true, true), Some(true)); // transition up
        assert_eq!(h.observe(true, true), None); // steady state, no log
        assert!(h.is_connected());
        assert_eq!(h.down_for_ms(), None);

        assert_eq!(h.observe(false, true), Some(false)); // transition down
        assert!(!h.is_connected());
        assert!(h.down_for_ms().is_some());
        // Down, but not yet long enough to call degraded.
        assert!(!h.degraded());

        assert_eq!(h.observe(true, true), Some(true));
        assert_eq!(h.down_for_ms(), None, "reconnect clears the outage clock");
    }

    #[test]
    fn health_counts_events_and_ages_them() {
        let h = WsHealth::default();
        h.record_events(0);
        assert_eq!(h.events(), 0);
        assert_eq!(h.last_event_age_ms(), None, "a zero-event batch is not a heartbeat");
        h.record_events(3);
        h.record_events(2);
        assert_eq!(h.events(), 5);
        assert!(h.last_event_age_ms().is_some_and(|a| a < 1_000));
    }
}
