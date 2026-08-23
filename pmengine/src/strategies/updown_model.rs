//! Pure math and gate predicates for the updown strategy's pricing model —
//! split out of updown.rs so the decision-orchestration file (Updown,
//! ArmState, decide()/tick()/feeds/rolls/commands) stays a readable size.
//! Every function here is a pure function of its arguments (no I/O, no
//! locks, no wall-clock reads) — that purity is what lets `decide()` and
//! replay.rs's `full` mode call the exact same `eval_model`.
//!
//! No plain `pub struct` in this file on purpose: `pmstrat transpile --all`
//! registers the first `pub struct` it finds in every file it scans in
//! strategies/, so a stray one here would either get skipped (harmless) or,
//! worse, silently registered as a bogus strategy. Every public item here
//! is `pub(crate)`.

use crate::strategies::updown::ArmParams;

/// Spot older than this is a dead feed — hold, never trade through it.
/// pub(crate): updown.rs's `twap_stale_feed_refuses` test constructs a
/// deliberately-stale `now` off this same threshold.
pub(crate) const MAX_SPOT_AGE_S: f64 = 5.0;
/// Live vol floor: minutes of trailing 1m closes for the fast estimate.
const VOL_FAST_WINDOW: usize = 12;
/// Slow trailing window (minutes) that supersedes the arm-time sigma param —
/// roll chains otherwise freeze a floor measured hours ago in a dead regime.
const SIGMA_SLOW_WINDOW: usize = 45;
/// Minimum close samples before the live slow estimate is trusted over the
/// arm-time param.
const SIGMA_SLOW_MIN: usize = 30;
/// Book-distrust brake: net above this is the book pricing in something the
/// model hasn't caught, not free edge — every blown-up window tonight
/// entered on huge claimed edge into a collapsing book. Banked-decided
/// TWAPs are exempt: the edge there is math, not a book mispricing.
const BOOK_DISTRUST_NET: f64 = 0.15;
/// No-averaging-down brake: our side getting cheaper after a clip is the
/// market repricing against the thesis, not a discount to chase.
const AVG_DOWN_TOL: f64 = 0.02;

// --- durable-tape event-type consts -------------------------------------
// Shared between updown.rs's record construction and replay.rs's matching
// so the two can't drift silently.
pub(crate) const EV_EVAL: &str = "eval";
pub(crate) const EV_FIRE: &str = "fire";
pub(crate) const EV_GATED: &str = "gated";
pub(crate) const EV_EXIT: &str = "exit";
pub(crate) const EV_CLEANUP: &str = "cleanup";
pub(crate) const EV_ROLL: &str = "roll";
pub(crate) const EV_BOOK: &str = "book";

/// `~/.pmt/engine` — the durable state directory every cross-session writer
/// shares (the tapes, the arm store). None when $HOME is unset.
pub(crate) fn engine_dir() -> Option<std::path::PathBuf> {
    std::env::var("HOME")
        .ok()
        .map(|h| std::path::PathBuf::from(h).join(".pmt/engine"))
}

/// Append one JSONL record to `~/.pmt/engine/<file_name>` — shared by every
/// durable-tape writer (eval/fire tape, book/spot recorder, oracle basis
/// samples). Silently no-ops if $HOME is unset or the write fails; a lost
/// tape line must never be allowed to impact live trading.
pub(crate) fn append_jsonl(file_name: &str, record: serde_json::Value) {
    use std::io::Write;
    let Some(dir) = engine_dir() else { return };
    let _ = std::fs::create_dir_all(&dir);
    if let Ok(mut f) = std::fs::OpenOptions::new()
        .create(true)
        .append(true)
        .open(dir.join(file_name))
    {
        let _ = writeln!(f, "{}", record);
    }
}

/// Public Binance market-data host — no key, no rate-limit headaches.
pub(crate) const BINANCE_DATA: &str = "https://data-api.binance.vision";
/// How far before a window's start the 1m kline fetch reaches back. The
/// regime autocorr needs real history or rho pins at 0 and the chop gate
/// goes dead — and replay must reach back exactly as far as the live feed
/// did, or it reconstructs a different rho than the one that traded.
pub(crate) const KLINE_LOOKBACK_S: i64 = 2700;

/// One 1m Binance kline reduced to what the model reads: bucket epoch,
/// open, close.
#[derive(Debug, Clone, Copy, PartialEq)]
pub(crate) struct Kline {
    pub(crate) t: i64,
    pub(crate) o: f64,
    pub(crate) c: f64,
}

impl Kline {
    /// (open+close)/2 — the per-minute mark that proxies the Chainlink 60s
    /// TWAP. Every banked minute in the model is one of these.
    pub(crate) fn mid(&self) -> f64 {
        (self.o + self.c) / 2.0
    }
}

/// Shape a raw Binance `/api/v3/klines` array into `Kline`s, dropping rows
/// that don't parse or price at zero.
///
/// The live feed poller and the replay corpus fetcher each did this parse
/// inline — same field offsets, same zero filter, two copies. A drift in
/// either would have silently de-synced replay from the code it exists to
/// judge, so both call this now.
pub(crate) fn shape_klines(v: &serde_json::Value) -> Vec<Kline> {
    let mut out = Vec::new();
    for k in v.as_array().unwrap_or(&Vec::new()) {
        let t = k[0].as_i64().unwrap_or(0) / 1000;
        let o: f64 = k[1].as_str().and_then(|s| s.parse().ok()).unwrap_or(0.0);
        let c: f64 = k[4].as_str().and_then(|s| s.parse().ok()).unwrap_or(0.0);
        if o > 0.0 && c > 0.0 {
            out.push(Kline { t, o, c });
        }
    }
    out
}

/// Shared state an arm's Binance feed threads keep warm. In replay the
/// corpus loader builds one of these per recorded sample instead.
#[derive(Default)]
pub(crate) struct FeedState {
    pub(crate) spot: f64,
    pub(crate) spot_ts: f64,
    /// minute epoch -> (open+close)/2, proxying the Chainlink 60s TWAP.
    pub(crate) per_min: std::collections::BTreeMap<i64, f64>,
    /// Exact 1h candle open once the close_open window starts.
    pub(crate) candle_open: Option<f64>,
    /// Recent 1m closes, oldest first — feeds fast vol + regime autocorr.
    pub(crate) closes: Vec<f64>,
    /// Lag-1 autocorrelation of trailing 1m returns. Negative = the tape
    /// fades its own moves; momentum "locks" are mirages there.
    pub(crate) rho: f64,
    pub(crate) last_err: Option<String>,
}

