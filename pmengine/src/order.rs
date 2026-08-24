//! Order management wrapping the Polymarket SDK.

use crate::client::{PolymarketClient, Side};
use crate::order_tape::OrderTimings;
use crate::position::Fill;
use crate::strategy::{Signal, Urgency};
use rust_decimal::{Decimal, RoundingStrategy};
use std::collections::HashMap;
use std::sync::Arc;
use tokio::sync::mpsc;

/// Order state.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum OrderStatus {
    Pending,
    Open,
    PartiallyFilled,
    Filled,
    Cancelled,
}

/// Tracked order.
#[derive(Debug, Clone)]
pub struct Order {
    pub id: String,
    pub token_id: String,
    pub is_buy: bool,
    pub price: Decimal,
    pub size: Decimal,
    pub filled_size: Decimal,
    pub status: OrderStatus,
    pub created_at: chrono::DateTime<chrono::Utc>,
}

impl Order {
    pub fn is_active(&self) -> bool {
        matches!(self.status, OrderStatus::Pending | OrderStatus::Open | OrderStatus::PartiallyFilled)
    }
}

/// A placed order and the stage stamps its path collected.
///
/// The stamps travel back to the caller rather than being written here so
/// the tape line lands outside every lock the engine holds — and so the
/// engine can attach the decision id that ties the record to the fire.
#[derive(Debug, Clone)]
pub struct Placement {
    pub order_id: String,
    /// Price and size as SENT — after the tick rounding, so a tape line
    /// reports what hit the wire rather than what the strategy asked for.
    pub price: Decimal,
    pub size: Decimal,
    /// Whether this went out as a maker (post-only) order. Travels back to
    /// the caller so the Phase 7 tape can label it: a resting quote and a
    /// crossing clip are different trades and must not read alike.
    pub post_only: bool,
    pub timings: OrderTimings,
}

/// What one signal actually becomes on the wire: `(post_only, tick-rounded
/// price)`. Split out of the async order path so both halves are testable
/// without a live CLOB.
///
/// `Urgency::Low` is the only urgency the wire can express — it means ADD
/// liquidity, never take it, and Polymarket REJECTS a post-only order that
/// would match rather than repricing it (docs/maker-design.md §2). Every
/// other urgency stays a plain crossing-allowed GTC limit, as it always was.
///
/// A post-only order rounds AWAY from the book, never toward it: half-up on
/// a coarse tick lifts a 0.985 bid to 0.99, past the clamp the strategy
/// priced it under and straight into a rejection. Same rounding class as
/// docs/LESSONS.md#L3, opposite direction — there the tick was ignored,
/// here it must not be allowed to make a quote more aggressive than priced.
fn wire_shape(urgency: Urgency, is_buy: bool, price: Decimal, dp: u32) -> (bool, Decimal) {
    let post_only = urgency == Urgency::Low;
    let price = match (post_only, is_buy) {
        (true, true) => price.round_dp_with_strategy(dp, RoundingStrategy::ToZero),
        (true, false) => price.round_dp_with_strategy(dp, RoundingStrategy::AwayFromZero),
        _ => price.round_dp(dp),
    };
    (post_only, price)
}

/// The tick the order path assumes when the tick lookup can't answer — the
/// `unwrap_or` in `place_order`, named so the delta matcher's cold-cache
/// fallback cannot drift from the one the wire actually uses.
///
/// 2 dp (0.01) is the COARSEST tick any measured crypto up/down book runs,
/// which is the safe direction to guess in: believing the wire is coarser
/// than it is can only floor a maker bid further from the book (cheaper,
/// never a post-only rejection) and hold a standing quote through a
/// sub-tick move. Guessing finer replaces orders that never moved.
pub(crate) const FALLBACK_TICK_DECIMALS: u32 = 2;

