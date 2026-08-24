"""The doctor: the assignment map's refusals, what a heartbeat says, and the
checker's exit code — which encodes page-worthiness, not up-ness.
"""

from __future__ import annotations

import json
import time

import pytest
from orchestrator import check as check_mod
from orchestrator import heartbeat as hb_mod
from orchestrator.assign import MapInvalid, parse, series_window_dur_s, validate
from orchestrator.lease import Lease, grace_floor
from orchestrator.notify import Notifier, read_latch, write_latch
from orchestrator.store import FakeStore

NOW = 1_787_543_100.0


def _map(series: dict, nodes=None, leases_active: bool = True) -> dict:
    return {
        "version": 1,
        "leases_active": leases_active,
        "nodes": nodes or {"desktop": {}, "euw": {}},
        "series": series,
    }


def _ok_series(**kw) -> dict:
    base = {"home": "desktop", "failover": "euw", "grace_s": 420,
            "arm_template": {"size": 25, "clip": 10}}
    base.update(kw)
    return base


# --- the map ---------------------------------------------------------------

def test_the_shipped_map_is_valid():
    from orchestrator.assign import load

    fm = load()
    assert validate(fm) == []
    assert fm.nodes.keys() >= {"desktop", "euw"}
    # The thing Hunter actually asked for, asserted as a fact about the map.
    assert fm.series["bnb-updown-5m"].home == "euw"
    assert fm.series["bnb-updown-5m"].failover == "desktop"
    assert fm.series["btc-updown-5m"].home == "desktop"
    assert fm.series["btc-updown-5m"].failover == "euw"


def test_a_grace_below_the_floor_is_refused_not_rounded_up():
    with pytest.raises(MapInvalid) as e:
        parse(_map({"btc-updown-5m": _ok_series(grace_s=60)}))
    assert "below the" in str(e.value) and "floor" in str(e.value)


def test_a_5m_grace_on_a_15m_series_is_caught():
    # The copy-paste that looks generous and is short by a whole window.
    with pytest.raises(MapInvalid):
        parse(_map({"btc-updown-15m": _ok_series(grace_s=420)}))
    fm = parse(_map({"btc-updown-15m": _ok_series(grace_s=1200)}))
    assert fm.series["btc-updown-15m"].effective_grace_s() >= grace_floor(900)


def test_a_series_key_that_prefixes_another_is_refused():
    """PMENGINE_SERIES_ALLOWLIST matches by prefix, so an engine allowed
    `btc-updown` is allowed `btc-updown-15m-...` too. A map that gives those
    two different homes is a partition that does not partition.

    The coarse form is not hypothetical — pilot2's ENGINE_OWNED writes exactly
    this ("btc-updown" claims every btc duration), so it is the shape someone
    would reach for when copying that list across.
    """
    with pytest.raises(MapInvalid) as e:
        parse(_map({
            "btc-updown": _ok_series(grace_s=1200, window_dur_s=900),
            "btc-updown-15m": _ok_series(home="euw", failover="desktop", grace_s=1200),
        }))
    msg = str(e.value)
    assert "prefix" in msg and "desktop" in msg and "euw" in msg


def test_a_failover_that_is_the_home_node_is_a_typo_not_a_failover():
    with pytest.raises(MapInvalid) as e:
        parse(_map({"btc-updown-5m": _ok_series(failover="desktop")}))
    assert "same node as home" in str(e.value)


def test_an_undeclared_node_is_refused():
    with pytest.raises(MapInvalid) as e:
        parse(_map({"btc-updown-5m": _ok_series(failover="mars")}))
    assert "not a declared node" in str(e.value)


def test_a_failover_without_an_arm_template_is_refused():
    with pytest.raises(MapInvalid) as e:
        parse(_map({"btc-updown-5m": {"home": "desktop", "failover": "euw", "grace_s": 420}}))
    assert "arm_template" in str(e.value)


def test_a_declared_duration_contradicting_the_series_key_is_caught():
    with pytest.raises(MapInvalid) as e:
        parse(_map({"btc-updown-15m": _ok_series(grace_s=1200, window_dur_s=300)}))
    assert "contradicts" in str(e.value)


def test_an_unknown_map_version_is_refused_rather_than_guessed_at():
    with pytest.raises(MapInvalid):
        parse({"version": 99, "nodes": {"desktop": {}}, "series": {}})


