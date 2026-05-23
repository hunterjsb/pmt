//! Local HTTP control plane for the running engine.
//!
//! Exposes read-only introspection (status, strategies, orders, alerts) and,
//! in later phases, action endpoints (approve/reject alerts, cancel orders,
//! pause strategies). All traffic is local: the server binds to a loopback
//! address by default. Remote access, if ever wanted, should route through
//! pmproxy with Cognito.
//!
//! ## Pattern
//!
//! The control plane is a thin axum HTTP server. Handlers do not touch
//! engine state directly — they send a typed `EngineCommand` over an
//! mpsc channel and wait on a oneshot reply. The engine's `tokio::select!`
//! receives the command and builds the reply inline, with full access to
//! its own state and no locking. This keeps the hot path lock-free while
//! still allowing external introspection.

use axum::{
    extract::{Path, Query, State},
    http::StatusCode,
    response::Json,
    routing::get,
    Router,
};
use serde::Deserialize;
use chrono::{DateTime, Utc};
use rust_decimal::Decimal;
use serde::Serialize;
use std::net::SocketAddr;
use tokio::sync::{mpsc, oneshot};
use tokio::task::JoinHandle;

/// Commands the control plane sends to the engine main loop.
///
/// Each variant carries a oneshot sender for the typed reply. Adding a new
/// endpoint = one new variant + one inline arm in the engine's command
/// handler + one handler function below.
#[derive(Debug)]
pub enum EngineCommand {
    GetStatus(oneshot::Sender<StatusReport>),
    ListStrategies(oneshot::Sender<Vec<StrategyInfo>>),
    ListOrders(oneshot::Sender<Vec<OrderInfo>>),
    ListAlerts(oneshot::Sender<Vec<crate::alerts::PendingAlert>>),
    ApproveAlert {
        id: String,
        reply: oneshot::Sender<Result<String, String>>,
    },
    RejectAlert {
        id: String,
        reply: oneshot::Sender<Result<(), String>>,
    },
    ListSubscriptions(oneshot::Sender<Vec<String>>),
    ListTrades {
        token_id: String,
        since_ts: Option<i64>,
        reply: oneshot::Sender<Vec<TradeInfo>>,
    },
    /// Programmatic Subscribe — used by the engine's market scanner to
    /// add tokens the matched filter produced. Idempotent on the engine
    /// side; sender ignores the empty reply.
    SubscribeToken {
        token_id: String,
        reply: oneshot::Sender<()>,
    },
    /// Programmatic Unsubscribe — scanner counterpart to SubscribeToken
    /// for tokens that have dropped out of the filter set.
    UnsubscribeToken {
        token_id: String,
        reply: oneshot::Sender<()>,
    },
    /// Register an order that was placed outside the engine (e.g. via
    /// `pmt buy/sell` on the CLI). The engine adds it to its
    /// external-orders map so /orders/all returns the unified view.
    /// Idempotent — re-registering the same id overwrites.
    RegisterExternalOrder {
        order: ExternalOrder,
        reply: oneshot::Sender<()>,
    },
    /// Mark an externally-registered order as cancelled (called by pmt
    /// CLI after a successful direct-CLOB cancel, when engine wasn't
    /// the one to do it). Removes from the engine's view.
    MarkExternalCancelled {
        order_id: String,
        reply: oneshot::Sender<()>,
    },
    /// List the union of engine-placed and externally-registered orders.
    /// Each row carries a `source` field so consumers can tell them apart.
    ListAllOrders(oneshot::Sender<Vec<UnifiedOrderInfo>>),
    /// Cancel any order on the account by ID — bypasses the engine's
    /// own order_manager and hits the CLOB cancel endpoint directly.
    /// Also clears the order from external_orders if present.
    CancelOrderById {
        order_id: String,
        reply: oneshot::Sender<Result<(), String>>,
    },
    /// Schedule a cancel for `order_id` to run at-or-after `at`. The
    /// engine drains the queue on each tick and calls `cancel_order` on
    /// every entry whose deadline has passed. Used by `pmt buy/sell --ttl`
    /// to set a client-side expiry on otherwise-GTC orders.
    ScheduleCancel {
        order_id: String,
        at: DateTime<Utc>,
        reply: oneshot::Sender<()>,
    },
    /// Place an order on behalf of an external caller (e.g. `pmt buy/sell`).
    /// Routing CLI writes through the engine puts every account-touching
    /// write on one queue, so the engine + CLI no longer compete for the
    /// account-wide ~5 req/sec budget. The engine looks up tick decimals
    /// (cached), rounds the price, places via its single PolymarketClient,
    /// and auto-registers the result in `external_orders` for `/orders/all`.
    PlaceOrder {
        token_id: String,
        side: String,    // "buy" or "sell"
        price: Decimal,
        size: Decimal,
        reply: oneshot::Sender<Result<String, String>>, // order_id or error
    },
}

