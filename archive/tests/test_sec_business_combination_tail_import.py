import gzip
import json
from pathlib import Path
import zipfile

import pandas as pd
import pytest

from scripts.sec_business_combination_tail_import import import_tail


def test_imports_contiguous_cross_cik_tail_with_filing_binding(tmp_path: Path):
    prices = tmp_path / "prices"
    prices.mkdir()
    pd.DataFrame(
        [{"date": "2025-04-10", "ticker": "OLD", "open": 10, "high": 11,
          "low": 9, "close": 10, "volume": 100}]
    ).to_csv(prices / "old.csv", index=False)
    archive_path = tmp_path / "stooq.zip"
    with zipfile.ZipFile(archive_path, "w") as archive:
        archive.writestr(
            "data/daily/us/nasdaq stocks/new.us.txt",
            "<TICKER>,<PER>,<DATE>,<TIME>,<OPEN>,<HIGH>,<LOW>,<CLOSE>,<VOL>,<OPENINT>\n"
            "NEW.US,D,20250409,000000,18,19,17,18,100,0\n"
            "NEW.US,D,20250411,000000,20,22,18,21,200,0\n",
        )
    payload = b"OLD converted one share; NEW began trading on 2025-04-11"
    sec_cache = tmp_path / "filing.json.gz"
    with gzip.open(sec_cache, "wt", encoding="utf-8") as handle:
        json.dump({
            "payload_hex": payload.hex(),
            "source_url": "https://www.sec.gov/example",
        }, handle)
    audit = tmp_path / "audit.json"
    audit.write_text(json.dumps({"unresolved_terminal_return_histories": [
        {"ticker": "OLD", "last_price_date": "2025-04-10"}
    ]}))
    output = tmp_path / "evidence.json"

    dry_run = import_tail(
        historical_ticker="OLD", successor_ticker="NEW",
        effective_date="2025-04-09", source_start_date="2025-04-11",
        exchange_ratio=0.5,
        sec_cache_path=sec_cache,
        expected_filing_phrases=["converted one share", "NEW began trading"],
        archive_path=archive_path,
        archive_member="data/daily/us/nasdaq stocks/new.us.txt",
        audit_path=audit, output=output, price_dir=prices,
        end="2025-04-11",
    )
    assert dry_run["status"] == "DRY_RUN_ELIGIBLE"
    assert len(pd.read_csv(prices / "old.csv")) == 1

    applied = import_tail(
        historical_ticker="OLD", successor_ticker="NEW",
        effective_date="2025-04-09", source_start_date="2025-04-11",
        exchange_ratio=0.5,
        sec_cache_path=sec_cache,
        expected_filing_phrases=["converted one share", "NEW began trading"],
        archive_path=archive_path,
        archive_member="data/daily/us/nasdaq stocks/new.us.txt",
        audit_path=audit, output=output, price_dir=prices,
        end="2025-04-11", apply=True,
    )
    persisted = pd.read_csv(prices / "old.csv")
    assert applied["status"] == "UPDATED"
    assert persisted.iloc[-1]["close"] == 10.5
    assert persisted.iloc[-1]["volume"] == 400


def test_pre_effective_renamed_archive_requires_and_records_overlap(tmp_path: Path):
    prices = tmp_path / "prices"
    prices.mkdir()
    local_rows = []
    archive_rows = [
        "<TICKER>,<PER>,<DATE>,<TIME>,<OPEN>,<HIGH>,<LOW>,<CLOSE>,<VOL>,<OPENINT>"
    ]
    dates = pd.bdate_range("2025-03-03", periods=23)
    for index, day in enumerate(dates):
        value = 10 + index / 100
        local_rows.append({
            "date": day.strftime("%Y-%m-%d"), "ticker": "OLD",
            "open": value, "high": value + 0.1, "low": value - 0.1,
            "close": value + 0.05, "volume": 100 + index,
        })
        archive_rows.append(
            f"NEW.US,D,{day:%Y%m%d},000000,{value},{value + 0.1},"
            f"{value - 0.1},{value + 0.05},{100 + index},0"
        )
    archive_rows.extend([
        "NEW.US,D,20250403,000000,11,11.1,10.9,11.05,200,0",
        "NEW.US,D,20250404,000000,12,12.1,11.9,12.05,300,0",
    ])
    pd.DataFrame(local_rows).to_csv(prices / "old.csv", index=False)
    archive_path = tmp_path / "stooq.zip"
    with zipfile.ZipFile(archive_path, "w") as archive:
        archive.writestr(
            "data/daily/us/nasdaq stocks/new.us.txt",
            "\n".join(archive_rows) + "\n",
        )
    payload = b"OLD converted one share; NEW began trading"
    sec_cache = tmp_path / "filing.json.gz"
    with gzip.open(sec_cache, "wt", encoding="utf-8") as handle:
        json.dump({"payload_hex": payload.hex(), "source_url": "https://sec"}, handle)
    audit = tmp_path / "audit.json"
    audit.write_text(json.dumps({"unresolved_terminal_return_histories": [
        {"ticker": "OLD", "last_price_date": dates[-1].strftime("%Y-%m-%d")}
    ]}))
    kwargs = dict(
        historical_ticker="OLD", successor_ticker="NEW",
        effective_date="2025-04-04", source_start_date="2025-04-03",
        exchange_ratio=1.0, sec_cache_path=sec_cache,
        expected_filing_phrases=["converted one share", "NEW began trading"],
        archive_path=archive_path,
        archive_member="data/daily/us/nasdaq stocks/new.us.txt",
        audit_path=audit, output=tmp_path / "evidence.json", price_dir=prices,
        end="2025-04-04",
    )
    with pytest.raises(ValueError, match="source start cannot precede"):
        import_tail(**kwargs)
    report = import_tail(**kwargs, allow_pre_effective_source_overlap=True)
    assert report["pre_effective_source_overlap"]["passed"] is True
    assert report["rows_added"] == 2
