import subprocess
import gzip
import json
from datetime import date

import pandas as pd
import pytest

from src.io import nasdaq_update
from src.research.universe_history import load_universe_snapshots


def test_import_nasdaq_listings_history_uses_commit_date_and_filters_non_stocks(
    tmp_path, monkeypatch
):
    repository = tmp_path / "source"
    repository.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repository, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=repository, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=repository, check=True)
    subprocess.run(
        [
            "git",
            "remote",
            "add",
            "origin",
            "https://github.com/example/nasdaq-listings.git",
        ],
        cwd=repository,
        check=True,
    )
    data = repository / "data"
    data.mkdir()
    pd.DataFrame({
        "Symbol": ["A", "B", "F", "W"],
        "Security Name": ["A Common Stock", "B Common Stock", "F ETF", "W Warrant"],
    }).to_csv(data / "nasdaq-listed.csv", index=False)
    subprocess.run(["git", "add", "data/nasdaq-listed.csv"], cwd=repository, check=True)
    subprocess.run(
        ["git", "commit", "-q", "-m", "snapshot"],
        cwd=repository,
        check=True,
        env={"GIT_AUTHOR_DATE": "2025-06-01T00:00:00Z", "GIT_COMMITTER_DATE": "2025-06-01T00:00:00Z"},
    )
    destination = tmp_path / "snapshots"
    monkeypatch.setattr(nasdaq_update, "NASDAQ_300M_STOCK_LIST_FILE", destination.parent / "nasdaq_300M.csv")
    monkeypatch.setattr(nasdaq_update, "PROJECT_PATH", tmp_path)
    result = nasdaq_update.import_nasdaq_listings_history(repository, minimum_rows=2)
    assert len(result["imported"]) == 1
    assert result["source_repository"] == (
        "https://github.com/example/nasdaq-listings.git"
    )
    assert result["imported"][0]["snapshot"] == (
        "snapshots/nasdaq_listed_2025-06-01.csv"
    )
    assert result["manifest"] == (
        "snapshots/listings_git_import_manifest.json"
    )
    snapshot = pd.read_csv(
        destination / "nasdaq_listed_2025-06-01.csv"
    )
    assert snapshot["Source Repository"].unique().tolist() == [
        "https://github.com/example/nasdaq-listings.git"
    ]
    assert snapshot["Source File"].unique().tolist() == [
        "data/nasdaq-listed.csv"
    ]
    snapshots = load_universe_snapshots(destination)
    assert snapshots[pd.Timestamp("2025-06-01")] == {"A", "B"}


def test_import_nasdaq_trader_git_history_uses_each_commit_date(tmp_path, monkeypatch):
    repository = tmp_path / "source"
    source = repository / "data"
    source.mkdir(parents=True)
    subprocess.run(["git", "init", "-q"], cwd=repository, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=repository, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=repository, check=True)
    for observed_at, symbols in (("2022-01-10", ["A", "B"]), ("2022-02-10", ["A", "C"])):
        rows = "\n".join(f"{symbol}|{symbol} Common Stock|N|N|N|" for symbol in symbols)
        source.joinpath("nasdaqlisted.txt").write_text(
            "Symbol|Security Name|Test Issue|ETF|NextShares|\n" + rows + "\n",
            encoding="utf-8",
        )
        subprocess.run(["git", "add", "data/nasdaqlisted.txt"], cwd=repository, check=True)
        subprocess.run(
            ["git", "commit", "-q", "-m", observed_at], cwd=repository, check=True,
            env={"GIT_AUTHOR_DATE": f"{observed_at}T00:00:00Z", "GIT_COMMITTER_DATE": f"{observed_at}T00:00:00Z"},
        )
    destination = tmp_path / "snapshots"
    monkeypatch.setattr(nasdaq_update, "NASDAQ_300M_STOCK_LIST_FILE", destination.parent / "nasdaq_300M.csv")
    result = nasdaq_update.import_nasdaq_trader_git_history(repository, minimum_rows=2)
    assert [row["observed_at"] for row in result["imported"]] == ["2022-01-10", "2022-02-10"]
    snapshots = load_universe_snapshots(destination)
    assert snapshots[pd.Timestamp("2022-01-10")] == {"A", "B"}
    assert snapshots[pd.Timestamp("2022-02-10")] == {"A", "C"}


def test_common_crawl_range_import_preserves_archive_provenance(
    tmp_path, monkeypatch
):
    text = (
        "WARC/1.0\r\n\r\nHTTP/1.1 200 OK\r\n\r\n"
        "Symbol|Security Name|Test Issue|ETF|NextShares|\n"
        "A|A Common Stock|N|N|N|\n"
        "B|B Common Stock|N|N|N|\n"
        "File Creation Time: 0614202121:31||||\n"
    )
    payload = gzip.compress(text.encode())
    requests = []

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return None

        def read(self):
            return payload

    def fake_urlopen(request, timeout):
        requests.append(request)
        return Response()

    destination = tmp_path / "snapshots"
    monkeypatch.setattr(
        nasdaq_update,
        "NASDAQ_300M_STOCK_LIST_FILE",
        destination.parent / "nasdaq_300M.csv",
    )
    monkeypatch.setattr(nasdaq_update, "urlopen", fake_urlopen)
    source = (
        "https://data.commoncrawl.org/crawl-data/example.warc.gz"
        "#offset=100&length=200&timestamp=20210614232702"
    )

    result = nasdaq_update.import_nasdaq_trader_files(
        [source], minimum_rows=2
    )

    assert requests[0].headers["Range"] == "bytes=100-299"
    assert result["imported"][0]["observed_at"] == "2021-06-14"
    snapshot = pd.read_csv(destination / "nasdaq_listed_2021-06-14.csv")
    assert snapshot["Source File"].unique().tolist() == [source]


