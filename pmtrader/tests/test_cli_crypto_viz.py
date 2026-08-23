"""Tests for cli_crypto_viz module."""

import json
from click.testing import CliRunner
from cli_crypto import crypto_group
from cli_crypto_viz import (
    _calc_twap_p_up, _calc_close_open_p_up, build_trade_metrics,
    render_ascii_p_curve, build_sensitivity_matrix,
)

def test_calc_twap_p_up_at_ref():
    # Spot == Ref, Banked == Ref, 50% elapsed -> P(UP) should be ~0.50
    p, proj, breakeven = _calc_twap_p_up(
        spot=90000.0, ref_px=90000.0, banked=90000.0,
        banked_s=150.0, rem_s=150.0, sig1m=0.0015
    )
    assert round(p, 2) == 0.50
    assert proj == 90000.0
    assert breakeven == 90000.0

def test_calc_twap_p_up_positive_margin():
    # Spot > Ref, Banked > Ref -> P(UP) should be > 0.50
    p, proj, breakeven = _calc_twap_p_up(
        spot=90100.0, ref_px=90000.0, banked=90050.0,
        banked_s=150.0, rem_s=150.0, sig1m=0.0015
    )
    assert p > 0.50
    assert proj > 90000.0

def test_calc_close_open_p_up():
    # Spot > Open -> P(UP) > 0.5
    p_high = _calc_close_open_p_up(spot=90100.0, open_px=90000.0, t_min=2.5, sig1m=0.0015)
    assert p_high > 0.50

    # Spot < Open -> P(UP) < 0.5
    p_low = _calc_close_open_p_up(spot=89900.0, open_px=90000.0, t_min=2.5, sig1m=0.0015)
    assert p_low < 0.50

def test_build_trade_metrics():
    metrics = build_trade_metrics(fair_up=0.60, ask_up=0.50, ask_down=0.45, fee_rate=0.07)
    up = metrics["up"]
    assert up["fair"] == 0.60
    assert up["ask"] == 0.50
    assert up["fee"] == 0.07 * 0.50  # fee at ask 0.50 is 0.035
    assert round(up["cost"], 4) == 0.535
    assert round(up["delta_p"], 2) == 0.10
    assert round(up["net_edge"], 4) == 0.065
    assert round(up["roi"], 1) == 12.1  # 0.065 / 0.535 * 100

    down = metrics["down"]
    assert down["fair"] == 0.40
    assert down["ask"] == 0.45

def test_render_ascii_p_curve():
    curve = render_ascii_p_curve(
        spot=90000.0, ref_px=90000.0, banked=90000.0,
        banked_s=150.0, rem_s=150.0, sig1m=0.0015, kind="twap"
    )
    assert "P(UP) Sensitivity S-Curve" in curve
    assert "Margin Shift (bp)" in curve

def test_cli_crypto_viz_sim_json():
    runner = CliRunner()
    res = runner.invoke(crypto_group, ["viz", "--json", "--spot", "90000", "--ask-up", "0.50"])
    assert res.exit_code == 0
    data = json.loads(res.output)
    assert data["symbol"] == "BTCUSDT"
    assert "metrics" in data
    assert "p_up" in data

def test_cli_crypto_viz_sim_table_output():
    runner = CliRunner()
    res = runner.invoke(crypto_group, ["viz", "--spot", "90000", "--ask-up", "0.48", "--ask-down", "0.48"])
    assert res.exit_code == 0
    assert "Model Mechanics & Current Variables" in res.output
    assert "Probability Delta (ΔP) & Expected Return (ROI) Analysis" in res.output
    assert "Scenario & Sensitivity Analysis Matrix" in res.output
