import pandas as pd

from scripts.research_v14_normalize_exact_quarter_coordinates import (
    exact_coordinate_mappings,
)


def _rows(end: str, available: str, revenue: float, profit: float) -> list[dict]:
    return [
        {"ticker": "JJSF", "fiscal_end": end, "available_date": available,
         "metric": "revenue", "value": revenue},
        {"ticker": "JJSF", "fiscal_end": end, "available_date": available,
         "metric": "net_income", "value": profit},
    ]


def test_exact_duplicate_uses_first_contemporaneous_coordinate() -> None:
    frame = pd.DataFrame([
        *_rows("2020-06-27", "2020-07-31", 214_563_000, -12_647_000),
        *_rows("2020-06-30", "2020-11-25", 214_563_000, -12_647_000),
    ])
    mappings = exact_coordinate_mappings(frame)
    assert mappings == [{
        "ticker": "JJSF",
        "duplicate_fiscal_end": "2020-06-30",
        "canonical_fiscal_end": "2020-06-27",
        "day_gap": 3,
        "revenue": 214_563_000.0,
        "net_income": -12_647_000.0,
        "canonical_first_pair_available": "2020-07-31",
        "duplicate_first_pair_available": "2020-11-25",
    }]


def test_near_dates_with_different_values_are_not_merged() -> None:
    frame = pd.DataFrame([
        *_rows("2020-06-27", "2020-07-31", 214_563_000, -12_647_000),
        *_rows("2020-06-30", "2020-11-25", 214_563_001, -12_647_000),
    ])
    assert exact_coordinate_mappings(frame) == []


def test_one_metric_is_not_enough_to_prove_duplicate_coordinate() -> None:
    frame = pd.DataFrame([
        *_rows("2020-06-27", "2020-07-31", 214_563_000, -12_647_000),
        {"ticker": "JJSF", "fiscal_end": "2020-06-30",
         "available_date": "2020-11-25", "metric": "net_income",
         "value": -12_647_000},
    ])
    assert exact_coordinate_mappings(frame) == []
