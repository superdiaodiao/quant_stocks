import gzip
import hashlib
import json
from pathlib import Path

import pandas as pd

from scripts.historicaldata_price_import import _frame_sha256
from scripts.otc_historical_price_repair import _parse_edgar
from src.conf import PROJECT_PATH


EVIDENCE = Path(PROJECT_PATH) / (
    "output/data_provenance/"
    "sec_otc_alias_terminal_isrl_isrlf_applied_2026-08-09.json"
)


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_isrl_otc_alias_evidence_replays_identity_source_and_appended_rows() -> None:
    report = json.loads(EVIDENCE.read_text(encoding="utf-8"))
    record = next(
        row for row in report["records"] if row["historical_ticker"] == "ISRL"
    )
    assert report["research_only"] is True
    assert report["tail_mode"] == "terminal"
    assert record["successor_ticker"] == "ISRLF"
    assert record["status"] == "UPDATED"
    assert record["cik"] == "0001915328"
    assert record["sec_search_payload_sha256"]
    assert {
        str(issuer["cik"]).zfill(10) for issuer in record["sec_issuers"]
    } == {"0001915328"}
    assert all(
        issuer["submission_payload_sha256"] for issuer in record["sec_issuers"]
    )

    stooq_report = Path(report["stooq_report_path"])
    if not stooq_report.is_absolute():
        stooq_report = Path(PROJECT_PATH) / stooq_report
    assert _sha256(stooq_report) == report["stooq_report_sha256"]

    cache_path = Path(record["cache_path"])
    raw = gzip.decompress(cache_path.read_bytes())
    assert hashlib.sha256(raw).hexdigest() == record["source_payload_sha256"]
    source, company_name = _parse_edgar(raw, "ISRLF")
    assert company_name == record["source_company_name"]

    validation = record["cross_validation"]
    assert validation["validation_scope"] == "recent_stable_overlap_tail"
    assert validation["sessions"] == 252
    assert validation["ohlc_within_1pct"] == 1.0
    assert validation["volume_median_ratio"] == 1.0
    for field in ("open", "high", "low", "close"):
        source[field] = source[field].astype(float) * float(
            validation["price_factor"]
        )
    source["volume"] = source["volume"].astype(float) * float(
        validation["volume_factor"]
    )
    source["ticker"] = "ISRL"
    missing = source.loc[
        source["date"].gt(pd.Timestamp(record["local_last_date"]))
        & source["date"].le(pd.Timestamp(report["end"]))
    ].copy().sort_values("date")
    assert len(missing) == record["rows_missing"] == 154
    assert _frame_sha256(missing) == record["missing_rows_sha256"]
    assert int(missing["volume"].gt(0).sum()) == record[
        "positive_volume_rows_missing"
    ] == 68
    assert int(missing["volume"].eq(0).sum()) == record[
        "zero_volume_rows_missing"
    ] == 86
    assert int(missing["close"].nunique()) == record[
        "unique_close_values_missing"
    ] == 37
    assert missing.loc[missing["volume"].gt(0), "date"].max().strftime(
        "%Y-%m-%d"
    ) == record["last_positive_volume_date"] == "2026-07-17"

    price_path = Path(record["price_path"])
    persisted = pd.read_csv(price_path, parse_dates=["date"])
    appended = persisted.loc[persisted["date"].isin(missing["date"])]
    assert _sha256(price_path) == record["local_sha256_after"]
    assert _frame_sha256(appended) == record["persisted_appended_rows_sha256"]


