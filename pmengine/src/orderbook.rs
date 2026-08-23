//! Order book management with full depth tracking.
//!
//! Maintains local order book state from WebSocket updates and provides
//! broadcast channels for market data distribution.

use async_broadcast::{Receiver, Sender};
use polymarket_client_sdk_v2::clob::ws::types::response::{
    BookUpdate, OrderBookLevel, PriceChange,
};
use polymarket_client_sdk_v2::clob::types::Side as WsSide;
use rust_decimal::Decimal;
use std::collections::{HashMap, VecDeque};
use std::sync::Arc;
use std::time::Duration;
use tokio::sync::RwLock;

/// Local-receipt age past which a REST snapshot is applied even though its
/// event timestamp looks older than the state we already hold.
///
/// The staleness compare is between two different clocks (the WS event ts and
/// the REST snapshot ts), so a persistent skew could otherwise lock REST out
/// forever and freeze a book that the WS has silently stopped feeding. This
/// bound is the escape hatch: past it, a snapshot is better than a fossil.
const REST_OVERRIDE_STALE_MS: i64 = 5_000;

/// Wall clock in unix millis — book age is measured against this, not the
/// exchange's event timestamp, so a skewed venue clock can't hide staleness.
pub fn now_ms() -> i64 {
    chrono::Utc::now().timestamp_millis()
}

/// Which feed last wrote a book's state.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum BookSource {
    /// Created empty by a subscription, never fed.
    Init,
    /// Market WebSocket (`book` snapshot or `price_change` delta).
    Ws,
    /// REST `/book` poll.
    Rest,
}

impl BookSource {
    /// Stable wire name — this is what lands in the book tape's `src` field.
    pub fn as_str(self) -> &'static str {
        match self {
            Self::Init => "init",
            Self::Ws => "ws",
            Self::Rest => "rest",
        }
    }
}

/// A single price level in the order book.
#[derive(Debug, Clone)]
pub struct Level {
    pub price: Decimal,
    pub size: Decimal,
}

impl From<&OrderBookLevel> for Level {
    fn from(l: &OrderBookLevel) -> Self {
        Self {
            price: l.price,
            size: l.size,
        }
    }
}

/// Full-depth order book for a single token.
#[derive(Debug, Clone)]
pub struct OrderBook {
    pub token_id: String,
    /// Bid levels, sorted by price descending (best bid first)
    pub bids: Vec<Level>,
    /// Ask levels, sorted by price ascending (best ask first)
    pub asks: Vec<Level>,
    /// Venue event timestamp of the state we hold (Unix ms)
    pub timestamp: i64,
    /// Book hash for validation
    pub hash: Option<String>,
    /// Which feed wrote this state.
    pub source: BookSource,
    /// Local wall clock (unix ms) at which this state was accepted. Book age
    /// is `now - received_at_ms`; `timestamp` is the venue's own clock and is
    /// only used for the staleness compare between feeds.
    pub received_at_ms: i64,
    /// Accepted updates since this book was created — 0 means never fed.
    pub update_count: u64,
}

impl OrderBook {
    /// Create a new empty order book.
    pub fn new(token_id: String) -> Self {
        Self {
            token_id,
            bids: Vec::new(),
            asks: Vec::new(),
            timestamp: 0,
            hash: None,
            source: BookSource::Init,
            received_at_ms: 0,
            update_count: 0,
        }
    }

    /// Age of the held state in ms, or None if the book has never been fed.
    pub fn age_ms(&self, now_ms: i64) -> Option<i64> {
        if self.update_count == 0 {
            None
        } else {
            Some((now_ms - self.received_at_ms).max(0))
        }
    }

    /// Record who wrote this state and when. Every accepted update goes
    /// through here so `update_count` and `received_at_ms` can't drift apart
    /// from the data.
    fn stamp(&mut self, source: BookSource, event_ts: i64, now: i64) {
        self.source = source;
        self.timestamp = event_ts;
        self.received_at_ms = now;
        self.update_count += 1;
    }

    /// Update from a WebSocket book update.
    ///
    /// Polymarket's WS sends bids/asks unsorted (or in arbitrary order). We
    /// sort here so `best_bid` / `best_ask` (which take `.first()`) return
    /// the actual top of book.
    pub fn update_from_ws(&mut self, update: &BookUpdate) {
        let mut bids: Vec<Level> = update.bids.iter().map(Level::from).collect();
        bids.sort_by_key(|l| std::cmp::Reverse(l.price)); // descending: best bid first
        let mut asks: Vec<Level> = update.asks.iter().map(Level::from).collect();
        asks.sort_by_key(|l| l.price); // ascending: best ask first
        self.bids = bids;
        self.asks = asks;
        self.hash = update.hash.clone();
        self.stamp(BookSource::Ws, update.timestamp, now_ms());
    }

