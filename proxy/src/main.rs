use clap::Parser;
use pmproxy::{build_router, ProxyState};
use std::sync::Arc;
use tracing::{info, Level};
use tracing_subscriber::FmtSubscriber;

#[derive(Parser, Debug)]
#[command(
    name = "pmproxy",
    about = "Simple HTTP reverse proxy for Polymarket APIs"
)]
struct Args {
    /// Host to bind to
    #[arg(short = 'H', long, default_value = "0.0.0.0")]
    host: String,

    /// Port to listen on
    #[arg(short, long, default_value = "8080")]
    port: u16,

    /// Log level
    #[arg(short, long, default_value = "info")]
    log_level: String,
}

#[tokio::main]
async fn main() -> Result<(), Box<dyn std::error::Error>> {
    let args = Args::parse();

    // Set up logging
    let level = match args.log_level.to_lowercase().as_str() {
        "trace" => Level::TRACE,
        "debug" => Level::DEBUG,
        "info" => Level::INFO,
        "warn" => Level::WARN,
        "error" => Level::ERROR,
        _ => Level::INFO,
    };

    FmtSubscriber::builder()
        .with_max_level(level)
        .with_target(false)
        .compact()
        .init();

    let state = Arc::new(ProxyState::new()?);
    let app = build_router(state);

    let addr = format!("{}:{}", args.host, args.port);
    info!("pmproxy starting on http://{}", addr);
    info!("  Routes:");
    info!("    /clob/*   → https://clob.polymarket.com/*");
    info!("    /gamma/*  → https://gamma-api.polymarket.com/*");
    info!("    /chain/*  → https://polygon-rpc.com");

    let listener = tokio::net::TcpListener::bind(&addr).await?;
    axum::serve(listener, app).await?;

    Ok(())
}