def test_pmd_otc_alias_evidence_replays_scaled_source_and_appended_rows() -> None:
    evidence = Path(PROJECT_PATH) / (
        "output/data_provenance/"
        "sec_otc_alias_terminal_pmd_pmdi_applied_2026-08-09.json"
    )
    report = json.loads(evidence.read_text(encoding="utf-8"))
    record = report["records"][0]
    assert report["research_only"] is True
    assert report["tail_mode"] == "terminal"
    assert report["end"] == "2026-07-17"
    assert record["historical_ticker"] == "PMD"
    assert record["successor_ticker"] == "PMDI"
    assert record["cik"] == "0000806517"
    assert record["status"] == "UPDATED"
    assert record["sec_search_payload_sha256"]
    assert {
        str(issuer["cik"]).zfill(10) for issuer in record["sec_issuers"]
    } == {"0000806517"}
    assert all(
        issuer["submission_payload_sha256"] for issuer in record["sec_issuers"]
    )

    stooq_report = Path(report["stooq_report_path"])
    if not stooq_report.is_absolute():
        stooq_report = Path(PROJECT_PATH) / stooq_report
    assert _sha256(stooq_report) == report["stooq_report_sha256"]
    raw = gzip.decompress(Path(record["cache_path"]).read_bytes())
    assert hashlib.sha256(raw).hexdigest() == record["source_payload_sha256"]
    source, company_name = _parse_edgar(raw, "PMDI")
    assert company_name == record["source_company_name"] == "PSYCHEMEDICS CORP"

    validation = record["cross_validation"]
    assert validation["validation_scope"] == "recent_stable_overlap_tail"
    assert validation["sessions"] == 252
    assert validation["ohlc_within_1pct"] == 1.0
    assert validation["price_factor"] == 1.0048231191030463
    assert validation["volume_factor"] == 0.9952017229029049
    for field in ("open", "high", "low", "close"):
        source[field] = source[field].astype(float) * float(
            validation["price_factor"]
        )
    source["volume"] = source["volume"].astype(float) * float(
        validation["volume_factor"]
    )
    source["ticker"] = "PMD"
    missing = source.loc[
        source["date"].gt(pd.Timestamp(record["local_last_date"]))
        & source["date"].le(pd.Timestamp(report["end"]))
    ].copy().sort_values("date")
    assert len(missing) == record["rows_missing"] == 385
    assert _frame_sha256(missing) == record["missing_rows_sha256"]
    assert int(missing["volume"].gt(0).sum()) == record[
        "positive_volume_rows_missing"
    ] == 204
    assert int(missing["close"].nunique()) == record[
        "unique_close_values_missing"
    ] == 91
    assert missing.loc[missing["volume"].gt(0), "date"].max().strftime(
        "%Y-%m-%d"
    ) == record["last_positive_volume_date"] == "2026-06-26"

    price_path = Path(record["price_path"])
    persisted = pd.read_csv(price_path, parse_dates=["date"])
    appended = persisted.loc[persisted["date"].isin(missing["date"])]
    assert _sha256(price_path) == record["local_sha256_after"]
    assert _frame_sha256(appended) == record["persisted_appended_rows_sha256"]


def test_btm_otc_alias_evidence_replays_carried_boundary_and_tail() -> None:
    evidence = Path(PROJECT_PATH) / (
        "output/data_provenance/"
        "sec_otc_alias_terminal_btm_boundary_applied_2026-08-09.json"
    )
    report = json.loads(evidence.read_text(encoding="utf-8"))
    record = next(
        row for row in report["records"] if row["historical_ticker"] == "BTM"
    )
    assert report["research_only"] is True
    assert report["tail_mode"] == "terminal"
    assert report["replace_carried_terminal_row"] is True
    assert report["end"] == "2026-07-17"
    assert record["successor_ticker"] == "BTMCQ"
    assert record["cik"] == "0001901799"
    assert record["status"] == "UPDATED"
    assert record["rows_replaced"] == 1
    assert {
        str(issuer["cik"]).zfill(10) for issuer in record["sec_issuers"]
    } == {"0001901799"}

    raw = gzip.decompress(Path(record["cache_path"]).read_bytes())
    assert hashlib.sha256(raw).hexdigest() == record["source_payload_sha256"]
    source, company_name = _parse_edgar(raw, "BTMCQ")
    assert company_name == record["source_company_name"]

    validation = record["cross_validation"]
    assert validation["validation_scope"] == "replace_single_carried_terminal_row"
    assert validation["sec_cik"] == "0001901799"
    assert validation["replacement_date"] == "2026-05-26"
    assert all(validation["local_terminal_repeated_fields"].values())
    assert validation["old_local_row"] == {
        "open": 0.5919,
        "high": 0.628,
        "low": 0.45,
        "close": 0.4919,
        "volume": 1534154.0,
    }
    assert validation["new_source_row_before_normalization"] == {
        "open": 0.215,
        "high": 0.37,
        "low": 0.155,
        "close": 0.288994,
        "volume": 443349.0,
    }
    stable = validation["prior_stable_tail_validation"]
    assert stable["sessions"] == 64
    assert stable["ohlc_within_1pct"] == 1.0
    assert validation["price_factor"] == 1.0
    assert validation["volume_factor"] == 0.9998952749002563

    for field in ("open", "high", "low", "close"):
        source[field] = source[field].astype(float) * float(
            validation["price_factor"]
        )
    source["volume"] = source["volume"].astype(float) * float(
        validation["volume_factor"]
    )
    source["ticker"] = "BTM"
    replacement_and_tail = source.loc[
        source["date"].ge(pd.Timestamp(record["local_last_date"]))
        & source["date"].le(pd.Timestamp(report["end"]))
    ].copy().sort_values("date")
    assert len(replacement_and_tail) == record["rows_missing"] == 37
    assert _frame_sha256(replacement_and_tail) == record["missing_rows_sha256"]
    assert int(replacement_and_tail["volume"].gt(0).sum()) == 37
    assert int(replacement_and_tail["close"].nunique()) == 30
    assert record["last_positive_volume_date"] == "2026-07-17"

    price_path = Path(record["price_path"])
    persisted = pd.read_csv(price_path, parse_dates=["date"])
    persisted_rows = persisted.loc[
        persisted["date"].isin(replacement_and_tail["date"])
    ]
    assert _sha256(price_path) == record["local_sha256_after"]
    assert _frame_sha256(persisted_rows) == record[
        "persisted_appended_rows_sha256"
    ]


