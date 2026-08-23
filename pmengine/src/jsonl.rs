//! Durable JSONL tape plumbing shared by every cross-session writer — the
//! eval/fire tape, the book/spot recorder, oracle basis samples, and the
//! order-latency tape. Lives outside `strategies/` so the tape writers the
//! whole crate uses never depend on any one strategy module.

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