/// Where a model price actually RESTS once the order path has shaped it —
/// `wire_shape`'s price half, with the model's binary residue scrubbed off
/// first. Two model prices that land here are the same order on the wire.
///
/// The pre-round is the decimal form of `wire_px`'s 1e-9 epsilon in the
/// private maker fix (pmt-strategies d59727f). A strategy price arrives as
/// `Decimal::from_f64` of an f64, and a value a hair UNDER its own tick
/// (0.29 is 28.999999999999996 ticks in binary; six of the ninety-nine cent
/// ticks land short) would have a bare floor shave a whole tick off a price
/// already ON the grid. That reads as a wire move and replaces the exact
/// order the comparison exists to keep. Rounding nine digits below the tick
/// absorbs that residue and nothing a 0.001-grid model can express.
pub(crate) fn wire_price(urgency: Urgency, is_buy: bool, price: Decimal, dp: u32) -> Decimal {
    let scrubbed = price.round_dp(dp.saturating_add(9).min(28));
    wire_shape(urgency, is_buy, scrubbed, dp).1
}

/// Order manager wraps the SDK and tracks orders.
pub struct OrderManager {
    client: Arc<PolymarketClient>,
    orders: HashMap<String, Order>,
    fill_sender: mpsc::Sender<Fill>,
}

impl OrderManager {
    pub fn new(client: Arc<PolymarketClient>, fill_sender: mpsc::Sender<Fill>) -> Self {
        Self {
            client,
            orders: HashMap::new(),
            fill_sender,
        }
    }

    /// Execute a signal by placing/canceling orders.
    pub async fn execute(&mut self, signal: Signal) -> Result<Option<Placement>, OrderError> {
        match signal {
            Signal::Hold => Ok(None),

            Signal::Cancel { token_id } => {
                self.cancel_all(&token_id).await?;
                Ok(None)
            }

            Signal::Buy { token_id, price, size, urgency } => {
                self.place_order(&token_id, true, price, size, urgency).await
            }

            Signal::Sell { token_id, price, size, urgency } => {
                self.place_order(&token_id, false, price, size, urgency).await
            }

            // These are engine-level signals; the engine consumes them
            // before the order pipeline ever sees them. Listed here only
            // for exhaustiveness.
            Signal::Shutdown { .. }
            | Signal::StrategyComplete { .. }
            | Signal::Subscribe { .. }
            | Signal::Unsubscribe { .. }
            | Signal::Alert { .. }
            | Signal::CancelOrder { .. } => Ok(None),
        }
    }

    async fn place_order(
        &mut self,
        token_id: &str,
        is_buy: bool,
        price: Decimal,
        size: Decimal,
        urgency: Urgency,
    ) -> Result<Option<Placement>, OrderError> {
        // Anchor the Phase 7 record here: the tick resolution below is part
        // of "build", and on a cold token it is the expensive part.
        let mut timings = OrderTimings::start();

        // Round price to the market's actual tick size (Polymarket rejects
        // orders that don't sit on a tick). Default to 2 dp if the tick
        // lookup fails — better to attempt at coarser precision than to
        // drop the order entirely.
        let dp = self
            .client
            .tick_decimals_for(token_id)
            .await
            .unwrap_or(FALLBACK_TICK_DECIMALS);
        let (post_only, price) = wire_shape(urgency, is_buy, price, dp);
        let size = size.round_dp(2);

        // Skip if size rounds to zero
        if size.is_zero() {
            tracing::debug!(token_id = token_id, "Order size rounded to zero, skipping");
            return Ok(None);
        }

        let side = if is_buy { Side::Buy } else { Side::Sell };

        // Place order via SDK (handles dry-run internally)
        let order_id = self
            .client
            .place_limit_order_timed(token_id, side, price, size, post_only, &mut timings)
            .await
            .map_err(|e| OrderError::SdkError(e.to_string()))?;

        // Track order locally
        let order = Order {
            id: order_id.clone(),
            token_id: token_id.to_string(),
            is_buy,
            price,
            size,
            filled_size: Decimal::ZERO,
            status: OrderStatus::Open,
            created_at: chrono::Utc::now(),
        };

        self.orders.insert(order_id.clone(), order);
        Ok(Some(Placement {
            order_id,
            price,
            size,
            post_only,
            timings,
        }))
    }

