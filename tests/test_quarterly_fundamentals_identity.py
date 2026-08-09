import pandas as pd

from src.financial import quarterly_fundamentals


def test_quarterly_loader_maps_pre_rename_provider_ticker(
    tmp_path, monkeypatch
):
    fundamentals = tmp_path / "quarterly.csv"
    pd.DataFrame({
        "ticker": ["NEW", "NEW"],
        "fiscal_end": ["2025-06-30", "2025-09-30"],
        "available_date": ["2025-08-01", "2025-11-01"],
        "metric": ["revenue", "revenue"],
        "value": [10.0, 11.0],
    }).to_csv(fundamentals, index=False)

    def fake_normalize(frame):
        result = frame.copy()
        result.loc[
            result["period_end"].eq("2025-06-30"), "ticker"
        ] = "OLD"
        return result

    monkeypatch.setattr(
        quarterly_fundamentals,
        "normalize_point_in_time_tickers",
        fake_normalize,
    )

    result = quarterly_fundamentals.load_quarterly_fundamentals(
        fundamentals
    )

    assert result["ticker"].tolist() == ["OLD", "NEW"]
