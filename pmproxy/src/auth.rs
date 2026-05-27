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
use crate::metrics::Metrics;
use std::sync::Arc;

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
    metrics: Option<Arc<Metrics>>,
}

impl JwksCache {
    /// Create a new JWKS cache.
    pub fn new(config: &ProxyConfig) -> Self {
        Self::new_with_metrics(config, None)
    }

    /// Create with a metrics sink — refresh outcomes get recorded.
    pub fn new_with_metrics(config: &ProxyConfig, metrics: Option<Arc<Metrics>>) -> Self {
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
            metrics,
        }
    }

    fn record_refresh(&self, ok: bool) {
        if let Some(ref m) = self.metrics {
            m.record_jwks_refresh(ok);
        }
    }

    /// Test-only: pre-seed the cache with a (kid → DecodingKey) entry,
    /// bypassing the JWKS fetch. Lets unit tests exercise validate_token
    /// without standing up a fake Cognito.
    #[cfg(test)]
    pub(crate) async fn seed_for_test(&self, kid: &str, key: DecodingKey) {
        let mut cache = self.cache.write().await;
        let mut keys = HashMap::new();
        keys.insert(kid.to_string(), key);
        *cache = Some(CachedJwks {
            keys,
            fetched_at: Instant::now(),
        });
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
                self.record_refresh(false);
                AuthError::JwksFetchError(e.to_string())
            })?;

        if !response.status().is_success() {
            let status = response.status();
            let body = response.text().await.unwrap_or_default();
            error!(status = %status, body = %body, "JWKS fetch failed");
            self.record_refresh(false);
            return Err(AuthError::JwksFetchError(format!(
                "HTTP {}: {}",
                status, body
            )));
        }

        let jwks: JwksResponse = response.json().await.map_err(|e| {
            error!(error = %e, "Failed to parse JWKS");
            self.record_refresh(false);
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
            self.record_refresh(false);
            return Err(AuthError::JwksFetchError("No valid keys in JWKS".to_string()));
        }

        info!(key_count = keys.len(), "JWKS cache refreshed");

        let mut cache = self.cache.write().await;
        *cache = Some(CachedJwks {
            keys,
            fetched_at: Instant::now(),
        });
        self.record_refresh(true);

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
    /// Subject — unique user identifier (used as tenant ID).
    pub sub: String,

    /// Expiration time (Unix timestamp).
    pub exp: u64,

    /// Issuer — Cognito User Pool URL.
    pub iss: String,

    /// "access" or "id".
    pub token_use: String,

    /// Custom claim: tenant tier for rate limiting.
    #[serde(rename = "custom:tenant_tier", default)]
    pub tenant_tier: Option<String>,
}

impl CognitoClaims {
    /// Tenant tier, defaulting to Free when absent.
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
            tenant_tier: Some("pro".to_string()),
        };
        assert_eq!(claims.tier(), TenantTier::Pro);

        let claims_no_tier = CognitoClaims {
            tenant_tier: None,
            ..claims
        };
        assert_eq!(claims_no_tier.tier(), TenantTier::Free);
    }
}

#[cfg(test)]
mod jwt_validation_tests {
    //! End-to-end JWT validation tests using a baked-in test RSA keypair.
    //!
    //! Tests sign JWTs with the test private key, pre-seed the JwksCache
    //! with the corresponding public key, then call validate_token and
    //! assert on the AuthError variant. Covers the failure paths that
    //! tripped over the original happy-path-only test coverage.
    use super::*;
    use jsonwebtoken::{encode, EncodingKey, Header};
    use serde_json::json;
    use std::time::{SystemTime, UNIX_EPOCH};