    /// Apply one `price_change` delta.
    ///
    /// The venue sends the NEW aggregate size at `price` for that side, so a
    /// level replace is the exact semantics (size 0 removes the level). The
    /// accompanying `best_bid` / `best_ask`, when present, are used only to
    /// prune levels better than the reported top — those are levels the venue
    /// has already consumed and we'd otherwise quote against a ghost.
    /// Missing top-of-book levels are never synthesized: an invented level of
    /// unknown size is worse for sizing than the depth we can actually vouch
    /// for, and the next `book` snapshot resyncs full depth anyway.
    pub fn apply_price_change(
        &mut self,
        is_bid: bool,
        price: Decimal,
        size: Option<Decimal>,
        best_bid: Option<Decimal>,
        best_ask: Option<Decimal>,
        event_ts: i64,
    ) {
        if let Some(size) = size {
            let levels = if is_bid { &mut self.bids } else { &mut self.asks };
            match levels.iter().position(|l| l.price == price) {
                Some(idx) if size.is_zero() => {
                    levels.remove(idx);
                }
                Some(idx) => levels[idx].size = size,
                None if size.is_zero() => {}
                None => {
                    levels.push(Level { price, size });
                    if is_bid {
                        levels.sort_by_key(|l| std::cmp::Reverse(l.price));
                    } else {
                        levels.sort_by_key(|l| l.price);
                    }
                }
            }
        }

        if let Some(bb) = best_bid {
            self.bids.retain(|l| l.price <= bb);
        }
        if let Some(ba) = best_ask {
            self.asks.retain(|l| l.price >= ba);
        }

        self.stamp(BookSource::Ws, event_ts, now_ms());
    }

    /// Best bid price and size.
    pub fn best_bid(&self) -> Option<&Level> {
        self.bids.first()
    }

    /// Best ask price and size.
    pub fn best_ask(&self) -> Option<&Level> {
        self.asks.first()
    }

    /// Best bid size (for Python DSL compatibility).
    /// Returns 0 if no bids exist.
    pub fn bid_size(&self) -> Decimal {
        self.best_bid().map(|l| l.size).unwrap_or(Decimal::ZERO)
    }

    /// Best ask size (for Python DSL compatibility).
    /// Returns 0 if no asks exist.
    pub fn ask_size(&self) -> Decimal {
        self.best_ask().map(|l| l.size).unwrap_or(Decimal::ZERO)
    }

    /// Mid price (average of best bid and ask).
    pub fn mid_price(&self) -> Option<Decimal> {
        match (self.best_bid(), self.best_ask()) {
            (Some(bid), Some(ask)) => Some((bid.price + ask.price) / Decimal::TWO),
            _ => None,
        }
    }

    /// Spread (best ask - best bid).
    pub fn spread(&self) -> Option<Decimal> {
        match (self.best_bid(), self.best_ask()) {
            (Some(bid), Some(ask)) => Some(ask.price - bid.price),
            _ => None,
        }
    }

    /// Spread as percentage of mid price.
    pub fn spread_bps(&self) -> Option<Decimal> {
        match (self.spread(), self.mid_price()) {
            (Some(spread), Some(mid)) if mid > Decimal::ZERO => {
                Some(spread / mid * Decimal::from(10000))
            }
            _ => None,
        }
    }

    /// Total bid depth (sum of all bid sizes).
    pub fn bid_depth(&self) -> Decimal {
        self.bids.iter().map(|l| l.size).sum()
    }

    /// Total ask depth (sum of all ask sizes).
    pub fn ask_depth(&self) -> Decimal {
        self.asks.iter().map(|l| l.size).sum()
    }

    /// Volume-weighted average price for buying `size` units.
    /// Returns None if insufficient liquidity.
    pub fn vwap_buy(&self, size: Decimal) -> Option<Decimal> {
        let mut remaining = size;
        let mut total_cost = Decimal::ZERO;

        for level in &self.asks {
            if remaining <= Decimal::ZERO {
                break;
            }
            let fill = remaining.min(level.size);
            total_cost += fill * level.price;
            remaining -= fill;
        }

        if remaining > Decimal::ZERO {
            None // Insufficient liquidity
        } else {
            Some(total_cost / size)
        }
    }


    /// Imbalance ratio: (bid_depth - ask_depth) / (bid_depth + ask_depth)
    /// Positive = more bids, negative = more asks.
    pub fn imbalance(&self) -> Option<Decimal> {
        let bid_depth = self.bid_depth();
        let ask_depth = self.ask_depth();
        let total = bid_depth + ask_depth;
        if total > Decimal::ZERO {
            Some((bid_depth - ask_depth) / total)
        } else {
            None
        }
    }
}

