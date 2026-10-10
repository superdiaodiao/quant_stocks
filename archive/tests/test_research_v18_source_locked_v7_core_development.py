import pandas as pd
import pytest

from scripts import research_v18_source_locked_v7_core_development as v18


def test_source_locked_targets_preserve_stocks_at_20_percent_and_qqq_at_80():
    targets = pd.DataFrame({
        "effective_date": pd.to_datetime([
            "2023-01-03", "2023-02-01", "2023-02-01", "2025-01-02"
        ]),
        "ticker": ["__CASH__", "A", "B", "FUTURE"],
        "target_weight": [0.0, 0.25, 0.75, 1.0],
        "base_transaction_cost_bps": [10.0] * 4,
    })

    result = v18.source_locked_core_satellite_targets(
        targets, end="2024-12-31"
    )

    january = result.loc[
        result["effective_date"].eq(pd.Timestamp("2023-01-03"))
    ]
    february = result.loc[
        result["effective_date"].eq(pd.Timestamp("2023-02-01"))
    ]
    assert january.set_index("ticker")["target_weight"].to_dict() == {
        v18.CORE_TICKER: 0.8,
    }
    assert february.set_index("ticker")["target_weight"].to_dict() == {
        "A": 0.05,
        "B": pytest.approx(0.15),
        v18.CORE_TICKER: 0.8,
    }
    assert "FUTURE" not in set(result["ticker"])


def test_development_summary_requires_positive_2023_excess():
    dates = pd.to_datetime([
        "2022-12-30", "2023-12-29", "2024-12-31"
    ])
    result = pd.DataFrame({
        "strategy": [0.10, 0.04, 0.10],
        "benchmark": [0.05, 0.05, 0.05],
    }, index=dates)
    v14 = pd.DataFrame({
        "strategy": [0.10, -0.10, 0.10],
        "benchmark": [0.05, 0.05, 0.05],
    }, index=dates)
    gates = {
        **v18.DEVELOPMENT_GATES,
        "annual_win_count": {
            str(cost): {"required": 2, "years": 3}
            for cost in (10, 30, 50)
        },
        "motivating_2023_excess_improvement_pp": 5.0,
    }

    summary = v18.summarize_development(
        {10: result, 30: result, 50: result},
        v14,
        result["benchmark"],
        gates,
    )

    assert summary["source_locked_architecture_gate"]["gate_passed"] is False
    assert summary["all_development_gates_passed"] is False
