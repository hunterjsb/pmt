"""Book parsing, window discovery, the grader, status, live gating, the CLI.

No network anywhere: every HTTP call goes through an injected fake session.
"""

from __future__ import annotations

import json
import math

import pytest

from pilot2 import books, cli, execution, grade, policy, series, state, status, windows

SLUG = "doge-updown-5m-1787400000"
END = 1787400300.0


# --- top of book -----------------------------------------------------------

def test_top_of_book_aggregates_size_at_the_best_price():
    """The CLOB can list one price across several maker rows. A fill cap read
    off the first row alone understates what was really on offer."""
    top = books.parse_top({
        "bids": [{"price": "0.53", "size": "10"}, {"price": "0.52", "size": "99"}],
        "asks": [{"price": "0.55", "size": "40"}, {"price": "0.55", "size": "60"},
                 {"price": "0.58", "size": "999"}],
    })
    assert top.ask == 0.55 and top.ask_size == 100.0
    assert top.bid == 0.53 and top.bid_size == 10.0


def test_an_unquoted_side_is_nan_not_a_substituted_number():
    top = books.parse_top({"bids": [{"price": "0.5", "size": "1"}], "asks": []})
    assert math.isnan(top.ask) and not top.quoted
    assert books.parse_top(None) is books.EMPTY or math.isnan(books.parse_top(None).ask)


def test_a_junk_book_payload_degrades_to_no_quote():
    assert math.isnan(books.parse_top({"asks": [{"price": "x", "size": "1"}]}).ask)
    assert math.isnan(books.parse_top("not a book").ask)


class FakeSession:
    """Records every call and answers from a canned map."""

    def __init__(self, answers=None, fail=False):
        self.answers = answers or {}
        self.calls = []
        self.fail = fail

    def get(self, url, params=None, headers=None, timeout=None):
        self.calls.append((url, dict(params or {})))
        if self.fail:
            raise OSError("network down")
        return _Resp(self.answers.get(url, []))


class _Resp:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        pass

    def json(self):
        return self._payload


def test_an_unreachable_book_is_no_quote_never_an_exception():
    p = books.BookPoller(session=FakeSession(fail=True))
    assert math.isnan(p.top("TOK").ask)
    assert p.failures == 1


def test_the_poll_interval_is_a_pilot_interval_not_an_hft_one():
    """analysis/watch_load.md: a hot loop that treats an upstream as free is
    the bug. Two seconds, and it is a constant."""
    assert books.POLL_INTERVAL_S == 2.0
    assert books.REQUEST_TIMEOUT_S <= 3.0


# --- window discovery ------------------------------------------------------

GAMMA_MARKET = [{
    "slug": SLUG,
    "outcomes": json.dumps(["Up", "Down"]),
    "clobTokenIds": json.dumps(["111", "222"]),
    "feeSchedule": {"rate": 0.07},
}]


def test_a_gamma_market_becomes_a_window_with_bounds_from_the_slug():
    w = windows.parse_market(SLUG, GAMMA_MARKET)
    assert w.token_up == "111" and w.token_down == "222"
    assert (w.start, w.end, w.dur_s) == (1787400000.0, END, 300)
    assert w.symbol == "doge/usd" and w.series == "doge-updown-5m"
    assert w.elapsed_frac(1787400150.0) == 0.5


def test_a_market_with_no_tokens_is_not_a_window():
    assert windows.parse_market(SLUG, [{"outcomes": "[]", "clobTokenIds": "[]"}]) is None
    assert windows.parse_market(SLUG, []) is None
    assert windows.parse_market("not-a-slug", GAMMA_MARKET) is None


def test_discovery_asks_gamma_once_per_window(monkeypatch):
    from polymarket import hosts
    s = FakeSession({f"{hosts.GAMMA}/markets": GAMMA_MARKET})
    c = windows.WindowCache(session=s)
    for _ in range(5):
        assert c.current("doge-updown-5m", 1787400123.0).slug == SLUG
    assert c.lookups == 1, "a window is immutable; one lookup for its whole life"


def test_a_gamma_miss_is_not_retried_on_every_poll():
    from polymarket import hosts
    s = FakeSession({f"{hosts.GAMMA}/markets": []})
    c = windows.WindowCache(session=s)
    for _ in range(10):
        assert c.current("doge-updown-5m", 1787400123.0) is None
    assert c.lookups == 1, "one absent market must not become a request storm"


