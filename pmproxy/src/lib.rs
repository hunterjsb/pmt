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
pub mod chain;
pub mod config;
pub mod error;
pub mod headers;
pub mod metrics;
pub mod ratelimit;
pub mod upstream;

#[cfg(feature = "ws")]
pub mod ws;

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
use metrics::Metrics;
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
    /// Counter store exported at /metrics. Always present; auth + WS code
    /// pokes it conditionally.
    pub metrics: Arc<Metrics>,
    /// Optional /chain/* JSON-RPC method allowlist (None = pass-through,
    /// the single-tenant default — see chain.rs for the threat model).
    pub chain_method_allowlist: Option<Arc<std::collections::HashSet<String>>>,
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
            metrics: Arc::new(Metrics::new()),
            chain_method_allowlist: None,
        })
    }

    /// Create new proxy state with authentication.
    pub fn with_auth(config: &ProxyConfig) -> Result<Self, reqwest::Error> {
        let client = reqwest::Client::builder()
            .timeout(std::time::Duration::from_secs(30))
            .build()?;

        let metrics = Arc::new(Metrics::new());
        let chain_method_allowlist = config.chain_method_allowlist.clone().map(Arc::new);
        if config.auth_enabled {
            Ok(Self {
                client,
                jwks_cache: Some(Arc::new(JwksCache::new_with_metrics(config, Some(metrics.clone())))),
                rate_limiter: Some(Arc::new(TenantRateLimiter::new(config))),
                auth_enabled: true,
                metrics,
                chain_method_allowlist,
            })
        } else {
            Ok(Self {
                client,
                jwks_cache: None,
                rate_limiter: None,
                auth_enabled: false,
                metrics,
                chain_method_allowlist,
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
    let router = Router::new()
        .route("/health", get(health_handler))
        .route("/badge", get(badge_handler))
        .route("/metrics", get(metrics_handler));

    #[cfg(feature = "ws")]
    let router = router.route("/clob/ws/{channel}", get(ws::ws_handler));

    router
        .fallback(proxy_handler)
        .with_state(state)
}

/// Health check endpoint (no auth required).
pub async fn health_handler(State(state): State<Arc<ProxyState>>) -> impl IntoResponse {
    state.metrics.record_request("health", 200);
    Response::builder()
        .status(StatusCode::OK)
        .header("Content-Type", "application/json")
        .body(Body::from(r#"{"status":"healthy"}"#))
        .unwrap()
}

/// Shields.io badge endpoint for server status.
pub async fn badge_handler(State(state): State<Arc<ProxyState>>) -> impl IntoResponse {
    state.metrics.record_request("badge", 200);
    Response::builder()
        .status(StatusCode::OK)
        .header("Content-Type", "application/json")
        .body(Body::from(
            r#"{"schemaVersion":1,"label":"pmproxy","message":"online","color":"brightgreen"}"#,
        ))
        .unwrap()
}

/// Prometheus-format metrics endpoint (no auth — scrapers can't carry a JWT).
pub async fn metrics_handler(State(state): State<Arc<ProxyState>>) -> impl IntoResponse {
    let tenants = state
        .rate_limiter
        .as_ref()
        .map(|r| r.tenant_count())
        .unwrap_or(0);
    let body = state.metrics.render(tenants);
    state.metrics.record_request("metrics", 200);
    Response::builder()
        .status(StatusCode::OK)
        // Prometheus exposition content type per the spec
        .header("Content-Type", "text/plain; version=0.0.4; charset=utf-8")
        .body(Body::from(body))
        .unwrap()
}

/// Authenticate request if auth is enabled.
pub async fn authenticate(
    state: &ProxyState,
    auth_header: Option<&str>,
) -> Result<Option<AuthenticatedTenant>, AuthError> {
    if !state.auth_enabled {
        return Ok(None);
    }

    // Extract and validate token
    let token = extract_bearer_token(auth_header).map_err(|e| {
        state.metrics.record_auth_failure("missing_token");
        e
    })?;

    let jwks_cache = state
        .jwks_cache
        .as_ref()
        .ok_or_else(|| {
            state.metrics.record_auth_failure("service_unavailable");
            AuthError::JwksFetchError("Auth enabled but JWKS cache not initialized".to_string())
        })?;

    let claims = jwks_cache.validate_token(token).await.map_err(|e| {
        let reason = match &e {
            AuthError::ExpiredToken => "expired_token",
            AuthError::InvalidToken(_) => "invalid_token",
            AuthError::JwksFetchError(_) => "service_unavailable",
            _ => "other",
        };
        state.metrics.record_auth_failure(reason);
        e
    })?;
    let tenant = AuthenticatedTenant::from(claims);

    // Check rate limit
    if let Some(ref limiter) = state.rate_limiter {
        limiter.check(&tenant.tenant_id, tenant.tier).map_err(|e| {
            state.metrics.record_rate_limit_drop();
            e
        })?;
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
    let Some(route) = upstream::route(path) else {
        error!("Unknown path prefix: {}", path);
        state.metrics.record_request("unknown", 404);
        return Response::builder()
            .status(StatusCode::NOT_FOUND)
            .body(Body::from("Not found"))
            .unwrap();
    };
    let route_label = route.label;

    let upstream_url = if query.is_empty() {
        format!("{}/{}", route.upstream_base, route.upstream_path)
    } else {
        format!("{}/{}?{}", route.upstream_base, route.upstream_path, query)
    };

    debug!(upstream = %upstream_url, route = route_label, "Forwarding");

    let body = match axum::body::to_bytes(req.into_body(), usize::MAX).await {
        Ok(b) => b,
        Err(e) => {
            error!("Failed to read request body: {}", e);
            state.metrics.record_request(route_label, 400);
            return Response::builder()
                .status(StatusCode::BAD_REQUEST)
                .body(Body::from("Bad request"))
                .unwrap();
        }
    };

    // /chain/* JSON-RPC method allowlist enforcement (opt-in via
    // PMPROXY_CHAIN_METHOD_ALLOWLIST). When unset, this branch is skipped
    // entirely — solo-tenant pass-through pays no overhead.
    if route_label == "chain" {
        if let Some(ref allowlist) = state.chain_method_allowlist {
            match chain::validate(&body, allowlist) {
                chain::AllowDecision::Allow => {}
                chain::AllowDecision::Deny(method) => {
                    info!(method = %method, "Chain method not in allowlist");
                    state.metrics.record_request(route_label, 403);
                    return Response::builder()
                        .status(StatusCode::FORBIDDEN)
                        .header("Content-Type", "application/json")
                        .body(Body::from(format!(
                            r#"{{"error":"method_not_allowed","method":"{}"}}"#,
                            method
                        )))
                        .unwrap();
                }
                chain::AllowDecision::Malformed => {
                    state.metrics.record_request(route_label, 400);
                    return Response::builder()
                        .status(StatusCode::BAD_REQUEST)
                        .header("Content-Type", "application/json")
                        .body(Body::from(r#"{"error":"malformed_jsonrpc"}"#))
                        .unwrap();
                }
            }
        }
    }

    let mut upstream_req = headers::forward_request_headers(
        state.client.request(method.clone(), &upstream_url),
        &headers,
    );
    if !body.is_empty() {
        upstream_req = upstream_req.body(body);
    }

    let upstream_resp = match upstream_req.send().await {
        Ok(r) => r,
        Err(e) => {
            error!("Upstream request failed: {}", e);
            state.metrics.record_request(route_label, 502);
            return Response::builder()
                .status(StatusCode::BAD_GATEWAY)
                .body(Body::from(format!("Upstream error: {}", e)))
                .unwrap();
        }
    };

    let status = upstream_resp.status();
    debug!(status = %status, "Upstream responded");
    state.metrics.record_request(route_label, status.as_u16());

    let response = headers::forward_response_headers(
        Response::builder().status(status),
        upstream_resp.headers(),
    );

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
        let state = Arc::new(ProxyState::default());
        let response = health_handler(State(state)).await.into_response();
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
            chain_method_allowlist: None,
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
            chain_method_allowlist: None,
        };

        let state = ProxyState::with_auth(&config).unwrap();
        assert!(state.auth_enabled);
        assert!(state.jwks_cache.is_some());
        assert!(state.rate_limiter.is_some());
    }
}
