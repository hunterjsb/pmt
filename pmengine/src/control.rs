//! Local HTTP control plane for the running engine.
//!
//! Exposes read-only introspection (status, strategies, orders, alerts) and
//! action endpoints (approve/reject alerts, cancel orders, pause/resume/stop
//! strategies, per-strategy commands). All traffic is local: the server binds to a loopback
//! address by default. Remote access, if ever wanted, should route through
//! pmproxy behind its IAM-authed Function URL.
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
    /// Pause a strategy: stop ticking it and pull its resting orders, but
    /// keep it registered so it can be resumed. Reply is Ok(()) if the
    /// strategy exists, Err with a message otherwise.
    PauseStrategy {
        id: String,
        reply: oneshot::Sender<Result<(), String>>,
    },
    /// Resume a paused strategy — it starts quoting again on its next tick.
    ResumeStrategy {
        id: String,
        reply: oneshot::Sender<Result<(), String>>,
    },
    /// Stop and remove a strategy entirely (runs on_shutdown, pulls its
    /// orders). Cannot be resumed without a restart.
    StopStrategy {
        id: String,
        reply: oneshot::Sender<Result<(), String>>,
    },
    /// Route a JSON command to a strategy's `on_command` — the channel for
    /// feeding parameters into a running strategy (e.g. arming the updown
    /// trigger on a market the operator just priced).
    StrategyCommand {
        id: String,
        body: serde_json::Value,
        reply: oneshot::Sender<Result<serde_json::Value, String>>,
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

// ---------- decision-tape tail (`GET /tape`) ----------
//
// The one endpoint here that does NOT go through `EngineCommand`. The tape is
// a file the engine appends to and nothing else reads, so routing it through
// the command channel would put a file read inside the trading `select!` arm —
// the exact shape that darkened the plane for 9.6s in analysis/watch_load.md.
// This handler owns its own bounded read on a blocking thread instead.

/// Bytes read off the END of the tape per request. The tape is append-only
/// and already 18MB+ and growing, so a full scan is unbounded work that gets
/// worse every day. 512KB is ~1500 records — many minutes of fleet activity,
/// far more recency than a remote dashboard needs — and it stays 512KB
/// whatever the file becomes.
const TAPE_TAIL_BYTES: u64 = 512 * 1024;
/// Records per response when the caller doesn't say, and the hard cap it
/// can't argue past. Both bound the payload the SSM tunnel has to carry.
const TAPE_LIMIT_DEFAULT: usize = 200;
const TAPE_LIMIT_MAX: usize = 500;
/// The durable eval/fire tape, under `crate::jsonl::engine_dir()`.
const TAPE_FILE: &str = "updown-tape.jsonl";

#[derive(Debug, Deserialize)]
pub struct TapeQuery {
    /// Cursor: return records whose `t` is strictly greater. Absent = 0, i.e.
    /// "whatever the window holds", which is what a cold client wants.
    pub since: Option<f64>,
    pub limit: Option<usize>,
}

/// One `GET /tape` answer: records oldest-first, plus the honest admission of
/// what it left out.
#[derive(Debug, Default, Serialize)]
pub struct TapeSlice {
    pub records: Vec<serde_json::Value>,
    /// Records past `since` that this response does NOT carry — either the
    /// byte window didn't reach back to the cursor, or there were more than
    /// `limit` of them and the newest won. The client advances its cursor
    /// anyway and accepts the gap: a remote tape is a recency feed, not
    /// history.
    pub truncated: bool,
    /// Newest `t` in `records` — the cursor to send next. `null` when the
    /// response is empty, in which case the client keeps the one it had.
    pub cursor: Option<f64>,
}

/// Read the tail of a JSONL tape and return the records newer than `since`,
/// oldest-first.
///
/// Bounded twice over, and both bounds are load-bearing: at most
/// `TAPE_TAIL_BYTES` are READ regardless of file size, and at most `limit + 1`
/// records are PARSED because the scan runs backwards from the newest line and
/// stops at the cursor. Cost is therefore a function of how much is new, not
/// of how big the tape has grown.
///
/// Lines that don't parse — a torn mid-write append, a record with no `t` —
/// are skipped, never fatal: one bad line must not cost the operator the whole
/// panel.
fn tape_tail(path: &std::path::Path, since: f64, limit: usize) -> std::io::Result<TapeSlice> {
    use std::io::{Read, Seek, SeekFrom};

    let mut f = std::fs::File::open(path)?;
    let len = f.metadata()?.len();
    let start = len.saturating_sub(TAPE_TAIL_BYTES);
    f.seek(SeekFrom::Start(start))?;
    let mut buf = Vec::with_capacity(TAPE_TAIL_BYTES.min(len) as usize);
    // take(), not a bare read_to_end: the engine is appending concurrently, so
    // the file can be longer now than metadata() just said.
    (&mut f).take(TAPE_TAIL_BYTES).read_to_end(&mut buf)?;

    // A window that starts mid-file starts mid-record. Drop the partial head
    // rather than hand back half a line.
    let body: &[u8] = if start > 0 {
        match buf.iter().position(|&b| b == b'\n') {
            Some(i) => &buf[i + 1..],
            None => &[],
        }
    } else {
        &buf
    };

    // NEWEST FIRST, and stop the moment the answer is complete. The window is
    // the read bound; this is the PARSE bound, and it's the one that matters —
    // the steady state is a 2s poll with a handful of new records, and parsing
    // the whole 512KB to find them costs ~50x what parsing them does. One
    // writer appends this tape, so `t` ascends and the first record at-or-
    // before the cursor genuinely ends the search.
    let mut matched: Vec<serde_json::Value> = Vec::new();
    let mut reached_cursor = false;
    let mut over_limit = false;
    for line in body.rsplit(|&b| b == b'\n') {
        if line.is_empty() {
            continue;
        }
        let Ok(v) = serde_json::from_slice::<serde_json::Value>(line) else {
            continue;
        };
        let Some(t) = v.get("t").and_then(serde_json::Value::as_f64) else {
            continue;
        };
        if t <= since {
            reached_cursor = true;
            break;
        }
        // Checked after the cursor test, so a batch that lands exactly on the
        // cap isn't reported as truncated when nothing was actually dropped.
        if matched.len() == limit {
            over_limit = true;
            break;
        }
        matched.push(v);
    }
    let cursor = matched.first().and_then(|v| v["t"].as_f64());
    matched.reverse();

    // Truncated two ways, both meaning "records past your cursor that this
    // answer doesn't carry": we filled the cap, or we ran out of window before
    // reaching the cursor. A window that started at byte 0 IS the whole file,
    // so it can't be hiding anything.
    let truncated = over_limit || (!reached_cursor && start > 0);
    Ok(TapeSlice { records: matched, truncated, cursor })
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
    /// Market WebSocket state. `ws_connected` false with tokens subscribed
    /// means every book below is REST-cadence bound.
    pub ws_connected: bool,
    /// Socket has been down past the degrade threshold (see `wsfeed`).
    pub ws_degraded: bool,
    /// Tokens currently streaming on the socket.
    pub ws_tokens: usize,
    /// Market events applied since process start.
    pub ws_events: u64,
    pub ws_last_event_age_ms: Option<i64>,
    pub ws_down_for_ms: Option<i64>,
    /// Book freshness across tracked tokens, measured off local receipt time.
    /// This is the number the WS work exists to move.
    pub book_age_p50_ms: Option<i64>,
    pub book_age_p90_ms: Option<i64>,
    pub book_age_max_ms: Option<i64>,
    /// How many books were last written by each feed — the honest read on
    /// which source is actually carrying the engine.
    pub books_from_ws: usize,
    pub books_from_rest: usize,
}

#[derive(Debug, Serialize)]
pub struct StrategyInfo {
    pub id: String,
    pub tick_interval_ms: u64,
    pub subscribed_tokens: Vec<String>,
    pub last_tick_at: Option<DateTime<Utc>>,
    pub paused: bool,
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

/// Bind the control-plane port and spawn the HTTP server on a background task.
///
/// The bind happens synchronously (before spawning) so a bind failure is
/// returned to the caller rather than swallowed inside the task. This lets
/// the engine fail fast: if the port is already held by another instance,
/// the engine refuses to start instead of trading headless without a
/// reachable control plane (which previously let two engines quote the
/// same token at once).
///
/// Returns the join handle so the engine can abort it on shutdown.
pub async fn spawn(
    bind: SocketAddr,
    cmd_tx: mpsc::Sender<EngineCommand>,
) -> Result<JoinHandle<()>, std::io::Error> {
    let listener = tokio::net::TcpListener::bind(bind).await.inspect_err(|e| {
        tracing::error!(bind = %bind, error = %e, "Control plane bind failed");
    })?;
    tracing::info!(bind = %bind, "Control plane listening");

    let handle = tokio::spawn(async move {
        let app = build_router(cmd_tx);

        if let Err(e) = axum::serve(listener, app).await {
            tracing::error!(error = %e, "Control plane serve loop ended");
        }
    });

    Ok(handle)
}

/// Route table, extracted so a unit test constructs it. axum panics on
/// route-syntax errors at RUNTIME (the 0.7->0.8 `/:id` -> `/{id}` change
/// took the control plane down while cargo test stayed green — the engine
/// ticked on, headless). Construction under test makes that impossible.
fn build_router(cmd_tx: mpsc::Sender<EngineCommand>) -> Router {
    Router::new()
            .route("/status", get(status_handler))
            .route("/strategies", get(strategies_handler))
            .route(
                "/strategies/{id}/pause",
                axum::routing::post(pause_strategy_handler),
            )
            .route(
                "/strategies/{id}/resume",
                axum::routing::post(resume_strategy_handler),
            )
            .route(
                "/strategies/{id}/stop",
                axum::routing::post(stop_strategy_handler),
            )
            .route(
                "/strategies/{id}/command",
                axum::routing::post(strategy_command_handler),
            )
            .route("/orders", get(orders_handler))
            .route("/alerts", get(alerts_handler))
            .route(
                "/alerts/{id}/approve",
                axum::routing::post(approve_alert_handler),
            )
            .route(
                "/alerts/{id}/reject",
                axum::routing::post(reject_alert_handler),
            )
            .route("/subscriptions", get(subscriptions_handler))
            .route("/trades/{token_id}", get(trades_handler))
            .route("/tape", get(tape_handler))
            .route("/orders/all", get(orders_all_handler))
            .route(
                "/orders/external",
                axum::routing::post(register_external_order_handler),
            )
            .route(
                "/orders/external/{id}/cancelled",
                axum::routing::post(mark_external_cancelled_handler),
            )
            .route(
                "/orders/{id}/cancel",
                axum::routing::post(cancel_order_by_id_handler),
            )
            .route(
                "/orders/{id}/schedule-cancel",
                axum::routing::post(schedule_cancel_handler),
            )
            .route(
                "/trade/place",
                axum::routing::post(place_trade_handler),
            )
            .with_state(cmd_tx)
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

async fn pause_strategy_handler(
    State(cmd_tx): State<mpsc::Sender<EngineCommand>>,
    Path(id): Path<String>,
) -> Result<Json<serde_json::Value>, (StatusCode, String)> {
    let (tx, rx) = oneshot::channel();
    cmd_tx
        .send(EngineCommand::PauseStrategy { id, reply: tx })
        .await
        .map_err(|_| (StatusCode::SERVICE_UNAVAILABLE, "engine offline".to_string()))?;
    match rx.await {
        Ok(Ok(())) => Ok(Json(serde_json::json!({"paused": true}))),
        Ok(Err(e)) => Err((StatusCode::NOT_FOUND, e)),
        Err(_) => Err((StatusCode::INTERNAL_SERVER_ERROR, "engine dropped reply".to_string())),
    }
}

async fn resume_strategy_handler(
    State(cmd_tx): State<mpsc::Sender<EngineCommand>>,
    Path(id): Path<String>,
) -> Result<Json<serde_json::Value>, (StatusCode, String)> {
    let (tx, rx) = oneshot::channel();
    cmd_tx
        .send(EngineCommand::ResumeStrategy { id, reply: tx })
        .await
        .map_err(|_| (StatusCode::SERVICE_UNAVAILABLE, "engine offline".to_string()))?;
    match rx.await {
        Ok(Ok(())) => Ok(Json(serde_json::json!({"resumed": true}))),
        Ok(Err(e)) => Err((StatusCode::NOT_FOUND, e)),
        Err(_) => Err((StatusCode::INTERNAL_SERVER_ERROR, "engine dropped reply".to_string())),
    }
}

async fn stop_strategy_handler(
    State(cmd_tx): State<mpsc::Sender<EngineCommand>>,
    Path(id): Path<String>,
) -> Result<Json<serde_json::Value>, (StatusCode, String)> {
    let (tx, rx) = oneshot::channel();
    cmd_tx
        .send(EngineCommand::StopStrategy { id, reply: tx })
        .await
        .map_err(|_| (StatusCode::SERVICE_UNAVAILABLE, "engine offline".to_string()))?;
    match rx.await {
        Ok(Ok(())) => Ok(Json(serde_json::json!({"stopped": true}))),
        Ok(Err(e)) => Err((StatusCode::NOT_FOUND, e)),
        Err(_) => Err((StatusCode::INTERNAL_SERVER_ERROR, "engine dropped reply".to_string())),
    }
}

async fn strategy_command_handler(
    State(cmd_tx): State<mpsc::Sender<EngineCommand>>,
    Path(id): Path<String>,
    Json(body): Json<serde_json::Value>,
) -> Result<Json<serde_json::Value>, (StatusCode, String)> {
    let (tx, rx) = oneshot::channel();
    cmd_tx
        .send(EngineCommand::StrategyCommand { id, body, reply: tx })
        .await
        .map_err(|_| (StatusCode::SERVICE_UNAVAILABLE, "engine offline".to_string()))?;
    match rx.await {
        Ok(Ok(v)) => Ok(Json(v)),
        Ok(Err(e)) => Err((StatusCode::BAD_REQUEST, e)),
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

/// `GET /tape?since=<epoch_float>&limit=<n>` — the bounded tail of the
/// decision tape, so a `pmt crypto watch` running off-box (control URL through
/// an SSM tunnel) can see the same records the desktop reads straight off the
/// file. Read-only, stateless, and the only handler that never enters the
/// engine's command queue.
///
/// Measured release-build cost against the live 24.7MB tape, 2026-08-23:
/// **88µs** in the steady state (a 2s-poll cursor, 7 new records), 162µs for
/// a 30s cursor, 471µs cold at the default limit and 1.20ms cold at the 500
/// cap. Flat in file size by construction — see `tape_tail`.
async fn tape_handler(Query(q): Query<TapeQuery>) -> Result<Json<TapeSlice>, StatusCode> {
    let since = q.since.unwrap_or(0.0);
    let limit = q.limit.unwrap_or(TAPE_LIMIT_DEFAULT).clamp(1, TAPE_LIMIT_MAX);
    let Some(path) = crate::jsonl::engine_dir().map(|d| d.join(TAPE_FILE)) else {
        return Ok(Json(TapeSlice::default()));
    };
    // spawn_blocking, not the event loop. It is a small read, but it is real
    // file I/O in a process where the axum tasks share a runtime with the
    // trading select! — the cost of getting that wrong is measured in seconds.
    match tokio::task::spawn_blocking(move || tape_tail(&path, since, limit)).await {
        Ok(Ok(slice)) => Ok(Json(slice)),
        // No tape yet isn't an error: a fresh engine simply has nothing to
        // show, and a remote dashboard must not read that as a dead plane.
        Ok(Err(e)) if e.kind() == std::io::ErrorKind::NotFound => Ok(Json(TapeSlice::default())),
        Ok(Err(_)) | Err(_) => Err(StatusCode::INTERNAL_SERVER_ERROR),
    }
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

/// Body for `POST /orders/{id}/schedule-cancel`. Either `at` (absolute
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

#[cfg(test)]
mod tests {
    use super::*;
    use std::io::Write;
    use std::path::{Path, PathBuf};

    #[test]
    fn router_constructs_without_panicking() {
        let (tx, _rx) = mpsc::channel::<EngineCommand>(1);
        let _ = build_router(tx);
    }

    // ---------- GET /tape ----------

    /// Own directory per test + pid, so parallel runs never share a tape.
    fn scratch(name: &str) -> PathBuf {
        let dir = std::env::temp_dir()
            .join(format!("pmengine-tape-{}-{}", name, std::process::id()));
        let _ = std::fs::remove_dir_all(&dir);
        std::fs::create_dir_all(&dir).unwrap();
        dir
    }

    /// A tape of `n` eval records at t = 1000, 1001, ... — plus enough padding
    /// per line to make byte-window arithmetic testable at a known size.
    fn write_tape(path: &Path, n: usize, pad: usize) {
        let f = std::fs::File::create(path).unwrap();
        let mut w = std::io::BufWriter::new(f);
        for i in 0..n {
            let rec = serde_json::json!({
                "t": 1000.0 + i as f64, "ev": "eval", "slug": "btc-updown-5m-1",
                "pad": "x".repeat(pad),
            });
            writeln!(w, "{}", rec).unwrap();
        }
        w.flush().unwrap();
    }

    #[test]
    fn tape_tail_returns_only_records_past_the_cursor_oldest_first() {
        let dir = scratch("cursor");
        let p = dir.join("tape.jsonl");
        write_tape(&p, 10, 0);

        let slice = tape_tail(&p, 1005.0, 200).unwrap();
        let ts: Vec<f64> = slice.records.iter().map(|r| r["t"].as_f64().unwrap()).collect();
        assert_eq!(ts, vec![1006.0, 1007.0, 1008.0, 1009.0]);
        assert_eq!(slice.cursor, Some(1009.0));
        // The whole file fit in the window and nothing hit the cap.
        assert!(!slice.truncated);
    }

    #[test]
    fn tape_tail_with_no_cursor_returns_the_whole_small_file_untruncated() {
        let dir = scratch("cold");
        let p = dir.join("tape.jsonl");
        write_tape(&p, 5, 0);

        let slice = tape_tail(&p, 0.0, 200).unwrap();
        assert_eq!(slice.records.len(), 5);
        assert!(!slice.truncated, "a file smaller than the window hides nothing");
    }

    #[test]
    fn tape_tail_caps_at_limit_and_keeps_the_newest() {
        let dir = scratch("cap");
        let p = dir.join("tape.jsonl");
        write_tape(&p, 50, 0);

        let slice = tape_tail(&p, 0.0, 10).unwrap();
        assert_eq!(slice.records.len(), 10);
        // The NEWEST ten, not the oldest: a dashboard wants current, and
        // says so by marking the answer truncated.
        assert_eq!(slice.records[0]["t"].as_f64().unwrap(), 1040.0);
        assert_eq!(slice.cursor, Some(1049.0));
        assert!(slice.truncated);
    }

    #[test]
    fn tape_tail_never_scans_past_the_byte_window() {
        let dir = scratch("window");
        let p = dir.join("tape.jsonl");
        // ~1KB per record over 4MB of file: eight times the 512KB window, so a
        // handler that scanned the whole thing would answer from record 0.
        write_tape(&p, 4000, 900);
        let size = std::fs::metadata(&p).unwrap().len();
        assert!(size > 4 * TAPE_TAIL_BYTES, "fixture must dwarf the window");

        // A limit far above what the window holds, so the BYTE bound is what
        // stops this and not the record cap.
        let slice = tape_tail(&p, 0.0, 100_000).unwrap();
        let oldest = slice.records.first().unwrap()["t"].as_f64().unwrap();
        let in_window = TAPE_TAIL_BYTES as f64 / (size as f64 / 4000.0);
        assert!(
            oldest > 1000.0 + 4000.0 - in_window * 1.1,
            "answered from t={oldest}, which is older than the last 512KB holds"
        );
        // Newest record always present — recency is the whole point.
        assert_eq!(slice.cursor, Some(4999.0));
        assert!(slice.truncated, "a cursor the window can't reach is a gap, and must say so");
    }

    #[test]
    fn tape_tail_marks_truncation_only_when_the_window_misses_the_cursor() {
        let dir = scratch("trunc");
        let p = dir.join("tape.jsonl");
        write_tape(&p, 4000, 900);

        // A cursor inside the window: complete answer, nothing hidden.
        let fresh = tape_tail(&p, 4990.0, TAPE_LIMIT_MAX).unwrap();
        assert_eq!(fresh.records.len(), 9);
        assert!(!fresh.truncated, "the window covered this cursor");

        // A cursor from before the window: the gap is real and reported.
        let stale = tape_tail(&p, 1001.0, TAPE_LIMIT_MAX).unwrap();
        assert!(stale.truncated);
        assert!(!stale.records.is_empty(), "truncated still returns what it has");
    }

    #[test]
    fn tape_tail_skips_malformed_lines_without_failing_the_request() {
        let dir = scratch("malformed");
        let p = dir.join("tape.jsonl");
        let mut f = std::fs::File::create(&p).unwrap();
        writeln!(f, r#"{{"t":1000.0,"ev":"eval"}}"#).unwrap();
        writeln!(f, r#"{{"t":1001.0,"ev":"fi"#).unwrap();   // torn mid-write append
        writeln!(f).unwrap();                                // blank
        writeln!(f, r#"["not","an","object"]"#).unwrap();
        writeln!(f, r#"{{"ev":"eval"}}"#).unwrap();          // no t
        writeln!(f, r#"{{"t":"soon","ev":"eval"}}"#).unwrap(); // t isn't a number
        writeln!(f, r#"{{"t":1002.0,"ev":"fire"}}"#).unwrap();
        drop(f);

        let slice = tape_tail(&p, 0.0, 200).unwrap();
        let ts: Vec<f64> = slice.records.iter().map(|r| r["t"].as_f64().unwrap()).collect();
        assert_eq!(ts, vec![1000.0, 1002.0]);
    }

    #[test]
    fn tape_tail_drops_the_partial_line_the_window_starts_inside() {
        let dir = scratch("partial");
        let p = dir.join("tape.jsonl");
        write_tape(&p, 4000, 900);

        // Every record that comes back must be whole and parseable — the seek
        // lands mid-record, and half a line is not a record.
        let slice = tape_tail(&p, 0.0, TAPE_LIMIT_MAX).unwrap();
        for r in &slice.records {
            assert!(r.get("ev").is_some() && r.get("t").is_some());
        }
    }

    #[test]
    fn tape_tail_on_a_missing_tape_is_a_not_found_the_handler_can_answer_empty() {
        let missing = std::env::temp_dir().join("pmengine-tape-definitely-absent.jsonl");
        let err = tape_tail(&missing, 0.0, 200).unwrap_err();
        assert_eq!(err.kind(), std::io::ErrorKind::NotFound);
    }

    #[test]
    fn tape_tail_cost_is_flat_in_file_size() {
        let dir = scratch("cost");
        let p = dir.join("tape.jsonl");
        // 4MB, eight windows deep. A full scan of the live 24MB tape runs
        // hundreds of ms; the bound here is loose enough for a cold CI box and
        // still an order of magnitude under that.
        write_tape(&p, 4000, 900);

        let start = std::time::Instant::now();
        for _ in 0..5 {
            let _ = tape_tail(&p, 0.0, TAPE_LIMIT_MAX).unwrap();
        }
        let per_call = start.elapsed() / 5;
        assert!(
            per_call < std::time::Duration::from_millis(50),
            "tape_tail took {per_call:?} — the read is supposed to be window-bounded"
        );
    }
}

