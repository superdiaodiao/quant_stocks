from __future__ import annotations

import pandas as pd

from scripts.research_v2_evaluation import evaluate


def test_evaluate_requires_oos_cost_and_selected_data_gates() -> None:
    walk = pd.DataFrame({"excess_vs_nasdaq": [0.1, 0.2, -0.1, 0.3, 0.4]})
    cost = pd.DataFrame(
        {
            "cost_bps": [30.0] * 5,
            "excess_vs_nasdaq": [0.08, 0.1, -0.2, 0.2, 0.3],
        }
    )
    audit = {
        "positions_with_missing_holding_prices": 0,
        "positions_with_unresolved_terminal_return": 0,
    }
    result = evaluate(walk, cost, audit)
    assert result["oos_wins_vs_nasdaq"] == 4
    assert result["wins_at_30_bps"] == 4
    assert result["historical_candidate_passed"] is True


def test_evaluate_rejects_three_of_five_even_with_complete_data() -> None:
    walk = pd.DataFrame({"excess_vs_nasdaq": [0.1, -0.2, 0.3, -0.1, 0.4]})
    cost = pd.DataFrame(
        {"cost_bps": [30.0] * 5, "excess_vs_nasdaq": walk["excess_vs_nasdaq"]}
    )
    audit = {
        "positions_with_missing_holding_prices": 0,
        "positions_with_unresolved_terminal_return": 0,
    }
    result = evaluate(walk, cost, audit)
    assert result["checks"]["minimum_oos_wins"] is False
    assert result["historical_candidate_passed"] is False
