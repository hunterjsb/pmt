//! Phase 7 order-path telemetry — one JSONL record per decision, splitting
//! decision->ack into build / sign / send / ack.
//!
//! Why this file exists: every latency figure in `analysis/latency_report.txt`
//! had to *bound* the order path rather than measure it. The only fill clock
//! was a Polygon block (integer seconds, ~2s inclusion), and the engine log
//! offered decision->ack as a single 452ms p50 lump with ~334ms of it
//! unattributed — "build/sign/SDK/cancel (residual)" in the report's budget
//! table. This tape splits that lump so the next report names the stage
//! instead of inferring it.
//!
//! Stamps are `std::time::Instant` deltas against ONE wall-clock read taken
//! at build start. Per-stage `Utc::now()` would cost more and is not
//! monotonic — an NTP step mid-order would print a negative send time.
//!
//! Two contracts the callers must keep:
//!   * a record is written OUTSIDE every engine lock (nothing here awaits,
//!     and `record_fill` drops its map guard before touching the disk);
//!   * a failed write is dropped silently — a lost tape line must never
//!     impact live trading, same as `updown_model::append_jsonl`.

use std::collections::{HashMap, VecDeque};
use std::sync::atomic::{AtomicU64, Ordering};
use std::sync::{Mutex, OnceLock};
use std::time::Instant;

use rust_decimal::Decimal;

use crate::strategies::updown_model::append_jsonl;

/// Sits beside the eval/book tapes in `~/.pmt/engine/`. Its own file: the
/// order path samples on a completely different clock from the 5s eval
/// throttle, and mixing them would make either one awkward to read.
const TAPE_FILE: &str = "order-latency-tape.jsonl";

/// How many acked orders stay eligible for a fill stamp. Fills arrive via
/// the ~5s trades poll, so an order still unmatched hundreds of placements
/// later never will be — the bound keeps the map from growing with uptime.
const PENDING_FILL_CAP: usize = 512;

/// Stage stamps for one order's trip down the wire. `wall` is the single
/// clock read; every other field is a monotonic `Instant` whose offset from
/// `build_start` is what the tape records.
#[derive(Debug, Clone, Copy)]
pub struct OrderTimings {
    /// Unix seconds at `build_start`. Matches the `"t"` field the updown
    /// tape writes, so the two files join on time without a format dance.
    pub wall: f64,
    /// Order build entered — includes the tick-size resolution, which is a
    /// cache read once the token has been prewarmed.
    pub build_start: Instant,
    /// EIP-712 signature produced (build + neg-risk resolution done).
    pub sign_done: Option<Instant>,
    /// Request handed to the socket — everything after this is wire.
    pub send: Option<Instant>,
    /// CLOB response fully read, order id in hand.
    pub ack: Option<Instant>,
}

impl OrderTimings {
    /// Anchor a new record. Call this at the very top of the order path;
    /// everything downstream is an `Instant` delta against it.
    pub fn start() -> Self {
        Self {
            wall: unix_seconds(),
            build_start: Instant::now(),
            sign_done: None,
            send: None,
            ack: None,
        }
    }

    /// Milliseconds from `build_start` to `at`, `None` for a stage the
    /// order never reached (a build failure, a dry run, a rejection).
    fn offset_ms(&self, at: Option<Instant>) -> Option<f64> {
        at.map(|i| i.duration_since(self.build_start).as_secs_f64() * 1000.0)
    }
}

impl Default for OrderTimings {
    fn default() -> Self {
        Self::start()
    }
}

fn unix_seconds() -> f64 {
    chrono::Utc::now().timestamp_micros() as f64 / 1_000_000.0
}

/// Process-unique key joining a tape line to the fire that caused it: unix
/// millis plus a monotonic counter. Dependency-free on purpose — a uuid
/// crate for an id nothing outside this process reads would be a new
/// dependency for no gain.
pub fn next_decision_id() -> String {
    static SEQ: AtomicU64 = AtomicU64::new(0);
    let n = SEQ.fetch_add(1, Ordering::Relaxed);
    format!("{}-{}", chrono::Utc::now().timestamp_millis(), n)
}

/// What an acked order needs to remember so a later fill can be stamped
/// against the same anchor the ack used.
#[derive(Debug, Clone)]
struct PendingFill {
    decision_id: String,
    token_id: String,
    wall: f64,
    build_start: Instant,
}

/// (order_id -> pending, insertion order for eviction). A plain `Mutex`:
/// every critical section is a map operation with no await inside it.
type PendingMap = Mutex<(HashMap<String, PendingFill>, VecDeque<String>)>;

fn pending() -> &'static PendingMap {
    static PENDING: OnceLock<PendingMap> = OnceLock::new();
    PENDING.get_or_init(|| Mutex::new((HashMap::new(), VecDeque::new())))
}

