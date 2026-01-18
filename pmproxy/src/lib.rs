//! pmproxy - HTTP reverse proxy for Polymarket APIs with optional multi-tenant auth.
//!
//! When authentication is enabled (`PMPROXY_AUTH_ENABLED=true`), requests must include
//! a valid Cognito JWT in the Authorization header:
//!
//! ```text
//! Authorization: Bearer <token>
//! ```
//!
//! The proxy validates the JWT, extracts the tenant ID, applies rate limiting based on
//! the tenant's tier, and then forwards the request to the upstream Polymarket API.

pub mod auth;
pub mod config;
pub mod error;
pub mod ratelimit;

use std::sync::Arc;

use axum::{
    body::Body,
    extract::{Request, State},
    http::{header, StatusCode},
    response::{IntoResponse, Response},
    routing::get,
    Router,
};
use tracing::{debug, error, info};

use auth::{extract_bearer_token, AuthenticatedTenant, JwksCache};
use config::ProxyConfig;
use error::AuthError;
use ratelimit::TenantRateLimiter;

/// Shared proxy state.
#[derive(Clone)]
pub struct ProxyState {
    /// HTTP client for upstream requests.
    pub client: reqwest::Client,
    /// JWKS cache for JWT validation (None if auth disabled).
    pub jwks_cache: Option<Arc<JwksCache>>,
    /// Per-tenant rate limiter (None if auth disabled).
    pub rate_limiter: Option<Arc<TenantRateLimiter>>,
    /// Whether authentication is enabled.
    pub auth_enabled: bool,
}

impl ProxyState {
    /// Create new proxy state without authentication.
    pub fn new() -> Result<Self, reqwest::Error> {
        let client = reqwest::Client::builder()
            .timeout(std::time::Duration::from_secs(30))
            .build()?;
        Ok(Self {
            client,
            jwks_cache: None,
            rate_limiter: None,
            auth_enabled: false,
        })
    }

    /// Create new proxy state with authentication.
    pub fn with_auth(config: &ProxyConfig) -> Result<Self, reqwest::Error> {
        let client = reqwest::Client::builder()
            .timeout(std::time::Duration::from_secs(30))
            .build()?;

        if config.auth_enabled {
            Ok(Self {
                client,
                jwks_cache: Some(Arc::new(JwksCache::new(config))),
                rate_limiter: Some(Arc::new(TenantRateLimiter::new(config))),
                auth_enabled: true,
            })
        } else {
            Ok(Self {
                client,
                jwks_cache: None,
                rate_limiter: None,
                auth_enabled: false,
            })
        }
    }

    /// Pre-fetch JWKS if authentication is enabled.
    pub async fn prefetch_jwks(&self) -> Result<(), error::AuthError> {
        if let Some(ref cache) = self.jwks_cache {
            cache.prefetch().await?;
        }
        Ok(())
    }
}

impl Default for ProxyState {
    fn default() -> Self {
        Self::new().expect("Failed to create HTTP client")
    }
}

/// Build the proxy router with shared state.
pub fn build_router(state: Arc<ProxyState>) -> Router {
    Router::new()
        .route("/health", get(health_handler))
        .fallback(proxy_handler)
        .with_state(state)
}

/// Health check endpoint (no auth required).
pub async fn health_handler() -> impl IntoResponse {
    Response::builder()
        .status(StatusCode::OK)
        .header("Content-Type", "application/json")
        .body(Body::from(r#"{"status":"healthy"}"#))
        .unwrap()
}

/// Authenticate request if auth is enabled.
async fn authenticate(
    state: &ProxyState,
    auth_header: Option<&str>,
) -> Result<Option<AuthenticatedTenant>, AuthError> {
    if !state.auth_enabled {
        return Ok(None);
    }

    // Extract and validate token
    let token = extract_bearer_token(auth_header)?;

    let jwks_cache = state
        .jwks_cache
        .as_ref()
        .ok_or_else(|| AuthError::JwksFetchError("Auth enabled but JWKS cache not initialized".to_string()))?;

    let claims = jwks_cache.validate_token(token).await?;
    let tenant = AuthenticatedTenant::from(claims);

    // Check rate limit
    if let Some(ref limiter) = state.rate_limiter {
        limiter.check(&tenant.tenant_id, tenant.tier)?;
    }

    Ok(Some(tenant))
}

/// Core proxy handler - authenticates (if enabled) and forwards requests to upstream APIs.
pub async fn proxy_handler(
    State(state): State<Arc<ProxyState>>,
    req: Request,
) -> impl IntoResponse {
    let uri = req.uri().clone();
    let method = req.method().clone();
    let headers = req.headers().clone();

    let path = uri.path();
    let query = uri.query().unwrap_or("");

    // Authenticate if enabled
    let auth_header = headers
        .get(header::AUTHORIZATION)
        .and_then(|v| v.to_str().ok());

    let tenant = match authenticate(&state, auth_header).await {
        Ok(t) => t,
        Err(e) => {
            return e.into_response();
        }
    };

    // Log with tenant info if available
    if let Some(ref t) = tenant {
        info!(
            tenant_id = %t.tenant_id,
            tier = ?t.tier,
            method = %method,
            path = %path,
            "Proxying authenticated request"
        );
    } else {
        info!(
            method = %method,
            path = %path,
            query = %if query.is_empty() { "" } else { query },
            "Proxying request"
        );
    }

    // Determine upstream based on path prefix
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

    // Forward all headers except Host and Authorization (reqwest sets Host automatically,
    // and we don't forward our auth to upstream)
    for (name, value) in headers.iter() {
        let name_str = name.as_str();
        if name_str == "host" || name_str == "authorization" {
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

#[cfg(test)]
mod tests {
    use super::*;

    #[tokio::test]
    async fn test_health_handler() {
        let response = health_handler().await.into_response();
        assert_eq!(response.status(), StatusCode::OK);
    }

    #[test]
    fn test_proxy_state_default() {
        let state = ProxyState::default();
        assert!(!state.auth_enabled);
        assert!(state.jwks_cache.is_none());
        assert!(state.rate_limiter.is_none());
    }

    #[test]
    fn test_proxy_state_with_auth_disabled() {
        let config = ProxyConfig {
            auth_enabled: false,
            cognito_region: "us-east-1".to_string(),
            cognito_pool_id: "test".to_string(),
            cognito_client_id: None,
            rate_limit_rpm: 100,
            rate_limit_burst: 20,
        };

        let state = ProxyState::with_auth(&config).unwrap();
        assert!(!state.auth_enabled);
        assert!(state.jwks_cache.is_none());
        assert!(state.rate_limiter.is_none());
    }

    #[test]
    fn test_proxy_state_with_auth_enabled() {
        let config = ProxyConfig {
            auth_enabled: true,
            cognito_region: "us-east-1".to_string(),
            cognito_pool_id: "us-east-1_test123".to_string(),
            cognito_client_id: Some("client123".to_string()),
            rate_limit_rpm: 100,
            rate_limit_burst: 20,
        };

        let state = ProxyState::with_auth(&config).unwrap();
        assert!(state.auth_enabled);
        assert!(state.jwks_cache.is_some());
        assert!(state.rate_limiter.is_some());
    }
}
