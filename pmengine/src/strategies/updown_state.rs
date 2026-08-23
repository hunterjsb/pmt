//! Arm-state survivorship for the updown strategy: what an armed window
//! leaves on disk so a crash or a restart isn't the end of it.
//!
//! The hole this closes (2026-08-23 config audit): arms lived only in the
//! `Updown` struct. A poweroff mid-window meant the open window's position
//! rode to resolution with nothing running the exit rule, the roll chain
//! died silently, and once `end` passed there was no code path left that
//! could recreate the arm — recovery depended entirely on a human noticing
//! in time. Nothing logged, nothing alerted.
//!
//! Two halves live here:
//!   - `ArmStore` + `plan_recovery` — the durable snapshot and the pure
//!     decision of what to do with each entry on the way back up.
//!   - `unmanaged_from_positions` + `spawn_unmanaged_check` — the startup
//!     reconcile that shouts about inventory nobody was managing.
//!
//! No plain `pub struct` in this file on purpose (same reason as
//! updown_model.rs): `pmstrat transpile --all` registers the first one it
//! finds in strategies/ as a strategy.

use crate::alerts::Notifier;
use crate::strategies::updown::{next_window, ArmParams};
use crate::strategies::updown_model::engine_dir;
use serde::{Deserialize, Serialize};
use std::path::PathBuf;

/// Bumped only if the on-disk shape changes incompatibly; a mismatch is
/// treated as "no state" rather than guessed at.
const STATE_VERSION: u32 = 1;

/// Sub-share remainders aren't worth paging a human over — clips are 5+.
const MIN_UNMANAGED_SHARES: f64 = 1.0;

/// One pending roll, flattened for disk. `next_try_at` is deliberately not
/// persisted: on recovery every task is due immediately.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub(crate) struct RollRecord {
    pub(crate) params: ArmParams,
    pub(crate) next_slug: String,
    pub(crate) next_start: f64,
    pub(crate) next_end: f64,
}

/// The whole strategy's durable state — every live arm's params plus the
/// pending roll chain.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub(crate) struct ArmsState {
    pub(crate) version: u32,
    pub(crate) written_at: f64,
    pub(crate) arms: Vec<ArmParams>,
    #[serde(default)]
    pub(crate) rolls: Vec<RollRecord>,
}

impl ArmsState {
    pub(crate) fn new(arms: Vec<ArmParams>, rolls: Vec<RollRecord>, now: f64) -> Self {
        Self { version: STATE_VERSION, written_at: now, arms, rolls }
    }

    pub(crate) fn is_empty(&self) -> bool {
        self.arms.is_empty() && self.rolls.is_empty()
    }
}

/// Where the arm snapshot lives. `None` = disabled: every load misses and
/// every save is a no-op, which is what unit tests get so they can never
/// read or clobber the operator's live state.
#[derive(Debug, Clone, Default)]
pub(crate) struct ArmStore {
    path: Option<PathBuf>,
}

impl ArmStore {
    /// The live store at `~/.pmt/engine/arms-state.json`.
    ///
    /// Disabled under `cfg(test)`: `Updown::new()` recovers from this store,
    /// and a unit test that reached the real file would spawn feeds against
    /// whatever the operator has armed right now. Tests that exercise
    /// persistence hand in `ArmStore::at(tmp)` explicitly.
    pub(crate) fn live() -> Self {
        if cfg!(test) {
            return Self::default();
        }
        Self { path: engine_dir().map(|d| d.join("arms-state.json")) }
    }

    /// Test-only: a store at an explicit path. The live path is never
    /// constructed this way, so it can't be pointed somewhere by accident.
    #[cfg(test)]
    pub(crate) fn at(path: PathBuf) -> Self {
        Self { path: Some(path) }
    }

    /// Read the snapshot. None when the store is disabled, the file is
    /// absent (the inert case — a first run, or a clean disarm), or the
    /// contents don't parse.
    pub(crate) fn load(&self) -> Option<ArmsState> {
        let path = self.path.as_ref()?;
        let raw = std::fs::read_to_string(path).ok()?;
        match serde_json::from_str::<ArmsState>(&raw) {
            Ok(s) if s.version == STATE_VERSION => Some(s),
            Ok(s) => {
                tracing::error!(
                    path = %path.display(), found = s.version, expected = STATE_VERSION,
                    "updown arm state version mismatch — not recovering, arms must be re-armed by hand"
                );
                None
            }
            Err(e) => {
                tracing::error!(
                    path = %path.display(), err = %e,
                    "updown arm state unreadable — not recovering, arms must be re-armed by hand"
                );
                None
            }
        }
    }