    // Test-only RSA-2048 keypair. NOT used in production — only for
    // unit tests of validate_token's failure modes.
    const TEST_PRIV_PEM: &str = "-----BEGIN PRIVATE KEY-----
MIIEvAIBADANBgkqhkiG9w0BAQEFAASCBKYwggSiAgEAAoIBAQC4n+iatQBK3sei
MXi821NMWVl5aMOYgiCNeUADliKNM3iAb7BRpeTHlzQg+OfhQBlgUhUpmKQSYCmT
1WM/nWBfj5ZXJOL0kW6SX1vK0fPiNELNvLe3ckhRfNxriON2Ghqo9GLv6e31mJ3X
Y8+86d25L/muoWaGRjMrk1/mCxUp4e0b4IYUxnMJZYutlHT2XtRTgOwzHXtssCPo
NnFifKpvvdGASIteMU/OYTKg5rcBYUWw0hm51qQ8tNqBO8nQOCW55lRj75f6sKuK
k9QbpojGRIeuzTPzFXRh/1R/rczVOfbSPf+6wpyuVz9TAMx6PjI/hUmoYJdLDwTP
cJUSbMIhAgMBAAECggEAAJqfkTiZNkKvlXUUbpVjI7oenPlm7MxLReGrVW8/ugEX
t5nikkIXOVKIUA5Rz0Bd9XKEoaB4H9IAnJvZmPlG9Dnr8TxyD3UMqOgjw6razWsw
xYfIHqUUoTTPdEkiRcoGfpVbtSbxWJRclv2S+KwkGx3iw9coNjwjrmaNApcTScov
19C2serKuFVLSgIYmRcXUENdaq2oA1S+V/lF1IRynbr/dBHpEAd7EEEeWhC0Te7y
vxpPwMbi3WZP7iXiGSKUfuPUd5rN1n0GedTaF7g2vz2Zt/IWdjEmQ3np1WlSDifm
xGtEhYb+zuwCdfnE9luXJ5455kp2RyW2MgK6t0Gm1QKBgQDoOr0cPkcg7BgM2/9h
J2Hfklt1KrtXVlvd49OhUTP8VRS6vrKB8tpReXyN0pIkI8iQ44MhjkhB33sk++tE
esDIO4hecd0FrualUCDcSS2HieKd8iexM0TFliWSVtkjslvy6sbqzQG9gAR37Xnj
W0MyAs0ruassy1BEbCv8vQa1rwKBgQDLhb/z4hYoR47Xhteoddm41ytLKKj/fGzL
JCkbJInAWJ+W7+K8LIopkceereY9z63OO/fKUKqGji2K6JklNGWvvPJTIAA3eXNd
dRZ9z8K07QmFb/VbtEbJJ8RAm+vGL+1mFGs2atJMJOeoLm6vfTZ8yzCDSavK0kp1
5QXurOXJLwKBgHk9iVOAdBQNDnVQOeDX9bIKL/NYrtvm+yk581fqFBDtvlfMjVdo
mXAl09AbGi8B+4khLmnLZY/2g80INIjY6WLgKc7c9T4tVL8DuVQoZDu50fUR4oUR
thrNy6m967lGOdj1l4ooI3typWKTOapoEAnBCqqEUYieULaYHtLhQOqDAoGAQU00
/ee4/EuZhYX6hE7sAObpOUBemTsvHS8JEXBz0oedDS0DLyWLXzMrPbrGeWa9ecK8
Cuo/DNVpv3xKRym8xtp1Vj6aUzJg1cfP46ZZ7vtvZqU5sKbzX2+nBKQCzqBqJ6q9
i8RSnaPpwIjFcwFWDkyT0Ew/FuDKi3FkqeRIBnkCgYALdfVsqDc3qcNUhDgAGFlF
EbTZBwWHu1zN/DOXyjFzZNwn57ZKJqNdtcePgRdqc7/K680CafJRDhuKIvXl7kmq
DoDLEKf4julhkZ3tadHIlFJ4AE73WyQR6m/Of9IAsEDb00yb5AcnG9runyHiyQ/Q
H/RTRklk/NRqE60ISIcZCQ==
-----END PRIVATE KEY-----";