/// One model read: everything the clip engine needs to pick a mode.
#[derive(Debug, Clone)]
pub(crate) struct ModelEval {
    pub(crate) p_up: f64,
    pub(crate) sig_bp: f64,
    /// twap only: the banked contribution alone decides the window even if
    /// the remaining path fully reverts, with basis + vol cushion. The one
    /// entry condition immune to mean reversion.
    pub(crate) banked_decided: bool,
    /// Stronger: the banked margin survives even an adversarial spot push
    /// (manip_push_bp) sustained for the whole remaining window. When true,
    /// the book's late panic/push prices are free money — nobody can flip
    /// this TWAP anymore.
    pub(crate) flip_proof: bool,
    pub(crate) rho: f64,
    /// R9 safety-gate inputs, recorded on every tick AND gating live (see
    /// `safety_gate_blocks`, wired into decide()'s first-clip brake):
    /// projected full-window margin, the locked banked contribution, and the
    /// 1σ-scale residual of the unlocked piece. Per-side safety is
    /// `side_safety` — signed, not |banked|/cushion: banked mass pointing
    /// the wrong way has to count AGAINST that side, not for it.
    pub(crate) margin_bp: f64,
    pub(crate) banked_margin_bp: f64,
    pub(crate) cushion_bp: f64,
    /// The basis guard actually enforced this eval — p.basis_guard_bp,
    /// possibly raised (never lowered) by the live Chainlink-vs-Binance
    /// oracle read. See `updown_oracle::live_guard_bp`. Recorded on every
    /// eval (not just twap gate checks) so the tape always shows what
    /// threshold was live, even for close_open evals that don't gate on it.
    pub(crate) guard_bp: f64,
}

/// Why an eval refused, as data rather than prose.
///
/// The basis guard used to format margin/banked/cushion into the error
/// STRING and two Python consumers (`cli_crypto._MARGIN_RE`,
/// `shadow._MARGIN_RE`) regexed them back out — any reword broke both
/// silently, with no compiler or test anywhere in the loop. The numbers now
/// travel as fields; `reason` stays the same human sentence so the durable
/// tape's `reason` key and every existing consumer of it keep working.
/// `None` on the numeric fields = a gate that has no margin to report
/// (stale feed, reference print missing).
#[derive(Debug, Clone, PartialEq)]
pub(crate) struct GateReason {
    pub(crate) reason: String,
    pub(crate) margin_bp: Option<f64>,
    pub(crate) banked_bp: Option<f64>,
    pub(crate) cushion_bp: Option<f64>,
    pub(crate) guard_bp: Option<f64>,
}

impl GateReason {
    /// A gate with no numbers behind it — stale feed, missing print.
    pub(crate) fn plain(reason: impl Into<String>) -> Self {
        Self {
            reason: reason.into(),
            margin_bp: None,
            banked_bp: None,
            cushion_bp: None,
            guard_bp: None,
        }
    }
}

/// Lets `?` on a plain `&str` gate (`ok_or("...")?`) build a GateReason.
impl From<&str> for GateReason {
    fn from(reason: &str) -> Self {
        Self::plain(reason)
    }
}

/// Replay-only policy knobs. Live arms always run the defaults — the brake
/// constants are the law in production and are NOT reachable from the arm
/// command. The A/B harness needs the pre-brake policy expressible to
/// reproduce recorded nights; that is this struct's only reason to exist.
#[derive(Debug, Clone, Copy)]
pub(crate) struct Tunables {
    pub(crate) distrust_net: f64,
    pub(crate) avg_down_tol: f64,
}

impl Default for Tunables {
    fn default() -> Self {
        Self { distrust_net: BOOK_DISTRUST_NET, avg_down_tol: AVG_DOWN_TOL }
    }
}

/// The pricing model as a pure function of (params, feed snapshot, t,
/// effective guard) — the live tick locks the feed, computes the live-
/// raised guard on the ArmState side, and delegates; replay hands in a
/// feed state reconstructed from the corpus and passes p.basis_guard_bp
/// unchanged (see src/replay.rs). `effective_guard_bp` is p.basis_guard_bp
/// itself, or higher if the live oracle poller has raised it — never
/// lower. Errors = gated.
pub(crate) fn eval_model(
    p: &ArmParams,
    f: &FeedState,
    now: f64,
    effective_guard_bp: f64,
) -> Result<ModelEval, GateReason> {
    {
        if now - f.spot_ts > MAX_SPOT_AGE_S {
            return Err(GateReason::plain(match &f.last_err {
                Some(e) => format!("feed stale: {}", e),
                None => "feed stale".to_string(),
            }));
        }
        let spot = f.spot;
        // Floor = live 45m trailing sigma once the feed holds real history
        // (the arm-time param is only the cold-start fallback); the fast
        // window still catches the storm either trailing estimate lags.
        let fast_bp = trailing_sigma_bp(&f.closes, VOL_FAST_WINDOW);
        let slow_bp = trailing_sigma_bp(&f.closes, SIGMA_SLOW_WINDOW);
        let sig_bp = vol_floor_bp(slow_bp, f.closes.len(), p.sigma_bp_per_min).max(fast_bp);
        let sig_frac = sig_bp / 1e4;
        let rho = f.rho;

        if p.kind == "close_open" {
            let open = f.candle_open.ok_or("candle open not printed yet")?;
            let t_min = ((p.end - now) / 60.0).max(0.005);
            let z = (spot / open).ln() / (sig_frac * t_min.sqrt());
            Ok(ModelEval {
                p_up: norm_cdf(z), sig_bp, banked_decided: false, flip_proof: false, rho,
                margin_bp: (spot / open - 1.0) * 1e4, banked_margin_bp: 0.0, cushion_bp: 0.0,
                guard_bp: effective_guard_bp,
            })
        } else if p.settle_rule == "terminal" {
            let ref_px = *f
                .per_min
                .get(&(p.start as i64 - 60))
                .ok_or("range-start reference not printed yet")?;
            eval_terminal(p, now, spot, ref_px, sig_frac, sig_bp, rho, effective_guard_bp)
        } else if p.settle_rule == "hybrid" {
            let ref_px = *f
                .per_min
                .get(&(p.start as i64 - 60))
                .ok_or("range-start reference not printed yet")?;
            eval_hybrid(p, f, now, spot, ref_px, sig_frac, sig_bp, rho, effective_guard_bp)
        } else {
            let ref_px = *f
                .per_min
                .get(&(p.start as i64 - 60))
                .ok_or("range-start reference not printed yet")?;
            eval_range_avg(p, f, now, spot, ref_px, sig_frac, sig_bp, rho, effective_guard_bp)
        }
    }
}