/// Build the ack record. Split from the write so its shape is testable
/// without a filesystem.
#[allow(clippy::too_many_arguments)]
fn placed_record(
    decision_id: &str,
    token_id: &str,
    side: &str,
    price: Decimal,
    size: Decimal,
    order_id: &str,
    post_only: bool,
    t: &OrderTimings,
) -> serde_json::Value {
    let mut rec = serde_json::json!({
        "t": t.wall,
        "stage": "ack",
        "decision_id": decision_id,
        "token": token_id,
        "side": side,
        "price": price.to_string(),
        "size": size.to_string(),
        "suppressed": false,
        "order_id": order_id,
    });
    let map = rec.as_object_mut().expect("json! built an object");
    // Additive and only when true: every tape line ever written lacks the
    // key, so a reader defaults it to false and old tape stays readable.
    if post_only {
        map.insert("post_only".to_string(), serde_json::Value::Bool(true));
    }
    // Cumulative offsets from build start — the report's t_sign_done /
    // t_send / t_ack. Per-stage costs are differences; keeping the raw
    // offsets means a reader never has to trust our subtraction.
    for (key, value) in [
        ("sign_done_ms", t.offset_ms(t.sign_done)),
        ("send_ms", t.offset_ms(t.send)),
        ("ack_ms", t.offset_ms(t.ack)),
    ] {
        if let Some(v) = value {
            map.insert(key.to_string(), round_ms(v));
        }
    }
    rec
}

/// Build the record for a fire the delta matcher declined to act on — the
/// 8.7% of clip firings that never reached the wire (report section 7).
/// They carry no HTTP timings because there was no HTTP.
fn suppressed_record(
    decision_id: &str,
    token_id: &str,
    side: &str,
    price: Decimal,
    size: Decimal,
    kept_order_id: &str,
) -> serde_json::Value {
    serde_json::json!({
        "t": unix_seconds(),
        "stage": "suppressed",
        "decision_id": decision_id,
        "token": token_id,
        "side": side,
        "price": price.to_string(),
        "size": size.to_string(),
        "suppressed": true,
        "kept_order_id": kept_order_id,
    })
}

fn fill_record(p: &PendingFill, order_id: &str, price: Decimal, size: Decimal, at: Instant) -> serde_json::Value {
    serde_json::json!({
        "t": p.wall,
        "stage": "fill",
        "decision_id": p.decision_id,
        "token": p.token_id,
        "order_id": order_id,
        "price": price.to_string(),
        "size": size.to_string(),
        "fill_ms": round_ms(at.duration_since(p.build_start).as_secs_f64() * 1000.0),
    })
}

/// Two decimals is finer than any clock jitter we can resolve and keeps the
/// tape lines short.
fn round_ms(v: f64) -> serde_json::Value {
    serde_json::json!((v * 100.0).round() / 100.0)
}

/// Record an acknowledged order and arm it for a later fill stamp.
#[allow(clippy::too_many_arguments)]
pub fn record_placed(
    decision_id: &str,
    token_id: &str,
    side: &str,
    price: Decimal,
    size: Decimal,
    order_id: &str,
    post_only: bool,
    t: &OrderTimings,
) {
    let record =
        placed_record(decision_id, token_id, side, price, size, order_id, post_only, t);
    arm_fill(
        order_id,
        PendingFill {
            decision_id: decision_id.to_string(),
            token_id: token_id.to_string(),
            wall: t.wall,
            build_start: t.build_start,
        },
    );
    append_jsonl(TAPE_FILE, record);
}

/// Record a fire the delta matcher suppressed in favour of a standing order.
pub fn record_suppressed(
    decision_id: &str,
    token_id: &str,
    side: &str,
    price: Decimal,
    size: Decimal,
    kept_order_id: &str,
) {
    append_jsonl(
        TAPE_FILE,
        suppressed_record(decision_id, token_id, side, price, size, kept_order_id),
    );
}

/// Stamp a fill against the decision that produced its order. No-op for an
/// order this process did not place (or one evicted by the cap) — the tape
/// simply has no fill line for it.
pub fn record_fill(order_id: &str, price: Decimal, size: Decimal) {
    let at = Instant::now();
    // Scoped so the guard is dropped before the write: the tape line goes
    // to disk outside the lock, always.
    let found = {
        let Ok(mut guard) = pending().lock() else { return };
        let (map, order) = &mut *guard;
        let hit = map.remove(order_id);
        if hit.is_some() {
            order.retain(|id| id != order_id);
        }
        hit
    };
    let Some(p) = found else { return };
    append_jsonl(TAPE_FILE, fill_record(&p, order_id, price, size, at));
}