/// An order placed outside the engine that the engine has been told to
/// track. Mirrors the fields the CLI registers; status is updated by
/// engine-side events (cancel, fill notifications when those land).
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct ExternalOrder {
    pub id: String,
    pub token_id: String,
    pub side: String,
    #[serde(with = "rust_decimal::serde::str")]
    pub price: Decimal,
    #[serde(with = "rust_decimal::serde::str")]
    pub size: Decimal,
    /// Where the order came from — "pmt-cli", "web", etc. Free-form.
    pub source: String,
    pub created_at: DateTime<Utc>,
}

/// Unified view of an order, regardless of whether the engine placed it
/// itself or the CLI registered it after placing.
#[derive(Debug, Serialize)]
pub struct UnifiedOrderInfo {
    pub id: String,
    pub token_id: String,
    pub side: String,
    #[serde(with = "rust_decimal::serde::str")]
    pub price: Decimal,
    #[serde(with = "rust_decimal::serde::str")]
    pub size: Decimal,
    #[serde(with = "rust_decimal::serde::str")]
    pub filled: Decimal,
    pub status: String,
    pub created_at: DateTime<Utc>,
    /// "engine" or whatever the external registrar supplied.
    pub source: String,
}

#[derive(Debug, Serialize)]
pub struct TradeInfo {
    pub token_id: String,
    #[serde(with = "rust_decimal::serde::str")]
    pub price: Decimal,
    #[serde(with = "rust_decimal::serde::str")]
    pub size: Decimal,
    pub side: String,
    pub timestamp: i64,
}

#[derive(Debug, Deserialize)]
pub struct TradesQuery {
    pub since: Option<i64>,
}

#[derive(Debug, Serialize)]
pub struct StatusReport {
    pub uptime_secs: u64,
    pub tick_count: u64,
    pub dry_run: bool,
    #[serde(with = "rust_decimal::serde::str")]
    pub balance_usdc: Decimal,
    pub subscribed_tokens: usize,
    pub strategies: usize,
    pub open_orders: usize,
    #[serde(with = "rust_decimal::serde::str")]
    pub total_exposure_usd: Decimal,
    #[serde(with = "rust_decimal::serde::str")]
    pub realized_pnl: Decimal,
    #[serde(with = "rust_decimal::serde::str")]
    pub unrealized_pnl: Decimal,
    pub halted: bool,
}

#[derive(Debug, Serialize)]
pub struct StrategyInfo {
    pub id: String,
    pub tick_interval_ms: u64,
    pub subscribed_tokens: Vec<String>,
    pub last_tick_at: Option<DateTime<Utc>>,
}

#[derive(Debug, Serialize)]
pub struct OrderInfo {
    pub id: String,
    pub token_id: String,
    pub side: &'static str,
    #[serde(with = "rust_decimal::serde::str")]
    pub price: Decimal,
    #[serde(with = "rust_decimal::serde::str")]
    pub size: Decimal,
    #[serde(with = "rust_decimal::serde::str")]
    pub filled: Decimal,
    pub status: String,
    pub created_at: DateTime<Utc>,
}

