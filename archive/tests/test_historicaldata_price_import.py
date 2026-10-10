from __future__ import annotations

import json
import zipfile
from pathlib import Path

import pandas as pd

from scripts.historicaldata_price_import import (
    _load_missing_price_tickers,
    _load_unresolved_terminal_tickers,
    import_archive,
)


def _write_archive(
    path: Path,
    *,
    mismatch: bool = False,
    volume_mismatch: bool = False,
    split_scale: float | None = None,
) -> None:
    dates = pd.bdate_range("2025-01-02", periods=27)
    values = pd.Series(range(27), dtype=float) + 10
    source = pd.DataFrame(
        {
            "Time": dates.strftime("%Y-%m-%d"),
            "Open": values + (5 if mismatch else 0),
            "Close": values + 0.2,
            "Volume": (1000 + values) * (2 if volume_mismatch else 1),
            "High": values + 0.5,
            "Low": values - 0.5,
            "Average": values,
            "Transactions": 10,
            "AdjOpen": values,
            "AdjClose": values + 0.2,
            "AdjVolume": 1000 + values,
            "AdjHigh": values + 0.5,
            "AdjLow": values - 0.5,
            "AdjAverage": values,
            "Dividend": "",
            "Split": "",
        }
    )
    if split_scale is not None:
        for column in ["Open", "High", "Low", "Close"]:
            source[column] = source[column] / split_scale
        source["Volume"] = source["Volume"] * split_scale
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("delisted_CS_TEST_2025-02-07_day.csv", source.to_csv(index=False))
        reused = source.copy()
        reused["Time"] = pd.bdate_range("2022-01-03", periods=27).strftime("%Y-%m-%d")
        reused["Open"] = reused["Open"] + 100
        reused["High"] = reused["High"] + 100
        reused["Low"] = reused["Low"] + 100
        reused["Close"] = reused["Close"] + 100
        archive.writestr("delisted_CS_TEST_2022-02-08_day.csv", reused.to_csv(index=False))


def _write_local(path: Path) -> None:
    dates = pd.bdate_range("2025-01-02", periods=25)
    values = pd.Series(range(25), dtype=float) + 10
    pd.DataFrame(
        {
            "date": dates.strftime("%Y-%m-%d"),
            "ticker": "TEST",
            "open": values,
            "high": values + 0.5,
            "low": values - 0.5,
            "close": values + 0.2,
            "volume": 1000 + values,
        }
    ).to_csv(path, index=False)


def _write_stooq_archive(path: Path) -> None:
    dates = pd.bdate_range("2025-01-02", periods=27)
    values = pd.Series(range(27), dtype=float) + 10
    source = pd.DataFrame(
        {
            "<TICKER>": "TEST.US",
            "<PER>": "D",
            "<DATE>": dates.strftime("%Y%m%d"),
            "<TIME>": "000000",
            "<OPEN>": values,
            "<HIGH>": values + 0.5,
            "<LOW>": values - 0.5,
            "<CLOSE>": values + 0.2,
            "<VOL>": 1000 + values,
            "<OPENINT>": 0,
        }
    )
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr(
            "data/daily/us/nasdaq stocks/test.us.txt",
            source.to_csv(index=False),
        )


def test_dry_run_then_apply_appends_only_missing_rows(tmp_path: Path) -> None:
    archive = tmp_path / "daily.zip"
    prices = tmp_path / "prices"
    prices.mkdir()
    local_path = prices / "test.csv"
    output = tmp_path / "provenance.json"
    _write_archive(archive)
    _write_local(local_path)
    original_bytes = local_path.read_bytes()

    dry_run = import_archive(archive, ["TEST"], price_dir=prices, output=output)
    assert dry_run["records"][0]["status"] == "DRY_RUN_ELIGIBLE"
    assert dry_run["records"][0]["rows_missing"] == 2
    assert sum(
        item["cross_validation"]["passed"]
        for item in dry_run["records"][0]["member_validations"]
    ) == 1
    assert len(pd.read_csv(local_path)) == 25
    assert local_path.read_bytes() == original_bytes
    assert dry_run["schema_version"] == 3
    assert dry_run["status"] == "COMPLETE"
    assert dry_run["records"][0]["missing_dates"] == ["2025-02-06", "2025-02-07"]
    assert dry_run["records"][0]["member_validations"][0]["member_sha256"]
    assert dry_run["records"][0]["local_sha256_before"]
    assert dry_run["records"][0]["local_sha256_after"] == dry_run["records"][0]["local_sha256_before"]

    applied = import_archive(archive, ["TEST"], price_dir=prices, output=output, apply=True)
    assert applied["records"][0]["status"] == "UPDATED"
    frame = pd.read_csv(local_path)
    assert len(frame) == 27
    assert frame["date"].is_unique
    assert json.loads(output.read_text())["archive_sha256"]
    record = applied["records"][0]
    assert record["local_rows_before"] == 25
    assert record["local_rows_after"] == 27
    assert record["local_sha256_after"] != record["local_sha256_before"]
    assert record["persisted_appended_rows_sha256"] == record["missing_rows_sha256"]


