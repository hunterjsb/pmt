"""Pure-seam tests for the crypto watch dashboard's render layer: margin-regex
parsing, evidence/countdown color thresholds, the tape log's fixed-width
alignment, the risk header's committed/undecided math, the recent-windows
strip, the arms table's column geometry, and the terminal-mode helpers.

Every dashboard render function must tolerate a missing/partial eval (an
engine restart mid-watch leaves last_eval None or half-built) — several tests
below exist specifically to pin that behavior down.

The fetch/grade side of the same dashboard is tested in test_cli_crypto.py.
"""

from __future__ import annotations

import json

import click

import watch_ui as cc

# ---------- margin-regex / gated-reason compaction ----------

def test_gated_reason_compact_parses_basis_guard():
    reason = ("basis guard: projected margin -4.9bp inside 6.0bp noise band "
              "[banked -3.2bp cushion 9.3bp]")
    assert cc._gated_reason_compact(reason) == "margin -4.9 vs 6.0bp"


def test_gated_reason_compact_positive_margin():
    reason = ("basis guard: projected margin +12.3bp inside 6.0bp noise band "
              "[banked 1bp cushion 2bp]")
    assert cc._gated_reason_compact(reason) == "margin +12.3 vs 6.0bp"


def test_gated_reason_compact_falls_back_to_raw_when_regex_misses():
    reason = "window 42% elapsed, firing opens at 50%"
    assert cc._gated_reason_compact(reason) == reason


def test_gated_reason_compact_handles_missing_reason():
    assert cc._gated_reason_compact(None) == "gated"
    assert cc._gated_reason_compact("") == "gated"


def test_gated_reason_compact_prefers_structured_fields_over_the_regex():
    # A reworded reason the regex cannot touch — the fields still render it.
    e = {"reason": "guard says no", "margin_bp": -4.9, "guard_bp": 6.0}
    assert cc._gated_reason_compact(e["reason"], e) == "margin -4.9 vs 6.0bp"


def test_gated_reason_compact_falls_back_to_regex_on_a_pre_structured_eval():
    # Engine built before the fields shipped: reason only, nulls (or no keys).
    reason = ("basis guard: projected margin -4.9bp inside 6.0bp noise band "
              "[banked -3.2bp cushion 9.3bp]")
    e = {"reason": reason, "margin_bp": None, "guard_bp": None}
    assert cc._gated_reason_compact(reason, e) == "margin -4.9 vs 6.0bp"
    assert cc._gated_reason_compact(reason, {"reason": reason}) == "margin -4.9 vs 6.0bp"


def test_gated_reason_compact_structured_survives_a_non_basis_gate():
    # feed stale: nulls everywhere, reason still shown verbatim.
    e = {"reason": "feed stale", "margin_bp": None, "guard_bp": None}
    assert cc._gated_reason_compact("feed stale", e) == "feed stale"


# ---------- evidence column color thresholds ----------

def test_evidence_style_banked_decided_is_always_green():
    assert cc._evidence_style(0.1, 9.3, True) == "green"


def test_evidence_style_ratio_thresholds():
    assert cc._evidence_style(9.3, 9.3, False) == "green"   # |banked| == cushion
    assert cc._evidence_style(3.0, 9.3, False) == "yellow"  # 0.3*9.3=2.79 <= 3.0 < 9.3
    assert cc._evidence_style(1.0, 9.3, False) == "dim"     # below theta-cleared band
    assert cc._evidence_style(1.0, 0.0, False) == "dim"     # zero cushion: no div-by-zero


def test_evidence_markup_missing_fields_render_dash():
    assert cc._evidence_markup({}) == "[dim]—[/dim]"
    assert cc._evidence_markup({"banked_bp": 1.0}) == "[dim]—[/dim]"  # cushion missing


# ---------- countdown column thresholds ----------

def test_countdown_style_thresholds():
    assert cc._countdown_style(30) == "bold red"   # < 60s
    assert cc._countdown_style(250) == "white"     # < 5min
    assert cc._countdown_style(400) == "dim"       # > 5min


def test_countdown_markup_unparseable_slug_is_dash():
    assert cc._countdown_markup("not-a-slug", 1000.0) == "[dim]—[/dim]"


def test_countdown_markup_past_end_shows_zero():
    slug = "btc-updown-5m-1000"  # end = 1300
    assert cc._countdown_markup(slug, 2000.0) == "[dim]0:00[/dim]"


# ---------- mode text ----------

def test_mode_text_prefers_quiesce_window_state():
    assert cc._mode_text({"state": "flip"}) == "flip"
    assert cc._mode_text({"state": "quiesce"}) == "quiesce"


def test_mode_text_falls_back_to_armed_mode_field():
    assert cc._mode_text({"state": "armed", "mode": "safe"}) == "safe"
    assert cc._mode_text({"state": "armed", "mode": "spec"}) == "spec"


def test_mode_text_missing_is_dash():
    assert cc._mode_text({}) == "—"


# ---------- tape log fixed-width alignment ----------

