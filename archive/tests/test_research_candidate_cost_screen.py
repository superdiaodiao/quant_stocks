from __future__ import annotations

import pandas as pd
import pytest

from scripts.research_candidate_cost_screen import summarize_screen


def _rows(config_id: int, cost: int, excess: list[float]) -> list[dict]:
    return [
        {
            "config_id": config_id,
            "cost_bps": cost,
            "year": year,
            "excess_vs_nasdaq": value,
        }
        for year, value in zip(range(2022, 2027), excess, strict=True)
    ]


def test_summarize_screen_requires_robustness_at_every_cost() -> None:
    rows = []
    rows.extend(_rows(0, 10, [0.1, 0.1, 0.1, 0.1, -0.1]))
    rows.extend(_rows(0, 30, [0.1, 0.1, 0.1, 0.1, -0.1]))
    rows.extend(_rows(0, 50, [0.1, 0.1, 0.1, -0.1, -0.1]))
    rows.extend(_rows(1, 10, [0.1, 0.1, 0.1, 0.1, -0.1]))
    rows.extend(_rows(1, 30, [0.1, 0.1, 0.1, 0.1, -0.1]))
    rows.extend(_rows(1, 50, [0.1, 0.1, 0.1, 0.1, -0.1]))

    summaries = summarize_screen(pd.DataFrame(rows))

    assert summaries[0]["passed_all_costs"] is False
    assert summaries[0]["costs"]["50"]["wins"] == 3
    assert summaries[1]["passed_all_costs"] is True


def test_summarize_screen_rejects_incomplete_years() -> None:
    frame = pd.DataFrame(_rows(0, 10, [0.1, 0.1, 0.1, 0.1, -0.1])[:-1])

    with pytest.raises(ValueError, match="expected"):
        summarize_screen(frame)
