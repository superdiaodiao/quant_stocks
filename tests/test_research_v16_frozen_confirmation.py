import pandas as pd

from scripts.research_v16_frozen_confirmation import evaluate_frozen_gates


def _gates() -> dict:
    return {
        "confirmation_annual_excess_win_count": {
            "10_bps": {"required": 2, "total_years": 2},
            "30_bps": {"required": 1, "total_years": 2},
            "50_bps": {"required": 1, "total_years": 2},
        },
        "confirmation_compounded_excess": {"threshold": 0.0},
        "full_history_annual_excess_win_count": {
            "10_bps": {"required": 4, "total_years": 5},
            "30_bps": {"required": 3, "total_years": 5},
            "50_bps": {"required": 3, "total_years": 5},
        },
        "full_history_compounded_excess": {"threshold": 0.0},
        "drawdown": {
            "maximum_loss_fraction": 0.40,
            "maximum_underperformance_vs_nasdaq_percentage_points": 5.0,
        },
        "leave_one_out": {"compounded_excess_threshold": 0.0},
    }


def test_confirmation_gate_evaluator_checks_confirmation_and_full_history():
    dates = pd.to_datetime([
        "2022-12-30", "2023-12-29", "2024-12-31",
        "2025-12-31", "2026-07-17",
    ])
    results = {}
    for cost in (10, 30, 50):
        results[cost] = pd.DataFrame({
            "strategy": [0.10] * 5,
            "benchmark": [0.05] * 5,
        }, index=dates)
    removed = pd.DataFrame({
        "strategy": [0.08] * 5,
        "benchmark": [0.05] * 5,
    }, index=dates)
    contributions = pd.DataFrame({
        "ticker": ["BIG", "SMALL"],
        "gross_return_contribution": [0.10, 0.02],
        "transaction_cost_contribution": [0.01, 0.00],
        "net_return_contribution": [0.09, 0.02],
    })

    report = evaluate_frozen_gates(
        results=results,
        removed_daily=removed,
        contributions=contributions,
        gates=_gates(),
    )

    assert report["all_predeclared_gates_passed"] is True
    assert report["costs"]["10"]["confirmation_annual_win_count"] == 2
    assert report["costs"]["10"]["full_history_annual_win_count"] == 5
    assert report["leave_one_out"]["removed_ticker"] == "BIG"


def test_confirmation_gate_fails_when_10bps_does_not_win_both_years():
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
    removed = results[10].copy()
    contributions = pd.DataFrame({
        "ticker": ["BIG"],
        "gross_return_contribution": [0.10],
        "transaction_cost_contribution": [0.01],
        "net_return_contribution": [0.09],
    })

    report = evaluate_frozen_gates(
        results=results,
        removed_daily=removed,
        contributions=contributions,
        gates=_gates(),
    )

    assert report["all_predeclared_gates_passed"] is False
    assert report["costs"]["10"]["confirmation_annual_win_gate_passed"] is False