def test_tape_head_is_fixed_width_regardless_of_slug_length():
    short = cc._tape_head({"t": 1700000100, "slug": "btc-updown-5m-1700000000"})
    long = cc._tape_head({"t": 1700000100, "slug": "doge-updown-60m-1700000000"})
    assert len(short) == len(long) == 24  # "HH:MM:SS  " (10) + slug padded to 14


def test_tape_render_columns_align_across_event_types():
    slug = "doge-updown-60m-1700000000"  # exercises the widest display() form
    base_t = 1700000100
    head = cc._tape_head({"t": base_t, "slug": slug})
    fire = {"t": base_t, "slug": slug, "ev": "fire", "side": "up", "ask": 0.97,
            "fair": 0.99, "net": 0.01, "size": 10, "committed": 5.0, "rho": 0.1,
            "mode": "safe"}
    eval_ = {"t": base_t, "slug": slug, "ev": "eval", "p_up": 0.98, "rho": 0.2,
              "committed": 5.0, "banked_decided": False, "sides": []}
    gated = {"t": base_t, "slug": slug, "ev": "gated",
              "reason": "window 42% elapsed, firing opens at 50%"}
    roll = {"t": base_t, "slug": slug, "ev": "roll", "size": 25}

    rendered = {name: click.unstyle(cc._tape_render(json.dumps(r)))
                for name, r in (("fire", fire), ("eval", eval_),
                                 ("gated", gated), ("roll", roll))}
    for name, line in rendered.items():
        assert line[:len(head)] == head, name
        assert line[len(head)] == " ", name
        # the field after the fixed-width event tag starts at the same offset
        # for every event type — this is the actual alignment claim.
        assert line[len(head) + 1 + cc._TAPE_TAG_WIDTH] == " ", name


# ---------- risk header: committed / undecided ----------

def test_risk_committed_sums_filled_and_flags_undecided():
    arms = {
        "a": {"filled_usdc": 20.0, "eval": {"banked_decided": True}},
        "b": {"filled_usdc": 15.0, "eval": {"banked_decided": False}},
        "c": {"filled_usdc": 5.0, "eval": None},  # missing eval -> undecided
        "d": "not-a-dict",  # defensive: corrupt status entry, must not raise
    }
    committed, undecided = cc._risk_committed(arms)
    assert committed == 40.0
    assert undecided == 20.0  # b (15) + c (5)


def test_risk_committed_empty_or_none():
    assert cc._risk_committed({}) == (0.0, 0.0)
    assert cc._risk_committed(None) == (0.0, 0.0)


def test_build_risk_header_color_thresholds():
    sb = {"riding_n": 0, "riding_usd": 0.0}

    def undecided(amount):
        status = {"arms": {"a": {"filled_usdc": amount, "eval": {"banked_decided": False}}}}
        return cc.build_risk_header(status, {"total": 1000.0}, sb)

    assert "[red]" in undecided(600.0)
    assert "[yellow]" in undecided(400.0)
    line = undecided(200.0)
    assert "[red]" not in line and "[yellow]" not in line


def test_build_risk_header_missing_balance_shows_ellipsis():
    line = cc.build_risk_header({}, {}, {})
    assert "capital …" in line


# ---------- recent-windows strip ----------

def test_window_chip_formats_win_and_loss():
    win = cc._window_chip({"slug": "btc-updown-5m-1700000000", "won": True,
                            "pnl": 12.0, "est": False})
    loss = cc._window_chip({"slug": "eth-updown-15m-1700000000", "won": False,
                             "pnl": -44.0, "est": False})
    assert win == "[green]✓ btc5 +12[/green]"
    assert loss == "[red]✗ eth15 -44[/red]"


def test_window_chip_estimated_is_dim_not_win_loss_colored():
    chip = cc._window_chip({"slug": "sol-updown-5m-1700000000", "won": True,
                             "pnl": 3.0, "est": True})
    assert chip.startswith("[dim]") and "[green]" not in chip


def test_build_windows_strip_empty():
    assert cc.build_windows_strip([]) == "[dim]no resolved windows yet[/dim]"
    assert cc.build_windows_strip(None) == "[dim]no resolved windows yet[/dim]"


# ---------- arms table: missing/partial eval tolerance (4d) ----------

def test_build_arms_table_no_arms_shows_placeholder():
    from rich.console import Console

    t = cc.build_arms_table({}, now=1000.0)
    c = Console(record=True, width=140)
    c.print(t)
    assert "engine unreachable or no arms" in c.export_text()


def test_build_arms_table_tolerates_none_and_partial_eval():
    from rich.console import Console

    arms = {
        "btc-updown-5m-9999999999": {"filled_usdc": 10.0, "roll": True, "eval": None},
        "eth-updown-15m-8888888888": {"filled_usdc": 5.0, "roll": False,
                                       "eval": {"state": "armed"}},  # no p_up/rho/sides
        "not-a-real-slug": {},  # missing filled_usdc/roll entirely
    }
    t = cc.build_arms_table(arms, now=1000.0)  # must not raise
    c = Console(record=True, width=160)
    c.print(t)
    out = c.export_text()
    assert "—" in out  # missing fields degrade gracefully, not a traceback
    assert "$10.00" in out


