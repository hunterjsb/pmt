//! Per-tenant rate limiting using token bucket algorithm.

use std::num::NonZeroU32;
use std::sync::Arc;

use dashmap::DashMap;
use governor::{
    clock::DefaultClock,
    state::{InMemoryState, NotKeyed},
    Quota, RateLimiter,
};
use tracing::debug;

use crate::config::{ProxyConfig, TenantTier};
use crate::error::AuthError;

/// Rate limiter state for a single tenant.
type TenantLimiter = RateLimiter<NotKeyed, InMemoryState, DefaultClock>;

/// Per-tenant rate limiter.
///
/// Each tenant gets their own token bucket based on their tier.
pub struct TenantRateLimiter {
    /// Map of tenant_id -> rate limiter.
    limiters: DashMap<String, Arc<TenantLimiter>>,
    /// Default config for fallback limits.
    #[allow(dead_code)]
    config: ProxyConfig,
}

impl TenantRateLimiter {
    /// Create a new per-tenant rate limiter.
    pub fn new(config: &ProxyConfig) -> Self {
        Self {
            limiters: DashMap::new(),
            config: config.clone(),
        }
    }

    /// Get or create a rate limiter for a tenant.
    fn get_or_create(&self, tenant_id: &str, tier: TenantTier) -> Arc<TenantLimiter> {
        // Check if we already have a limiter for this tenant
        if let Some(limiter) = self.limiters.get(tenant_id) {
            return limiter.clone();
        }

        // Create a new limiter for this tenant
        let rpm = tier.requests_per_minute();
        let burst = tier.burst_size();

        // Convert to quota: rpm requests per 60 seconds
        // Use burst as the initial capacity
        let quota = Quota::per_minute(NonZeroU32::new(rpm).unwrap_or(NonZeroU32::new(1).unwrap()))
            .allow_burst(NonZeroU32::new(burst).unwrap_or(NonZeroU32::new(1).unwrap()));

        let limiter = Arc::new(RateLimiter::direct(quota));

        debug!(
            tenant_id = %tenant_id,
            tier = ?tier,
            rpm = rpm,
            burst = burst,
            "Created rate limiter for tenant"
        );

        // Insert and return (handle race condition by checking again)
        self.limiters
            .entry(tenant_id.to_string())
            .or_insert(limiter)
            .clone()
    }

    /// Check if a request should be allowed.
    ///
    /// Returns Ok(()) if allowed, Err(AuthError::RateLimited) if rejected.
    pub fn check(&self, tenant_id: &str, tier: TenantTier) -> Result<(), AuthError> {
        let limiter = self.get_or_create(tenant_id, tier);

        match limiter.check() {
            Ok(_) => {
                debug!(tenant_id = %tenant_id, "Rate limit check passed");
                Ok(())
            }
            Err(_) => {
                debug!(tenant_id = %tenant_id, tier = ?tier, "Rate limit exceeded");
                Err(AuthError::RateLimited)
            }
        }
    }

    /// Get the number of active tenant limiters (for monitoring).
    pub fn tenant_count(&self) -> usize {
        self.limiters.len()
    }

    /// Clean up stale limiters (tenants that haven't made requests in a while).
    ///
    /// This can be called periodically to prevent unbounded memory growth.
    /// In practice, governor's internal state is very lightweight.
    pub fn cleanup_stale(&self, max_tenants: usize) {
        if self.limiters.len() > max_tenants {
            // Simple strategy: remove half the entries
            // A more sophisticated approach would track last-access time
            let to_remove: Vec<String> = self
                .limiters
                .iter()
                .take(self.limiters.len() / 2)
                .map(|entry| entry.key().clone())
                .collect();

            for key in to_remove {
                self.limiters.remove(&key);
            }

            debug!(
                remaining = self.limiters.len(),
                "Cleaned up stale rate limiters"
            );
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_rate_limiter_creation() {
        let config = ProxyConfig {
            auth_enabled: true,
            cognito_region: "us-east-1".to_string(),
            cognito_pool_id: "test".to_string(),
            cognito_client_id: None,
            rate_limit_rpm: 100,
            rate_limit_burst: 20,
        };

        let limiter = TenantRateLimiter::new(&config);
        assert_eq!(limiter.tenant_count(), 0);
    }

    #[test]
    fn test_rate_limiter_allows_requests() {
        let config = ProxyConfig {
            auth_enabled: true,
            cognito_region: "us-east-1".to_string(),
            cognito_pool_id: "test".to_string(),
            cognito_client_id: None,
            rate_limit_rpm: 100,
            rate_limit_burst: 20,
        };

        let limiter = TenantRateLimiter::new(&config);

        // First request should always succeed
        assert!(limiter.check("tenant-1", TenantTier::Free).is_ok());
        assert_eq!(limiter.tenant_count(), 1);

        // Multiple tenants should get separate limiters
        assert!(limiter.check("tenant-2", TenantTier::Pro).is_ok());
        assert_eq!(limiter.tenant_count(), 2);
    }

    #[test]
    fn test_rate_limiter_burst() {
        let config = ProxyConfig {
            auth_enabled: true,
            cognito_region: "us-east-1".to_string(),
            cognito_pool_id: "test".to_string(),
            cognito_client_id: None,
            rate_limit_rpm: 60, // 1 per second
            rate_limit_burst: 5,
        };

        let limiter = TenantRateLimiter::new(&config);

        // Should allow burst of requests up to burst size
        // Note: The Free tier has burst of 10, so we test with that
        for i in 0..10 {
            assert!(
                limiter.check("burst-tenant", TenantTier::Free).is_ok(),
                "Request {} should succeed",
                i
            );
        }

        // After exhausting burst, subsequent requests should be rate limited
        // (assuming no time has passed to replenish tokens)
        assert!(limiter.check("burst-tenant", TenantTier::Free).is_err());
    }
}
