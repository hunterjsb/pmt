"""Momentum-Fade Spike Detector.

Watches a small set of tokens for volume spikes. When the last `SHORT_WINDOW`
seconds of volume on a token exceeds `SPIKE_MULTIPLIER` × the recent baseline
volume (computed from the prior `LONG_WINDOW` seconds), raise a `Signal::Alert`
proposing a fade trade: a Sell at the current mid. The human reviews the
alert (typically by checking whether @Polymarket / Trump / etc. just tweeted
about the market) and approves or rejects via `pmt engine approve|reject`.

This strategy doesn't *itself* identify whether the move is informed or
sheep-driven — that's the human-touch step. It just flags candidates where
the volume profile looks like a coordinated pump worth a closer look.

Designed for the engine's Phase 5 alert pipeline: the strategy never places
a real order — every action is gated on human approval.
"""

from decimal import Decimal

from ..dsl import strategy
from ..signal import Alert, Hold, Sell, Signal, Urgency


# Tokens to watch. Start small with high-volume markets where pumps actually
# happen. Polymarket's spike-prone markets tend to be politics, crypto, and
# event-driven (election outcomes, court rulings, AI announcements, etc.).
#
# Hardcoded for now — Phase 6 ships an MVP that exercises the pipeline.
# Future revisions could add Signal::Subscribe on startup based on a
# scanner query against gamma.
WATCH_TOKENS: list[str] = [
    # Hantavirus pandemic NO — we have data for this already
    "95212449865986159112377413335252801281670333750637442556685159781445406848396",
]

# Spike detection knobs.
SHORT_WINDOW = 60     # seconds — the window we evaluate for "recent" volume
LONG_WINDOW = 900     # seconds — baseline window (15 min)
SPIKE_MULTIPLIER = Decimal("5.0")   # short-window rate / baseline rate threshold
MIN_SHORT_VOLUME = Decimal("100")   # ignore tiny markets — at least N shares
                                    # in the short window before we bother

ALERT_TTL_SECS = 600   # 10-minute window for the human to act
SUGGESTED_SIZE = Decimal("100")     # what to fade with — modest


@strategy(
    name="momentum_fade",
    tokens=WATCH_TOKENS,
    tick_interval_ms=5000,   # 5s — fast enough to catch a 60s spike,
                              # slow enough to not hammer
    params={
        "SHORT_WINDOW": SHORT_WINDOW,
        "LONG_WINDOW": LONG_WINDOW,
        "SPIKE_MULTIPLIER": SPIKE_MULTIPLIER,
        "MIN_SHORT_VOLUME": MIN_SHORT_VOLUME,
        "SUGGESTED_SIZE": SUGGESTED_SIZE,
        "ALERT_TTL_SECS": ALERT_TTL_SECS,
    },
)
def on_tick(ctx) -> list[Signal]:
    signals: list[Signal] = []

    for token_id in WATCH_TOKENS:
        book = ctx.book(token_id)
        if book is None:
            continue
        best_bid = book.best_bid
        if best_bid is None:
            continue
        best_ask = book.best_ask
        if best_ask is None:
            continue

        short_vol = ctx.volume_in_window(token_id, SHORT_WINDOW)
        if short_vol < MIN_SHORT_VOLUME:
            continue

        long_vol = ctx.volume_in_window(token_id, LONG_WINDOW)
        # Baseline = long-window volume excluding the short window, normalized
        # to a per-second rate so it's comparable to the short-window rate.
        baseline_vol = long_vol - short_vol
        if baseline_vol <= Decimal("0"):
            # No baseline = first observation; can't say it's a spike.
            continue
        baseline_secs = LONG_WINDOW - SHORT_WINDOW
        baseline_rate = baseline_vol / Decimal(baseline_secs)
        short_rate = short_vol / Decimal(SHORT_WINDOW)
        if short_rate < baseline_rate * SPIKE_MULTIPLIER:
            continue

        # Spike detected. Propose a fade.
        mid = (best_bid + best_ask) / Decimal("2")
        # Round suggested sell price down to a tick we know is safe; the
        # engine's per-market tick rounding will adjust if needed.
        suggested_price = mid - Decimal("0.005")
        if suggested_price <= Decimal("0.01"):
            suggested_price = Decimal("0.01")

        # Dedupe key bucketed to the minute so we re-alert if the spike
        # persists into a new minute, but not every tick.
        bucket = int(ctx.timestamp.timestamp()) // 60
        signals.append(
            Alert(
                reason=f"vol spike: short {short_vol} / baseline {baseline_vol:.0f} ({float(short_rate/baseline_rate):.1f}x)",
                suggested=Sell(
                    token_id=token_id,
                    size=SUGGESTED_SIZE,
                    price=suggested_price,
                    urgency=Urgency.MEDIUM,
                ),
                ttl_secs=ALERT_TTL_SECS,
                dedupe_key=f"momentum_fade-{token_id[:16]}-{bucket}",
            )
        )

    if not signals:
        return [Hold()]
    return signals