def test_the_window_duration_comes_free_from_the_series_key():
    assert series_window_dur_s("btc-updown-5m") == 300
    assert series_window_dur_s("btc-updown-15m") == 900
    assert series_window_dur_s("not-a-series") is None


# --- heartbeats ------------------------------------------------------------

def test_series_held_reads_arms_and_rolls_and_dedupes(tmp_path):
    p = tmp_path / "arms-state.json"
    p.write_text(json.dumps({
        "version": 1,
        "arms": [
            {"slug": "btc-updown-15m-1787543100"},
            {"slug": "btc-updown-5m-1787543100"},
            {"slug": "btc-updown-5m-1787543400"},   # same series, next window
        ],
        "rolls": [{"params": {"slug": "eth-updown-5m-1787543100"}}],
    }))
    assert hb_mod.series_held([p]) == ["btc-updown-15m", "btc-updown-5m", "eth-updown-5m"]


def test_a_missing_arms_file_means_no_arms_not_a_failure(tmp_path):
    # The engine DELETES arms-state.json when the last arm goes away — absent
    # is the documented "inert" state, not an observation failure.
    assert hb_mod.series_held([tmp_path / "nope.json"]) == []


def test_both_arms_files_are_read_because_updown2_keeps_its_own(tmp_path):
    a = tmp_path / "arms-state.json"
    b = tmp_path / "updown2-arms-state.json"
    a.write_text(json.dumps({"arms": [{"slug": "btc-updown-5m-1"}]}))
    b.write_text(json.dumps({"arms": [{"slug": "sol-updown-5m-1"}]}))
    assert hb_mod.series_held([a, b]) == ["btc-updown-5m", "sol-updown-5m"]


def test_a_torn_arms_file_does_not_take_the_daemon_down(tmp_path):
    p = tmp_path / "arms-state.json"
    p.write_text('{"arms": [{"slug": "btc-upd')
    assert hb_mod.series_held([p]) == []


def test_heartbeat_from_a_live_engine_status(tmp_path):
    status = {
        "halted": False, "balance_usdc": "2157.52", "ws_connected": True,
        "ws_last_event_age_ms": 8, "tick_count": 34562,
    }
    hb = hb_mod.build("desktop", arms_paths=[tmp_path / "x.json"], status=status, now=NOW)
    assert hb["node"] == "desktop"
    assert hb["engine_active"] is True
    assert hb["balance_ok"] is True
    assert hb["feed_age"] == pytest.approx(0.008)
    assert hb["tick_count"] == 34562
    assert "shutdown" not in hb


def test_an_unreachable_engine_beats_as_down_rather_than_not_beating(tmp_path):
    hb = hb_mod.build("euw", arms_paths=[tmp_path / "x.json"], status=None, now=NOW)
    assert hb["engine_active"] is False
    assert hb["balance_ok"] is False
    assert hb["feed_age"] == -1.0     # the wire form of "unknown"


def test_a_halted_engine_is_not_active_even_though_it_answers(tmp_path):
    hb = hb_mod.build("desktop", arms_paths=[], status={"halted": True, "balance_usdc": "100"},
                      now=NOW)
    assert hb["engine_active"] is False and hb["halted"] is True


def test_a_disconnected_socket_reports_an_unknown_feed_age_not_a_stale_small_one(tmp_path):
    hb = hb_mod.build("desktop", arms_paths=[], now=NOW, status={
        "halted": False, "balance_usdc": "100", "ws_connected": False,
        "ws_last_event_age_ms": 8,
    })
    assert hb["feed_age"] == -1.0


def test_a_low_balance_shows_as_not_ok(tmp_path):
    hb = hb_mod.build("euw", arms_paths=[], status={"halted": False, "balance_usdc": "3.5"},
                      now=NOW, min_balance=25.0)
    assert hb["balance_ok"] is False and hb["balance_usdc"] == 3.5


def test_the_shutdown_stamp_and_its_self_clearing():
    off = hb_mod.build("desktop", arms_paths=[], status=None, now=NOW, shutdown=True)
    assert hb_mod.shutdown_clean(off)
    back = hb_mod.build("desktop", arms_paths=[], status=None, now=NOW + 100)
    assert not hb_mod.shutdown_clean(back)   # the whole-row rewrite drops the stamp


def test_a_missing_heartbeat_is_not_a_clean_shutdown():
    # Page rather than silently skip a real wedge.
    assert hb_mod.shutdown_clean(None) is False


# --- the checker -----------------------------------------------------------