    /// Write the snapshot atomically (temp + rename), or delete the file
    /// when there is nothing left to remember. Deleting rather than writing
    /// an empty document keeps "absent = inert" true after a full disarm.
    ///
    /// Best-effort like the tapes: a failed write logs and is dropped —
    /// losing durability must never take the engine down mid-window.
    pub(crate) fn save(&self, state: &ArmsState) {
        let Some(path) = self.path.as_ref() else { return };
        if state.is_empty() {
            if path.exists() {
                if let Err(e) = std::fs::remove_file(path) {
                    tracing::warn!(path = %path.display(), err = %e, "updown arm state clear failed");
                }
            }
            return;
        }
        if let Some(dir) = path.parent() {
            let _ = std::fs::create_dir_all(dir);
        }
        let tmp = path.with_extension("json.tmp");
        let body = match serde_json::to_string(state) {
            Ok(b) => b,
            Err(e) => {
                tracing::error!(err = %e, "updown arm state serialize failed");
                return;
            }
        };
        // Rename over the live file only after the bytes are all down: a
        // poweroff mid-write leaves the previous good snapshot, never a
        // truncated one that would read as "no arms".
        if let Err(e) = std::fs::write(&tmp, body).and_then(|_| std::fs::rename(&tmp, path)) {
            tracing::warn!(path = %path.display(), err = %e, "updown arm state write failed");
            let _ = std::fs::remove_file(&tmp);
        }
    }
}

/// What a restart should do with the snapshot it found.
#[derive(Debug, Default)]
pub(crate) struct RecoveryPlan {
    /// Windows still open — re-arm verbatim. Token ids are already in the
    /// params, so this needs no gamma call and no network at all.
    pub(crate) rearm: Vec<ArmParams>,
    /// Roll chains to resume. A task whose own target window also expired
    /// while we were down hops forward on the normal `process_rolls` path.
    pub(crate) rolls: Vec<RollRecord>,
    /// Entries whose window closed while the engine was down. Dropped from
    /// the store, and each one is an unmanaged-position candidate.
    pub(crate) ended: Vec<ArmParams>,
}

/// Sort a snapshot into re-arms, rolls, and dead entries. Pure — the caller
/// owns the feeds, the logging, and the rewrite.
///
/// Persisted roll tasks come back untouched and are NOT unmanaged
/// candidates: a roll task only exists because a live engine watched that
/// window close and ran its cleanup. An entry still in `arms` past its end
/// is the opposite — proof the engine was down at the close.
pub(crate) fn plan_recovery(state: &ArmsState, now: f64) -> RecoveryPlan {
    let mut plan = RecoveryPlan::default();
    for p in &state.arms {
        if now < entry_end(p) {
            plan.rearm.push(p.clone());
            continue;
        }
        plan.ended.push(p.clone());
        if p.roll {
            if let Some((next_slug, next_start, next_end)) = next_window(&p.slug, p.start, p.end) {
                plan.rolls.push(RollRecord {
                    params: p.clone(),
                    next_slug,
                    next_start,
                    next_end,
                });
            }
        }
    }
    plan.rolls.extend(state.rolls.iter().cloned());
    plan
}

/// A persisted entry's window end. The params are authoritative when the
/// slug's epoch tail agrees with `start`; when it doesn't, the slug wins —
/// an `end` far in the future (a hand-edited or mangled file) would re-arm
/// a dead window and, worse, hide inventory that needs a human.
pub(crate) fn entry_end(p: &ArmParams) -> f64 {
    match slug_epoch(&p.slug) {
        Some(t) if (t - p.start).abs() > 0.5 => t + (p.end - p.start),
        _ => p.end,
    }
}

/// Trailing unix epoch on a recurring-window slug
/// (`btc-updown-5m-1787442000` -> 1787442000). None when the tail isn't a
/// plausible epoch — `-5m` and `-15` are suffixes, not timestamps.
pub(crate) fn slug_epoch(slug: &str) -> Option<f64> {
    let (_, tail) = slug.rsplit_once('-')?;
    let t: i64 = tail.parse().ok()?;
    (t > 1_000_000_000).then_some(t as f64)
}

