//! Order book management with full depth tracking.
//!
//! Maintains local order book state from WebSocket updates and provides
//! broadcast channels for market data distribution.

use async_broadcast::{Receiver, Sender};
use polymarket_client_sdk_v2::clob::ws::types::response::{BookUpdate, OrderBookLevel};
use rust_decimal::Decimal;
use std::collections::{HashMap, VecDeque};
use std::sync::Arc;
use std::time::Duration;
use tokio::sync::RwLock;

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
    /// Timestamp of last update (Unix ms)
    pub timestamp: i64,
    /// Book hash for validation
    pub hash: Option<String>,
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
        }
    }

    /// Update from a WebSocket book update.
    ///
    /// Polymarket's WS sends bids/asks unsorted (or in arbitrary order). We
    /// sort here so `best_bid` / `best_ask` (which take `.first()`) return
    /// the actual top of book.
    pub fn update_from_ws(&mut self, update: &BookUpdate) {
        let mut bids: Vec<Level> = update.bids.iter().map(Level::from).collect();
        bids.sort_by(|a, b| b.price.cmp(&a.price)); // descending: best bid first
        let mut asks: Vec<Level> = update.asks.iter().map(Level::from).collect();
        asks.sort_by(|a, b| a.price.cmp(&b.price)); // ascending: best ask first
        self.bids = bids;
        self.asks = asks;
        self.timestamp = update.timestamp;
        self.hash = update.hash.clone();
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

    /// Bid depth up to a price (for liquidity analysis).
    pub fn bid_depth_to_price(&self, price: Decimal) -> Decimal {
        self.bids
            .iter()
            .filter(|l| l.price >= price)
            .map(|l| l.size)
            .sum()
    }

    /// Ask depth up to a price (for liquidity analysis).
    pub fn ask_depth_to_price(&self, price: Decimal) -> Decimal {
        self.asks
            .iter()
            .filter(|l| l.price <= price)
            .map(|l| l.size)
            .sum()
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

    /// Volume-weighted average price for selling `size` units.
    /// Returns None if insufficient liquidity.
    pub fn vwap_sell(&self, size: Decimal) -> Option<Decimal> {
        let mut remaining = size;
        let mut total_proceeds = Decimal::ZERO;

        for level in &self.bids {
            if remaining <= Decimal::ZERO {
                break;
            }
            let fill = remaining.min(level.size);
            total_proceeds += fill * level.price;
            remaining -= fill;
        }

        if remaining > Decimal::ZERO {
            None // Insufficient liquidity
        } else {
            Some(total_proceeds / size)
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

    /// Process a WebSocket book update.
    pub async fn process_book_update(&self, update: BookUpdate) {
        let token_id = update.asset_id.to_string();

        // Update or create order book
        let book = {
            let mut books = self.books.write().await;
            let book = books
                .entry(token_id.clone())
                .or_insert_with(|| Arc::new(OrderBook::new(token_id.clone())));

            // Create updated book
            let mut new_book = (**book).clone();
            new_book.update_from_ws(&update);
            let new_book = Arc::new(new_book);

            // Replace in map
            *book = new_book.clone();
            new_book
        };

        // Broadcast update
        let _ = self.tx.broadcast(MarketEvent::BookUpdate {
            token_id,
            book,
        }).await;
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

    /// Replace a token's book wholesale, e.g. from a REST poll snapshot.
    ///
    /// Unlike `process_book_update` (which applies an incremental WS diff),
    /// this overwrites bids/asks/timestamp/hash with the provided snapshot
    /// and broadcasts a `BookUpdate` event so subscribers see the change.
    pub async fn set_book(&self, book: OrderBook) {
        let token_id = book.token_id.clone();
        let arc = Arc::new(book);

        {
            let mut books = self.books.write().await;
            books.insert(token_id.clone(), arc.clone());
        }

        let _ = self
            .tx
            .broadcast(MarketEvent::BookUpdate {
                token_id,
                book: arc,
            })
            .await;
    }

    /// Get number of tracked order books.
    pub async fn book_count(&self) -> usize {
        self.books.read().await.len()
    }
}

impl Default for MarketDataHub {
    fn default() -> Self {
        Self::new(1000)
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use rust_decimal_macros::dec;

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
}