    /// Cancel all orders for a token.
    pub async fn cancel_all(&mut self, token_id: &str) -> Result<usize, OrderError> {
        let to_cancel: Vec<String> = self
            .orders
            .iter()
            .filter(|(_, o)| o.token_id == token_id && o.is_active())
            .map(|(id, _)| id.clone())
            .collect();

        // Keep going past individual failures; never `?` out of this loop
        // (see docs/LESSONS.md#L6).
        let count = to_cancel.len();
        let mut failed = 0usize;
        let mut last_err: Option<OrderError> = None;
        for order_id in to_cancel {
            if let Err(e) = self.cancel_order(&order_id).await {
                failed += 1;
                tracing::warn!(order_id = %order_id, error = %e, "Cancel failed — continuing");
                last_err = Some(e);
            }
        }
        if let Some(e) = last_err {
            tracing::warn!(token_id, failed, count, "cancel_all left orders live");
            return Err(e);
        }
        tracing::info!(token_id = token_id, count = count, "Cancelled orders");
        Ok(count)
    }

    /// Cancel a specific order.
    ///
    /// A terminal refusal ("already canceled or matched", not found) is a
    /// SUCCESS, not an error: the order is off the book, which is the only
    /// thing a cancel wanted. Returning `Err` here left the order `Open` in
    /// the map forever, and an order that is permanently active but
    /// permanently dead is exactly what the delta matcher re-cancels every
    /// tick — this path is one of the storm's feeders, not just its victim.
    pub async fn cancel_order(&mut self, order_id: &str) -> Result<(), OrderError> {
        if let Some(order) = self.orders.get_mut(order_id) {
            if order.is_active() {
                // Cancel via SDK (handles dry-run internally)
                match self.client.cancel_order(order_id).await {
                    Ok(()) => {}
                    Err(e)
                        if crate::client::classify_cancel_refusal(&e.to_string())
                            == crate::client::CancelRefusal::Terminal =>
                    {
                        tracing::debug!(
                            order_id = order_id,
                            reason = %e,
                            "Cancel: order was already off the book"
                        );
                    }
                    Err(e) => return Err(OrderError::SdkError(e.to_string())),
                }
                order.status = OrderStatus::Cancelled;
            }
        }
        Ok(())
    }

    /// Mark a tracked order cancelled without issuing the network call.
    ///
    /// For callers that drive the CLOB themselves — the delta matcher runs
    /// its batch of cancels concurrently through the shared client, then
    /// applies local state here for each one that actually succeeded.
    /// Never call this for a cancel that failed: a locally-cancelled order
    /// still live on the book is the ghost `cancel_all` was fixed to avoid
    /// (docs/LESSONS.md#L6).
    pub fn mark_cancelled(&mut self, order_id: &str) {
        if let Some(order) = self.orders.get_mut(order_id) {
            order.status = OrderStatus::Cancelled;
        }
    }

    /// Cancel all active orders (for shutdown).
    pub async fn cancel_all_orders(&mut self) -> Result<usize, OrderError> {
        let active: Vec<String> = self
            .orders
            .iter()
            .filter(|(_, o)| o.is_active())
            .map(|(id, _)| id.clone())
            .collect();

        let requested = active.len();
        let mut count = 0usize;
        if requested > 0 {
            // Batch cancel via SDK — one request for the whole book.
            let order_refs: Vec<&str> = active.iter().map(|s| s.as_str()).collect();
            let report = self
                .client
                .cancel_orders(&order_refs)
                .await
                .map_err(|e| OrderError::SdkError(e.to_string()))?;

            // What the CLOB confirmed is off the book gets marked cancelled
            // locally — and so does anything it refused TERMINALLY, because
            // "already canceled or matched" is the exchange confirming the
            // same fact in different words. Only an unanswered refusal is
            // still live and must keep saying so. Those are logged loud
            // rather than returned — an `Err` here would abort the rest of
            // shutdown (strategy cleanup, final P&L) over orders the
            // operator now needs the log to go find.
            count = report.cancelled.len();
            for order_id in &report.cancelled {
                self.mark_cancelled(order_id);
            }
            let mut still_live = std::collections::HashMap::new();
            for (order_id, why) in &report.failed {
                match crate::client::classify_cancel_refusal(why) {
                    crate::client::CancelRefusal::Terminal => self.mark_cancelled(order_id),
                    crate::client::CancelRefusal::Transient => {
                        still_live.insert(order_id.clone(), why.clone());
                    }
                }
            }
            if !still_live.is_empty() {
                tracing::error!(
                    requested,
                    cancelled = count,
                    still_live = ?still_live,
                    "Shutdown cancel left orders ON THE BOOK"
                );
            }
        }

        tracing::info!(requested, count, "Cancelled all orders on shutdown");
        Ok(count)
    }

