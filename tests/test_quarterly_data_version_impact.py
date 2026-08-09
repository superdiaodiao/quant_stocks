import pandas as pd

from src.research.quarterly_data_version_impact import (
    changed_target_signals,
    quarterly_inventory_diff,
)


def test_quarterly_inventory_diff_ignores_fetch_time_but_counts_new_key():
    reference = pd.DataFrame({
        "ticker": ["AAA"],
        "fiscal_end": pd.to_datetime(["2024-03-31"]),
        "available_date": pd.to_datetime(["2024-05-01"]),
        "metric": ["revenue"],
        "value": [100.0],
        "taxonomy": ["us-gaap"],
        "concept": ["Revenue"],
        "form": ["10-Q"],
        "accession": ["old"],
        "fetched_at": ["2026-07-01"],
    })
    candidate = pd.concat([
        reference.assign(fetched_at="2026-07-31"),
        reference.assign(
            fiscal_end=pd.Timestamp("2024-06-30"),
            available_date=pd.Timestamp("2024-08-01"),
            accession="new",
        ),
    ], ignore_index=True)

    result = quarterly_inventory_diff(reference, candidate)

    assert result["candidate_only_semantic_fact_rows"] == 1
    assert result["reference_only_semantic_fact_rows"] == 0
    assert result["candidate_only_fact_keys"] == 1
    assert result["reference_only_fact_keys"] == 0


def test_changed_target_signals_carries_unchanged_positions_forward():
    reference = pd.DataFrame({
        "signal_date": ["2024-01-31", "2024-02-29"],
        "ticker": ["AAA", "BBB"],
        "target_weight_after": [1.0, 1.0],
    })
    candidate = pd.DataFrame({
        "signal_date": ["2024-01-31", "2024-02-29"],
        "ticker": ["AAA", "AAA"],
        "target_weight_after": [1.0, 0.0],
    })

    result = changed_target_signals(reference, candidate)

    assert result.to_dict("records") == [{
        "signal_date": "2024-02-29",
        "reference_tickers": "AAA|BBB",
        "candidate_tickers": "",
        "removed_tickers": "AAA|BBB",
        "added_tickers": "",
    }]
