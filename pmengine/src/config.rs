//! Configuration loaded from environment variables.

use std::env;

/// Engine configuration loaded from environment.
#[derive(Debug, Clone)]
pub struct Config {
    /// Ethereum private key for signing orders (hex, with or without 0x prefix)
    pub private_key: String,
    /// Funder address (the proxy wallet that holds funds)
    pub funder_address: Option<String>,
    /// CLOB API base URL
    pub clob_url: String,
    /// Maximum position size per market (in USDC)
    pub max_position_size: f64,
    /// Maximum total exposure (in USDC)
    pub max_total_exposure: f64,
    /// Strategy tick interval in milliseconds
    pub tick_interval_ms: u64,
    /// Signature type (0=EOA, 1=PolyProxy, 2=GnosisSafe)
    pub signature_type: u8,
    /// On engine startup, cancel any pre-existing user orders on the
    /// strategies' subscribed tokens. Treat the engine as the sole order
    /// manager for those markets. Default true.
    pub reconcile_on_startup: bool,
}

impl Config {
    /// Load configuration from environment variables.
    pub fn from_env() -> Result<Self, ConfigError> {
        let private_key = env::var("PMENGINE_PRIVATE_KEY")
            .or_else(|_| env::var("PM_PRIVATE_KEY"))
            .or_else(|_| env::var("PRIVATE_KEY"))
            .map_err(|_| ConfigError::MissingVar("PMENGINE_PRIVATE_KEY or PM_PRIVATE_KEY"))?;

        let funder_address = env::var("PMENGINE_FUNDER_ADDRESS")
            .or_else(|_| env::var("PM_FUNDER_ADDRESS"))
            .ok();

        // PMPROXY_URL routes /clob/* to clob.polymarket.com/*
        // SDK concatenates paths without separator, so we need trailing slash
        let clob_url = env::var("PMENGINE_CLOB_URL")
            .or_else(|_| env::var("PMPROXY_URL").map(|u| format!("{}/clob/", u.trim_end_matches('/'))))
            .unwrap_or_else(|_| "https://clob.polymarket.com/".to_string());

        let max_position_size = env::var("PMENGINE_MAX_POSITION_SIZE")
            .unwrap_or_else(|_| "50".to_string())
            .parse()
            .map_err(|_| ConfigError::InvalidValue("PMENGINE_MAX_POSITION_SIZE"))?;

        let max_total_exposure = env::var("PMENGINE_MAX_TOTAL_EXPOSURE")
            .unwrap_or_else(|_| "50".to_string())
            .parse()
            .map_err(|_| ConfigError::InvalidValue("PMENGINE_MAX_TOTAL_EXPOSURE"))?;

        let tick_interval_ms = env::var("PMENGINE_TICK_INTERVAL_MS")
            .unwrap_or_else(|_| "1000".to_string())
            .parse()
            .map_err(|_| ConfigError::InvalidValue("PMENGINE_TICK_INTERVAL_MS"))?;

        let signature_type = env::var("PM_SIGNATURE_TYPE")
            .or_else(|_| env::var("PMENGINE_SIGNATURE_TYPE"))
            .unwrap_or_else(|_| "0".to_string())
            .parse()
            .unwrap_or(0);

        let reconcile_on_startup = env::var("PMENGINE_RECONCILE_ON_STARTUP")
            .map(|v| !matches!(v.to_lowercase().as_str(), "false" | "0" | "no"))
            .unwrap_or(true);

        Ok(Self {
            private_key,
            funder_address,
            clob_url,
            max_position_size,
            max_total_exposure,
            tick_interval_ms,
            signature_type,
            reconcile_on_startup,
        })
    }

}

/// Above this, an engine loop interval is too coarse for a strategy whose own
/// cadence is sub-second — the OUTER loop is the real ceiling, and it defaults
/// to 1000ms against `updown`'s declared 50ms. See docs/LESSONS.md#L21.
pub const SLOW_TICK_WARN_MS: u64 = 250;

/// Warning text when the engine's loop interval throttles a strategy that
/// asked for a much faster one. `None` when the pairing is fine.
///
/// Diagnostic only — no clamping, no refusal: the operator's env var stays
/// the law, this just stops the mismatch from being invisible.
pub fn slow_tick_warning(strategy: &str, declared_ms: u64, engine_ms: u64) -> Option<String> {
    if engine_ms <= SLOW_TICK_WARN_MS || declared_ms >= engine_ms {
        return None;
    }
    Some(format!(
        "strategy '{}' asks for a {}ms tick but the engine loop runs at {}ms \
         (PMENGINE_TICK_INTERVAL_MS) — its latency model assumes the faster rate; \
         set PMENGINE_TICK_INTERVAL_MS<={} or launch via `pmt engine start`",
        strategy, declared_ms, engine_ms, SLOW_TICK_WARN_MS
    ))
}

#[derive(Debug)]
pub enum ConfigError {
    MissingVar(&'static str),
    InvalidValue(&'static str),
}

impl std::fmt::Display for ConfigError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            ConfigError::MissingVar(var) => write!(f, "Missing environment variable: {}", var),
            ConfigError::InvalidValue(var) => write!(f, "Invalid value for: {}", var),
        }
    }
}

impl std::error::Error for ConfigError {}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn slow_tick_warning_fires_on_the_binary_default_for_updown() {
        // The exact live footgun: `pmengine run updown` with no env var.
        let w = slow_tick_warning("updown", 50, 1000).expect("1000ms throttles a 50ms strategy");
        assert!(w.contains("updown") && w.contains("50ms") && w.contains("1000ms"), "{w}");
    }

    #[test]
    fn slow_tick_warning_silent_when_the_loop_keeps_up() {
        assert!(slow_tick_warning("updown", 50, 50).is_none(), "what the CLI passes");
        assert!(slow_tick_warning("updown", 50, 250).is_none(), "at the threshold, not over it");
    }

    #[test]
    fn slow_tick_warning_silent_for_strategies_that_want_a_slow_tick() {
        // A strategy declaring 5s is not throttled by a 1s loop.
        assert!(slow_tick_warning("slow_quoter", 5000, 1000).is_none());
        assert!(slow_tick_warning("slow_quoter", 1000, 1000).is_none());
    }
}
