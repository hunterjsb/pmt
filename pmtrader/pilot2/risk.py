"""Hard risk law. Every constant here is a number the retrospective paid for.

None of these are tunable at runtime and none read an env var. That is
deliberate: `RETROSPECTIVE.md` §1.1 is the finding that a window firing 1-4
clips is a 95.5%-win / +3.05% RoN business and a window firing 5+ clips is a
79.8% / -9.48% one, with non-overlapping Wilson intervals in both eras. The
mechanism is a feedback loop — the book reprices against us, a saturated model
reads the cheaper ask as a LARGER edge, and buys more. A knob that can be
turned up is that loop's on-switch, so there is no knob.

The refusals are checked in a fixed order and each returns a NAMED reason, so
the shadow tape records which law stopped a would-be trade rather than just
its absence.
"""

from __future__ import annotations

import math
import time
from dataclasses import dataclass, field
from pathlib import Path

from polymarket.constants import taker_fee

# --- the caps -------------------------------------------------------------

# Total live notional across every open window. The whole EU wallet is ~$185
# and this pilot is a trust-ladder proof, not a size run; the loss leg of a
# lost window is exactly -100% of notional (sizing_method), so this number IS
# the maximum the pilot can lose in a single correlated event. rho ~= 0.77 and
# N_eff 1.2 across the fleet mean concurrent windows are one bet, not several.
MAX_TOTAL_EXPOSURE_USDC = 40.0

# One entry's dollar notional. Five dollars buys a real fill on these books
# without moving them, and 8 simultaneous windows at the cap still sit inside
# MAX_TOTAL_EXPOSURE_USDC.
MAX_CLIP_USDC = 5.0

# THE ACCELERATOR FIX. `size = clip_usdc / ask` means a fixed-dollar clip buys
# UNBOUNDED shares as the price falls: 34 clips fired below $0.50 carried
# 13.9% of every share the fleet ever bought. RETROSPECTIVE.md's closing line
# is that no study has ever tested a share cap; this is the cap. At the $5 clip
# it binds below ask 0.20, which is exactly the region where the dollar cap
# stops being a cap.
MAX_SHARES_PER_WINDOW = 25.0

# ONE clip per (window, side) EVER. Not "one at a time", not "one per N
# seconds" — one, for the life of the window, with no re-entry after the
# position is closed or the book moves. This is the escalation ban: the retro's
# loss engine is a SECOND clip into a falling book, so the pilot cannot build
# one. See RETROSPECTIVE.md §1.1.
MAX_CLIPS_PER_WINDOW_SIDE = 1

# No entry inside the last 30s of a window. The settlement average is drawn
# from [end-60, end], so an entry here is buying an outcome that is already
# half-printed, at a price the book has already moved to, with no time for the
# fill to be anything but adverse selection.
NO_ENTRY_FINAL_S = 30.0

# Filled inventory rides to resolution. There are no exits and no evacuation
# because the engine has none either (CLAUDE.md), and a paper policy that gets
# to be smarter than the executor is measuring a strategy nobody runs.
HOLD_TO_RESOLUTION = True

HALT_FILENAME = "HALT"


# --- named refusals -------------------------------------------------------

R_HALT = "halt_file"
R_CLIP_ALREADY_FIRED = "one_clip_per_window_side"
R_TOTAL_EXPOSURE = "max_total_exposure"
R_FINAL_SECONDS = "no_entry_final_30s"
R_NO_SIZE = "no_tradeable_size"
R_WINDOW_ENDED = "window_ended"
R_PAIRED_LOSS = "paired_sides_lock_a_loss"


def halt_path(home: Path) -> Path:
    return Path(home) / HALT_FILENAME


def halted(home: Path) -> bool:
    """Is the kill file present? Checked every loop AND again immediately
    before any order leaves the process — a file that appeared mid-poll must
    still stop that poll's order."""
    return halt_path(home).exists()


@dataclass(frozen=True)
class Sizing:
    """What a clip would actually be, after all three caps."""

    shares: float
    notional: float
    capped_by: str  # "clip" | "shares" | "book" | "exposure"


def size_clip(ask: float, ask_size: float, exposure_used: float) -> Sizing | None:
    """Shares to buy at `ask`, or None if nothing tradeable survives the caps.

    Order of the caps is the order they bind in, and each is recorded so the
    tape says WHICH one shaped the clip:
      * the dollar clip     — MAX_CLIP_USDC
      * the share cap       — MAX_SHARES_PER_WINDOW (the accelerator fix)
      * the quoted size     — never claim a fill larger than was on offer
      * the exposure budget — MAX_TOTAL_EXPOSURE_USDC across all windows
    """
    if not (0.0 < ask < 1.0):
        return None
    budget = min(MAX_CLIP_USDC, MAX_TOTAL_EXPOSURE_USDC - exposure_used)
    if budget <= 0.0:
        return None
    shares = budget / ask
    capped_by = "exposure" if budget < MAX_CLIP_USDC else "clip"
    if shares > MAX_SHARES_PER_WINDOW:
        shares, capped_by = MAX_SHARES_PER_WINDOW, "shares"
    # nan size means "depth unknown" (an unreadable book), not "nothing on
    # offer" — it must not silently zero the clip.
    if ask_size is not None and math.isfinite(ask_size) and ask_size > 0.0 \
            and shares > ask_size:
        shares, capped_by = float(ask_size), "book"
    if shares <= 0.0:
        return None
    return Sizing(shares=shares, notional=shares * ask, capped_by=capped_by)


