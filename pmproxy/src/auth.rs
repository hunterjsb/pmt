//! JWT authentication for Cognito tokens.
//!
//! Handles JWKS fetching, caching, and JWT validation.

use std::collections::HashMap;
use std::time::{Duration, Instant};

use jsonwebtoken::{decode, decode_header, Algorithm, DecodingKey, Validation};
use serde::{Deserialize, Serialize};
use tokio::sync::RwLock;
use tracing::{debug, error, info, warn};

use crate::config::{ProxyConfig, TenantTier};
use crate::error::AuthError;

/// JWKS (JSON Web Key Set) response from Cognito.
#[derive(Debug, Deserialize)]
struct JwksResponse {
    keys: Vec<Jwk>,
}

/// Individual JSON Web Key.
#[derive(Debug, Clone, Deserialize)]
#[allow(dead_code)]
struct Jwk {
    kid: String,
    kty: String,
    alg: Option<String>,
    n: String,  // RSA modulus
    e: String,  // RSA exponent
    #[serde(rename = "use")]
    key_use: Option<String>,
}

/// Cached JWKS with TTL.
struct CachedJwks {
    keys: HashMap<String, DecodingKey>,
    fetched_at: Instant,
}

/// JWKS cache that fetches and caches keys from Cognito.
pub struct JwksCache {
    jwks_url: String,
    expected_issuer: String,
    client_id: Option<String>,
    cache: RwLock<Option<CachedJwks>>,
    http_client: reqwest::Client,
    /// Cache TTL (default: 1 hour).
    cache_ttl: Duration,
}

impl JwksCache {
    /// Create a new JWKS cache.
    pub fn new(config: &ProxyConfig) -> Self {
        Self {
            jwks_url: config.jwks_url(),
            expected_issuer: config.expected_issuer(),
            client_id: config.cognito_client_id.clone(),
            cache: RwLock::new(None),
            http_client: reqwest::Client::builder()
                .timeout(Duration::from_secs(10))
                .build()
                .expect("Failed to create HTTP client"),
            cache_ttl: Duration::from_secs(3600), // 1 hour
        }
    }

    /// Pre-fetch JWKS at startup.
    pub async fn prefetch(&self) -> Result<(), AuthError> {
        self.refresh_cache().await
    }

    /// Refresh the JWKS cache.
    async fn refresh_cache(&self) -> Result<(), AuthError> {
        info!(url = %self.jwks_url, "Fetching JWKS");

        let response = self
            .http_client
            .get(&self.jwks_url)
            .send()
            .await
            .map_err(|e| {
                error!(error = %e, "Failed to fetch JWKS");
                AuthError::JwksFetchError(e.to_string())
            })?;

        if !response.status().is_success() {
            let status = response.status();
            let body = response.text().await.unwrap_or_default();
            error!(status = %status, body = %body, "JWKS fetch failed");
            return Err(AuthError::JwksFetchError(format!(
                "HTTP {}: {}",
                status, body
            )));
        }

        let jwks: JwksResponse = response.json().await.map_err(|e| {
            error!(error = %e, "Failed to parse JWKS");
            AuthError::JwksFetchError(e.to_string())
        })?;

        let mut keys = HashMap::new();
        for jwk in jwks.keys {
            // Only process RSA keys
            if jwk.kty != "RSA" {
                continue;
            }

            match DecodingKey::from_rsa_components(&jwk.n, &jwk.e) {
                Ok(key) => {
                    debug!(kid = %jwk.kid, "Loaded RSA key");
                    keys.insert(jwk.kid.clone(), key);
                }
                Err(e) => {
                    warn!(kid = %jwk.kid, error = %e, "Failed to parse RSA key");
                }
            }
        }

        if keys.is_empty() {
            return Err(AuthError::JwksFetchError("No valid keys in JWKS".to_string()));
        }

        info!(key_count = keys.len(), "JWKS cache refreshed");

        let mut cache = self.cache.write().await;
        *cache = Some(CachedJwks {
            keys,
            fetched_at: Instant::now(),
        });

        Ok(())
    }

    /// Get a decoding key by key ID, refreshing cache if needed.
    async fn get_key(&self, kid: &str) -> Result<DecodingKey, AuthError> {
        // Check if cache is valid
        {
            let cache = self.cache.read().await;
            if let Some(ref cached) = *cache {
                if cached.fetched_at.elapsed() < self.cache_ttl {
                    if let Some(key) = cached.keys.get(kid) {
                        return Ok(key.clone());
                    }
                }
            }
        }

        // Cache miss or expired - refresh
        self.refresh_cache().await?;

        // Try again after refresh
        let cache = self.cache.read().await;
        if let Some(ref cached) = *cache {
            if let Some(key) = cached.keys.get(kid) {
                return Ok(key.clone());
            }
        }

        Err(AuthError::InvalidToken(format!(
            "Key ID '{}' not found in JWKS",
            kid
        )))
    }

