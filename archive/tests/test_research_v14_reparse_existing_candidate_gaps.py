import pandas as pd

from scripts.research_v14_reparse_existing_candidate_gaps import (
    select_reparse_tickers,
)


def test_select_reparse_tickers_is_narrow(tmp_path) -> None:
    path = tmp_path / "priorities.csv"
    pd.DataFrame([
        {"ticker": "A", "recommended_data_action": "REVIEW_US_GAAP_PARSER", "reporting_profile": "NO_PARSED_SEC_FINANCIALS", "raw_sec_cache_profile": "US_GAAP_WITH_10Q"},
        {"ticker": "B", "recommended_data_action": "REPARSE_OR_ACCEPT_HISTORY_LIMIT", "reporting_profile": "SEC_ANNUAL_ONLY_OR_UNMAPPED_QUARTERLY", "raw_sec_cache_profile": "US_GAAP_WITH_10Q"},
        {"ticker": "D", "recommended_data_action": "REPARSE_OR_ACCEPT_HISTORY_LIMIT", "reporting_profile": "SEC_QUARTERLY_PARTIAL", "raw_sec_cache_profile": "US_GAAP_WITH_10Q"},
        {"ticker": "E", "recommended_data_action": "REPARSE_OR_ACCEPT_HISTORY_LIMIT", "reporting_profile": "FOREIGN_ANNUAL_ONLY_NEEDS_QUARTERLY_SOURCE", "raw_sec_cache_profile": "US_GAAP_WITH_10Q"},
        {"ticker": "F", "recommended_data_action": "REPARSE_OR_ACCEPT_HISTORY_LIMIT", "reporting_profile": "FOREIGN_ANNUAL_ONLY_NEEDS_QUARTERLY_SOURCE", "raw_sec_cache_profile": "FOREIGN_PERIODIC_NO_10Q"},
        {"ticker": "C", "recommended_data_action": "REVIEW_US_GAAP_PARSER", "reporting_profile": "NO_PARSED_SEC_FINANCIALS", "raw_sec_cache_profile": "NOT_CACHED"},
    ]).to_csv(path, index=False)
    assert select_reparse_tickers(path) == ["A", "B", "D", "E"]
