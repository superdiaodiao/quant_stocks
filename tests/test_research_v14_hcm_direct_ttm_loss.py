import pandas as pd

from scripts.research_v14_hcm_direct_ttm_loss import EXPECTED_TTM, SOURCES
from src.financial.quarterly_fundamentals import quarterly_profit_ttm_snapshot


def test_hcm_direct_ttm_values_use_exact_annual_and_half_year_math() -> None:
    assert SOURCES["2020_h1"]["filed"] == "2020-07-30"
    assert SOURCES["2021_h1"]["filed"] == "2021-07-28"
    assert EXPECTED_TTM["2020-06-30"] == -110_349_000.0
    assert EXPECTED_TTM["2021-06-30"] == -178_433_000.0


def test_profit_snapshot_accepts_exact_ttm_loss_without_inventing_quarters() -> None:
    frame = pd.DataFrame({
        "ticker": ["HCM"],
        "fiscal_end": pd.to_datetime(["2021-06-30"]),
        "available_date": pd.to_datetime(["2021-07-28"]),
        "metric": ["net_income_ttm"],
        "value": [-178_433_000.0],
    })

    before = quarterly_profit_ttm_snapshot(
        frame, pd.Timestamp("2021-07-27"), 365
    )
    after = quarterly_profit_ttm_snapshot(
        frame, pd.Timestamp("2021-07-30"), 365
    )

    assert before.empty
    assert after.loc["HCM", "net_income_ttm"] == -178_433_000.0
    assert after.loc["HCM", "financial_age_days"] == 2
