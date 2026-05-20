//! WebSocket proxy handler — bridges client WS connections to Polymarket's WS API.
//!
//! Only compiled when the `ws` feature is enabled (EC2 deployment), since
//! Lambda Function URLs cannot upgrade HTTP to WebSocket.
//!
//! Routes `/clob/ws/<channel>` to `wss://ws-subscriptions-clob.polymarket.com/ws/<channel>`.
//! Authentication is performed via the Authorization header on the upgrade request,
//! same as HTTP routes.

use std::sync::Arc;

use axum::{
    extract::{
        ws::{Message as AxumMessage, WebSocket, WebSocketUpgrade},
        Path, State,
    },
    http::{header, HeaderMap, StatusCode},
    response::{IntoResponse, Response},
};
use futures_util::{SinkExt, StreamExt};
use tokio_tungstenite::{connect_async, tungstenite::Message as TungMessage};
use tracing::{debug, error, info, warn};

use crate::{authenticate, ProxyState};

const UPSTREAM_WS_BASE: &str = "wss://ws-subscriptions-clob.polymarket.com/ws";

/// Axum handler for WS upgrade requests at `/clob/ws/{channel}`.
pub async fn ws_handler(
    State(state): State<Arc<ProxyState>>,
    Path(channel): Path<String>,
    headers: HeaderMap,
    ws: WebSocketUpgrade,
) -> Response {
    // Validate JWT before upgrading.
    let auth_header = headers
        .get(header::AUTHORIZATION)
        .and_then(|v| v.to_str().ok());

    let tenant = match authenticate(&state, auth_header).await {
        Ok(t) => t,
        Err(e) => {
            warn!(error = ?e, "WS upgrade rejected: auth failed");
            return e.into_response();
        }
    };

    if let Some(ref t) = tenant {
        info!(tenant_id = %t.tenant_id, channel = %channel, "Accepting WS upgrade");
    } else {
        info!(channel = %channel, "Accepting WS upgrade (no auth)");
    }

    // Validate channel — only allow known Polymarket channels.
    if channel != "market" && channel != "user" {
        warn!(channel = %channel, "Rejected WS upgrade: unknown channel");
        return (StatusCode::NOT_FOUND, "Unknown WS channel").into_response();
    }

    let upstream_url = format!("{}/{}", UPSTREAM_WS_BASE, channel);

    ws.on_upgrade(move |socket| handle_socket(socket, upstream_url))
}

/// Bidirectionally pipe frames between client `socket` and upstream Polymarket WS.
async fn handle_socket(client_socket: WebSocket, upstream_url: String) {
    debug!(upstream = %upstream_url, "Connecting to upstream WS");

    let upstream = match connect_async(&upstream_url).await {
        Ok((s, _resp)) => s,
        Err(e) => {
            error!(error = %e, upstream = %upstream_url, "Failed to connect to upstream WS");
            return;
        }
    };

    let (mut client_tx, mut client_rx) = client_socket.split();
    let (mut up_tx, mut up_rx) = upstream.split();

    // client → upstream
    let c2u = async move {
        while let Some(result) = client_rx.next().await {
            match result {
                Ok(msg) => {
                    let tung_msg = match axum_to_tungstenite(msg) {
                        Some(m) => m,
                        None => continue, // ignore unconvertible (e.g. axum's reserved frames)
                    };
                    if let Err(e) = up_tx.send(tung_msg).await {
                        debug!(error = %e, "client→upstream send failed");
                        break;
                    }
                }
                Err(e) => {
                    debug!(error = %e, "client recv error");
                    break;
                }
            }
        }
    };

    // upstream → client
    let u2c = async move {
        while let Some(result) = up_rx.next().await {
            match result {
                Ok(msg) => {
                    let axum_msg = match tungstenite_to_axum(msg) {
                        Some(m) => m,
                        None => continue,
                    };
                    if let Err(e) = client_tx.send(axum_msg).await {
                        debug!(error = %e, "upstream→client send failed");
                        break;
                    }
                }
                Err(e) => {
                    debug!(error = %e, "upstream recv error");
                    break;
                }
            }
        }
    };

    // When either side closes, drop both halves.
    tokio::select! {
        _ = c2u => debug!("Client side closed"),
        _ = u2c => debug!("Upstream side closed"),
    }
}

fn axum_to_tungstenite(msg: AxumMessage) -> Option<TungMessage> {
    match msg {
        AxumMessage::Text(t) => Some(TungMessage::Text(t.to_string().into())),
        AxumMessage::Binary(b) => Some(TungMessage::Binary(b.to_vec().into())),
        AxumMessage::Ping(p) => Some(TungMessage::Ping(p.to_vec().into())),
        AxumMessage::Pong(p) => Some(TungMessage::Pong(p.to_vec().into())),
        AxumMessage::Close(Some(cf)) => Some(TungMessage::Close(Some(
            tokio_tungstenite::tungstenite::protocol::CloseFrame {
                code: cf.code.into(),
                reason: cf.reason.to_string().into(),
            },
        ))),
        AxumMessage::Close(None) => Some(TungMessage::Close(None)),
    }
}

fn tungstenite_to_axum(msg: TungMessage) -> Option<AxumMessage> {
    match msg {
        TungMessage::Text(t) => Some(AxumMessage::Text(t.to_string().into())),
        TungMessage::Binary(b) => Some(AxumMessage::Binary(b.to_vec().into())),
        TungMessage::Ping(p) => Some(AxumMessage::Ping(p.to_vec().into())),
        TungMessage::Pong(p) => Some(AxumMessage::Pong(p.to_vec().into())),
        TungMessage::Close(Some(cf)) => Some(AxumMessage::Close(Some(
            axum::extract::ws::CloseFrame {
                code: cf.code.into(),
                reason: cf.reason.to_string().into(),
            },
        ))),
        TungMessage::Close(None) => Some(AxumMessage::Close(None)),
        TungMessage::Frame(_) => None, // raw frames not exposed by axum
    }
}
