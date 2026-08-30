from pathlib import Path

import pandas as pd

from scripts import research_v29_recovered_2019_stock_momentum as v29


def test_recovered_dates_are_exact_and_source_bound():
    paths = v29.recovered_snapshot_paths()

    assert tuple(stamp.strftime("%Y-%m-%d") for stamp in paths) == (
        "2019-02-22",
        "2019-07-15",
        "2019-10-02",
        "2019-12-31",
    )
    assert all(path.exists() for path in paths.values())
    assert all(path.parent == v29.RECOVERED_SNAPSHOT_DIRECTORY for path in paths.values())
    assert all(Path(path).exists() for path in v29.RECOVERY_MANIFESTS)


def test_coverage_improves_to_eight_of_twelve_without_stale_extension():
    coverage = v29.coverage_adjudication()

    assert coverage["maximum_snapshot_age_days"] == 40
    assert coverage["usable_signal_count"] == 8
    assert coverage["expected_signal_count"] == 12
    assert coverage["missing_signal_dates"] == [
        "2019-04-30",
        "2019-05-31",
        "2019-08-30",
        "2019-09-30",
    ]
    assert coverage["missing_universe_policy"] == (
        "CASH_NO_BACKFILL_NO_STALE_EXTENSION"
    )


def test_repaired_loader_adds_only_predeclared_dates(monkeypatch):
    formal = {pd.Timestamp("2019-01-09"): {"A"}}
    recovered = {
        pd.Timestamp(date): {f"R{index}"}
        for index, date in enumerate(v29.RECOVERED_2019_DATES)
    }
    recovered[pd.Timestamp("2019-03-01")] = {"UNDECLARED"}

    def fake_loader(directory=None, carry_forward_confirmed_types=True):
        return dict(formal if directory is None else recovered)

    monkeypatch.setattr(v29, "load_universe_snapshots", fake_loader)
    actual = v29.load_repaired_universe_snapshots()

    assert actual[pd.Timestamp("2019-01-09")] == {"A"}
    assert pd.Timestamp("2019-03-01") not in actual
    for index, date in enumerate(v29.RECOVERED_2019_DATES):
        assert actual[pd.Timestamp(date)] == {f"R{index}"}


def test_candidate_grid_is_unchanged_from_v26():
    assert v29.candidate_specs() == v29.v26.candidate_specs()
    assert len(v29.candidate_specs()) == 18
    assert v29.OBSERVATION_START == v29.v26.OBSERVATION_START
    assert "QQQ" in v29.FORBIDDEN_ETFS
