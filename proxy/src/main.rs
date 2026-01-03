use axum::{
    body::Body,
    extract::{Request, State},
    http::{HeaderMap, StatusCode, Uri},
    response::{IntoResponse, Response},
    routing::any,
    Router,
};
use clap::Parser;
use std::sync::Arc;
use tracing::{debug, error, info, Level};
use tracing_subscriber::FmtSubscriber;

#[derive(Parser, Debug)]
#[command(name = "pmproxy", about = "Simple HTTP reverse proxy for Polymarket APIs")]
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

#[derive(Clone)]
struct ProxyState {
    client: reqwest::Client,
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

    // Create HTTP client for upstream requests
    let client = reqwest::Client::builder()
        .timeout(std::time::Duration::from_secs(30))
        .build()?;

    let state = Arc::new(ProxyState { client });

    // Build router - catch all paths and forward
    let app = Router::new()
        .fallback(proxy_handler)
        .with_state(state);

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

async fn proxy_handler(
    State(state): State<Arc<ProxyState>>,
    req: Request,
) -> impl IntoResponse {
    let uri = req.uri().clone();
    let method = req.method().clone();
    let headers = req.headers().clone();

    let path = uri.path();
    let query = uri.query().unwrap_or("");

    info!("Proxying {} {} {}", method, path, if query.is_empty() { "" } else { query });

    // Determine upstream based on path prefix
    let (upstream_base, upstream_path) = if let Some(rest) = path.strip_prefix("/clob/") {
        ("https://clob.polymarket.com", rest)
    } else if let Some(rest) = path.strip_prefix("/gamma/") {
        ("https://gamma-api.polymarket.com", rest)
    } else if let Some(rest) = path.strip_prefix("/chain/") {
        ("https://polygon-rpc.com", rest)
    } else {
        error!("Unknown path prefix: {}", path);
        return Response::builder()
            .status(StatusCode::NOT_FOUND)
            .body(Body::from("Not found"))
            .unwrap();
    };

    // Build upstream URL
    let upstream_url = if query.is_empty() {
        format!("{}/{}", upstream_base, upstream_path)
    } else {
        format!("{}/{}?{}", upstream_base, upstream_path, query)
    };

    debug!("Upstream URL: {}", upstream_url);

    // Read request body
    let body = match axum::body::to_bytes(req.into_body(), usize::MAX).await {
        Ok(b) => b,
        Err(e) => {
            error!("Failed to read request body: {}", e);
            return Response::builder()
                .status(StatusCode::BAD_REQUEST)
                .body(Body::from("Bad request"))
                .unwrap();
        }
    };

    let mut upstream_req = state.client.request(method.clone(), &upstream_url);

    // Forward all headers except Host (reqwest sets it automatically)
    for (name, value) in headers.iter() {
        if name != "host" {
            upstream_req = upstream_req.header(name, value);
        }
    }

    // Forward body if present
    if !body.is_empty() {
        upstream_req = upstream_req.body(body);
    }

    // Send request
    let upstream_resp = match upstream_req.send().await {
        Ok(r) => r,
        Err(e) => {
            error!("Upstream request failed: {}", e);
            return Response::builder()
                .status(StatusCode::BAD_GATEWAY)
                .body(Body::from(format!("Upstream error: {}", e)))
                .unwrap();
        }
    };

    // Build response
    let status = upstream_resp.status();
    debug!("Upstream status: {}", status);

    let mut response = Response::builder().status(status);

    // Forward response headers (skip hop-by-hop headers)
    for (name, value) in upstream_resp.headers().iter() {
        let name_str = name.as_str();
        // Skip hop-by-hop headers
        if name_str != "connection"
            && name_str != "transfer-encoding"
            && name_str != "keep-alive"
            && name_str != "proxy-authenticate"
            && name_str != "proxy-authorization"
            && name_str != "trailer"
            && name_str != "upgrade" {
            response = response.header(name, value);
        }
    }

    // Forward response body
    let body_bytes = match upstream_resp.bytes().await {
        Ok(b) => b,
        Err(e) => {
            error!("Failed to read upstream response: {}", e);
            return Response::builder()
                .status(StatusCode::BAD_GATEWAY)
                .body(Body::from("Failed to read response"))
                .unwrap();
        }
    };

    response.body(Body::from(body_bytes)).unwrap()
}
