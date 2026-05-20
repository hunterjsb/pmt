use crate::position::Fill;
use crate::strategy::{Signal, Strategy, StrategyContext, Urgency};
use rust_decimal::Decimal;
use serde_json::Value;
use std::sync::{
    atomic::{AtomicU8, Ordering},
    Arc,
};
use tokio::time::{interval, Duration};

const HANTAVIRUS_PANDEMIC_NO: &str =
    "95212449865986159112377413335252801281670333750637442556685159781445406848396";

// WHO removed RSS in 2024. The JSON API is the current equivalent.
const WHO_DON_API: &str = "https://www.who.int/api/news/diseaseoutbreaknews?sf_culture=en&$top=20&$orderby=PublicationDateAndTime+desc";
const POLL_INTERVAL_SECS: u64 = 60;

// Must appear in the title for ANY action (gate check before severity classification).
const HANTAVIRUS_KW: &str = "hantavirus";

// Alarm: WHO has actually declared something — this is what resolves the market YES.
const ALARM_KEYWORDS: &[&str] = &[
    "pandemic",
    "pheic",
    "public health emergency",
    "emergency of international concern",
];

// Warning: active multi-country or community spread — risk elevated but not resolved.
const WARNING_KEYWORDS: &[&str] = &[
    "multi-country",
    "multiple countries",
    "community transmission",
    "widespread",
    "rapid increase",
    "global spread",
];

/// Severity of a WHO hantavirus alert.
///
/// Ordering matters: Alarm > Warning > Watch > None.
/// The engine escalates through levels but never de-escalates within a session.
#[derive(Debug, Clone, Copy, PartialEq, Eq, PartialOrd, Ord)]
pub enum AlertLevel {
    None    = 0,
    Watch   = 1, // any DON entry → log, no trade action
    Warning = 2, // multi-country / community spread → cancel maker bid
    Alarm   = 3, // PHEIC / pandemic declaration → exit position + shutdown
}

impl AlertLevel {
    fn from_u8(v: u8) -> Self {
        match v {
            1 => Self::Watch,
            2 => Self::Warning,
            3 => Self::Alarm,
            _ => Self::None,
        }
    }
    pub fn as_u8(self) -> u8 { self as u8 }
}

/// Classify a single DON entry title into a severity level.
/// Returns `None` if the title isn't hantavirus-related.
pub fn classify(title: &str) -> Option<AlertLevel> {
    let lower = title.to_lowercase();
    if !lower.contains(HANTAVIRUS_KW) {
        return None;
    }
    if ALARM_KEYWORDS.iter().any(|kw| lower.contains(kw)) {
        Some(AlertLevel::Alarm)
    } else if WARNING_KEYWORDS.iter().any(|kw| lower.contains(kw)) {
        Some(AlertLevel::Warning)
    } else {
        Some(AlertLevel::Watch)
    }
}

/// Scan a WHO DON JSON response and return the highest severity entry found,
/// along with its title.
pub fn assess_don(json: &Value) -> (AlertLevel, Option<String>) {
    let items = match json.get("value").and_then(|v| v.as_array()) {
        Some(a) => a,
        None => return (AlertLevel::None, None),
    };

    let mut max_level = AlertLevel::None;
    let mut max_title: Option<String> = None;

    for item in items {
        let title = item.get("Title").and_then(|t| t.as_str()).unwrap_or("");
        if let Some(level) = classify(title) {
            if level > max_level {
                max_level = level;
                max_title = Some(title.to_string());
            }
        }
    }

    (max_level, max_title)
}

pub struct WhoMonitor {
    id: String,
    /// Shared with background poller. Stores the highest AlertLevel seen as u8.
    severity: Arc<AtomicU8>,
    alert_title: Arc<std::sync::Mutex<Option<String>>>,
    /// The highest level we've already acted on. Signals are emitted once per
    /// escalation, not on every tick, so this tracks what we've handled.
    last_acted: AlertLevel,
}

