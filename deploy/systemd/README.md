# systemd user units (proposal)

Seven units, none installed. They are checked in as a proposal so adopting
them is a decision with a diff behind it, not a thing that happened.

| unit | what it runs | restarts? |
| --- | --- | --- |
| `pmengine.service` | `pmengine ... run updown --skip-warmup`, same as `pmt engine start` | on failure, 5s→120s backoff, 5 tries / 5min |
| `pmt-rtds-recorder.service` | `uv run python -m polymarket.rtds` in `pmtrader/` | on failure, 10s→300s backoff, 10 tries / 10min |
| `pmt-print-recorder.service` | `uv run python -m polymarket.prints` in `pmtrader/` | on failure, 10s→300s backoff, 10 tries / 10min |
| `pmt-spot-recorder.service` | `uv run python -m polymarket.spot` in `pmtrader/` — **~5 GB/day at full width, read its header first** | on failure, 10s→300s backoff, 10 tries / 10min |
| `pmt-pilot2.service` | `uv run python -m pilot2 run` in `pmtrader/` — **SHADOW, places no orders** | on failure, 10s→300s backoff; **never** on exit 2 |
| `pmt-fleet-doctor.service` | `uv run python -m orchestrator beat` in `pmtrader/` — **observation only, places no orders** | on failure, 10s→300s backoff; **never** on exit 2 |
| `pmt-backup.service` + `.timer` | `scripts/pmt-backup.sh` — ~/.pmt corpus + tapes → `s3://xanmc/pmt-backups/YYYY-MM-DD.tar.zst` | no. oneshot, daily 03:30, `Persistent=true` |

## The fleet doctor, and the stamp that keeps the nightly poweroff quiet

`pmt-fleet-doctor.service` writes one row to the `pmt-fleet` DynamoDB table
every 30 seconds saying what this node can see about itself — engine up, feed
age, balance, which series are armed. That is all it does. It takes no lease,
places no order and touches no arm; the lease protocol it will eventually run
(`orchestrator/DESIGN.md`) is built and tested but nothing calls its mutating
path yet.

Like the backup unit and unlike the other four, **this one has no caveat
section**. It cannot bring a position back, cannot re-enter the market and
cannot stop the engine. Enabling it is not a risk decision.

The one thing to know is the clean-shutdown stamp. On SIGTERM the daemon writes
a final heartbeat carrying `shutdown`, which is how the checker tells "Hunter
turned the box off" from "the box wedged" — the same marker discipline the mubs
worker uses, for the same reason. Two consequences:

- **`TimeoutStopSec=20` is leaving room for that last write.** Do not trim it.
- **`systemctl --user kill -s SIGKILL` skips it**, and the node then looks
  wedged for as long as the checker's staleness window. Use `stop`.

The stamp self-clears — the heartbeat write replaces the row whole, so the first
live beat after the next boot removes it with nobody having to remember.

Before enabling, the two commands worth running:

```sh
(cd pmtrader && uv run python -m orchestrator map)          # validates the assignment map
(cd pmtrader && uv run python -m orchestrator check)        # what the fleet looks like now
```

## The pilot unit, and the two switches it deliberately does not throw

`pmt-pilot2.service` runs the Strategy 2.0 interim pilot
(`pmtrader/pilot2/README.md`) in SHADOW mode. It prices the majors, writes down
what it WOULD have traded, and places nothing.

Live requires **both** `PILOT2_LIVE=1` and `--live` on the ExecStart line, and
the unit ships with `Environment=PILOT2_LIVE=0` and no flag. One of them alone
is a mistake somebody could make — a flag survives a copied command line, an
env var survives a unit file — so it takes two edits in two different places.

Three things about this unit that differ from the recorders:

- **`RestartPreventExitStatus=2`.** Exit 2 is a REFUSED series partition: the
  pilot was configured onto a series a running engine already trades, which is
  wash-trade shaped. A restart cannot fix a config error and a unit that keeps
  bouncing on one hides it. It stays visibly stopped.
- **The kill file beats systemctl.** `touch ~/.pmt/pilot2/HALT` stops the pilot
  at the top of its next poll (≤2s) and it exits 0, so `Restart=on-failure`
  leaves it down. Remove the file before restarting. Filled positions ride to
  resolution either way — that is the strategy, not a degraded mode.
- **A gap here is not a corpus gap.** The recorders write data nobody can
  re-observe; the pilot writes a decision log. Restart-on-failure is still
  right (a pilot running half the day produces a record nobody can conclude
  from), but this is the unit to stop first when something is wrong.

It shares nothing mutable with `pmengine.service`: separate process, separate
state directory (`~/.pmt/pilot2`), separate wallet when live, and a series
partition checked at startup.

## The spot recorder, and the one that can actually fill the disk

`pmt-spot-recorder.service` records the underlying the oracle follows.
`opponent_model.md` §1d splits the makers' ~3s lead over our settlement feed
into ~1.7s of our own relay and **~1.3s of real information**: they are pricing
the spot market Chainlink is a lagging function of. The relay half is plumbing;
the other half is only closed by reading the same spot they read.

