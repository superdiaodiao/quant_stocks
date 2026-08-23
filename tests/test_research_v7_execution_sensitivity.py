import importlib.util
from pathlib import Path

import pandas as pd
import pytest


SCRIPT = Path("scripts/research_v7_execution_sensitivity.py")
SPEC = importlib.util.spec_from_file_location("research_v7_execution", SCRIPT)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(MODULE)


def test_target_panel_scales_stock_and_adds_qqq(tmp_path):
    pd.DataFrame({
        "date": ["2024-01-02"], "close": [50.0]
    }).to_csv(tmp_path / "abc.csv", index=False)
    targets = pd.DataFrame({
        "effective_date": ["2024-01-02"],
        "ticker": ["ABC"],
        "target_weight": [0.5],
    })
    qqq = pd.Series([400.0], index=pd.to_datetime(["2024-01-02"]))
    panel = MODULE.build_v7_target_panel(targets, qqq, tmp_path)
    weights = panel.set_index("ticker")["target_weight"]
    assert weights["ABC"] == pytest.approx(0.10)
    assert weights["QQQ"] == pytest.approx(0.80)
    assert weights.sum() == pytest.approx(0.90)


def test_target_panel_rejects_missing_execution_price(tmp_path):
    targets = pd.DataFrame({
        "effective_date": ["2024-01-02"],
        "ticker": ["ABC"],
        "target_weight": [1.0],
    })
    qqq = pd.Series([400.0], index=pd.to_datetime(["2024-01-02"]))
    with pytest.raises((FileNotFoundError, ValueError)):
        MODULE.build_v7_target_panel(targets, qqq, tmp_path)


def test_path_summary_reports_qqq_separately():
    index = pd.to_datetime(["2024-01-02", "2024-01-03"])
    result = pd.DataFrame({
        "return": [0.02, 0.0], "benchmark_return": [0.0, 0.0],
        "nav": [102.0, 102.0], "transaction_cost": [0.0, 0.0],
        "slippage_cost": [0.0, 0.0], "requested_share_delta": [1, 0],
        "filled_share_delta": [1, 0],
    }, index=index)
    summary = MODULE.summarize_path(
        result, pd.Series([0.02, 0.0], index=index),
        pd.Series([0.01, 0.0], index=index),
    )
    assert summary["wins_vs_nasdaq"] == 1
    assert summary["wins_vs_qqq"] == 1
