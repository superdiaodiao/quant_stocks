import pandas as pd
import pytest

from src.research.can_slim_robustness import (
    SCENARIOS,
    annual_scenario_metrics,
    financial_freshness_summary,
    realized_concentration,
    tail_dependency_diagnostics,
)


def test_robustness_scenarios_do_not_expand_the_parameter_grid():
    assert {row["scenario"] for row in SCENARIOS} == {
        "baseline",
        "snapshot_max_40d",
        "snapshot_max_30d",
        "financial_max_200d",
        "financial_max_150d",
        "financial_max_120d",
        "no_split_adjustment_stress",
        "confirmed_selected_actions_only",
        "confirmed_all_actions_only",
        "confirmed_all_with_contemporaneous_price_filter",
        "confirmed_all_plus_failed_heuristic_stress",
        "combined_40d_snapshot_150d_financial",
    }


def test_annual_scenario_metrics_compounds_and_measures_drawdown():
    index = pd.to_datetime(["2021-01-04", "2021-01-05"])
    result = pd.DataFrame({
        "strategy": [0.10, -0.20],
        "benchmark": [0.05, 0.00],
        "invested": [1.0, 1.0],
        "holdings": [3, 3],
        "turnover": [1.0, 0.0],
    }, index=index)

    annual = annual_scenario_metrics(result)

    assert annual.loc[0, "strategy"] == pytest.approx(-0.12)
    assert annual.loc[0, "nasdaq"] == pytest.approx(0.05)
    assert annual.loc[0, "maximum_drawdown"] == pytest.approx(-0.20)


def test_realized_concentration_is_explicitly_ledger_based():
    ledger = pd.DataFrame({
        "execution_date": ["2024-01-02", "2024-02-01"],
        "ticker": ["A", "B"],
        "realized_pnl": [75.0, 25.0],
    })

    result = realized_concentration(ledger)

    assert result.loc[0, "top_ticker"] == "A"
    assert result.loc[0, "top1_share_of_net_realized_pnl"] == 0.75


def test_tail_dependency_removes_same_dates_from_both_series():
    index = pd.to_datetime([
        "2020-01-02",
        "2024-01-02",
        "2024-02-01",
        "2025-01-02",
        "2025-02-03",
    ])
    result = pd.DataFrame({
        "strategy": [5.0, 0.50, 0.0, 0.10, 0.02],
        "benchmark": [0.0, 0.01, 0.0, 0.01, 0.0],
    }, index=index)

    details, summary = tail_dependency_diagnostics(result)

    year_2024 = details.set_index("year").loc[2024]
    assert year_2024["best_strategy_day"] == "2024-01-02"
    assert year_2024["nasdaq_return_without_same_day"] == pytest.approx(0.0)
    assert year_2024["outperformance_depends_on_best_day"]
    assert year_2024["outperformance_depends_on_best_month"]
    assert summary["original_wins_vs_nasdaq"] == 2
    assert summary["wins_without_each_year_best_day"] == 1
    assert summary["wins_without_each_year_best_month"] == 1
    assert summary["years_dependent_on_best_day"] == [2024]
    assert summary["years_dependent_on_best_month"] == [2024]
    assert details["year"].tolist() == [2024, 2025]


def test_financial_freshness_summary_counts_changed_executed_signals():
    details = pd.DataFrame({
        "signal_date": ["2024-01-31", "2024-02-29", "2024-03-28"],
        "baseline_quarterly_financial_ages": [
            "30|121|201",
            "60|90|150",
            "",
        ],
        "raw_top3_changed": [True, True, False],
        "executed_top3_changed": [True, False, False],
    })

    summary = financial_freshness_summary(details)

    assert summary["signal_count"] == 3
    assert summary["selected_position_observations"] == 6
    assert summary["maximum_selected_quarterly_financial_age_days"] == 201
    assert summary["selected_positions_older_than_120d"] == 3
    assert summary["selected_positions_older_than_150d"] == 1
    assert summary["selected_positions_older_than_200d"] == 1
    assert summary["raw_top3_changed_signals"] == 2
    assert summary["executed_top3_changed_signals"] == 1
    assert summary["executed_changed_signal_dates"] == ["2024-01-31"]
