import pandas as pd

from scripts.research_v14_exact_annual_ttm_losses import (
    exact_annual_ttm_losses,
)
from src.financial.quarterly_fundamentals import quarterly_profit_ttm_snapshot
from src.io.fundamentals_update import OUTPUT_COLUMNS


def _row(ticker: str, value: float, form: str, metric: str = "net_income"):
    return {
        "ticker": ticker,
        "fiscal_end": "2019-12-31",
        "available_date": "2020-03-01",
        "metric": metric,
        "value": value,
        "taxonomy": "us-gaap",
        "concept": "NetIncomeLoss",
        "form": form,
        "accession": f"accession-{ticker}-{metric}",
        "fetched_at": "2026-08-14",
    }


def test_exact_annual_ttm_losses_cannot_create_growth_eligibility() -> None:
    annual = pd.DataFrame([
        _row("LOSS", -10.0, "20-F"),
        _row("ZERO", 0.0, "10-K"),
        _row("PROFIT", 10.0, "10-K"),
        _row("QUARTER", -10.0, "10-Q"),
        _row("REVENUE", -10.0, "20-F", metric="revenue"),
    ], columns=OUTPUT_COLUMNS)

    facts = exact_annual_ttm_losses(annual)

    assert set(facts["ticker"]) == {"LOSS", "ZERO"}
    assert set(facts["metric"]) == {"net_income_ttm"}
    assert not facts["concept"].str.contains("revenue", case=False).any()


def test_exact_annual_loss_is_available_only_after_filing_date() -> None:
    annual = pd.DataFrame([
        _row("LOSS", -10.0, "20-F"),
    ], columns=OUTPUT_COLUMNS)
    facts = exact_annual_ttm_losses(annual)
    facts["fiscal_end"] = pd.to_datetime(facts["fiscal_end"])
    facts["available_date"] = pd.to_datetime(facts["available_date"])

    before = quarterly_profit_ttm_snapshot(
        facts, pd.Timestamp("2020-02-28"), 365
    )
    after = quarterly_profit_ttm_snapshot(
        facts, pd.Timestamp("2020-03-31"), 365
    )

    assert before.empty
    assert after.loc["LOSS", "net_income_ttm"] == -10.0
    assert after.loc["LOSS", "financial_age_days"] == 30