/// Spawn the control plane HTTP server on a background task.
///
/// Returns the join handle so the engine can abort it on shutdown. The
/// server runs forever until aborted; binding failures are logged and the
/// task exits.
pub fn spawn(bind: SocketAddr, cmd_tx: mpsc::Sender<EngineCommand>) -> JoinHandle<()> {
    tokio::spawn(async move {
        let app = Router::new()
            .route("/status", get(status_handler))
            .route("/strategies", get(strategies_handler))
            .route("/orders", get(orders_handler))
            .route("/alerts", get(alerts_handler))
            .route(
                "/alerts/:id/approve",
                axum::routing::post(approve_alert_handler),
            )
            .route(
                "/alerts/:id/reject",
                axum::routing::post(reject_alert_handler),
            )
            .route("/subscriptions", get(subscriptions_handler))
            .route("/trades/:token_id", get(trades_handler))
            .route("/orders/all", get(orders_all_handler))
            .route(
                "/orders/external",
                axum::routing::post(register_external_order_handler),
            )
            .route(
                "/orders/external/:id/cancelled",
                axum::routing::post(mark_external_cancelled_handler),
            )
            .route(
                "/orders/:id/cancel",
                axum::routing::post(cancel_order_by_id_handler),
            )
            .route(
                "/orders/:id/schedule-cancel",
                axum::routing::post(schedule_cancel_handler),
            )
            .route(
                "/trade/place",
                axum::routing::post(place_trade_handler),
            )
            .with_state(cmd_tx);

        let listener = match tokio::net::TcpListener::bind(bind).await {
            Ok(l) => l,
            Err(e) => {
                tracing::error!(bind = %bind, error = %e, "Control plane bind failed");
                return;
            }
        };

        tracing::info!(bind = %bind, "Control plane listening");

        if let Err(e) = axum::serve(listener, app).await {
            tracing::error!(error = %e, "Control plane serve loop ended");
        }
    })
}

async fn status_handler(
    State(cmd_tx): State<mpsc::Sender<EngineCommand>>,
) -> Result<Json<StatusReport>, StatusCode> {
    let (tx, rx) = oneshot::channel();
    cmd_tx
        .send(EngineCommand::GetStatus(tx))
        .await
        .map_err(|_| StatusCode::SERVICE_UNAVAILABLE)?;
    rx.await
        .map(Json)
        .map_err(|_| StatusCode::INTERNAL_SERVER_ERROR)
}

async fn strategies_handler(
    State(cmd_tx): State<mpsc::Sender<EngineCommand>>,
) -> Result<Json<Vec<StrategyInfo>>, StatusCode> {
    let (tx, rx) = oneshot::channel();
    cmd_tx
        .send(EngineCommand::ListStrategies(tx))
        .await
        .map_err(|_| StatusCode::SERVICE_UNAVAILABLE)?;
    rx.await
        .map(Json)
        .map_err(|_| StatusCode::INTERNAL_SERVER_ERROR)
}

async fn orders_handler(
    State(cmd_tx): State<mpsc::Sender<EngineCommand>>,
) -> Result<Json<Vec<OrderInfo>>, StatusCode> {
    let (tx, rx) = oneshot::channel();
    cmd_tx
        .send(EngineCommand::ListOrders(tx))
        .await
        .map_err(|_| StatusCode::SERVICE_UNAVAILABLE)?;
    rx.await
        .map(Json)
        .map_err(|_| StatusCode::INTERNAL_SERVER_ERROR)
}

async fn alerts_handler(
    State(cmd_tx): State<mpsc::Sender<EngineCommand>>,
) -> Result<Json<Vec<crate::alerts::PendingAlert>>, StatusCode> {
    let (tx, rx) = oneshot::channel();
    cmd_tx
        .send(EngineCommand::ListAlerts(tx))
        .await
        .map_err(|_| StatusCode::SERVICE_UNAVAILABLE)?;
    rx.await
        .map(Json)
        .map_err(|_| StatusCode::INTERNAL_SERVER_ERROR)
}

async fn approve_alert_handler(
    State(cmd_tx): State<mpsc::Sender<EngineCommand>>,
    Path(id): Path<String>,
) -> Result<Json<serde_json::Value>, (StatusCode, String)> {
    let (tx, rx) = oneshot::channel();
    cmd_tx
        .send(EngineCommand::ApproveAlert { id, reply: tx })
        .await
        .map_err(|_| (StatusCode::SERVICE_UNAVAILABLE, "engine offline".to_string()))?;
    match rx.await {
        Ok(Ok(order_id)) => Ok(Json(serde_json::json!({"approved": true, "order_id": order_id}))),
        Ok(Err(e)) => Err((StatusCode::BAD_REQUEST, e)),
        Err(_) => Err((StatusCode::INTERNAL_SERVER_ERROR, "engine dropped reply".to_string())),
    }
}

