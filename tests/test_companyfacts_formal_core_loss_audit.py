import hashlib
import json
from pathlib import Path

from src.conf import PROJECT_PATH


AUDIT = Path(PROJECT_PATH) / (
    "output/data_provenance/"
    "companyfacts_formal_quarterly_core_loss_audit_2026-08-09.json"
)


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_companyfacts_full_rebuild_core_losses_are_explicit_and_bound() -> None:
    report = json.loads(AUDIT.read_text(encoding="utf-8"))
    formal = Path(PROJECT_PATH) / report["formal_quarterly_path"]
    candidate = Path(PROJECT_PATH) / report["candidate_quarterly_path"]

    assert report["research_only"] is True
    assert report["status"] == "BLOCKS_FULL_REBUILD_PROMOTION"
    assert _sha256(formal) == report["formal_quarterly_sha256"]
    assert _sha256(candidate) == report["candidate_quarterly_sha256"]
    assert report["lost_core_row_count"] == 66
    assert report["lost_core_ticker_count"] == 26
    assert report["status_counts"] == {
        "CONCEPT_ABSENT": 58,
        "EXACT_ACCESSION_CONCEPT_PRESENT": 8,
    }
    assert len(report["records"]) == report["lost_core_row_count"]
    assert {
        row["ticker"]
        for row in report["records"]
        if row["current_payload_status"] == "EXACT_ACCESSION_CONCEPT_PRESENT"
    } == {"BSY", "HFWA", "NEWT", "OCSL", "OUST", "XERS"}
