//! `GET /tape` over a real socket — the half of the endpoint a unit test on
//! `tape_tail` cannot reach.
//!
//! Three things only live at the HTTP layer and each has cost someone a
//! debugging session somewhere: that the route is actually in the table (axum
//! resolves routes at runtime — see `build_router`'s comment for the time
//! that took the whole control plane down with `cargo test` green), that the
//! query string parses into the handler's types, and that the 500-record hard
//! cap is enforced against a caller who asks for more.
//!
//! Its own binary with a single test, because it clobbers `HOME` for the
//! whole process — the tape path derives from it. Same reason, same shape as
//! characterization_offline.rs.

use pmengine::control::{self, EngineCommand};

/// 2000 records of ~450 bytes each: ~900KB, comfortably more than the 512KB
/// read window, so the byte bound and the record cap are both in play.
const RECORDS: usize = 2000;

fn fake_home() -> std::path::PathBuf {
    let home = std::env::temp_dir().join(format!("pmengine-tape-http-{}", std::process::id()));
    let dir = home.join(".pmt/engine");
    let _ = std::fs::remove_dir_all(&home);
    std::fs::create_dir_all(&dir).unwrap();
    let body: String = (0..RECORDS)
        .map(|i| {
            format!(
                "{{\"t\":{}.5,\"ev\":\"eval\",\"slug\":\"btc-updown-5m-1\",\"pad\":\"{}\"}}\n",
                1000 + i,
                "x".repeat(400)
            )
        })
        .collect();
    std::fs::write(dir.join("updown-tape.jsonl"), body).unwrap();
    home
}

#[tokio::test]
async fn tape_endpoint_answers_over_http() {
    let home = fake_home();
    std::env::set_var("HOME", &home);

    // The engine end of the command channel is never read: this endpoint is
    // the one that must answer WITHOUT the trading loop's help, and a dangling
    // receiver proves it.
    let (tx, _rx) = tokio::sync::mpsc::channel::<EngineCommand>(8);
    let port = 20000 + (std::process::id() % 20000) as u16;
    let addr: std::net::SocketAddr = format!("127.0.0.1:{port}").parse().unwrap();
    let _server = control::spawn(addr, tx).await.unwrap();
    tokio::time::sleep(std::time::Duration::from_millis(150)).await;

    let client = reqwest::Client::new();
    let newest = 1000.0 + RECORDS as f64 - 1.0 + 0.5;
    let get = |q: &str| {
        let c = client.clone();
        let url = format!("http://127.0.0.1:{port}/tape{q}");
        async move {
            let r = c.get(&url).send().await.expect("request");
            assert_eq!(r.status(), 200, "{url}");
            r.json::<serde_json::Value>().await.expect("json")
        }
    };
    let ts = |v: &serde_json::Value| -> Vec<f64> {
        v["records"].as_array().unwrap().iter().map(|r| r["t"].as_f64().unwrap()).collect()
    };

    // No query at all: the default limit, the newest records, oldest-first.
    let v = get("").await;
    let got = ts(&v);
    assert_eq!(got.len(), 200, "default limit");
    assert!(got.windows(2).all(|w| w[0] < w[1]), "records must arrive oldest-first");
    assert_eq!(*got.last().unwrap(), newest);
    assert_eq!(v["cursor"].as_f64(), Some(newest));
    assert_eq!(v["truncated"], true);

    // A cursor from four records ago: the whole gap, and nothing hidden.
    let v = get(&format!("?since={}", newest - 4.0)).await;
    assert_eq!(ts(&v), vec![newest - 3.0, newest - 2.0, newest - 1.0, newest]);
    assert_eq!(v["truncated"], false);

    // Caught up: an empty answer with a null cursor, which is what the poll
    // sees on nearly every tick and must not mistake for a failure.
    let v = get(&format!("?since={newest}")).await;
    assert_eq!(ts(&v), Vec::<f64>::new());
    assert!(v["cursor"].is_null());
    assert_eq!(v["truncated"], false);

    // The hard cap is the engine's to enforce, not the caller's to choose.
    let v = get("?since=0&limit=99999").await;
    assert_eq!(ts(&v).len(), 500);
    assert_eq!(v["truncated"], true);

    let _ = std::fs::remove_dir_all(&home);
}
