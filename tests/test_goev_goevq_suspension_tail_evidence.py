import gzip
import hashlib
import json
from pathlib import Path

import pandas as pd

from scripts.historicaldata_price_import import _frame_sha256
from scripts.sec_suspension_tail_import import _read_stooq_text
from scripts.sec_terminal_filing_evidence import _filing_text
from src.conf import PROJECT_PATH


EVIDENCE = Path(PROJECT_PATH) / (
    "output/data_provenance/sec_suspension_tail_goev_goevq_applied_2026-08-09.json"
)


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_goev_goevq_tail_replays_source_sec_boundary_and_persisted_rows():
    report = json.loads(EVIDENCE.read_text(encoding="utf-8"))
    assert report["status"] == "UPDATED"
    assert report["rows_added"] == 11
    assert report["overlap_validation"]["sessions"] == 1377
    assert report["overlap_validation"]["passed"] is True
    assert report["last_added_date"] == "2025-01-28"
    assert report["suspension_date"] == "2025-01-29"

    source_path = Path(PROJECT_PATH) / report["source_path"]
    assert _sha256(source_path) == report["source_sha256"]
    source = _read_stooq_text(source_path, "GOEVQ")
    assert _frame_sha256(source) == report["source_frame_sha256"]
    tail = source.loc[source["date"].ge(pd.Timestamp(report["first_added_date"]))].copy()
    tail["ticker"] = "GOEV"
    assert _frame_sha256(tail) == report["appended_rows_sha256"]

    price_path = Path(report["price_path"])
    assert _sha256(price_path) == report["local_sha256_after"]
    prices = pd.read_csv(price_path, parse_dates=["date"])
    persisted = prices.loc[prices["date"].isin(tail["date"])]
    assert _frame_sha256(persisted) == report["appended_rows_sha256"]

    with gzip.open(Path(PROJECT_PATH) / report["sec_cache_path"], "rt", encoding="utf-8") as handle:
        envelope = json.load(handle)
    payload = bytes.fromhex(envelope["payload_hex"])
    assert hashlib.sha256(payload).hexdigest() == report["sec_payload_sha256"]
    assert report["expected_filing_phrase"].lower() in _filing_text(payload).lower()
