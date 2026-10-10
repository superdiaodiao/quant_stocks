from __future__ import annotations

import pandas as pd

from scripts.research_v6_walkforward_defensive_ensemble import (
    combine_sleeves,
    prior_month_allocations,
    simulate_sleeve,
)


def test_allocation_uses_prior_completed_month() -> None:
    dates = pd.date_range("2024-01-02", "2024-03-29", freq="B")
    stock = pd.Series(0.01, index=dates)
    qqq = pd.Series(100.0, index=dates)
    allocation = prior_month_allocations(
        stock, qqq, relative_strength_window=5, trend_window=5, stock_weight=0.25
    )

    assert allocation.loc["2024-01", "risk_on"].eq(False).all()
    assert allocation.loc["2024-02", "risk_on"].eq(True).all()


def test_weekly_allocation_changes_only_after_prior_week() -> None:
    dates = pd.date_range("2024-01-02", "2024-01-19", freq="B")
    stock = pd.Series(0.02, index=dates)
    qqq = pd.Series(100.0, index=dates)
    allocation = prior_month_allocations(
        stock, qqq, relative_strength_window=2, trend_window=2,
        stock_weight=0.25, cadence="weekly",
    )

    assert allocation.iloc[:4]["risk_on"].eq(False).all()
    assert allocation.iloc[4:]["risk_on"].eq(True).all()


def test_weekly_confirmation_requires_two_completed_signals() -> None:
    dates = pd.date_range("2024-01-02", "2024-01-26", freq="B")
    stock = pd.Series(0.02, index=dates)
    qqq = pd.Series(100.0, index=dates)
    allocation = prior_month_allocations(
        stock, qqq, relative_strength_window=2, trend_window=2,
        stock_weight=0.25, cadence="weekly", confirmation_periods=2,
    )

    assert allocation.iloc[:9]["risk_on"].eq(False).all()
    assert allocation.iloc[9:]["risk_on"].eq(True).all()


def test_simulation_keeps_stock_weight_and_costs_monthly_switch() -> None:
    dates = pd.date_range("2024-01-02", periods=45, freq="B")
    base = pd.DataFrame({
        "strategy": 0.0,
        "benchmark": 0.0,
        "turnover": 0.0,
    }, index=dates)
    close = pd.Series(100.0, index=dates)
    dividend = pd.Series(0.0, index=dates)

    result = simulate_sleeve(
        base, close, dividend,
        relative_strength_window=5,
        trend_window=5,
        stock_weight=0.25,
        transaction_cost_bps=30.0,
    )

    assert result["nav"].iloc[0] < 1.0
    assert result["sleeve_transaction_cost"].gt(0.0).any()
    assert result["stock_weight"].eq(0.25).all()


def test_ensemble_is_equal_weighted_across_sleeves() -> None:
    dates = pd.date_range("2024-01-02", periods=2, freq="B")
    left = pd.DataFrame({
        "return": [0.0, 0.02], "benchmark_return": [0.0, 0.01],
        "qqq_return": [0.0, 0.015],
        "risk_on": [True, True],
    }, index=dates)
    right = pd.DataFrame({
        "return": [0.0, 0.00], "benchmark_return": [0.0, 0.01],
        "qqq_return": [0.0, 0.015],
        "risk_on": [False, False],
    }, index=dates)

    result = combine_sleeves({42: left, 45: right})

    assert result["return"].iloc[-1] == 0.01
    assert result["risk_on_sleeves"].tolist() == [1, 1]
