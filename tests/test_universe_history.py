import pandas as pd

from src.research.universe_history import (
    known_non_common_symbols,
    load_security_master,
    load_universe_snapshots,
    snapshot_coverage,
    universe_as_of,
)


def test_universe_snapshot_is_only_available_on_or_after_its_date(tmp_path):
    pd.DataFrame({
        "Symbol": ["A", "B-W"],
        "Name": ["A Common Stock", "B Warrant"],
    }).to_csv(tmp_path / "nasdaq_300M_2025-04-14.csv", index=False)
    snapshots = load_universe_snapshots(tmp_path)
    assert universe_as_of(snapshots, pd.Timestamp("2025-04-13")) is None
    assert universe_as_of(snapshots, pd.Timestamp("2025-04-14")) == {"A"}


def test_snapshot_coverage_never_claims_earlier_history(tmp_path):
    pd.DataFrame({"Symbol": ["A"], "Name": ["A Common Stock"]}).to_csv(
        tmp_path / "nasdaq_300M_2025-04-14.csv", index=False
    )
    snapshots = load_universe_snapshots(tmp_path)
    report = snapshot_coverage(snapshots, "2021-01-01", "2026-07-17")
    assert report["earliest_snapshot"] == "2025-04-14"
    assert report["maximum_snapshot_gap_days"] > 40
    assert not report["full_period_covered"]


def test_snapshot_coverage_allows_latest_snapshot_to_remain_effective(tmp_path):
    for observed_at in ("2025-06-01", "2025-07-01"):
        pd.DataFrame({"Symbol": ["A"], "Name": ["A Common Stock"]}).to_csv(
            tmp_path / f"nasdaq_listed_{observed_at}.csv", index=False
        )
    report = snapshot_coverage(
        load_universe_snapshots(tmp_path), "2025-06-01", "2025-07-17"
    )
    assert report["full_period_covered"]


def test_security_master_uses_latest_observed_security_type(tmp_path):
    pd.DataFrame({
        "Symbol": ["A", "W"],
        "Name": ["A Warrant", "W Warrant"],
    }).to_csv(tmp_path / "nasdaq_300M_2025-04-14.csv", index=False)
    pd.DataFrame({
        "Symbol": ["A", "W"],
        "Name": ["A Common Stock", "W Warrant"],
    }).to_csv(tmp_path / "nasdaq_300M_2025-05-19.csv", index=False)
    master = load_security_master(tmp_path).set_index("Symbol")
    assert bool(master.loc["A", "is_common_equity"])
    assert known_non_common_symbols(tmp_path) == {"W"}


def test_future_security_type_does_not_rewrite_an_older_snapshot(tmp_path):
    pd.DataFrame({
        "Symbol": ["A"], "Name": ["A Common Stock"],
    }).to_csv(tmp_path / "nasdaq_listed_2025-04-01.csv", index=False)
    pd.DataFrame({
        "Symbol": ["A"], "Name": ["A Warrant"],
    }).to_csv(tmp_path / "nasdaq_listed_2025-05-01.csv", index=False)

    snapshots = load_universe_snapshots(tmp_path)

    assert snapshots[pd.Timestamp("2025-04-01")] == {"A"}
    assert snapshots[pd.Timestamp("2025-05-01")] == set()


def test_full_listed_snapshot_is_preferred_on_the_same_date(tmp_path):
    pd.DataFrame({
        "Symbol": ["A"], "Name": ["A Common Stock"],
    }).to_csv(tmp_path / "nasdaq_300M_2025-06-01.csv", index=False)
    pd.DataFrame({
        "Symbol": ["A", "B", "F"],
        "Name": ["A Common Stock", "B Common Stock", "F ETF"],
    }).to_csv(tmp_path / "nasdaq_listed_2025-06-01.csv", index=False)
    snapshots = load_universe_snapshots(tmp_path)
    assert snapshots[pd.Timestamp("2025-06-01")] == {"A", "B"}
