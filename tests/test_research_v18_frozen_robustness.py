import pandas as pd

from scripts.research_v18_frozen_robustness import evaluate_frozen_gates


def _gates():
    return {
        "post_development_nasdaq_annual_win_count": {
            f"{cost}_bps": {"required": 2, "total_years": 2}
            for cost in (10, 30, 50)
        },
        "full_history_nasdaq_annual_win_count": {
            f"{cost}_bps": {"required": 5, "total_years": 5}
            for cost in (10, 30, 50)
        },
        "full_history_qqq_annual_win_count": {
            f"{cost}_bps": {"required": 3, "total_years": 5}
            for cost in (10, 30, 50)
        },
        "post_development_compounded_excess": {"threshold": 0.0},
        "full_history_compounded_excess": {"threshold": 0.0},
        "drawdown": {
            "maximum_loss_fraction": 0.40,
            "maximum_underperformance_vs_nasdaq_percentage_points": 5.0,
        },
        "leave_one_satellite_out": {"compounded_excess_threshold": 0.0},
    }


def test_v18_gate_evaluator_checks_nasdaq_qqq_and_satellite_removal():
    dates = pd.to_datetime([
        "2022-12-30", "2023-12-29", "2024-12-31",
        "2025-12-31", "2026-07-17",
    ])
    results = {
        cost: pd.DataFrame({
            "strategy": [0.10] * 5,
            "benchmark": [0.05] * 5,
        }, index=dates)
        for cost in (10, 30, 50)
    }
    qqq = pd.Series([0.04] * 5, index=dates)
    removed = pd.DataFrame({
        "strategy": [0.08] * 5,
        "benchmark": [0.05] * 5,
    }, index=dates)
    contributions = pd.DataFrame({
        "ticker": ["__QQQ_CORE__", "BIG", "SMALL"],
        "gross_return_contribution": [0.50, 0.10, 0.02],
        "transaction_cost_contribution": [0.01, 0.01, 0.00],
        "net_return_contribution": [0.49, 0.09, 0.02],
    })

    report = evaluate_frozen_gates(
        results=results,
        qqq_return=qqq,
        removed_daily=removed,
        contributions=contributions,
        gates=_gates(),
    )

    assert report["all_predeclared_gates_passed"] is True
    assert report["costs"]["10"]["full_history_nasdaq_annual_win_count"] == 5
    assert report["costs"]["10"]["full_history_qqq_annual_win_count"] == 5
    assert report["leave_one_satellite_out"]["removed_ticker"] == "BIG"


def test_v18_gate_fails_if_one_nasdaq_year_is_not_won():
    dates = pd.to_datetime([
        "2022-12-30", "2023-12-29", "2024-12-31",
        "2025-12-31", "2026-07-17",
    ])
    results = {
        cost: pd.DataFrame({
            "strategy": [0.10, 0.10, 0.10, 0.04, 0.10],
            "benchmark": [0.05] * 5,
        }, index=dates)
        for cost in (10, 30, 50)
    }
    qqq = pd.Series([0.03] * 5, index=dates)
    contributions = pd.DataFrame({
        "ticker": ["__QQQ_CORE__", "BIG"],
        "gross_return_contribution": [0.50, 0.10],
        "transaction_cost_contribution": [0.01, 0.01],
        "net_return_contribution": [0.49, 0.09],
    })

    report = evaluate_frozen_gates(
        results=results,
        qqq_return=qqq,
        removed_daily=results[10],
        contributions=contributions,
        gates=_gates(),
    )

    assert report["all_predeclared_gates_passed"] is False
    assert report["costs"]["10"]["full_history_nasdaq_annual_win_count"] == 4
