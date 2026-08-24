//! Live transport measurements against the CLOB, for order-path latency work.
//!
//! Ignored by default: they need the network and they take minutes. Run with
//! `cargo test --features ec2 --test order_transport_live -- --ignored --nocapture`.
//!
//! Everything here is UNAUTHENTICATED — `/ok` is a 4-byte public GET. No key
//! material, no L2 headers, and nothing that could place an order. The point
//! is to time the connection, and the connection does not care what rides it.
//!
//! What these prove, and why they are shaped this way: the order path's whole
//! transport cost is the TCP+TLS handshake it does or does not have to do
//! (~18ms to the CLOB from eu-west-1, ~16ms of it TLS). A tight loop can never
//! show that, because a tight loop is always warm. Only an idle gap longer
//! than the pool's idle timeout can, which is why the interesting test sleeps.
//!
//! See `analysis/order_latency_eu.md` for the decomposition these came out of.

use std::time::{Duration, Instant};

/// A 4-byte public GET. Small enough that the response body is one packet, so
/// what is measured is the round trip and not the download.
const PROBE_URL: &str = "https://clob.polymarket.com/ok";

/// The client pmengine shipped before the transport invariants were pinned:
/// reqwest's defaults, notably a 90s pool idle timeout and no h2 keep-alive.
/// Kept here as the BASELINE arm — it is what the numbers are measured against.
fn legacy_client() -> reqwest::Client {
    reqwest::Client::builder()
        .timeout(Duration::from_secs(30))
        .build()
        .expect("legacy client")
}

async fn probe(http: &reqwest::Client) -> Duration {
    let start = Instant::now();
    let resp = http.get(PROBE_URL).send().await.expect("probe request");
    assert!(resp.status().is_success(), "probe got {}", resp.status());
    resp.text().await.expect("probe body");
    start.elapsed()
}

fn ms(d: Duration) -> f64 {
    d.as_secs_f64() * 1000.0
}

fn pct(mut v: Vec<f64>, p: f64) -> f64 {
    v.sort_by(|a, b| a.partial_cmp(b).expect("no NaN timings"));
    let k = (v.len() - 1) as f64 * p / 100.0;
    let (f, c) = (k.floor() as usize, k.ceil() as usize);
    v[f] + (v[c] - v[f]) * (k - k.floor())
}

fn report(label: &str, v: Vec<f64>) {
    println!(
        "{label:34} n={:3} min={:7.2} p50={:7.2} p90={:7.2} max={:7.2}",
        v.len(),
        v.iter().cloned().fold(f64::INFINITY, f64::min),
        pct(v.clone(), 50.0),
        pct(v.clone(), 90.0),
        v.iter().cloned().fold(f64::NEG_INFINITY, f64::max),
    );
}

/// Warm-loop control. Both clients should be identical here, and that is the
/// POINT: the change is not supposed to make a hot path faster, so a
/// difference in this test would mean it did something unintended.
#[tokio::test]
#[ignore = "hits the live CLOB; run explicitly"]
async fn warm_loop_is_unchanged_by_the_new_settings() {
    const N: usize = 50;
    for (label, http) in [
        ("legacy (reqwest defaults)", legacy_client()),
        (
            "current (pinned invariants)",
            pmengine::client::build_order_http_client().expect("current client"),
        ),
    ] {
        // Discard the first request: it pays the handshake in both arms and
        // would otherwise sit in the tail as a fake regression.
        probe(&http).await;
        let mut out = Vec::with_capacity(N);
        for _ in 0..N {
            out.push(ms(probe(&http).await));
        }
        report(&format!("warm x{N} {label}"), out);
    }
}

/// The measurement the change is actually for.
///
/// Sleeps past reqwest's 90s default pool idle timeout but inside the engine's
/// 300s one, then times ONE request — the shape of a real first fire after a
/// quiet stretch between windows. The legacy arm has to rebuild TCP+TLS; the
/// current arm should still be holding the connection open.
#[tokio::test]
#[ignore = "hits the live CLOB and sleeps ~2x100s; run explicitly"]
async fn an_idle_gap_costs_the_legacy_client_a_handshake() {
    const IDLE: Duration = Duration::from_secs(100);
    let mut results = Vec::new();
    for (label, http) in [
        ("legacy (90s pool idle)", legacy_client()),
        (
            "current (300s pool + h2 ping)",
            pmengine::client::build_order_http_client().expect("current client"),
        ),
    ] {
        let warm = ms(probe(&http).await);
        // A second request proves the connection is pooled before the sleep,
        // so what the sleep changes is unambiguous.
        let pooled = ms(probe(&http).await);
        tokio::time::sleep(IDLE).await;
        let after_idle = ms(probe(&http).await);
        println!(
            "{label:32} cold={warm:7.2}ms pooled={pooled:7.2}ms after-{}s-idle={after_idle:7.2}ms",
            IDLE.as_secs()
        );
        results.push((label, pooled, after_idle));
    }
    println!("\n-- what the idle gap cost each client (after-idle minus pooled) --");
    for (label, pooled, after) in &results {
        println!("{label:32} {:+7.2}ms", after - pooled);
    }
}
