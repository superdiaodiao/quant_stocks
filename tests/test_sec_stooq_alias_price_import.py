import json
import zipfile

import pandas as pd

from scripts import sec_stooq_alias_price_import as importer


def test_stooq_alias_import_appends_only_validated_membership_tail(tmp_path):
    dates = pd.bdate_range("2025-01-02", periods=35)
    values = pd.DataFrame({
        "date": dates,
        "ticker": "OLD",
        "open": range(100, 135),
        "high": range(101, 136),
        "low": range(99, 134),
        "close": range(100, 135),
        "volume": range(1000, 1035),
    })
    price_dir = tmp_path / "prices"
    price_dir.mkdir()
    values.iloc[:30].to_csv(price_dir / "old.csv", index=False)
    archive_path = tmp_path / "stooq.zip"
    rows = ["<TICKER>,<PER>,<DATE>,<TIME>,<OPEN>,<HIGH>,<LOW>,<CLOSE>,<VOL>,<OPENINT>"]
    for row in values.itertuples(index=False):
        rows.append(
            f"NEW.US,D,{row.date:%Y%m%d},000000,{row.open},{row.high},"
            f"{row.low},{row.close},{row.volume},0"
        )
    with zipfile.ZipFile(archive_path, "w") as archive:
        archive.writestr(
            "data/daily/us/nasdaq stocks/new.us.txt", "\n".join(rows) + "\n"
        )
    probe_path = tmp_path / "probe.json"
    probe_path.write_text(json.dumps({"results": [{
        "ticker": "OLD",
        "status": "ok",
        "search_url": "https://sec.example/search",
        "search_payload_sha256": "search-sha",
        "matches": [{"cik": "0000000123"}],
        "issuers": [{"cik": "0000000123", "current_tickers": ["NEW"]}],
    }]}), encoding="utf-8")
    membership_end = dates[32].strftime("%Y-%m-%d")
    audit_path = tmp_path / "audit.json"
    audit_path.write_text(json.dumps({
        "missing_price_while_listed_histories": [{
            "ticker": "OLD", "last_membership_date": membership_end
        }]
    }), encoding="utf-8")

    report = importer.import_aliases(
        archive_path,
        probe_path=probe_path,
        audit_path=audit_path,
        price_dir=price_dir,
        output=tmp_path / "report.json",
        end="2025-12-31",
        apply=True,
    )

    record = report["records"][0]
    persisted = pd.read_csv(price_dir / "old.csv")
    assert record["status"] == "UPDATED"
    assert record["rows_missing"] == 3
    assert len(persisted) == 33
    assert persisted.iloc[-1]["date"] == membership_end
    assert set(persisted["ticker"]) == {"OLD"}


