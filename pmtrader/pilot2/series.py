"""Which series this pilot may touch, and the refusal that keeps it there.

Two engines under one operator must never share a series: their orders sit on
the same book under the same beneficial owner, so one can match the other's
quote, which is wash-trade shaped no matter what either intended
(CLAUDE.md, "Series partition"). The desktop engine owns the majors; the EU
engine owns `bnb-updown-5m`. This pilot is a THIRD participant and gets a
disjoint slice or it gets nothing.

The refusal is loud and fatal by design. `PMENGINE_SERIES_ALLOWLIST` is an
allowlist with no deny form and unset means unpartitioned; the failure mode
that matters here is a config typo pointing tiny live capital at a series the
fleet is already quoting, and that must stop the process, not warn in a log
nobody reads.
"""

from __future__ import annotations

import os

from polymarket.updown_slugs import parse_updown_slug

# Shadow-only. These are the desktop engine's book and the calibration corpus's
# best-covered series; the pilot prices them and writes down what it WOULD have
# done, and never touches them with capital in any mode.
SHADOW_SERIES: tuple[str, ...] = (
    "btc-updown-5m", "eth-updown-5m", "sol-updown-5m", "xrp-updown-5m",
)

# Live default. Non-engine series: doge and hype have ZERO book coverage in the
# fleet's tape (nobody ever subscribed) and bnb-15m is a duration the EU box
# does not run. calibrated_model.md §5 measures the model at Brier 0.1413 /
# 0.1442 on doge/hype against the incumbent's 0.1780 / 0.2016 — the model is
# not competing with a market there, it is the only estimator in the room.
DEFAULT_LIVE_SERIES: tuple[str, ...] = (
    "doge-updown-5m", "hype-updown-5m", "bnb-updown-15m",
)

# Owned by a running engine. A series matches if it IS one of these or sits
# under it — "btc-updown" claims every btc duration (the desktop rolls them
# all), while "bnb-updown-5m" claims exactly that one duration and leaves
# bnb-updown-15m free.
ENGINE_OWNED: tuple[str, ...] = (
    "btc-updown", "eth-updown", "sol-updown", "xrp-updown", "bnb-updown-5m",
)

SERIES_ENV = "PILOT2_SERIES"


class SeriesRefused(RuntimeError):
    """A configured live series belongs to an engine. Fatal, never downgraded."""


def owner_of(series: str) -> str | None:
    """The engine-owned prefix that claims `series`, or None if it is free."""
    s = series.strip().lower()
    for owned in ENGINE_OWNED:
        if s == owned or s.startswith(owned + "-"):
            return owned
    return None


def is_engine_owned(series: str) -> bool:
    return owner_of(series) is not None


def parse_series(raw: str | None) -> list[str]:
    """Comma-separated series list -> normalised, de-duplicated, order kept."""
    out: list[str] = []
    for part in (raw or "").split(","):
        s = part.strip().lower()
        if s and s not in out:
            out.append(s)
    return out


def live_series(raw: str | None = None) -> list[str]:
    """The series live mode may trade. Raises SeriesRefused on any engine-owned
    entry — including the case where the operator emptied the list, because an
    empty live allowlist means "nothing to do" and should say so, not run.
    """
    configured = parse_series(raw if raw is not None else os.environ.get(SERIES_ENV))
    series = configured or list(DEFAULT_LIVE_SERIES)
    stolen = [(s, owner_of(s)) for s in series if is_engine_owned(s)]
    if stolen:
        detail = ", ".join(f"{s} (owned by {o})" for s, o in stolen)
        raise SeriesRefused(
            f"PILOT2_SERIES names {len(stolen)} series an engine already trades: {detail}. "
            "Two participants on one book under one wallet is wash-trade shaped. "
            "Remove them, or drop the series from the engine's allowlist and restart "
            "that engine first."
        )
    bad = [s for s in series if not series_grid(s)]
    if bad:
        raise SeriesRefused(f"not parseable as an updown series: {', '.join(bad)}")
    return series


def shadow_series() -> list[str]:
    """The majors, always. Shadow mode places nothing, so no partition applies."""
    return list(SHADOW_SERIES)


def series_grid(series: str) -> int | None:
    """Window duration in seconds for a series key, or None if unparseable.

    Windows sit on a fixed grid (`start % dur_s == 0`), which is why the slug
    of the window trading right now is COMPUTABLE and gamma only ever has to
    confirm it and hand back token ids.
    """
    w = parse_updown_slug(f"{series}-0")
    return w["dur_s"] if w else None


def current_slug(series: str, now: float) -> str | None:
    """The slug of the window `series` is trading at `now`."""
    dur = series_grid(series)
    if not dur:
        return None
    return f"{series}-{int(now // dur) * dur}"


def window_bounds(slug: str) -> tuple[float, float] | None:
    """(start, end) epoch seconds from the slug itself — no network."""
    w = parse_updown_slug(slug)
    return (float(w["start"]), float(w["end"])) if w else None


def symbol_of(series: str) -> str | None:
    """RTDS symbol ("doge/usd") for a series key. The stream names every
    symbol against USD; the slug names it bare."""
    w = parse_updown_slug(f"{series}-0")
    return f"{w['symbol']}/usd" if w else None
