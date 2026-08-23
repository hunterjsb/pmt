//! Human-gated trade approval: queue + notification fan-out.
//!
//! Strategies emit `Signal::Alert` instead of a direct `Buy`/`Sell` when
//! they want a human to confirm an action. The engine receives the alert
//! in its tick dispatch, pushes a `PendingAlert` onto the queue (deduped
//! by key), and fires a notification through the configured `Notifier`
//! (ntfy, Discord, or a no-op for local dev). The user approves via
//! `pmt engine approve <id>` (or by tapping a notification action
//! button), at which point the engine dispatches the suggested signal
//! through the normal risk + order pipeline.

use chrono::{DateTime, Utc};
use serde::Serialize;
use std::collections::HashMap;

use crate::strategy::Signal;

/// One pending human-approval action.
#[derive(Debug, Clone, Serialize)]
pub struct PendingAlert {
    pub id: String,
    pub reason: String,
    /// Stored without exposing Box<Signal> (which doesn't serialize cleanly)
    /// — the engine keeps the live signal alongside via `suggested_signal`
    /// in a parallel map and the wire shape only carries the flattened
    /// summary the user sees.
    pub suggested: SuggestedOrder,
    pub created_at: DateTime<Utc>,
    pub expires_at: DateTime<Utc>,
    pub dedupe_key: String,
}

/// Flattened summary of the suggested order, safe to ship over JSON.
#[derive(Debug, Clone, Serialize)]
pub struct SuggestedOrder {
    pub token_id: String,
    pub side: String,
    #[serde(with = "rust_decimal::serde::str")]
    pub price: rust_decimal::Decimal,
    #[serde(with = "rust_decimal::serde::str")]
    pub size: rust_decimal::Decimal,
}

impl SuggestedOrder {
    /// Extract from a Buy/Sell signal. Returns None for non-order signals.
    pub fn from_signal(s: &Signal) -> Option<Self> {
        match s {
            Signal::Buy { token_id, price, size, .. } => Some(Self {
                token_id: token_id.clone(),
                side: "buy".to_string(),
                price: *price,
                size: *size,
            }),
            Signal::Sell { token_id, price, size, .. } => Some(Self {
                token_id: token_id.clone(),
                side: "sell".to_string(),
                price: *price,
                size: *size,
            }),
            _ => None,
        }
    }
}

/// In-memory alert queue owned by the engine. Single-owner (lives on
/// Engine), accessed synchronously from the main tick loop — no locking.
#[derive(Debug, Default)]
pub struct AlertQueue {
    /// id → (alert metadata, live signal to dispatch on approve)
    entries: HashMap<String, (PendingAlert, Signal)>,
}

impl AlertQueue {
    pub fn new() -> Self {
        Self::default()
    }

    /// Push an alert. Returns the assigned id, or None if a non-expired
    /// alert with the same `dedupe_key` already exists.
    pub fn push(
        &mut self,
        reason: String,
        suggested: Signal,
        ttl_secs: i64,
        dedupe_key: String,
    ) -> Option<String> {
        // Dedup against active entries with the same key.
        if self
            .entries
            .values()
            .any(|(a, _)| a.dedupe_key == dedupe_key)
        {
            return None;
        }
        let id = short_id();
        let now = Utc::now();
        let expires_at = now + chrono::Duration::seconds(ttl_secs);
        let summary = SuggestedOrder::from_signal(&suggested)?;
        let alert = PendingAlert {
            id: id.clone(),
            reason,
            suggested: summary,
            created_at: now,
            expires_at,
            dedupe_key,
        };
        self.entries.insert(id.clone(), (alert, suggested));
        Some(id)
    }

    /// Remove and return the alert with `id`, including its live signal
    /// (so the caller can dispatch it). None if not present.
    pub fn take(&mut self, id: &str) -> Option<(PendingAlert, Signal)> {
        self.entries.remove(id)
    }

