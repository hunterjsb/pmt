//! Gamma API client for market discovery.
//!
//! The Gamma API provides market metadata including:
//! - Market questions and outcomes
//! - End dates for expiration tracking
//! - Token IDs for trading
//!
//! NOTE: We use the /events endpoint with date filtering to find markets
//! expiring soon. The /markets endpoint doesn't support date filtering.

use chrono::{DateTime, Duration, Utc};
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
    /// Calculate hours until market expires (can be negative if past).
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

/// Raw event response from Gamma API /events endpoint.
#[derive(Debug, Deserialize)]
struct RawGammaEvent {
    #[serde(rename = "endDate")]
    end_date: Option<String>,
    markets: Option<Vec<RawGammaMarket>>,
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

    /// Fetch events with markets expiring in a time window.
    ///
    /// Uses the /events endpoint with end_date_min and end_date_max filtering.
    /// This is the correct way to find markets expiring soon.
    async fn fetch_events_in_window(
        &self,
        end_date_min: DateTime<Utc>,
        end_date_max: DateTime<Utc>,
        limit: usize,
    ) -> Result<Vec<RawGammaEvent>, GammaError> {
        let mut all_events = Vec::new();
        let mut offset = 0;
        let batch_size = 100;

        let min_str = end_date_min.format("%Y-%m-%dT%H:%M:%SZ").to_string();
        let max_str = end_date_max.format("%Y-%m-%dT%H:%M:%SZ").to_string();

        tracing::debug!(
            end_date_min = min_str.as_str(),
            end_date_max = max_str.as_str(),
            "Fetching events in time window"
        );

        while all_events.len() < limit {
            let url = format!(
                "{}/events?closed=false&limit={}&offset={}&order=endDate&ascending=true&end_date_min={}&end_date_max={}",
                self.base_url, batch_size, offset, min_str, max_str
            );

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

            let events: Vec<RawGammaEvent> = response
                .json()
                .await
                .map_err(|e| GammaError::ParseError(e.to_string()))?;

            if events.is_empty() {
                break;
            }

            let count = events.len();
            all_events.extend(events);
            offset += batch_size;

            if count < batch_size {
                break; // Last page
            }
        }

        Ok(all_events)
    }

    /// Fetch markets that are expiring soon with high certainty outcomes.
    ///
    /// This uses the /events endpoint with date filtering to find markets
    /// where endDate is between (now - 3h) and (now + max_hours).
    /// Markets with endDate in the recent past are about to resolve.
    pub async fn fetch_sure_bet_candidates(
        &self,
        max_hours_to_expiry: f64,
        min_certainty: Decimal,
    ) -> Result<Vec<GammaMarket>, GammaError> {
        let now = Utc::now();
        // Look for markets with endDate recently passed (resolving now) or about to pass
        let end_date_min = now - Duration::hours(3);
        let end_date_max = now + Duration::hours(max_hours_to_expiry as i64 + 1);

        let events = self.fetch_events_in_window(end_date_min, end_date_max, 2000).await?;

        tracing::info!(
            event_count = events.len(),
            "Fetched events from Gamma API"
        );

        let mut candidates = Vec::new();

        for event in events {
            let event_end_date = event.end_date.as_ref();

            if let Some(markets) = event.markets {
                for raw_market in markets {
                    // Skip inactive or closed markets
                    if !raw_market.active.unwrap_or(false) || raw_market.closed.unwrap_or(true) {
                        continue;
                    }

                    // Use market end_date, fall back to event end_date
                    let end_date_str = raw_market.end_date.clone().or_else(|| event_end_date.cloned());

                    if let Ok(market) = self.parse_market_with_end_date(raw_market, end_date_str.as_ref()) {
                        // Check hours until expiry: -3h to +max_hours
                        if let Some(hours) = market.hours_until_expiry() {
                            if hours >= -3.0 && hours <= max_hours_to_expiry {
                                // Check for high certainty outcome
                                if market.has_high_certainty_outcome(min_certainty) {
                                    candidates.push(market);
                                }
                            }
                        }
                    }
                }
            }
        }

        tracing::info!(
            candidate_count = candidates.len(),
            "Found sure bet candidates"
        );

        Ok(candidates)
    }

    /// Parse a raw market response into structured data.
    fn parse_market_with_end_date(
        &self,
        raw: RawGammaMarket,
        fallback_end_date: Option<&String>,
    ) -> Result<GammaMarket, GammaError> {
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

        // Skip markets without prices
        if outcome_prices.is_empty() {
            return Err(GammaError::InvalidData("No outcome prices".to_string()));
        }

        // Parse CLOB token IDs from JSON string
        let clob_token_ids: Vec<String> = raw
            .clob_token_ids
            .as_ref()
            .and_then(|s| serde_json::from_str(s).ok())
            .unwrap_or_default();

        // Skip markets without token IDs
        if clob_token_ids.is_empty() {
            return Err(GammaError::InvalidData("No token IDs".to_string()));
        }

        // Parse end date (use market's or fallback to event's)
        let end_date_str = raw.end_date.as_ref().or(fallback_end_date);
        let end_date = end_date_str.and_then(|s| parse_datetime(s));

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

/// Parse a datetime string in various formats.
fn parse_datetime(s: &str) -> Option<DateTime<Utc>> {
    // Try RFC3339 first
    if let Ok(dt) = DateTime::parse_from_rfc3339(s) {
        return Some(dt.with_timezone(&Utc));
    }

    // Try with Z suffix converted
    let s_fixed = if s.ends_with('Z') {
        format!("{}+00:00", &s[..s.len() - 1])
    } else {
        s.to_string()
    };

    DateTime::parse_from_rfc3339(&s_fixed)
        .ok()
        .map(|dt| dt.with_timezone(&Utc))
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
