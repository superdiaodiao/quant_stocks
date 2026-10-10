import pandas as pd
import pytest

from scripts import research_v17_stock_turnover_cap_development as v17
from src.research.can_slim import replay_can_slim_target_schedule


def _schedule(dates):
    return pd.DataFrame({
        "effective_date": [dates[0], dates[1], dates[2]],
        "ticker": ["A", "B", "__CASH__"],
        "target_weight": [1.0, 1.0, 0.0],
        "base_transaction_cost_bps": [0.0, 0.0, 0.0],
    })


def test_cap_applies_only_to_stock_to_stock_transition():
    dates = pd.bdate_range("2022-01-03", periods=3)
    close = pd.DataFrame({
        "A": [100.0, 100.0, 100.0],
        "B": [100.0, 100.0, 100.0],
    }, index=dates)
    index = pd.Series([1000.0, 1000.0, 1000.0], index=dates)

    result, _ = v17.replay_stock_turnover_cap(
        close,
        index,
        _schedule(dates),
        dates[0],
        dates[-1],
        stock_to_stock_turnover_cap=0.5,
        adjust_splits=False,
    )

    assert result.loc[dates[0], "turnover"] == pytest.approx(1.0)
    assert result.loc[dates[1], "turnover"] == pytest.approx(0.5)
    assert result.loc[dates[1], "holdings"] == 2
    assert result.loc[dates[2], "turnover"] == pytest.approx(1.0)
    assert result.loc[dates[2], "invested"] == pytest.approx(0.0)


def test_large_cap_matches_exact_frozen_target_replay():
    dates = pd.bdate_range("2022-01-03", periods=4)
    close = pd.DataFrame({
        "A": [100.0, 101.0, 102.0, 103.0],
        "B": [100.0, 99.0, 98.0, 97.0],
    }, index=dates)
    index = pd.Series([1000.0, 1010.0, 1000.0, 1020.0], index=dates)
    schedule = _schedule(dates)
    schedule["base_transaction_cost_bps"] = 10.0

    expected, expected_contributions = replay_can_slim_target_schedule(
        close,
        index,
        schedule,
        dates[0],
        dates[-1],
        adjust_splits=False,
    )
    actual, actual_contributions = v17.replay_stock_turnover_cap(
        close,
        index,
        schedule,
        dates[0],
        dates[-1],
        stock_to_stock_turnover_cap=3.0,
        adjust_splits=False,
    )

    pd.testing.assert_frame_equal(actual, expected)
    pd.testing.assert_frame_equal(actual_contributions, expected_contributions)


def test_candidate_selection_requires_all_gates_and_uses_fixed_score():
    def summary(passed, minimum, compounded, reduction):
        return {
            "all_development_gates_passed": passed,
            "turnover_reduction_fraction_10bps": reduction,
            "performance": {"costs": {"10": {
                "annual": [
                    {"excess_vs_nasdaq": minimum},
                    {"excess_vs_nasdaq": minimum + 0.1},
                ],
                "compounded_excess": compounded,
            }}},
        }

    selected = v17.select_candidate({
        0.5: summary(False, 0.5, 1.0, 0.5),
        1.0: summary(True, -0.02, 0.4, 0.3),
        1.5: summary(True, 0.01, 0.3, 0.2),
    })

    assert selected == 1.5
    assert v17.select_candidate({
        0.5: summary(False, 1.0, 1.0, 0.9)
    }) is None
