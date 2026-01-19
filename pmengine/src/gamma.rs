//! Gamma API client for market discovery.
//!
//! The Gamma API provides market metadata including:
//! - Market questions and outcomes
//! - End dates for expiration tracking
//! - Token IDs for trading

use chrono::{DateTime, Utc};
use reqwest::Client;
use rust_decimal::Decimal;
use serde::Deserialize;
use std::str::FromStr;

/// Gamma API client for fetching market metadata.
pub struct GammaClient {
    client: Client,
    base_url: String,
}

/// Market data from Gamma API.
#[derive(Debug, Clone)]
pub struct GammaMarket {
    /// Market question text
    pub question: String,
    /// URL slug
    pub slug: String,
    /// Market end date (when it resolves)
    pub end_date: Option<DateTime<Utc>>,
    /// Outcome names (e.g., ["Yes", "No"])
    pub outcomes: Vec<String>,
    /// Current prices for each outcome
    pub outcome_prices: Vec<Decimal>,
    /// CLOB token IDs for each outcome
    pub clob_token_ids: Vec<String>,
    /// Whether market is active for trading
    pub active: bool,
    /// Whether market is closed
    pub closed: bool,
}

impl GammaMarket {
    /// Calculate hours until market expires.
    pub fn hours_until_expiry(&self) -> Option<f64> {
        self.end_date.map(|end| {
            let now = Utc::now();
            let duration = end.signed_duration_since(now);
            duration.num_seconds() as f64 / 3600.0
        })
    }

    /// Check if any outcome has high certainty (price >= threshold).
    pub fn has_high_certainty_outcome(&self, threshold: Decimal) -> bool {
        self.outcome_prices.iter().any(|p| *p >= threshold)
    }

    /// Get the index of the highest-priced outcome.
    pub fn highest_certainty_index(&self) -> Option<usize> {
        self.outcome_prices
            .iter()
            .enumerate()
            .max_by(|(_, a), (_, b)| a.cmp(b))
            .map(|(i, _)| i)
    }
}

/// Raw market response from Gamma API.
#[derive(Debug, Deserialize)]
struct RawGammaMarket {
    question: Option<String>,
    slug: Option<String>,
    #[serde(rename = "endDate")]
    end_date: Option<String>,
    outcomes: Option<String>,  // JSON-encoded array
    #[serde(rename = "outcomePrices")]
    outcome_prices: Option<String>,  // JSON-encoded array
    #[serde(rename = "clobTokenIds")]
    clob_token_ids: Option<String>,  // JSON-encoded array
    active: Option<bool>,
    closed: Option<bool>,
}

/// Error type for Gamma API operations.
#[derive(Debug)]
pub enum GammaError {
    /// HTTP request failed
    RequestError(String),
    /// JSON parsing failed
    ParseError(String),
    /// Invalid response data
    InvalidData(String),
}

impl std::fmt::Display for GammaError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            GammaError::RequestError(e) => write!(f, "Request error: {}", e),
            GammaError::ParseError(e) => write!(f, "Parse error: {}", e),
            GammaError::InvalidData(e) => write!(f, "Invalid data: {}", e),
        }
    }
}

impl std::error::Error for GammaError {}

impl GammaClient {
    /// Create a new Gamma client with default base URL.
    pub fn new() -> Self {
        Self {
            client: Client::new(),
            base_url: "https://gamma-api.polymarket.com".to_string(),
        }
    }

    /// Create a new Gamma client with custom base URL.
    pub fn with_base_url(base_url: &str) -> Self {
        Self {
            client: Client::new(),
            base_url: base_url.to_string(),
        }
    }

    /// Fetch all active markets from Gamma API.
    pub async fn fetch_markets(&self) -> Result<Vec<GammaMarket>, GammaError> {
        let url = format!("{}/markets?closed=false&limit=500", self.base_url);

        let response = self
            .client
            .get(&url)
            .send()
            .await
            .map_err(|e| GammaError::RequestError(e.to_string()))?;

        if !response.status().is_success() {
            return Err(GammaError::RequestError(format!(
                "HTTP {}: {}",
                response.status(),
                response.status().canonical_reason().unwrap_or("Unknown")
            )));
        }

        let raw_markets: Vec<RawGammaMarket> = response
            .json()
            .await
            .map_err(|e| GammaError::ParseError(e.to_string()))?;

        let markets = raw_markets
            .into_iter()
            .filter_map(|raw| self.parse_market(raw).ok())
            .collect();

        Ok(markets)
    }