def test_vmca_vmcaf_evidence_replays_sec_identity_and_otc_tail() -> None:
    evidence = Path(PROJECT_PATH) / (
        "output/data_provenance/"
        "sec_otc_alias_vmca_vmcaf_applied_2026-08-09.json"
    )
    report = json.loads(evidence.read_text(encoding="utf-8"))
    record = next(
        row for row in report["records"]
        if row["historical_ticker"] == "VMCA"
        and row["successor_ticker"] == "VMCAF"
    )
    assert report["research_only"] is True
    assert report["tail_mode"] == "terminal"
    assert report["end"] == "2026-07-17"
    assert record["cik"] == "0001892747"
    assert record["status"] == "UPDATED"
    assert record["sec_issuers"][0]["current_tickers"] == [
        "VMCAF", "VMCUF", "VMCWF"
    ]

    raw = gzip.decompress(Path(record["cache_path"]).read_bytes())
    assert hashlib.sha256(raw).hexdigest() == record["source_payload_sha256"]
    source, company_name = _parse_edgar(raw, "VMCAF")
    assert company_name == record["source_company_name"] == (
        "VALUENCE MERGER CORP I"
    )
    validation = record["cross_validation"]
    assert validation["validation_scope"] == "recent_stable_overlap_tail"
    assert validation["sessions"] == 252
    assert validation["ohlc_within_1pct"] == 1.0
    assert validation["price_factor"] == 1.0
    assert validation["volume_factor"] == 1.0

    for field in ("open", "high", "low", "close"):
        source[field] = source[field].astype(float) * float(
            validation["price_factor"]
        )
    source["volume"] = source["volume"].astype(float) * float(
        validation["volume_factor"]
    )
    source["ticker"] = "VMCA"
    missing = source.loc[
        source["date"].gt(pd.Timestamp(record["local_last_date"]))
        & source["date"].le(pd.Timestamp(report["end"]))
    ].copy().sort_values("date")
    assert len(missing) == record["rows_missing"] == 340
    assert _frame_sha256(missing) == record["missing_rows_sha256"]
    assert int(missing["volume"].gt(0).sum()) == 29
    assert int(missing["close"].nunique()) == 17
    assert missing.loc[missing["volume"].gt(0), "date"].max().strftime(
        "%Y-%m-%d"
    ) == record["last_positive_volume_date"] == "2026-03-27"

    price_path = Path(record["price_path"])
    persisted = pd.read_csv(price_path, parse_dates=["date"])
    appended = persisted.loc[persisted["date"].isin(missing["date"])]
    assert _sha256(price_path) == record["local_sha256_after"]
    assert _frame_sha256(appended) == record[
        "persisted_appended_rows_sha256"
    ]
