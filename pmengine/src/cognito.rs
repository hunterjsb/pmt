//! AWS Cognito authentication for pmproxy.
//!
//! This module provides token acquisition and caching for Cognito JWT tokens
//! used to authenticate with pmproxy when multi-tenant auth is enabled.
//!
//! # Feature
//!
//! This module requires the `cognito` feature to be enabled:
//!
//! ```toml
//! pmengine = { version = "0.1", features = ["cognito"] }
//! ```
//!
//! # Environment Variables
//!
//! - `PMPROXY_COGNITO_CLIENT_ID`: Cognito App Client ID
//! - `PMPROXY_USERNAME`: Cognito username
//! - `PMPROXY_PASSWORD`: Cognito password
//! - `PMPROXY_COGNITO_REGION`: AWS region (default: us-east-1)

use std::sync::Arc;
use std::time::{Duration, Instant};

use aws_sdk_cognitoidentityprovider::Client as CognitoClient;
use tokio::sync::RwLock;
use tracing::{debug, error, info};

/// Cached Cognito token with expiration.
#[derive(Debug, Clone)]
struct CachedToken {
    access_token: String,
    id_token: String,
    refresh_token: Option<String>,
    expires_at: Instant,
}

/// Cognito authentication client with token caching.
///
/// Acquires and caches JWT tokens from AWS Cognito using USER_PASSWORD_AUTH flow.
/// Tokens are automatically refreshed when they expire.
pub struct CognitoAuth {
    client: CognitoClient,
    client_id: String,
    username: String,
    password: String,
    token: RwLock<Option<CachedToken>>,
    /// Buffer time before expiry to refresh (5 minutes)
    refresh_buffer: Duration,
}

impl CognitoAuth {
    /// Create a new Cognito auth client from environment variables.
    ///
    /// Required environment variables:
    /// - `PMPROXY_COGNITO_CLIENT_ID`
    /// - `PMPROXY_USERNAME`
    /// - `PMPROXY_PASSWORD`
    ///
    /// Optional:
    /// - `PMPROXY_COGNITO_REGION` (default: us-east-1)
    pub async fn from_env() -> Result<Self, CognitoError> {
        let client_id = std::env::var("PMPROXY_COGNITO_CLIENT_ID")
            .map_err(|_| CognitoError::MissingConfig("PMPROXY_COGNITO_CLIENT_ID".to_string()))?;
        let username = std::env::var("PMPROXY_USERNAME")
            .map_err(|_| CognitoError::MissingConfig("PMPROXY_USERNAME".to_string()))?;
        let password = std::env::var("PMPROXY_PASSWORD")
            .map_err(|_| CognitoError::MissingConfig("PMPROXY_PASSWORD".to_string()))?;

        let region = std::env::var("PMPROXY_COGNITO_REGION").unwrap_or_else(|_| "us-east-1".to_string());

        let config = aws_config::defaults(aws_config::BehaviorVersion::latest())
            .region(aws_config::Region::new(region))
            .load()
            .await;

        let client = CognitoClient::new(&config);

        Ok(Self {
            client,
            client_id,
            username,
            password,
            token: RwLock::new(None),
            refresh_buffer: Duration::from_secs(300), // 5 minutes
        })
    }

    /// Create a new Cognito auth client with explicit configuration.
    pub async fn new(
        client_id: String,
        username: String,
        password: String,
        region: Option<String>,
    ) -> Result<Self, CognitoError> {
        let region = region.unwrap_or_else(|| "us-east-1".to_string());

        let config = aws_config::defaults(aws_config::BehaviorVersion::latest())
            .region(aws_config::Region::new(region))
            .load()
            .await;

        let client = CognitoClient::new(&config);

        Ok(Self {
            client,
            client_id,
            username,
            password,
            token: RwLock::new(None),
            refresh_buffer: Duration::from_secs(300),
        })
    }

    /// Check if the cached token is still valid.
    async fn is_token_valid(&self) -> bool {
        let token = self.token.read().await;
        if let Some(ref t) = *token {
            Instant::now() < t.expires_at - self.refresh_buffer
        } else {
            false
        }
    }

    /// Authenticate with Cognito using USER_PASSWORD_AUTH flow.
    async fn authenticate(&self) -> Result<CachedToken, CognitoError> {
        info!("Authenticating with Cognito...");

        let result = self
            .client
            .initiate_auth()
            .client_id(&self.client_id)
            .auth_flow(aws_sdk_cognitoidentityprovider::types::AuthFlowType::UserPasswordAuth)
            .auth_parameters("USERNAME", &self.username)
            .auth_parameters("PASSWORD", &self.password)
            .send()
            .await
            .map_err(|e| {
                error!(error = %e, "Cognito authentication failed");
                CognitoError::AuthFailed(e.to_string())
            })?;

        let auth_result = result.authentication_result().ok_or_else(|| {
            CognitoError::AuthFailed("Missing authentication result".to_string())
        })?;

        let access_token = auth_result
            .access_token()
            .ok_or_else(|| CognitoError::AuthFailed("Missing access token".to_string()))?
            .to_string();

        let id_token = auth_result
            .id_token()
            .ok_or_else(|| CognitoError::AuthFailed("Missing ID token".to_string()))?
            .to_string();

        let refresh_token = auth_result.refresh_token().map(String::from);

        let expires_in = auth_result.expires_in() as u64;

        debug!(expires_in = expires_in, "Cognito authentication successful");

        Ok(CachedToken {
            access_token,
            id_token,
            refresh_token,
            expires_at: Instant::now() + Duration::from_secs(expires_in),
        })
    }