/// One public market trade, captured by the engine's trade-tape poller
/// and retained in the rolling per-token buffer on MarketDataHub.
///
/// Strategies read these to compute Δvolume / Δprice over windows the
/// orderbook alone can't reveal. The struct is owned-by-value (small
/// enough not to need Arc) so callers can clone freely.
#[derive(Debug, Clone)]
pub struct TradeRecord {
    pub token_id: String,
    pub price: Decimal,
    pub size: Decimal,
    pub side: String,
    pub timestamp: i64,
}

/// Market data event for broadcast.
#[derive(Debug, Clone)]
pub enum MarketEvent {
    /// Order book updated
    BookUpdate {
        token_id: String,
        book: Arc<OrderBook>,
    },
    /// Trade executed (from WebSocket trade feed)
    Trade {
        token_id: String,
        price: Decimal,
        size: Decimal,
        side: String,
        timestamp: i64,
    },
}

/// Market data hub - maintains order books and broadcasts updates.
pub struct MarketDataHub {
    /// Order books by token ID
    books: RwLock<HashMap<String, Arc<OrderBook>>>,
    /// Rolling trade history per token. Bounded by `max_trade_age` —
    /// inserts prune anything older than that. Strategies read this
    /// (via StrategyContext) to compute volume / price-change signals
    /// over windows the live book can't express.
    trades: RwLock<HashMap<String, VecDeque<TradeRecord>>>,
    /// Maximum age of trades retained in the rolling buffer. Defaults
    /// to 1 hour; override with PMENGINE_TRADE_HISTORY_SECS.
    max_trade_age: Duration,
    /// Broadcast sender for market events
    tx: Sender<MarketEvent>,
    /// Template receiver (clone this for new subscribers)
    rx: Receiver<MarketEvent>,
}

impl MarketDataHub {
    /// Create a new market data hub with specified channel capacity.
    pub fn new(capacity: usize) -> Self {
        let (mut tx, rx) = async_broadcast::broadcast(capacity);
        // Don't wait for receivers, drop old messages if buffer full
        tx.set_overflow(true);
        let max_trade_age = Duration::from_secs(
            std::env::var("PMENGINE_TRADE_HISTORY_SECS")
                .ok()
                .and_then(|s| s.parse().ok())
                .unwrap_or(3600),
        );
        Self {
            books: RwLock::new(HashMap::new()),
            trades: RwLock::new(HashMap::new()),
            max_trade_age,
            tx,
            rx,
        }
    }

    /// Snapshot of recent trades for a token. Returns an empty vec if the
    /// token is unknown or has no trades yet. The buffer is bounded by
    /// `max_trade_age`; callers can apply tighter time windows themselves.
    pub async fn recent_trades(&self, token_id: &str) -> Vec<TradeRecord> {
        let trades = self.trades.read().await;
        trades
            .get(token_id)
            .map(|q| q.iter().cloned().collect())
            .unwrap_or_default()
    }

    /// Snapshot of all per-token trade buffers. Used by the engine each
    /// tick to build StrategyContext.trade_history.
    pub async fn get_all_trade_history(&self) -> HashMap<String, Vec<TradeRecord>> {
        let trades = self.trades.read().await;
        trades
            .iter()
            .map(|(k, q)| (k.clone(), q.iter().cloned().collect()))
            .collect()
    }

    /// Subscribe to market events.
    pub fn subscribe(&self) -> Receiver<MarketEvent> {
        self.rx.clone()
    }

    /// Get current order book for a token.
    pub async fn get_book(&self, token_id: &str) -> Option<Arc<OrderBook>> {
        self.books.read().await.get(token_id).cloned()
    }

    /// Get all current order books.
    pub async fn get_all_books(&self) -> HashMap<String, Arc<OrderBook>> {
        self.books.read().await.clone()
    }

    /// Apply a WebSocket `book` snapshot. The WS is the authoritative feed:
    /// its updates always win, no staleness compare.
    ///
    /// Only tokens with an existing (subscription-created) book are touched.
    /// `get_all_books` is also the REST poller's rotation, so letting an
    /// unsolicited asset id create an entry would silently enlist it for
    /// polling. Returns whether anything was applied.
    pub async fn apply_ws_book(&self, update: &BookUpdate) -> bool {
        let token_id = update.asset_id.to_string();

        let book = {
            let mut books = self.books.write().await;
            let Some(slot) = books.get_mut(&token_id) else {
                return false;
            };
            let mut new_book = (**slot).clone();
            new_book.update_from_ws(update);
            let new_book = Arc::new(new_book);
            *slot = new_book.clone();
            new_book
        };

        let _ = self
            .tx
            .broadcast(MarketEvent::BookUpdate { token_id, book })
            .await;
        true
    }

