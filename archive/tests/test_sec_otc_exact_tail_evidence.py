import gzip
import hashlib
import json
from pathlib import Path

import pandas as pd

from scripts.historicaldata_price_import import _frame_sha256, _read_local
from scripts.otc_historical_price_repair import _parse_edgar
from src.conf import PROJECT_PATH


EVIDENCE = Path(PROJECT_PATH) / (
    "output/data_provenance/"
    "sec_otc_alias_unresolved_exact_tail_applied_2026-08-09.json"
)
SHORT_EVIDENCE = Path(PROJECT_PATH) / (
    "output/data_provenance/"
    "sec_otc_alias_short_fixed_mirror_applied_2026-08-09.json"
)
ROUNDING_EVIDENCE = Path(PROJECT_PATH) / (
    "output/data_provenance/"
    "sec_otc_alias_short_volume_rounding_applied_2026-08-09.json"
)
UOKA_EVIDENCE = Path(PROJECT_PATH) / (
    "output/data_provenance/"
    "sec_otc_alias_uoka_fixed_mirror_applied_2026-08-09.json"
)


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_sec_otc_exact_tails_replay_identity_scale_and_persisted_rows() -> None:
    report = json.loads(EVIDENCE.read_text(encoding="utf-8"))
    updated = [record for record in report["records"] if record["status"] == "UPDATED"]

    assert report["research_only"] is True
    assert report["applied"] is True
    assert report["terminal_returns_modified"] is False
    assert {record["historical_ticker"] for record in updated} == {
        "DTCK", "EPWK", "STBX"
    }

    for record in updated:
        validation = record["cross_validation"]
        assert validation["passed"] is True
        assert validation["validation_scope"] == (
            "exact_recent_tail_plus_sec_unique_cik"
        )
        assert 10 <= validation["sessions"] <= 19
        assert validation["tail_last_date"] == record["local_last_date"]
        assert validation["sec_cik"] == str(record["cik"]).zfill(10)

        payload = gzip.decompress(Path(record["cache_path"]).read_bytes())
        assert hashlib.sha256(payload).hexdigest() == record["source_payload_sha256"]
        source, company_name = _parse_edgar(payload, record["successor_ticker"])
        assert company_name == record["source_company_name"]

        price_path = Path(record["price_path"])
        assert _sha256(price_path) == record["local_sha256_after"]
        persisted = _read_local(price_path)
        appended_dates = pd.to_datetime(record["missing_dates"])
        appended = persisted.loc[persisted["date"].isin(appended_dates)]
        assert len(appended) == record["rows_missing"]
        assert _frame_sha256(appended) == record["persisted_appended_rows_sha256"]
        assert appended["date"].max().strftime("%Y-%m-%d") == "2026-07-17"
        assert source["date"].max().strftime("%Y-%m-%d") == "2026-07-17"


def test_blmz_short_otc_tail_replays_fixed_mirror_and_sec_identity() -> None:
    report = json.loads(SHORT_EVIDENCE.read_text(encoding="utf-8"))
    updated = [record for record in report["records"] if record["status"] == "UPDATED"]
    assert len(updated) == 1
    record = updated[0]
    assert (record["historical_ticker"], record["successor_ticker"]) == (
        "BLMZ", "BLMZF"
    )
    validation = record["cross_validation"]
    assert validation["validation_scope"] == (
        "exact_short_tail_plus_fixed_git_mirror_plus_sec_identity"
    )
    assert validation["sessions"] == 9
    cross_source = validation["fixed_mirror_sec_cross_source"]
    assert cross_source["passed"] is True
    assert cross_source["mirror_commit"] == (
        "aaa088ad222f72785821f4fca880ff340de20c25"
    )
    assert cross_source["mirror_historical_overlap"]["sessions"] == 240
    assert cross_source["sec_cik"] == str(record["cik"]).zfill(10)

    payload = gzip.decompress(Path(record["cache_path"]).read_bytes())
    assert hashlib.sha256(payload).hexdigest() == record["source_payload_sha256"]
    price_path = Path(record["price_path"])
    assert _sha256(price_path) == record["local_sha256_after"]
    persisted = _read_local(price_path)
    appended = persisted.loc[
        persisted["date"].isin(pd.to_datetime(record["missing_dates"]))
    ]
    assert len(appended) == 147
    assert _frame_sha256(appended) == record["persisted_appended_rows_sha256"]
    assert appended["date"].max().strftime("%Y-%m-%d") == "2026-07-17"


