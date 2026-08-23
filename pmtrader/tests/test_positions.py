"""The data-api positions client: the watch dashboard's `now` column.

A best-effort DISPLAY feed, so every test here is really the same test — it
must degrade to "no mark" rather than to a wrong one, because the number
sits inches from an entry price the operator reads as comparable.
"""

from __future__ import annotations

from polymarket import positions


def _pos(slug="bnb-updown-5m-1787510100", outcome="Up", px=0.99, **kw):
    row = {"slug": slug, "outcome": outcome, "curPrice": px}
    row.update(kw)
    return row


def test_current_odds_keys_by_window_and_our_side():
    rows = [_pos(outcome="Up", px=0.99), _pos(outcome="Down", px=0.01)]
    assert positions.current_odds(rows) == {
        ("bnb-updown-5m-1787510100", "up"): 0.99,
        ("bnb-updown-5m-1787510100", "down"): 0.01,
    }


def test_current_odds_drops_the_wallets_long_dated_markets():
    # The endpoint has no slug filter and the wallet holds unrelated
    # positions; this dashboard has nothing to say about them.
    rows = [_pos(), _pos(slug="will-bitcoin-dip-to-70k-in-august-2026",
                          outcome="No", px=0.845)]
    assert list(positions.current_odds(rows)) == [("bnb-updown-5m-1787510100", "up")]


def test_current_odds_keeps_a_resolved_but_unredeemed_position():
    # 0.00 or 1.00 IS the answer to "how did it land" — the exact moment the
    # operator is waiting on while the redeem row lags.
    rows = [_pos(px=0), _pos(slug="eth-updown-5m-1787462100", outcome="Down", px=1)]
    odds = positions.current_odds(rows)
    assert odds[("bnb-updown-5m-1787510100", "up")] == 0.0
    assert odds[("eth-updown-5m-1787462100", "down")] == 1.0


def test_current_odds_never_raises_on_a_half_built_row():
    rows = ["not-a-dict", {}, _pos(px=None), _pos(slug=""), _pos(outcome=""),
            _pos(px="n/a"), _pos(slug="ok-updown-5m-1", px="0.5")]
    odds = positions.current_odds(rows)
    assert odds == {("ok-updown-5m-1", "up"): 0.5}  # a numeric string is still a price


def test_current_odds_of_nothing():
    assert positions.current_odds(None) == {}
    assert positions.current_odds([]) == {}


def test_fetch_positions_asks_the_data_api_for_the_funder_only(monkeypatch):
    seen = {}

    class _Resp:
        def raise_for_status(self):
            pass

        def json(self):
            return [_pos()]

    def fake_get(url, params=None, headers=None, timeout=None):
        seen.update(url=url, params=params)
        return _Resp()

    monkeypatch.setattr(positions.requests, "get", fake_get)
    assert positions.fetch_positions("0xabc") == [_pos()]
    assert seen["url"] == positions.POSITIONS_URL
    assert seen["params"]["user"] == "0xabc"
    # Read-only and address-only: no key, no signature, no engine.
    assert set(seen["params"]) == {"user", "limit"}


def test_fetch_positions_tolerates_an_empty_body(monkeypatch):
    class _Resp:
        def raise_for_status(self):
            pass

        def json(self):
            return None

    monkeypatch.setattr(positions.requests, "get", lambda *a, **k: _Resp())
    assert positions.fetch_positions("0xabc") == []
