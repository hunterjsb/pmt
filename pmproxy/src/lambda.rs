use lambda_http::{run, tracing, Error};
use pmproxy::{build_router, config::ProxyConfig, ProxyState};
use std::sync::Arc;

#[tokio::main]
async fn main() -> Result<(), Error> {
    tracing::init_default_subscriber();

    // Load configuration from environment
    let config = ProxyConfig::from_env();

    // Create state with or without auth
    let state = Arc::new(ProxyState::with_auth(&config).map_err(|e| Error::from(e.to_string()))?);

    // Pre-fetch JWKS if auth is enabled
    if config.auth_enabled {
        tracing::info!(
            cognito_region = %config.cognito_region,
            cognito_pool_id = %config.cognito_pool_id,
            "Authentication enabled, fetching JWKS..."
        );

        if let Err(e) = state.prefetch_jwks().await {
            tracing::warn!(error = %e, "Failed to pre-fetch JWKS (will retry on first request)");
        }
    }

    let app = build_router(state);

    run(app).await
}
