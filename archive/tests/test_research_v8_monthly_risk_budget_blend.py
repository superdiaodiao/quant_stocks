import importlib.util
from pathlib import Path

import pandas as pd
import pytest


SCRIPT = Path("scripts/research_v8_monthly_risk_budget_blend.py")
SPEC = importlib.util.spec_from_file_location("research_v8", SCRIPT)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(MODULE)


def _sleeve(returns):
    index = pd.to_datetime(["2024-01-02", "2024-01-03", "2024-02-01"])
    return pd.DataFrame({
        "return": returns,
        "benchmark_return": [0.0, 0.0, 0.0],
        "qqq_return": [0.0, 0.0, 0.0],
    }, index=index)


def test_combination_holds_sleeves_between_monthly_boundaries():
    result = MODULE.combine_monthly_sleeves(
        _sleeve([0.0, 0.10, 0.0]),
        _sleeve([0.0, 0.0, 0.0]),
        v7_weight=0.25,
        transfer_cost_bps=0.0,
    )
    assert result.loc["2024-01-03", "nav"] == pytest.approx(1.075)
    assert result.loc["2024-01-03", "sleeve_turnover"] == 0.0
    assert result.loc["2024-02-01", "sleeve_turnover"] > 0.0


def test_transfer_cost_is_charged_only_at_rebalance():
    result = MODULE.combine_monthly_sleeves(
        _sleeve([0.0, 0.10, 0.0]),
        _sleeve([0.0, 0.0, 0.0]),
        v7_weight=0.5,
        transfer_cost_bps=100.0,
    )
    assert result.loc["2024-01-02", "sleeve_transfer_cost"] > 0.0
    assert result.loc["2024-01-03", "sleeve_transfer_cost"] == 0.0


def test_combination_validates_weights_and_overlap():
    sleeve = _sleeve([0.0, 0.0, 0.0])
    with pytest.raises(ValueError, match="v7_weight"):
        MODULE.combine_monthly_sleeves(sleeve, sleeve, v7_weight=1.0)
    with pytest.raises(ValueError, match="non-negative"):
        MODULE.combine_monthly_sleeves(sleeve, sleeve, transfer_cost_bps=-1)


def test_primary_configuration_uses_execution_robust_capital_split():
    assert MODULE.V7_STOCK_WEIGHT == pytest.approx(0.40)
    assert MODULE.V7_CAPITAL_WEIGHT == pytest.approx(0.75)
    assert MODULE.V6_CAPITAL_WEIGHT == pytest.approx(0.25)