def test_local_stooq_import_fills_gaps_without_replacing_existing_prices(tmp_path, monkeypatch):
    repository = tmp_path / "stooq"
    repository.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repository, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=repository, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=repository, check=True)
    source = repository / "daily/us/nasdaq stocks/1"
    source.mkdir(parents=True)
    source.joinpath("abc.us.txt").write_text(
        "<TICKER>,<PER>,<DATE>,<TIME>,<OPEN>,<HIGH>,<LOW>,<CLOSE>,<VOL>,<OPENINT>\n"
        "ABC,D,20250102,000000,10,11,9,10,100,0\n"
        "ABC,D,20250103,000000,11,12,10,11,100,0\n"
        "ABC,D,20250106,000000,12,13,11,12,100,0\n",
        encoding="utf-8",
    )
    subprocess.run(["git", "add", "."], cwd=repository, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "prices"], cwd=repository, check=True)
    prices = tmp_path / "prices"
    prices.mkdir()
    pd.DataFrame({
        "date": ["2025-01-02", "2025-01-06"], "ticker": ["ABC", "ABC"],
        "open": [99.0, 12.0], "high": [99.0, 13.0], "low": [99.0, 11.0],
        "close": [99.0, 12.0], "volume": [1.0, 100.0],
    }).to_csv(prices / "abc.csv", index=False)
    monkeypatch.setattr(nasdaq_update, "CLEANED_PRICE_DATA_DIR", prices)
    monkeypatch.setattr(nasdaq_update, "PROJECT_PATH", tmp_path)

    report = nasdaq_update.import_stooq_git_mirror(repository, "HEAD", ["ABC"])

    assert report["counts"] == {"updated": 1}
    assert len(report["runs"]) == 1
    repaired = pd.read_csv(prices / "abc.csv")
    assert repaired["date"].tolist() == ["2025-01-02", "2025-01-03", "2025-01-06"]
    assert repaired.loc[repaired["date"] == "2025-01-02", "close"].item() == 99.0
    assert repaired.loc[repaired["date"] == "2025-01-03", "close"].item() == 11.0

    second = nasdaq_update.import_stooq_git_mirror(repository, "HEAD", ["ABC"])
    assert second["counts"] == {"no_missing_rows": 1}
    assert len(second["runs"]) == 2


def test_local_stooq_import_reports_empty_source_without_failure(tmp_path, monkeypatch):
    repository = tmp_path / "stooq"
    repository.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repository, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=repository, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=repository, check=True)
    source = repository / "daily/us/nasdaq stocks/1"
    source.mkdir(parents=True)
    source.joinpath("empty.us.txt").write_text("", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=repository, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "empty"], cwd=repository, check=True)
    prices = tmp_path / "prices"
    prices.mkdir()
    monkeypatch.setattr(nasdaq_update, "CLEANED_PRICE_DATA_DIR", prices)
    monkeypatch.setattr(nasdaq_update, "PROJECT_PATH", tmp_path)

    report = nasdaq_update.import_stooq_git_mirror(repository, "HEAD", ["EMPTY"])

    assert report["counts"] == {"empty_source": 1}
    assert report["results"] == [{
        "ticker": "EMPTY",
        "status": "empty_source",
        "rows": 0,
        "source_path": "daily/us/nasdaq stocks/1/empty.us.txt",
    }]


