import gzip
import json

import pandas as pd

from scripts.sec_submission_triage import (
    load_cached_sec_ticker_maps,
    load_probe_ticker_ciks,
    load_ticker_cik_registry,
    load_unresolved_terminal_tickers,
    triage_sec_submissions,
)


def _submission_payload(current_tickers, form, filing_date):
    return {
        "name": "Example issuer",
        "tickers": current_tickers,
        "filings": {
            "recent": {
                "filingDate": [filing_date],
                "form": [form],
                "accessionNumber": ["0000000123-25-000001"],
                "primaryDocument": ["example.htm"],
            }
        },
    }


def test_sec_submission_triage_caches_leads_without_writing_prices(tmp_path):
    price_dir = tmp_path / "prices"
    price_dir.mkdir()
    price_path = price_dir / "old.csv"
    pd.DataFrame({
        "date": ["2025-07-09"],
        "ticker": ["OLD"],
        "open": [10.0],
        "high": [10.0],
        "low": [10.0],
        "close": [10.0],
        "volume": [100],
    }).to_csv(price_path, index=False)
    before = price_path.read_bytes()
    cache_dir = tmp_path / "cache"
    payload = _submission_payload([], "15-12G", "2025-09-01")

    report = triage_sec_submissions(
        ["old"],
        ticker_ciks={"OLD": {"cik": 123}},
        price_dir=price_dir,
        cache_dir=cache_dir,
        refresh=True,
        analysis_end="2025-12-31",
        benchmark_dates=pd.Series(pd.to_datetime([
            "2025-07-09", "2025-07-10", "2025-09-01",
        ])),
        fetcher=lambda cik: payload if cik == 123 else None,
    )

    record = report["records"][0]
    assert report["research_only"] is True
    assert record["status"] == "RESEARCH_LEAD_ONLY"
    assert record["resolution_review"] == (
        "PRICE_SOURCE_AND_TERMINAL_RETURN_REVIEW"
    )
    assert "Recover PIT prices" in record["next_required_evidence"]
    assert record["missing_nasdaq_sessions_through_analysis_end"] == 2
    assert record["event_filings_after_local_price"][0]["source_url"] == (
        "https://www.sec.gov/Archives/edgar/data/123/000000012325000001/"
        "example.htm"
    )
    assert (cache_dir / "CIK0000000123.json.gz").exists()
    assert price_path.read_bytes() == before

    offline = triage_sec_submissions(
        ["OLD"],
        ticker_ciks={"OLD": {"cik": 123}},
        price_dir=price_dir,
        cache_dir=cache_dir,
        analysis_end="2025-12-31",
        benchmark_dates=pd.Series(pd.to_datetime([
            "2025-07-09", "2025-07-10", "2025-09-01",
        ])),
        fetcher=lambda _cik: (_ for _ in ()).throw(AssertionError("network used")),
    )
    assert offline["mode"] == "offline_cache"
    assert offline["records"][0]["cache_payload_sha256"] == (
        record["cache_payload_sha256"]
    )
    assert price_path.read_bytes() == before

    missing_only = triage_sec_submissions(
        ["OLD"],
        ticker_ciks={"OLD": {"cik": 123}},
        price_dir=price_dir,
        cache_dir=cache_dir,
        refresh_missing_only=True,
        fetcher=lambda _cik: (_ for _ in ()).throw(AssertionError("network used")),
    )
    assert missing_only["mode"] == "refresh_missing_only"


def test_sec_submission_triage_marks_rename_and_missing_cik_as_review_only(tmp_path):
    report = triage_sec_submissions(
        ["OLD", "MISSING"],
        ticker_ciks={"OLD": {"cik": 456}},
        price_dir=tmp_path / "prices",
        cache_dir=tmp_path / "cache",
        refresh=True,
        fetcher=lambda _cik: _submission_payload(["NEW"], "8-K", "2025-08-01"),
    )

    rename, missing = report["records"]
    assert rename["resolution_review"] == "IDENTITY_TRANSITION_REVIEW"
    assert rename["status"] == "RESEARCH_LEAD_ONLY"
    assert missing == {
        "ticker": "MISSING",
        "status": "MISSING_CIK_MAPPING",
        "resolution_review": "CIK_MAPPING_REVIEW",
    }
    assert json.loads(json.dumps(report))["research_only"] is True


