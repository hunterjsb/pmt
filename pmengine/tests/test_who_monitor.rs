mod fixtures;
use fixtures::*;

use pmengine::strategies::WhoMonitor;
use pmengine::strategies::who_monitor::AlertLevel;
use pmengine::strategy::{Signal, Strategy};
use rust_decimal_macros::dec;

const NO_TOKEN: &str =
    "95212449865986159112377413335252801281670333750637442556685159781445406848396";

// ── helpers ──────────────────────────────────────────────────────────────────

fn is_hold(signals: &[Signal]) -> bool {
    signals.iter().all(|s| matches!(s, Signal::Hold))
}

fn has_cancel(signals: &[Signal]) -> bool {
    signals.iter().any(|s| matches!(s, Signal::Cancel { token_id } if token_id == NO_TOKEN))
}

fn has_shutdown(signals: &[Signal]) -> bool {
    signals.iter().any(|s| matches!(s, Signal::Shutdown { .. }))
}

fn sell_signal(signals: &[Signal]) -> Option<(rust_decimal::Decimal, rust_decimal::Decimal)> {
    signals.iter().find_map(|s| {
        if let Signal::Sell { token_id, price, size, .. } = s {
            if token_id == NO_TOKEN { return Some((*price, *size)); }
        }
        None
    })
}

// ── Watch level ───────────────────────────────────────────────────────────────

#[test]
fn watch_emits_hold_no_trading_action() {
    let mut strategy = WhoMonitor::new_at_level(AlertLevel::Watch, "Hantavirus — Korea");
    let ctx = create_context_with_markets(vec![]);
    let signals = strategy.on_tick(&ctx);
    assert!(is_hold(&signals), "Watch should not trade");
}

// ── Warning level ─────────────────────────────────────────────────────────────

#[test]
fn warning_cancels_maker_bid_no_sell_no_shutdown() {
    let mut strategy = WhoMonitor::new_at_level(
        AlertLevel::Warning,
        "Hantavirus cluster, Multi-country",
    );
    let ctx = create_context_with_markets(vec![
        (NO_TOKEN, dec!(0.85), dec!(0.87), 200.0, 50000.0, dec!(216)),
    ]);
    let signals = strategy.on_tick(&ctx);

    assert!(has_cancel(&signals), "Warning should cancel maker bid");
    assert!(!has_shutdown(&signals), "Warning should NOT shutdown");
    assert!(sell_signal(&signals).is_none(), "Warning should NOT sell position");
}

// ── Alarm level ───────────────────────────────────────────────────────────────

#[test]
fn alarm_with_position_emits_cancel_sell_shutdown() {
    let mut strategy = WhoMonitor::new_at_level(AlertLevel::Alarm, "Hantavirus pandemic declared");
    let ctx = create_context_with_markets(vec![
        (NO_TOKEN, dec!(0.85), dec!(0.87), 200.0, 50000.0, dec!(216)),
    ]);
    let signals = strategy.on_tick(&ctx);

    assert!(has_cancel(&signals));
    assert!(has_shutdown(&signals));

    let (price, size) = sell_signal(&signals).expect("expected Sell");
    assert_eq!(size, dec!(216));
    assert_eq!(price, dec!(0.85), "sell at best bid");
}

#[test]
fn alarm_without_position_no_sell() {
    let mut strategy = WhoMonitor::new_at_level(AlertLevel::Alarm, "Hantavirus pandemic");
    let ctx = create_context_with_markets(vec![]); // no position
    let signals = strategy.on_tick(&ctx);

    assert!(has_cancel(&signals));
    assert!(has_shutdown(&signals));
    assert!(sell_signal(&signals).is_none());
}

#[test]
fn alarm_sell_falls_back_to_one_cent_when_no_book() {
    use pmengine::position::PositionTracker;
    use pmengine::strategy::StrategyContext;
    use chrono::Utc;
    use std::collections::HashMap;

    let mut strategy = WhoMonitor::new_at_level(AlertLevel::Alarm, "Hantavirus pandemic");
    let mut positions = PositionTracker::new();
    let pos = positions.get_or_create(NO_TOKEN);
    pos.size = dec!(100);
    pos.avg_entry_price = dec!(0.93);

    let ctx = StrategyContext {
        timestamp: Utc::now(),
        order_books: HashMap::new(), // no book
        positions,
        markets: HashMap::new(),
        unrealized_pnl: dec!(0),
        realized_pnl: dec!(0),
        usdc_balance: dec!(0),
    };
    let signals = strategy.on_tick(&ctx);
    let (price, _) = sell_signal(&signals).expect("expected Sell");
    assert_eq!(price, dec!(0.01));
}

// ── Escalation / idempotency ───────────────────────────────────────────────────

#[test]
fn signals_emitted_only_once_per_level() {
    let mut strategy = WhoMonitor::new_at_level(AlertLevel::Warning, "cluster");
    let ctx = create_context_with_markets(vec![
        (NO_TOKEN, dec!(0.85), dec!(0.87), 200.0, 50000.0, dec!(50)),
    ]);

    let first = strategy.on_tick(&ctx);
    assert!(has_cancel(&first), "first tick should cancel");

    // Second tick at same level — should hold, not re-emit
    let second = strategy.on_tick(&ctx);
    assert!(is_hold(&second), "subsequent ticks at same level should hold");
}

#[test]
fn escalation_from_warning_to_alarm_emits_alarm_signals() {
    // Simulate a session where the monitor first sees Warning, then Alarm.
    // Warning tick: only Cancel, no Shutdown.
    let mut warning = WhoMonitor::new_at_level(AlertLevel::Warning, "cluster multi-country");
    let ctx = create_context_with_markets(vec![
        (NO_TOKEN, dec!(0.85), dec!(0.87), 200.0, 50000.0, dec!(100)),
    ]);
    let first = warning.on_tick(&ctx);
    assert!(has_cancel(&first));
    assert!(!has_shutdown(&first));

    // Alarm tick (new escalation): Cancel + Sell + Shutdown.
    let mut alarm = WhoMonitor::new_at_level(AlertLevel::Alarm, "pandemic declared");
    let second = alarm.on_tick(&ctx);
    assert!(has_shutdown(&second));
    assert!(sell_signal(&second).is_some());
}

#[test]
fn shutdown_reason_includes_title() {
    let mut strategy = WhoMonitor::new_at_level(AlertLevel::Alarm, "Hantavirus — global spread");
    let ctx = create_context_with_markets(vec![]);
    let signals = strategy.on_tick(&ctx);

    let reason = signals.iter().find_map(|s| {
        if let Signal::Shutdown { reason } = s { Some(reason.clone()) } else { None }
    }).expect("expected Shutdown");

    assert!(reason.contains("Hantavirus — global spread"), "got: {reason}");
}
