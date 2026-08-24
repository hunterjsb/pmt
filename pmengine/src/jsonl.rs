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
///
/// The record is serialised into ONE buffer and handed to ONE `write_all`.
/// `writeln!(f, "{}", record)` looks equivalent and is not: `Write::write_fmt`
/// on a bare `File` issues a syscall per fragment the formatter emits, and
/// `serde_json::Value`'s `Display` emits every key, colon and comma
/// separately — so two appenders racing this function interleave INSIDE a
/// token and produce lines no reader can parse. 26 such lines are in
/// `updown2-tape.jsonl` (2026-08-24), written by parallel `cargo test`
/// threads. A single `write_all` under `O_APPEND` is atomic against other
/// appenders on a regular file, which is the property the tape needs.
pub(crate) fn append_jsonl(file_name: &str, record: serde_json::Value) {
    let Some(dir) = engine_dir() else { return };
    let _ = std::fs::create_dir_all(&dir);
    append_line(&dir.join(file_name), &record);
}

/// One record, one `write_all`, to an explicit path. Split out so the
/// atomicity above is testable without pointing $HOME somewhere — a test that
/// mutated $HOME would change what every OTHER test in this binary's thread
/// pool resolves `engine_dir()` to.
fn append_line(path: &std::path::Path, record: &serde_json::Value) {
    use std::io::Write;
    if let Ok(mut f) = std::fs::OpenOptions::new().create(true).append(true).open(path) {
        let mut line = record.to_string();
        line.push('\n');
        let _ = f.write_all(line.as_bytes());
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    /// The corruption this shape exists to make impossible: many threads
    /// appending at once must never produce a line that is not one record.
    /// Under the old `writeln!(f, "{}", record)` this tears reliably.
    #[test]
    fn concurrent_appenders_never_interleave_inside_a_line() {
        let dir = std::env::temp_dir().join(format!("pmt-jsonl-{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&dir);
        std::fs::create_dir_all(&dir).expect("tmp dir");
        let path = dir.join("race.jsonl");

        std::thread::scope(|s| {
            for w in 0..8u32 {
                let path = path.clone();
                s.spawn(move || {
                    for i in 0..250u32 {
                        append_line(
                            &path,
                            &serde_json::json!({
                                "writer": w, "i": i,
                                "pad": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
                            }),
                        );
                    }
                });
            }
        });

        let body = std::fs::read_to_string(&path).expect("tape");
        let lines: Vec<&str> = body.lines().filter(|l| !l.is_empty()).collect();
        assert_eq!(lines.len(), 8 * 250, "every record is exactly one line");
        for l in &lines {
            serde_json::from_str::<serde_json::Value>(l)
                .unwrap_or_else(|e| panic!("torn line: {e}: {l}"));
        }
        let _ = std::fs::remove_dir_all(&dir);
    }
}
