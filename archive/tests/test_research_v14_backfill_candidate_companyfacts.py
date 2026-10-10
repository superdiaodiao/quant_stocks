import pandas as pd

from scripts.research_v14_backfill_candidate_companyfacts import (
    fetch_tickers,
    reparse_tickers,
)


def test_fetch_tickers_selects_only_missing_official_payloads(tmp_path) -> None:
    path = tmp_path / "priorities.csv"
    pd.DataFrame([
        {
            "ticker": "abc",
            "recommended_data_action": "FETCH_SEC_COMPANYFACTS",
            "raw_sec_cache_profile": "NOT_CACHED",
        },
        {
            "ticker": "ABC",
            "recommended_data_action": "FETCH_SEC_COMPANYFACTS",
            "raw_sec_cache_profile": "NOT_CACHED",
        },
        {
            "ticker": "OLD",
            "recommended_data_action": "REVIEW_US_GAAP_PARSER",
            "raw_sec_cache_profile": "US_GAAP_WITH_10Q",
        },
    ]).to_csv(path, index=False)
    assert fetch_tickers(path) == ["ABC"]


def test_reparse_tickers_selects_cached_us_quarterly_issuers(tmp_path) -> None:
    path = tmp_path / "priorities.csv"
    pd.DataFrame([
        {
            "ticker": "gbdc",
            "recommended_data_action": "REPARSE_OR_ACCEPT_HISTORY_LIMIT",
            "raw_sec_cache_profile": "US_GAAP_WITH_10Q",
        },
        {
            "ticker": "SNY",
            "recommended_data_action": "NEEDS_FOREIGN_QUARTERLY_SOURCE",
            "raw_sec_cache_profile": "FOREIGN_PERIODIC_NO_10Q",
        },
    ]).to_csv(path, index=False)
    assert reparse_tickers(path) == ["GBDC"]