def _fleet(_leases_active: bool = True, _nodes=None, **series) -> object:
    spec = {k: _ok_series(**v) for k, v in series.items()}
    return parse(_map(spec, nodes=_nodes, leases_active=_leases_active))


def _beat(store, node, ts, **kw):
    hb = {"node": node, "ts": ts, "engine_active": True, "series_held": [],
          "balance_ok": True, "feed_age": 0.1}
    hb.update(kw)
    store.put_heartbeat(hb)


def _lease(store, series, holder, expires_at, **kw):
    base = dict(series=series, holder=holder, epoch=1, expires_at=expires_at,
                released=False, home_extension_s=600.0, grace_s=420.0,
                holder_is_home=(holder == "desktop"))
    base.update(kw)
    store.put_lease(Lease(**base), expected_epoch=None)


def test_a_healthy_fleet_exits_zero():
    fm = _fleet(**{"btc-updown-5m": {}})
    s = FakeStore()
    _beat(s, "desktop", NOW, series_held=["btc-updown-5m"])
    _beat(s, "euw", NOW)
    _lease(s, "btc-updown-5m", "desktop", NOW + 60)
    rc, report, text = check_mod.run(s, fm, now=NOW)
    assert rc == check_mod.OK, report["attention"]
    assert "all good" in text


def test_a_cleanly_shut_down_node_is_a_note_and_never_a_page():
    """The nightly poweroff. This is the case that decides whether the checker's
    red means anything by Friday."""
    fm = _fleet(**{"bnb-updown-5m": {"home": "euw", "failover": "desktop"}})
    s = FakeStore()
    _beat(s, "desktop", NOW - 6 * 3600, shutdown=NOW - 6 * 3600, engine_active=False)
    _beat(s, "euw", NOW)
    _lease(s, "bnb-updown-5m", "euw", NOW + 60, holder_is_home=True)
    rc, report, _ = check_mod.run(s, fm, now=NOW)
    assert rc == check_mod.OK
    assert any("shut down cleanly" in n for n in report["notes"])
    assert report["attention"] == []


def test_strict_folds_a_cleanly_off_node_back_into_needing_attention():
    fm = _fleet(**{"bnb-updown-5m": {"home": "euw", "failover": "desktop"}})
    s = FakeStore()
    _beat(s, "desktop", NOW - 6 * 3600, shutdown=NOW - 6 * 3600, engine_active=False)
    _beat(s, "euw", NOW)
    _lease(s, "bnb-updown-5m", "euw", NOW + 60, holder_is_home=True)
    rc, _report, _ = check_mod.run(s, fm, now=NOW, strict=True)
    assert rc == check_mod.ATTENTION


def test_a_node_dark_without_a_marker_pages():
    fm = _fleet(**{"btc-updown-5m": {}})
    s = FakeStore()
    _beat(s, "desktop", NOW - 3600)          # no shutdown stamp — a wedge
    _beat(s, "euw", NOW)
    _lease(s, "btc-updown-5m", "desktop", NOW + 60)
    rc, report, _ = check_mod.run(s, fm, now=NOW)
    assert rc == check_mod.ATTENTION
    assert any("no clean-shutdown marker" in a for a in report["attention"])


def test_a_node_that_never_beat_says_so_specifically():
    fm = _fleet(**{"btc-updown-5m": {}})
    s = FakeStore()
    _beat(s, "desktop", NOW)
    _lease(s, "btc-updown-5m", "desktop", NOW + 60)
    rc, report, _ = check_mod.run(s, fm, now=NOW)
    assert rc == check_mod.ATTENTION
    assert any("no heartbeat ever" in a for a in report["attention"])


def test_a_holder_riding_its_extension_is_flagged_before_it_lapses():
    fm = _fleet(**{"btc-updown-5m": {}})
    s = FakeStore()
    _beat(s, "desktop", NOW)
    _beat(s, "euw", NOW)
    _lease(s, "btc-updown-5m", "desktop", NOW - 120)   # expired, inside the 600s ext
    rc, report, _ = check_mod.run(s, fm, now=NOW)
    assert rc == check_mod.ATTENTION
    assert [x for x in report["series"] if x["series"] == "btc-updown-5m"][0]["state"] == "extending"


