import pandas as pd

from src.research.can_slim_temporal_validation import (
    _comparison_rows,
    PARTIAL_2019_SEGMENTS,
)
from src.research.universe_history import universe_as_of


def test_universe_resolver_rejects_stale_snapshot_when_requested():
    snapshots = {pd.Timestamp("2020-01-01"): {"A"}}

    assert universe_as_of(
        snapshots, pd.Timestamp("2020-02-10"), maximum_age_days=40
    ) == {"A"}
    assert universe_as_of(
        snapshots, pd.Timestamp("2020-02-11"), maximum_age_days=40
    ) is None
    assert universe_as_of(snapshots, pd.Timestamp("2020-02-11")) == {"A"}


def test_partial_2019_segments_are_disjoint_and_resettable():
    assert PARTIAL_2019_SEGMENTS == (
        ("2019-01", "2019-01-31", "2019-02-28"),
        ("2019-06", "2019-06-28", "2019-07-31"),
        ("2019-11_12", "2019-11-29", "2019-12-31"),
    )


def test_comparison_uses_identical_benchmark_periods():
    index = pd.to_datetime(["2025-01-02", "2025-12-31"])
    challenger = pd.DataFrame(
        {"strategy": [0.10, 0.05], "benchmark": [0.02, 0.03]}, index=index
    )
    fixed = pd.DataFrame(
        {"strategy": [0.08, 0.04], "benchmark": [0.02, 0.03]}, index=index
    )

    rows = _comparison_rows(challenger, fixed, 10.0)

    assert len(rows) == 1
    assert rows[0]["challenger_minus_fixed"] > 0
    assert rows[0]["nasdaq"] == (1.02 * 1.03) - 1
