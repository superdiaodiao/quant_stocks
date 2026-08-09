import hashlib
import json
from pathlib import Path

from src.conf import PROJECT_PATH


EVIDENCE = Path(PROJECT_PATH) / (
    "output/data_provenance/"
    "stooq_2025_selected_price_cross_validation_2026-08-09.json"
)


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_post_validation_2025_selected_prices_are_independently_cross_validated() -> None:
    evidence = json.loads(EVIDENCE.read_text(encoding="utf-8"))
    records = {row["ticker"]: row for row in evidence["records"]}

    assert evidence["research_only"] is True
    assert evidence["applied"] is False
    assert evidence["formal_financial_files_modified"] is False
    assert evidence["terminal_returns_modified"] is False
    assert evidence["archive_sha256"] == (
        "3b818755da09c4754f5758b5140df869fbd68cfcd16c3c7634331212f70d1fb0"
    )
    assert len(evidence["requested_tickers"]) == 26
    assert set(records) == set(evidence["requested_tickers"])

    validated = {
        ticker: row
        for ticker, row in records.items()
        if row["status"] == "DRY_RUN_ELIGIBLE"
    }
    assert len(validated) == 25
    assert set(records) - set(validated) == {"VIRT"}
    for ticker, row in validated.items():
        assert row["rows_missing"] == 0, ticker
        assert row["cross_validation"]["passed"] is True, ticker
        assert row["cross_validation"]["fields"]["close"] == {
            "median_ratio": 1.0,
            "within_1pct": 1.0,
        }
        local_path = Path(PROJECT_PATH) / "cleaned_stocks_data/price" / f"{ticker.lower()}.csv"
        assert _sha256(local_path) == row["local_sha256_before"]
        assert row["local_sha256_after"] == row["local_sha256_before"]

    virt = records["VIRT"]["member_validations"][0]["cross_validation"]
    assert virt["passed"] is False
    assert virt["fields"]["close"] == {
        "median_ratio": 1.0,
        "within_1pct": 1.0,
    }