def test_bhat_short_otc_tail_bounds_volume_rounding_error() -> None:
    report = json.loads(ROUNDING_EVIDENCE.read_text(encoding="utf-8"))
    updated = [record for record in report["records"] if record["status"] == "UPDATED"]
    assert len(updated) == 1
    record = updated[0]
    assert (record["historical_ticker"], record["successor_ticker"]) == (
        "BHAT", "BHATF"
    )
    validation = record["cross_validation"]
    assert validation["validation_scope"] == (
        "exact_short_tail_plus_fixed_git_mirror_plus_sec_identity"
    )
    assert validation["sessions"] == 5
    assert validation["volume_within_0_1pct"] == 1.0
    assert abs(validation["volume_median_ratio"] - 1.0) <= 0.001
    cross_source = validation["fixed_mirror_sec_cross_source"]
    assert cross_source["mirror_historical_overlap"]["sessions"] == 1496
    assert cross_source["mirror_commit"] == (
        "aaa088ad222f72785821f4fca880ff340de20c25"
    )
    assert cross_source["sec_cik"] == str(record["cik"]).zfill(10)

    payload = gzip.decompress(Path(record["cache_path"]).read_bytes())
    assert hashlib.sha256(payload).hexdigest() == record["source_payload_sha256"]
    price_path = Path(record["price_path"])
    assert _sha256(price_path) == record["local_sha256_after"]
    persisted = _read_local(price_path)
    appended = persisted.loc[
        persisted["date"].isin(pd.to_datetime(record["missing_dates"]))
    ]
    assert len(appended) == 86
    assert _frame_sha256(appended) == record["persisted_appended_rows_sha256"]
    assert appended["date"].max().strftime("%Y-%m-%d") == "2026-07-17"


def test_uoka_short_otc_tail_replays_discovered_pinned_mirror() -> None:
    report = json.loads(UOKA_EVIDENCE.read_text(encoding="utf-8"))
    updated = [record for record in report["records"] if record["status"] == "UPDATED"]
    assert len(updated) == 1
    record = updated[0]
    assert (record["historical_ticker"], record["successor_ticker"]) == (
        "UOKA", "UOKAF"
    )
    validation = record["cross_validation"]
    assert validation["sessions"] == 4
    assert validation["volume_within_0_1pct"] == 1.0
    cross_source = validation["fixed_mirror_sec_cross_source"]
    assert cross_source["passed"] is True
    assert cross_source["mirror_historical_overlap"]["sessions"] == 1494
    assert cross_source["mirror_payload_sha256"] == (
        "6571deb99942391b44e3ae35482d9f438bd3667970ee2fd849da999f63682cf0"
    )
    assert cross_source["sec_cik"] == str(record["cik"]).zfill(10)

    mirror_provenance = Path(cross_source["mirror_provenance_path"])
    assert _sha256(mirror_provenance) == cross_source["mirror_provenance_sha256"]
    payload = gzip.decompress(Path(record["cache_path"]).read_bytes())
    assert hashlib.sha256(payload).hexdigest() == record["source_payload_sha256"]
    price_path = Path(record["price_path"])
    assert _sha256(price_path) == record["local_sha256_after"]
    persisted = _read_local(price_path)
    appended = persisted.loc[
        persisted["date"].isin(pd.to_datetime(record["missing_dates"]))
    ]
    assert len(appended) == 82
    assert _frame_sha256(appended) == record["persisted_appended_rows_sha256"]
    assert appended["date"].max().strftime("%Y-%m-%d") == "2026-07-17"
