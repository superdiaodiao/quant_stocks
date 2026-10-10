import gzip
import hashlib
import json
from pathlib import Path

import pandas as pd

from scripts.historicaldata_price_import import PRICE_COLUMNS, _frame_sha256
from scripts.sec_terminal_filing_evidence import _filing_text
from src.conf import PROJECT_PATH


PROVENANCE = (
    Path(PROJECT_PATH)
    / "output/data_provenance/para_ticker_reuse_contamination_repair_2026-08-09.json"
)


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_para_default_stock_election_continues_one_for_one_into_psky() -> None:
    contamination = json.loads(PROVENANCE.read_text(encoding="utf-8"))
    report_path = Path(PROJECT_PATH) / contamination["successor_import_report"]
    assert _sha256(report_path) == contamination["successor_import_report_sha256"]
    report = json.loads(report_path.read_text(encoding="utf-8"))

    assert contamination["research_only"] is True
    assert report["status"] == "UPDATED"
    assert report["historical_ticker"] == "PARA"
    assert report["successor_ticker"] == "PSKY"
    assert report["exchange_ratio"] == 1.0
    assert report["source_start_date"] == "2025-08-07"
    assert report["terminal_tail_gap_days"] == 1
    assert report["local_sha256_before"] == (
        contamination["price_sha256_after_removal_before_successor_import"]
    )

    price_path = Path(report["price_path"])
    assert _sha256(price_path) == report["local_sha256_after"]
    prices = pd.read_csv(price_path, parse_dates=["date"]).sort_values("date")
    assert not prices["date"].eq(pd.Timestamp("2026-08-07")).any()
    tail = prices.loc[prices["date"].ge(pd.Timestamp(report["source_start_date"]))]
    assert len(tail) == report["rows_added"] == 237
    assert tail.iloc[0]["date"].strftime("%Y-%m-%d") == "2025-08-07"
    assert tail.iloc[-1]["date"].strftime("%Y-%m-%d") == "2026-07-17"
    assert _frame_sha256(tail[PRICE_COLUMNS]) == report["appended_rows_sha256"]

    snapshot_path = Path(PROJECT_PATH) / report["source_snapshot_path"]
    assert _sha256(snapshot_path) == report["source_snapshot_sha256"]
    snapshot = json.loads(snapshot_path.read_text(encoding="utf-8"))
    source = pd.DataFrame(snapshot["records"])
    source["date"] = pd.to_datetime(source["date"], errors="raise")
    source = source[PRICE_COLUMNS].sort_values("date")
    assert _frame_sha256(source) == report["source_frame_sha256"]
    assert _frame_sha256(source) == snapshot["frame_sha256"]

    sec_cache = Path(PROJECT_PATH) / contamination["sec_cache_path"]
    assert _sha256(sec_cache) == contamination["sec_cache_sha256"]
    with gzip.open(sec_cache, "rt", encoding="utf-8") as handle:
        envelope = json.load(handle)
    payload = bytes.fromhex(envelope["payload_hex"])
    assert hashlib.sha256(payload).hexdigest() == contamination["sec_payload_sha256"]
    filing_text = _filing_text(payload).lower()
    assert all(phrase.lower() in filing_text for phrase in report["expected_filing_phrases"])
