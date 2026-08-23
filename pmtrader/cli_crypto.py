"""`pmt crypto ...` — the command-registration surface, and nothing else.

Every command lives in a focused sibling module and is attached here, so this
file stays a map of the group rather than a place code accretes:

    cli_crypto_arm.py      pricing + the live fleet (updown/arm/disarm/fleet/trigger)
    cli_crypto_stats.py    wallet acquisition, grading, the stats report
    cli_crypto_watch.py    the live dashboard's fetch/render split
    cli_crypto_data.py     read-only reports (tape/activity/window/basis/outcomes/journal)
    cli_crypto_fixture.py  the pmengine characterization-fixture freezer

Registered onto the top-level `cli` group by cli.py
(`from cli_crypto import crypto_group; cli.add_command(crypto_group)`).

Where the shared pieces live, so a sixth module doesn't invent a second copy:
console/_api/_pnl_color/_parse_since in cli_common.py; wallet acquisition and
grading in cli_crypto_stats.py (read its docstring before adding a caller);
every render function in watch_ui.py and stats_render.py.
"""

from __future__ import annotations

import click

from cli_crypto_arm import (
    crypto_arm, crypto_disarm, crypto_fleet, crypto_trigger, crypto_updown,
)
from cli_crypto_data import (
    crypto_activity, crypto_basis, crypto_journal, crypto_outcomes, crypto_tape,
    crypto_window,
)
from cli_crypto_fixture import crypto_fixture
from cli_crypto_stats import crypto_stats
from cli_crypto_watch import crypto_watch


@click.group("crypto")
def crypto_group() -> None:
    """Crypto up/down market pricing against Binance/Chainlink data."""


for _cmd in (
    crypto_activity, crypto_arm, crypto_basis, crypto_disarm, crypto_fixture,
    crypto_fleet, crypto_journal, crypto_outcomes, crypto_stats, crypto_tape,
    crypto_trigger, crypto_updown, crypto_watch, crypto_window,
):
    crypto_group.add_command(_cmd)
