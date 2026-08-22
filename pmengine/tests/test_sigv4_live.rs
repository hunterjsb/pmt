//! Live smoke tests against the deployed pmproxy Function URL.
//!
//! Ignored by default: they need network, `PMPROXY_URL`, and AWS credentials
//! resolvable from the default chain. Run explicitly with
//! `cargo test --test test_sigv4_live -- --ignored`.

#![cfg(feature = "sigv4")]

use pmengine::sigv4::SigV4Signer;

fn load_env() -> String {
    dotenvy::from_path(concat!(env!("CARGO_MANIFEST_DIR"), "/../.env")).ok();
    std::env::var("PMPROXY_URL")
        .expect("PMPROXY_URL not set")
        .trim_end_matches('/')
        .to_string()
}

#[tokio::test]
#[ignore = "hits the live proxy; needs PMPROXY_URL + AWS credentials"]
async fn signed_health_check() {
    let proxy = load_env();
    let signer = SigV4Signer::from_env().await.expect("AWS credentials");

    let url = format!("{}/health", proxy);
    let headers = signer.sign_headers("GET", &url, b"").expect("sign");
    let resp = reqwest::Client::new()
        .get(&url)
        .headers(headers)
        .send()
        .await
        .expect("request");
    let status = resp.status();
    let body = resp.text().await.unwrap_or_default();
    assert!(status.is_success(), "HTTP {}: {}", status, body);
}

#[tokio::test]
#[ignore = "hits the live proxy; needs PMPROXY_URL + AWS credentials"]
async fn signed_book_through_proxy() {
    let proxy = load_env();
    let signer = SigV4Signer::from_env().await.expect("AWS credentials");

    // Any live CLOB token works; grab one from gamma so the test doesn't
    // go stale when a hardcoded market resolves.
    let gamma: serde_json::Value = reqwest::Client::new()
        .get("https://gamma-api.polymarket.com/markets?closed=false&limit=1&order=volumeNum&ascending=false")
        .send()
        .await
        .expect("gamma request")
        .json()
        .await
        .expect("gamma json");
    let token_ids: Vec<String> = serde_json::from_str(
        gamma[0]["clobTokenIds"].as_str().expect("clobTokenIds"),
    )
    .expect("token ids");

    let url = format!("{}/clob/book?token_id={}", proxy, token_ids[0]);
    let headers = signer.sign_headers("GET", &url, b"").expect("sign");
    let resp = reqwest::Client::new()
        .get(&url)
        .headers(headers)
        .send()
        .await
        .expect("request");
    let status = resp.status();
    let body = resp.text().await.unwrap_or_default();
    assert!(status.is_success(), "HTTP {}: {}", status, body);
    assert!(body.contains("bids"), "unexpected body: {}", body);
}