/// One position left holding a bag on a window that already resolved.
#[derive(Debug, Clone, PartialEq)]
pub(crate) struct UnmanagedPosition {
    pub(crate) slug: String,
    pub(crate) side: String,
    pub(crate) token_id: String,
    pub(crate) size: f64,
    pub(crate) avg_price: f64,
}

impl UnmanagedPosition {
    /// The one line the operator gets, verbatim, in the log and the push.
    pub(crate) fn message(&self) -> String {
        format!(
            "unmanaged position rode to resolution while engine was down: {} {} {}",
            self.slug, self.side, self.size
        )
    }
}

/// Match ended windows' tokens against a data-api positions payload.
/// Pure so the matching is testable without a wallet or a network.
pub(crate) fn unmanaged_from_positions(
    ended: &[ArmParams],
    positions: &[serde_json::Value],
) -> Vec<UnmanagedPosition> {
    let mut out = Vec::new();
    for p in ended {
        for (side, token) in [("up", &p.token_up), ("down", &p.token_down)] {
            let Some(pos) = positions
                .iter()
                .find(|v| v.get("asset").and_then(|a| a.as_str()) == Some(token.as_str()))
            else {
                continue;
            };
            let size = pos.get("size").and_then(|s| s.as_f64()).unwrap_or(0.0);
            if size.abs() < MIN_UNMANAGED_SHARES {
                continue;
            }
            out.push(UnmanagedPosition {
                slug: p.slug.clone(),
                side: side.to_string(),
                token_id: token.clone(),
                size,
                avg_price: pos.get("avgPrice").and_then(|s| s.as_f64()).unwrap_or(0.0),
            });
        }
    }
    out
}

/// Startup reconcile for ended windows: ask the data-api what we still hold
/// on their tokens and shout about anything non-zero. Detection only — this
/// never sells; a resolved window can't be traded out of anyway, and one
/// that resolved against us wants a human, not a reflex.
///
/// Runs on its own OS thread: the caller is the engine's async startup path,
/// and reqwest::blocking must not be driven from a tokio worker (same rule
/// the replay entry point follows in main.rs).
pub(crate) fn spawn_unmanaged_check(ended: Vec<ArmParams>) {
    // Same house rule as ArmStore::live() and install_arm's feeds: a unit
    // test never reaches the network or the operator's phone. The matching
    // itself is pure and tested directly.
    if ended.is_empty() || cfg!(test) {
        return;
    }
    let Ok(funder) = std::env::var("PMENGINE_FUNDER_ADDRESS")
        .or_else(|_| std::env::var("PM_FUNDER_ADDRESS"))
    else {
        tracing::warn!(
            windows = ended.len(),
            "windows closed while the engine was down but no funder address is set — \
             cannot check for unmanaged positions; run `pmt crypto activity` by hand"
        );
        return;
    };
    std::thread::spawn(move || {
        let positions = match fetch_positions(&funder) {
            Ok(v) => v,
            Err(e) => {
                tracing::error!(
                    err = %e, windows = ended.len(),
                    "unmanaged-position check FAILED — windows closed while the engine was \
                     down and their inventory is unverified; run `pmt crypto activity`"
                );
                return;
            }
        };
        let hits = unmanaged_from_positions(&ended, &positions);
        if hits.is_empty() {
            tracing::info!(
                windows = ended.len(),
                "recovery: windows closed while down, no inventory left on them"
            );
            return;
        }
        let notifier = Notifier::from_env();
        for u in &hits {
            tracing::error!(
                slug = %u.slug, side = %u.side, size = u.size, avg_price = u.avg_price,
                notional = u.size * u.avg_price, token = %u.token_id,
                "{}", u.message()
            );
            notifier.notify_text_blocking("pmengine: unmanaged position", &u.message());
        }
    });
}

/// Every position the funder holds, straight off the unauthenticated
/// data-api (same endpoint the engine's own reconcile uses).
fn fetch_positions(funder: &str) -> Result<Vec<serde_json::Value>, String> {
    let client = reqwest::blocking::Client::builder()
        .timeout(std::time::Duration::from_secs(10))
        .build()
        .map_err(|e| format!("client: {}", e))?;
    client
        .get(format!(
            "https://data-api.polymarket.com/positions?user={}&sizeThreshold=0",
            funder
        ))
        .send()
        .and_then(|r| r.error_for_status())
        .map_err(|e| format!("positions: {}", e))?
        .json()
        .map_err(|e| format!("positions json: {}", e))
}

#[cfg(test)]
mod tests {
    use super::*;