/// The original whole-range-average model. Its "banked mass" is settlement
/// arithmetic only under a range-avg rule — under the true terminal rule it
/// is a MOMENTUM PROXY, one that measurably works at 5m and lies with
/// duration (docs/LESSONS.md). Kept verbatim as both the live 5m spec and
/// the evidence half of the hybrid spec.
#[allow(clippy::too_many_arguments)]
fn eval_range_avg(
    p: &ArmParams,
    f: &FeedState,
    now: f64,
    spot: f64,
    ref_px: f64,
    sig_frac: f64,
    sig_bp: f64,
    rho: f64,
    effective_guard_bp: f64,
) -> Result<ModelEval, GateReason> {
    {
        {
            let banked: Vec<f64> = f
                .per_min
                .iter()
                .filter(|(t, _)| **t as f64 >= p.start && (**t as f64) < (now - 30.0).min(p.end))
                .map(|(_, v)| *v)
                .collect();
            let banked_s = banked.len() as f64 * 60.0;
            let banked_avg = if banked.is_empty() {
                spot
            } else {
                banked.iter().sum::<f64>() / banked.len() as f64
            };
            let rem = (p.end - now).max(0.0);
            let window = banked_s + rem;
            if window <= 0.0 || rem <= 0.0 {
                let p_up = if banked_avg >= ref_px { 1.0 } else { 0.0 };
                let m = (banked_avg / ref_px - 1.0) * 1e4;
                return Ok(ModelEval {
                    p_up, sig_bp, banked_decided: true, flip_proof: true, rho,
                    margin_bp: m, banked_margin_bp: m, cushion_bp: effective_guard_bp,
                    guard_bp: effective_guard_bp,
                });
            }
            let proj = (banked_avg * banked_s + spot * rem) / window;
            let margin_bp = (proj / ref_px - 1.0) * 1e4;
            // Banked-decided inputs, computed before the guard so gated ticks
            // still record them — the R9 safety-gate sweep needs the corpus
            // to know what |banked|/cushion was while the flat guard held.
            let banked_margin_bp = (banked_avg / ref_px - 1.0) * 1e4 * (banked_s / window);
            let cushion_bp = effective_guard_bp
                + sig_bp * ((rem / 60.0).max(0.02) / 3.0).sqrt() * (rem / window);
            if margin_bp.abs() < effective_guard_bp {
                return Err(GateReason {
                    reason: format!(
                        "basis guard: projected margin {:+.1}bp inside {:.1}bp noise band [banked {:+.1}bp cushion {:.1}bp]",
                        margin_bp, effective_guard_bp, banked_margin_bp, cushion_bp
                    ),
                    margin_bp: Some(margin_bp),
                    banked_bp: Some(banked_margin_bp),
                    cushion_bp: Some(cushion_bp),
                    guard_bp: Some(effective_guard_bp),
                });
            }
            let breakeven = (ref_px * window - banked_avg * banked_s) / rem;
            let sig_avg = sig_frac * ((rem / 60.0).max(0.02) / 3.0).sqrt();
            // breakeven <= 0: the banked mass is so far above the reference
            // that NO positive remaining path can pull the average back —
            // up has already won. Without this guard, ln(negative) is NaN,
            // NaN comparisons all read false, and f64::min's NaN-eating
            // then priced BOTH sides at fair 1.0 simultaneously (adversarial
            // sweep 2026-08-23, compiled repro: 5% push on a heavily-banked
            // window fired clips on both outcomes at once).
            let p_up = if breakeven <= 0.0 {
                1.0
            } else {
                1.0 - norm_cdf((breakeven / spot).ln() / sig_avg)
            };
            debug_assert!(p_up.is_finite());
            // Banked-decided: the banked contribution alone survives a full
            // reversion of the remaining path to the reference, with basis
            // noise + one sigma of remaining-average cushion on top.
            let banked_decided =
                banked_margin_bp.abs() > cushion_bp && (banked_margin_bp > 0.0) == (p_up > 0.5);
            // Flip-proof: survives basis noise PLUS a full-remaining-window
            // adversarial push. rem/window scales the push's TWAP influence.
            // Uses effective_guard_bp too (not just the design brief's named
            // three sites) — a fourth p.basis_guard_bp read lived here;
            // leaving it on the stale param would let a raised live guard
            // still wave flip clips through quiesce on noise it no longer
            // trusts.
            let flip_proof = banked_decided
                && banked_margin_bp.abs()
                    > effective_guard_bp + p.manip_push_bp * (rem / window);
            Ok(ModelEval {
                p_up, sig_bp, banked_decided, flip_proof, rho,
                margin_bp, banked_margin_bp, cushion_bp, guard_bp: effective_guard_bp,
            })
        }
    }
}

/// Marketable-limit price for a clip: the decision ask plus a chase
/// buffer funded ONLY by surplus edge above the floor — a fill at the
/// worst case limit still clears edge_req. max_price stays the hard cap.
pub(crate) fn pay_up_limit(ask: f64, net: f64, edge_req: f64, pay_up_max: f64, max_price: f64) -> f64 {
    (ask + (net - edge_req).max(0.0).min(pay_up_max)).min(max_price)
}

