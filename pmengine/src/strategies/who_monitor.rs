use crate::position::Fill;
use crate::strategy::{Signal, Strategy, StrategyContext, Urgency};
use rust_decimal::Decimal;
use serde_json::Value;
use std::sync::{
    atomic::{AtomicBool, Ordering},
    Arc,
};
use tokio::time::{interval, Duration};

const HANTAVIRUS_PANDEMIC_NO: &str =
    "95212449865986159112377413335252801281670333750637442556685159781445406848396";

// WHO removed RSS in 2024. The JSON API is the current equivalent.
const WHO_DON_API: &str = "https://www.who.int/api/news/diseaseoutbreaknews?sf_culture=en&$top=20&$orderby=PublicationDateAndTime+desc";
const ALERT_KEYWORDS: &[&str] = &["hantavirus"];
const POLL_INTERVAL_SECS: u64 = 60;

pub struct WhoMonitor {
    id: String,
    triggered: Arc<AtomicBool>,
    alert_title: Arc<std::sync::Mutex<Option<String>>>,
}

impl WhoMonitor {
    pub fn new() -> Self {
        let triggered = Arc::new(AtomicBool::new(false));
        let alert_title = Arc::new(std::sync::Mutex::new(None::<String>));

        let triggered_bg = triggered.clone();
        let title_bg = alert_title.clone();

        tokio::spawn(async move {
            let client = reqwest::Client::builder()
                .user_agent("pmengine/1.0")
                .timeout(Duration::from_secs(10))
                .build()
                .expect("reqwest client build failed");

            let mut timer = interval(Duration::from_secs(POLL_INTERVAL_SECS));

            loop {
                timer.tick().await;

                if triggered_bg.load(Ordering::Relaxed) {
                    break;
                }

                match client.get(WHO_DON_API).send().await {
                    Err(e) => tracing::warn!(error = %e, "WHO DON fetch failed"),
                    Ok(resp) => match resp.json::<serde_json::Value>().await {
                        Err(e) => tracing::warn!(error = %e, "WHO DON JSON parse failed"),
                        Ok(json) => {
                            if let Some(title) = first_matching_don_title(&json) {
                                tracing::warn!(
                                    title = %title,
                                    "WHO ALERT — hantavirus mention in Disease Outbreak News"
                                );
                                *title_bg.lock().unwrap() = Some(title);
                                triggered_bg.store(true, Ordering::Relaxed);
                            } else {
                                tracing::debug!("WHO DON polled — no hantavirus mention");
                            }
                        }
                    },
                }
            }
        });

        Self {
            id: "who_monitor".to_string(),
            triggered,
            alert_title,
        }
    }
}

/// Search the WHO DON JSON response (`{ "value": [ { "Title": "...", ... } ] }`)
/// for the first entry whose title contains any ALERT_KEYWORD.
fn first_matching_don_title(json: &Value) -> Option<String> {
    let items = json.get("value")?.as_array()?;
    for item in items {
        let title = item.get("Title")?.as_str().unwrap_or("");
        if ALERT_KEYWORDS.iter().any(|kw| title.to_lowercase().contains(kw)) {
            return Some(title.to_string());
        }
    }
    None
}

impl WhoMonitor {
    /// Test constructor: pre-triggered state, no background task.
    /// Use in tests to exercise the `on_tick` signal path without a live engine.
    pub fn new_triggered(title: &str) -> Self {
        let triggered = Arc::new(AtomicBool::new(true));
        let alert_title = Arc::new(std::sync::Mutex::new(Some(title.to_string())));
        Self { id: "who_monitor".to_string(), triggered, alert_title }
    }
}

impl Default for WhoMonitor {
    fn default() -> Self {
        Self::new()
    }
}

impl Strategy for WhoMonitor {
    fn id(&self) -> &str {
        &self.id
    }

    fn subscriptions(&self) -> Vec<String> {
        vec![HANTAVIRUS_PANDEMIC_NO.to_string()]
    }

