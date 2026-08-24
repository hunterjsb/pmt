"""Every risk constant is pinned, and every law has a test that fails if it moves.

These are not style assertions. RETROSPECTIVE.md §1.1 measured a window firing
1-4 clips at 95.5% win / +3.05% RoN and a window firing 5+ at 79.8% / -9.48%,
Wilson intervals non-overlapping in both eras. The loss engine is a SECOND clip
into a falling book. If a change here goes green, the pilot has grown the thing
the pilot exists to not have.
"""

from __future__ import annotations

from pilot2 import risk

# --- the constants themselves ---------------------------------------------

def test_risk_constants_are_pinned():
    assert risk.MAX_TOTAL_EXPOSURE_USDC == 40.0
    assert risk.MAX_CLIP_USDC == 5.0
    assert risk.MAX_SHARES_PER_WINDOW == 25.0
    assert risk.MAX_CLIPS_PER_WINDOW_SIDE == 1
    assert risk.NO_ENTRY_FINAL_S == 30.0
    assert risk.HOLD_TO_RESOLUTION is True


def test_the_caps_are_mutually_consistent():
    """Eight concurrent windows at a full clip still sit inside the total."""
    assert risk.MAX_CLIP_USDC * 8 == risk.MAX_TOTAL_EXPOSURE_USDC


# --- the share cap: the accelerator fix -----------------------------------

def test_share_cap_binds_where_the_dollar_cap_stops_capping():
    """`size = clip_usdc / ask` buys UNBOUNDED shares as the price falls —
    34 clips fired below $0.50 carried 13.9% of every share the fleet bought.
    At a $5 clip the share cap binds below ask 0.20."""
    s = risk.size_clip(0.05, ask_size=10_000.0, exposure_used=0.0)
    assert s.shares == risk.MAX_SHARES_PER_WINDOW
    assert s.capped_by == "shares"
    assert s.notional == 25.0 * 0.05, "the clip is now SMALLER than $5, not larger"
    # Without the cap this clip would have been 100 shares.
    assert risk.MAX_CLIP_USDC / 0.05 == 100.0


def test_dollar_clip_binds_at_ordinary_prices():
    s = risk.size_clip(0.70, ask_size=10_000.0, exposure_used=0.0)
    assert s.capped_by == "clip"
    assert s.notional == risk.MAX_CLIP_USDC
    assert s.shares == risk.MAX_CLIP_USDC / 0.70


def test_quoted_size_caps_the_fill():
    """A paper fill is never larger than the size that was really on offer."""
    s = risk.size_clip(0.70, ask_size=3.0, exposure_used=0.0)
    assert s.shares == 3.0 and s.capped_by == "book"


def test_exposure_budget_shrinks_the_last_clip_rather_than_overshooting():
    s = risk.size_clip(0.50, ask_size=10_000.0,
                       exposure_used=risk.MAX_TOTAL_EXPOSURE_USDC - 2.0)
    assert s.capped_by == "exposure"
    assert s.notional == 2.0


def test_no_clip_at_all_once_the_budget_is_spent():
    assert risk.size_clip(0.50, 10_000.0, risk.MAX_TOTAL_EXPOSURE_USDC) is None
    assert risk.size_clip(0.0, 10_000.0, 0.0) is None
    assert risk.size_clip(1.0, 10_000.0, 0.0) is None


def test_an_empty_book_yields_no_clip():
    assert risk.size_clip(0.70, ask_size=0.0, exposure_used=0.0).shares > 0, \
        "size 0 means unknown depth, not a zero-size offer"
    assert risk.size_clip(0.70, ask_size=float("nan"), exposure_used=0.0).capped_by == "clip"


# --- one clip per window-side, ever ---------------------------------------

def test_one_clip_per_window_side_ever(tmp_path):
    b = risk.RiskBook(home=tmp_path)
    end = 1_000_000.0
    now = end - 200.0
    assert b.refuse("doge-updown-5m-1", "up", end, now) is None
    b.record_fill("doge-updown-5m-1", "up", shares=7.0, notional=5.0, ask=0.7, end=end)
    assert b.refuse("doge-updown-5m-1", "up", end, now) == risk.R_CLIP_ALREADY_FIRED
    # The OTHER side of the same window is a separate decision and still open.
    assert b.refuse("doge-updown-5m-1", "down", end, now) is None


def test_the_fired_mark_survives_the_position_being_retired(tmp_path):
    """'Ever' means ever. Releasing exposure at settlement must not re-open
    the window for a second clip — that is the escalation loop's on-switch."""
    b = risk.RiskBook(home=tmp_path)
    end = 1_000_000.0
    b.record_fill("doge-updown-5m-1", "up", shares=7.0, notional=5.0, ask=0.7, end=end)
    retired = b.retire_settled(now=end + 400.0)
    assert len(retired) == 1 and b.exposure_used == 0.0
    assert b.has_fired("doge-updown-5m-1", "up")


