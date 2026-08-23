//! `--mode full` for stream-fed arms: the FeedState timeline rebuilt from
//! the RTDS recorder corpus instead of from Binance klines.
//!
//! Full mode's whole claim is that it *reconstructs* the model's inputs
//! rather than replaying the numbers the tape already computed. For a
//! `feed = "binance"` arm that reconstruction is `feed_state_at`, shaping
//! 1m klines the way the live poller shaped them. For a `feed = "rtds"`
//! arm those klines are the wrong series entirely — the arm never read
//! Binance, and the settlement stream it did read has no candles, no
//! backfill and no history endpoint. Replaying a stream-fed window off
//! klines answers a question nobody asked.
//!
//! What the arm actually read is on disk: the recorder's
//! `~/.pmt/corpus/rtds/rtds-YYYYMMDD.jsonl`, 1 Hz across the three topics.
//! So this module does the one thing that cannot drift — it feeds those
//! records back through `updown_rtds`'s own router, into a real `RtdsHub`
//! with a real registered consumer. Spot, spot_ts, per_min, closes and rho
//! are then shaped by the *live* code, not by a replay-side lookalike.
//! There is no second implementation of the mapping to keep in step,
//! because there is no second implementation.
//!
//! The two things replay supplies that the socket supplied live:
//!
//!   - **Order.** Samples are routed in `t_recv` order (the recorder writes
//!     from several topic handlers and its lines are NOT ordered on disk),
//!     each stamped with its own receive time. `route_sample` reads that
//!     stamp for `spot_ts` and for the lag check, so a replayed window
//!     gates on feed staleness exactly where the live one did.
//!   - **Warmup.** A live hub had been running for hours before an arm
//!     registered, and seeded it. Here the consumer registers first and the
//!     corpus is fed from `HISTORY_WARMUP_S` before the window — which
//!     lands in the same place, because the fan-out writes the same closes
//!     vector `seed_feed` would have copied.

use crate::strategies::updown::ArmParams;
use crate::strategies::updown_model::{settle_tw_for, FeedState};
// Tests still build hubs at a raw window width; the live path goes through
// settle_tw_for, so ungated this is an unused import in a non-test build.
#[cfg(test)]
use crate::strategies::updown_model::settle_tw_secs;
use crate::strategies::updown_rtds::{
    self, CorpusSample, RtdsHub, RtdsSub, HISTORY_WARMUP_S, RTDS_SYMBOLS,
};
use serde_json::Value;
use std::collections::{BTreeMap, BTreeSet, HashMap};
use std::io::BufRead;
use std::path::{Path, PathBuf};
use std::sync::{Arc, Mutex};

/// `~/.pmt/corpus/rtds` — where `pmt crypto rtds record` writes.
pub(crate) fn default_corpus_dir() -> PathBuf {
    std::env::var("HOME")
        .map(PathBuf::from)
        .unwrap_or_else(|_| PathBuf::from("."))
        .join(".pmt/corpus/rtds")
}

/// Recorder files in a corpus directory. The recorder rotates daily and
/// names by date, so the glob is the whole contract; dotfiles are skipped
/// because the converter leaves `.rtds4.jsonl`-style scratch beside them.
fn corpus_files(path: &Path) -> Result<Vec<PathBuf>, String> {
    let meta = std::fs::metadata(path).map_err(|e| {
        format!(
            "{}: {} — pass --rtds-corpus <dir|file>, or record one with \
             `pmt crypto rtds record`. A stream-fed window cannot be replayed \
             from anything else: the feed has no history endpoint.",
            path.display(),
            e
        )
    })?;
    if meta.is_file() {
        return Ok(vec![path.to_path_buf()]);
    }
    let mut out: Vec<PathBuf> = std::fs::read_dir(path)
        .map_err(|e| format!("{}: {}", path.display(), e))?
        .filter_map(Result::ok)
        .map(|e| e.path())
        .filter(|p| {
            let name = p.file_name().and_then(|s| s.to_str()).unwrap_or("");
            name.starts_with("rtds-") && name.ends_with(".jsonl")
        })
        .collect();
    out.sort();
    if out.is_empty() {
        return Err(format!(
            "{}: no rtds-*.jsonl recorder files — a stream-fed window has no other \
             source, the feed serves no history",
            path.display()
        ));
    }
    Ok(out)
}