    /// Apply a WebSocket `price_change` batch.
    ///
    /// One message can carry entries for several assets (both legs of a
    /// market, typically); each entry is routed to its own book and entries
    /// for tokens we don't track are dropped. Returns the number of books
    /// updated. This is the event that actually carries the tape — `book`
    /// snapshots are rare by comparison, so ignoring `price_change` is the
    /// difference between a live book and a REST-cadence one.
    pub async fn apply_ws_price_change(&self, pc: &PriceChange) -> usize {
        let mut updated: Vec<(String, Arc<OrderBook>)> = Vec::new();

        {
            let mut books = self.books.write().await;
            for entry in &pc.price_changes {
                let token_id = entry.asset_id.to_string();
                let Some(slot) = books.get_mut(&token_id) else {
                    continue;
                };
                let is_bid = match entry.side {
                    WsSide::Buy => true,
                    WsSide::Sell => false,
                    // A side we can't place is a side we can't book.
                    _ => continue,
                };
                let mut new_book = (**slot).clone();
                new_book.apply_price_change(
                    is_bid,
                    entry.price,
                    entry.size,
                    entry.best_bid,
                    entry.best_ask,
                    pc.timestamp,
                );
                let new_book = Arc::new(new_book);
                *slot = new_book.clone();
                updated.push((token_id, new_book));
            }
        }

        let n = updated.len();
        for (token_id, book) in updated {
            let _ = self
                .tx
                .broadcast(MarketEvent::BookUpdate { token_id, book })
                .await;
        }
        n
    }

    /// Initialize an empty book for a token (for subscriptions).
    pub async fn init_book(&self, token_id: &str) {
        let mut books = self.books.write().await;
        books
            .entry(token_id.to_string())
            .or_insert_with(|| Arc::new(OrderBook::new(token_id.to_string())));
    }

    /// Drop a token's book from the hub. Used when a strategy emits
    /// `Signal::Unsubscribe` to stop watching a market — the REST poller
    /// keys off `get_all_books`, so removing the entry also takes it out
    /// of the polling rotation. Also clears the rolling trade buffer.
    pub async fn remove_book(&self, token_id: &str) -> bool {
        let mut books = self.books.write().await;
        let existed = books.remove(token_id).is_some();
        drop(books);
        let mut trades = self.trades.write().await;
        trades.remove(token_id);
        existed
    }

    /// Record + broadcast a public market trade.
    ///
    /// Pushes onto the per-token rolling buffer (pruning anything older
    /// than `max_trade_age`) and fires a `MarketEvent::Trade` on the
    /// broadcast channel. Trades arrive from the engine's public-trades
    /// REST poller; the buffer is what strategies actually read when they
    /// need volume / price-change signals — broadcast is for live
    /// consumers like dashboards.
    pub async fn broadcast_trade(
        &self,
        token_id: String,
        price: Decimal,
        size: Decimal,
        side: String,
        timestamp: i64,
    ) {
        let record = TradeRecord {
            token_id: token_id.clone(),
            price,
            size,
            side: side.clone(),
            timestamp,
        };

        // Push + prune in one lock acquisition. The cutoff is wall-clock
        // (chrono::Utc::now()) compared against the trade's unix-seconds
        // timestamp because that's how data-api emits them.
        let cutoff = chrono::Utc::now().timestamp() - self.max_trade_age.as_secs() as i64;
        {
            let mut trades = self.trades.write().await;
            let q = trades.entry(token_id.clone()).or_default();
            q.push_back(record);
            while q.front().map(|t| t.timestamp < cutoff).unwrap_or(false) {
                q.pop_front();
            }
        }

        let _ = self
            .tx
            .broadcast(MarketEvent::Trade {
                token_id,
                price,
                size,
                side,
                timestamp,
            })
            .await;
    }

