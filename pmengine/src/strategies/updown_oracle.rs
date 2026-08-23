//! Dynamic basis guard: a Chainlink poller per updown arm that tracks the
//! real oracle-vs-Binance basis live and can only RAISE the arm's
//! `basis_guard_bp` above the operator's offline-measured param, never
//! lower it — the measured basis distribution is non-stationary (BTC's
//! p95 basis halved between two consecutive days), so a guard set from a
//! 48h-old snapshot under- or over-trusts depending which regime printed
//! it. See `eval_model` in updown.rs for where the effective guard lands.
//!
//! No plain `pub struct` in this file on purpose: `pmstrat transpile --all`
//! registers the first `pub struct` it finds in every file it scans in
//! strategies/, so a stray one here would either get skipped (harmless) or,
//! worse, silently registered as a bogus strategy. Every public item here
//! is `pub(crate)`.
//!
//! One poller thread per ARM (not deduped by symbol) — an arm with two
//! windows on the same coin (e.g. btc 5m + btc 15m) runs two independent
//! pollers with independent rolling windows tonight. Dedupe-by-symbol
//! (share one poller + one OracleState per underlying coin across arms) is
//! a reasonable follow-up, not done here to keep this change's blast
//! radius to "one arm owns its own oracle state," matching how each arm
//! already owns its own Binance FeedState.

use super::updown::FeedState;
use std::collections::VecDeque;
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Arc, Mutex};

/// Rolling window length — ~30min at the 20s poll cadence.
pub(crate) const OBS_WINDOW: usize = 90;
/// Samples needed before the live estimate is trusted over the arm-time
/// param — mirrors updown.rs's SIGMA_SLOW_MIN pattern for the vol floor.
pub(crate) const OBS_MIN_SAMPLES: usize = 30;
const POLL_INTERVAL_S: u64 = 20;
const RPC_TIMEOUT_MS: u64 = 3000;
const SEL_LATEST_ROUND_DATA: &str = "0xfeaf968c";

/// RPC endpoints tried in order every poll; polygon-rpc.com LAST — it
/// currently 401s as the canonical gateway's public tier, so paying its
/// latency only after every free public fallback has already failed.
const RPC_URLS: [&str; 4] = [
    "https://polygon-bor-rpc.publicnode.com",
    "https://gateway.tenderly.co/public/polygon",
    "https://1rpc.io/matic",
    "https://polygon-rpc.com",
];

/// Shared oracle state an arm's Chainlink poller keeps warm, beside its
/// FeedState. `samples` is the rolling basis-magnitude window `live_guard_bp`
/// reads; the rest is poll-health diagnostics surfaced via `status`.
#[derive(Default)]
pub(crate) struct OracleState {
    pub(crate) samples: VecDeque<f64>,
    pub(crate) last_chainlink: Option<f64>,
    pub(crate) last_updated_at: i64,
    pub(crate) last_poll_at: f64,
    pub(crate) last_err: Option<String>,
}

struct ChainlinkFeed {
    address: &'static str,
    decimals: u32,
}

/// Polygon mainnet Chainlink proxy addresses — copied from
/// pmtrader/polymarket/chainlink.py's FEEDS map (verified live 2026-08-22).
fn feed_for_binance_symbol(symbol: &str) -> Option<ChainlinkFeed> {
    let (address, decimals) = match symbol.to_uppercase().as_str() {
        "BTCUSDT" => ("0xc907E116054Ad103354f2D350FD2514433D57F6f", 8),
        "ETHUSDT" => ("0xF9680D99D6C9589e2a93a78A04A279e509205945", 8),
        "SOLUSDT" => ("0x10C8264C0935b3B9870013e057f330Ff3e9C56dC", 8),
        "XRPUSDT" => ("0x785ba89291f676b5386652eB12b30cF361020694", 8),
        "DOGEUSDT" => ("0xbaf9327b6564454F4a3364C33eFeEf032b4b4444", 8),
        _ => return None,
    };
    Some(ChainlinkFeed { address, decimals })
}