def test_build_arms_table_column_geometry_is_identical_across_states():
    from rich.console import Console

    def col_boundaries(arms):
        t = cc.build_arms_table(arms, now=1700000000.0)
        c = Console(record=True, width=140)
        c.print(t)
        lines = [ln for ln in c.export_text().splitlines() if ln.startswith("│")]
        return [[i for i, ch in enumerate(ln) if ch == "│"] for ln in lines]

    short = col_boundaries({"btc-updown-5m-1700000300": {"filled_usdc": 1.0, "eval": None}})
    long_reason = ("basis guard: projected margin -4.9bp inside 6.0bp noise band "
                   "[banked -3.2bp cushion 9.3bp] and then some extra unexpected text")
    long_ = col_boundaries({
        "btc-updown-5m-1700000300": {"filled_usdc": 1.0,
                                      "eval": {"state": "gated", "reason": long_reason}},
        "eth-updown-15m-1700000900": {"filled_usdc": 2.0, "eval": {
            "state": "armed", "p_up": 0.8732, "mode": "safe", "rho": 0.4,
            "banked_bp": 12.3, "cushion_bp": 9.3, "banked_decided": True,
            "committed": 2.0, "sides": [{"side": "up", "safety": 0.9}],
        }},
    })
    assert short[0] == long_[0] == long_[1]

# ---------- terminal cbreak / quit helpers ----------

class _FakeStdin:
    def __init__(self, isatty=True, chars=""):
        self._isatty = isatty
        self._chars = chars

    def isatty(self):
        return self._isatty

    def read(self, n):
        ch, self._chars = self._chars[:n], self._chars[n:]
        return ch

    def fileno(self):
        return 0


def test_cbreak_stdin_noop_when_not_a_tty(monkeypatch):
    monkeypatch.setattr(cc.sys, "stdin", _FakeStdin(isatty=False))
    assert cc._cbreak_stdin() is None
    cc._restore_stdin(None)  # must not raise


def test_poll_key_none_when_not_a_tty(monkeypatch):
    monkeypatch.setattr(cc.sys, "stdin", _FakeStdin(isatty=False))
    assert cc._poll_key() is None


def test_poll_key_reads_q_when_ready(monkeypatch):
    monkeypatch.setattr(cc.sys, "stdin", _FakeStdin(isatty=True, chars="q"))
    monkeypatch.setattr(cc.select, "select", lambda r, w, x, t: ([0], [], []))
    monkeypatch.setattr(cc.os, "read", lambda fd, n: b"q")
    assert cc._poll_key() == "q"


def test_poll_key_returns_other_keys_lowercased(monkeypatch):
    monkeypatch.setattr(cc.sys, "stdin", _FakeStdin(isatty=True, chars="x"))
    monkeypatch.setattr(cc.select, "select", lambda r, w, x, t: ([0], [], []))
    monkeypatch.setattr(cc.os, "read", lambda fd, n: b"H")
    assert cc._poll_key() == "h"


def test_poll_key_none_when_nothing_ready(monkeypatch):
    monkeypatch.setattr(cc.sys, "stdin", _FakeStdin(isatty=True, chars=""))
    monkeypatch.setattr(cc.select, "select", lambda r, w, x, t: ([], [], []))
    assert cc._poll_key() is None


def test_quit_requested_swallows_select_errors(monkeypatch):
    monkeypatch.setattr(cc.sys, "stdin", _FakeStdin(isatty=True))

    def boom(*a, **k):
        raise OSError("bad fd")

    monkeypatch.setattr(cc.select, "select", boom)
    assert cc._poll_key() is None  # never raises, dashboard must survive


def test_controls_panel_renders():
    from rich.console import Console
    import io
    con = Console(file=io.StringIO(), width=160)
    con.print(cc._controls_panel())
    out = con.file.getvalue()
    assert "quit" in out and "controls" in out


def test_poll_key_passes_timeout_through_to_select(monkeypatch):
    seen = {}

    def fake_select(r, w, x, t):
        seen["timeout"] = t
        return ([], [], [])

    monkeypatch.setattr(cc.sys, "stdin", _FakeStdin(isatty=True))
    monkeypatch.setattr(cc.select, "select", fake_select)
    assert cc._poll_key() is None
    assert seen["timeout"] == 0.0          # default stays non-blocking
    assert cc._poll_key(0.05) is None
    assert seen["timeout"] == 0.05         # the watch loop's 20Hz pacing


def test_wait_key_without_a_tty_paces_instead_of_spinning(monkeypatch):
    slept = []
    monkeypatch.setattr(cc.sys, "stdin", _FakeStdin(isatty=False))
    monkeypatch.setattr(cc.time, "sleep", lambda s: slept.append(s))
    assert cc._wait_key(0.05) is None
    assert slept == [0.05]  # no tty to select on -> the sleep is the pacing

