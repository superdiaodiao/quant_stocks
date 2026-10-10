import pandas as pd

from scripts.research_v14_frozen_replay import evaluate_frozen_gates


def _gates() -> dict:
    return {
        "annual_excess_win_count": {
            "10_bps": {"required": 4, "total_years": 5},
            "30_bps": {"required": 3, "total_years": 5},
            "50_bps": {"required": 3, "total_years": 5},
        },
        "compounded_excess": {"threshold": 0.0},
        "drawdown": {
            "maximum_loss_fraction": 0.40,
            "maximum_underperformance_vs_nasdaq_percentage_points": 5.0,
        },
        "leave_one_out": {"compounded_excess_threshold": 0.0},
    }


def test_gate_evaluator_uses_predeclared_cost_drawdown_and_removal_rules():
    cost_rows = []
    for cost in (10, 30, 50):
        for year in range(2022, 2027):
            cost_rows.append({
                "cost_bps": cost,
                "test_year": year,
                "strategy": 0.10,
                "nasdaq": 0.05,
                "excess_vs_nasdaq": 0.05,
            })
    dates = pd.date_range("2022-01-03", periods=3, freq="D")
    daily = pd.DataFrame({
        "strategy": [0.02, -0.10, 0.12],
        "benchmark": [0.01, -0.08, 0.09],
    }, index=dates)
    removed = pd.DataFrame({
        "strategy": [0.01, -0.05, 0.08],
        "benchmark": daily["benchmark"],
    }, index=dates)
    contributions = pd.DataFrame({
        "ticker": ["BIG", "SMALL"],
        "gross_return_contribution": [0.10, 0.02],
        "transaction_cost_contribution": [0.01, 0.00],
        "net_return_contribution": [0.09, 0.02],
    })

    result = evaluate_frozen_gates(
        cost_stress=pd.DataFrame(cost_rows),
        daily=daily,
        removed_daily=removed,
        contributions=contributions,
        gates=_gates(),
    )

    assert result["all_predeclared_gates_passed"] is True
    assert result["annual_and_compounded"]["10"]["annual_win_count"] == 5
    assert result["drawdown"]["absolute_gate_passed"] is True
    assert result["leave_one_out"]["removed_ticker"] == "BIG"
    assert result["leave_one_out"]["removed_weight_behavior"] == (
        "leave as cash; do not renormalize"
    )
