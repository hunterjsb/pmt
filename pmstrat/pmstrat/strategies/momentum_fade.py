"""Momentum-Fade Spike Detector.

Watches the live market set (selected by `market_filter` below) for
volume spikes. When the last `SHORT_WINDOW` seconds of volume on a token
exceeds `SPIKE_MULTIPLIER` × the recent baseline volume (computed from
the prior `LONG_WINDOW - SHORT_WINDOW` seconds), raise a `Signal::Alert`
proposing a fade trade: a Sell at the current mid.

The human reviews the alert (typically by checking whether @Polymarket /
Trump / etc. just tweeted about the market) and approves or rejects via
`pmt engine approve|reject`. This strategy doesn't *itself* identify
whether the move is informed or sheep-driven — that's the human-touch
step. It just flags candidates where the volume profile looks like a
coordinated pump worth a closer look.

Watchlist is dynamic — the engine's scanner queries gamma every
PMENGINE_SCAN_INTERVAL_S (default 60s) for markets matching `market_filter`
and Subscribe/Unsubscribe to keep this strategy's set current.

Designed for the engine's Phase 5 alert pipeline: the strategy never
places a real order — every action is gated on human approval.
"""

from decimal import Decimal

from ..dsl import MarketFilter, strategy
from ..signal import Alert, Hold, Sell, Signal, Urgency


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
    tokens=[],   # empty — the scanner provides them based on market_filter
    tick_interval_ms=15000,   # 15s — scan many markets, don't hammer
    market_filter=MarketFilter(
        # Liquidity > $20k weeds out the long tail. Mid in [0.05, 0.95]
        # avoids the resolution-imminent markets where moves are signal,
        # not pump. Recurring series (BTC-updown-5m, SPX-daily, etc.) are
        # mechanical flow — exclude them.
        min_liquidity=20_000.0,
        min_mid="0.05",
        max_mid="0.95",
        exclude_recurring=True,
        # Cap subscriptions: 5 active markets keeps the REST poller well
        # under Polymarket's account-level rate limit. The original 20
        # was triggering 429s on order placement (account budget is
        # shared across reads + writes). 5 markets * 10s book poll
        # cadence ≈ 0.5 req/sec from this strategy.
        max_subscriptions=5,
    ),
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

    for token_id in ctx.subscribed_tokens():
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
