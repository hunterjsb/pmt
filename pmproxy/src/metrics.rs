//! Lightweight Prometheus-format metrics.
//!
//! Hand-rolled atomic counters — no metrics-ecosystem dep so the Lambda
//! zip stays small. Exported at `/metrics` (no auth, intentionally:
//! a Prometheus scraper can't carry a Cognito JWT).
//!
//! Counter names follow Prometheus conventions: `pmproxy_<noun>_<unit>`
//! plus `_total` for monotonically-increasing counters.

use std::fmt::Write;
use std::sync::atomic::{AtomicU64, Ordering};

use dashmap::DashMap;

/// All counters live here, behind atomic ops so the proxy hot path
/// pays at most one fetch_add per increment.
pub struct Metrics {
    /// Per (route, status_code) request count. Route is a short label
    /// from `upstream::Route::label`, plus a few static literals for
    /// /health, /badge, /metrics, and "unknown".
    requests: DashMap<(&'static str, u16), AtomicU64>,
    /// Auth-failure count by reason ("missing_token", "invalid_token",
    /// "expired_token", "service_unavailable").
    auth_failures: DashMap<&'static str, AtomicU64>,
    rate_limit_drops: AtomicU64,
    /// JWKS refresh result ("ok" or "error").
    jwks_refreshes: DashMap<&'static str, AtomicU64>,
    /// Lifetime WS upgrade count (only meaningful with `ws` feature).
    ws_connections_total: AtomicU64,
    /// Currently-open WS connections.
    ws_connections_active: AtomicU64,
}

impl Metrics {
    pub fn new() -> Self {
        Self {
            requests: DashMap::new(),
            auth_failures: DashMap::new(),
            rate_limit_drops: AtomicU64::new(0),
            jwks_refreshes: DashMap::new(),
            ws_connections_total: AtomicU64::new(0),
            ws_connections_active: AtomicU64::new(0),
        }
    }

    pub fn record_request(&self, route: &'static str, status: u16) {
        self.requests
            .entry((route, status))
            .or_insert_with(|| AtomicU64::new(0))
            .fetch_add(1, Ordering::Relaxed);
    }

    pub fn record_auth_failure(&self, reason: &'static str) {
        self.auth_failures
            .entry(reason)
            .or_insert_with(|| AtomicU64::new(0))
            .fetch_add(1, Ordering::Relaxed);
    }

    pub fn record_rate_limit_drop(&self) {
        self.rate_limit_drops.fetch_add(1, Ordering::Relaxed);
    }

    pub fn record_jwks_refresh(&self, ok: bool) {
        let key = if ok { "ok" } else { "error" };
        self.jwks_refreshes
            .entry(key)
            .or_insert_with(|| AtomicU64::new(0))
            .fetch_add(1, Ordering::Relaxed);
    }

    pub fn ws_connect(&self) {
        self.ws_connections_total.fetch_add(1, Ordering::Relaxed);
        self.ws_connections_active.fetch_add(1, Ordering::Relaxed);
    }

    pub fn ws_disconnect(&self) {
        self.ws_connections_active.fetch_sub(1, Ordering::Relaxed);
    }

    /// Render as Prometheus text format 0.0.4.
    pub fn render(&self, tenant_count: usize) -> String {
        let mut out = String::with_capacity(512);

        writeln!(out, "# HELP pmproxy_requests_total Total requests by route and status code").unwrap();
        writeln!(out, "# TYPE pmproxy_requests_total counter").unwrap();
        for entry in self.requests.iter() {
            let ((route, status), counter) = (entry.key(), entry.value());
            writeln!(
                out,
                r#"pmproxy_requests_total{{route="{}",status="{}"}} {}"#,
                route, status, counter.load(Ordering::Relaxed),
            ).unwrap();
        }

        writeln!(out, "# HELP pmproxy_auth_failures_total Authentication failures by reason").unwrap();
        writeln!(out, "# TYPE pmproxy_auth_failures_total counter").unwrap();
        for entry in self.auth_failures.iter() {
            let (reason, counter) = (entry.key(), entry.value());
            writeln!(
                out,
                r#"pmproxy_auth_failures_total{{reason="{}"}} {}"#,
                reason, counter.load(Ordering::Relaxed),
            ).unwrap();
        }

        writeln!(out, "# HELP pmproxy_rate_limit_drops_total Requests rejected by rate limiter").unwrap();
        writeln!(out, "# TYPE pmproxy_rate_limit_drops_total counter").unwrap();
        writeln!(out, "pmproxy_rate_limit_drops_total {}", self.rate_limit_drops.load(Ordering::Relaxed)).unwrap();

        writeln!(out, "# HELP pmproxy_jwks_refreshes_total JWKS cache refreshes by result").unwrap();
        writeln!(out, "# TYPE pmproxy_jwks_refreshes_total counter").unwrap();
        for entry in self.jwks_refreshes.iter() {
            let (result, counter) = (entry.key(), entry.value());
            writeln!(
                out,
                r#"pmproxy_jwks_refreshes_total{{result="{}"}} {}"#,
                result, counter.load(Ordering::Relaxed),
            ).unwrap();
        }

        writeln!(out, "# HELP pmproxy_ws_connections_total Lifetime WebSocket upgrades").unwrap();
        writeln!(out, "# TYPE pmproxy_ws_connections_total counter").unwrap();
        writeln!(out, "pmproxy_ws_connections_total {}", self.ws_connections_total.load(Ordering::Relaxed)).unwrap();

        writeln!(out, "# HELP pmproxy_ws_connections_active Currently-open WebSocket connections").unwrap();
        writeln!(out, "# TYPE pmproxy_ws_connections_active gauge").unwrap();
        writeln!(out, "pmproxy_ws_connections_active {}", self.ws_connections_active.load(Ordering::Relaxed)).unwrap();

        writeln!(out, "# HELP pmproxy_tenants_active Active per-tenant rate-limiter buckets").unwrap();
        writeln!(out, "# TYPE pmproxy_tenants_active gauge").unwrap();
        writeln!(out, "pmproxy_tenants_active {}", tenant_count).unwrap();

        out
    }
}

impl Default for Metrics {
    fn default() -> Self {
        Self::new()
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn render_includes_help_and_type_lines() {
        let m = Metrics::new();
        m.record_request("clob", 200);
        m.record_request("clob", 200);
        m.record_request("gamma", 401);
        m.record_auth_failure("invalid_token");
        m.record_rate_limit_drop();
        m.record_jwks_refresh(true);

        let out = m.render(3);

        // Help + type lines for each metric family
        assert!(out.contains("# HELP pmproxy_requests_total"));
        assert!(out.contains("# TYPE pmproxy_requests_total counter"));
        // Counts present
        assert!(out.contains(r#"pmproxy_requests_total{route="clob",status="200"} 2"#));
        assert!(out.contains(r#"pmproxy_requests_total{route="gamma",status="401"} 1"#));
        assert!(out.contains(r#"pmproxy_auth_failures_total{reason="invalid_token"} 1"#));
        assert!(out.contains("pmproxy_rate_limit_drops_total 1"));
        assert!(out.contains(r#"pmproxy_jwks_refreshes_total{result="ok"} 1"#));
        assert!(out.contains("pmproxy_tenants_active 3"));
    }

    #[test]
    fn ws_connect_disconnect_pair() {
        let m = Metrics::new();
        m.ws_connect();
        m.ws_connect();
        m.ws_disconnect();
        let out = m.render(0);
        assert!(out.contains("pmproxy_ws_connections_total 2"));
        assert!(out.contains("pmproxy_ws_connections_active 1"));
    }
}