    /// Listing for the control plane `/alerts` GET endpoint.
    pub fn list(&self) -> Vec<PendingAlert> {
        let mut alerts: Vec<_> = self.entries.values().map(|(a, _)| a.clone()).collect();
        alerts.sort_by_key(|a| a.created_at);
        alerts
    }

    /// Drop expired entries. Returns count removed.
    pub fn prune_expired(&mut self) -> usize {
        let now = Utc::now();
        let expired: Vec<String> = self
            .entries
            .iter()
            .filter(|(_, (a, _))| a.expires_at < now)
            .map(|(id, _)| id.clone())
            .collect();
        let n = expired.len();
        for id in expired {
            self.entries.remove(&id);
        }
        n
    }
}

/// 10-char base16-ish id. Plenty of entropy for a per-engine alert queue
/// that holds at most a few dozen entries at a time.
fn short_id() -> String {
    use std::time::{SystemTime, UNIX_EPOCH};
    let nanos = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|d| d.as_nanos())
        .unwrap_or(0);
    // Unix nanos mixed with a process-wide counter, so two ids minted inside
    // the same clock tick still differ. Avoids pulling in a rand crate.
    static COUNTER: std::sync::atomic::AtomicU64 = std::sync::atomic::AtomicU64::new(0);
    let c = COUNTER.fetch_add(1, std::sync::atomic::Ordering::Relaxed);
    format!("{:010x}", (nanos as u64).wrapping_add(c))
        .chars()
        .rev()
        .collect::<String>()[..10]
        .to_string()
}

/// Pluggable notification backend.
///
/// Concrete enum rather than a `dyn Notifier` trait to keep things simple
/// — no async-trait dependency, dispatch is one match arm.
#[derive(Debug, Clone)]
pub enum Notifier {
    /// Discard. Useful for local dev/tests.
    Noop,
    /// POST to ntfy.sh's public broker on a topic the user subscribes to
    /// via the ntfy mobile app or web client.
    Ntfy {
        topic: String,
        /// Base URL for approve/reject actions surfaced as ntfy buttons.
        /// e.g. http://your-host:7531; the engine appends
        /// /alerts/{id}/{approve|reject}.
        action_base_url: Option<String>,
    },
    /// POST a JSON embed to a Discord webhook.
    Discord { webhook_url: String },
    /// Fire all of these in sequence (best-effort: a failure on one
    /// doesn't stop the rest).
    Multi(Vec<Notifier>),
}

impl Notifier {
    /// Construct from environment:
    ///   PMENGINE_NTFY_TOPIC      — enable ntfy
    ///   PMENGINE_NTFY_ACTION_URL — base URL for ntfy action buttons
    ///   PMENGINE_DISCORD_WEBHOOK — enable Discord
    /// Both can be set to fan-out. Falls back to Noop if neither is set.
    pub fn from_env() -> Self {
        let mut backends = Vec::new();
        if let Ok(topic) = std::env::var("PMENGINE_NTFY_TOPIC") {
            backends.push(Notifier::Ntfy {
                topic,
                action_base_url: std::env::var("PMENGINE_NTFY_ACTION_URL").ok(),
            });
        }
        if let Ok(url) = std::env::var("PMENGINE_DISCORD_WEBHOOK") {
            backends.push(Notifier::Discord { webhook_url: url });
        }
        match backends.len() {
            0 => Notifier::Noop,
            1 => backends.into_iter().next().unwrap(),
            _ => Notifier::Multi(backends),
        }
    }

    /// Send a notification for the given alert. Best-effort: errors are
    /// logged rather than propagated; a failed push must not stop the
    /// engine. `Multi` fans out in declaration order.
    pub async fn notify(&self, alert: &PendingAlert) {
        match self {
            Notifier::Multi(backends) => {
                for n in backends {
                    n.notify_single(alert).await;
                }
            }
            other => other.notify_single(alert).await,
        }
    }