impl WhoMonitor {
    pub fn new() -> Self {
        let severity = Arc::new(AtomicU8::new(AlertLevel::None.as_u8()));
        let alert_title = Arc::new(std::sync::Mutex::new(None::<String>));

        let severity_bg = severity.clone();
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

                // Stop polling once we've hit Alarm — nothing higher to detect.
                if AlertLevel::from_u8(severity_bg.load(Ordering::Relaxed)) == AlertLevel::Alarm {
                    break;
                }

                match client.get(WHO_DON_API).send().await {
                    Err(e) => tracing::warn!(error = %e, "WHO DON fetch failed"),
                    Ok(resp) => match resp.json::<Value>().await {
                        Err(e) => tracing::warn!(error = %e, "WHO DON JSON parse failed"),
                        Ok(json) => {
                            let (new_level, new_title) = assess_don(&json);
                            let prev = AlertLevel::from_u8(severity_bg.load(Ordering::Relaxed));

                            if new_level > prev {
                                tracing::warn!(
                                    level = ?new_level,
                                    title = %new_title.as_deref().unwrap_or("?"),
                                    "WHO DON severity escalated"
                                );
                                if let Some(t) = new_title {
                                    *title_bg.lock().unwrap() = Some(t);
                                }
                                severity_bg.store(new_level.as_u8(), Ordering::Relaxed);
                            } else {
                                tracing::debug!(level = ?new_level, "WHO DON polled — no escalation");
                            }
                        }
                    },
                }
            }
        });

        Self {
            id: "who_monitor".to_string(),
            severity,
            alert_title,
            last_acted: AlertLevel::None,
        }
    }

    /// Test constructor: inject a specific alert level without a background task.
    pub fn new_at_level(level: AlertLevel, title: &str) -> Self {
        let severity = Arc::new(AtomicU8::new(level.as_u8()));
        let alert_title = Arc::new(std::sync::Mutex::new(Some(title.to_string())));
        Self {
            id: "who_monitor".to_string(),
            severity,
            alert_title,
            last_acted: AlertLevel::None,
        }
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
        let current = AlertLevel::from_u8(self.severity.load(Ordering::Relaxed));

        // Only act on escalation — emit signals once per level, not every tick.
        if current <= self.last_acted {
            return vec![Signal::Hold];
        }
        self.last_acted = current;

        let title = self
            .alert_title
            .lock()
            .ok()
            .and_then(|g| g.clone())
            .unwrap_or_else(|| "WHO hantavirus alert".to_string());

        match current {
            AlertLevel::None => vec![Signal::Hold],

            AlertLevel::Watch => {
                // Active DON entry but no spread indicators — log, don't trade.
                tracing::warn!(
                    title = %title,
                    "WhoMonitor WATCH — hantavirus DON entry, no trading action"
                );
                vec![Signal::Hold]
            }

            AlertLevel::Warning => {
                // Multi-country or community spread — cancel the resting maker
                // bid to stop accumulating more NO exposure, but hold the position.
                tracing::warn!(
                    title = %title,
                    "WhoMonitor WARNING — cancelling pandemic NO maker bid"
                );
                vec![Signal::Cancel {
                    token_id: HANTAVIRUS_PANDEMIC_NO.to_string(),
                }]
            }

            AlertLevel::Alarm => {
                // PHEIC or pandemic declaration — this likely resolves the market
                // YES. Exit position immediately and shutdown.
                tracing::error!(
                    title = %title,
                    "WhoMonitor ALARM — emergency exit + shutdown"
                );
                let mut signals = vec![Signal::Cancel {
                    token_id: HANTAVIRUS_PANDEMIC_NO.to_string(),
                }];

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
                    reason: format!("WHO alarm: {}", title),
                });
                signals
            }
        }
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
            .map(|t| json!({ "Title": t }))
            .collect();
        json!({ "value": items })
    }

    // --- classify ---

    #[test]
    fn classify_watch_plain_cluster() {
        assert_eq!(classify("Hantavirus — Republic of Korea"), Some(AlertLevel::Watch));
    }

    #[test]
    fn classify_warning_multi_country() {
        assert_eq!(
            classify("Hantavirus cluster linked to cruise ship travel, Multi-country"),
            Some(AlertLevel::Warning)
        );
    }

    #[test]
    fn classify_alarm_pandemic() {
        assert_eq!(
            classify("Hantavirus pandemic declared — WHO"),
            Some(AlertLevel::Alarm)
        );
    }

    #[test]
    fn classify_alarm_pheic() {
        assert_eq!(
            classify("Hantavirus: Public Health Emergency of International Concern"),
            Some(AlertLevel::Alarm)
        );
    }

    #[test]
    fn classify_none_for_unrelated() {
        assert_eq!(classify("Ebola update — DRC"), None);
    }

    #[test]
    fn classify_case_insensitive() {
        assert_eq!(classify("HANTAVIRUS PANDEMIC"), Some(AlertLevel::Alarm));
    }

    // --- assess_don ---

    #[test]
    fn assess_returns_highest_severity() {
        let resp = don_response(&[
            "Hantavirus — Republic of Korea",               // Watch
            "Hantavirus cluster, Multi-country",            // Warning
        ]);
        let (level, title) = assess_don(&resp);
        assert_eq!(level, AlertLevel::Warning);
        assert!(title.unwrap().contains("Multi-country"));
    }

    #[test]
    fn assess_alarm_takes_priority_over_warning() {
        let resp = don_response(&[
            "Hantavirus cluster, Multi-country",
            "Hantavirus pandemic declared",
        ]);
        let (level, _) = assess_don(&resp);
        assert_eq!(level, AlertLevel::Alarm);
    }

    #[test]
    fn assess_no_hantavirus_returns_none() {
        let resp = don_response(&["Ebola — DRC", "Measles — Bangladesh"]);
        let (level, _) = assess_don(&resp);
        assert_eq!(level, AlertLevel::None);
    }

    #[test]
    fn assess_empty_returns_none() {
        let (level, _) = assess_don(&json!({ "value": [] }));
        assert_eq!(level, AlertLevel::None);
    }

    // --- live WHO DON API ---

    /// Fetch the real WHO DON API and show what severity we'd assign.
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
        println!("Latest {} DON items:", items.len());
        for item in items.iter().take(10) {
            let title = item.get("Title").and_then(|t| t.as_str()).unwrap_or("?");
            let level = classify(title);
            println!("  [{:?}] {}", level, title);
        }

        let (level, title) = assess_don(&json);
        println!("\nMax severity: {:?} — {:?}", level, title);
    }
}
