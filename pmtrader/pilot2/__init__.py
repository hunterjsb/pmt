"""pilot2 — the Strategy 2.0 interim pilot.

A standalone service, NOT part of pmengine. It runs under its own systemd unit,
holds its own state under `~/.pmt/pilot2/`, and shares nothing mutable with the
engine except the read-only corpora.

What it is: `calibrated_model.md`'s shipping recommendation, live. A calibrated
model of the CORRECT settlement quantity (`predict.terminal_p_up`, ~20 flops,
no learned artefact) blended with the de-vigged book at a walk-forward weight,
gated on EV rather than on price level. That policy is the one estimator in the
study whose paper P&L interval excludes zero (+15.6c/$ over 199 windows, CI
[+7.7, +23.0]) and it is profitable in every price bucket.

What it is NOT: an engine. There is no escalation, no averaging down, no exit
policy and no size ladder. `RETROSPECTIVE.md` §1.1 measured a window that fires
1-4 clips at 95.5% / +3.05% RoN and a window that fires 5+ at 79.8% / -9.48%,
intervals non-overlapping in both eras. The loss engine is a SECOND clip into a
falling book, so this pilot cannot build one: one clip per window per side,
ever, with a share cap so a fixed-dollar clip cannot buy unbounded shares.

Modes:
  SHADOW (default) — price the majors, log what we WOULD have done, place
                     nothing. Graded later by `pilot2 grade` against gamma.
  LIVE   (off)     — `--live` AND `PILOT2_LIVE=1`, only on series in
                     PILOT2_SERIES, and never one an engine owns.
"""

from __future__ import annotations

__all__ = ["books", "cli", "execution", "grade", "policy", "predict",
           "risk", "series", "service", "state", "status", "stream", "windows"]