    /// Apply a REST `/book` snapshot, subject to the staleness compare.
    ///
    /// The REST poller is a health check and a fallback, not the truth: a
    /// snapshot is dropped when the state we already hold carries a NEWER
    /// venue timestamp. That is what keeps a 10s poll (or a cached CDN
    /// snapshot — the latency report caught 15 byte-identical 1s samples)
    /// from rolling a live WS book backwards.
    ///
    /// The one exception is `REST_OVERRIDE_STALE_MS`: a book nobody has fed
    /// in that long takes the snapshot regardless, so clock skew between the
    /// two feeds can never freeze it. Returns whether the snapshot was applied.
    pub async fn apply_rest_book(&self, book: OrderBook) -> bool {
        let token_id = book.token_id.clone();
        let now = now_ms();
        let mut book = book;
        let event_ts = book.timestamp;
        book.stamp(BookSource::Rest, event_ts, now);

        let arc = {
            let mut books = self.books.write().await;
            if let Some(existing) = books.get(&token_id) {
                if !rest_supersedes(existing, event_ts, now) {
                    return false;
                }
                // Carry the running count forward — it's per-token, not
                // per-snapshot, and the tape reads it as "how much have we
                // actually seen on this book".
                book.update_count = existing.update_count + 1;
            }
            let arc = Arc::new(book);
            books.insert(token_id.clone(), arc.clone());
            arc
        };

        let _ = self
            .tx
            .broadcast(MarketEvent::BookUpdate {
                token_id,
                book: arc,
            })
            .await;
        true
    }

    /// Get number of tracked order books.
    pub async fn book_count(&self) -> usize {
        self.books.read().await.len()
    }

    /// Book-age distribution across every tracked token — the health signal
    /// for "is the WS actually carrying the book". Ages come off local
    /// receipt time, so this measures our view, not the venue's clock.
    ///
    /// Read it as time-since-last-change, not error: a quiet market's book
    /// ages without being wrong, and on a thin new window the p50 sits in the
    /// hundreds of ms to seconds simply because nothing traded. The REST
    /// health poll bounds the age of even a silent book at its slow cadence.
    /// What a REST-bound engine looks like instead is a p50 pinned AT the
    /// poll interval across every token, busy ones included.
    pub async fn book_age_stats(&self) -> BookAgeStats {
        let now = now_ms();
        let books = self.books.read().await;
        let mut ages: Vec<i64> = Vec::with_capacity(books.len());
        let mut stats = BookAgeStats {
            books: books.len(),
            ..BookAgeStats::default()
        };
        for book in books.values() {
            match book.source {
                BookSource::Ws => stats.from_ws += 1,
                BookSource::Rest => stats.from_rest += 1,
                BookSource::Init => stats.never_fed += 1,
            }
            if let Some(age) = book.age_ms(now) {
                ages.push(age);
            }
        }
        drop(books);

        ages.sort_unstable();
        stats.fed = ages.len();
        stats.p50_ms = percentile(&ages, 0.50);
        stats.p90_ms = percentile(&ages, 0.90);
        stats.max_ms = ages.last().copied();
        stats
    }
}

/// The REST staleness compare, as a pure decision.
///
/// Separate from `apply_rest_book` so it can be tested against synthetic
/// clocks — the frozen-book escape hatch needs a receipt time in the past,
/// which no live code path can produce.
fn rest_supersedes(held: &OrderBook, incoming_ts: i64, now: i64) -> bool {
    if held.update_count == 0 {
        return true; // nothing to lose to
    }
    if incoming_ts >= held.timestamp {
        return true; // snapshot is at least as new as what we hold
    }
    // Older snapshot: only take it if what we hold has gone unfed long
    // enough that clock skew, not freshness, is the likely explanation.
    held.age_ms(now)
        .is_none_or(|age| age >= REST_OVERRIDE_STALE_MS)
}

/// Nearest-rank percentile over a sorted slice. None on empty.
fn percentile(sorted: &[i64], q: f64) -> Option<i64> {
    if sorted.is_empty() {
        return None;
    }
    let idx = ((sorted.len() as f64 - 1.0) * q).round() as usize;
    sorted.get(idx).copied()
}

/// Book freshness across all tracked tokens, as reported by `/status` and
/// the periodic health log.
#[derive(Debug, Clone, Copy, Default, PartialEq, Eq, serde::Serialize)]
pub struct BookAgeStats {
    /// Tokens tracked.
    pub books: usize,
    /// Tokens that have received at least one update.
    pub fed: usize,
    /// Tokens whose last write came from the WebSocket.
    pub from_ws: usize,
    /// Tokens whose last write came from the REST poller.
    pub from_rest: usize,
    /// Tokens subscribed but never fed by either.
    pub never_fed: usize,
    pub p50_ms: Option<i64>,
    pub p90_ms: Option<i64>,
    pub max_ms: Option<i64>,
}

