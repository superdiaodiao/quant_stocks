import pandas as pd

from src.io.security_identity import normalize_point_in_time_tickers


def test_old_provider_history_is_mapped_to_historical_ticker(tmp_path):
    mapping = tmp_path / "identity.csv"
    mapping.write_text(
        "provider_ticker,historical_ticker,last_historical_date,current_ticker_first_date,source_url,verified_at\n"
        "RDUS,SCHN,2023-08-31,2023-09-01,https://www.sec.gov/example,2026-07-19T00:00:00Z\n"
    )
    source = pd.DataFrame({
        "ticker": ["RDUS", "RDUS"],
        "period_end": ["2023-08-31", "2023-11-30"],
    })

    result = normalize_point_in_time_tickers(source, mapping)

    assert result["ticker"].tolist() == ["SCHN", "RDUS"]