    /// Refresh the token using the refresh token.
    async fn refresh_token(&self, refresh_token: &str) -> Result<CachedToken, CognitoError> {
        debug!("Refreshing Cognito token...");

        let result = self
            .client
            .initiate_auth()
            .client_id(&self.client_id)
            .auth_flow(aws_sdk_cognitoidentityprovider::types::AuthFlowType::RefreshTokenAuth)
            .auth_parameters("REFRESH_TOKEN", refresh_token)
            .send()
            .await;

        match result {
            Ok(resp) => {
                let auth_result = resp.authentication_result().ok_or_else(|| {
                    CognitoError::AuthFailed("Missing authentication result".to_string())
                })?;

                let access_token = auth_result
                    .access_token()
                    .ok_or_else(|| CognitoError::AuthFailed("Missing access token".to_string()))?
                    .to_string();

                let id_token = auth_result
                    .id_token()
                    .ok_or_else(|| CognitoError::AuthFailed("Missing ID token".to_string()))?
                    .to_string();

                let expires_in = auth_result.expires_in() as u64;

                debug!(expires_in = expires_in, "Token refresh successful");

                Ok(CachedToken {
                    access_token,
                    id_token,
                    refresh_token: Some(refresh_token.to_string()),
                    expires_at: Instant::now() + Duration::from_secs(expires_in),
                })
            }
            Err(_) => {
                // Refresh failed, fall back to full authentication
                debug!("Token refresh failed, re-authenticating...");
                self.authenticate().await
            }
        }
    }

    /// Get a valid access token, refreshing if necessary.
    pub async fn get_access_token(&self) -> Result<String, CognitoError> {
        if self.is_token_valid().await {
            let token = self.token.read().await;
            if let Some(ref t) = *token {
                return Ok(t.access_token.clone());
            }
        }

        // Token expired or missing, refresh/authenticate
        let new_token = {
            let current = self.token.read().await;
            if let Some(ref t) = *current {
                if let Some(ref refresh) = t.refresh_token {
                    self.refresh_token(refresh).await?
                } else {
                    self.authenticate().await?
                }
            } else {
                self.authenticate().await?
            }
        };

        let access_token = new_token.access_token.clone();
        *self.token.write().await = Some(new_token);

        Ok(access_token)
    }

    /// Get a valid ID token, refreshing if necessary.
    pub async fn get_id_token(&self) -> Result<String, CognitoError> {
        if self.is_token_valid().await {
            let token = self.token.read().await;
            if let Some(ref t) = *token {
                return Ok(t.id_token.clone());
            }
        }

        // Token expired or missing, refresh/authenticate
        let new_token = {
            let current = self.token.read().await;
            if let Some(ref t) = *current {
                if let Some(ref refresh) = t.refresh_token {
                    self.refresh_token(refresh).await?
                } else {
                    self.authenticate().await?
                }
            } else {
                self.authenticate().await?
            }
        };

        let id_token = new_token.id_token.clone();
        *self.token.write().await = Some(new_token);

        Ok(id_token)
    }

    /// Get the Authorization header value with Bearer token.
    pub async fn get_auth_header(&self) -> Result<String, CognitoError> {
        let token = self.get_access_token().await?;
        Ok(format!("Bearer {}", token))
    }

    /// Clear the cached token, forcing re-authentication on next request.
    pub async fn clear_cache(&self) {
        *self.token.write().await = None;
    }
}

/// Errors that can occur during Cognito authentication.
#[derive(Debug)]
pub enum CognitoError {
    /// Missing required configuration.
    MissingConfig(String),
    /// Authentication failed.
    AuthFailed(String),
}

impl std::fmt::Display for CognitoError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            CognitoError::MissingConfig(key) => write!(f, "Missing config: {}", key),
            CognitoError::AuthFailed(msg) => write!(f, "Auth failed: {}", msg),
        }
    }
}

impl std::error::Error for CognitoError {}

/// Create a CognitoAuth instance from environment variables, if available.
///
/// Returns None if required environment variables are not set.
pub async fn create_cognito_auth() -> Option<Arc<CognitoAuth>> {
    match CognitoAuth::from_env().await {
        Ok(auth) => Some(Arc::new(auth)),
        Err(e) => {
            debug!(error = %e, "Cognito auth not configured");
            None
        }
    }
}