impl Default for MarketDataHub {
    fn default() -> Self {
        Self::new(1000)
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use polymarket_client_sdk_v2::clob::ws::types::response::PriceChangeBatchEntry;
    use polymarket_client_sdk_v2::types::B256;
    use rust_decimal_macros::dec;
    use std::str::FromStr;

    const TOKEN: &str = "12345";
    const OTHER: &str = "67890";

    fn asset(id: &str) -> polymarket_client_sdk_v2::types::U256 {
        polymarket_client_sdk_v2::types::U256::from_str(id).unwrap()
    }

    fn ws_level(price: Decimal, size: Decimal) -> OrderBookLevel {
        OrderBookLevel::builder().price(price).size(size).build()
    }

    /// A WS `book` snapshot for `token` at venue time `ts`.
    fn ws_book(token: &str, ts: i64, bid: Decimal, ask: Decimal) -> BookUpdate {
        BookUpdate::builder()
            .asset_id(asset(token))
            .market(B256::ZERO)
            .timestamp(ts)
            .bids(vec![ws_level(bid, dec!(100))])
            .asks(vec![ws_level(ask, dec!(100))])
            .build()
    }

    /// A REST snapshot for `token` at venue time `ts`, as the client builds it.
    fn rest_book(token: &str, ts: i64, bid: Decimal, ask: Decimal) -> OrderBook {
        OrderBook {
            token_id: token.to_string(),
            bids: vec![Level { price: bid, size: dec!(100) }],
            asks: vec![Level { price: ask, size: dec!(100) }],
            timestamp: ts,
            hash: None,
            source: BookSource::Rest,
            received_at_ms: 0,
            update_count: 0,
        }
    }

    fn price_change(
        token: &str,
        ts: i64,
        side: WsSide,
        price: Decimal,
        size: Option<Decimal>,
        best_bid: Option<Decimal>,
        best_ask: Option<Decimal>,
    ) -> PriceChange {
        let entry = PriceChangeBatchEntry::builder()
            .asset_id(asset(token))
            .price(price)
            .side(side)
            .maybe_size(size)
            .maybe_best_bid(best_bid)
            .maybe_best_ask(best_ask)
            .build();
        PriceChange::builder()
            .market(B256::ZERO)
            .timestamp(ts)
            .price_changes(vec![entry])
            .build()
    }

    fn make_book() -> OrderBook {
        let mut book = OrderBook::new("test".to_string());
        book.bids = vec![
            Level { price: dec!(0.50), size: dec!(100) },
            Level { price: dec!(0.49), size: dec!(200) },
            Level { price: dec!(0.48), size: dec!(300) },
        ];
        book.asks = vec![
            Level { price: dec!(0.51), size: dec!(100) },
            Level { price: dec!(0.52), size: dec!(200) },
            Level { price: dec!(0.53), size: dec!(300) },
        ];
        book
    }

    #[test]
    fn test_best_bid_ask() {
        let book = make_book();
        assert_eq!(book.best_bid().unwrap().price, dec!(0.50));
        assert_eq!(book.best_ask().unwrap().price, dec!(0.51));
    }

    #[test]
    fn test_mid_price() {
        let book = make_book();
        assert_eq!(book.mid_price(), Some(dec!(0.505)));
    }

    #[test]
    fn test_spread() {
        let book = make_book();
        assert_eq!(book.spread(), Some(dec!(0.01)));
    }

    #[test]
    fn test_depth() {
        let book = make_book();
        assert_eq!(book.bid_depth(), dec!(600));
        assert_eq!(book.ask_depth(), dec!(600));
    }

    #[test]
    fn test_vwap_buy() {
        let book = make_book();
        // Buy 50 at 0.51 = 25.5
        assert_eq!(book.vwap_buy(dec!(50)), Some(dec!(0.51)));
        // Buy 150 = 100*0.51 + 50*0.52 = 51 + 26 = 77 / 150 = 0.5133...
        let vwap = book.vwap_buy(dec!(150)).unwrap();
        assert!(vwap > dec!(0.51) && vwap < dec!(0.52));
    }

    #[test]
    fn test_vwap_insufficient() {
        let book = make_book();
        // Try to buy 1000, only 600 available
        assert_eq!(book.vwap_buy(dec!(1000)), None);
    }

    #[test]
    fn test_imbalance() {
        let book = make_book();
        // Equal depth = 0 imbalance
        assert_eq!(book.imbalance(), Some(dec!(0)));

        // More bids = positive imbalance
        let mut book2 = book.clone();
        book2.bids.push(Level { price: dec!(0.47), size: dec!(400) });
        let imb = book2.imbalance().unwrap();
        assert!(imb > Decimal::ZERO);
    }

    // --- book metadata -------------------------------------------------

    #[test]
    fn fresh_book_has_no_age_and_no_source() {
        let book = OrderBook::new(TOKEN.into());
        assert_eq!(book.source, BookSource::Init);
        assert_eq!(book.update_count, 0);
        assert_eq!(book.age_ms(now_ms()), None, "never fed is not age zero");
    }

    #[test]
    fn ws_update_stamps_source_time_and_count() {
        let mut book = OrderBook::new(TOKEN.into());
        book.update_from_ws(&ws_book(TOKEN, 1_700, dec!(0.40), dec!(0.42)));
        assert_eq!(book.source, BookSource::Ws);
        assert_eq!(book.timestamp, 1_700, "venue clock is kept verbatim");
        assert_eq!(book.update_count, 1);
        // Age comes off the LOCAL clock, not the venue's 1970-era ts.
        assert!(book.age_ms(now_ms()).is_some_and(|a| a < 1_000));

        book.update_from_ws(&ws_book(TOKEN, 1_800, dec!(0.41), dec!(0.43)));
        assert_eq!(book.update_count, 2);
    }

    #[test]
    fn book_source_wire_names_are_stable() {
        // The book tape's `src` field is these strings; replay reads them.
        assert_eq!(BookSource::Ws.as_str(), "ws");
        assert_eq!(BookSource::Rest.as_str(), "rest");
        assert_eq!(BookSource::Init.as_str(), "init");
    }

    // --- price_change deltas -------------------------------------------

    #[test]
    fn price_change_replaces_a_level() {
        let mut book = make_book();
        book.apply_price_change(true, dec!(0.49), Some(dec!(500)), None, None, 10);
        assert_eq!(book.bids[1].size, dec!(500));
        assert_eq!(book.bids.len(), 3, "replace, not insert");
        assert_eq!(book.source, BookSource::Ws);
    }

    #[test]
    fn price_change_zero_size_removes_the_level() {
        let mut book = make_book();
        book.apply_price_change(false, dec!(0.51), Some(dec!(0)), None, None, 10);
        assert_eq!(book.best_ask().unwrap().price, dec!(0.52), "top ask consumed");
        assert_eq!(book.asks.len(), 2);
    }

    #[test]
    fn price_change_inserts_a_new_level_in_sort_order() {
        let mut book = make_book();
        book.apply_price_change(true, dec!(0.505), Some(dec!(50)), None, None, 10);
        assert_eq!(book.best_bid().unwrap().price, dec!(0.505), "new best bid sorts first");
        book.apply_price_change(false, dec!(0.505), Some(dec!(50)), None, None, 10);
        assert_eq!(book.best_ask().unwrap().price, dec!(0.505));
    }

    #[test]
    fn price_change_prunes_levels_better_than_the_reported_top() {
        let mut book = make_book();
        // Venue says best bid is now 0.49 / best ask 0.52: our 0.50 and 0.51
        // levels are ghosts we'd otherwise quote against.
        book.apply_price_change(
            true,
            dec!(0.49),
            Some(dec!(200)),
            Some(dec!(0.49)),
            Some(dec!(0.52)),
            10,
        );
        assert_eq!(book.best_bid().unwrap().price, dec!(0.49));
        assert_eq!(book.best_ask().unwrap().price, dec!(0.52));
    }

    #[test]
    fn price_change_never_synthesizes_a_missing_top_level() {
        let mut book = make_book();
        // best_bid below anything we hold: pruning leaves the book empty
        // rather than inventing a level of unknown size.
        book.apply_price_change(true, dec!(0.10), None, Some(dec!(0.10)), None, 10);
        assert!(book.bids.is_empty());
        assert_eq!(book.bid_size(), Decimal::ZERO);
    }

    // --- hub authority ---------------------------------------------------

    #[tokio::test]
    async fn ws_writes_only_reach_subscribed_tokens() {
        let hub = MarketDataHub::new(16);
        hub.init_book(TOKEN).await;

        assert!(hub.apply_ws_book(&ws_book(TOKEN, 100, dec!(0.4), dec!(0.5))).await);
        // An unsolicited asset id must not create a book: get_all_books is
        // the REST poller's rotation, so it would silently enlist for polling.
        assert!(!hub.apply_ws_book(&ws_book(OTHER, 100, dec!(0.4), dec!(0.5))).await);
        assert_eq!(hub.book_count().await, 1);

        let pc = price_change(OTHER, 200, WsSide::Buy, dec!(0.45), Some(dec!(10)), None, None);
        assert_eq!(hub.apply_ws_price_change(&pc).await, 0);
        assert_eq!(hub.book_count().await, 1);
    }

    #[tokio::test]
    async fn price_change_updates_the_live_book() {
        let hub = MarketDataHub::new(16);
        hub.init_book(TOKEN).await;
        hub.apply_ws_book(&ws_book(TOKEN, 100, dec!(0.40), dec!(0.42))).await;

        let pc = price_change(
            TOKEN,
            200,
            WsSide::Sell,
            dec!(0.42),
            Some(dec!(0)),
            None,
            Some(dec!(0.43)),
        );
        assert_eq!(hub.apply_ws_price_change(&pc).await, 1);

        let book = hub.get_book(TOKEN).await.unwrap();
        assert!(book.best_ask().is_none(), "0.42 consumed, 0.43 never quoted to us");
        assert_eq!(book.timestamp, 200);
        assert_eq!(book.update_count, 2);
    }

    #[tokio::test]
    async fn stale_rest_snapshot_never_rolls_back_a_live_ws_book() {
        let hub = MarketDataHub::new(16);
        hub.init_book(TOKEN).await;
        hub.apply_ws_book(&ws_book(TOKEN, 5_000, dec!(0.40), dec!(0.42))).await;

        // The exact failure mode the latency report suspected: a REST poll
        // returning a cached, older snapshot on top of a live book.
        assert!(!hub.apply_rest_book(rest_book(TOKEN, 4_000, dec!(0.30), dec!(0.60))).await);

        let book = hub.get_book(TOKEN).await.unwrap();
        assert_eq!(book.source, BookSource::Ws);
        assert_eq!(book.best_ask().unwrap().price, dec!(0.42));
    }

    #[tokio::test]
    async fn newer_rest_snapshot_resyncs_full_depth() {
        let hub = MarketDataHub::new(16);
        hub.init_book(TOKEN).await;
        hub.apply_ws_book(&ws_book(TOKEN, 5_000, dec!(0.40), dec!(0.42))).await;

        assert!(hub.apply_rest_book(rest_book(TOKEN, 6_000, dec!(0.30), dec!(0.60))).await);
        let book = hub.get_book(TOKEN).await.unwrap();
        assert_eq!(book.source, BookSource::Rest);
        assert_eq!(book.best_ask().unwrap().price, dec!(0.60));
        assert_eq!(book.update_count, 2, "count is per token, not per feed");
    }

    #[tokio::test]
    async fn first_rest_snapshot_fills_an_empty_book() {
        let hub = MarketDataHub::new(16);
        hub.init_book(TOKEN).await;
        assert!(hub.apply_rest_book(rest_book(TOKEN, 1, dec!(0.4), dec!(0.5))).await);
        assert_eq!(hub.get_book(TOKEN).await.unwrap().update_count, 1);
    }

    #[test]
    fn rest_supersedes_covers_the_clock_skew_escape_hatch() {
        let now = 1_000_000_i64;
        let mut held = OrderBook::new(TOKEN.into());

        // Never fed: anything beats nothing.
        assert!(rest_supersedes(&held, 1, now));

        // Fed a moment ago with a NEWER venue ts: the snapshot loses.
        held.stamp(BookSource::Ws, 9_000, now - 1_000);
        assert!(!rest_supersedes(&held, 8_999, now));
        // Same ts is allowed through — a full-depth resync costs nothing.
        assert!(rest_supersedes(&held, 9_000, now));

        // Same book, but nothing has fed it in REST_OVERRIDE_STALE_MS. A
        // WS clock running ahead must not be able to freeze it forever.
        held.stamp(BookSource::Ws, 9_000, now - REST_OVERRIDE_STALE_MS);
        assert!(rest_supersedes(&held, 8_999, now));
    }

    // --- age stats -------------------------------------------------------

    #[tokio::test]
    async fn book_age_stats_split_by_feed() {
        let hub = MarketDataHub::new(16);
        hub.init_book(TOKEN).await;
        hub.init_book(OTHER).await;
        hub.init_book("never").await;

        hub.apply_ws_book(&ws_book(TOKEN, 100, dec!(0.4), dec!(0.5))).await;
        hub.apply_rest_book(rest_book(OTHER, 100, dec!(0.4), dec!(0.5))).await;

        let stats = hub.book_age_stats().await;
        assert_eq!(stats.books, 3);
        assert_eq!(stats.fed, 2);
        assert_eq!(stats.from_ws, 1);
        assert_eq!(stats.from_rest, 1);
        assert_eq!(stats.never_fed, 1, "an unfed book must not read as age 0");
        assert!(stats.p50_ms.is_some_and(|ms| ms < 1_000));
        assert!(stats.max_ms >= stats.p50_ms);
    }

    #[test]
    fn percentile_is_nearest_rank() {
        assert_eq!(percentile(&[], 0.5), None);
        assert_eq!(percentile(&[7], 0.5), Some(7));
        assert_eq!(percentile(&[0, 10, 20, 30, 40], 0.5), Some(20));
        assert_eq!(percentile(&[0, 10, 20, 30, 40], 0.9), Some(40));
        assert_eq!(percentile(&[0, 10, 20, 30, 40], 0.0), Some(0));
    }
}
