import pandas as pd

from scripts.research_v14_qdel_historical_companyfacts import (
    _earliest_paired_quarters,
)


def test_earliest_paired_quarters_does_not_backdate_later_comparative():
    rows = pd.DataFrame([
        {"ticker": "QDEL", "fiscal_end": "2019-03-31", "available_date": "2019-05-09", "metric": "revenue", "value": 10.0},
        {"ticker": "QDEL", "fiscal_end": "2019-03-31", "available_date": "2019-05-09", "metric": "net_income", "value": 2.0},
        {"ticker": "QDEL", "fiscal_end": "2019-03-31", "available_date": "2020-05-07", "metric": "revenue", "value": 10.0},
        {"ticker": "QDEL", "fiscal_end": "2019-03-31", "available_date": "2020-05-07", "metric": "net_income", "value": 2.0},
    ])
    selected = _earliest_paired_quarters(rows)
    assert len(selected) == 2
    assert set(selected["available_date"]) == {pd.Timestamp("2019-05-09")}


def test_earliest_pair_requires_both_metrics_on_same_availability_date():
    rows = pd.DataFrame([
        {"ticker": "QDEL", "fiscal_end": "2019-03-31", "available_date": "2019-05-09", "metric": "revenue", "value": 10.0},
        {"ticker": "QDEL", "fiscal_end": "2019-03-31", "available_date": "2019-08-09", "metric": "revenue", "value": 10.0},
        {"ticker": "QDEL", "fiscal_end": "2019-03-31", "available_date": "2019-08-09", "metric": "net_income", "value": 2.0},
    ])
    selected = _earliest_paired_quarters(rows)
    assert len(selected) == 2
    assert set(selected["available_date"]) == {pd.Timestamp("2019-08-09")}
