import importlib.util
from pathlib import Path

import pandas as pd
import pytest


SCRIPT = Path("scripts/research_v7_qqq_targeted_core_satellite.py")
SPEC = importlib.util.spec_from_file_location("research_v7", SCRIPT)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(MODULE)


def _candidate(values):
    index = pd.to_datetime(["2020-12-31", "2021-12-31", "2022-12-30"])
    return pd.DataFrame({
        "strategy": values,
        "benchmark": [0.0, 0.0, 0.0],
        "turnover": [0.0, 0.0, 0.0],
    }, index=index)


def test_selector_uses_only_completed_prior_years():
    candidates = {0: _candidate([0.10, 0.10, -0.90]), 1: _candidate([0.05, 0.05, 0.90])}
    qqq = pd.Series(0.0, index=next(iter(candidates.values())).index)
    selected, ranking = MODULE.select_qqq_stable_ensemble(
        candidates, qqq, "2021-12-31", ensemble_size=1
    )
    assert selected == [0]
    assert ranking["training_end"].max() == pd.Timestamp("2021-12-31")


def test_monthly_simulator_does_not_daily_rebalance():
    index = pd.to_datetime(["2024-01-02", "2024-01-03", "2024-02-01"])
    stock = pd.DataFrame({
        "strategy": [0.0, 0.10, 0.0],
        "benchmark": [0.0, 0.0, 0.0],
        "turnover": [0.0, 0.0, 0.0],
    }, index=index)
    result = MODULE.simulate_monthly_core_satellite(
        stock, pd.Series(0.0, index=index), transaction_cost_bps=0.0
    )
    assert result.loc["2024-01-03", "nav"] == pytest.approx(1.02)
    assert result.loc["2024-01-03", "turnover"] == 0.0
    assert result.loc["2024-02-01", "turnover"] > 0.0


def test_weight_and_cost_validation():
    index = pd.to_datetime(["2024-01-02"])
    stock = pd.DataFrame({"strategy": [0.0], "benchmark": [0.0], "turnover": [0.0]}, index=index)
    qqq = pd.Series(0.0, index=index)
    with pytest.raises(ValueError, match="weights"):
        MODULE.simulate_monthly_core_satellite(stock, qqq, stock_weight=0.8, qqq_weight=0.3)
    with pytest.raises(ValueError, match="transaction_cost"):
        MODULE.simulate_monthly_core_satellite(stock, qqq, transaction_cost_bps=-1)


def test_qqq_loader_allows_only_leading_warmup_gap():
    index = pd.to_datetime(["2017-12-29", "2018-01-02", "2018-01-03"])
    qqq = pd.DataFrame({"close": [100.0, 101.0]}, index=index[1:])
    result = MODULE.qqq_total_return(qqq, index)
    assert pd.isna(result.iloc[0])
    assert result.iloc[1] == 0.0
    broken = pd.DataFrame({"close": [100.0, 101.0]}, index=index[[0, 2]])
    with pytest.raises(ValueError, match="internal gap"):
        MODULE.qqq_total_return(broken, index)
    allowed = pd.Series([False, True, False], index=index)
    repaired = MODULE.qqq_total_return(
        broken, index, allowed_market_closed=allowed
    )
    assert repaired.iloc[1] == 0.0


def test_summary_neutralizes_largest_day_without_hiding_qqq_result():
    index = pd.to_datetime(["2024-01-02", "2024-01-03"])
    result = pd.DataFrame({
        "return": [0.10, 0.01],
        "benchmark_return": [0.0, 0.0],
        "qqq_return": [0.0, 0.0],
        "drawdown": [0.0, 0.0],
    }, index=index)
    summary = MODULE.summarize(result)
    assert summary["largest_daily_return_date"] == "2024-01-02"
    assert summary["wins_vs_qqq_after_largest_day_neutralized"] == 1
    assert summary["wins_vs_qqq_after_paired_market_day_neutralized"] == 1