@dataclass
class RiskBook:
    """The pilot's whole exposure ledger. One process, one book, in memory.

    Fired keys are (slug, side) and are NEVER cleared for a window that is
    still known — that is what makes MAX_CLIPS_PER_WINDOW_SIDE mean "ever"
    rather than "currently".

    In memory is not the same as forgotten: the LIVE book is rebuilt on startup
    from the order tape (`service.Pilot.rehydrate`), because a restart that
    forgets its fired keys can buy a still-open window a second time — the
    escalation the whole class exists to forbid, arriving through systemd.
    """

    home: Path
    fired: set[tuple[str, str]] = field(default_factory=set)
    # slug -> {"side", "shares", "notional", "ask", "end", "token", "t"}
    positions: dict[str, dict] = field(default_factory=dict)

    @property
    def exposure_used(self) -> float:
        return sum(p["notional"] for p in self.positions.values())

    def has_fired(self, slug: str, side: str) -> bool:
        return (slug, side) in self.fired

    def locks_a_paired_loss(self, slug: str, side: str, ask: float) -> bool:
        """Would buying `side` at `ask` guarantee a loss against the other side
        we already hold in this window?

        The measured policy takes whichever side clears costs, both sides, at
        any price. A live shadow run on 2026-08-24 showed what that means when
        a window whipsaws: it bought btc DOWN at 0.53 and, minutes later, btc
        UP at 0.53. Exactly one of those pays $1, so the overlapping shares
        cost 1.06 to collect 1.00 — a *guaranteed* 6c loss before fees, with no
        opinion in it at all.

        This is not the escalation ban wearing a hat: it never blocks a first
        clip, and it never blocks a second side that is genuinely cheap
        (asks + fees summing under $1 lock a PROFIT on the paired shares).
        It only refuses the arithmetic the retro flagged as "the -27c unpaired
        residual" and closed permanently for the incumbent.
        """
        held = self.positions.get(f"{slug}:{'down' if side == 'up' else 'up'}")
        if held is None or not (0.0 < ask < 1.0):
            return False
        held_ask = float(held["ask"])
        return ask + taker_fee(ask) + held_ask + taker_fee(held_ask) >= 1.0

    def refuse(self, slug: str, side: str, end: float, now: float | None = None,
               ask: float | None = None) -> str | None:
        """The named law that forbids this entry, or None if it is allowed.

        Cheapest and most absolute checks first: the kill file outranks
        everything, then the escalation ban, then the clock, then the budget,
        then the paired-loss check (which needs a price and so goes last).
        """
        now = time.time() if now is None else now
        if halted(self.home):
            return R_HALT
        if self.has_fired(slug, side):
            return R_CLIP_ALREADY_FIRED
        if now >= end:
            return R_WINDOW_ENDED
        if end - now <= NO_ENTRY_FINAL_S:
            return R_FINAL_SECONDS
        if self.exposure_used >= MAX_TOTAL_EXPOSURE_USDC:
            return R_TOTAL_EXPOSURE
        if ask is not None and self.locks_a_paired_loss(slug, side, ask):
            return R_PAIRED_LOSS
        return None

    def record_fill(self, slug: str, side: str, *, shares: float, notional: float,
                    ask: float, end: float, token: str = "", t: float | None = None) -> None:
        """Book a clip. Marks the (slug, side) fired FIRST so a partial fill,
        a duplicate ack, or an exception downstream can never earn a re-entry."""
        self.fired.add((slug, side))
        self.positions[f"{slug}:{side}"] = {
            "slug": slug, "side": side, "shares": shares, "notional": notional,
            "ask": ask, "end": end, "token": token, "t": time.time() if t is None else t,
        }

    def retire_settled(self, now: float | None = None, grace_s: float = 300.0) -> list[dict]:
        """Drop positions whose window resolved long enough ago to be swept.

        Releasing the exposure is what lets the next window use the budget;
        the position itself is handed back so the caller can queue it for the
        redeem sweep. `fired` is deliberately NOT cleared — a window that has
        ended can never be re-entered anyway, and keeping the key means a
        replayed/duplicated window slug cannot buy twice.
        """
        now = time.time() if now is None else now
        done = [k for k, p in self.positions.items() if now >= p["end"] + grace_s]
        return [self.positions.pop(k) for k in done]
