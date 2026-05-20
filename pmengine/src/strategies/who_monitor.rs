use crate::position::Fill;
use crate::strategy::{Signal, Strategy, StrategyContext, Urgency};
use rust_decimal::Decimal;
use std::sync::{
    atomic::{AtomicBool, Ordering},
    Arc,
};
use tokio::time::{interval, Duration};

const HANTAVIRUS_PANDEMIC_NO: &str =
    "95212449865986159112377413335252801281670333750637442556685159781445406848396";

const WHO_DON_RSS: &str = "https://www.who.int/feeds/entity/csr/don/en/rss.xml";
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
                    break; // already fired, stop polling
                }

                match client.get(WHO_DON_RSS).send().await {
                    Err(e) => tracing::warn!(error = %e, "WHO DON fetch failed"),
                    Ok(resp) => match resp.text().await {
                        Err(e) => tracing::warn!(error = %e, "WHO DON body read failed"),
                        Ok(body) => {
                            if let Some(title) = first_matching_item_title(&body) {
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

/// Scan RSS XML for the first <item> whose text matches any ALERT_KEYWORD.
/// Uses simple substring search — avoids a full XML parser dependency.
fn first_matching_item_title(body: &str) -> Option<String> {
    let lower = body.to_lowercase();
    let mut pos = 0;

    while let Some(rel) = lower[pos..].find("<item>") {
        let start = pos + rel;
        let end = lower[start..].find("</item>").map(|r| start + r).unwrap_or(lower.len());

        let item_lower = &lower[start..end];
        if ALERT_KEYWORDS.iter().any(|kw| item_lower.contains(kw)) {
            let item_orig = &body[start..end];
            return Some(extract_tag(item_orig, "title").unwrap_or_else(|| "(unknown)".into()));
        }

        pos = end + 7; // step past </item>
    }
    None
}

/// Pull the text content of the first `<tag>…</tag>` in `text`.
fn extract_tag(text: &str, tag: &str) -> Option<String> {
    let open = format!("<{}>", tag);
    let close = format!("</{}>", tag);
    let start = text.find(&open)? + open.len();
    let end = text[start..].find(&close)? + start;
    Some(text[start..end].trim().to_string())
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

    fn rss(items: &[(&str, &str)]) -> String {
        let items_xml: String = items
            .iter()
            .map(|(title, desc)| {
                format!(
                    "<item><title>{}</title><description>{}</description></item>",
                    title, desc
                )
            })
            .collect();
        format!("<rss><channel>{}</channel></rss>", items_xml)
    }

    #[test]
    fn matches_hantavirus_in_title() {
        let body = rss(&[("Hantavirus outbreak update — China", "Several cases confirmed.")]);
        assert_eq!(
            first_matching_item_title(&body),
            Some("Hantavirus outbreak update — China".into())
        );
    }

    #[test]
    fn matches_hantavirus_in_description() {
        let body = rss(&[("Disease Outbreak News", "Hantavirus cases reported in three provinces.")]);
        assert!(first_matching_item_title(&body).is_some());
    }

    #[test]
    fn no_match_for_unrelated_outbreak() {
        let body = rss(&[("Ebola update — DRC", "Ongoing response in Équateur province.")]);
        assert_eq!(first_matching_item_title(&body), None);
    }

    #[test]
    fn returns_first_matching_item() {
        let body = rss(&[
            ("Influenza surveillance", "Seasonal flu activity."),
            ("Hantavirus — Republic of Korea", "Rodent-borne cases confirmed."),
            ("Hantavirus follow-up", "Second report."),
        ]);
        assert_eq!(
            first_matching_item_title(&body),
            Some("Hantavirus — Republic of Korea".into())
        );
    }

    #[test]
    fn case_insensitive_match() {
        let body = rss(&[("HANTAVIRUS PANDEMIC DECLARED", "Global alert issued.")]);
        assert!(first_matching_item_title(&body).is_some());
    }

    #[test]
    fn extract_tag_basic() {
        assert_eq!(
            extract_tag("<item><title>Hello</title></item>", "title"),
            Some("Hello".into())
        );
    }

    #[test]
    fn extract_tag_missing() {
        assert_eq!(extract_tag("<item><desc>x</desc></item>", "title"), None);
    }
}