    fn on_tick(&mut self, ctx: &StrategyContext) -> Vec<Signal> {
        if !self.triggered.load(Ordering::Relaxed) {
            return vec![Signal::Hold];
        }

        let title = self
            .alert_title
            .lock()
            .ok()
            .and_then(|g| g.clone())
            .unwrap_or_else(|| "WHO hantavirus alert".to_string());

        tracing::warn!(title = %title, "WhoMonitor: emitting emergency exit + shutdown");

        let mut signals = vec![Signal::Cancel {
            token_id: HANTAVIRUS_PANDEMIC_NO.to_string(),
        }];

        // Size the sell from actual position; exit at best bid or 1¢ floor
        if let Some(pos) = ctx.positions.get(HANTAVIRUS_PANDEMIC_NO) {
            if pos.size > Decimal::ZERO {
                let exit_price = ctx
                    .order_books
                    .get(HANTAVIRUS_PANDEMIC_NO)
                    .and_then(|b| b.best_bid())
                    .map(|l| l.price)
                    .unwrap_or(Decimal::new(1, 2)); // 0.01 floor

                signals.push(Signal::Sell {
                    token_id: HANTAVIRUS_PANDEMIC_NO.to_string(),
                    price: exit_price,
                    size: pos.size,
                    urgency: Urgency::Immediate,
                });
            }
        }

        signals.push(Signal::Shutdown {
            reason: format!("WHO alert: {}", title),
        });

        signals
    }

    fn on_fill(&mut self, _fill: &Fill) {}
    fn on_shutdown(&mut self) {}
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    fn don_response(titles: &[&str]) -> Value {
        let items: Vec<Value> = titles
            .iter()
            .map(|t| json!({ "Title": t, "PublicationDateAndTime": "2026-05-01T00:00:00Z" }))
            .collect();
        json!({ "value": items })
    }

    #[test]
    fn matches_hantavirus_in_title() {
        let resp = don_response(&["Hantavirus outbreak update — China"]);
        assert_eq!(
            first_matching_don_title(&resp),
            Some("Hantavirus outbreak update — China".into())
        );
    }

    #[test]
    fn no_match_for_unrelated_outbreak() {
        let resp = don_response(&["Ebola update — DRC"]);
        assert_eq!(first_matching_don_title(&resp), None);
    }

    #[test]
    fn returns_first_matching_item() {
        let resp = don_response(&[
            "Influenza surveillance",
            "Hantavirus — Republic of Korea",
            "Hantavirus follow-up",
        ]);
        assert_eq!(
            first_matching_don_title(&resp),
            Some("Hantavirus — Republic of Korea".into())
        );
    }

    #[test]
    fn case_insensitive_match() {
        let resp = don_response(&["HANTAVIRUS PANDEMIC DECLARED"]);
        assert!(first_matching_don_title(&resp).is_some());
    }

    #[test]
    fn empty_value_array_returns_none() {
        let resp = json!({ "value": [] });
        assert_eq!(first_matching_don_title(&resp), None);
    }

    /// Fetch the real WHO DON JSON API and print any hantavirus entries found.
    /// Run with: cargo test -- --ignored live_who
    #[tokio::test]
    #[ignore]
    async fn live_who_don_api() {
        let client = reqwest::Client::builder()
            .user_agent("pmengine-test/1.0")
            .timeout(Duration::from_secs(15))
            .build()
            .unwrap();

        let json: Value = client
            .get(WHO_DON_API)
            .send()
            .await
            .expect("WHO DON API fetch failed")
            .json()
            .await
            .expect("JSON parse failed");

        let items = json.get("value").and_then(|v| v.as_array()).expect("expected value array");
        assert!(!items.is_empty(), "expected DON items");
        println!("Latest {} DON items:", items.len());
        for item in items.iter().take(10) {
            println!("  {}", item.get("Title").and_then(|t| t.as_str()).unwrap_or("?"));
        }

        let hit = first_matching_don_title(&json);
        println!("\nHantavirus match: {:?}", hit);
    }
}