def test_github_stooq_import_uses_verified_path_and_fills_file_head(
    tmp_path, monkeypatch
):
    payload = (
        "<TICKER>,<PER>,<DATE>,<TIME>,<OPEN>,<HIGH>,<LOW>,<CLOSE>,<VOL>,<OPENINT>\n"
        "ABC.US,D,20250102,000000,10,11,9,10,100,0\n"
        "ABC.US,D,20250103,000000,49,50,48,49.5,2,0\n"
    ).encode()
    requests = []

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return None

        def read(self):
            return payload

    def fake_urlopen(request, timeout):
        requests.append(request.full_url)
        return Response()

    prices = tmp_path / "prices"
    prices.mkdir()
    pd.DataFrame({
        "date": ["2025-01-03"],
        "ticker": ["ABC"],
        "open": [99.0],
        "high": [99.0],
        "low": [99.0],
        "close": [99.0],
        "volume": [1.0],
    }).to_csv(prices / "abc.csv", index=False)
    monkeypatch.setattr(nasdaq_update, "urlopen", fake_urlopen)
    monkeypatch.setattr(
        nasdaq_update, "CLEANED_PRICE_DATA_DIR", prices
    )
    monkeypatch.setattr(nasdaq_update, "PROJECT_PATH", tmp_path)

    report = nasdaq_update.import_stooq_github_mirror(
        "owner/repository",
        "fixed-commit",
        ["ABC"],
        workers=1,
        source_paths={"ABC": "daily/us/abc.us.txt"},
        adjustment_factors={
            "ABC": {
                "price_factor": 2.0,
                "volume_factor": 0.5,
                "minimum_overlap_sessions": 1,
                "source_url": "https://example.test/split",
                "note": "2-for-1 normalization",
            }
        },
    )

    repaired = pd.read_csv(prices / "abc.csv")
    assert report["path_discovery"] == "verified_source_paths"
    assert len(requests) == 1
    assert "git/trees" not in requests[0]
    assert repaired["date"].tolist() == [
        "2025-01-02", "2025-01-03"
    ]
    assert repaired.loc[
        repaired["date"] == "2025-01-03", "close"
    ].item() == 99.0
    assert repaired.loc[
        repaired["date"] == "2025-01-02", "close"
    ].item() == 20.0
    assert repaired.loc[
        repaired["date"] == "2025-01-02", "volume"
    ].item() == 50.0
    assert report["results"][0]["price_factor"] == 2.0
    assert report["results"][0]["adjustment_source_url"] == (
        "https://example.test/split"
    )
    assert report["results"][0]["adjustment_validation"][
        "status"
    ] == "VERIFIED"


def test_github_stooq_import_reports_empty_source_without_failure(
    tmp_path, monkeypatch
):
    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return None

        def read(self):
            return b""

    monkeypatch.setattr(nasdaq_update, "urlopen", lambda request, timeout: Response())
    prices = tmp_path / "prices"
    prices.mkdir()
    monkeypatch.setattr(nasdaq_update, "CLEANED_PRICE_DATA_DIR", prices)
    monkeypatch.setattr(nasdaq_update, "PROJECT_PATH", tmp_path)

    report = nasdaq_update.import_stooq_github_mirror(
        "owner/repository",
        "fixed-commit",
        ["EMPTY"],
        workers=1,
        source_paths={"EMPTY": "daily/us/empty.us.txt"},
    )

    assert report["counts"] == {"empty_source": 1}
    assert report["results"][0]["status"] == "empty_source"


def test_price_adjustment_overlap_rejects_wrong_factor(tmp_path):
    target = tmp_path / "abc.csv"
    pd.DataFrame({
        "date": ["2025-01-02", "2025-01-03"],
        "close": [20.0, 40.0],
        "volume": [50.0, 100.0],
    }).to_csv(target, index=False)
    incoming = pd.DataFrame({
        "date": pd.to_datetime(["2025-01-02", "2025-01-03"]),
        "close": [10.0, 20.0],
        "volume": [100.0, 200.0],
    })

    with pytest.raises(
        ValueError, match="do not match stable overlap"
    ):
        nasdaq_update._validate_price_adjustment_overlap(
            target,
            incoming,
            price_factor=3.0,
            volume_factor=0.5,
            minimum_sessions=2,
            tolerance=0.01,
        )


def test_official_backfill_scales_only_missing_dates_and_records_provenance(
    tmp_path, monkeypatch
):
    prices = tmp_path / "prices"
    prices.mkdir()
    pd.DataFrame({
        "date": ["2025-01-03"],
        "ticker": ["ABC"],
        "open": [99.0],
        "high": [99.0],
        "low": [99.0],
        "close": [99.0],
        "volume": [1.0],
    }).to_csv(prices / "abc.csv", index=False)
    incoming = pd.DataFrame({
        "date": pd.to_datetime(["2025-01-02", "2025-01-03"]),
        "open": [250.0, 275.0],
        "high": [275.0, 300.0],
        "low": [225.0, 250.0],
        "close": [250.0, 275.0],
        "volume": [4.0, 8.0],
    })
    monkeypatch.setattr(
        nasdaq_update, "fetch_history", lambda *_args, **_kwargs: incoming
    )
    monkeypatch.setattr(nasdaq_update, "CLEANED_PRICE_DATA_DIR", prices)
    monkeypatch.setattr(nasdaq_update, "PROJECT_PATH", tmp_path)

    report = nasdaq_update.backfill_official_history(
        "ABC",
        date(2025, 1, 2),
        date(2025, 1, 3),
        price_factor=0.04,
        volume_factor=25.0,
        source_note="confirmed 1-for-25 reverse split",
        source_url="https://www.sec.gov/example",
    )

    repaired = pd.read_csv(prices / "abc.csv")
    assert report["rows_added"] == 1
    assert repaired["date"].tolist() == ["2025-01-02", "2025-01-03"]
    assert repaired.loc[0, "close"] == 10.0
    assert repaired.loc[0, "volume"] == 100.0
    assert repaired.loc[1, "close"] == 99.0
    provenance = json.loads(
        (
            tmp_path
            / "output/data_provenance/nasdaq_official_history_backfill.json"
        ).read_text()
    )
    assert provenance["runs"][0]["source_url"] == "https://www.sec.gov/example"
