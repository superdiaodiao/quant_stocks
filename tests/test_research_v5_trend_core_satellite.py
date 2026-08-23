from __future__ import annotations

import pandas as pd
import pytest

from scripts.research_v5_trend_core_satellite import simulate_candidate


def test_candidate_uses_only_prior_completed_month_signals() -> None:
    index = pd.bdate_range("2025-01-01", periods=260)
    v4 = pd.DataFrame({
        "strategy": 0.0,
        "benchmark": 0.0,
        "turnover": 0.0,
    }, index=index)
    core = pd.Series(range(1, len(index) + 1), index=index, dtype=float)

    result = simulate_candidate(v4, core, transaction_cost_bps=30.0)

    assert result["nav"].gt(0).all()
    first_eligible_month = result.loc[result["core_weight"].gt(0)].index.min()
    crossing_month = (core > core.rolling(200, min_periods=200).mean()).loc[
        lambda values: values
    ].index.min()
    assert first_eligible_month.to_period("M") >= crossing_month.to_period("M") + 1


def test_candidate_rejects_cost_below_embedded_v4_cost() -> None:
    with pytest.raises(ValueError, match="at least 10"):
        simulate_candidate(
            pd.DataFrame(), pd.Series(dtype=float), transaction_cost_bps=5.0
        )


def test_candidate_rejects_missing_core_price_on_nonzero_session() -> None:
    index = pd.bdate_range("2025-01-01", periods=220)
    v4 = pd.DataFrame({
        "strategy": 0.0,
        "benchmark": 0.0,
        "turnover": 0.0,
    }, index=index)
    v4.loc[index[-1], "strategy"] = 0.01
    core = pd.Series(range(1, len(index)), index=index[:-1], dtype=float)

    with pytest.raises(ValueError, match="missing a non-zero"):
        simulate_candidate(v4, core, transaction_cost_bps=30.0)


def test_monthly_rebalance_occurs_after_current_close_return() -> None:
    index = pd.bdate_range("2025-01-01", periods=260)
    v4 = pd.DataFrame({
        "strategy": 0.0,
        "benchmark": 0.0,
        "turnover": 0.0,
    }, index=index)
    boundary = next(
        stamp for prior, stamp in zip(index, index[1:])
        if stamp.month != prior.month and stamp > index[210]
    )
    v4.loc[boundary, "strategy"] = 0.10
    core = pd.Series(100.0, index=index)

    result = simulate_candidate(v4, core, transaction_cost_bps=10.0)
    prior = result.loc[:boundary].iloc[-2]
    current = result.loc[boundary]
    pre_trade_nav = current["nav"] + current["sleeve_transaction_cost"]
    expected = (
        prior["satellite_value"] * 1.10
        + prior["core_value"]
        + prior["cash"]
    )

    assert pre_trade_nav == pytest.approx(expected)