def test_no_new_rows_is_not_reported_as_dry_run_eligible(tmp_path: Path) -> None:
    archive = tmp_path / "daily.zip"
    prices = tmp_path / "prices"
    prices.mkdir()
    local_path = prices / "test.csv"
    output = tmp_path / "provenance.json"
    audit = tmp_path / "historical_data_audit.json"
    _write_archive(archive)
    _write_local(local_path)
    first = import_archive(archive, ["TEST"], price_dir=prices, output=output, apply=True)
    assert first["records"][0]["status"] == "UPDATED"
    audit.write_text(
        json.dumps({"unresolved_terminal_return_histories": [{"ticker": "TEST"}]}),
        encoding="utf-8",
    )

    report = import_archive(
        archive,
        ["TEST"],
        price_dir=prices,
        output=output,
        selection_source_path=audit,
        selection_source_key="unresolved_terminal_return_histories",
    )

    assert report["records"][0]["status"] == "NO_NEW_ROWS"
    assert report["records"][0]["rows_missing"] == 0
    assert report["selection_source_path"] == str(audit)
    assert report["selection_source_sha256"]
    assert report["selection_source_key"] == "unresolved_terminal_return_histories"


def test_rejects_mismatched_overlap(tmp_path: Path) -> None:
    archive = tmp_path / "daily.zip"
    prices = tmp_path / "prices"
    prices.mkdir()
    local_path = prices / "test.csv"
    _write_archive(archive, mismatch=True)
    _write_local(local_path)

    report = import_archive(archive, ["TEST"], price_dir=prices, output=tmp_path / "audit.json", apply=True)
    assert report["records"][0]["status"] == "REJECT_CROSS_VALIDATION"
    assert len(pd.read_csv(local_path)) == 25


def test_rejects_mismatched_volume(tmp_path: Path) -> None:
    archive = tmp_path / "daily.zip"
    prices = tmp_path / "prices"
    prices.mkdir()
    local_path = prices / "test.csv"
    _write_archive(archive, volume_mismatch=True)
    _write_local(local_path)

    report = import_archive(
        archive,
        ["TEST"],
        price_dir=prices,
        output=tmp_path / "audit.json",
        apply=True,
    )
    validation = report["records"][0]["member_validations"][0]["cross_validation"]
    assert validation["fields"]["volume"]["within_5pct"] == 0.0
    assert report["records"][0]["status"] == "REJECT_CROSS_VALIDATION"
    assert len(pd.read_csv(local_path)) == 25


def test_accepts_constant_reciprocal_split_scale_with_evidence(tmp_path: Path) -> None:
    archive = tmp_path / "daily.zip"
    prices = tmp_path / "prices"
    prices.mkdir()
    local_path = prices / "test.csv"
    _write_archive(archive, split_scale=10.0)
    _write_local(local_path)

    report = import_archive(
        archive,
        ["TEST"],
        price_dir=prices,
        output=tmp_path / "audit.json",
        apply=True,
    )
    member = report["records"][0]["member_validations"][0]
    evidence = member["scale_normalization"]
    assert member["raw_cross_validation"]["passed"] is False
    assert member["cross_validation"]["passed"] is True
    assert evidence["method"] == "CONSTANT_SPLIT_SCALE_FROM_OVERLAP"
    assert evidence["price_factor"] == 10.0
    assert evidence["volume_factor"] == 0.1
    assert len(pd.read_csv(local_path)) == 27


def test_auto_detects_and_imports_stooq_daily_archive(tmp_path: Path) -> None:
    archive = tmp_path / "d_us_txt.zip"
    prices = tmp_path / "prices"
    prices.mkdir()
    local_path = prices / "test.csv"
    _write_stooq_archive(archive)
    _write_local(local_path)

    report = import_archive(
        archive,
        ["TEST"],
        price_dir=prices,
        output=tmp_path / "stooq_provenance.json",
        apply=True,
    )

    assert report["source_format"] == "stooq"
    assert report["source_url"] == "https://stooq.com/db/h/"
    assert report["records"][0]["status"] == "UPDATED"
    assert report["records"][0]["members"] == [
        "data/daily/us/nasdaq stocks/test.us.txt"
    ]
    assert len(pd.read_csv(local_path)) == 27


def test_loads_missing_price_tickers_from_historical_audit(tmp_path: Path) -> None:
    audit = tmp_path / "historical_data_audit.json"
    audit.write_text(
        json.dumps(
            {
                "missing_price_while_listed_histories": [
                    {"ticker": "B"},
                    {"ticker": "a"},
                    {"ticker": "B"},
                ]
            }
        )
    )
    assert _load_missing_price_tickers(audit) == ["A", "B"]


def test_loads_unresolved_terminal_tickers_from_historical_audit(
    tmp_path: Path,
) -> None:
    audit = tmp_path / "historical_data_audit.json"
    audit.write_text(
        json.dumps(
            {
                "unresolved_terminal_return_histories": [
                    {"ticker": "ZYXI"},
                    {"ticker": "aadi"},
                    {"ticker": "ZYXI"},
                ]
            }
        )
    )
    assert _load_unresolved_terminal_tickers(audit) == ["AADI", "ZYXI"]