def test_resolution_pins_closed_true():
    """Gamma's /markets defaults to closed=false and answers [] for every
    SETTLED window, which parses as 'not resolved yet' and rides forever.
    That default hid -$272.35 for 13-25h on 2026-08-23."""
    from polymarket import hosts
    resolved = [{"outcomes": json.dumps(["Up", "Down"]),
                 "outcomePrices": json.dumps(["1", "0"])}]
    s = FakeSession({f"{hosts.GAMMA}/markets": resolved})
    out = windows.resolution(SLUG, session=s)
    assert out == {"resolved": True, "winner": "up", "reachable": True}
    assert s.calls[0][1].get("closed") == "true", \
        "settled markets are invisible without the flag"


def test_an_unreachable_gamma_is_not_a_resolution():
    s = FakeSession(fail=True)
    out = windows.resolution(SLUG, session=s)
    assert out["resolved"] is False and out["reachable"] is False


# --- the grader ------------------------------------------------------------

def _shadow_row(home, slug=SLUG, side="up", ask=0.55, shares=9.0, t=END - 100.0,
                model=0.90, book=0.50):
    state.append(state.SHADOW_TAPE, {
        "t": t, "ev": state.EV_SHADOW, "mode": "shadow", "slug": slug,
        "series": "doge-updown-5m", "side": side, "end": END, "ask": ask,
        "shares": shares, "notional": shares * ask, "edge": 0.3,
        "p_side": 0.9, "model_p_up": model, "book_p_up": book,
        "blend_p_up": 0.7, "w": policy.W_SEED, "would_trade": True,
    }, home)


def test_grade_pays_a_winner_a_dollar_a_share_and_wipes_a_loser(tmp_path):
    _shadow_row(tmp_path)
    out = grade.run(tmp_path, now=END + 1000.0,
                    resolve=lambda s: {"resolved": True, "winner": "up"},
                    log=lambda *_: None)
    assert out["graded"] == 1 and out["wins"] == 1
    row = next(iter(state.iter_records(state.GRADED, tmp_path)))
    # the graded row rounds to 4dp, so compare at that grain, not at approx's
    # relative default
    assert row["pnl"] == pytest.approx(policy.realized_pnl(9.0, 0.55, True), abs=1e-4)
    assert row["pnl"] > 0


def test_grade_books_the_loss_leg_at_minus_one_hundred_percent(tmp_path):
    _shadow_row(tmp_path)
    grade.run(tmp_path, now=END + 1000.0,
              resolve=lambda s: {"resolved": True, "winner": "down"},
              log=lambda *_: None)
    row = next(iter(state.iter_records(state.GRADED, tmp_path)))
    assert row["won"] is False
    assert row["pnl"] < -9.0 * 0.55, "the fee is charged on the wipeout too"


def test_grade_waits_out_the_settlement_grace(tmp_path):
    _shadow_row(tmp_path)
    out = grade.run(tmp_path, now=END + 10.0, resolve=lambda s: 1 / 0, log=lambda *_: None)
    assert out["graded"] == 0, "gamma is not even asked before the grace expires"


def test_grade_leaves_an_unresolved_window_riding(tmp_path):
    _shadow_row(tmp_path)
    out = grade.run(tmp_path, now=END + 1000.0,
                    resolve=lambda s: {"resolved": False, "winner": None},
                    log=lambda *_: None)
    assert out["graded"] == 0 and out["unresolved"] == 1
    assert list(state.iter_records(state.GRADED, tmp_path)) == []


def test_grade_is_idempotent(tmp_path):
    _shadow_row(tmp_path)
    r = {"resolved": True, "winner": "up"}
    grade.run(tmp_path, now=END + 1000.0, resolve=lambda s: r, log=lambda *_: None)
    again = grade.run(tmp_path, now=END + 2000.0, resolve=lambda s: r, log=lambda *_: None)
    assert again["graded"] == 0
    assert len(list(state.iter_records(state.GRADED, tmp_path))) == 1


