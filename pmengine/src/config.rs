//! Configuration loaded from environment variables.

use std::env;

/// Engine configuration loaded from environment.
#[derive(Debug, Clone)]
pub struct Config {
    /// Ethereum private key for signing orders (hex, with or without 0x prefix)
    pub private_key: String,
    /// CLOB API base URL
    pub clob_url: String,
    /// WebSocket URL for market data
    pub ws_url: String,
    /// Maximum position size per market (in USDC)
    pub max_position_size: f64,
    /// Maximum total exposure (in USDC)
    pub max_total_exposure: f64,
    /// Strategy tick interval in milliseconds
    pub tick_interval_ms: u64,
    /// Log level
    pub log_level: String,
    /// Signature type (0=EOA, 1=PolyProxy, 2=GnosisSafe)
    pub signature_type: u8,
}

impl Config {
    /// Load configuration from environment variables.
    pub fn from_env() -> Result<Self, ConfigError> {
        let private_key = env::var("PMENGINE_PRIVATE_KEY")
            .or_else(|_| env::var("PRIVATE_KEY"))
            .map_err(|_| ConfigError::MissingVar("PMENGINE_PRIVATE_KEY or PRIVATE_KEY"))?;

        let clob_url = env::var("PMENGINE_CLOB_URL")
            .unwrap_or_else(|_| "https://clob.polymarket.com".to_string());

        let ws_url = env::var("PMENGINE_WS_URL")
            .unwrap_or_else(|_| "wss://ws-subscriptions-clob.polymarket.com/ws".to_string());

        let max_position_size = env::var("PMENGINE_MAX_POSITION_SIZE")
            .unwrap_or_else(|_| "1000".to_string())
            .parse()
            .map_err(|_| ConfigError::InvalidValue("PMENGINE_MAX_POSITION_SIZE"))?;

        let max_total_exposure = env::var("PMENGINE_MAX_TOTAL_EXPOSURE")
            .unwrap_or_else(|_| "5000".to_string())
            .parse()
            .map_err(|_| ConfigError::InvalidValue("PMENGINE_MAX_TOTAL_EXPOSURE"))?;

        let tick_interval_ms = env::var("PMENGINE_TICK_INTERVAL_MS")
            .unwrap_or_else(|_| "1000".to_string())
            .parse()
            .map_err(|_| ConfigError::InvalidValue("PMENGINE_TICK_INTERVAL_MS"))?;

        let log_level = env::var("PMENGINE_LOG_LEVEL")
            .or_else(|_| env::var("RUST_LOG"))
            .unwrap_or_else(|_| "info".to_string());

        let signature_type = env::var("PM_SIGNATURE_TYPE")
            .or_else(|_| env::var("PMENGINE_SIGNATURE_TYPE"))
            .unwrap_or_else(|_| "0".to_string())
            .parse()
            .unwrap_or(0);

        Ok(Self {
            private_key,
            clob_url,
            ws_url,
            max_position_size,
            max_total_exposure,
            tick_interval_ms,
            log_level,
            signature_type,
        })
    }

    /// Normalize private key (strip 0x prefix if present)
    pub fn private_key_bytes(&self) -> Result<[u8; 32], ConfigError> {
        let key = self.private_key.strip_prefix("0x").unwrap_or(&self.private_key);
        let bytes = hex::decode(key).map_err(|_| ConfigError::InvalidValue("PRIVATE_KEY format"))?;
        bytes.try_into().map_err(|_| ConfigError::InvalidValue("PRIVATE_KEY length"))
    }
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
