import gzip
import hashlib
import json
from pathlib import Path

import pandas as pd

from scripts.historicaldata_price_import import PRICE_COLUMNS, _frame_sha256
from scripts.sec_terminal_filing_evidence import _filing_text
from src.conf import PROJECT_PATH


EVIDENCE = Path(PROJECT_PATH) / (
    "output/data_provenance/sec_business_combination_hcvi_namm_applied_2026-08-09.json"
)


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_hcvi_namm_evidence_replays_nasdaq_snapshot_and_sec_filing() -> None:
    report = json.loads(EVIDENCE.read_text(encoding="utf-8"))
    assert report["status"] == "UPDATED"
    assert report["historical_ticker"] == "HCVI"
    assert report["successor_ticker"] == "NAMM"
    assert report["exchange_ratio"] == 1.0
    assert report["rows_added"] == 322
    assert report["first_added_date"] == "2025-04-04"
    assert report["last_added_date"] == "2026-07-17"
    assert report["pre_effective_source_overlap"]["sessions"] == 424
    assert report["pre_effective_source_overlap"]["passed"] is True

    snapshot_path = Path(report["source_snapshot_path"])
    assert _sha256(snapshot_path) == report["source_snapshot_sha256"]
    snapshot = json.loads(snapshot_path.read_text(encoding="utf-8"))
    source = pd.DataFrame(snapshot["records"])
    source["date"] = pd.to_datetime(source["date"], errors="raise")
    source = source[PRICE_COLUMNS].sort_values("date")
    assert _frame_sha256(source) == snapshot["frame_sha256"]
    assert snapshot["frame_sha256"] == report["source_frame_sha256"]

    appended = source.loc[
        source["date"].between(
            pd.Timestamp(report["first_added_date"]),
            pd.Timestamp(report["last_added_date"]),
        )
    ].copy()
    appended["ticker"] = "HCVI"
    assert len(appended) == report["rows_added"]
    assert _frame_sha256(appended) == report["appended_rows_sha256"]

    price_path = Path(report["price_path"])
    assert _sha256(price_path) == report["local_sha256_after"]
    persisted = pd.read_csv(price_path, parse_dates=["date"])
    persisted_tail = persisted.loc[persisted["date"].isin(appended["date"])]
    assert _frame_sha256(persisted_tail) == report["appended_rows_sha256"]

    with gzip.open(report["sec_cache_path"], "rt", encoding="utf-8") as handle:
        envelope = json.load(handle)
    payload = bytes.fromhex(envelope["payload_hex"])
    assert hashlib.sha256(payload).hexdigest() == report["sec_payload_sha256"]
    filing_text = _filing_text(payload).lower()
    for phrase in report["expected_filing_phrases"]:
        assert phrase.lower() in filing_text
