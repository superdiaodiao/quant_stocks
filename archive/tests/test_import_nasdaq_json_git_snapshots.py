import json
import subprocess
from pathlib import Path

import pandas as pd

from scripts.import_nasdaq_json_git_snapshots import import_snapshots


def _git(repository: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=repository, check=True, capture_output=True)


def test_selected_git_snapshot_is_reproducible_and_dry_run_by_default(tmp_path):
    repository = tmp_path / "source"
    repository.mkdir()
    _git(repository, "init")
    _git(repository, "config", "user.email", "test@example.com")
    _git(repository, "config", "user.name", "Test")
    _git(repository, "remote", "add", "origin", "https://example.test/source.git")
    source = repository / "nasdaq"
    source.mkdir()
    rows = [{"symbol": f"T{i:04d}", "name": f"Company {i}"} for i in range(1001)]
    source.joinpath("nasdaq_full_tickers.json").write_text(json.dumps(rows))
    _git(repository, "add", ".")
    subprocess.run(
        ["git", "commit", "-m", "snapshot"], cwd=repository, check=True,
        capture_output=True,
        env={"GIT_AUTHOR_DATE": "2023-05-19T12:00:00Z", "GIT_COMMITTER_DATE": "2023-05-19T12:00:00Z"},
    )
    snapshots = tmp_path / "snapshots"
    output = tmp_path / "report.json"

    dry_run = import_snapshots(
        repository=repository, dates=["2023-05-20"],
        snapshot_dir=snapshots, output=output,
    )
    assert dry_run["records"][0]["status"] == "DRY_RUN_ELIGIBLE"
    assert not snapshots.exists()

    applied = import_snapshots(
        repository=repository, dates=["2023-05-20"],
        snapshot_dir=snapshots, output=output, apply=True,
    )
    record = applied["records"][0]
    assert record["observed_at"] == "2023-05-19"
    assert record["rows"] == 1001
    assert record["source_payload_sha256"]
    frame = pd.read_csv(snapshots / "nasdaq_listed_2023-05-19.csv")
    assert frame.columns.tolist() == [
        "Symbol", "Name", "Source Repository", "Source Commit",
        "Source File", "Observed At",
    ]
    assert frame["Source Commit"].nunique() == 1