def test_grade_never_books_one_window_side_twice_from_a_doubled_tape(tmp_path):
    """A restart mid-window forgets `fired` (it is in memory only) and the next
    poll writes a SECOND shadow row for a side already taken. Idempotency that
    only holds across runs would grade both and double that window into the
    ledger for good."""
    _shadow_row(tmp_path)
    _shadow_row(tmp_path)  # the post-restart duplicate
    r = {"resolved": True, "winner": "up"}
    out = grade.run(tmp_path, now=END + 1000.0, resolve=lambda s: r, log=lambda *_: None)
    assert out["graded"] == 1, "one window-side is one graded row"
    rows = list(state.iter_records(state.GRADED, tmp_path))
    assert len(rows) == 1
    assert out["pnl"] == pytest.approx(rows[0]["pnl"], abs=1e-4), "P&L is not doubled"
    # and the second run still adds nothing
    assert grade.run(tmp_path, now=END + 2000.0, resolve=lambda s: r,
                     log=lambda *_: None)["graded"] == 0


def test_grade_writes_the_weight_and_keeps_the_seed_below_the_fit_floor(tmp_path):
    _shadow_row(tmp_path)
    out = grade.run(tmp_path, now=END + 1000.0,
                    resolve=lambda s: {"resolved": True, "winner": "up"},
                    log=lambda *_: None)
    assert out["w"] == policy.W_SEED and out["w_source"] == policy.W_SOURCE_SEED
    d = state.read_json(state.BLEND_WEIGHT, tmp_path)
    assert d["w"] == policy.W_SEED and d["min_rows"] == policy.MIN_FIT_ROWS


def test_the_weight_refit_is_walk_forward_by_construction(tmp_path):
    """Calibration rows only exist for windows that have RESOLVED, so no fit
    can see a row it is later scored on. Here the model is always right."""
    for i in range(policy.MIN_FIT_ROWS):
        state.append(state.CALIB, {"ev": state.EV_CALIB, "slug": f"doge-updown-5m-{i}",
                                   "series": "doge-updown-5m", "end": END,
                                   "model_p_up": 1.0, "book_p_up": 0.0}, tmp_path)
    out = grade.run(tmp_path, now=END + 1000.0,
                    resolve=lambda s: {"resolved": True, "winner": "up"},
                    log=lambda *_: None)
    assert out["w_rows"] == policy.MIN_FIT_ROWS
    assert out["w"] == 1.0 and out["w_source"] == policy.W_SOURCE_FIT


def test_calibration_rows_skip_windows_that_have_not_settled(tmp_path):
    state.append(state.CALIB, {"ev": state.EV_CALIB, "slug": SLUG, "end": END,
                               "model_p_up": 0.9, "book_p_up": 0.5}, tmp_path)
    assert grade.calibration_rows(tmp_path, now=END + 10.0, resolve=lambda s: 1 / 0) == []


# --- live gating -----------------------------------------------------------

def test_live_needs_both_switches():
    assert execution.live_enabled(True, {"PILOT2_LIVE": "1"}) is True
    assert execution.live_enabled(True, {}) is False
    assert execution.live_enabled(False, {"PILOT2_LIVE": "1"}) is False
    assert execution.live_enabled(True, {"PILOT2_LIVE": "true"}) is False


def test_deposit_wallet_needs_a_funder():
    with pytest.raises(execution.LiveRefused):
        execution.read_creds({"PM_PRIVATE_KEY": "0x1", "PM_SIGNATURE_TYPE": "3"})
    c = execution.read_creds({"PM_PRIVATE_KEY": "0x1", "PM_SIGNATURE_TYPE": "3",
                              "PM_FUNDER_ADDRESS": "0x6da3"})
    assert c.sig_type == execution.DEPOSIT_WALLET_SIG_TYPE == 3


def test_no_key_no_live():
    with pytest.raises(execution.LiveRefused):
        execution.read_creds({"PM_FUNDER_ADDRESS": "0x6da3"})


def test_credentials_describe_themselves_without_the_key():
    d = execution.read_creds({"PM_PRIVATE_KEY": "0xSECRET", "PM_SIGNATURE_TYPE": "3",
                              "PM_FUNDER_ADDRESS": "0x6da3"}).describe()
    assert d == {"funder": "0x6da3", "sig_type": 3, "host": execution.hosts.CLOB}
    assert "SECRET" not in json.dumps(d)


