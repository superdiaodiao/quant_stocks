from pathlib import Path

import pandas as pd

from src.io import nasdaq_update
from src.research.universe_history import load_universe_snapshots


def test_import_nasdaq_trader_file_uses_embedded_creation_date(tmp_path, monkeypatch):
    source = tmp_path / "nasdaqlisted.txt"
    source.write_text(
        "Symbol|Security Name|Market Category|Test Issue|Financial Status|Round Lot Size|ETF|NextShares\n"
        "A|A Common Stock|Q|N|N|100|N|N\n"
        "F|F Fund|G|N|N|100|Y|N\n"
        "File Creation Time: 0205202121:32|||||||\n",
        encoding="utf-8",
    )
    destination = tmp_path / "universe" / "snapshots"
    monkeypatch.setattr(
        nasdaq_update, "NASDAQ_300M_STOCK_LIST_FILE", destination.parent / "nasdaq_300M.csv"
    )
    result = nasdaq_update.import_nasdaq_trader_files([source], minimum_rows=2)
    assert result["imported"][0]["observed_at"] == "2021-02-05"
    snapshots = load_universe_snapshots(destination)
    assert snapshots[next(iter(snapshots))] == {"A"}


def test_import_nasdaq_trader_file_accepts_isolated_snapshot_dir(tmp_path):
    source = tmp_path / "nasdaqlisted.txt"
    source.write_text(
        "Symbol|Security Name|Test Issue|ETF|NextShares\n"
        "A|A Common Stock|N|N|N\n"
        "File Creation Time: 0205202121:32||||\n",
        encoding="utf-8",
    )
    destination = tmp_path / "isolated"
    result = nasdaq_update.import_nasdaq_trader_files(
        [source], minimum_rows=1, snapshot_dir=destination
    )
    assert result["imported"][0]["snapshot"] == str(
        destination / "nasdaq_listed_2021-02-05.csv"
    )
    assert load_universe_snapshots(destination)[pd.Timestamp("2021-02-05")] == {"A"}
