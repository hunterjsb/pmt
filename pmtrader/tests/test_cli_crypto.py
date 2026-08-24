"""Tests for the `crypto` group's registration surface.

cli_crypto.py is a map now, not a place code lives, so what's testable about
it is the map: every command module's commands are actually attached, and the
commands we deleted stay deleted. The behavior of each command is tested in
the test module mirroring its own module (test_cli_crypto_arm/_stats/_watch/
_data), and the render halves in test_watch_ui / test_stats_render.
"""

from __future__ import annotations

import json

import pytest

import cli_crypto as cc
import cli_crypto_stats as cs


def _install_fake_pipeline(monkeypatch, rows, fires_by_slug, gamma_by_slug):
    monkeypatch.setattr(cs.wallet, "funder_address", lambda: "0xabc")
    monkeypatch.setattr(cs.wallet, "fetch_wallet_activity", lambda addr, floor: rows)

    def fake_iter_records(path, evs=None, floor=None):
        out = []
        for recs in fires_by_slug.values():
            out.extend(recs)
        return iter(out)

    monkeypatch.setattr(cs.tape, "iter_records", fake_iter_records)
    monkeypatch.setattr(cs, "_gamma_resolution_cached",
                         lambda slug: gamma_by_slug.get(slug))



def _era_rows(*specs):
    """Wallet activity for windows at given (start, won) — one BUY, one REDEEM.

    The redeem is deliberately posted 10 minutes AFTER the window ends, so a
    window sitting near a boundary has its two rows land on opposite sides of
    it. That is the trap era math has to survive: a window belongs to the era
    that PRICED it, not to whenever its money came back.
    """
    rows = []
    for start, won in specs:
        slug = f"btc-updown-5m-{start}"
        rows.append({"type": "TRADE", "side": "BUY", "usdcSize": 10.0, "size": 11.0,
                     "slug": slug, "timestamp": start + 60})
        rows.append({"type": "REDEEM", "usdcSize": 11.0 if won else 0.0,
                     "outcome": "up" if won else None,
                     "slug": slug, "timestamp": start + 300 + 600})
    return rows


_ERA_REG = (cs.eras.Era("old", 0.0, "before"),
            cs.eras.Era("mid", 2_000_000.0, "middle"),
            cs.eras.Era("new", 3_000_000.0, "current"))