    /// Process a fill event from the user-trades stream.
    ///
    /// `fee` is the realized trading fee in USDC (not basis points). Compute
    /// it from the Polymarket TradeMessage as
    /// `price * size * fee_rate_bps / 10_000` before calling — the engine
    /// doesn't recompute it here, so callers must pass the actual fee.
    pub async fn process_fill(
        &mut self,
        order_id: &str,
        price: Decimal,
        size: Decimal,
        fee: Decimal,
    ) -> Result<(), OrderError> {
        if let Some(order) = self.orders.get_mut(order_id) {
            order.filled_size += size;
            if order.filled_size >= order.size {
                order.status = OrderStatus::Filled;
            } else {
                order.status = OrderStatus::PartiallyFilled;
            }

            let fill = Fill {
                order_id: order_id.to_string(),
                token_id: order.token_id.clone(),
                is_buy: order.is_buy,
                price,
                size,
                timestamp: chrono::Utc::now(),
                fee,
            };

            tracing::info!(
                order_id = order_id,
                token_id = fill.token_id,
                side = if fill.is_buy { "BUY" } else { "SELL" },
                price = %fill.price,
                size = %fill.size,
                "Order filled"
            );

            self.fill_sender.send(fill).await.map_err(|_| OrderError::ChannelClosed)?;
        }
        Ok(())
    }

    /// Get an order by ID.
    pub fn get_order(&self, order_id: &str) -> Option<&Order> {
        self.orders.get(order_id)
    }

    /// Owned snapshot of currently-active orders, for introspection by the
    /// control plane. Cloned so the caller can serialize without holding a
    /// reference into the manager.
    pub fn active_orders_snapshot(&self) -> Vec<Order> {
        self.orders
            .values()
            .filter(|o| o.is_active())
            .cloned()
            .collect()
    }

    /// Get active orders for a token.
    pub fn active_orders_for_token(&self, token_id: &str) -> Vec<&Order> {
        self.orders
            .values()
            .filter(|o| o.token_id == token_id && o.is_active())
            .collect()
    }
}

#[derive(Debug)]
pub enum OrderError {
    SdkError(String),
    ChannelClosed,
}

impl std::fmt::Display for OrderError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            OrderError::SdkError(e) => write!(f, "SDK error: {}", e),
            OrderError::ChannelClosed => write!(f, "Fill channel closed"),
        }
    }
}

impl std::error::Error for OrderError {}

#[cfg(test)]
mod tests {
    use super::*;
    use rust_decimal::prelude::FromPrimitive;
    use rust_decimal_macros::dec;

    #[test]
    fn only_low_urgency_goes_out_post_only() {
        // Before maker step 0 the urgency was bound as `_urgency` and every
        // order pmengine ever placed was a plain crossing GTC — updown's
        // taker clips must keep behaving exactly that way.
        for u in [Urgency::Medium, Urgency::High, Urgency::Immediate] {
            let (post_only, _) = wire_shape(u, true, dec!(0.94), 3);
            assert!(!post_only, "{u:?} must still cross");
        }
        let (post_only, _) = wire_shape(Urgency::Low, true, dec!(0.94), 3);
        assert!(post_only, "Low is the strategy asking to add liquidity");
    }

    /// A post-only SELL is the mirror of the post-only buy on the wire, and
    /// the flag is what the adapter dispatches on — never re-derived from
    /// price or side downstream. This is the whole public half of a resting
    /// exit ask: `Urgency::Low` on a `Signal::Sell` becomes GTC + `postOnly`
    /// at a price rounded UP, away from the bid it must not cross.
    #[test]
    fn a_low_urgency_sell_goes_out_post_only_and_never_rounds_into_the_bid() {
        let (post_only, price) = wire_shape(Urgency::Low, false, dec!(0.612), 2);
        assert!(post_only, "Low on a SELL is the strategy asking to add liquidity");
        assert_eq!(price, dec!(0.62), "an ask ceils onto the tick, away from the book");

        // The hazard the direction exists for: nearest-rounding 0.612 to 0.61
        // would put the ask AT a 0.61 bid, and Polymarket rejects a post-only
        // order that would match rather than repricing it.
        assert!(price > dec!(0.61));

        // Every other urgency is still a plain crossing sell — L1's
        // math-forced evacuation has to be able to hit the bid.
        for u in [Urgency::Medium, Urgency::High, Urgency::Immediate] {
            let (post_only, price) = wire_shape(u, false, dec!(0.612), 2);
            assert!(!post_only, "{u:?} must still cross");
            assert_eq!(price, dec!(0.61), "and round to nearest, as it always did");
        }
    }

