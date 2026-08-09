import gzip
import hashlib
import json
from pathlib import Path
import zipfile

import pandas as pd

from scripts.historicaldata_price_import import (
    _frame_sha256,
    _member_sha256,
    _read_stooq_member,
)
from scripts.sec_submission_triage import _payload_sha256
from src.conf import PROJECT_PATH


EVIDENCE = Path(PROJECT_PATH) / (
    "output/data_provenance/sec_stooq_alias_vape_bnc_applied_2026-08-09.json"
)


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_vape_bnc_evidence_replays_sec_identity_and_stooq_tail() -> None:
    report = json.loads(EVIDENCE.read_text(encoding="utf-8"))
    record = report["records"][0]
    assert report["research_only"] is True
    assert report["tail_mode"] == "terminal"
    assert report["successor_overrides"] == {"VAPE": "BNC"}
    assert report["archive_sha256"] == (
        "3b818755da09c4754f5758b5140df869fbd68cfcd16c3c7634331212f70d1fb0"
    )
    assert record["historical_ticker"] == "VAPE"
    assert record["successor_ticker"] == "BNC"
    assert record["cik"] == "0001482541"
    assert record["status"] == "UPDATED"
    assert record["rows_missing"] == 238

    issuer = record["sec_issuers"][0]
    assert issuer["current_tickers"] == ["BNC", "BNCWW", "BNCWZ"]
    cache_path = Path(issuer["submission_cache_path"])
    assert _sha256(cache_path) == issuer["submission_cache_file_sha256"]
    with gzip.open(cache_path, "rt", encoding="utf-8") as handle:
        envelope = json.load(handle)
    assert _payload_sha256(envelope["payload"]) == (
        issuer["submission_payload_sha256"]
    )
    assert envelope["payload"]["tickers"] == ["BNC", "BNCWW", "BNCWZ"]

    validation = record["member_validations"][0]
    archive_path = Path(report["archive_path"])
    assert _sha256(archive_path) == report["archive_sha256"]
    with zipfile.ZipFile(archive_path) as archive:
        assert _member_sha256(archive, validation["member"]) == (
            validation["member_sha256"]
        )
        source = _read_stooq_member(archive, validation["member"], "BNC")
    cross = validation["cross_validation"]
    assert cross["passed"] is True
    assert cross["sessions"] == 853
    for field in ("open", "high", "low", "close"):
        assert cross["fields"][field]["median_ratio"] == 1.0
        assert cross["fields"][field]["within_1pct"] == 1.0

    source["ticker"] = "VAPE"
    missing = source.loc[
        source["date"].gt(pd.Timestamp(record["local_last_date"]))
        & source["date"].le(pd.Timestamp(report["end"]))
    ].copy().sort_values("date")
    assert len(missing) == record["rows_missing"] == 238
    assert _frame_sha256(missing) == record["missing_rows_sha256"]

    price_path = Path(record["price_path"])
    persisted = pd.read_csv(price_path, parse_dates=["date"])
    appended = persisted.loc[persisted["date"].isin(missing["date"])]
    assert _sha256(price_path) == record["local_sha256_after"]
    assert _frame_sha256(appended) == record[
        "persisted_appended_rows_sha256"
    ]
