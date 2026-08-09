import gzip
import hashlib
import json
from pathlib import Path

import pandas as pd

from scripts.sec_terminal_filing_evidence import _filing_text
from src.conf import PROJECT_PATH


PROVENANCE = (
    Path(PROJECT_PATH)
    / "output/data_provenance/stkl_post_completion_duplicate_tail_repair_2026-08-09.json"
)


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_stkl_duplicate_provider_tail_is_removed_after_sec_confirmed_halt() -> None:
    evidence = json.loads(PROVENANCE.read_text(encoding="utf-8"))
    price_path = Path(PROJECT_PATH) / evidence["price_path"]
    prices = pd.read_csv(price_path)
    prices["date"] = pd.to_datetime(prices["date"], errors="raise")

    assert evidence["research_only"] is True
    assert evidence["status"] == "APPLIED"
    assert _sha256(price_path) == evidence["price_sha256_after"]
    assert evidence["price_sha256_after"] == evidence["restored_prior_validated_price_sha256"]
    assert prices.iloc[-1]["date"].strftime("%Y-%m-%d") == "2026-05-01"
    assert float(prices.iloc[-1]["close"]) == 6.5
    assert not prices["date"].isin(pd.to_datetime(["2026-05-04", "2026-05-05"])).any()

    sina_report = json.loads(
        (
            Path(PROJECT_PATH)
            / "output/data_provenance/sina_historical_price_repair_full_2026-08-08.json"
        ).read_text(encoding="utf-8")
    )
    stkl = next(row for row in sina_report["records"] if row["ticker"] == "STKL")
    assert stkl["append_cutoff"] == "2026-05-01"
    assert stkl["local_sha256_after"] == evidence["price_sha256_after"]

    sina_cache = Path(PROJECT_PATH) / evidence["primary_price_cache_path"]
    assert _sha256(sina_cache) == evidence["primary_price_cache_sha256"]
    assert hashlib.sha256(gzip.decompress(sina_cache.read_bytes())).hexdigest() == (
        evidence["primary_price_payload_sha256"]
    )

    sec_cache = Path(PROJECT_PATH) / evidence["sec_cache_path"]
    assert _sha256(sec_cache) == evidence["sec_cache_sha256"]
    with gzip.open(sec_cache, "rt", encoding="utf-8") as handle:
        envelope = json.load(handle)
    payload = bytes.fromhex(envelope["payload_hex"])
    assert hashlib.sha256(payload).hexdigest() == evidence["sec_filing_payload_sha256"]
    filing_text = _filing_text(payload)
    assert all(phrase in filing_text for phrase in evidence["required_phrases"])