async fn reject_alert_handler(
    State(cmd_tx): State<mpsc::Sender<EngineCommand>>,
    Path(id): Path<String>,
) -> Result<Json<serde_json::Value>, (StatusCode, String)> {
    let (tx, rx) = oneshot::channel();
    cmd_tx
        .send(EngineCommand::RejectAlert { id, reply: tx })
        .await
        .map_err(|_| (StatusCode::SERVICE_UNAVAILABLE, "engine offline".to_string()))?;
    match rx.await {
        Ok(Ok(())) => Ok(Json(serde_json::json!({"rejected": true}))),
        Ok(Err(e)) => Err((StatusCode::NOT_FOUND, e)),
        Err(_) => Err((StatusCode::INTERNAL_SERVER_ERROR, "engine dropped reply".to_string())),
    }
}

async fn subscriptions_handler(
    State(cmd_tx): State<mpsc::Sender<EngineCommand>>,
) -> Result<Json<Vec<String>>, StatusCode> {
    let (tx, rx) = oneshot::channel();
    cmd_tx
        .send(EngineCommand::ListSubscriptions(tx))
        .await
        .map_err(|_| StatusCode::SERVICE_UNAVAILABLE)?;
    rx.await
        .map(Json)
        .map_err(|_| StatusCode::INTERNAL_SERVER_ERROR)
}

async fn trades_handler(
    State(cmd_tx): State<mpsc::Sender<EngineCommand>>,
    Path(token_id): Path<String>,
    Query(q): Query<TradesQuery>,
) -> Result<Json<Vec<TradeInfo>>, StatusCode> {
    let (tx, rx) = oneshot::channel();
    cmd_tx
        .send(EngineCommand::ListTrades {
            token_id,
            since_ts: q.since,
            reply: tx,
        })
        .await
        .map_err(|_| StatusCode::SERVICE_UNAVAILABLE)?;
    rx.await
        .map(Json)
        .map_err(|_| StatusCode::INTERNAL_SERVER_ERROR)
}

/// Body for `POST /orders/external`. Fields match `ExternalOrder` minus
/// `created_at`, which the engine fills in on receipt.
#[derive(Debug, Deserialize)]
pub struct RegisterExternalOrderBody {
    pub id: String,
    pub token_id: String,
    pub side: String,
    #[serde(with = "rust_decimal::serde::str")]
    pub price: Decimal,
    #[serde(with = "rust_decimal::serde::str")]
    pub size: Decimal,
    #[serde(default = "default_source")]
    pub source: String,
}

fn default_source() -> String {
    "external".to_string()
}

async fn orders_all_handler(
    State(cmd_tx): State<mpsc::Sender<EngineCommand>>,
) -> Result<Json<Vec<UnifiedOrderInfo>>, StatusCode> {
    let (tx, rx) = oneshot::channel();
    cmd_tx
        .send(EngineCommand::ListAllOrders(tx))
        .await
        .map_err(|_| StatusCode::SERVICE_UNAVAILABLE)?;
    rx.await
        .map(Json)
        .map_err(|_| StatusCode::INTERNAL_SERVER_ERROR)
}

async fn register_external_order_handler(
    State(cmd_tx): State<mpsc::Sender<EngineCommand>>,
    Json(body): Json<RegisterExternalOrderBody>,
) -> Result<Json<serde_json::Value>, (StatusCode, String)> {
    let order = ExternalOrder {
        id: body.id,
        token_id: body.token_id,
        side: body.side,
        price: body.price,
        size: body.size,
        source: body.source,
        created_at: Utc::now(),
    };
    let (tx, rx) = oneshot::channel();
    cmd_tx
        .send(EngineCommand::RegisterExternalOrder { order, reply: tx })
        .await
        .map_err(|_| (StatusCode::SERVICE_UNAVAILABLE, "engine offline".to_string()))?;
    rx.await
        .map(|_| Json(serde_json::json!({"registered": true})))
        .map_err(|_| (StatusCode::INTERNAL_SERVER_ERROR, "engine dropped reply".to_string()))
}

async fn mark_external_cancelled_handler(
    State(cmd_tx): State<mpsc::Sender<EngineCommand>>,
    Path(order_id): Path<String>,
) -> Result<Json<serde_json::Value>, (StatusCode, String)> {
    let (tx, rx) = oneshot::channel();
    cmd_tx
        .send(EngineCommand::MarkExternalCancelled { order_id, reply: tx })
        .await
        .map_err(|_| (StatusCode::SERVICE_UNAVAILABLE, "engine offline".to_string()))?;
    rx.await
        .map(|_| Json(serde_json::json!({"marked": true})))
        .map_err(|_| (StatusCode::INTERNAL_SERVER_ERROR, "engine dropped reply".to_string()))
}

