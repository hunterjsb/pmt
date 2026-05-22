//! Test strategy: emits one Signal::Alert on first tick.
//!
//! Used to smoke-test the alert pipeline (queue + notifier + control plane
//! approve/reject) without waiting on a real momentum-detection signal.
//! The "suggested" inner signal is a tiny Buy on the Vermont Governor
//! market at $0.01 — same token order_test uses, low-stakes price.

use crate::strategy::{Signal, Strategy, StrategyContext, Urgency};
use rust_decimal_macros::dec;

pub struct AlertTest {
    id: String,
    tokens: Vec<String>,
    fired: bool,
}

impl AlertTest {
    pub fn new() -> Self {
        Self {
            id: "alert_test".to_string(),
            // Vermont Governor 2026 — same as order_test
            tokens: vec![
                "41583919731714354912849507182398941127545694257513505398713274521520484370640"
                    .to_string(),
            ],
            fired: false,
        }
    }
}

impl Default for AlertTest {
    fn default() -> Self {
        Self::new()
    }
}

impl Strategy for AlertTest {
    fn id(&self) -> &str {
        &self.id
    }

    fn subscriptions(&self) -> Vec<String> {
        self.tokens.clone()
    }

    fn tick_interval_ms(&self) -> u64 {
        // Fire fast so the smoke test doesn't have to wait
        500
    }

    fn on_tick(&mut self, _ctx: &StrategyContext) -> Vec<Signal> {
        if self.fired {
            return vec![Signal::Hold];
        }
        self.fired = true;
        let token = self.tokens[0].clone();
        let suggested = Signal::Buy {
            token_id: token.clone(),
            price: dec!(0.01),
            size: dec!(5),
            urgency: Urgency::Low,
        };
        vec![Signal::Alert {
            reason: "alert_test sanity check — buy 5 @ $0.01".to_string(),
            suggested: Box::new(suggested),
            ttl_secs: 600,
            dedupe_key: "alert_test-vermont-001".to_string(),
        }]
    }
}