/// Maker step 0's resting-bid price (docs/maker-design.md §6 step 0).
///
/// Three clamps, tightest wins: the edge floor still has to hold at the
/// price we'd actually pay — and a maker pays NO fee, so there is no fee
/// term here the way `pay_up_limit`'s caller has one; we improve the book
/// by at most one tick, never leaping the queue we cannot see; and
/// `max_price` is the same hard cap a clip has.
///
/// The result is floored onto the `tick` grid because the pure core has no
/// per-market tick size. The order path floors again to the market's real
/// tick, so a coarser market can only make the bid CHEAPER, never richer.
pub(crate) fn maker_bid_price(
    fair: f64,
    edge_req: f64,
    best_bid: f64,
    max_price: f64,
    tick: f64,
) -> f64 {
    let px = (fair - edge_req).min(best_bid + tick).min(max_price);
    if tick <= 0.0 {
        return px;
    }
    // The epsilon is load-bearing: 0.985/0.001 is 984.999999999999886 in
    // binary, and a bare floor would shave a whole tick off every price
    // that already sits exactly ON the grid.
    let ticks = (px / tick + 1e-9).floor();
    ((ticks * tick) * 1e6).round() / 1e6
}

/// Settlement TWAP width for a window: 30s for 5m markets, 60s otherwise
/// (the post-2026-08-07 Chainlink stream widths).
pub(crate) fn settle_tw_secs(window_secs: f64) -> f64 {
    if window_secs <= 300.0 { 30.0 } else { 60.0 }
}

/// What the terminal rule has actually locked at `rem` seconds out:
/// (banked_bp, cushion_bp, horizon_min). Before the settlement window
/// opens nothing is locked and the cushion is 1σ diffusion to the TWAP's
/// center of mass (~tw/2 before the wire) plus the oracle guard. Inside
/// the forming TWAP the elapsed share locks at ~spot (the minute-grain
/// feed can't resolve finer; spot is the live stream's best proxy) and
/// the cushion is residual diffusion of the unformed share.
fn terminal_lock(rem: f64, tw: f64, margin_bp: f64, sig_frac: f64,
                 effective_guard_bp: f64) -> (f64, f64, f64) {
    if rem > tw {
        let h = ((rem - tw / 2.0) / 60.0).max(0.02);
        (0.0, effective_guard_bp + sig_frac * h.sqrt() * 1e4, h)
    } else {
        let locked_frac = ((tw - rem) / tw).clamp(0.0, 1.0);
        let h = (rem / 60.0).max(0.02);
        let banked = margin_bp * locked_frac;
        let cushion = effective_guard_bp
            + sig_frac * (h / 3.0).sqrt() * 1e4 * (1.0 - locked_frac);
        (banked, cushion, h)
    }
}

/// Hybrid spec: evidence from momentum, risk from settlement arithmetic.
/// The range-avg proxy supplies p_up / margin / banked evidence (what the
/// wallet proved at 5m), while cushion, banked_decided and flip_proof come
/// from the TERMINAL rule — so the theta gate divides momentum evidence by
/// what can genuinely still be undone at the wire, and the brakes' "decided"
/// waivers only open on real lock (range history called 13.2% of 15m
/// windows decided on mass the terminal rule hadn't banked at all).
#[allow(clippy::too_many_arguments)]
fn eval_hybrid(
    p: &ArmParams,
    f: &FeedState,
    now: f64,
    spot: f64,
    ref_px: f64,
    sig_frac: f64,
    sig_bp: f64,
    rho: f64,
    effective_guard_bp: f64,
) -> Result<ModelEval, GateReason> {
    let rem = (p.end - now).max(0.0);
    let tw = settle_tw_secs(p.end - p.start);
    if rem <= 0.0 {
        // At/past the wire only the terminal read is the settlement.
        return eval_terminal(p, now, spot, ref_px, sig_frac, sig_bp, rho, effective_guard_bp);
    }
    let r = eval_range_avg(p, f, now, spot, ref_px, sig_frac, sig_bp, rho, effective_guard_bp)?;
    let term_margin_bp = (spot / ref_px - 1.0) * 1e4;
    let (t_banked, t_cushion, _) =
        terminal_lock(rem, tw, term_margin_bp, sig_frac, effective_guard_bp);
    let banked_decided = t_banked.abs() > t_cushion && (t_banked > 0.0) == (r.p_up > 0.5);
    let flip_proof = banked_decided
        && t_banked.abs() > effective_guard_bp + p.manip_push_bp * (rem / tw).min(1.0);
    Ok(ModelEval {
        p_up: r.p_up, sig_bp, banked_decided, flip_proof, rho,
        margin_bp: r.margin_bp, banked_margin_bp: r.banked_margin_bp,
        cushion_bp: t_cushion, guard_bp: effective_guard_bp,
    })
}

/// Terminal-rule pricing: these markets settle on the 60s-TWAP stream's
/// value at range END vs its value at range start — NOT the whole-range
/// average (verified 2026-08-23 against 3,193 resolutions; see
/// docs/LESSONS.md). Consequences priced here:
///   - Before the settlement window opens, NOTHING is locked: p_up is
///     pure diffusion of spot to the TWAP's center of mass, margin is
///     spot-vs-ref, banked/safety are honestly zero (the old "banked
///     mass" was a momentum proxy, not settlement arithmetic).
///   - Inside the final TW seconds, the forming settlement TWAP locks
///     linearly: banked = the elapsed share of the TWAP already beyond
///     reach, cushion = residual diffusion of the unformed share. The
///     banked_decided / flip_proof semantics regain literal truth here.
#[allow(clippy::too_many_arguments)]
fn eval_terminal(
    p: &ArmParams,
    now: f64,
    spot: f64,
    ref_px: f64,
    sig_frac: f64,
    sig_bp: f64,
    rho: f64,
    effective_guard_bp: f64,
) -> Result<ModelEval, GateReason> {
    let rem = (p.end - now).max(0.0);
    let tw = settle_tw_secs(p.end - p.start);
    let margin_bp = (spot / ref_px - 1.0) * 1e4;

    if rem <= 0.0 {
        let p_up = if spot >= ref_px { 1.0 } else { 0.0 };
        return Ok(ModelEval {
            p_up, sig_bp, banked_decided: true, flip_proof: true, rho,
            margin_bp, banked_margin_bp: margin_bp, cushion_bp: effective_guard_bp,
            guard_bp: effective_guard_bp,
        });
    }

    let (banked_margin_bp, cushion_bp, horizon_min) =
        terminal_lock(rem, tw, margin_bp, sig_frac, effective_guard_bp);

    if margin_bp.abs() < effective_guard_bp {
        return Err(GateReason {
            reason: format!(
                "basis guard: terminal margin {:+.1}bp inside {:.1}bp noise band [banked {:+.1}bp cushion {:.1}bp]",
                margin_bp, effective_guard_bp, banked_margin_bp, cushion_bp
            ),
            margin_bp: Some(margin_bp),
            banked_bp: Some(banked_margin_bp),
            cushion_bp: Some(cushion_bp),
            guard_bp: Some(effective_guard_bp),
        });
    }

    let p_up = if ref_px <= 0.0 || spot <= 0.0 {
        0.5
    } else {
        1.0 - norm_cdf((ref_px / spot).ln() / (sig_frac * horizon_min.sqrt()))
    };
    debug_assert!(p_up.is_finite());
    let banked_decided =
        banked_margin_bp.abs() > cushion_bp && (banked_margin_bp > 0.0) == (p_up > 0.5);
    let flip_proof = banked_decided
        && banked_margin_bp.abs()
            > effective_guard_bp + p.manip_push_bp * (rem / tw).min(1.0);
    Ok(ModelEval {
        p_up, sig_bp, banked_decided, flip_proof, rho,
        margin_bp, banked_margin_bp, cushion_bp, guard_bp: effective_guard_bp,
    })
}