def test_key_paths_are_parameterised_never_hardcoded(tmp_path):
    """The box keeps the L0 key in ~/.pmt/l0.env and nothing in this package
    may name that path. PILOT2_ENV_FILES is the only way in."""
    import ast
    import inspect
    tree = ast.parse(inspect.getsource(execution))
    docstrings = {id(ast.get_docstring(n, clean=False))
                  for n in ast.walk(tree)
                  if isinstance(n, (ast.Module, ast.ClassDef, ast.FunctionDef))}
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str) \
                and id(node.value) not in docstrings:
            assert "ec2-user" not in node.value and "/.pmt/" not in node.value, \
                f"a key path is a code constant, not a parameter: {node.value!r}"
    f = tmp_path / "creds.env"
    f.write_text("PILOT2_TEST_VAR=from_file\n")
    assert execution.load_env_files(str(f)) == [str(f)]
    import os
    assert os.environ.get("PILOT2_TEST_VAR") == "from_file"
    os.environ.pop("PILOT2_TEST_VAR", None)


def test_filled_shares_reads_zero_from_an_ack_that_says_nothing():
    assert execution.filled_shares({"success": True}) == 0.0
    assert execution.filled_shares(None) == 0.0
    assert execution.filled_shares({"takingAmount": "12.5"}) == 12.5


# --- status + cli ----------------------------------------------------------

def test_status_counts_opportunities_would_trades_and_refusals(tmp_path):
    _shadow_row(tmp_path)
    state.append(state.SHADOW_TAPE, {"ev": state.EV_REFUSED, "slug": SLUG, "side": "up",
                                     "refused": "one_clip_per_window_side"}, tmp_path)
    state.append(state.SHADOW_TAPE, {"ev": state.EV_WINDOW, "slug": SLUG,
                                     "series": "doge-updown-5m", "polls": 90,
                                     "priced": 88, "two_sided": 80, "ev_pass": 4,
                                     "fired": 1, "refused": {}}, tmp_path)
    grade.run(tmp_path, now=END + 1000.0,
              resolve=lambda s: {"resolved": True, "winner": "up"}, log=lambda *_: None)
    s = status.summarise(tmp_path, now=END + 2000.0)
    assert s["shadow"]["windows"] == 1
    assert s["shadow"]["ev_opportunities"] == 4
    assert s["shadow"]["would_trade"] == 1
    assert s["shadow"]["refused"] == {"one_clip_per_window_side": 1}
    assert s["graded"]["n"] == 1 and s["graded"]["wins"] == 1
    assert s["risk"]["max_total_exposure_usdc"] == 40.0
    assert s["halted"] is False
    text = status.render(s)
    assert "would-trade" in text and "MANUAL SWEEP" in text


def test_status_reports_the_halt_file(tmp_path):
    (tmp_path / "HALT").write_text("stop\n")
    s = status.summarise(tmp_path, now=END)
    assert s["halted"] is True
    assert "PRESENT" in status.render(s)


def test_status_survives_a_torn_tape_line(tmp_path):
    p = state.ensure_home(tmp_path) / state.SHADOW_TAPE
    p.write_text(json.dumps({"t": 1, "ev": state.EV_SHADOW, "series": "x"}) + "\n"
                 + '{"t": 2, "ev": "shado\n')
    s = status.summarise(tmp_path, now=END)
    assert s["shadow"]["would_trade"] == 1


def test_cli_status_exits_zero(tmp_path, capsys):
    assert cli.main(["--home", str(tmp_path), "status"]) == 0
    assert "pilot2" in capsys.readouterr().out


def test_cli_series_refuses_an_engine_owned_series(monkeypatch, capsys):
    monkeypatch.setenv(series.SERIES_ENV, "btc-updown-5m")
    assert cli.main(["series"]) == 2
    assert "REFUSED" in capsys.readouterr().out


def test_cli_run_refuses_before_it_builds_a_client(monkeypatch, tmp_path):
    monkeypatch.setenv(series.SERIES_ENV, "sol-updown-5m")
    monkeypatch.setenv("PILOT2_LIVE", "1")
    monkeypatch.setattr(execution, "build_client",
                        lambda *a, **kw: pytest.fail("a client was built for a refused series"))
    assert cli.main(["--home", str(tmp_path), "run", "--live"]) == 2


def test_state_writes_only_under_the_pilots_own_home(tmp_path):
    state.append(state.SHADOW_TAPE, {"ev": "x"}, tmp_path)
    assert (tmp_path / state.SHADOW_TAPE).exists()
    assert state.pilot_home(tmp_path) == tmp_path


def test_pilot_home_prefers_the_explicit_argument_then_the_env(monkeypatch, tmp_path):
    monkeypatch.setenv(state.HOME_ENV, str(tmp_path / "fromenv"))
    assert state.pilot_home() == tmp_path / "fromenv"
    assert state.pilot_home(tmp_path / "explicit") == tmp_path / "explicit"
