//! End-to-end WebSocket bridge test.
//!
//! Stands up:
//! 1. A fake upstream WS server that echoes whatever it receives.
//! 2. pmproxy bound to a localhost port (auth disabled, ws feature on),
//!    pointed at the fake upstream via PMPROXY_WS_UPSTREAM_BASE.
//! 3. A WS client that connects to pmproxy and sends a text frame.
//!
//! Asserts the frame round-trips back, exercising both halves of the
//! bidirectional bridge.

#![cfg(feature = "ws")]

use std::net::SocketAddr;
use std::sync::Arc;
use std::time::Duration;

use futures_util::{SinkExt, StreamExt};
use pmproxy::{build_router, ProxyState};
use tokio::net::TcpListener;
use tokio_tungstenite::{accept_async, connect_async, tungstenite::Message};

/// Bind an ephemeral port and return (listener, bound addr).
async fn bind_ephemeral() -> (TcpListener, SocketAddr) {
    let listener = TcpListener::bind("127.0.0.1:0").await.unwrap();
    let addr = listener.local_addr().unwrap();
    (listener, addr)
}

/// Spawn a WS echo server. Accepts a single connection, echoes every text
/// message back, closes on disconnect.
async fn spawn_echo_upstream() -> SocketAddr {
    let (listener, addr) = bind_ephemeral().await;
    tokio::spawn(async move {
        let (stream, _) = listener.accept().await.unwrap();
        let mut ws = accept_async(stream).await.unwrap();
        while let Some(Ok(msg)) = ws.next().await {
            if msg.is_close() {
                break;
            }
            if let Message::Text(t) = msg {
                let _ = ws.send(Message::Text(t)).await;
            }
        }
    });
    addr
}

/// Spawn pmproxy in-process. Returns the bound URL.
async fn spawn_pmproxy() -> String {
    let (listener, addr) = bind_ephemeral().await;
    let state = Arc::new(ProxyState::default()); // auth disabled
    let app = build_router(state);
    tokio::spawn(async move {
        axum::serve(listener, app).await.unwrap();
    });
    format!("ws://{}", addr)
}

#[tokio::test]
async fn ws_frame_roundtrips_through_pmproxy() {
    // 1. Fake upstream
    let upstream_addr = spawn_echo_upstream().await;
    let upstream_base = format!("ws://{}", upstream_addr);
    // SAFETY: tests modify env; tokio test runtime serializes by default
    unsafe {
        std::env::set_var("PMPROXY_WS_UPSTREAM_BASE", &upstream_base);
    }

    // 2. pmproxy in-process
    let proxy_base = spawn_pmproxy().await;
    // Tiny pause so axum::serve binds before we connect.
    tokio::time::sleep(Duration::from_millis(50)).await;

    // 3. Client connects through pmproxy to channel "market" (which the
    //    handler allowlists), then sends a frame and waits for the echo.
    let proxy_url = format!("{}/clob/ws/market", proxy_base);
    let (mut client, _) = connect_async(&proxy_url).await.expect("client connect");

    client.send(Message::Text("hello-roundtrip".into())).await.unwrap();

    // Receive the echo (with a timeout so a hang doesn't deadlock the test).
    let received = tokio::time::timeout(Duration::from_secs(2), client.next())
        .await
        .expect("timed out waiting for echo")
        .expect("stream closed early")
        .expect("recv error");

    match received {
        Message::Text(t) => assert_eq!(t.as_str(), "hello-roundtrip"),
        other => panic!("expected Text echo, got {other:?}"),
    }

    let _ = client.close(None).await;
}

#[tokio::test]
async fn ws_rejects_unknown_channel() {
    // Even with auth off, the handler should refuse channels other than
    // "market" / "user".
    let proxy_base = spawn_pmproxy().await;
    tokio::time::sleep(Duration::from_millis(50)).await;

    let url = format!("{}/clob/ws/orderbook", proxy_base);
    let result = connect_async(&url).await;
    // Either we get an outright HTTP error (not a 101 upgrade) or the
    // connection comes back already rejected. Both signal "not upgraded".
    assert!(result.is_err(), "unknown channel should NOT upgrade, got {result:?}");
}
