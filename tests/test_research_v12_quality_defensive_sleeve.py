import pandas as pd

from scripts.research_v12_quality_defensive_sleeve import quality_snapshot


def test_quality_snapshot_uses_only_facts_available_by_as_of():
    annual = pd.DataFrame({
        "ticker": ["AAA"] * 8,
        "fiscal_end": pd.to_datetime(["2023-12-31"] * 4 + ["2024-12-31"] * 4),
        "available_date": pd.to_datetime(["2024-02-01"] * 4 + ["2025-02-01"] * 4),
        "metric": ["net_income", "operating_cash_flow", "assets", "equity"] * 2,
        "value": [10, 12, 100, 60, 1000, 1200, 100, 60],
    })
    result = quality_snapshot(annual, pd.Timestamp("2024-12-31"))
    assert result.loc["AAA", "fiscal_end"] == pd.Timestamp("2023-12-31")
    assert result.loc["AAA", "roa"] == 0.10
