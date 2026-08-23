from __future__ import annotations

import pandas as pd

from scripts.research_v14_adaptive_data_audit import (
    _snapshot_binding,
    monthly_decision_sessions,
    snapshot_age,
    training_windows,
)


def test_monthly_decisions_use_last_observed_session() -> None:
    sessions = pd.DatetimeIndex([
        "2020-01-30", "2020-01-31", "2020-02-27", "2020-02-28"
    ])
    result = monthly_decision_sessions(sessions, "2020-01-01", "2020-02-29")
    assert result.tolist() == [pd.Timestamp("2020-01-31"), pd.Timestamp("2020-02-28")]


def test_snapshot_age_never_uses_future_membership() -> None:
    snapshots = {
        pd.Timestamp("2020-01-01"): {"OLD"},
        pd.Timestamp("2020-02-01"): {"NEW"},
    }
    stamp, age, symbols = snapshot_age(snapshots, pd.Timestamp("2020-01-31"))
    assert stamp == pd.Timestamp("2020-01-01")
    assert age == 30
    assert symbols == {"OLD"}


def test_training_windows_are_strictly_prior() -> None:
    result = training_windows(2019)
    assert result["v6_36_month"] == {
        "start": "2016-01-01", "end": "2018-12-31"
    }
    assert result["v7_4_completed_year"] == {
        "start": "2015-01-01", "end": "2018-12-31"
    }


def test_snapshot_binding_hashes_isolated_inputs(tmp_path) -> None:
    snapshot = tmp_path / "nasdaq_listed_2020-01-01.csv"
    snapshot.write_text("Symbol,Name\nA,A Common Stock\n", encoding="utf-8")
    binding = _snapshot_binding(tmp_path)
    assert binding["path"] == str(tmp_path)
    assert binding["files"][0]["path"] == str(snapshot)
    assert len(binding["files"][0]["sha256"]) == 64