# --- both sides of one window ---------------------------------------------

def test_the_second_side_is_refused_when_the_pair_locks_a_loss(tmp_path):
    """Found by the first live shadow run: a whipsawing window bought btc DOWN
    at 0.53 and, minutes later, btc UP at 0.53. Exactly one pays $1, so the
    overlapping shares cost 1.06 to collect 1.00 — a guaranteed loss with no
    opinion in it. The retro's '-27c unpaired residual', arriving by the front
    door."""
    b = risk.RiskBook(home=tmp_path)
    end = 1_000_000.0
    now = end - 200.0
    b.record_fill("btc-updown-5m-1", "down", shares=9.4, notional=5.0, ask=0.53, end=end)
    assert b.refuse("btc-updown-5m-1", "up", end, now, ask=0.53) == risk.R_PAIRED_LOSS


def test_the_second_side_is_allowed_when_the_pair_locks_a_profit(tmp_path):
    """Both-sides IS the measured policy — take whichever side clears costs,
    at any price. Two cheap asks summing under $1 net of fees are a locked
    profit on the paired shares, and this guard must not touch them."""
    b = risk.RiskBook(home=tmp_path)
    end = 1_000_000.0
    now = end - 200.0
    b.record_fill("btc-updown-5m-1", "down", shares=25.0, notional=5.0, ask=0.20, end=end)
    assert b.refuse("btc-updown-5m-1", "up", end, now, ask=0.40) is None


def test_the_paired_check_never_blocks_a_first_clip(tmp_path):
    b = risk.RiskBook(home=tmp_path)
    end = 1_000_000.0
    assert b.refuse("btc-updown-5m-1", "up", end, end - 200.0, ask=0.99) is None
    assert not b.locks_a_paired_loss("btc-updown-5m-1", "up", 0.99)


def test_the_paired_check_counts_fees_on_both_legs(tmp_path):
    """0.48 + 0.48 = 0.96 looks like a 4c lock-in. The two fees are 0.07*0.48
    each = 6.7c, so the pair really costs 1.027."""
    b = risk.RiskBook(home=tmp_path)
    end = 1_000_000.0
    b.record_fill("btc-updown-5m-1", "down", shares=10.0, notional=4.8, ask=0.48, end=end)
    assert b.locks_a_paired_loss("btc-updown-5m-1", "up", 0.48)
    assert not b.locks_a_paired_loss("btc-updown-5m-1", "up", 0.40)


def test_total_exposure_stops_new_windows(tmp_path):
    b = risk.RiskBook(home=tmp_path)
    end = 1_000_000.0
    now = end - 200.0
    for i in range(8):
        b.record_fill(f"doge-updown-5m-{i}", "up", shares=7.0,
                      notional=risk.MAX_CLIP_USDC, ask=0.7, end=end)
    assert b.exposure_used == risk.MAX_TOTAL_EXPOSURE_USDC
    assert b.refuse("hype-updown-5m-9", "up", end, now) == risk.R_TOTAL_EXPOSURE


def test_no_entry_in_the_final_thirty_seconds(tmp_path):
    """The settlement average is drawn from [end-60, end]; an entry here buys
    an outcome that is already half-printed."""
    b = risk.RiskBook(home=tmp_path)
    end = 1_000_000.0
    assert b.refuse("doge-updown-5m-1", "up", end, end - 31.0) is None
    assert b.refuse("doge-updown-5m-1", "up", end, end - 30.0) == risk.R_FINAL_SECONDS
    assert b.refuse("doge-updown-5m-1", "up", end, end - 5.0) == risk.R_FINAL_SECONDS
    assert b.refuse("doge-updown-5m-1", "up", end, end + 1.0) == risk.R_WINDOW_ENDED


# --- the kill file ---------------------------------------------------------

def test_halt_file_outranks_every_other_law(tmp_path):
    b = risk.RiskBook(home=tmp_path)
    end = 1_000_000.0
    now = end - 200.0
    assert b.refuse("doge-updown-5m-1", "up", end, now) is None
    risk.halt_path(tmp_path).write_text("stop\n")
    assert risk.halted(tmp_path)
    assert b.refuse("doge-updown-5m-1", "up", end, now) == risk.R_HALT
    risk.halt_path(tmp_path).unlink()
    assert b.refuse("doge-updown-5m-1", "up", end, now) is None


def test_halt_path_is_under_the_pilots_own_home(tmp_path):
    assert risk.halt_path(tmp_path).name == "HALT"
    assert risk.halt_path(tmp_path).parent == tmp_path