    #[test]
    fn a_post_only_quote_never_rounds_toward_the_book() {
        // The hazard: 0.987 is a legal price on a 0.001-tick market and
        // nearest-rounding on a 0.01-tick one lifts it to 0.99 — above the
        // max_price the strategy clamped it under, and into a postOnly
        // rejection that costs a placement token and returns nothing.
        let (_, taker) = wire_shape(Urgency::High, true, dec!(0.987), 2);
        assert_eq!(taker, dec!(0.99), "the taker path is unchanged");

        let (_, bid) = wire_shape(Urgency::Low, true, dec!(0.987), 2);
        assert_eq!(bid, dec!(0.98), "a maker bid rounds DOWN onto the tick");

        let (_, ask) = wire_shape(Urgency::Low, false, dec!(0.982), 2);
        assert_eq!(ask, dec!(0.99), "and a maker ask rounds UP — same rule, mirrored");

        // On a tick the price already sits on, nothing moves.
        let (_, exact) = wire_shape(Urgency::Low, true, dec!(0.985), 3);
        assert_eq!(exact, dec!(0.985));
    }

    #[test]
    fn wire_price_projects_a_model_price_onto_the_tick_it_will_rest_on() {
        // A whole 0.001 neighbourhood of model prices is ONE order at 0.98
        // on a cent book — which is the fact the delta matcher needs and the
        // reason it cannot compare a model price to a wire price.
        for px in [dec!(0.980), dec!(0.984), dec!(0.985), dec!(0.989)] {
            assert_eq!(wire_price(Urgency::Low, true, px, 2), dec!(0.98), "{px}");
        }
        // A real move off the tick still lands somewhere else.
        assert_eq!(wire_price(Urgency::Low, true, dec!(0.975), 2), dec!(0.97));
        assert_eq!(wire_price(Urgency::Low, true, dec!(0.991), 2), dec!(0.99));

        // Mirrored for a maker ask, and untouched for a crossing taker.
        assert_eq!(wire_price(Urgency::Low, false, dec!(0.982), 2), dec!(0.99));
        assert_eq!(wire_price(Urgency::High, true, dec!(0.987), 2), dec!(0.99));

        // On a 0.001 market the model grid IS the wire grid: no-op.
        for px in [dec!(0.984), dec!(0.985), dec!(0.987)] {
            assert_eq!(wire_price(Urgency::Low, true, px, 3), px, "{px}");
        }
    }

    #[test]
    fn wire_price_scrubs_the_binary_shortfall_off_a_price_on_the_grid() {
        // The 0.29/0.01 class (pmt-strategies d59727f): six of the ninety-
        // nine cent ticks are a hair short of themselves in binary, and a
        // bare floor reads them a whole tick low — a wire "move" that never
        // happened, which replaces the order the matcher exists to keep.
        for p in [0.29f64, 0.47, 0.57, 0.58, 0.59, 0.94] {
            let residue = Decimal::from_f64_retain(p).unwrap();
            let expected = Decimal::from_f64(p).unwrap();
            assert_eq!(
                wire_price(Urgency::Low, true, residue, 2),
                expected,
                "{p} sits on the cent grid",
            );
            // The maker ask ceils, so its hazard is the mirror image: a
            // price a hair OVER its tick must not be lifted a whole one.
            assert_eq!(
                wire_price(Urgency::Low, false, residue, 2),
                expected,
                "{p} sits on the cent grid (ask side)",
            );
        }
    }

    #[test]
    fn the_matchers_cold_tick_is_the_one_the_order_path_falls_back_to() {
        // `place_order` shapes on this when `tick_decimals_for` fails, so a
        // matcher guessing anything else would compare against a tick the
        // wire never used.
        assert_eq!(FALLBACK_TICK_DECIMALS, 2);
    }
}
