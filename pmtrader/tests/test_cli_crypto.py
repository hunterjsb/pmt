"""Tests for the `crypto` group's registration surface.

cli_crypto.py is a map now, not a place code lives, so what's testable about
it is the map: every command module's commands are actually attached, and the
commands we deleted stay deleted. The behavior of each command is tested in
the test module mirroring its own module (test_cli_crypto_arm/_stats/_watch/
_data), and the render halves in test_watch_ui / test_stats_render.
"""

from __future__ import annotations

import cli_crypto as cc


def test_every_command_module_is_actually_attached():
    # The failure mode the thin surface introduces: a module gets written and
    # nobody adds it to the registration list, so the command silently isn't
    # there. Name them explicitly — a set comparison, not a count.
    assert set(cc.crypto_group.commands) == {
        "activity", "arm", "basis", "disarm", "fixture", "fleet", "journal",
        "outcomes", "stats", "tape", "trigger", "updown", "watch", "window",
    }


# ---------- removed commands stay removed ----------

def test_spot_and_oracle_are_gone_from_the_crypto_group():
    # `spot` duplicated updown/trigger/watch's own price line; `oracle` was only
    # ever a manual prerequisite of `outcomes`, which now fetches for itself.
    names = set(cc.crypto_group.commands)
    assert "spot" not in names and "oracle" not in names
    assert not hasattr(cc, "crypto_spot") and not hasattr(cc, "crypto_oracle")


def test_spot_price_helper_survives_because_pricing_still_uses_it():
    # Only the CLI wrapper went away; eval_updown reads the same Binance tick.
    from polymarket.crypto import spot_price

    assert callable(spot_price)


def test_shadow_is_gone_and_lives_on_as_a_stats_flag():
    # Its gate cost/saved summary is `pmt crypto stats --gates` now: one
    # report, one wallet walk, one place the operator looks.
    assert "shadow" not in set(cc.crypto_group.commands)
    assert not hasattr(cc, "crypto_shadow")