def test_every_command_module_is_actually_attached():
    # The failure mode the thin surface introduces: a module gets written and
    # nobody adds it to the registration list, so the command silently isn't
    # there. Name them explicitly — a set comparison, not a count.
    assert set(cc.crypto_group.commands) == {
        "activity", "arm", "basis", "disarm", "errors", "fixture", "fleet",
        "journal", "outcomes", "regime", "stats", "tape", "trigger", "updown",
        "watch", "window",
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
    assert callable(cs._gates_report)


def test_stats_exposes_full_and_gates_without_changing_the_default():
    opts = {p.name for p in cs.crypto_stats.params}
    assert {"since", "full", "gates", "as_json"} <= opts
    defaults = {p.name: p.default for p in cs.crypto_stats.params}
    assert defaults["full"] is False and defaults["gates"] is False


# ---------- policy eras: the by-era table's grading and its sharing discipline
#             (inline fixture: wallet/tape monkeypatched, no network) ----------

def _era_rows(*specs):
    """Wallet activity for windows at given (start, won) — one BUY, one REDEEM.

    The redeem is deliberately posted 10 minutes AFTER the window ends, so a
    window sitting near a boundary has its two rows land on opposite sides of
    it. That is the trap era math has to survive: a window belongs to the era
    that PRICED it, not to whenever its money came back.
    """
    rows = []
    for start, won in specs:
        slug = f"btc-updown-5m-{start}"
        rows.append({"type": "TRADE", "side": "BUY", "usdcSize": 10.0, "size": 11.0,
                     "slug": slug, "timestamp": start + 60})
        rows.append({"type": "REDEEM", "usdcSize": 11.0 if won else 0.0,
                     "outcome": "up" if won else None,
                     "slug": slug, "timestamp": start + 300 + 600})
    return rows


_ERA_REG = (cs.eras.Era("old", 0.0, "before"),
            cs.eras.Era("mid", 2_000_000.0, "middle"),
            cs.eras.Era("new", 3_000_000.0, "current"))


def test_score_activity_ceiling_selects_windows_by_start_not_row_time(monkeypatch):
    # Window A starts inside the range and redeems outside it; window B starts
    # outside and buys inside. Only A may count — grading on row timestamps is
    # exactly the phantom-profit bug the floor was fixed for.
    _install_fake_pipeline(monkeypatch, _era_rows((1_999_000, True), (2_000_100, False)),
                            {}, {})
    rows = cs.wallet.fetch_wallet_activity("0xabc", 0.0)

    sb = cs.score_activity(rows, 0.0, ceiling=2_000_000.0)
    assert (sb["wins"], sb["losses"]) == (1, 0)
    assert [w["slug"] for w in sb["eff_windows"]] == ["btc-updown-5m-1999000"]


def test_score_activity_ceiling_is_half_open_at_the_boundary(monkeypatch):
    _install_fake_pipeline(monkeypatch, _era_rows((2_000_000, True)), {}, {})
    rows = cs.wallet.fetch_wallet_activity("0xabc", 0.0)
    # A window starting exactly on a boundary belongs to the NEW era only.
    assert cs.score_activity(rows, 0.0, ceiling=2_000_000.0)["wins"] == 0
    assert cs.score_activity(rows, 2_000_000.0)["wins"] == 1


def test_era_scoreboards_file_each_window_by_its_start(monkeypatch):
    _install_fake_pipeline(monkeypatch, _era_rows(
        (1_000_000, False),   # old
        (1_999_999, True),    # old — starts one second before the boundary
        (2_000_000, True),    # mid — starts ON the boundary
        (2_500_000, False),   # mid
        (3_100_000, True),    # new
    ), {}, {})
    rows = cs.wallet.fetch_wallet_activity("0xabc", 0.0)

    out = cs.era_scoreboards(rows, tape_records=[], registry=_ERA_REG)
    got = {r["name"]: (r["sb"]["wins"], r["sb"]["losses"]) for r in out}
    assert got == {"old": (1, 1), "mid": (1, 1), "new": (1, 0)}


def test_era_scoreboards_partition_the_all_time_scoreboard_exactly(monkeypatch):
    specs = [(1_000_000, False), (2_100_000, True), (2_900_000, True), (3_500_000, False)]
    _install_fake_pipeline(monkeypatch, _era_rows(*specs), {}, {})
    rows = cs.wallet.fetch_wallet_activity("0xabc", 0.0)

    all_time = cs.score_activity(rows, 0.0)
    out = cs.era_scoreboards(rows, tape_records=[], registry=_ERA_REG)
    # No window counted twice, none dropped: that is what makes the era table a
    # re-cut of the ledger rather than a second, disagreeing ledger.
    assert sum(r["sb"]["wins"] for r in out) == all_time["wins"]
    assert sum(r["sb"]["losses"] for r in out) == all_time["losses"]
    assert sum(r["sb"]["net"] for r in out) == pytest.approx(all_time["net"])


def test_era_scoreboards_lists_every_era_including_the_empty_ones(monkeypatch):
    _install_fake_pipeline(monkeypatch, _era_rows((3_100_000, True)), {}, {})
    rows = cs.wallet.fetch_wallet_activity("0xabc", 0.0)

    out = cs.era_scoreboards(rows, tape_records=[], registry=_ERA_REG)
    # An era that traded nothing still gets a row — an era omitted for being
    # empty is how a dead regime gets quietly forgotten.
    assert [r["name"] for r in out] == ["old", "mid", "new"]
    assert [(r["sb"]["wins"], r["sb"]["losses"]) for r in out] == [(0, 0), (0, 0), (1, 0)]


def test_era_scoreboards_carry_each_eras_own_breakeven_bar(monkeypatch):
    # `old` has a win and a loss, so its payoff shape can be sized; `new` has
    # only a win, so its bar is None and must never render as a zero.
    _install_fake_pipeline(monkeypatch, _era_rows(
        (1_000_000, True), (1_500_000, False), (3_100_000, True)), {}, {})
    rows = cs.wallet.fetch_wallet_activity("0xabc", 0.0)

    out = {r["name"]: r for r in cs.era_scoreboards(rows, tape_records=[],
                                                     registry=_ERA_REG)}
    assert out["old"]["breakeven"] == pytest.approx(
        cs.effectiveness.breakeven_win_rate(out["old"]["sb"]["eff_windows"]))
    assert out["new"]["breakeven"] is None


def test_era_scoreboards_span_hours_are_none_for_the_open_left_era(monkeypatch):
    _install_fake_pipeline(monkeypatch, [], {}, {})
    out = {r["name"]: r for r in cs.era_scoreboards([], tape_records=[],
                                                     registry=_ERA_REG)}
    assert out["old"]["span_h"] is None            # opens at epoch 0 — no duration
    assert out["mid"]["span_h"] == pytest.approx(1_000_000 / 3600.0)
    assert out["new"]["span_h"] > 0                # still running, measured to now


# -- the sharing discipline: N eras, one walk, one tape read --

def test_era_scoreboards_never_walk_the_wallet(monkeypatch):
    def boom(*a, **kw):
        raise AssertionError("the era table grades the caller's rows, never its own walk")

    monkeypatch.setattr(cs.wallet, "fetch_wallet_activity", boom)
    monkeypatch.setattr(cs.wallet, "funder_address", boom)
    monkeypatch.setattr(cs.tape, "iter_records", lambda *a, **kw: iter(()))
    assert len(cs.era_scoreboards([], tape_records=[], registry=_ERA_REG)) == 3


def test_era_scoreboards_read_the_tape_once_not_once_per_era(monkeypatch):
    reads = []

    def counting_iter(path, evs=None, floor=None):
        reads.append(path)
        return iter(())

    monkeypatch.setattr(cs.tape, "iter_records", counting_iter)
    monkeypatch.setattr(cs, "_gamma_resolution_cached", lambda slug: None)
    cs.era_scoreboards([], registry=_ERA_REG)
    # Three eras, one 15MB tape: paying for it per era would charge the report
    # three times over for one answer.
    assert len(reads) == 1


def test_score_activity_prefers_handed_in_tape_records(monkeypatch):
    def boom(*a, **kw):
        raise AssertionError("a shared tape slice must not be re-read")

    monkeypatch.setattr(cs.tape, "iter_records", boom)
    monkeypatch.setattr(cs, "_gamma_resolution_cached", lambda slug: None)
    recs = [{"ev": "roll", "t": 5.0}, {"ev": "roll", "t": 50.0}]
    assert cs.score_activity([], 0.0, tape_records=recs)["rolls"] == 2
    assert cs.score_activity([], 0.0, ceiling=10.0, tape_records=recs)["rolls"] == 1


# -- the --era flag on the command --

def _stats_cli(monkeypatch, args, rows):
    from click.testing import CliRunner

    def down(*a, **kw):
        raise RuntimeError("not reachable in a test")

    _install_fake_pipeline(monkeypatch, rows, {}, {})
    monkeypatch.setattr(cs, "_engine_post", down)
    monkeypatch.setattr(cs, "_api", down)
    return CliRunner().invoke(cs.crypto_stats, args)


def _real_era_rows():
    """One window inside each SHIPPED era, so the CLI test exercises the
    registry that actually ships rather than a stand-in."""
    return _era_rows(*[(int(e.start) + 300, True) for e in cs.eras.ERAS])


def test_stats_era_flag_scopes_the_report_to_one_era(monkeypatch):
    result = _stats_cli(monkeypatch, ["--era", "theta", "--json"], _real_era_rows())
    assert result.exit_code == 0, result.output
    out = json.loads(result.output)
    assert out["era"] == "theta"
    assert (out["wins"], out["losses"]) == (1, 0)   # only theta's own window
    # ...and the era table stays complete: scoping the view hides no era.
    assert [e["name"] for e in out["eras"]] == cs.eras.names()
    assert sum(e["wins"] for e in out["eras"]) == len(cs.eras.ERAS)


def test_stats_default_view_carries_every_era(monkeypatch):
    result = _stats_cli(monkeypatch, ["--json"], _real_era_rows())
    assert result.exit_code == 0, result.output
    out = json.loads(result.output)
    assert out["era"] is None
    assert out["wins"] == len(cs.eras.ERAS)          # all-time stays the headline
    assert [e["name"] for e in out["eras"]] == cs.eras.names()
    assert out["eras"][-1]["end"] is None            # the running era is open-ended


def test_stats_rejects_an_unknown_era(monkeypatch):
    result = _stats_cli(monkeypatch, ["--era", "guard-6bp"], [])
    assert result.exit_code != 0
    assert "unknown era" in result.output and "pre-brake" in result.output


def test_stats_refuses_era_and_since_together(monkeypatch):
    result = _stats_cli(monkeypatch, ["--era", "theta", "--since", "6"], [])
    assert result.exit_code != 0
    assert "use one" in result.output


def test_stats_since_omits_the_era_table_rather_than_showing_a_short_one(monkeypatch):
    result = _stats_cli(monkeypatch, ["--since", "6", "--json"], _real_era_rows())
    assert result.exit_code == 0, result.output
    # --since floors the WALK, so older eras would read short. Half an era
    # table is worse than none.
    assert json.loads(result.output)["eras"] == []


def test_stats_exposes_era_without_changing_the_default():
    opts = {p.name for p in cs.crypto_stats.params}
    assert "era_name" in opts
    defaults = {p.name: p.default for p in cs.crypto_stats.params}
    assert defaults["era_name"] is None
