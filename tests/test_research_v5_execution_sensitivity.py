from __future__ import annotations

import pandas as pd
import pytest

from scripts.research_v5_execution_sensitivity import (
    simulate_continuous_whole_share,
    summarize_whole_share_rounding,
)


def test_whole_share_rounding_flags_unbuyable_targets() -> None:
    targets = pd.DataFrame({
        "effective_date": pd.to_datetime(["2026-01-02"] * 2),
        "ticker": ["HIGH", "LOW"],
        "sleeve": ["v4", "v4"],
        "target_weight": [0.05, 0.05],
        "execution_close": [600.0, 20.0],
    })

    result = summarize_whole_share_rounding(targets, 10_000.0)

    assert result["zero_share_stock_targets"] == 1
    assert result["periods_with_unbuyable_stock"] == 1
    assert result["maximum_period_rounding_cash_drag_fraction"] == pytest.approx(0.05)


def test_whole_share_rounding_rejects_invalid_account_size() -> None:
    with pytest.raises(ValueError, match="finite and positive"):
        summarize_whole_share_rounding(pd.DataFrame(), 0.0)


def test_continuous_whole_share_replay_marks_positions_and_cash() -> None:
    index = pd.to_datetime(["2026-01-02", "2026-01-05", "2026-02-02"])
    targets = pd.DataFrame({
        "effective_date": pd.to_datetime(["2026-01-02", "2026-02-02"]),
        "ticker": ["A", "A"],
        "target_weight": [0.5, 0.0],
    })
    stock_close = pd.DataFrame({"A": [100.0, 110.0, 120.0]}, index=index)
    qqq = pd.Series([200.0, 202.0, 204.0], index=index)

    result = simulate_continuous_whole_share(
        targets,
        stock_close,
        qqq,
        pd.Series(0.0, index=index),
        pd.Series(0.0, index=index),
        account_size=1_000.0,
        transaction_cost_bps=0.0,
    )

    assert result.loc[index[0], "holdings"] == 1
    assert result.loc[index[1], "nav"] == pytest.approx(1_050.0)
    assert result.loc[index[-1], "holdings"] == 0
    assert result.loc[index[-1], "nav"] == pytest.approx(1_100.0)


def test_partial_fill_retries_remaining_shares_next_session() -> None:
    index = pd.to_datetime(["2026-01-02", "2026-01-05", "2026-01-06"])
    targets = pd.DataFrame({
        "effective_date": [index[0]],
        "ticker": ["A"],
        "target_weight": [1.0],
    })
    prices = pd.DataFrame({"A": 100.0}, index=index)

    result = simulate_continuous_whole_share(
        targets,
        prices,
        pd.Series(200.0, index=index),
        pd.Series(0.0, index=index),
        pd.Series(0.0, index=index),
        account_size=1_000.0,
        transaction_cost_bps=0.0,
        fill_fraction=0.75,
    )

    assert result["requested_share_delta"].tolist() == [10, 2, 0]
    assert result["filled_share_delta"].tolist() == [8, 2, 0]
    assert result.loc[index[1], "invested_fraction"] == pytest.approx(1.0)
