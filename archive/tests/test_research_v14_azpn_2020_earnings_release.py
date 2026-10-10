import pandas as pd

from scripts.research_v14_azpn_2020_earnings_release import (
    CURRENT_NET_INCOME,
    CURRENT_REVENUE,
    PRIOR_NET_INCOME,
    PRIOR_REVENUE,
    direct_growth_facts,
    exact_growth_evidence,
)
from src.financial.quarterly_fundamentals import quarterly_growth_snapshot


def test_exact_annual_gaap_growth_is_negative() -> None:
    evidence = exact_growth_evidence()

    assert evidence["current_revenue_ttm"] == CURRENT_REVENUE == 590_181_000
    assert evidence["prior_revenue_ttm"] == PRIOR_REVENUE == 598_345_000
    assert evidence["current_net_income_ttm"] == CURRENT_NET_INCOME == 225_708_000
    assert evidence["prior_net_income_ttm"] == PRIOR_NET_INCOME == 262_734_000
    assert evidence["revenue_growth"] == CURRENT_REVENUE / PRIOR_REVENUE - 1
    assert evidence["net_income_growth"] == CURRENT_NET_INCOME / PRIOR_NET_INCOME - 1
    assert evidence["decision"] == "FAIL_REVENUE_AND_NET_INCOME_GROWTH"


def test_direct_bundle_is_available_only_after_8k() -> None:
    facts = direct_growth_facts("2026-08-23")
    facts["fiscal_end"] = pd.to_datetime(facts["fiscal_end"])
    facts["available_date"] = pd.to_datetime(facts["available_date"])

    before = quarterly_growth_snapshot(facts, pd.Timestamp("2020-08-11"), 150)
    at_signal = quarterly_growth_snapshot(facts, pd.Timestamp("2020-11-30"), 150)

    assert before.empty
    assert at_signal.loc["AZPN", "revenue_growth"] < 0
    assert at_signal.loc["AZPN", "net_income_growth"] < 0
    assert set(facts["metric"]) == {
        "revenue_ttm", "revenue_growth", "net_income_ttm", "net_income_growth"
    }


def test_bundle_remains_research_only_by_construction() -> None:
    facts = direct_growth_facts("2026-08-23")

    assert set(facts["form"]) == {"8-K_EX-99.1_EXACT_ANNUAL"}
    assert set(facts["accession"]) == {"0000929940-20-000036"}
    assert not facts["concept"].str.contains("non-gaap", case=False).any()