def test_a_lapsed_unreleased_lease_reads_as_nobody_trading_it():
    fm = _fleet(**{"btc-updown-5m": {}})
    s = FakeStore()
    _beat(s, "desktop", NOW)
    _beat(s, "euw", NOW)
    _lease(s, "btc-updown-5m", "desktop", NOW - 5000)
    rc, report, _ = check_mod.run(s, fm, now=NOW)
    assert rc == check_mod.ATTENTION
    assert any("lapsed un-released" in a for a in report["attention"])


def test_a_released_lease_is_a_note_not_an_alarm():
    fm = _fleet(**{"btc-updown-5m": {}})
    s = FakeStore()
    _beat(s, "desktop", NOW - 7200, shutdown=NOW - 7200, engine_active=False)
    _beat(s, "euw", NOW)
    _lease(s, "btc-updown-5m", "desktop", NOW - 7200, released=True)
    rc, report, _ = check_mod.run(s, fm, now=NOW)
    assert rc == check_mod.OK
    assert any("released it cleanly" in n for n in report["notes"])


def test_an_armed_series_whose_lease_this_node_does_not_hold_is_the_loudest_finding():
    """The exact thing the system exists to make impossible. If it is ever
    observed, it must not be a quiet row in a table."""
    fm = _fleet(**{"btc-updown-5m": {}})
    s = FakeStore()
    _beat(s, "desktop", NOW, series_held=["btc-updown-5m"])
    _beat(s, "euw", NOW, series_held=["btc-updown-5m"])     # both armed
    _lease(s, "btc-updown-5m", "desktop", NOW + 60)
    rc, report, _ = check_mod.run(s, fm, now=NOW)
    assert rc == check_mod.ATTENTION
    assert any("ARMED but does not hold its lease" in a for a in report["attention"])


def test_a_holder_the_map_does_not_recognise_is_flagged():
    fm = _fleet(**{"btc-updown-5m": {}})
    s = FakeStore()
    _beat(s, "desktop", NOW)
    _beat(s, "euw", NOW)
    _lease(s, "btc-updown-5m", "stranger", NOW + 60)
    rc, report, _ = check_mod.run(s, fm, now=NOW)
    assert rc == check_mod.ATTENTION
    assert any("neither home" in a for a in report["attention"])


def test_a_failed_over_series_is_reported_as_such():
    fm = _fleet(**{"btc-updown-5m": {}})
    s = FakeStore()
    _beat(s, "desktop", NOW - 7200, shutdown=NOW - 7200, engine_active=False)
    _beat(s, "euw", NOW, series_held=["btc-updown-5m"])
    _lease(s, "btc-updown-5m", "euw", NOW + 60, holder_is_home=False)
    rc, report, _ = check_mod.run(s, fm, now=NOW)
    assert rc == check_mod.OK
    assert any("FAILED OVER to euw" in n for n in report["notes"])


def test_an_unreachable_store_is_blind_and_not_the_same_as_a_sick_fleet():
    fm = _fleet(**{"btc-updown-5m": {}})
    s = FakeStore()
    s.unavailable = True
    rc, report, text = check_mod.run(s, fm, now=NOW)
    assert rc == check_mod.BLIND
    assert rc != check_mod.ATTENTION
    assert report is None and "STORE UNREACHABLE" in text


def test_the_kill_switch_shows_up_in_the_report():
    fm = _fleet(**{"btc-updown-5m": {}})
    s = FakeStore()
    s.set_failover_disabled(True)
    _beat(s, "desktop", NOW)
    _beat(s, "euw", NOW)
    _lease(s, "btc-updown-5m", "desktop", NOW + 60)
    _rc, report, _ = check_mod.run(s, fm, now=NOW)
    assert report["failover_disabled"] is True
    assert any("kill switch" in n for n in report["notes"])


def test_render_survives_every_state_it_can_produce():
    fm = _fleet(**{"btc-updown-5m": {}, "eth-updown-5m": {}})
    s = FakeStore()
    _beat(s, "desktop", NOW - 9999)
    _lease(s, "btc-updown-5m", "desktop", NOW - 9999)
    _rc, report, text = check_mod.run(s, fm, now=NOW)
    assert "pmt fleet" in text and "NEEDS ATTENTION" in text
    assert "eth-updown-5m" in text and "unheld" in text


# --- notification ----------------------------------------------------------

