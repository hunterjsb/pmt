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
