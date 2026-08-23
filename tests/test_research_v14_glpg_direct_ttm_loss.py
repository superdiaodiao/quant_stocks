import pandas as pd

from scripts.research_v14_glpg_direct_ttm_loss import EXPECTED_TTM, SOURCES
from src.financial.quarterly_fundamentals import quarterly_profit_ttm_snapshot


def test_glpg_direct_ttm_uses_exact_annual_and_half_year_math() -> None:
    assert SOURCES["2018_fy"]["filed"] == "2019-03-29"
    assert SOURCES["2019_h1"]["filed"] == "2019-07-25"
    assert EXPECTED_TTM["2019-06-30"] == (
        SOURCES["2018_fy"]["expected"]
        - SOURCES["2018_h1"]["expected"]
        + SOURCES["2019_h1"]["expected"]
    )


def test_glpg_exact_ttm_loss_cannot_invent_quarterly_growth() -> None:
    facts = pd.DataFrame({
        "ticker": ["GLPG"],
        "fiscal_end": pd.to_datetime(["2019-06-30"]),
        "available_date": pd.to_datetime(["2019-07-25"]),
        "metric": ["net_income_ttm"],
        "value": [EXPECTED_TTM["2019-06-30"]],
    })
    snapshot = quarterly_profit_ttm_snapshot(
        facts, pd.Timestamp("2020-01-31"), 365
    )
    assert snapshot.loc["GLPG", "net_income_ttm"] == -66_108_000.0
    assert set(facts["metric"]) == {"net_income_ttm"}
