use axum::{
    body::Body,
    extract::{Request, State},
    http::StatusCode,
    response::{IntoResponse, Response},
    Router,
};
use std::sync::Arc;
use tracing::{debug, error, info};

#[derive(Clone)]
pub struct ProxyState {
    pub client: reqwest::Client,
}

impl ProxyState {
    pub fn new() -> Result<Self, reqwest::Error> {
        let client = reqwest::Client::builder()
            .timeout(std::time::Duration::from_secs(30))
            .build()?;
        Ok(Self { client })
    }
}

impl Default for ProxyState {
    fn default() -> Self {
        Self::new().expect("Failed to create HTTP client")
    }
}

/// Build the proxy router with shared state
pub fn build_router(state: Arc<ProxyState>) -> Router {
    Router::new().fallback(proxy_handler).with_state(state)
}

/// Core proxy handler - forwards requests to upstream APIs
pub async fn proxy_handler(
    State(state): State<Arc<ProxyState>>,
    req: Request,
) -> impl IntoResponse {
    let uri = req.uri().clone();
    let method = req.method().clone();
    let headers = req.headers().clone();

    let path = uri.path();
    let query = uri.query().unwrap_or("");

    info!(
        "Proxying {} {} {}",
        method,
        path,
        if query.is_empty() { "" } else { query }
    );

    // Determine upstream based on path prefix
    // Handle both /clob and /clob/... patterns
    let (upstream_base, upstream_path) = if path == "/clob" {
        ("https://clob.polymarket.com", "")
    } else if let Some(rest) = path.strip_prefix("/clob/") {
        ("https://clob.polymarket.com", rest)
    } else if path == "/gamma" {
        ("https://gamma-api.polymarket.com", "")
    } else if let Some(rest) = path.strip_prefix("/gamma/") {
        ("https://gamma-api.polymarket.com", rest)
    } else if path == "/chain" {
        ("https://polygon-rpc.com", "")
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
    // Note: axum/hyper lowercases header names, but Polymarket's L1 auth
    // requires specific casing for POLY_* headers
    for (name, value) in headers.iter() {
        let name_str = name.as_str();
        if name_str == "host" {
            continue;
        }

        // Restore original casing for POLY_* headers
        let header_name = match name_str {
            "poly_address" => "POLY_ADDRESS",
            "poly_signature" => "POLY_SIGNATURE",
            "poly_timestamp" => "POLY_TIMESTAMP",
            "poly_nonce" => "POLY_NONCE",
            "poly_api_key" => "POLY_API_KEY",
            "poly_passphrase" => "POLY_PASSPHRASE",
            _ => name_str,
        };

        upstream_req = upstream_req.header(header_name, value);
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
            && name_str != "upgrade"
        {
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
