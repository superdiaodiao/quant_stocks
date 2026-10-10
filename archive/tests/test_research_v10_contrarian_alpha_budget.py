import numpy as np
import pandas as pd
import pytest

from scripts.research_v10_contrarian_alpha_budget import simulate_contrarian_core_satellite


def test_contrarian_decision_uses_only_returns_before_rebalance():
    dates = pd.bdate_range("2024-01-02", periods=85)
    stock = pd.DataFrame({
        "strategy": np.r_[np.full(64, 0.01), -0.99, np.zeros(20)],
        "benchmark": np.zeros(85),
        "turnover": np.zeros(85),
    }, index=dates)
    qqq = pd.Series(0.0, index=dates)
    _, decisions = simulate_contrarian_core_satellite(
        stock, qqq, lookback=5, transaction_cost_bps=0.0,
    )
    later = decisions.loc[decisions["trailing_sessions"].eq(5)].iloc[0]
    decision_position = dates.get_loc(pd.Timestamp(later["date"]))
    expected = np.prod(1 + stock["strategy"].iloc[:decision_position].tail(5)) - 1
    assert later["trailing_relative_return"] == pytest.approx(expected)


def test_positive_trailing_alpha_reduces_stock_weight():
    dates = pd.bdate_range("2024-01-02", periods=70)
    stock = pd.DataFrame({
        "strategy": np.full(70, 0.01),
        "benchmark": np.zeros(70),
        "turnover": np.zeros(70),
    }, index=dates)
    _, decisions = simulate_contrarian_core_satellite(
        stock, pd.Series(0.0, index=dates), lookback=5,
        transaction_cost_bps=0.0,
    )
    mature = decisions.loc[decisions["trailing_sessions"].eq(5)]
    assert mature["crowded"].all()
    assert (mature["stock_weight"] == 0.20).all()