/// Every recorder sample this run needs, per symbol, in receive order.
///
/// Loaded ONCE per replay run and shared by every window on the symbol: the
/// day's file is ~100 MB and a per-window re-read is the entire runtime of
/// a fleet pass.
pub(crate) struct RtdsCorpus {
    by_symbol: HashMap<String, Arc<[CorpusSample]>>,
    /// What was read, so a refusal can name it.
    source: String,
    lines: usize,
}

impl RtdsCorpus {
    /// Load `wanted` symbols out of the recorder files under `path`.
    ///
    /// Restricted to the symbols in play on purpose: the stream carries
    /// eight, a run needs one or two, and parsing the other seven-eighths
    /// costs seconds and hundreds of megabytes for samples no consumer is
    /// registered for.
    pub(crate) fn load(path: &Path, wanted: &BTreeSet<String>) -> Result<Self, String> {
        let files = corpus_files(path)?;
        let mut by_symbol: HashMap<String, Vec<CorpusSample>> = HashMap::new();
        let mut lines = 0usize;
        for file in &files {
            let f = std::fs::File::open(file)
                .map_err(|e| format!("open {}: {}", file.display(), e))?;
            for line in std::io::BufReader::new(f).lines().map_while(Result::ok) {
                lines += 1;
                // Substring pre-filter before the JSON parse. Purely a
                // speed gate — the parsed symbol below is what actually
                // decides — but it skips ~7/8 of a day's 500k lines.
                if !wanted.iter().any(|s| line.contains(s.as_str())) {
                    continue;
                }
                let Some(sample) = updown_rtds::parse_corpus_line(&line) else { continue };
                if !wanted.contains(sample.symbol()) {
                    continue;
                }
                by_symbol.entry(sample.symbol().to_string()).or_default().push(sample);
            }
        }
        // The recorder writes from three topic handlers, so its lines are
        // interleaved but not ordered. Live arrival order is what the
        // router's freshness and mark-tolerance rules read, so sort on it —
        // stably, to keep same-instant topics in the order they landed.
        for samples in by_symbol.values_mut() {
            samples.sort_by(|a, b| {
                a.t_recv.partial_cmp(&b.t_recv).unwrap_or(std::cmp::Ordering::Equal)
            });
        }
        Ok(Self {
            by_symbol: by_symbol.into_iter().map(|(k, v)| (k, v.into())).collect(),
            source: if files.len() == 1 {
                files[0].display().to_string()
            } else {
                format!("{} ({} file(s))", path.display(), files.len())
            },
            lines,
        })
    }

    fn symbols_present(&self) -> String {
        let mut names: Vec<&str> = self.by_symbol.keys().map(String::as_str).collect();
        names.sort_unstable();
        if names.is_empty() {
            "nothing".to_string()
        } else {
            names.join(", ")
        }
    }
}

/// How far past a window's close the stream is drained before settlement is
/// read off it. The settlement mark for the final minute prints AT the
/// close, and a late-but-in-tolerance substitute up to `MARK_TOL_S` after
/// that; a couple of minutes covers both with room. Not look-ahead: the
/// window is over, and scoring a closed window after the fact is the same
/// licence `settle_winner` takes with klines.
const SETTLE_DRAIN_S: f64 = 120.0;

/// One window's settlement stream, replayed sample by sample through the
/// live hub.
pub(crate) struct RtdsTimeline {
    hub: RtdsHub,
    /// Held for its `Drop`: the consumer unregisters with the timeline, the
    /// same way a retired arm's does.
    _sub: RtdsSub,
    feed: Arc<Mutex<FeedState>>,
    samples: Arc<[CorpusSample]>,
    cursor: usize,
}

impl RtdsTimeline {
    /// The timeline for `p` off a loaded corpus. Every refusal names the
    /// gap: a stream-fed window that silently replayed off short or absent
    /// history would produce numbers nobody could tell from good ones.
    pub(crate) fn new(p: &ArmParams, corpus: &RtdsCorpus) -> Result<Self, String> {
        let symbol = updown_rtds::rtds_symbol(&p.symbol).ok_or_else(|| {
            format!(
                "{}: feed 'rtds' does not carry {} — the stream serves {}",
                p.slug,
                p.symbol,
                RTDS_SYMBOLS.join(", ")
            )
        })?;
        let samples = corpus.by_symbol.get(&symbol).cloned().ok_or_else(|| {
            format!(
                "{}: the RTDS corpus has no {} samples. Read {} ({} line(s)); it carries {}.",
                p.slug,
                symbol,
                corpus.source,
                corpus.lines,
                corpus.symbols_present()
            )
        })?;
        Self::build(p, &symbol, samples, &corpus.source)
    }

