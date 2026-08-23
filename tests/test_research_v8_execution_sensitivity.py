import importlib.util
from pathlib import Path

import pandas as pd
import pytest


SCRIPT = Path("scripts/research_v8_execution_sensitivity.py")
SPEC = importlib.util.spec_from_file_location("research_v8_execution", SCRIPT)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(MODULE)


def test_combined_target_math_and_overlap():
    date = pd.Timestamp("2024-02-01")
    v6 = pd.DataFrame({
        "effective_date": [date], "ticker": ["ABC"], "target_weight": [1.0]
    })
    v7 = pd.DataFrame({
        "effective_date": [date], "ticker": ["ABC"], "target_weight": [1.0]
    })
    daily = pd.DataFrame({"risk_on_sleeves": [2]}, index=[date])
    result = MODULE.build_v8_targets(v6, v7, daily).set_index("ticker")
    expected_stock = 0.25 * 0.25 + 0.40 * 0.75
    expected_qqq = 0.60 * 0.75 + 2 * 0.375 * 0.25
    assert result.loc["ABC", "target_weight"] == pytest.approx(expected_stock)
    assert result.loc["QQQ", "target_weight"] == pytest.approx(expected_qqq)
    assert result["target_weight"].sum() == pytest.approx(1.0)


def test_zero_risk_on_v6_leaves_defensive_cash():
    date = pd.Timestamp("2024-02-01")
    cash = pd.DataFrame({
        "effective_date": [date], "ticker": ["__CASH__"], "target_weight": [0.0]
    })
    daily = pd.DataFrame({"risk_on_sleeves": [0]}, index=[date])
    result = MODULE.build_v8_targets(cash, cash, daily)
    assert result["target_weight"].sum() == pytest.approx(0.60 * 0.75)