def test_sec_submission_triage_keeps_pre_end_price_gap_separate_from_later_exit(tmp_path):
    report = triage_sec_submissions(
        ["OLD"],
        ticker_ciks={"OLD": {"cik": 789}},
        price_dir=tmp_path / "prices",
        cache_dir=tmp_path / "cache",
        refresh=True,
        analysis_end="2025-12-31",
        fetcher=lambda _cik: _submission_payload([], "15-12G", "2026-01-05"),
    )

    record = report["records"][0]
    assert record["resolution_review"] == (
        "PRICE_SOURCE_REVIEW_BEFORE_POST_END_TERMINATION"
    )


def test_foreign_issuer_6k_without_current_ticker_enters_terminal_review(tmp_path):
    report = triage_sec_submissions(
        ["ADR"],
        ticker_ciks={"ADR": {"cik": 321}},
        price_dir=tmp_path / "prices",
        cache_dir=tmp_path / "cache",
        refresh=True,
        fetcher=lambda _cik: _submission_payload([], "6-K", "2025-08-01"),
    )

    record = report["records"][0]
    assert record["resolution_review"] == "PRICE_SOURCE_AND_TERMINAL_RETURN_REVIEW"
    assert record["event_filings_after_local_price"][0]["form"] == "6-K"


def test_ticker_cik_registry_uses_cached_current_map_and_historical_alias(tmp_path):
    cache_dir = tmp_path / "companyfacts"
    map_dir = cache_dir / "ticker_maps"
    map_dir.mkdir(parents=True)
    with gzip.open(map_dir / "ticker_map.json.gz", "wt", encoding="utf-8") as handle:
        json.dump({"CPRX": 1369568, "OLD": 100}, handle)
    historical = tmp_path / "historical.json"
    historical.write_text(json.dumps({"entries": {
        "OLD": {"cik": 200, "source_url": "https://www.sec.gov/example"},
    }}), encoding="utf-8")

    registry = load_ticker_cik_registry(historical, cache_dir)

    assert registry["CPRX"]["cik"] == 1369568
    assert registry["CPRX"]["source_type"] == "cached_sec_ticker_map"
    assert registry["OLD"]["cik"] == 200


def test_cached_ticker_maps_exclude_reused_ticker_without_blocking_others(tmp_path):
    map_dir = tmp_path / "ticker_maps"
    map_dir.mkdir()
    for name, payload in [
        ("old.json.gz", {"UROY": 1, "SAFE": 10}),
        ("new.json.gz", {"UROY": 2, "SAFE": 10}),
    ]:
        with gzip.open(map_dir / name, "wt", encoding="utf-8") as handle:
            json.dump(payload, handle)

    registry = load_cached_sec_ticker_maps(tmp_path)

    assert "UROY" not in registry
    assert registry["SAFE"]["cik"] == 10


def test_probe_registry_requires_one_cik_across_all_reports(tmp_path):
    first = tmp_path / "probe_a.json"
    second = tmp_path / "probe_b.json"
    first.write_text(json.dumps({"results": [
        {"ticker": "SAFE", "status": "ok", "issuers": [{"cik": "000123"}]},
        {"ticker": "MULTI", "status": "ok", "issuers": [
            {"cik": "000456"}, {"cik": "000789"}
        ]},
    ]}), encoding="utf-8")
    second.write_text(json.dumps({"results": [
        {"ticker": "SAFE", "status": "ok", "issuers": [{"cik": "000123"}]},
        {"ticker": "CONFLICT", "status": "ok", "issuers": [{"cik": "000111"}]},
        {"ticker": "CONFLICT", "status": "ok", "issuers": [{"cik": "000222"}]},
    ]}), encoding="utf-8")

    registry = load_probe_ticker_ciks([str(tmp_path / "probe_*.json")])

    assert registry["SAFE"]["cik"] == 123
    assert registry["SAFE"]["source_type"] == "sec_transition_probe_unique_cik"
    assert len(registry["SAFE"]["source_paths"]) == 2
    assert "MULTI" not in registry
    assert "CONFLICT" not in registry


def test_load_unresolved_terminal_tickers_preserves_audit_order(tmp_path):
    audit = tmp_path / "audit.json"
    audit.write_text(json.dumps({"unresolved_terminal_return_histories": [
        {"ticker": "old"}, {"ticker": "NEW"}, {"ticker": "OLD"}
    ]}), encoding="utf-8")

    assert load_unresolved_terminal_tickers(audit) == ["OLD", "NEW"]