    /// Validate a JWT and return the claims.
    pub async fn validate_token(&self, token: &str) -> Result<CognitoClaims, AuthError> {
        // Decode header to get kid
        let header = decode_header(token).map_err(|e| {
            debug!(error = %e, "Failed to decode JWT header");
            AuthError::InvalidToken(format!("Invalid JWT header: {}", e))
        })?;

        let kid = header.kid.ok_or_else(|| {
            debug!("JWT missing kid claim");
            AuthError::InvalidToken("Missing key ID in JWT header".to_string())
        })?;

        // Get the key
        let key = self.get_key(&kid).await?;

        // Set up validation
        let mut validation = Validation::new(Algorithm::RS256);
        validation.set_issuer(&[&self.expected_issuer]);
        validation.set_required_spec_claims(&["exp", "sub", "iss", "token_use"]);

        // Set audience if client_id is configured
        if let Some(ref client_id) = self.client_id {
            validation.set_audience(&[client_id]);
        } else {
            validation.validate_aud = false;
        }

        // Decode and validate
        let token_data = decode::<CognitoClaims>(token, &key, &validation).map_err(|e| {
            debug!(error = %e, "JWT validation failed");
            match e.kind() {
                jsonwebtoken::errors::ErrorKind::ExpiredSignature => AuthError::ExpiredToken,
                jsonwebtoken::errors::ErrorKind::InvalidIssuer => {
                    AuthError::InvalidToken("Invalid issuer".to_string())
                }
                jsonwebtoken::errors::ErrorKind::InvalidAudience => {
                    AuthError::InvalidToken("Invalid audience".to_string())
                }
                _ => AuthError::InvalidToken(e.to_string()),
            }
        })?;

        // Validate token_use
        if token_data.claims.token_use != "access" && token_data.claims.token_use != "id" {
            return Err(AuthError::InvalidToken(format!(
                "Invalid token_use: {}",
                token_data.claims.token_use
            )));
        }

        Ok(token_data.claims)
    }
}

/// Claims from a Cognito JWT.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct CognitoClaims {
    /// Subject - unique user identifier (tenant ID).
    pub sub: String,

    /// Expiration time (Unix timestamp).
    pub exp: u64,

    /// Issuer - Cognito User Pool URL.
    pub iss: String,

    /// Token use - "access" or "id".
    pub token_use: String,

    /// Optional: Client ID.
    #[serde(default)]
    pub client_id: Option<String>,

    /// Optional: Username.
    #[serde(default)]
    pub username: Option<String>,

    /// Custom claim: Tenant tier for rate limiting.
    #[serde(rename = "custom:tenant_tier", default)]
    pub tenant_tier: Option<String>,
}

impl CognitoClaims {
    /// Get the tenant ID (sub claim).
    pub fn tenant_id(&self) -> &str {
        &self.sub
    }

    /// Get the tenant tier, defaulting to Free.
    pub fn tier(&self) -> TenantTier {
        self.tenant_tier
            .as_ref()
            .map(|t| TenantTier::from_str(t))
            .unwrap_or_default()
    }
}

/// Authenticated tenant info extracted from JWT.
#[derive(Debug, Clone)]
pub struct AuthenticatedTenant {
    /// Tenant ID (from sub claim).
    pub tenant_id: String,
    /// Tenant tier for rate limiting.
    pub tier: TenantTier,
}

impl From<CognitoClaims> for AuthenticatedTenant {
    fn from(claims: CognitoClaims) -> Self {
        let tier = claims.tier();
        Self {
            tenant_id: claims.sub,
            tier,
        }
    }
}

/// Extract Bearer token from Authorization header.
pub fn extract_bearer_token(header_value: Option<&str>) -> Result<&str, AuthError> {
    let value = header_value.ok_or(AuthError::MissingToken)?;

    let token = value
        .strip_prefix("Bearer ")
        .or_else(|| value.strip_prefix("bearer "))
        .ok_or(AuthError::MissingToken)?;

    if token.is_empty() {
        return Err(AuthError::MissingToken);
    }

    Ok(token)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_extract_bearer_token() {
        assert_eq!(
            extract_bearer_token(Some("Bearer abc123")).unwrap(),
            "abc123"
        );
        assert_eq!(
            extract_bearer_token(Some("bearer abc123")).unwrap(),
            "abc123"
        );
        assert!(extract_bearer_token(None).is_err());
        assert!(extract_bearer_token(Some("")).is_err());
        assert!(extract_bearer_token(Some("Basic abc123")).is_err());
        assert!(extract_bearer_token(Some("Bearer ")).is_err());
    }

    #[test]
    fn test_cognito_claims_tier() {
        let claims = CognitoClaims {
            sub: "user-123".to_string(),
            exp: 0,
            iss: "https://cognito-idp.us-east-1.amazonaws.com/us-east-1_abc".to_string(),
            token_use: "access".to_string(),
            client_id: None,
            username: None,
            tenant_tier: Some("pro".to_string()),
        };
        assert_eq!(claims.tier(), TenantTier::Pro);

        let claims_no_tier = CognitoClaims {
            sub: "user-123".to_string(),
            exp: 0,
            iss: "https://cognito-idp.us-east-1.amazonaws.com/us-east-1_abc".to_string(),
            token_use: "access".to_string(),
            client_id: None,
            username: None,
            tenant_tier: None,
        };
        assert_eq!(claims_no_tier.tier(), TenantTier::Free);
    }
}