Three connections, three files, one process — Binance (`data-stream.binance.vision`,
because `stream.binance.com` answers **HTTP 451** from this box), Kraken v2, and
Hyperliquid for HYPE. Every venue is its own thread and its own socket, so a
stall on one cannot silence another.

**It is the first unit here that can fill the disk.** Measured over the study
run: Binance alone is ~370 rows/s and ~215 MB/h, which is ~5 GB/day — three
orders of magnitude past anything else in `~/.pmt/corpus`, and it has no
`--retention-days` sweep like the print recorder does. The unit's `ExecStart`
is therefore narrowed to the low-volume venues on purpose; widening it is a
decision to make deliberately, after picking a retention story. Until then the
honest way to run it is bounded — `--minutes N`, which is how the lead study
ran it.

Unlike prints, this tape is **not backfillable**: neither Binance nor Kraken
serves a free historical tick feed, so an hour nobody recorded is an hour the
lead estimate can never be measured on. Same argument as the RTDS unit.

`--once` is the smoke test: one connection attempt per venue, no reconnect,
and a **nonzero exit** if any venue received zero frames or nothing parsed.
That exit contract exists because the btc1h sampler produced a zero-byte file
and a zero exit code for a day before anyone noticed.

## The print recorder, and why it is a second unit

`~/.pmt/corpus/prints.jsonl` was written by a one-shot backfill
(`analysis/firsthalf_harvest_prints.py`, now in the private vault). It walked
the windows the book tape happened to hold, exhausted that list, and exited 0.
Nothing killed it — **it was never continuous**, and its coverage stops at the
newest window that existed when it ran:

```
legacy prints.jsonl   167,941 prints   2026-08-22T02:59:36Z → 08-23T07:39:00Z
rtds corpus                            2026-08-23T08:28:55Z → forward, forever
```

Fifty minutes of dead space between them and **not one overlapping instant**.
That makes print-vs-stream lead — does Polymarket print flow move before or
after the Chainlink stream these markets settle on — not merely noisy but
unmeasurable. `polymarket.prints` closes it by harvesting continuously, over
the same windows, for as long as both recorders are up. On first start it finds
~900 windows the backfill never covered, everything after 08:28:55Z of which
overlaps the stream corpus.

- **It polls `data-api /trades` after the bell, not during.** That endpoint
  serves full print history per market long after a window resolves, and the
  print timestamps are the exchange's, so a post-close harvest preserves the
  lead signal at a fraction of the request budget a live poller would spend.
  `--settle-lag` (default 180 s) is the wait; `--max-per-scan` (default 40)
  caps a catch-up burst so a recorder that has been down for a day cannot open
  the throttle on data-api all at once.
- **Its window list is the engine's own book tape**, the same source the
  backfill used, so the corpora stay comparable and it can only ever harvest
  windows the engine actually watched.
- **`prints-YYYYMMDD.jsonl`, rotated on PRINT time** rather than harvest time,
  so a day's prints are one file that joins to `rtds-YYYYMMDD.jsonl` with no
  filter. Unlike the RTDS corpus it is also **bounded**: `--retention-days`
  (default 30) prunes old files, because print volume tracks market activity
  rather than wall clock and the backfill alone was 54 MB.
- **It seeds from the existing corpus at startup** — daily files plus the
  legacy `prints.jsonl` — so a restart never re-fetches what it already has,
  and the two files concatenate field-for-field with no shim.

Why not fold it into `pmt-rtds-recorder.service`: that recorder is a websocket
consumer whose whole job is to not miss a frame, and this one makes blocking
REST calls to a host measured stalling for whole seconds under contention
(`analysis/watch_load.md`). A multi-second HTTP call inside the stream reader's
loop is the exact bug that document is about. Separate processes cannot do
that to each other.

Unlike the stream, prints are backfillable, so a gap here is recoverable —
`uv run python -m polymarket.prints --once --stdout` runs a single catch-up
pass and exits. That also makes this the safer of the two to try first.

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
cp deploy/systemd/pmt-print-recorder.service ~/.config/systemd/user/
# read the disk note in this one's header before widening --symbols/--venues
cp deploy/systemd/pmt-spot-recorder.service ~/.config/systemd/user/
cp deploy/systemd/pmt-pilot2.service ~/.config/systemd/user/
cp deploy/systemd/pmt-fleet-doctor.service ~/.config/systemd/user/
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

The pilot, before enabling it — the partition check exits 2 if it is pointed
at a series an engine already trades, and that is the whole safety story:

```sh
(cd pmtrader && uv run python -m pilot2 series); echo "exit $?"
systemctl --user start pmt-pilot2.service
(cd pmtrader && uv run python -m pilot2 status)
touch ~/.pmt/pilot2/HALT      # stops it within one poll; rm before restarting
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
