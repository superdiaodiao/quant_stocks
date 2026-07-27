import subprocess
import gzip

import pandas as pd

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
    result = nasdaq_update.import_nasdaq_listings_history(repository, minimum_rows=2)
    assert len(result["imported"]) == 1
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
    repaired = pd.read_csv(prices / "abc.csv")
    assert repaired["date"].tolist() == ["2025-01-02", "2025-01-03", "2025-01-06"]
    assert repaired.loc[repaired["date"] == "2025-01-02", "close"].item() == 99.0
    assert repaired.loc[repaired["date"] == "2025-01-03", "close"].item() == 11.0