    /// Fetch markets that are expiring soon with high certainty outcomes.
    pub async fn fetch_sure_bet_candidates(
        &self,
        max_hours_to_expiry: f64,
        min_certainty: Decimal,
    ) -> Result<Vec<GammaMarket>, GammaError> {
        let markets = self.fetch_markets().await?;

        let candidates: Vec<GammaMarket> = markets
            .into_iter()
            .filter(|m| m.active && !m.closed)
            .filter(|m| {
                m.hours_until_expiry()
                    .map(|h| h > 0.0 && h < max_hours_to_expiry)
                    .unwrap_or(false)
            })
            .filter(|m| m.has_high_certainty_outcome(min_certainty))
            .collect();

        Ok(candidates)
    }

    /// Parse a raw market response into structured data.
    fn parse_market(&self, raw: RawGammaMarket) -> Result<GammaMarket, GammaError> {
        // Parse outcomes from JSON string
        let outcomes: Vec<String> = raw
            .outcomes
            .as_ref()
            .and_then(|s| serde_json::from_str(s).ok())
            .unwrap_or_default();

        // Parse outcome prices from JSON string
        let outcome_prices: Vec<Decimal> = raw
            .outcome_prices
            .as_ref()
            .and_then(|s| {
                let strings: Vec<String> = serde_json::from_str(s).ok()?;
                strings
                    .iter()
                    .map(|p| Decimal::from_str(p).ok())
                    .collect::<Option<Vec<_>>>()
            })
            .unwrap_or_default();

        // Parse CLOB token IDs from JSON string
        let clob_token_ids: Vec<String> = raw
            .clob_token_ids
            .as_ref()
            .and_then(|s| serde_json::from_str(s).ok())
            .unwrap_or_default();

        // Parse end date
        let end_date = raw.end_date.as_ref().and_then(|s| {
            DateTime::parse_from_rfc3339(s)
                .ok()
                .map(|dt| dt.with_timezone(&Utc))
        });

        Ok(GammaMarket {
            question: raw.question.unwrap_or_default(),
            slug: raw.slug.unwrap_or_default(),
            end_date,
            outcomes,
            outcome_prices,
            clob_token_ids,
            active: raw.active.unwrap_or(false),
            closed: raw.closed.unwrap_or(true),
        })
    }
}

impl Default for GammaClient {
    fn default() -> Self {
        Self::new()
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use rust_decimal_macros::dec;

    #[test]
    fn test_hours_until_expiry() {
        let market = GammaMarket {
            question: "Test?".to_string(),
            slug: "test".to_string(),
            end_date: Some(Utc::now() + chrono::Duration::hours(2)),
            outcomes: vec!["Yes".to_string(), "No".to_string()],
            outcome_prices: vec![dec!(0.95), dec!(0.05)],
            clob_token_ids: vec!["123".to_string(), "456".to_string()],
            active: true,
            closed: false,
        };

        let hours = market.hours_until_expiry().unwrap();
        assert!(hours > 1.9 && hours < 2.1);
    }

    #[test]
    fn test_high_certainty() {
        let market = GammaMarket {
            question: "Test?".to_string(),
            slug: "test".to_string(),
            end_date: None,
            outcomes: vec!["Yes".to_string(), "No".to_string()],
            outcome_prices: vec![dec!(0.95), dec!(0.05)],
            clob_token_ids: vec!["123".to_string(), "456".to_string()],
            active: true,
            closed: false,
        };

        assert!(market.has_high_certainty_outcome(dec!(0.95)));
        assert!(!market.has_high_certainty_outcome(dec!(0.96)));
    }

    #[test]
    fn test_highest_certainty_index() {
        let market = GammaMarket {
            question: "Test?".to_string(),
            slug: "test".to_string(),
            end_date: None,
            outcomes: vec!["Yes".to_string(), "No".to_string()],
            outcome_prices: vec![dec!(0.30), dec!(0.70)],
            clob_token_ids: vec!["123".to_string(), "456".to_string()],
            active: true,
            closed: false,
        };

        assert_eq!(market.highest_certainty_index(), Some(1));
    }

    #[tokio::test]
    async fn test_gamma_client_fetch() {
        // This test requires network access, so we just test client creation
        let client = GammaClient::new();
        assert_eq!(client.base_url, "https://gamma-api.polymarket.com");
    }
}
