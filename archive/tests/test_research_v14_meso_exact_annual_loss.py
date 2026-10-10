import pandas as pd

from scripts.research_v14_meso_exact_annual_loss import (
    EXPECTED_NET_LOSS,
    PRIOR_NET_LOSS,
    direct_ttm_facts,
    exact_loss_evidence,
)
from src.financial.quarterly_fundamentals import (
    quarterly_growth_snapshot,
    quarterly_profit_ttm_snapshot,
)


def test_exact_ifrs_annual_loss_is_nonpositive() -> None:
    evidence = exact_loss_evidence()

    assert evidence["net_income_ttm"] == EXPECTED_NET_LOSS == -77_940_000
    assert evidence["prior_net_income_ttm"] == PRIOR_NET_LOSS == -89_799_000
    assert evidence["decision"] == "known_nonpositive_profit"
    assert evidence["available_date"] == "2020-08-28"


def test_loss_is_available_before_monday_signal_without_inventing_growth() -> None:
    facts = direct_ttm_facts("2026-08-23")
    facts["fiscal_end"] = pd.to_datetime(facts["fiscal_end"])
    facts["available_date"] = pd.to_datetime(facts["available_date"])

    before = quarterly_profit_ttm_snapshot(facts, pd.Timestamp("2020-08-27"), 150)
    at_signal = quarterly_profit_ttm_snapshot(facts, pd.Timestamp("2020-08-31"), 150)
    growth = quarterly_growth_snapshot(facts, pd.Timestamp("2020-08-31"), 150)

    assert before.empty
    assert at_signal.loc["MESO", "net_income_ttm"] == EXPECTED_NET_LOSS
    assert growth.empty
    assert set(facts["metric"]) == {"net_income_ttm"}


def test_fact_uses_issuer_total_not_eps_or_convenience_translation() -> None:
    facts = direct_ttm_facts("2026-08-23")

    assert set(facts["taxonomy"]) == {"ifrs-full"}
    assert set(facts["form"]) == {"6-K_EX-99.1_EXACT_ANNUAL"}
    assert not facts["concept"].str.contains("eps|per.share", case=False, regex=True).any()
