//! Configuration for pmproxy authentication and rate limiting.
//!
//! All configuration is loaded from environment variables.

use std::env;

/// Tenant tier determines rate limits.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Default)]
pub enum TenantTier {
    #[default]
    Free,
    Pro,
    Enterprise,
}

impl TenantTier {
    /// Parse tier from string (case-insensitive).
    pub fn from_str(s: &str) -> Self {
        match s.to_lowercase().as_str() {
            "pro" => TenantTier::Pro,
            "enterprise" => TenantTier::Enterprise,
            _ => TenantTier::Free,
        }
    }

    /// Get requests per minute for this tier.
    pub fn requests_per_minute(&self) -> u32 {
        match self {
            TenantTier::Free => 60,
            TenantTier::Pro => 300,
            TenantTier::Enterprise => 1000,
        }
    }

    /// Get burst allowance for this tier.
    pub fn burst_size(&self) -> u32 {
        match self {
            TenantTier::Free => 10,
            TenantTier::Pro => 50,
            TenantTier::Enterprise => 100,
        }
    }
}

/// Proxy configuration loaded from environment.
#[derive(Debug, Clone)]
pub struct ProxyConfig {
    /// Whether authentication is enabled (feature flag for backward compat).
    pub auth_enabled: bool,

    /// AWS Cognito region (e.g., "us-east-1").
    pub cognito_region: String,

    /// Cognito User Pool ID (e.g., "us-east-1_xxxxxxxx").
    pub cognito_pool_id: String,

    /// Optional: Cognito App Client ID for audience validation.
    pub cognito_client_id: Option<String>,

    /// Default rate limit (requests per minute) for unknown tiers.
    pub rate_limit_rpm: u32,

    /// Default burst allowance for unknown tiers.
    pub rate_limit_burst: u32,
}

impl ProxyConfig {
    /// Load configuration from environment variables.
    pub fn from_env() -> Self {
        Self {
            auth_enabled: env::var("PMPROXY_AUTH_ENABLED")
                .map(|v| v.to_lowercase() == "true" || v == "1")
                .unwrap_or(false),
            cognito_region: env::var("PMPROXY_COGNITO_REGION")
                .unwrap_or_else(|_| "us-east-1".to_string()),
            cognito_pool_id: env::var("PMPROXY_COGNITO_POOL_ID").unwrap_or_default(),
            cognito_client_id: env::var("PMPROXY_COGNITO_APP_CLIENT_ID").ok(),
            rate_limit_rpm: env::var("PMPROXY_RATE_LIMIT_RPM")
                .ok()
                .and_then(|v| v.parse().ok())
                .unwrap_or(100),
            rate_limit_burst: env::var("PMPROXY_RATE_LIMIT_BURST")
                .ok()
                .and_then(|v| v.parse().ok())
                .unwrap_or(20),
        }
    }

    /// Get the JWKS URL for the configured Cognito User Pool.
    pub fn jwks_url(&self) -> String {
        format!(
            "https://cognito-idp.{}.amazonaws.com/{}/.well-known/jwks.json",
            self.cognito_region, self.cognito_pool_id
        )
    }

    /// Get the expected issuer for JWT validation.
    pub fn expected_issuer(&self) -> String {
        format!(
            "https://cognito-idp.{}.amazonaws.com/{}",
            self.cognito_region, self.cognito_pool_id
        )
    }
}

impl Default for ProxyConfig {
    fn default() -> Self {
        Self::from_env()
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_tenant_tier_from_str() {
        assert_eq!(TenantTier::from_str("free"), TenantTier::Free);
        assert_eq!(TenantTier::from_str("pro"), TenantTier::Pro);
        assert_eq!(TenantTier::from_str("PRO"), TenantTier::Pro);
        assert_eq!(TenantTier::from_str("enterprise"), TenantTier::Enterprise);
        assert_eq!(TenantTier::from_str("ENTERPRISE"), TenantTier::Enterprise);
        assert_eq!(TenantTier::from_str("unknown"), TenantTier::Free);
    }

    #[test]
    fn test_tenant_tier_limits() {
        assert_eq!(TenantTier::Free.requests_per_minute(), 60);
        assert_eq!(TenantTier::Pro.requests_per_minute(), 300);
        assert_eq!(TenantTier::Enterprise.requests_per_minute(), 1000);

        assert_eq!(TenantTier::Free.burst_size(), 10);
        assert_eq!(TenantTier::Pro.burst_size(), 50);
        assert_eq!(TenantTier::Enterprise.burst_size(), 100);
    }

    #[test]
    fn test_config_jwks_url() {
        let config = ProxyConfig {
            auth_enabled: true,
            cognito_region: "us-east-1".to_string(),
            cognito_pool_id: "us-east-1_abc123".to_string(),
            cognito_client_id: None,
            rate_limit_rpm: 100,
            rate_limit_burst: 20,
        };

        assert_eq!(
            config.jwks_url(),
            "https://cognito-idp.us-east-1.amazonaws.com/us-east-1_abc123/.well-known/jwks.json"
        );
        assert_eq!(
            config.expected_issuer(),
            "https://cognito-idp.us-east-1.amazonaws.com/us-east-1_abc123"
        );
    }
}