    /// Fire a plain title/body push — no queue entry, no approve/reject
    /// workflow. For conditions a human must SEE but cannot act on with one
    /// tap (the startup unmanaged-position report: the window already
    /// resolved, there is no order to approve).
    ///
    /// Blocking and synchronous on purpose: callers are the strategy's own
    /// `std::thread` helpers, which have no runtime to await on — and
    /// reqwest::blocking must not be driven from a tokio worker. Best-effort
    /// like `notify`: failures log and are dropped.
    pub fn notify_text_blocking(&self, title: &str, body: &str) {
        match self {
            Notifier::Multi(backends) => {
                for n in backends {
                    n.notify_text_blocking(title, body);
                }
            }
            Notifier::Noop => tracing::info!(title, body, "Notification (noop notifier)"),
            Notifier::Ntfy { topic, .. } => {
                let res = reqwest::blocking::Client::new()
                    .post(format!("https://ntfy.sh/{}", topic))
                    .header("Title", title)
                    .header("Tags", "rotating_light,robot")
                    .header("Priority", "high")
                    .body(body.to_string())
                    .send();
                if let Err(e) = res {
                    tracing::warn!(error = %e, "ntfy text notify failed");
                }
            }
            Notifier::Discord { webhook_url } => {
                let payload = serde_json::json!({
                    "content": format!("**{}**\n{}", title, body),
                });
                let res = reqwest::blocking::Client::new()
                    .post(webhook_url)
                    .json(&payload)
                    .send();
                if let Err(e) = res {
                    tracing::warn!(error = %e, "discord text notify failed");
                }
            }
        }
    }

    /// Inner dispatch — never recurses into Multi (Multi is unwrapped in
    /// `notify` above). Keeping this split avoids the "recursive async fn
    /// requires boxing" error from naive recursion.
    async fn notify_single(&self, alert: &PendingAlert) {
        match self {
            Notifier::Noop => {
                tracing::info!(
                    alert_id = %alert.id,
                    reason = %alert.reason,
                    side = %alert.suggested.side,
                    price = %alert.suggested.price,
                    size = %alert.suggested.size,
                    "Alert raised (noop notifier)"
                );
            }
            Notifier::Ntfy { topic, action_base_url } => {
                let url = format!("https://ntfy.sh/{}", topic);
                let body = format!(
                    "{}\n{} {} @ ${} × {}",
                    alert.reason,
                    alert.suggested.side.to_uppercase(),
                    &alert.suggested.token_id[..12.min(alert.suggested.token_id.len())],
                    alert.suggested.price,
                    alert.suggested.size,
                );
                let client = reqwest::Client::new();
                let mut req = client
                    .post(&url)
                    .header("Title", format!("pmengine alert {}", alert.id))
                    .header("Tags", "warning,robot")
                    .header("Priority", "high")
                    .body(body);
                if let Some(base) = action_base_url {
                    let actions = format!(
                        "http, Approve, {}/alerts/{}/approve, method=POST; http, Reject, {}/alerts/{}/reject, method=POST",
                        base, alert.id, base, alert.id
                    );
                    req = req.header("Actions", actions);
                }
                if let Err(e) = req.send().await {
                    tracing::warn!(error = %e, "ntfy notify failed");
                }
            }
            Notifier::Discord { webhook_url } => {
                let payload = serde_json::json!({
                    "content": format!("**pmengine alert** `{}`\n{}\n{} {} @ ${} × {}\n`pmt engine approve {}` to execute",
                        alert.id, alert.reason,
                        alert.suggested.side.to_uppercase(),
                        &alert.suggested.token_id[..12.min(alert.suggested.token_id.len())],
                        alert.suggested.price, alert.suggested.size, alert.id),
                });
                let client = reqwest::Client::new();
                if let Err(e) = client.post(webhook_url).json(&payload).send().await {
                    tracing::warn!(error = %e, "discord notify failed");
                }
            }
            Notifier::Multi(_) => {
                // Unreachable: notify() unwraps Multi before calling here.
                tracing::error!("Multi notifier reached notify_single — bug");
            }
        }
    }
}