    // Matching public key components for DecodingKey::from_rsa_components.
    const TEST_PUB_N: &str = "uJ_omrUASt7HojF4vNtTTFlZeWjDmIIgjXlAA5YijTN4gG-wUaXkx5c0IPjn4UAZYFIVKZikEmApk9VjP51gX4-WVyTi9JFukl9bytHz4jRCzby3t3JIUXzca4jjdhoaqPRi7-nt9Zid12PPvOnduS_5rqFmhkYzK5Nf5gsVKeHtG-CGFMZzCWWLrZR09l7UU4DsMx17bLAj6DZxYnyqb73RgEiLXjFPzmEyoOa3AWFFsNIZudakPLTagTvJ0DglueZUY--X-rCripPUG6aIxkSHrs0z8xV0Yf9Uf63M1Tn20j3_usKcrlc_UwDMej4yP4VJqGCXSw8Ez3CVEmzCIQ";
    const TEST_PUB_E: &str = "AQAB";
    const TEST_KID: &str = "test-kid-1";
    const TEST_ISSUER: &str = "https://cognito-idp.us-east-1.amazonaws.com/us-east-1_test";

    fn now_secs() -> u64 {
        SystemTime::now().duration_since(UNIX_EPOCH).unwrap().as_secs()
    }

    /// Build a JwksCache pre-seeded with the test public key. `client_id`
    /// controls audience validation (None disables it).
    async fn seeded_cache(client_id: Option<&str>) -> JwksCache {
        let config = ProxyConfig {
            auth_enabled: true,
            cognito_region: "us-east-1".to_string(),
            cognito_pool_id: "us-east-1_test".to_string(),
            cognito_client_id: client_id.map(String::from),
            rate_limit_rpm: 60,
            rate_limit_burst: 10,
        };
        let cache = JwksCache::new(&config);
        let decoding_key = DecodingKey::from_rsa_components(TEST_PUB_N, TEST_PUB_E).unwrap();
        cache.seed_for_test(TEST_KID, decoding_key).await;
        cache
    }

    fn sign(claims: serde_json::Value, kid: Option<&str>) -> String {
        let mut header = Header::new(Algorithm::RS256);
        header.kid = kid.map(String::from);
        let enc = EncodingKey::from_rsa_pem(TEST_PRIV_PEM.as_bytes()).unwrap();
        encode(&header, &claims, &enc).unwrap()
    }

    #[tokio::test]
    async fn happy_path_access_token() {
        let cache = seeded_cache(None).await;
        let token = sign(
            json!({
                "sub": "user-1", "exp": now_secs() + 3600,
                "iss": TEST_ISSUER, "token_use": "access",
            }),
            Some(TEST_KID),
        );
        let claims = cache.validate_token(&token).await.unwrap();
        assert_eq!(claims.sub, "user-1");
    }

    #[tokio::test]
    async fn happy_path_id_token() {
        let cache = seeded_cache(None).await;
        let token = sign(
            json!({
                "sub": "user-1", "exp": now_secs() + 3600,
                "iss": TEST_ISSUER, "token_use": "id",
            }),
            Some(TEST_KID),
        );
        assert!(cache.validate_token(&token).await.is_ok());
    }

    #[tokio::test]
    async fn rejects_expired_token() {
        let cache = seeded_cache(None).await;
        // jsonwebtoken has a default 60s leeway, so "expired 60s ago" still
        // passes. Use a clear margin so the assertion isn't flaky.
        let token = sign(
            json!({
                "sub": "user-1", "exp": now_secs() - 3600,
                "iss": TEST_ISSUER, "token_use": "access",
            }),
            Some(TEST_KID),
        );
        assert!(matches!(
            cache.validate_token(&token).await,
            Err(AuthError::ExpiredToken)
        ));
    }

    #[tokio::test]
    async fn rejects_wrong_issuer() {
        let cache = seeded_cache(None).await;
        let token = sign(
            json!({
                "sub": "user-1", "exp": now_secs() + 3600,
                "iss": "https://attacker.example.com/", "token_use": "access",
            }),
            Some(TEST_KID),
        );
        let err = cache.validate_token(&token).await.unwrap_err();
        assert!(matches!(err, AuthError::InvalidToken(ref m) if m.contains("issuer")), "got {err:?}");
    }

