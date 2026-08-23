//! Live market-WS probe — read-only, places nothing.
//!
//! Proves the ENGINE's feed path from whatever box it runs on: it builds the
//! real `MarketDataHub` and the real `WsFeed`, subscribes the token ids you
//! pass, and samples book age exactly the way a strategy would (off the hub,
//! at tick cadence). WS behaviour can't be unit-tested; this is the check
//! that replaces it.
//!
//! ```text
//! cargo run --release --features ec2 --bin pmengine-wsprobe -- <token_id> [more...] [--secs 60]
//! ```
//!
//! Reads `book age p50` in ms. REST-cadence bound looks like ~1000-3000ms;
//! a live WS on an active market is a small multiple of the event gap.

use pmengine::orderbook::{now_ms, MarketDataHub};
use pmengine::wsfeed::WsFeed;
use std::sync::Arc;
use std::time::{Duration, Instant};

/// Sampling cadence — matches the engine's own tick, so the ages printed are
/// the ages a strategy would have read.
const SAMPLE: Duration = Duration::from_millis(50);

#[tokio::main]
async fn main() {
    tracing_subscriber::fmt()
        .with_max_level(tracing::Level::INFO)
        .compact()
        .init();

    let mut tokens: Vec<String> = Vec::new();
    let mut secs: u64 = 60;
    let mut args = std::env::args().skip(1);
    while let Some(arg) = args.next() {
        match arg.as_str() {
            "--secs" => {
                secs = args
                    .next()
                    .and_then(|v| v.parse().ok())
                    .unwrap_or_else(|| die("--secs needs a number"));
            }
            "-h" | "--help" => die("usage: pmengine-wsprobe <token_id>... [--secs N]"),
            other => tokens.push(other.to_string()),
        }
    }
    if tokens.is_empty() {
        die("usage: pmengine-wsprobe <token_id>... [--secs N]");
    }

    let hub = Arc::new(MarketDataHub::new(1000));
    for t in &tokens {
        hub.init_book(t).await;
    }

    let feed = WsFeed::spawn(hub.clone());
    let health = feed.health();
    for t in &tokens {
        feed.subscribe(t);
    }

    println!(
        "probing {} token(s) for {}s — read-only market data\n",
        tokens.len(),
        secs
    );

    let start = Instant::now();
    let mut ages: Vec<i64> = Vec::new();
    let mut first_event_at: Option<Duration> = None;
    let mut last_report = Instant::now();
    let mut last_events: u64 = 0;

    while start.elapsed() < Duration::from_secs(secs) {
        tokio::time::sleep(SAMPLE).await;

        if first_event_at.is_none() && health.events() > 0 {
            first_event_at = Some(start.elapsed());
        }

        let now = now_ms();
        for book in hub.get_all_books().await.values() {
            if let Some(age) = book.age_ms(now) {
                ages.push(age);
            }
        }

        if last_report.elapsed() >= Duration::from_secs(5) {
            let events = health.events();
            let rate = (events - last_events) as f64 / last_report.elapsed().as_secs_f64();
            last_events = events;
            last_report = Instant::now();
            let stats = hub.book_age_stats().await;
            println!(
                "t+{:>3}s  connected={:<5} events={:<7} {:>6.1}/s  age p50={:?} p90={:?} max={:?}  ws_books={} rest_books={}",
                start.elapsed().as_secs(),
                health.is_connected(),
                events,
                rate,
                stats.p50_ms,
                stats.p90_ms,
                stats.max_ms,
                stats.from_ws,
                stats.from_rest,
            );
        }
    }

    ages.sort_unstable();
    let pick = |q: f64| -> i64 {
        if ages.is_empty() {
            return -1;
        }
        ages[(((ages.len() - 1) as f64) * q).round() as usize]
    };
    let events = health.events();

    println!("\n--- summary ---------------------------------------------");
    println!("connected           : {}", health.is_connected());
    println!(
        "time to first event : {}",
        first_event_at.map_or("never".to_string(), |d| format!("{:.0}ms", d.as_millis()))
    );
    println!("events              : {}", events);
    println!(
        "events/sec          : {:.1}",
        events as f64 / start.elapsed().as_secs_f64()
    );
    println!(
        "book age (ms)       : p50 {} p90 {} p99 {} max {}  over {} samples",
        pick(0.50),
        pick(0.90),
        pick(0.99),
        ages.last().copied().unwrap_or(-1),
        ages.len()
    );
    for (token, book) in hub.get_all_books().await {
        println!(
            "  {}… src={} updates={} bid={:?} ask={:?}",
            &token[..12.min(token.len())],
            book.source.as_str(),
            book.update_count,
            book.best_bid().map(|l| l.price),
            book.best_ask().map(|l| l.price),
        );
    }

    feed.abort();
}

fn die(msg: &str) -> ! {
    eprintln!("{}", msg);
    std::process::exit(2);
}