def test_phase_one_is_quiet_even_though_every_series_is_armed_and_lease_less():
    """The shipped state. Eight armed series, no lease items, and this must be
    a calm report — otherwise the checker's red means nothing by rollout step 6."""
    fm = _fleet(_leases_active=False, **{"btc-updown-5m": {}, "eth-updown-5m": {}})
    s = FakeStore()
    _beat(s, "desktop", NOW, series_held=["btc-updown-5m", "eth-updown-5m"])
    _beat(s, "euw", NOW)
    rc, report, _ = check_mod.run(s, fm, now=NOW)
    assert rc == check_mod.OK, report["attention"]
    assert any("NOT the authority yet" in n for n in report["notes"])
    assert all(x["state"] == "n/a" for x in report["series"])


def test_the_shipped_map_is_in_phase_one_and_the_eu_box_is_not_deployed_yet():
    from orchestrator.assign import load

    fm = load()
    assert fm.leases_active is False
    assert fm.node_active("euw") is False
    assert fm.node_active("desktop") is True


def test_a_node_not_deployed_yet_is_shown_but_never_alarming():
    fm = _fleet(_nodes={"desktop": {}, "euw": {"doctor_active": False}},
                _leases_active=False, **{"btc-updown-5m": {}})
    s = FakeStore()
    _beat(s, "desktop", NOW)
    rc, report, _ = check_mod.run(s, fm, now=NOW)
    assert rc == check_mod.OK, report["attention"]
    assert [n for n in report["nodes"] if n["node"] == "euw"][0]["state"] == "not-deployed"
    assert any("not deployed yet" in n for n in report["notes"])


def test_an_unexpected_lease_still_alarms_in_phase_one():
    # leases_active=False suppresses "nobody holds it". It must NOT suppress a
    # lease that exists and has gone wrong — that is an anomaly in phase 1.
    fm = _fleet(_leases_active=False, **{"btc-updown-5m": {}})
    s = FakeStore()
    _beat(s, "desktop", NOW)
    _beat(s, "euw", NOW)
    _lease(s, "btc-updown-5m", "stranger", NOW + 60)
    rc, report, _ = check_mod.run(s, fm, now=NOW)
    assert rc == check_mod.ATTENTION
    assert any("neither home" in a for a in report["attention"])


def test_the_default_notifier_is_dry_and_reports_that_nothing_left():
    lines: list[str] = []
    n = Notifier(log=lines.append)
    assert n.page("test") is False
    assert n.resolve() is False
    assert any("notify:dry" in x for x in lines)


def test_the_cmd_backend_hands_the_payload_to_a_command_and_honours_its_exit_code(tmp_path):
    out = tmp_path / "got.json"
    ok_cmd = f"sh -c 'cat > {out}'"
    n = Notifier("cmd", cmd=ok_cmd, log=lambda _m: None)
    assert n.page("btc-updown-5m lapsed", node="desktop") is True
    payload = json.loads(out.read_text())
    assert payload == {
        "action": "raise", "service": "pmt-fleet",
        "detail": "btc-updown-5m lapsed", "detected_by": "pmt-fleet-doctor@desktop",
    }

    assert Notifier("cmd", cmd="sh -c 'exit 3'", log=lambda _m: None).page("x") is False
    assert Notifier("cmd", cmd=None, log=lambda _m: None).page("x") is False


def test_a_page_detail_is_truncated_the_way_the_attention_lambda_truncates_it(tmp_path):
    out = tmp_path / "got.json"
    n = Notifier("cmd", cmd=f"sh -c 'cat > {out}'", log=lambda _m: None)
    n.page("x" * 500)
    assert len(json.loads(out.read_text())["detail"]) == 200


def test_the_latch_reads_as_already_paged_when_it_cannot_be_read(tmp_path):
    # Never spam on a disk blip. The inverse turns one bad read into a page
    # every cadence.
    bad = tmp_path / "nope" / "paged.json"
    assert read_latch(bad)["paged"] is True

    good = tmp_path / "paged.json"
    write_latch(True, detail="d", path=good)
    assert read_latch(good)["paged"] is True
    write_latch(False, path=good)
    assert read_latch(good)["paged"] is False


def test_stale_detection_is_generous_enough_for_three_missed_beats():
    now = time.time()
    fresh = {"node": "d", "ts": now - 60}
    old = {"node": "d", "ts": now - 200}
    assert not hb_mod.is_stale(fresh, now=now, stale_after_s=check_mod.DEFAULT_STALE_AFTER_S)
    assert hb_mod.is_stale(old, now=now, stale_after_s=check_mod.DEFAULT_STALE_AFTER_S)
    assert hb_mod.is_stale({"node": "d"}, now=now, stale_after_s=120)