    /// The same timeline from records already in hand — the fixture seam.
    /// A fixture carries its own slice and never reaches a corpus, exactly
    /// as it carries its own klines and never reaches Binance.
    pub(crate) fn from_records(p: &ArmParams, records: &[Value]) -> Result<Self, String> {
        let symbol = updown_rtds::rtds_symbol(&p.symbol).ok_or_else(|| {
            format!("{}: feed 'rtds' does not carry {}", p.slug, p.symbol)
        })?;
        let mut samples: Vec<CorpusSample> = records
            .iter()
            .filter_map(updown_rtds::parse_corpus_record)
            .filter(|s| s.symbol() == symbol)
            .collect();
        samples.sort_by(|a, b| {
            a.t_recv.partial_cmp(&b.t_recv).unwrap_or(std::cmp::Ordering::Equal)
        });
        if samples.is_empty() {
            return Err(format!(
                "{}: the fixture's rtds slice carries no {} samples ({} record(s) read)",
                p.slug,
                symbol,
                records.len()
            ));
        }
        Self::build(p, &symbol, samples.into(), "the fixture's rtds slice")
    }

    fn build(
        p: &ArmParams,
        symbol: &str,
        samples: Arc<[CorpusSample]>,
        source: &str,
    ) -> Result<Self, String> {
        let first = samples[0].t_recv;
        let last = samples[samples.len() - 1].t_recv;
        // The window needs its settlement reference (the mark printed AT
        // `start`, keyed to start-60) through its close. Short of that
        // there is no honest replay, only a quieter wrong one.
        if first > p.start || last < p.end {
            return Err(format!(
                "{}: the RTDS corpus covers {:.0}..{:.0} but this window needs {:.0}..{:.0} \
                 (its settlement reference through the close) — short by {:.0}s at the start \
                 and {:.0}s at the end. Source: {}.",
                p.slug,
                first,
                last,
                p.start,
                p.end,
                (first - p.start).max(0.0),
                (p.end - last).max(0.0),
                source
            ));
        }
        let warm_from = p.start - HISTORY_WARMUP_S;
        if first > warm_from {
            // Not fatal: the estimators fall back the way a freshly-started
            // hub's do. Loud, because the fallback is invisible in the
            // output and moves rho and the slow sigma.
            eprintln!(
                "[replay] {}: only {:.0}s of RTDS history before the window ({:.0}s wanted) — \
                 rho and the slow sigma replay off a shorter tape than the live arm read",
                p.slug,
                p.start - first,
                HISTORY_WARMUP_S
            );
        }
        let hub = RtdsHub::new();
        let feed = Arc::new(Mutex::new(FeedState::default()));
        let sub = hub.register_offline(
            symbol,
            settle_tw_for(p),
            p.start,
            feed.clone(),
        );
        let cursor = samples.partition_point(|s| s.t_recv < warm_from);
        Ok(Self { hub, _sub: sub, feed, samples, cursor })
    }

    /// The FeedState a live arm would have been holding at `now`.
    pub(crate) fn state_at(&mut self, now: f64) -> FeedState {
        self.advance_to(now);
        self.feed.lock().unwrap().clone()
    }

    /// Route every sample that had arrived by `now`, and not one more —
    /// the look-ahead cutoff, enforced by the receive clock rather than by
    /// a rule about minutes.
    fn advance_to(&mut self, now: f64) {
        while self.cursor < self.samples.len() && self.samples[self.cursor].t_recv <= now {
            self.hub.replay_sample(&self.samples[self.cursor]);
            self.cursor += 1;
        }
    }