/// Effective guard = the operator's param, RAISED (never lowered) to the
/// live basis p95 once enough samples exist. The param is the operator's
/// floor — it came from an offline-measured study and a replay-backed
/// change (see ROADMAP R1); loosening the guard below it needs a human and
/// a replay A/B, never an unattended live estimate. Below `min_samples`
/// (cold start / dead poller) the param governs alone, same shape as
/// updown.rs's `vol_floor_bp`.
pub(crate) fn live_guard_bp(samples: &[f64], param_bp: f64, min_samples: usize) -> f64 {
    if samples.len() < min_samples {
        return param_bp;
    }
    let mut sorted: Vec<f64> = samples.to_vec();
    sorted.sort_by(|a, b| a.partial_cmp(b).unwrap_or(std::cmp::Ordering::Equal));
    param_bp.max(percentile(&sorted, 95.0))
}

/// Linear-interpolation percentile, p in [0, 100]. `sorted_vals` must
/// already be sorted ascending and non-empty.
fn percentile(sorted_vals: &[f64], p: f64) -> f64 {
    let n = sorted_vals.len();
    if n == 1 {
        return sorted_vals[0];
    }
    let k = (p / 100.0) * (n - 1) as f64;
    let f = k.floor() as usize;
    let c = k.ceil() as usize;
    if f == c {
        return sorted_vals[f];
    }
    sorted_vals[f] * (c as f64 - k) + sorted_vals[c] * (k - f as f64)
}

/// Push one sample, evicting the oldest once the window exceeds OBS_WINDOW.
pub(crate) fn push_sample(win: &mut VecDeque<f64>, sample: f64) {
    win.push_back(sample);
    while win.len() > OBS_WINDOW {
        win.pop_front();
    }
}

/// Minute bucket (unix seconds, floored to :00) containing `updated_at` —
/// the FeedState.per_min key to look up for the lag-aligned mark.
pub(crate) fn updated_at_minute(updated_at: i64) -> i64 {
    updated_at.div_euclid(60) * 60
}

/// One basis sample: |chainlink/binance_mark - 1| * 1e4, LAG-ALIGNED.
///
/// Chainlink lags Binance (deviation-threshold + heartbeat updates, plus
/// its own venue aggregation) — comparing its answer against the live
/// spot during a fast move measures trend speed x oracle lag, not
/// settlement basis, which would inflate the guard exactly in the regimes
/// where banked-margin trades are best (the operator caught this before
/// this landed). So `binance_mark` must be Binance's mark for the MINUTE
/// CONTAINING the oracle's own `updatedAt` (FeedState.per_min at
/// `updated_at_minute(updated_at)`), never "now" — that minute is what the
/// oracle actually saw when it printed. Persistent venue basis survives
/// this alignment; the lag artifact mostly cancels.
///
/// None on a non-finite/non-positive mark (can't safely ratio).
pub(crate) fn basis_sample_bp(chainlink_price: f64, binance_mark: f64) -> Option<f64> {
    if !chainlink_price.is_finite() || !binance_mark.is_finite() || binance_mark <= 0.0 {
        return None;
    }
    Some((chainlink_price / binance_mark - 1.0).abs() * 1e4)
}

/// Chainlink's `answer` is decimals-scaled fixed point (all five feeds use
/// 8 decimals) — divide down to a plain USD float.
pub(crate) fn decode_price(answer: i128, decimals: u32) -> f64 {
    answer as f64 / 10f64.powi(decimals as i32)
}

