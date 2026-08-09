import json

import pandas as pd

from src.research.universe_snapshot_provenance import (
    audit_snapshot_provenance,
)


def _snapshot(path, source_file=None, repository=None, commit=None):
    frame = pd.DataFrame({
        "Symbol": ["A", "B"],
        "Name": ["A Common Stock", "B Common Stock"],
    })
    if source_file is not None:
        frame["Source File"] = source_file
    if repository is not None:
        frame["Source Repository"] = repository
    if commit is not None:
        frame["Source Commit"] = commit
    frame.to_csv(path, index=False)


def test_snapshot_provenance_resolves_manifest_and_ranks_signal_use(tmp_path):
    commit = "a" * 40
    _snapshot(tmp_path / "nasdaq_listed_2025-01-01.csv")
    _snapshot(
        tmp_path / "nasdaq_listed_2025-02-01.csv",
        "https://raw.githubusercontent.com/example/repo/HEAD/list.txt",
    )
    (tmp_path / "listings_git_import_manifest.json").write_text(
        json.dumps({
            "source_repository": "https://github.com/datasets/listings",
            "imported": [{
                "commit": commit,
                "observed_at": "2025-01-01",
                "source_path": "data/listed.csv",
            }],
        }),
        encoding="utf-8",
    )

    details, report = audit_snapshot_provenance(
        tmp_path,
        [
            pd.Timestamp("2025-01-31"),
            pd.Timestamp("2025-02-28"),
        ],
    )

    indexed = details.set_index("observed_at")
    january = indexed.loc[pd.Timestamp("2025-01-01")]
    february = indexed.loc[pd.Timestamp("2025-02-01")]
    assert january["source_status"] == "IMMUTABLE_GIT_COMMIT"
    assert january["signals_using_snapshot"] == 1
    assert february["source_status"] == "FLOATING_GIT_REF"
    assert february["signals_using_snapshot"] == 1
    assert report["signal_source_status_counts"] == {
        "FLOATING_GIT_REF": 1,
        "IMMUTABLE_GIT_COMMIT": 1,
    }
    assert report["nonreproducible_signal_dates"] == ["2025-02-28"]
    assert not report["all_signal_snapshots_reproducible"]


def test_common_crawl_range_is_reproducible_without_git_commit(tmp_path):
    _snapshot(
        tmp_path / "nasdaq_listed_2020-01-01.csv",
        "https://data.commoncrawl.org/crawl-data/example.warc.gz"
        "#offset=10&length=20&timestamp=20200101120000",
    )

    details, report = audit_snapshot_provenance(
        tmp_path, pd.DatetimeIndex(["2020-01-31"])
    )

    assert details.iloc[0]["source_status"] == "IMMUTABLE_WEB_ARCHIVE"
    assert report["all_signal_snapshots_reproducible"]


def test_inconsistent_row_provenance_is_never_accepted(tmp_path):
    frame = pd.DataFrame({
        "Symbol": ["A", "B"],
        "Name": ["A Common Stock", "B Common Stock"],
        "Source Repository": [
            "https://github.com/example/one",
            "https://github.com/example/two",
        ],
        "Source Commit": ["a" * 40, "a" * 40],
    })
    frame.to_csv(
        tmp_path / "nasdaq_listed_2025-01-01.csv", index=False
    )

    details, _ = audit_snapshot_provenance(tmp_path, [])

    assert details.iloc[0]["source_status"] == "INCONSISTENT_METADATA"
    assert not details.iloc[0]["source_reproducible"]


def test_immutable_raw_github_url_recovers_missing_metadata(tmp_path):
    commit = "b" * 40
    _snapshot(
        tmp_path / "nasdaq_listed_2023-08-25.csv",
        f"https://raw.githubusercontent.com/example/listings/{commit}/"
        "nasdaqlisted.txt",
    )

    details, report = audit_snapshot_provenance(
        tmp_path, [pd.Timestamp("2023-08-31")]
    )

    row = details.iloc[0]
    assert row["source_status"] == "IMMUTABLE_GIT_COMMIT"
    assert row["source_repository"] == (
        "https://github.com/example/listings"
    )
    assert row["source_commit"] == commit
    assert report["all_signal_snapshots_reproducible"]