/// Signed safety for one side: banked evidence divided by the residual
/// noise cushion, positive only when the banked margin points the side's
/// way. safety >= 1 on the fired side ≈ banked_decided.
pub(crate) fn side_safety(is_up: bool, banked_margin_bp: f64, cushion_bp: f64) -> f64 {
    let signed = if is_up { banked_margin_bp } else { -banked_margin_bp };
    signed / cushion_bp.max(1e-9)
}

/// R9 entry gate: the FIRST clip of a window needs banked evidence, not a
/// clock reading — both 2026-08-23 post-brake losses entered at safety
/// < 0.25 while the 50% clock said go. Applies to twap arms only
/// (close_open has no banked mass to measure), and only until the first
/// clip lands; position management after entry belongs to the brakes.
pub(crate) fn safety_gate_blocks(theta: f64, kind: &str, no_clips_yet: bool, safety: f64) -> bool {
    theta > 0.0 && kind == "twap" && no_clips_yet && safety < theta
}

/// Book-distrust brake predicate: a book handing over more than
/// `threshold` net (BOOK_DISTRUST_NET live) is pricing in something the
/// model missed, unless the TWAP math itself has already decided the
/// window. The threshold is a parameter only so replay can express the
/// pre-brake policy — live arms always pass the constant.
pub(crate) fn distrust_blocks(net: f64, threshold: f64, banked_decided: bool) -> bool {
    net > threshold && !banked_decided
}

/// No-averaging-down brake predicate: the ask dropping more than `tol`
/// (AVG_DOWN_TOL live) below our last clip on this token means the market
/// is repricing against the thesis, unless the TWAP math has already
/// decided.
pub(crate) fn avg_down_blocks(ask: f64, last_clip_ask: Option<f64>, tol: f64, banked_decided: bool) -> bool {
    match last_clip_ask {
        Some(prev) => ask < prev - tol && !banked_decided,
        None => false,
    }
}

/// Full-budget unlock: absolute seconds left, not window fraction — a 15m
/// window's 60%-elapsed mark still leaves 6 minutes of risk on the table.
pub(crate) fn budget_unlocked(now: f64, end: f64, late_rem_s: f64, banked_decided: bool) -> bool {
    (end - now) <= late_rem_s || banked_decided
}

/// Book recorder cadence: 5s normally, 1s inside the final 90s before
/// settlement — manipulation-signature research (R4) needs the book
/// fine-grained right where it matters and can afford coarse elsewhere.
pub(crate) fn book_sample_due(now: f64, last: f64, end: f64) -> bool {
    let interval = if end - now <= 90.0 { 1.0 } else { 5.0 };
    now - last >= interval
}

/// Realized sigma (bp per 1m bar) over the last `window` returns.
pub(crate) fn trailing_sigma_bp(closes: &[f64], window: usize) -> f64 {
    let n = closes.len().min(window + 1);
    if n < 4 {
        return 0.0;
    }
    let rets: Vec<f64> =
        closes[closes.len() - n..].windows(2).map(|w| (w[1] / w[0]).ln()).collect();
    let mu = rets.iter().sum::<f64>() / rets.len() as f64;
    let var = rets.iter().map(|r| (r - mu).powi(2)).sum::<f64>() / (rets.len() - 1) as f64;
    var.sqrt() * 1e4
}

/// Roll chains clone params forever, so the arm-time sigma goes stale within
/// hours — the live slow estimate supersedes it once enough history printed.
pub(crate) fn vol_floor_bp(slow_bp: f64, samples: usize, param_bp: f64) -> f64 {
    if samples >= SIGMA_SLOW_MIN && slow_bp > 0.0 {
        slow_bp
    } else {
        param_bp
    }
}

/// Lag-1 autocorrelation of log-returns over the last `n` closes.
/// pub(crate): the replay harness's full-mode feed reconstruction needs
/// the exact same regime signal the live feed thread computes.
pub(crate) fn lag1_autocorr(closes: &[f64], n: usize) -> f64 {
    let m = closes.len().min(n + 1);
    if m < 10 {
        return 0.0;
    }
    let rets: Vec<f64> = closes[closes.len() - m..]
        .windows(2)
        .map(|w| (w[1] / w[0]).ln())
        .collect();
    let mu = rets.iter().sum::<f64>() / rets.len() as f64;
    let var = rets.iter().map(|r| (r - mu).powi(2)).sum::<f64>();
    if var <= 0.0 {
        return 0.0;
    }
    let cov: f64 = rets.windows(2).map(|w| (w[0] - mu) * (w[1] - mu)).sum();
    cov / var
}

/// Abramowitz & Stegun 7.1.26 — |error| < 1.5e-7, plenty for pricing.
fn erf(x: f64) -> f64 {
    let sign = if x < 0.0 { -1.0 } else { 1.0 };
    let x = x.abs();
    let t = 1.0 / (1.0 + 0.3275911 * x);
    let y = 1.0
        - (((((1.061405429 * t - 1.453152027) * t) + 1.421413741) * t - 0.284496736) * t
            + 0.254829592)
            * t
            * (-x * x).exp();
    sign * y
}