    fn params(slug: &str, start: f64, end: f64, roll: bool) -> ArmParams {
        serde_json::from_value(serde_json::json!({
            "slug": slug, "kind": "twap", "symbol": "BTCUSDT",
            "token_up": format!("{}-u", slug), "token_down": format!("{}-d", slug),
            "start": start, "end": end,
            "sigma_bp_per_min": 3.0, "fee_rate": 0.07, "size_usdc": 100.0,
            "roll": roll,
        }))
        .unwrap()
    }

    fn tmp_store(name: &str) -> (ArmStore, PathBuf) {
        let dir = std::env::temp_dir().join(format!("pmengine-armstate-{}-{}", name, std::process::id()));
        std::fs::create_dir_all(&dir).unwrap();
        let path = dir.join("arms-state.json");
        let _ = std::fs::remove_file(&path);
        (ArmStore::at(path.clone()), path)
    }

    #[test]
    fn absent_store_is_inert() {
        let (store, path) = tmp_store("absent");
        assert!(store.load().is_none());
        // Saving an empty state must not create a file — absent stays absent.
        store.save(&ArmsState::new(vec![], vec![], 100.0));
        assert!(!path.exists());
    }

    #[test]
    fn round_trips_arms_and_rolls() {
        let (store, _) = tmp_store("roundtrip");
        let a = params("btc-updown-5m-1787442000", 1787442000.0, 1787442300.0, true);
        let r = RollRecord {
            params: a.clone(),
            next_slug: "btc-updown-5m-1787442300".into(),
            next_start: 1787442300.0,
            next_end: 1787442600.0,
        };
        store.save(&ArmsState::new(vec![a.clone()], vec![r], 1787442100.0));
        let back = store.load().expect("state reloads");
        assert_eq!(back.arms.len(), 1);
        assert_eq!(back.arms[0].slug, a.slug);
        assert_eq!(back.arms[0].token_up, a.token_up);
        assert_eq!(back.arms[0].size_usdc, 100.0);
        assert!(back.arms[0].roll);
        assert_eq!(back.rolls[0].next_slug, "btc-updown-5m-1787442300");
    }

    #[test]
    fn empty_save_clears_an_existing_file() {
        let (store, path) = tmp_store("clear");
        store.save(&ArmsState::new(
            vec![params("btc-updown-5m-1787442000", 1787442000.0, 1787442300.0, true)],
            vec![],
            100.0,
        ));
        assert!(path.exists());
        store.save(&ArmsState::new(vec![], vec![], 200.0));
        assert!(!path.exists(), "a full disarm leaves no file to resurrect from");
        assert!(store.load().is_none());
    }