    /// The settlement-width TWAP marks this window printed, read off the
    /// arm's own `per_min` after draining the stream past the close.
    ///
    /// Read from the consumer rather than re-derived: `per_min` is exactly
    /// the settlement series at this arm's width, keyed by the live
    /// router's own mark rule. A second implementation here is how the
    /// replay's verdict and the model's arithmetic drift apart.
    pub(crate) fn settle_marks(&mut self, end: f64) -> BTreeMap<i64, f64> {
        self.advance_to(end + SETTLE_DRAIN_S);
        self.feed.lock().unwrap().per_min.clone()
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    /// The refusal text, without needing `Debug` on a struct that owns a
    /// hub, a mutex and a slice of the corpus.
    fn refuse<T>(r: Result<T, String>) -> String {
        r.map(|_| ()).expect_err("expected a refusal, got a value")
    }

    fn params(slug: &str, start: f64, end: f64) -> ArmParams {
        serde_json::from_value(serde_json::json!({
            "slug": slug, "kind": "twap", "symbol": "XRPUSDT", "feed": "rtds",
            "token_up": format!("{}-u", slug), "token_down": format!("{}-d", slug),
            "start": start, "end": end,
            "sigma_bp_per_min": 3.0, "fee_rate": 0.07, "size_usdc": 100.0,
        }))
        .unwrap()
    }

    /// One recorder row, exactly as `pmt crypto rtds record` writes it.
    pub(super) fn row(topic: &str, symbol: &str, ts_s: i64, value: f64, t_recv: f64) -> Value {
        serde_json::json!({
            "t_recv": t_recv, "topic": topic, "symbol": symbol,
            "ts": ts_s * 1000, "value": value,
            "full_accuracy_value": format!("{:.0}", value * 1e18),
            "window_s": if topic == updown_rtds::TOPIC_TWAP30 { 30 } else { 60 },
        })
    }

    /// A 5m window's stream: 1 Hz chainlink and a 30s TWAP mark a minute,
    /// from `from` to `to`, arriving 0.2s after each print.
    pub(super) fn stream(from: i64, to: i64, px: impl Fn(i64) -> f64) -> Vec<Value> {
        let mut out = Vec::new();
        for ts in from..to {
            out.push(row(updown_rtds::TOPIC_SPOT, "xrp/usd", ts, px(ts), ts as f64 + 0.2));
            if ts.rem_euclid(60) == 0 {
                out.push(row(updown_rtds::TOPIC_TWAP30, "xrp/usd", ts, px(ts), ts as f64 + 0.2));
            }
        }
        out
    }

    #[test]
    fn a_missing_corpus_names_the_path_and_the_recorder() {
        let missing = std::env::temp_dir().join("pmengine-rtds-definitely-absent");
        let _ = std::fs::remove_dir_all(&missing);
        let err = refuse(RtdsCorpus::load(&missing, &BTreeSet::new()));
        assert!(err.contains("pmengine-rtds-definitely-absent"), "{err}");
        assert!(err.contains("pmt crypto rtds record"), "{err}");
        assert!(err.contains("no history endpoint"), "{err}");
    }

    #[test]
    fn an_empty_corpus_dir_is_a_refusal_not_an_empty_replay() {
        let dir = std::env::temp_dir()
            .join(format!("pmengine-rtds-empty-{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&dir);
        std::fs::create_dir_all(&dir).unwrap();
        // A stray non-recorder file must not count as a corpus.
        std::fs::write(dir.join("notes.txt"), "hi").unwrap();
        let err = refuse(RtdsCorpus::load(&dir, &BTreeSet::new()));
        assert!(err.contains("no rtds-*.jsonl"), "{err}");
        let _ = std::fs::remove_dir_all(&dir);
    }

    #[test]
    fn a_symbol_the_corpus_never_carried_names_what_it_does_carry() {
        let dir = std::env::temp_dir()
            .join(format!("pmengine-rtds-sym-{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&dir);
        std::fs::create_dir_all(&dir).unwrap();
        let lines: Vec<String> = stream(1000, 1100, |_| 2.5)
            .iter()
            .map(|v| v.to_string())
            .collect();
        std::fs::write(dir.join("rtds-20260823.jsonl"), lines.join("\n")).unwrap();

        let wanted: BTreeSet<String> =
            ["xrp/usd", "doge/usd"].iter().map(|s| s.to_string()).collect();
        let corpus = RtdsCorpus::load(&dir, &wanted).unwrap();

        let mut p = params("doge-updown-5m-1000", 1000.0, 1300.0);
        p.symbol = "DOGEUSDT".into();
        let err = refuse(RtdsTimeline::new(&p, &corpus));
        assert!(err.contains("no doge/usd samples"), "{err}");
        assert!(err.contains("it carries xrp/usd"), "{err}");
        assert!(err.contains("rtds-20260823.jsonl"), "the refusal names the file it read: {err}");
        let _ = std::fs::remove_dir_all(&dir);
    }

    #[test]
    fn a_symbol_the_stream_does_not_serve_is_refused_before_the_corpus() {
        let corpus = RtdsCorpus {
            by_symbol: HashMap::new(),
            source: "test".into(),
            lines: 0,
        };
        let mut p = params("pepe-updown-5m-1000", 1000.0, 1300.0);
        p.symbol = "PEPEUSDT".into();
        let err = refuse(RtdsTimeline::new(&p, &corpus));
        assert!(err.contains("does not carry PEPEUSDT"), "{err}");
        assert!(err.contains("xrp/usd"), "and lists what it does serve: {err}");
    }

    #[test]
    fn a_corpus_that_stops_mid_window_is_refused_with_the_gap_named() {
        // Covers the start, dies 100s before the close.
        let recs = stream(600, 1200, |_| 2.5);
        let p = params("xrp-updown-5m-900", 900.0, 1300.0);
        let err = refuse(RtdsTimeline::from_records(&p, &recs));
        assert!(err.contains("short by"), "{err}");
        assert!(err.contains("101s at the end"), "the gap is measured, not hinted: {err}");

        // And the mirror: history that starts after the window did.
        let late = stream(1000, 1400, |_| 2.5);
        let err = refuse(RtdsTimeline::from_records(&p, &late));
        assert!(err.contains("100s at the start"), "{err}");
    }

    #[test]
    fn an_empty_fixture_slice_is_refused_rather_than_replayed_blank() {
        let p = params("xrp-updown-5m-900", 900.0, 1300.0);
        let err = refuse(RtdsTimeline::from_records(&p, &[]));
        assert!(err.contains("carries no xrp/usd samples"), "{err}");
    }

    /// THE test this whole module exists to make possible.
    ///
    /// Same stream, two doors: the live one (`RtdsHub::register` +
    /// `ingest_price`, the path a connected socket drives) and the replay
    /// one (`RtdsTimeline` over recorder rows). Every field of the
    /// resulting FeedState has to be identical — bit for bit on the floats,
    /// because both sides do the same arithmetic in the same order, not
    /// merely close.
    ///
    /// Drift between the live shaping and the replay shaping is the failure
    /// this harness exists to prevent: it would make every stream-fed
    /// replay, every fixture cut from one, and every conclusion drawn off
    /// them quietly describe an engine that never traded.
    #[test]
    fn corpus_shaping_is_the_live_shaping_field_for_field() {
        use crate::strategies::updown_rtds::RtdsHub;

        let (start, end) = (1_787_442_300.0_f64, 1_787_442_600.0_f64);
        // A tape with a real shape to it: a trend, a mean-reverting stretch
        // (so rho is a number worth comparing), and marks on the minute.
        let px = |ts: i64| {
            let m = (ts - start as i64).div_euclid(60);
            2.50 + m as f64 * 0.004 + if ts % 2 == 0 { 0.0005 } else { -0.0005 }
        };
        let from = start as i64 - 3600;
        let rows = stream(from, end as i64 + 120, px);

        // --- live: a hub fed one frame at a time, as the socket does.
        let live_hub = RtdsHub::new();
        let live_feed = Arc::new(Mutex::new(FeedState::default()));
        let _live_sub = live_hub.register(
            "xrp/usd",
            settle_tw_secs(end - start),
            start,
            live_feed.clone(),
        );
        for r in &rows {
            let t_recv = r["t_recv"].as_f64().unwrap();
            if t_recv > end {
                break;
            }
            live_hub.ingest_price(
                r["topic"].as_str().unwrap(),
                r["symbol"].as_str().unwrap(),
                r["ts"].as_i64().unwrap() / 1000,
                r["value"].as_f64().unwrap(),
                t_recv,
            );
        }
        let live = live_feed.lock().unwrap().clone();

        // --- replay: the same stream out of a recorder corpus.
        let p = params("xrp-updown-5m-1787442300", start, end);
        let mut tl = RtdsTimeline::from_records(&p, &rows).unwrap();
        let replayed = tl.state_at(end);

        assert_eq!(replayed.spot, live.spot, "spot");
        assert_eq!(replayed.spot_ts, live.spot_ts, "spot_ts is the receive stamp, not the print's");
        assert_eq!(replayed.per_min, live.per_min, "settlement marks");
        assert_eq!(replayed.closes, live.closes, "1m closes");
        assert_eq!(replayed.rho, live.rho, "regime autocorr");
        assert_eq!(replayed.candle_open, live.candle_open, "never set on this feed");

        // And the numbers are real, not two identical empties.
        assert!(replayed.spot > 0.0);
        assert_eq!(replayed.closes.len(), 65, "one close a minute over the 65 minutes fed");
        assert!(replayed.rho.abs() > 0.0, "the alternating tape reads as a regime");
        assert_eq!(
            replayed.per_min.get(&(start as i64 - 60)),
            Some(&px(start as i64)),
            "the mark printed AT the start IS the range-start reference"
        );
    }

    #[test]
    fn the_stream_is_replayed_in_receive_order_however_the_file_is_written() {
        // The recorder writes from three topic handlers and its lines land
        // out of order (verified on the live corpus: consecutive rows go
        // .791, .773, .746). Routing them file-order would stamp spot_ts
        // backwards and bank the wrong sample as a minute's close.
        let (start, end) = (1_787_442_300.0_f64, 1_787_442_600.0_f64);
        let px = |ts: i64| 2.5 + (ts % 7) as f64 * 0.001;
        let mut rows = stream(start as i64 - 3600, end as i64 + 120, px);
        let ordered = RtdsTimeline::from_records(
            &params("xrp-updown-5m-1787442300", start, end),
            &rows,
        )
        .unwrap()
        .state_at(end);

        // Shuffle deterministically: reverse each 7-row run, so every
        // second is internally scrambled but the file still spans the same
        // range.
        for chunk in rows.chunks_mut(7) {
            chunk.reverse();
        }
        let shuffled = RtdsTimeline::from_records(
            &params("xrp-updown-5m-1787442300", start, end),
            &rows,
        )
        .unwrap()
        .state_at(end);

        assert_eq!(shuffled.spot, ordered.spot);
        assert_eq!(shuffled.spot_ts, ordered.spot_ts);
        assert_eq!(shuffled.per_min, ordered.per_min);
        assert_eq!(shuffled.closes, ordered.closes);
    }

    #[test]
    fn a_tick_never_sees_a_sample_that_had_not_arrived() {
        // The look-ahead cutoff, enforced on the receive clock. A window
        // replayed with tomorrow's prints already in the FeedState is the
        // classic backtest lie.
        let (start, end) = (1_787_442_300.0_f64, 1_787_442_600.0_f64);
        let rows = stream(start as i64 - 3600, end as i64 + 120, |ts| {
            if (ts as f64) < start + 100.0 { 2.50 } else { 9.99 }
        });
        let mut tl =
            RtdsTimeline::from_records(&params("xrp-updown-5m-1787442300", start, end), &rows)
                .unwrap();

        let early = tl.state_at(start + 50.0);
        assert_eq!(early.spot, 2.50, "the 9.99 prints have not happened yet");
        assert_eq!(early.spot_ts, start + 50.0 - 0.8, "stamped from the last sample that HAD");

        let later = tl.state_at(start + 150.0);
        assert_eq!(later.spot, 9.99, "and they land once the clock reaches them");
    }

    #[test]
    fn settlement_reads_the_windows_own_marks_after_draining_the_close() {
        // Marks for the final minute print AT the close, so a timeline
        // stopped at the last decision tick would settle on an incomplete
        // average. The drain is what makes the verdict the real one.
        let (start, end) = (1_787_442_300.0_f64, 1_787_442_600.0_f64);
        // The mark printed AT `start` averages the minute that just ENDED,
        // so it is the pre-window reference; the step belongs after it.
        let rows = stream(start as i64 - 3600, end as i64 + 120, |ts| {
            if (ts as f64) <= start { 2.50 } else { 2.60 }
        });
        let mut tl =
            RtdsTimeline::from_records(&params("xrp-updown-5m-1787442300", start, end), &rows)
                .unwrap();
        // Decisions only ever reached mid-window.
        let _ = tl.state_at(start + 120.0);

        let marks = tl.settle_marks(end);
        assert_eq!(marks.get(&(start as i64 - 60)), Some(&2.50), "the reference");
        for m in 0..5 {
            assert_eq!(
                marks.get(&(start as i64 + m * 60)),
                Some(&2.60),
                "every in-window minute banked, including the one printing at the close"
            );
        }
    }
}
