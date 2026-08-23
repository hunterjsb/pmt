# systemd user units (proposal)

Three units, none installed. They are checked in as a proposal so adopting
them is a decision with a diff behind it, not a thing that happened.

| unit | what it runs | restarts? |
| --- | --- | --- |
| `pmengine.service` | `pmengine ... run updown --skip-warmup`, same as `pmt engine start` | on failure, 5s→120s backoff, 5 tries / 5min |
| `pmt-rtds-recorder.service` | `uv run python -m polymarket.rtds` in `pmtrader/` | on failure, 10s→300s backoff, 10 tries / 10min |
| `pmt-backup.service` + `.timer` | `scripts/pmt-backup.sh` — ~/.pmt corpus + tapes → `s3://xanmc/pmt-backups/YYYY-MM-DD.tar.zst` | no. oneshot, daily 03:30, `Persistent=true` |

## THE CAVEAT: auto-restart interacts with arm recovery

That interaction is the point of these units, and it is also the reason not
to enable them casually.

The engine now persists its live arms to `~/.pmt/engine/arms-state.json` on
every mutation. On startup it reads that file back and:

- **re-arms every window still open** (`now < end`) — token ids come from the
  file, so no gamma call and no operator in the loop;
- **resumes roll chains** whose window closed while it was down, hopping
  forward to the current window;
- **drops entries whose window ended** with no roll, and shouts (log +
  ntfy/Discord) about any position still sitting on those tokens.

So `Restart=on-failure` does not just bring a process back — **it brings the
positions back under management.** An engine that dies at 02:00 with three
armed windows resumes hunting them at 02:00:05 with nobody awake. That is
the intended behaviour. It is also full autonomy, and it is exactly what the
operator is opting into.

Before enabling, be sure you want:

1. A crashed engine to re-enter the market unattended. If the crash was
   *caused* by market conditions, the restart walks straight back into them.
   The 5-tries/5-minutes start limit is the only brake.
2. Roll chains to keep rolling across a restart. `pmt crypto disarm` is
   still the off switch — a disarm deletes from the state file, so a
   disarmed market cannot come back from a restart (tested:
   `disarm_deletes_the_persisted_arm`).
3. Recovery to be inert when the file is absent. It is: no file, no arms,
   no writes. Deleting `~/.pmt/engine/arms-state.json` while the engine is
   stopped is a hard reset.

## The backup unit is the one without a caveat

`pmt-backup` reads files and writes to S3. It never places an order, never
touches the engine, and cannot bring a position back — so unlike the two units
above, enabling it is not a risk decision.

What it ships, once a day, as one `.tar.zst` object:

- `~/.pmt/corpus/**` — the RTDS stream recording (already one file per day, so
  a restore can take a single dark day back), the klines and Chainlink pulls
  behind every replay, the outcomes and activity corpora.
- `~/.pmt/engine/*.jsonl` — `updown-tape`, `book-tape`, `order-latency-tape`.
- `~/.pmt/engine/arms-state.json` — what comes back after a restart.

What it deliberately leaves behind: the rotated engine logs (`*.log`,
`*.log.gz`) and the recorder's own log/pidfile. They are the bulk of the
directory and the tapes already carry structurally what the logs carry in
prose.

Two things worth knowing before enabling it:

1. **It reads tapes the engine is actively appending to.** `tar` exits 1 with
   "file changed as we read it", which the script treats as normal rather than
   fatal — a JSONL tail torn mid-line is exactly what `tape.iter_records`
   already skips, so the archive stays restorable.
2. **One object per day, skipped if it already exists.** That is what makes
   `Persistent=true` safe: the box powers off nightly, the 03:30 run is missed
   more often than not, and the catch-up run on the next boot costs one
   `s3 ls` if the day is already up there. `--force` overwrites; `--dry-run`
   prints the member list and the destination and uploads nothing.

`--date YYYY-MM-DD` backfills under another day's key. Env overrides:
`PMT_HOME`, `PMT_BACKUP_S3`, `PMT_AWS`, `PMT_BACKUP_ZSTD_LEVEL`,
`PMT_BACKUP_TMPDIR` (staging is `/var/tmp` by default and never `/tmp`, which
is tmpfs on this box).

## Install

```sh
mkdir -p ~/.config/systemd/user
cp deploy/systemd/pmengine.service ~/.config/systemd/user/
cp deploy/systemd/pmt-rtds-recorder.service ~/.config/systemd/user/
cp deploy/systemd/pmt-backup.service ~/.config/systemd/user/
cp deploy/systemd/pmt-backup.timer ~/.config/systemd/user/
systemctl --user daemon-reload
```

The backup, end to end, before trusting the timer with it:

```sh
scripts/pmt-backup.sh --dry-run          # member list + destination, no upload
systemctl --user start pmt-backup.service
journalctl --user -u pmt-backup -n 50

systemctl --user enable --now pmt-backup.timer
systemctl --user list-timers pmt-backup.timer
```

Start one without enabling it at boot (the way to try it):

```sh
systemctl --user start pmt-rtds-recorder.service
systemctl --user status pmt-rtds-recorder.service
journalctl --user -u pmt-rtds-recorder.service -f
```

Enable at login once you trust it:

```sh
systemctl --user enable --now pmengine.service
```

Survive logout / start at boot without a session (otherwise user units stop
when the last session ends):

```sh
sudo loginctl enable-linger hunter
```

Stop and disable:

```sh
systemctl --user disable --now pmengine.service
```

## Interactions with the existing CLI

- **Don't run both.** `pmt engine start` and the unit launch the same binary,
  and the second one to start fails fast on the control-plane port
  (127.0.0.1:7531) — by design, two engines quoting the same token is a real
  hazard. Pick one lifecycle.
- **`pmt engine kill` half-works on a unit-started engine.** It has no pidfile
  to read, so it falls through to `pkill -f 'pmengine.*run'`. That is a clean
  SIGTERM (exit 0), so `Restart=on-failure` correctly does *not* resurrect it
  — but systemd still thinks it owns the unit. Use `systemctl --user stop`.
- **`pmt engine stop updown` does not clear the arm state.** It removes the
  strategy from the runtime and unsubscribes its tokens, but the arms stay on
  disk and come back on the next start. `on_shutdown` can't tell "the
  operator is halting this strategy" from "the box is powering off", and
  clearing on the second one is precisely the hole this work closed. To stop
  and stay stopped: `pmt crypto disarm` first, *then* `pmt engine stop
  updown`.
- **Logs move.** A unit-started engine writes to the journal, not to
  `~/.pmt/engine/engine-*.log`, so `pmt engine logs` shows the last
  hand-started run. Use `journalctl --user -u pmengine -f`.
- **The durable tapes don't move.** `updown-tape.jsonl`, `book-tape.jsonl`,
  `oracle-tape.jsonl` and `arms-state.json` are all still under
  `~/.pmt/engine/`.

## Nightly poweroff

The box powers off on purpose every night. Poweroff sends SIGTERM, the engine
exits 0, `Restart=on-failure` does nothing — no restart storm on the way down.

On the next boot, if the units are enabled (and lingering is on), the engine
reads `arms-state.json` and **resumes the roll chain** for anything that was
armed with `--roll` when the box went down — hopping forward across the whole
dark span to the current window — and reports any position left riding on the
windows that resolved overnight. Arms retired with `pmt crypto disarm` before
poweroff do not come back. Decide which of those two you want *before*
enabling `pmengine.service`.
