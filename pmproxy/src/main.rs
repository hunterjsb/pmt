use clap::Parser;
use pmproxy::{build_router, config::ProxyConfig, ProxyState};
use std::sync::Arc;
use tracing::{info, warn, Level};
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

    // Load configuration
    let config = ProxyConfig::from_env();

    // Create state with or without auth
    let state = Arc::new(ProxyState::with_auth(&config)?);

    // Pre-fetch JWKS if auth is enabled
    if config.auth_enabled {
        info!(
            cognito_region = %config.cognito_region,
            cognito_pool_id = %config.cognito_pool_id,
            "Authentication enabled, fetching JWKS..."
        );

        if let Err(e) = state.prefetch_jwks().await {
            warn!(error = %e, "Failed to pre-fetch JWKS (will retry on first request)");
        }
    }

    let app = build_router(state);

    let addr = format!("{}:{}", args.host, args.port);
    info!("pmproxy starting on http://{}", addr);
    info!("  Routes:");
    info!("    /health   → Health check (no auth)");
    info!("    /clob/*   → https://clob.polymarket.com/*");
    info!("    /gamma/*  → https://gamma-api.polymarket.com/*");
    info!("    /chain/*  → https://polygon-rpc.com");
    if config.auth_enabled {
        info!("  Authentication: ENABLED (Cognito JWT)");
        info!("    Region: {}", config.cognito_region);
        info!("    Pool ID: {}", config.cognito_pool_id);
        info!("    Rate limits:");
        info!("      Free: 60 rpm, burst 10");
        info!("      Pro: 300 rpm, burst 50");
        info!("      Enterprise: 1000 rpm, burst 100");
    } else {
        info!("  Authentication: DISABLED");
    }

    let listener = tokio::net::TcpListener::bind(&addr).await?;
    axum::serve(listener, app).await?;

    Ok(())
}