async fn cancel_order_by_id_handler(
    State(cmd_tx): State<mpsc::Sender<EngineCommand>>,
    Path(order_id): Path<String>,
) -> Result<Json<serde_json::Value>, (StatusCode, String)> {
    let (tx, rx) = oneshot::channel();
    cmd_tx
        .send(EngineCommand::CancelOrderById { order_id, reply: tx })
        .await
        .map_err(|_| (StatusCode::SERVICE_UNAVAILABLE, "engine offline".to_string()))?;
    match rx.await {
        Ok(Ok(())) => Ok(Json(serde_json::json!({"cancelled": true}))),
        Ok(Err(e)) => Err((StatusCode::BAD_REQUEST, e)),
        Err(_) => Err((StatusCode::INTERNAL_SERVER_ERROR, "engine dropped reply".to_string())),
    }
}

/// Body for `POST /orders/:id/schedule-cancel`. Either `at` (absolute
/// RFC3339 deadline) or `after_seconds` (relative; engine computes
/// `Utc::now() + after_seconds`). `after_seconds` is preferred since it's
/// immune to clock skew between CLI and engine.
#[derive(Debug, Deserialize)]
pub struct ScheduleCancelBody {
    pub at: Option<DateTime<Utc>>,
    pub after_seconds: Option<u64>,
}

/// Body for `POST /trade/place`. Side is "buy" or "sell" (case-insensitive).
/// Price and size are strings (Decimal) to dodge float-precision surprises.
/// The engine rounds price to the market's tick decimals before submitting.
#[derive(Debug, Deserialize)]
pub struct PlaceTradeBody {
    pub token_id: String,
    pub side: String,
    #[serde(with = "rust_decimal::serde::str")]
    pub price: Decimal,
    #[serde(with = "rust_decimal::serde::str")]
    pub size: Decimal,
}

async fn place_trade_handler(
    State(cmd_tx): State<mpsc::Sender<EngineCommand>>,
    Json(body): Json<PlaceTradeBody>,
) -> Result<Json<serde_json::Value>, (StatusCode, String)> {
    let side = body.side.to_lowercase();
    if side != "buy" && side != "sell" {
        return Err((StatusCode::BAD_REQUEST, format!("invalid side '{}'", body.side)));
    }
    let (tx, rx) = oneshot::channel();
    cmd_tx
        .send(EngineCommand::PlaceOrder {
            token_id: body.token_id,
            side,
            price: body.price,
            size: body.size,
            reply: tx,
        })
        .await
        .map_err(|_| (StatusCode::SERVICE_UNAVAILABLE, "engine offline".to_string()))?;
    match rx.await {
        Ok(Ok(order_id)) => Ok(Json(serde_json::json!({"order_id": order_id, "success": true}))),
        Ok(Err(e)) => Err((StatusCode::BAD_REQUEST, e)),
        Err(_) => Err((StatusCode::INTERNAL_SERVER_ERROR, "engine dropped reply".to_string())),
    }
}

async fn schedule_cancel_handler(
    State(cmd_tx): State<mpsc::Sender<EngineCommand>>,
    Path(order_id): Path<String>,
    Json(body): Json<ScheduleCancelBody>,
) -> Result<Json<serde_json::Value>, (StatusCode, String)> {
    let at = match (body.at, body.after_seconds) {
        (Some(t), _) => t,
        (None, Some(secs)) => Utc::now() + chrono::Duration::seconds(secs as i64),
        (None, None) => {
            return Err((
                StatusCode::BAD_REQUEST,
                "must supply `at` (RFC3339) or `after_seconds`".to_string(),
            ))
        }
    };
    let (tx, rx) = oneshot::channel();
    cmd_tx
        .send(EngineCommand::ScheduleCancel { order_id, at, reply: tx })
        .await
        .map_err(|_| (StatusCode::SERVICE_UNAVAILABLE, "engine offline".to_string()))?;
    rx.await
        .map(|_| Json(serde_json::json!({"scheduled": true, "at": at})))
        .map_err(|_| (StatusCode::INTERNAL_SERVER_ERROR, "engine dropped reply".to_string()))
}
