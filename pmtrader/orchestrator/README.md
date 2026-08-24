# pmt fleet orchestrator

Cross-node health for the trading fleet, and the lease protocol behind automatic
series failover. **The design lives in `orchestrator/DESIGN.md` at the repo
root** — the protocol, the failure matrix, and the argument that no interleaving
ever puts two engines on one book. Read that first; this file is just how to run
the thing.

**Phase 1, which is what is built, places no orders, takes no lease, and touches
no arm.** It writes heartbeats and reads them back.

## Why it is not `pmt fleet`

`pmt crypto fleet` already exists and means the R7 fleet-wide un-decided
exposure cap. And this follows pilot2's rule: a standalone service with its own
unit and its own exit-code contract gets its own entry point, which keeps it one
typo further from the fleet's arms.

## Commands

```sh
pmt-fleet map                  # print and validate the assignment map
pmt-fleet table --dry-run      # the CreateTable spec, creates nothing
pmt-fleet table                # create pmt-fleet (idempotent)
pmt-fleet beat --once          # one heartbeat
pmt-fleet beat                 # the daemon: a heartbeat every 30s
pmt-fleet check                # fleet status; nonzero if it needs attention
pmt-fleet check --json         # same, machine-readable
pmt-fleet kill-switch          # show / --on / --off
```

`python -m orchestrator <verb>` works identically and is what the unit uses.

### Exit codes

| code | meaning |
| --- | --- |
| 0 | nothing needs attention |
| 1 | something does — a wedged node, a lapsed lease, a series nobody holds |
| 2 | the assignment map is refused (config error) |
| 3 | the store is unreachable — **the doctor is blind**, which says nothing about the engines |

The code answers "does something need attention?", not "is everything running?".
Those differ on the case this fleet hits nightly: Hunter powers the desktop off
on purpose, and a checker that goes red on that is a checker whose red means
nothing by Friday. `--strict` asks the other question.

## Configuration

- `orchestrator/assignments.json` — who is home for what. Validated on every
  load; a bad map is fatal, never a warning.
- `PMT_FLEET_NODE` — this node's id. Must be a key in the map. Falls back to
  `$PMT_FLEET_MAP`'s hostname, which is a convenience on the desktop and a
  hazard anywhere a box gets renamed, so the daemon logs which it used.
- `PMT_FLEET_MAP` — an alternate map path.
- AWS credentials come from the standard chain. **The daemon reads no secrets of
  its own**: it never opens `.env`, never touches a private key, and reads
  `arms-state.json` by path for slug names only.

## Files

| file | what |
| --- | --- |
| `lease.py` | the protocol — fence, claim bar, grace arithmetic, skew bound |
| `store.py` | DynamoDB, plus a fake with real CAS semantics |
| `clock.py` | this node's offset from the store's clock |
| `assign.py` | the map, and every reason to refuse one |
| `heartbeat.py` | what a node says about itself |
| `check.py` | the checker |
| `notify.py` | mubs attention, or `--notify-cmd` |
| `table.py` | table creation and the cost guards |

Tests: `tests/test_fleet_lease.py`, `test_fleet_store.py`, `test_fleet_doctor.py`.
The one to read first is
`test_no_interleaving_ever_puts_two_nodes_on_one_book` — it drives the safety
property through random crashes, partitions, store outages and in-bound clock
skew, and it has a companion test proving the simulation is not vacuous.
