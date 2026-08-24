"""pmt fleet orchestrator — cross-node health, and the lease protocol behind failover.

Phase 1 (this code) observes: heartbeats in, fleet status out, pages through
mubs. Phase 2 (specified in orchestrator/DESIGN.md, not built) turns the lease
protocol here into actual series failover.

The one invariant everything serves: two engines under one operator must never
quote the same series at the same instant.
"""