    #[tokio::test]
    async fn rejects_wrong_audience() {
        let cache = seeded_cache(Some("expected-client-id")).await;
        let token = sign(
            json!({
                "sub": "user-1", "exp": now_secs() + 3600,
                "iss": TEST_ISSUER, "token_use": "access",
                "aud": "different-client-id",
            }),
            Some(TEST_KID),
        );
        let err = cache.validate_token(&token).await.unwrap_err();
        assert!(matches!(err, AuthError::InvalidToken(ref m) if m.contains("audience")), "got {err:?}");
    }

    #[tokio::test]
    async fn rejects_missing_kid() {
        let cache = seeded_cache(None).await;
        let token = sign(
            json!({
                "sub": "user-1", "exp": now_secs() + 3600,
                "iss": TEST_ISSUER, "token_use": "access",
            }),
            None,
        );
        let err = cache.validate_token(&token).await.unwrap_err();
        assert!(matches!(err, AuthError::InvalidToken(ref m) if m.contains("key ID") || m.contains("kid")), "got {err:?}");
    }

    #[tokio::test]
    async fn rejects_unknown_kid() {
        // Seeded with TEST_KID, ask for a token signed with a different kid.
        // refresh_cache will try to network-fetch and fail (no upstream),
        // bubbling up as JwksFetchError → caller renders 503.
        let cache = seeded_cache(None).await;
        let token = sign(
            json!({
                "sub": "user-1", "exp": now_secs() + 3600,
                "iss": TEST_ISSUER, "token_use": "access",
            }),
            Some("totally-different-kid"),
        );
        let err = cache.validate_token(&token).await.unwrap_err();
        assert!(matches!(err, AuthError::JwksFetchError(_)), "got {err:?}");
    }

    #[tokio::test]
    async fn rejects_wrong_token_use() {
        let cache = seeded_cache(None).await;
        let token = sign(
            json!({
                "sub": "user-1", "exp": now_secs() + 3600,
                "iss": TEST_ISSUER, "token_use": "refresh",
            }),
            Some(TEST_KID),
        );
        let err = cache.validate_token(&token).await.unwrap_err();
        assert!(matches!(err, AuthError::InvalidToken(ref m) if m.contains("token_use")), "got {err:?}");
    }

    #[tokio::test]
    async fn rejects_missing_required_claim() {
        let cache = seeded_cache(None).await;
        // No `exp` claim — required spec claim per Validation::set_required_spec_claims
        let token = sign(
            json!({
                "sub": "user-1", "iss": TEST_ISSUER, "token_use": "access",
            }),
            Some(TEST_KID),
        );
        assert!(cache.validate_token(&token).await.is_err());
    }

    #[tokio::test]
    async fn rejects_signature_mismatch() {
        // Sign with one private key, verify against a different public key.
        let cache = seeded_cache(None).await;

        // Construct a header + payload but craft an invalid signature by
        // tampering with the JWT.
        let token = sign(
            json!({
                "sub": "user-1", "exp": now_secs() + 3600,
                "iss": TEST_ISSUER, "token_use": "access",
            }),
            Some(TEST_KID),
        );
        // Flip a byte in the signature segment.
        let mut parts: Vec<&str> = token.split('.').collect();
        let mut sig_bytes = parts[2].as_bytes().to_vec();
        sig_bytes[0] = if sig_bytes[0] == b'a' { b'b' } else { b'a' };
        let tampered_sig = std::str::from_utf8(&sig_bytes).unwrap().to_string();
        parts[2] = &tampered_sig;
        let tampered = parts.join(".");

        let err = cache.validate_token(&tampered).await.unwrap_err();
        assert!(matches!(err, AuthError::InvalidToken(_)), "got {err:?}");
    }
}