def test_stooq_alias_import_appends_contiguous_terminal_tail(tmp_path):
    dates = pd.bdate_range("2025-01-02", periods=35)
    values = pd.DataFrame({
        "date": dates,
        "ticker": "OLD",
        "open": range(100, 135),
        "high": range(101, 136),
        "low": range(99, 134),
        "close": range(100, 135),
        "volume": range(1000, 1035),
    })
    price_dir = tmp_path / "prices"
    price_dir.mkdir()
    values.iloc[:30].to_csv(price_dir / "old.csv", index=False)
    archive_path = tmp_path / "stooq.zip"
    rows = ["<TICKER>,<PER>,<DATE>,<TIME>,<OPEN>,<HIGH>,<LOW>,<CLOSE>,<VOL>,<OPENINT>"]
    for row in values.itertuples(index=False):
        rows.append(
            f"NEW.US,D,{row.date:%Y%m%d},000000,{row.open},{row.high},"
            f"{row.low},{row.close},{row.volume},0"
        )
    with zipfile.ZipFile(archive_path, "w") as archive:
        archive.writestr(
            "data/daily/us/nasdaq stocks/new.us.txt", "\n".join(rows) + "\n"
        )
    probe_path = tmp_path / "probe.json"
    probe_path.write_text(json.dumps({"results": [{
        "ticker": "OLD",
        "status": "ok",
        "search_url": "https://sec.example/search",
        "search_payload_sha256": "search-sha",
        "matches": [{"cik": "0000000123"}],
        "issuers": [{"cik": "0000000123", "current_tickers": ["NEW"]}],
    }]}), encoding="utf-8")
    audit_path = tmp_path / "audit.json"
    audit_path.write_text(json.dumps({
        "missing_price_while_listed_histories": [],
        "unresolved_terminal_return_histories": [{
            "ticker": "OLD",
            "last_price_date": dates[29].strftime("%Y-%m-%d"),
            "last_membership_date": dates[20].strftime("%Y-%m-%d"),
        }],
    }), encoding="utf-8")

    report = importer.import_aliases(
        archive_path,
        probe_path=probe_path,
        audit_path=audit_path,
        price_dir=price_dir,
        output=tmp_path / "report.json",
        end="2025-12-31",
        apply=True,
        terminal_tail=True,
    )

    record = report["records"][0]
    persisted = pd.read_csv(price_dir / "old.csv")
    assert report["tail_mode"] == "terminal"
    assert record["status"] == "UPDATED"
    assert record["rows_missing"] == 5
    assert len(persisted) == 35
    assert persisted.iloc[-1]["date"] == dates[-1].strftime("%Y-%m-%d")


def test_stooq_alias_import_rejects_noncontiguous_terminal_tail(tmp_path):
    dates = pd.bdate_range("2025-01-02", periods=30)
    future = pd.bdate_range("2025-03-03", periods=2)
    values = pd.DataFrame({
        "date": dates,
        "ticker": "OLD",
        "open": range(100, 130),
        "high": range(101, 131),
        "low": range(99, 129),
        "close": range(100, 130),
        "volume": range(1000, 1030),
    })
    price_dir = tmp_path / "prices"
    price_dir.mkdir()
    values.to_csv(price_dir / "old.csv", index=False)
    archive_path = tmp_path / "stooq.zip"
    rows = ["<TICKER>,<PER>,<DATE>,<TIME>,<OPEN>,<HIGH>,<LOW>,<CLOSE>,<VOL>,<OPENINT>"]
    source = pd.concat([
        values,
        pd.DataFrame({
            "date": future,
            "ticker": "OLD",
            "open": [130, 131], "high": [131, 132], "low": [129, 130],
            "close": [130, 131], "volume": [1030, 1031],
        }),
    ])
    for row in source.itertuples(index=False):
        rows.append(
            f"NEW.US,D,{row.date:%Y%m%d},000000,{row.open},{row.high},"
            f"{row.low},{row.close},{row.volume},0"
        )
    with zipfile.ZipFile(archive_path, "w") as archive:
        archive.writestr(
            "data/daily/us/nasdaq stocks/new.us.txt", "\n".join(rows) + "\n"
        )
    probe_path = tmp_path / "probe.json"
    probe_path.write_text(json.dumps({"results": [{
        "ticker": "OLD", "status": "ok",
        "matches": [{"cik": "0000000123"}],
        "issuers": [{"cik": "0000000123", "current_tickers": ["NEW"]}],
    }]}), encoding="utf-8")
    audit_path = tmp_path / "audit.json"
    audit_path.write_text(json.dumps({
        "unresolved_terminal_return_histories": [{
            "ticker": "OLD", "last_price_date": dates[-1].strftime("%Y-%m-%d")
        }]
    }), encoding="utf-8")

    report = importer.import_aliases(
        archive_path,
        probe_path=probe_path,
        audit_path=audit_path,
        price_dir=price_dir,
        output=tmp_path / "report.json",
        end="2025-12-31",
        apply=True,
        terminal_tail=True,
    )

    assert report["records"][0]["status"] == (
        "REJECT_NONCONTIGUOUS_TERMINAL_TAIL"
    )
    assert len(pd.read_csv(price_dir / "old.csv")) == 30
