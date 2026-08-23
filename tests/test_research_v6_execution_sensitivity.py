from __future__ import annotations

import pandas as pd

from scripts.research_v6_execution_sensitivity import build_v6_targets


def test_v6_targets_scale_stock_and_average_risk_sleeves() -> None:
    base = pd.DataFrame({
        "effective_date": ["2024-02-01", "2024-02-01"],
        "ticker": ["AAA", "BBB"],
        "target_weight": [0.6, 0.4],
        "base_transaction_cost_bps": [10.0, 10.0],
    })
    dates = pd.date_range("2024-02-01", periods=3, freq="B")
    allocation = pd.DataFrame({
        "risk_on_sleeves": [1, 1, 1],
    }, index=dates)

    targets = build_v6_targets(base, allocation)

    weights = targets.set_index("ticker")["target_weight"]
    assert weights["AAA"] == 0.15
    assert weights["BBB"] == 0.10
    assert weights["QQQ"] == 0.375
    assert weights.sum() == 0.625


def test_v6_targets_keep_cash_only_rebalance_date() -> None:
    base = pd.DataFrame(columns=[
        "effective_date", "ticker", "target_weight", "base_transaction_cost_bps"
    ])
    dates = pd.date_range("2024-02-01", periods=3, freq="B")
    allocation = pd.DataFrame({"risk_on_sleeves": 0}, index=dates)

    targets = build_v6_targets(base, allocation)

    assert targets["ticker"].tolist() == ["__CASH__"]
    assert targets["target_weight"].tolist() == [0.0]