/// Decode (answer, updatedAt) from a `latestRoundData()` eth_call result —
/// hand-rolled ABI decode (no web3 dep), mirroring
/// pmtrader/polymarket/chainlink.py's `_decode_round`. Layout: 5 x 32-byte
/// words (roundId, answer, startedAt, updatedAt, answeredInRound); we only
/// need the middle two.
fn decode_round_hex(hex_str: &str) -> Result<(i128, i64), String> {
    let s = hex_str.strip_prefix("0x").unwrap_or(hex_str);
    let bytes = hex::decode(s).map_err(|e| format!("hex decode: {}", e))?;
    if bytes.len() < 128 {
        return Err(format!("short round data: {} bytes", bytes.len()));
    }
    let mut answer_buf = [0u8; 16];
    answer_buf.copy_from_slice(&bytes[32 + 16..64]);
    // Two's-complement truncation to the low 16 bytes is exact for any
    // magnitude that fits i128 — true for every real price this ever sees.
    let answer = i128::from_be_bytes(answer_buf);

    let mut updated_buf = [0u8; 8];
    updated_buf.copy_from_slice(&bytes[96 + 24..128]);
    let updated_at = u64::from_be_bytes(updated_buf) as i64;

    Ok((answer, updated_at))
}

fn eth_call(client: &reqwest::blocking::Client, url: &str, address: &str) -> Result<String, String> {
    let body = serde_json::json!({
        "jsonrpc": "2.0", "id": 1, "method": "eth_call",
        "params": [{"to": address, "data": SEL_LATEST_ROUND_DATA}, "latest"],
    });
    let resp: serde_json::Value = client
        .post(url)
        .json(&body)
        .send()
        .map_err(|e| format!("send: {}", e))?
        .error_for_status()
        .map_err(|e| format!("status: {}", e))?
        .json()
        .map_err(|e| format!("json: {}", e))?;
    if let Some(err) = resp.get("error") {
        return Err(format!("rpc error: {}", err));
    }
    resp.get("result")
        .and_then(|v| v.as_str())
        .map(|s| s.to_string())
        .ok_or_else(|| "no result field".to_string())
}

/// Try every RPC endpoint in order, returning the first success. Never
/// panics — every failure mode (timeout, 401, malformed body) becomes an
/// Err that the poller records and moves past.
fn eth_call_latest_round_data(client: &reqwest::blocking::Client, address: &str) -> Result<String, String> {
    let mut last_err = String::new();
    for url in RPC_URLS {
        match eth_call(client, url, address) {
            Ok(hex) => return Ok(hex),
            Err(e) => last_err = format!("{}: {}", url, e),
        }
    }
    Err(format!("all RPC endpoints failed, last: {}", last_err))
}

fn poll_once(client: &reqwest::blocking::Client, feed: &ChainlinkFeed) -> Result<(f64, i64), String> {
    let hex_result = eth_call_latest_round_data(client, feed.address)?;
    let (answer, updated_at) = decode_round_hex(&hex_result)?;
    Ok((decode_price(answer, feed.decimals), updated_at))
}

/// Append one JSONL basis-sample record to ~/.pmt/engine/oracle-tape.jsonl
/// — the recorded corpus a future replay A/B of this feature needs (see
/// `book_tape` in updown.rs for the identical append-only pattern).
fn oracle_tape(record: serde_json::Value) {
    use std::io::Write;
    let Ok(home) = std::env::var("HOME") else { return };
    let dir = std::path::PathBuf::from(home).join(".pmt/engine");
    let _ = std::fs::create_dir_all(&dir);
    if let Ok(mut f) = std::fs::OpenOptions::new()
        .create(true)
        .append(true)
        .open(dir.join("oracle-tape.jsonl"))
    {
        let _ = writeln!(f, "{}", record);
    }
}

fn unix_now() -> f64 {
    std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map(|d| d.as_secs_f64())
        .unwrap_or(0.0)
}