fn norm_cdf(x: f64) -> f64 {
    0.5 * (1.0 + erf(x / std::f64::consts::SQRT_2))
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::strategies::updown::d_late_rem;

    fn params(slug: &str) -> ArmParams {
        serde_json::from_value(serde_json::json!({
            "slug": slug, "kind": "twap", "symbol": "BTCUSDT",
            "token_up": format!("{}-u", slug), "token_down": format!("{}-d", slug),
            "start": 600.0, "end": 1500.0,
            "sigma_bp_per_min": 3.0, "fee_rate": 0.07, "size_usdc": 100.0,
        }))
        .unwrap()
    }

    /// Builds a FeedState with a flat banked history (minutes 600..1380 at
    /// `banked_px`) plus a range-start reference at minute 540 = 100.0 —
    /// mirrors updown.rs's `armed_with_feed` test helper, minus the
    /// ArmState wrapper (eval_model only needs the FeedState + ArmParams).
    fn feed_with(banked_px: f64, spot: f64) -> (FeedState, f64) {
        let now = 1400.0;
        let mut f = FeedState { spot, spot_ts: now, ..Default::default() };
        f.per_min.insert(540, 100.0); // range-start reference
        for t in (600..1380).step_by(60) {
            f.per_min.insert(t, banked_px);
        }
        (f, now)
    }

    #[test]
    fn norm_cdf_sane() {
        assert!((norm_cdf(0.0) - 0.5).abs() < 1e-7);
        assert!((norm_cdf(1.96) - 0.975).abs() < 1e-3);
        assert!((norm_cdf(-1.96) - 0.025).abs() < 1e-3);
    }

    #[test]
    fn distrust_brake_blocks_outsized_net_unless_banked() {
        let t = BOOK_DISTRUST_NET;
        assert!(distrust_blocks(0.16, t, false));
        assert!(!distrust_blocks(0.16, t, true), "banked-decided is exempt");
        assert!(!distrust_blocks(0.15, t, false), "at the threshold, not over it");
        assert!(!distrust_blocks(0.05, t, false));
    }

    #[test]
    fn avg_down_brake_blocks_cheapening_ask_unless_banked() {
        let t = AVG_DOWN_TOL;
        assert!(avg_down_blocks(0.50, Some(0.53), t, false), "3c cheaper clears tolerance");
        assert!(!avg_down_blocks(0.50, Some(0.53), t, true), "banked-decided is exempt");
        assert!(!avg_down_blocks(0.52, Some(0.53), t, false), "within tolerance");
        assert!(!avg_down_blocks(0.55, Some(0.53), t, false), "richer ask, not cheaper");
        assert!(!avg_down_blocks(0.50, None, t, false), "no prior clip on this token");
    }

    #[test]
    fn pay_up_limit_spends_only_surplus_edge() {
        assert_eq!(pay_up_limit(0.90, 0.05, 0.015, 0.02, 0.985), 0.92, "full 2c buffer");
        assert_eq!(pay_up_limit(0.90, 0.02, 0.015, 0.02, 0.985), 0.905, "surplus only");
        assert_eq!(pay_up_limit(0.90, 0.015, 0.015, 0.02, 0.985), 0.90, "no surplus, no chase");
        assert_eq!(pay_up_limit(0.90, 0.05, 0.015, 0.0, 0.985), 0.90, "disabled by default");
        assert_eq!(pay_up_limit(0.98, 0.05, 0.015, 0.02, 0.985), 0.985, "max_price caps");
    }

    #[test]
    fn maker_bid_price_takes_the_tightest_of_its_three_clamps() {
        let tick = 0.001;
        // The edge floor binds: fair is capped well under the book.
        assert_eq!(
            maker_bid_price(0.98, 0.015, 0.99, 0.985, tick), 0.965,
            "fair minus the edge floor, with no fee term — makers pay none"
        );
        // The book binds: never leap a queue we cannot see by more than a tick.
        assert_eq!(
            maker_bid_price(1.0, 0.015, 0.50, 0.985, tick), 0.501,
            "one tick above the standing best bid, no further"
        );
        // max_price binds: the same hard cap a clip has.
        assert_eq!(
            maker_bid_price(1.0, 0.005, 0.99, 0.985, tick), 0.985,
            "max_price is a ceiling for a resting bid too"
        );
    }

    #[test]
    fn maker_bid_price_floors_onto_the_tick_grid() {
        // A price that already sits ON the grid must survive intact — the
        // naive floor loses a whole tick here, because 0.985/0.001 is
        // 984.99999999999989 in binary.
        assert_eq!(maker_bid_price(1.0, 0.015, 0.99, 0.99, 0.001), 0.985);
        // An off-grid price floors DOWN, never up: rounding a maker bid
        // toward the book makes it more aggressive than it was priced.
        assert_eq!(maker_bid_price(0.9999, 0.015, 0.99, 0.99, 0.001), 0.984);
        assert_eq!(maker_bid_price(1.0, 0.015, 0.99, 0.99, 0.01), 0.98, "coarser grid, cheaper bid");
    }

    #[test]
    fn side_safety_signs_by_side_and_floors_cushion() {
        assert!((side_safety(true, 6.0, 12.0) - 0.5).abs() < 1e-9);
        assert!((side_safety(false, 6.0, 12.0) + 0.5).abs() < 1e-9, "banked-up hurts down");
        assert!((side_safety(false, -6.0, 12.0) - 0.5).abs() < 1e-9);
        assert!(side_safety(true, 5.0, 0.0) > 1e6, "zero cushion never divides by zero");
    }

    #[test]
    fn safety_gate_blocks_only_first_twap_clip_when_armed() {
        assert!(safety_gate_blocks(0.3, "twap", true, 0.2));
        assert!(!safety_gate_blocks(0.3, "twap", true, 0.31), "evidence clears it");
        assert!(!safety_gate_blocks(0.0, "twap", true, 0.0), "theta 0 = disabled");
        assert!(!safety_gate_blocks(0.3, "twap", false, 0.0), "post-entry belongs to brakes");
        assert!(!safety_gate_blocks(0.3, "close_open", true, 0.0), "no banked mass to measure");
    }

    #[test]
    fn late_rem_s_default_matches_old_late_frac_on_a_300s_window() {
        // 300s window: the deleted ArmParams.late_frac field's historical
        // default (0.6 = 60% elapsed) and the current late_rem_s default
        // land on the same instant.
        let (start, end) = (0.0, 300.0);
        let old_unlock_now = start + (end - start) * 0.6;
        assert!(budget_unlocked(old_unlock_now, end, d_late_rem(), false));
        assert!(!budget_unlocked(old_unlock_now - 1.0, end, d_late_rem(), false));
    }

    #[test]
    fn late_rem_s_unlocks_a_900s_window_at_two_minutes_left_not_six() {
        let end = 900.0;
        // old late_frac 0.6 would have unlocked at rem=360s (6min); the
        // absolute-time brake holds until rem=120s (2min).
        assert!(!budget_unlocked(end - 360.0, end, d_late_rem(), false));
        assert!(!budget_unlocked(end - 121.0, end, d_late_rem(), false));
        assert!(budget_unlocked(end - 120.0, end, d_late_rem(), false));
        assert!(budget_unlocked(end - 119.0, end, d_late_rem(), false));
    }

    #[test]
    fn late_rem_s_banked_decided_unlocks_regardless_of_time() {
        assert!(budget_unlocked(0.0, 900.0, 120.0, true));
    }

    #[test]
    fn book_sample_cadence_tightens_in_the_final_90s() {
        let end = 1000.0;
        // Mid-window: 5s cadence.
        assert!(!book_sample_due(603.0, 600.0, end), "3s in — not due yet");
        assert!(book_sample_due(605.0, 600.0, end), "5s in — due");
        // Inside the final 90s: 1s cadence.
        assert!(book_sample_due(end - 89.0, end - 90.0, end), "1s in — due");
        assert!(!book_sample_due(end - 89.5, end - 90.0, end), "0.5s in — not due yet");
    }

    #[test]
    fn trailing_sigma_needs_history_and_scales_with_moves() {
        assert_eq!(trailing_sigma_bp(&[100.0, 100.0, 100.0], 45), 0.0);
        let flat: Vec<f64> = vec![100.0; 40];
        assert!(trailing_sigma_bp(&flat, 45) < 1e-9);
        // ~10bp alternating moves → sigma near 10bp/min.
        let choppy: Vec<f64> =
            (0..40).map(|i| if i % 2 == 0 { 100.0 } else { 100.1 }).collect();
        let s = trailing_sigma_bp(&choppy, 45);
        assert!(s > 8.0 && s < 12.0, "got {s}");
    }

    #[test]
    fn live_slow_sigma_supersedes_stale_param_floor() {
        // Enough history: the live estimate wins in BOTH directions —
        // that's the point (stale-high floors under-fire, stale-low
        // floors overtrust).
        assert_eq!(vol_floor_bp(9.0, 40, 3.0), 9.0);
        assert_eq!(vol_floor_bp(2.0, 40, 8.0), 2.0);
        // Thin history or dead feed: fall back to the arm-time param.
        assert_eq!(vol_floor_bp(9.0, 10, 3.0), 3.0);
        assert_eq!(vol_floor_bp(0.0, 40, 3.0), 3.0);
    }

    #[test]
    fn autocorr_reads_reverting_tape() {
        let alternating: Vec<f64> =
            (0..40).map(|i| if i % 2 == 0 { 100.0 } else { 100.5 }).collect();
        assert!(lag1_autocorr(&alternating, 60) < -0.8);
        let trending: Vec<f64> = (0..40).map(|i| 100.0 + i as f64 * 0.5).collect();
        assert!(lag1_autocorr(&trending, 60) > -0.2);
        assert_eq!(lag1_autocorr(&[100.0; 5], 60), 0.0); // too short
    }

    #[test]
    fn eval_model_explicit_effective_guard_can_exceed_the_static_param() {
        // margin_bp ~5bp here — clears the 3bp arm-time param comfortably.
        let p = params("s");
        let (f, now) = feed_with(100.05, 100.05);
        assert!(
            eval_model(&p, &f, now, p.basis_guard_bp).is_ok(),
            "static param alone passes"
        );
        // A higher effective guard (as a live-raised oracle read would
        // produce) gates the exact same margin — proves eval_model reads
        // the explicit argument, not p.basis_guard_bp directly.
        let err = eval_model(&p, &f, now, 8.0).unwrap_err();
        assert!(err.reason.contains("basis guard"), "{}", err.reason);
        assert!(
            err.reason.contains("8.0"),
            "reason string must print the effective value: {}",
            err.reason
        );
        assert_eq!(err.guard_bp, Some(8.0), "and carry it structurally");
    }

    #[test]
    fn basis_gate_carries_its_numbers_as_fields_not_only_prose() {
        // The whole point of GateReason: the margin/banked/cushion/guard a
        // consumer needs are fields, so rewording `reason` can't break them.
        let p = params("s");
        let (f, now) = feed_with(100.001, 100.001); // ~0.1bp — inside the 3bp param
        let g = eval_model(&p, &f, now, p.basis_guard_bp).unwrap_err();
        let margin = g.margin_bp.expect("basis gate reports its margin");
        assert!(margin.abs() < p.basis_guard_bp, "gated margin is inside the guard: {margin}");
        assert_eq!(g.guard_bp, Some(p.basis_guard_bp));
        assert!(g.banked_bp.is_some() && g.cushion_bp.is_some());
        // The fields must agree with the sentence the old regexes parsed —
        // that agreement is what makes the regex a safe legacy fallback.
        assert!(g.reason.contains(&format!("{:+.1}bp", margin)), "{}", g.reason);
    }

    #[test]
    fn non_basis_gates_report_no_numbers() {
        let p = params("s");
        let (f, _) = feed_with(100.05, 100.05);
        let g = eval_model(&p, &f, 1400.0 + MAX_SPOT_AGE_S + 1.0, p.basis_guard_bp).unwrap_err();
        assert!(g.reason.contains("stale"), "{}", g.reason);
        assert_eq!(
            (g.margin_bp, g.banked_bp, g.cushion_bp, g.guard_bp),
            (None, None, None, None),
            "a stale feed has no margin to report"
        );
    }

    #[test]
    fn shape_klines_drops_malformed_and_zero_rows() {
        let raw = serde_json::json!([
            [60000, "100.0", "101.0", "99.0", "100.5", "1.0"],
            [120000, "0", "0", "0", "0", "1.0"],          // zero prices — dropped
            [180000, "not-a-number", "1", "1", "100.0", "1.0"], // unparseable open — dropped
            [240000, "200.0", "201.0", "199.0", "201.0", "1.0"],
        ]);
        let ks = shape_klines(&raw);
        assert_eq!(ks.len(), 2);
        assert_eq!(ks[0], Kline { t: 60, o: 100.0, c: 100.5 });
        assert!((ks[0].mid() - 100.25).abs() < 1e-9, "mid is (o+c)/2");
        assert_eq!(ks[1].t, 240);
        assert!(shape_klines(&serde_json::json!({"code": -1121})).is_empty(), "error body -> no rows");
    }

    #[test]
    fn eval_model_effective_equal_to_param_matches_old_behavior() {
        // Mechanical check that threading effective_guard_bp through
        // doesn't change anything when effective == param — the case every
        // live arm hits until the oracle poller has 30+ samples.
        let p = params("s");
        let (f, now) = feed_with(100.05, 100.05);
        let m = eval_model(&p, &f, now, p.basis_guard_bp).unwrap();
        assert_eq!(m.guard_bp, p.basis_guard_bp);
    }

    #[test]
    fn negative_breakeven_is_certainty_not_nan() {
        // 13min banked at +5% vs ref, 35s left: breakeven goes negative.
        // Pre-fix this made p_up NaN and both sides priced fair 1.0 at
        // once via f64::min's NaN-eating (compiled repro, 2026-08-23).
        let p = params("s");
        let (mut f, _) = feed_with(105.0, 105.0);
        let now = p.end - 35.0;
        f.spot_ts = now; // fresh at the late-window eval instant
        let m = eval_model(&p, &f, now, p.basis_guard_bp).unwrap();
        assert!(m.p_up.is_finite(), "p_up must never be NaN");
        assert!((m.p_up - 1.0).abs() < 1e-9, "banked beyond reach = up certain");
        assert!(m.banked_decided, "an unreachable margin is decided");
    }

    #[test]
    fn settle_tw_widths_by_duration() {
        assert_eq!(settle_tw_secs(300.0), 30.0);
        assert_eq!(settle_tw_secs(900.0), 60.0);
        assert_eq!(settle_tw_secs(14400.0), 60.0);
    }

    #[test]
    fn terminal_rule_banks_nothing_before_the_settlement_window() {
        let mut p = params("s");
        p.settle_rule = "terminal".into();
        let (mut f, now) = feed_with(105.0, 100.5); // +50bp spot margin
        f.spot_ts = now;
        let m = eval_model(&p, &f, now, p.basis_guard_bp).unwrap();
        assert_eq!(m.banked_margin_bp, 0.0, "range history must not count");
        assert!(!m.banked_decided);
        assert!(m.p_up > 0.9, "+50bp vs 3bp/min over ~1min is near-certain");
        assert!((m.margin_bp - 50.0).abs() < 1.0);
        // A stormy sigma over the same margin must soften the read.
        p.sigma_bp_per_min = 60.0;
        let stormy = eval_model(&p, &f, now, p.basis_guard_bp).unwrap();
        assert!(stormy.p_up > 0.5 && stormy.p_up < 0.9,
                "diffusion softens under vol: {}", stormy.p_up);
    }

    #[test]
    fn terminal_rule_locks_inside_the_forming_twap() {
        let mut p = params("s"); // 900s window -> tw 60
        p.settle_rule = "terminal".into();
        let (mut f, _) = feed_with(105.0, 100.5);
        let now = p.end - 15.0; // 45s of the 60s TWAP formed
        f.spot_ts = now;
        let m = eval_model(&p, &f, now, p.basis_guard_bp).unwrap();
        assert!((m.banked_margin_bp - 50.0 * 0.75).abs() < 1.0, "3/4 locked");
        assert!(m.banked_decided, "+37bp locked vs a small residual cushion");
    }

    #[test]
    fn hybrid_prices_like_the_proxy_but_cushions_like_the_terminal_rule() {
        let mut p = params("s"); // 900s window, now=1400 -> rem 100 > tw 60
        let (mut f, now) = feed_with(105.0, 105.0);
        f.spot_ts = now;
        let range = eval_model(&p, &f, now, p.basis_guard_bp).unwrap();
        p.settle_rule = "hybrid".into();
        let hybrid = eval_model(&p, &f, now, p.basis_guard_bp).unwrap();
        // evidence half identical to the proxy...
        assert_eq!(hybrid.p_up, range.p_up);
        assert_eq!(hybrid.margin_bp, range.margin_bp);
        assert_eq!(hybrid.banked_margin_bp, range.banked_margin_bp);
        // ...but the cushion is honest terminal diffusion, far wider than
        // the proxy's banked-shrunk cushion, and nothing is truly locked
        // yet so the decided/flip waivers stay shut.
        assert!(hybrid.cushion_bp > range.cushion_bp);
        assert!(!hybrid.banked_decided, "no lock before the settlement window");
        assert!(!hybrid.flip_proof);
        assert!(range.banked_decided, "the proxy would have called this decided");
    }

    #[test]
    fn hybrid_locks_only_inside_the_forming_twap() {
        let mut p = params("s");
        p.settle_rule = "hybrid".into();
        let (mut f, _) = feed_with(105.0, 100.5); // +50bp terminal margin
        let now = p.end - 15.0; // 45s of the 60s TWAP formed
        f.spot_ts = now;
        let m = eval_model(&p, &f, now, p.basis_guard_bp).unwrap();
        assert!(m.banked_decided, "3/4 of +50bp locked beats the residual cushion");
    }

    #[test]
    fn terminal_rule_range_history_cannot_fake_certainty() {
        // The sol15 shape: 13 banked minutes far above ref, spot back AT ref.
        // range_avg called this decided; terminal must call it a coin flip
        // (margin inside the guard -> gated).
        let mut p = params("s");
        p.settle_rule = "terminal".into();
        let (mut f, now) = feed_with(105.0, 100.001); // spot ~ref
        f.spot_ts = now;
        let err = eval_model(&p, &f, now, p.basis_guard_bp).unwrap_err();
        assert!(err.reason.contains("terminal margin"), "{}", err.reason);
    }
}
