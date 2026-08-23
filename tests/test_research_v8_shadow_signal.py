import pandas as pd
import pytest

from scripts.research_v8_shadow_signal import build_v8_recommendations


def test_v8_signal_combines_overlapping_component_targets_and_cash():
    date = pd.Timestamp("2026-09-30")
    v6 = pd.DataFrame({
        "ticker": ["AAA", "__CASH__"],
        "target_weight": [1.0, 0.0],
    })
    v7 = pd.DataFrame({
        "ticker": ["AAA", "BBB"],
        "target_weight": [0.5, 0.5],
    })
    result = build_v8_recommendations(
        v6, v7, {"risk_on_sleeves": 2},
        decision_date=date, execution_date="2026-10-01",
    ).set_index("ticker")
    assert result.loc["AAA", "target_weight"] == pytest.approx(0.0625 + 0.15)
    assert result.loc["BBB", "target_weight"] == pytest.approx(0.15)
    assert result.loc["QQQ", "target_weight"] == pytest.approx(0.6375)
    assert result.loc["__CASH__", "target_weight"] == pytest.approx(0.0)
    assert result["target_weight"].sum() == pytest.approx(1.0)
    assert not result["broker_action_authorized"].any()


def test_v8_signal_preserves_defensive_cash_when_v6_is_risk_off():
    date = pd.Timestamp("2026-09-30")
    cash = pd.DataFrame({"ticker": ["__CASH__"], "target_weight": [0.0]})
    result = build_v8_recommendations(
        cash, cash, {"risk_on_sleeves": 0},
        decision_date=date, execution_date="2026-10-01",
    ).set_index("ticker")
    assert result.loc["QQQ", "target_weight"] == pytest.approx(0.45)
    assert result.loc["__CASH__", "target_weight"] == pytest.approx(0.55)