    #[test]
    fn corrupt_state_does_not_recover() {
        let (store, path) = tmp_store("corrupt");
        std::fs::write(&path, "{not json").unwrap();
        assert!(store.load().is_none());
        // Wrong version is refused too, not half-read.
        std::fs::write(&path, r#"{"version":99,"written_at":0,"arms":[],"rolls":[]}"#).unwrap();
        assert!(store.load().is_none());
    }

    #[test]
    fn plan_rearms_a_still_open_window() {
        let s = ArmsState::new(
            vec![params("btc-updown-5m-1787442000", 1787442000.0, 1787442300.0, true)],
            vec![],
            1787442100.0,
        );
        let plan = plan_recovery(&s, 1787442100.0);
        assert_eq!(plan.rearm.len(), 1);
        assert_eq!(plan.rearm[0].token_up, "btc-updown-5m-1787442000-u");
        assert!(plan.rolls.is_empty(), "a live window rolls at ITS close, not now");
        assert!(plan.ended.is_empty());
    }

    #[test]
    fn plan_hops_a_closed_roll_window_forward() {
        let s = ArmsState::new(
            vec![params("btc-updown-5m-1787442000", 1787442000.0, 1787442300.0, true)],
            vec![],
            1787442100.0,
        );
        // Back up an hour later: the window is long gone.
        let plan = plan_recovery(&s, 1787445900.0);
        assert!(plan.rearm.is_empty(), "a dead window is never re-armed");
        assert_eq!(plan.rolls.len(), 1);
        // The immediate successor — process_rolls walks it forward from here.
        assert_eq!(plan.rolls[0].next_slug, "btc-updown-5m-1787442300");
        assert_eq!(plan.rolls[0].next_end, 1787442600.0);
        assert_eq!(plan.ended.len(), 1, "its inventory still needs checking");
    }

    #[test]
    fn plan_drops_a_closed_no_roll_window() {
        let s = ArmsState::new(
            vec![params("btc-updown-5m-1787442000", 1787442000.0, 1787442300.0, false)],
            vec![],
            1787442100.0,
        );
        let plan = plan_recovery(&s, 1787445900.0);
        assert!(plan.rearm.is_empty());
        assert!(plan.rolls.is_empty(), "--no-roll means the chain ends here");
        assert_eq!(plan.ended.len(), 1);
    }

    #[test]
    fn plan_resumes_persisted_rolls_without_flagging_them() {
        let a = params("btc-updown-5m-1787442000", 1787442000.0, 1787442300.0, true);
        let s = ArmsState::new(
            vec![],
            vec![RollRecord {
                params: a,
                next_slug: "btc-updown-5m-1787442300".into(),
                next_start: 1787442300.0,
                next_end: 1787442600.0,
            }],
            1787442310.0,
        );
        let plan = plan_recovery(&s, 1787445900.0);
        assert_eq!(plan.rolls.len(), 1);
        // A live engine saw that window close and cleaned it up — the task's
        // own params are not an unmanaged candidate.
        assert!(plan.ended.is_empty());
    }

    #[test]
    fn entry_end_prefers_the_slug_epoch_over_a_lying_end() {
        let mut p = params("btc-updown-5m-1787442000", 1787442000.0, 1787442300.0, true);
        assert_eq!(entry_end(&p), 1787442300.0, "consistent params are taken as-is");
        // Params claim the window runs for another century; the slug says
        // it started at 1787442000 and lasts 300s.
        p.start = 1787442000.0;
        p.end = 1787442300.0;
        p.slug = "btc-updown-5m-1787000000".into();
        assert_eq!(entry_end(&p), 1787000300.0);
        // A non-epoch tail leaves the params alone.
        p.slug = "some-market".into();
        assert_eq!(entry_end(&p), 1787442300.0);
    }

    #[test]
    fn slug_epoch_rejects_non_timestamps() {
        assert_eq!(slug_epoch("btc-updown-5m-1787442000"), Some(1787442000.0));
        assert_eq!(slug_epoch("btc-updown-5m"), None);
        assert_eq!(slug_epoch("btc-updown-5m-15"), None);
        assert_eq!(slug_epoch("nodashes"), None);
    }

    #[test]
    fn unmanaged_matches_ended_windows_only() {
        let ended = vec![params("btc-updown-5m-1787442000", 1787442000.0, 1787442300.0, true)];
        let positions = vec![
            serde_json::json!({"asset": "btc-updown-5m-1787442000-u", "size": 42.0, "avgPrice": 0.94}),
            // A live window's token — not in `ended`, so not reported.
            serde_json::json!({"asset": "btc-updown-5m-1787442300-u", "size": 30.0, "avgPrice": 0.9}),
        ];
        let hits = unmanaged_from_positions(&ended, &positions);
        assert_eq!(hits.len(), 1);
        assert_eq!(hits[0].side, "up");
        assert_eq!(hits[0].size, 42.0);
        assert_eq!(
            hits[0].message(),
            "unmanaged position rode to resolution while engine was down: \
             btc-updown-5m-1787442000 up 42"
        );
    }

    #[test]
    fn unmanaged_ignores_dust_and_redeemed_positions() {
        let ended = vec![params("btc-updown-5m-1787442000", 1787442000.0, 1787442300.0, false)];
        let positions = vec![
            serde_json::json!({"asset": "btc-updown-5m-1787442000-u", "size": 0.0, "avgPrice": 0.94}),
            serde_json::json!({"asset": "btc-updown-5m-1787442000-d", "size": 0.0004, "avgPrice": 0.1}),
        ];
        assert!(unmanaged_from_positions(&ended, &positions).is_empty());
    }

    #[test]
    fn unmanaged_reports_both_sides_of_a_window() {
        let ended = vec![params("btc-updown-5m-1787442000", 1787442000.0, 1787442300.0, false)];
        let positions = vec![
            serde_json::json!({"asset": "btc-updown-5m-1787442000-u", "size": 20.0, "avgPrice": 0.6}),
            serde_json::json!({"asset": "btc-updown-5m-1787442000-d", "size": 15.0, "avgPrice": 0.4}),
        ];
        let hits = unmanaged_from_positions(&ended, &positions);
        assert_eq!(hits.len(), 2);
        assert_eq!(hits[1].side, "down");
    }
}