fn arm_fill(order_id: &str, p: PendingFill) {
    let Ok(mut guard) = pending().lock() else { return };
    let (map, order) = &mut *guard;
    if map.insert(order_id.to_string(), p).is_none() {
        order.push_back(order_id.to_string());
    }
    while order.len() > PENDING_FILL_CAP {
        if let Some(oldest) = order.pop_front() {
            map.remove(&oldest);
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use rust_decimal_macros::dec;
    use std::time::Duration;

    fn stamped() -> OrderTimings {
        let base = Instant::now();
        OrderTimings {
            wall: 1_787_476_981.5,
            build_start: base,
            sign_done: Some(base + Duration::from_millis(3)),
            send: Some(base + Duration::from_micros(3_200)),
            ack: Some(base + Duration::from_millis(171)),
        }
    }

    #[test]
    fn decision_ids_are_unique() {
        let ids: std::collections::HashSet<String> =
            (0..1000).map(|_| next_decision_id()).collect();
        assert_eq!(ids.len(), 1000);
    }

    #[test]
    fn offsets_are_cumulative_from_build_start() {
        let t = stamped();
        assert_eq!(t.offset_ms(Some(t.build_start)), Some(0.0));
        let sign = t.offset_ms(t.sign_done).unwrap();
        let send = t.offset_ms(t.send).unwrap();
        let ack = t.offset_ms(t.ack).unwrap();
        assert!(sign <= send && send <= ack, "{sign} {send} {ack}");
        assert!((ack - 171.0).abs() < 0.01);
    }

    #[test]
    fn a_stage_never_reached_is_absent_not_zero() {
        let mut t = stamped();
        t.ack = None;
        assert_eq!(t.offset_ms(t.ack), None);
        let rec = placed_record("d1", "tok", "buy", dec!(0.98), dec!(25), "0xabc", false, &t);
        assert!(rec.get("sign_done_ms").is_some());
        assert!(
            rec.get("ack_ms").is_none(),
            "an un-acked order must not report an ack time"
        );
    }

    #[test]
    fn placed_record_shape() {
        let t = stamped();
        let rec = placed_record("d1", "tok7", "buy", dec!(0.98), dec!(25), "0xabc", false, &t);
        assert_eq!(rec["stage"], "ack");
        assert_eq!(rec["decision_id"], "d1");
        assert_eq!(rec["token"], "tok7");
        assert_eq!(rec["side"], "buy");
        assert_eq!(rec["price"], "0.98");
        assert_eq!(rec["size"], "25");
        assert_eq!(rec["order_id"], "0xabc");
        assert_eq!(rec["suppressed"], false);
        assert_eq!(rec["t"], 1_787_476_981.5);
        assert!((rec["ack_ms"].as_f64().unwrap() - 171.0).abs() < 0.01);
        // One line, one object — the tape is JSONL, not pretty JSON.
        assert!(!serde_json::to_string(&rec).unwrap().contains('\n'));
    }

    #[test]
    fn post_only_is_additive_and_absent_on_a_taker_line() {
        let t = stamped();
        let taker = placed_record("d1", "tok7", "buy", dec!(0.94), dec!(26), "0xabc", false, &t);
        assert!(
            taker.get("post_only").is_none(),
            "a taker line must read exactly as every line already on disk"
        );
        let maker = placed_record("d2", "tok7", "buy", dec!(0.98), dec!(25), "0xdef", true, &t);
        assert_eq!(maker["post_only"], true, "a resting quote says so");
        assert_eq!(maker["stage"], "ack", "and is otherwise an ordinary ack line");
        assert_eq!(maker["suppressed"], false);
    }

    #[test]
    fn suppressed_record_carries_no_http_timings() {
        let rec = suppressed_record("d2", "tok7", "buy", dec!(0.95), dec!(26), "0xdead");
        assert_eq!(rec["stage"], "suppressed");
        assert_eq!(rec["suppressed"], true);
        assert_eq!(rec["kept_order_id"], "0xdead");
        for k in ["sign_done_ms", "send_ms", "ack_ms", "order_id"] {
            assert!(rec.get(k).is_none(), "suppressed record leaked {k}");
        }
    }

    #[test]
    fn fill_record_inherits_the_decision_anchor() {
        let base = Instant::now();
        let p = PendingFill {
            decision_id: "d3".to_string(),
            token_id: "tok7".to_string(),
            wall: 1_787_476_981.5,
            build_start: base,
        };
        let rec = fill_record(&p, "0xabc", dec!(0.98), dec!(25), base + Duration::from_millis(2410));
        assert_eq!(rec["stage"], "fill");
        assert_eq!(rec["decision_id"], "d3");
        assert_eq!(rec["token"], "tok7");
        assert_eq!(rec["t"], 1_787_476_981.5);
        assert!((rec["fill_ms"].as_f64().unwrap() - 2410.0).abs() < 0.01);
    }

    #[test]
    fn pending_fill_map_is_bounded_and_evicts_oldest_first() {
        let base = Instant::now();
        let mk = |i: usize| PendingFill {
            decision_id: format!("d{i}"),
            token_id: "tok".to_string(),
            wall: 0.0,
            build_start: base,
        };
        let tag = format!("bound-{}", next_decision_id());
        for i in 0..(PENDING_FILL_CAP + 10) {
            arm_fill(&format!("{tag}-{i}"), mk(i));
        }
        let guard = pending().lock().unwrap();
        let (map, order) = &*guard;
        assert!(order.len() <= PENDING_FILL_CAP, "queue grew past the cap");
        assert!(map.len() <= PENDING_FILL_CAP, "map grew past the cap");
        assert!(
            !map.contains_key(&format!("{tag}-0")),
            "the oldest entry survived eviction"
        );
        assert!(
            map.contains_key(&format!("{tag}-{}", PENDING_FILL_CAP + 9)),
            "the newest entry was evicted"
        );
    }
}