/// Spawn the poller thread for one arm. Pushed into the same
/// `feed_handles` Vec as the Binance threads, so `ArmState::stop_feed`'s
/// existing join loop already covers its lifecycle — no separate teardown
/// path needed.
pub(crate) fn spawn_poller(
    oracle: Arc<Mutex<OracleState>>,
    feed: Arc<Mutex<FeedState>>,
    stop: Arc<AtomicBool>,
    symbol: String,
    end: f64,
) -> std::thread::JoinHandle<()> {
    std::thread::spawn(move || {
        let Some(chainlink_feed) = feed_for_binance_symbol(&symbol) else {
            oracle.lock().unwrap().last_err = Some(format!("no chainlink feed for {}", symbol));
            return;
        };
        let client = match reqwest::blocking::Client::builder()
            .timeout(std::time::Duration::from_millis(RPC_TIMEOUT_MS))
            .build()
        {
            Ok(c) => c,
            Err(e) => {
                oracle.lock().unwrap().last_err = Some(format!("oracle client: {}", e));
                return;
            }
        };
        while !stop.load(Ordering::SeqCst) {
            let now = unix_now();
            if now > end + 30.0 {
                break;
            }
            match poll_once(&client, &chainlink_feed) {
                Ok((chainlink_price, updated_at)) => {
                    let minute = updated_at_minute(updated_at);
                    let mark = feed.lock().unwrap().per_min.get(&minute).copied();
                    let sample = mark.and_then(|m| basis_sample_bp(chainlink_price, m));
                    {
                        let mut o = oracle.lock().unwrap();
                        o.last_chainlink = Some(chainlink_price);
                        o.last_updated_at = updated_at;
                        o.last_poll_at = now;
                        o.last_err = None;
                        if let Some(basis_bp) = sample {
                            push_sample(&mut o.samples, basis_bp);
                        }
                    }
                    if let (Some(mark), Some(basis_bp)) = (mark, sample) {
                        oracle_tape(serde_json::json!({
                            "t": now, "sym": symbol, "chainlink": chainlink_price,
                            "updated_at": updated_at, "binance_mark": mark, "basis_bp": basis_bp,
                        }));
                    }
                    // else: minute still forming or out of FeedState's
                    // reach-back window — skip the sample, not an error.
                }
                Err(e) => {
                    oracle.lock().unwrap().last_err = Some(e);
                }
            }
            // Sleep in 1s ticks rather than one 20s sleep so stop_feed's
            // join (called synchronously from the disarm command handler)
            // isn't blocked for up to POLL_INTERVAL_S.
            let mut waited = 0u64;
            while waited < POLL_INTERVAL_S {
                if stop.load(Ordering::SeqCst) {
                    return;
                }
                std::thread::sleep(std::time::Duration::from_secs(1));
                waited += 1;
            }
        }
    })
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn live_guard_floors_below_min_samples() {
        let samples = vec![50.0; 29];
        assert_eq!(
            live_guard_bp(&samples, 3.0, 30),
            3.0,
            "29 < min_samples 30 — param alone governs even though raw samples imply a much higher p95"
        );
    }

    #[test]
    fn live_guard_raises_above_param_when_p95_higher() {
        let samples: Vec<f64> = (0..30).map(|i| i as f64).collect(); // 0..29
        let g = live_guard_bp(&samples, 3.0, 30);
        assert!(g > 3.0, "got {g}");
    }

    #[test]
    fn live_guard_never_lowers_below_param() {
        let samples = vec![1.0; 30]; // p95 = 1.0, well under the param
        assert_eq!(live_guard_bp(&samples, 3.0, 30), 3.0, "the param is a floor, never loosened");
    }

    #[test]
    fn live_guard_min_samples_boundary_is_inclusive() {
        let below = vec![10.0; 29];
        assert_eq!(live_guard_bp(&below, 3.0, 30), 3.0);
        let at = vec![10.0; 30];
        assert_eq!(live_guard_bp(&at, 3.0, 30), 10.0, "exactly min_samples raises");
    }

    #[test]
    fn push_sample_trims_to_window_fifo() {
        let mut win: VecDeque<f64> = VecDeque::new();
        for i in 0..(OBS_WINDOW + 10) {
            push_sample(&mut win, i as f64);
        }
        assert_eq!(win.len(), OBS_WINDOW);
        assert_eq!(*win.front().unwrap(), 10.0, "oldest 10 evicted");
        assert_eq!(*win.back().unwrap(), (OBS_WINDOW + 9) as f64);
    }

    #[test]
    fn updated_at_minute_floors_to_the_minute() {
        assert_eq!(updated_at_minute(1_787_442_075), 1_787_442_060);
        assert_eq!(updated_at_minute(1_787_442_060), 1_787_442_060);
        assert_eq!(updated_at_minute(1_787_442_119), 1_787_442_060);
    }

    #[test]
    fn basis_sample_math_matches_bp_definition() {
        assert!((basis_sample_bp(101.0, 100.0).unwrap() - 100.0).abs() < 1e-9);
        assert!((basis_sample_bp(99.5, 100.0).unwrap() - 50.0).abs() < 1e-9, "abs — sign doesn't matter");
        assert_eq!(basis_sample_bp(100.0, 0.0), None, "no safe ratio through zero");
        assert_eq!(basis_sample_bp(f64::NAN, 100.0), None);
        assert_eq!(basis_sample_bp(100.0, f64::NAN), None);
    }

    #[test]
    fn decode_price_scales_by_decimals() {
        assert_eq!(decode_price(650_000_000_000, 8), 6500.0);
        assert!((decode_price(123_456_789, 8) - 1.23456789).abs() < 1e-9);
        assert_eq!(decode_price(-100_000_000, 8), -1.0, "sign path, even though real prices are positive");
    }

    fn word32_signed(v: i128) -> [u8; 32] {
        let mut w = [0u8; 32];
        if v < 0 {
            for b in w[0..16].iter_mut() {
                *b = 0xFF;
            }
        }
        w[16..32].copy_from_slice(&v.to_be_bytes());
        w
    }

    fn word32_unsigned(v: u64) -> [u8; 32] {
        let mut w = [0u8; 32];
        w[24..32].copy_from_slice(&v.to_be_bytes());
        w
    }

    #[test]
    fn decode_round_hex_roundtrips_answer_and_updated_at() {
        let mut bytes = Vec::new();
        bytes.extend_from_slice(&word32_unsigned(42)); // round_id
        bytes.extend_from_slice(&word32_signed(650_000_000_000)); // answer
        bytes.extend_from_slice(&word32_unsigned(1_787_442_000)); // started_at
        bytes.extend_from_slice(&word32_unsigned(1_787_442_060)); // updated_at
        bytes.extend_from_slice(&word32_unsigned(42)); // answered_in_round
        let hex_str = format!("0x{}", hex::encode(bytes));
        let (answer, updated_at) = decode_round_hex(&hex_str).unwrap();
        assert_eq!(answer, 650_000_000_000);
        assert_eq!(updated_at, 1_787_442_060);
    }

    #[test]
    fn decode_round_hex_handles_negative_answer() {
        let mut bytes = Vec::new();
        bytes.extend_from_slice(&word32_unsigned(1));
        bytes.extend_from_slice(&word32_signed(-500));
        bytes.extend_from_slice(&word32_unsigned(0));
        bytes.extend_from_slice(&word32_unsigned(100));
        bytes.extend_from_slice(&word32_unsigned(1));
        let hex_str = format!("0x{}", hex::encode(bytes));
        let (answer, updated_at) = decode_round_hex(&hex_str).unwrap();
        assert_eq!(answer, -500);
        assert_eq!(updated_at, 100);
    }

    #[test]
    fn decode_round_hex_rejects_short_data() {
        assert!(decode_round_hex("0x1234").is_err());
    }

    #[test]
    fn feed_lookup_matches_known_symbols_case_insensitively() {
        assert!(feed_for_binance_symbol("BTCUSDT").is_some());
        assert!(feed_for_binance_symbol("btcusdt").is_some());
        assert_eq!(feed_for_binance_symbol("BTCUSDT").unwrap().decimals, 8);
        assert!(feed_for_binance_symbol("NOPEUSDT").is_none());
    }
}
