"""Tests for the commands that touch the live fleet.

Only the pure seams run here: the per-symbol basis-guard resolution and the
payload `crypto arm` would hand the engine, with `eval_updown` and the engine
post both stubbed. Nothing in this file can reach a running pmengine.
"""

from __future__ import annotations

import cli_crypto_arm as ca


def test_arm_basis_guard_defaults_to_the_measured_per_symbol_value():
    # A bare arm on an alt must NOT get a flat band (docs/LESSONS.md#L32).
    assert ca._resolve_basis_guard(None, "BTCUSDT") == (6.0, None)
    assert ca._resolve_basis_guard(None, "ETHUSDT") == (8.0, None)
    assert ca._resolve_basis_guard(None, "SOLUSDT") == (10.0, None)


def test_arm_basis_guard_explicit_always_wins():
    for symbol in ("ETHUSDT", "XRPUSDT", "NOTAPAIR"):
        assert ca._resolve_basis_guard(2.5, symbol) == (2.5, None)


def test_arm_basis_guard_unmeasured_symbol_falls_back_loudly():
    from polymarket.constants import BASIS_NOISE_BP

    guard, warning = ca._resolve_basis_guard(None, "XRPUSDT")
    assert guard == BASIS_NOISE_BP
    assert warning and "XRPUSDT" in warning and "--basis-guard" in warning


def test_arm_basis_guard_unknown_symbol_falls_back_loudly():
    guard, warning = ca._resolve_basis_guard(None, "PEPEUSDT")
    assert guard == 3.0
    assert warning is not None



# ---------- `crypto arm --feed` ----------

def _arm(monkeypatch, evaluated, eval_kwargs=None, **kwargs):
    """Run `crypto arm` with the market pricing + engine post stubbed, and
    return the payload that would have gone to the engine.

    `eval_kwargs`, when passed, collects what the CLI asked `eval_updown` for
    — the pre-flight has to price off the same feed the arm is about to use.
    """
    from click.testing import CliRunner

    sent = {}

    def fake_eval(ref, **kw):
        if eval_kwargs is not None:
            eval_kwargs.update(kw)
        return evaluated

    monkeypatch.setattr("polymarket.crypto.eval_updown", fake_eval)

    def fake_post(path, payload):
        sent.update(payload)
        return {"armed": evaluated["slug"]}

    monkeypatch.setattr(ca, "_engine_post", fake_post)
    args = ["https://polymarket.com/event/x", "--size", "100"]
    for k, v in kwargs.items():
        args += [f"--{k}", str(v)]
    result = CliRunner().invoke(ca.crypto_arm, args)
    return result, sent


_TWAP_MARKET = {
    "slug": "xrp-updown-5m-1787442000", "kind": "twap", "symbol": "XRPUSDT",
    "tokens": {"up": "1", "down": "2"}, "start": 1787442000.0, "end": 1787442300.0,
    "sigma_bp_per_min": 14.0, "fee_rate": 0.07, "rem_s": 250.0, "verdict": "ok",
    "spot": 2.9, "spot_source": "binance", "sigma_source": "binance-klines-1m",
    "feed_fell_back": False,
}


def test_arm_feed_defaults_to_binance_and_plumbs_rtds_through(monkeypatch):
    result, payload = _arm(monkeypatch, dict(_TWAP_MARKET))
    assert result.exit_code == 0, result.output
    assert payload["feed"] == "binance", "the default must stay the old engine"

    result, payload = _arm(monkeypatch, dict(_TWAP_MARKET), feed="rtds")
    assert result.exit_code == 0, result.output
    assert payload["feed"] == "rtds"
    assert "feed rtds" in result.output


def test_arm_refuses_rtds_on_a_close_open_market(monkeypatch):
    # The settlement stream has no candle opens; the engine refuses this
    # too, but catching it here names the flag that caused it.
    market = dict(_TWAP_MARKET, kind="close_open")
    result, payload = _arm(monkeypatch, market, feed="rtds")
    assert result.exit_code != 0
    assert "close_open" in result.output and "--feed binance" in result.output
    assert payload == {}, "nothing reached the engine"


def test_arm_rejects_an_unknown_feed(monkeypatch):
    result, payload = _arm(monkeypatch, dict(_TWAP_MARKET), feed="coinbase")
    assert result.exit_code != 0
    assert payload == {}


# ---------- symbols Binance does not list (hype) ----------

def test_arm_preflight_source_follows_listability_not_the_feed_flag(monkeypatch):
    # xrp/doge/bnb arm on rtds AND are listed on Binance. Routing the
    # pre-flight by --feed would re-derive their sigma off a different series
    # — a live-money change that has nothing to do with hype.
    seen = {}
    result, _ = _arm(monkeypatch, dict(_TWAP_MARKET), eval_kwargs=seen, feed="rtds")
    assert result.exit_code == 0, result.output
    assert seen["feed"] == "binance"
    # 5m rtds arms settle on the 60s series (analysis/settle_width.md), so a
    # fallback pre-flight's marks must come from that topic too.
    assert seen["settle_tw_s"] == 60


def test_arm_preflight_follows_an_explicit_settle_width(monkeypatch):
    seen = {}
    _arm(monkeypatch, dict(_TWAP_MARKET), eval_kwargs=seen, feed="rtds", **{"settle-tw": 30})
    assert seen["settle_tw_s"] == 30


_HYPE_MARKET = dict(_TWAP_MARKET, symbol="HYPEUSDT", slug="hype-updown-5m-1787538000",
                    spot=80.9, spot_source="rtds-corpus(2s)",
                    sigma_source="rtds-closes-1m(n=120)", feed_fell_back=True)


def test_arm_refuses_binance_feed_when_binance_has_no_such_pair(monkeypatch):
    # hype: the eval only got a price by reaching for the settlement stream.
    # Arming feed=binance anyway gives the engine a feed that never ticks.
    result, payload = _arm(monkeypatch, dict(_HYPE_MARKET))
    assert result.exit_code != 0
    assert "HYPEUSDT" in result.output and "--feed rtds" in result.output
    assert payload == {}, "nothing reached the engine"


def test_arm_allows_the_stream_fallback_when_the_engine_is_on_that_stream(monkeypatch):
    result, payload = _arm(monkeypatch, dict(_HYPE_MARKET), feed="rtds")
    assert result.exit_code == 0, result.output
    assert payload["feed"] == "rtds" and payload["symbol"] == "HYPEUSDT"
    # updown_rtds maps HYPEUSDT -> hype/usd; the 5m auto-raise still applies.
    assert payload["settle_tw_s"] == 60.0
    assert payload["sigma_bp_per_min"] == 14.0


def test_arm_passes_the_sigma_override_into_the_preflight(monkeypatch):
    seen = {}
    result, _ = _arm(monkeypatch, dict(_TWAP_MARKET), eval_kwargs=seen,
                     feed="rtds", **{"sigma-bp": 18.0})
    assert result.exit_code == 0, result.output
    assert seen["sigma_bp"] == 18.0


def test_arm_names_where_its_numbers_came_from(monkeypatch):
    market = dict(_TWAP_MARKET, spot_source="rtds-corpus(2s)",
                  sigma_source="rtds-closes-1m(n=120)")
    result, _ = _arm(monkeypatch, market, feed="rtds")
    assert result.exit_code == 0, result.output
    assert "rtds-corpus" in result.output and "rtds-closes-1m" in result.output
