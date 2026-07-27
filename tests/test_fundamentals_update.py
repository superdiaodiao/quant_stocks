import pandas as pd

from src.io.fundamentals_update import (
    audit_fundamentals_coverage,
    parse_companyfacts_annual,
    parse_companyfacts_quarterly,
)
from src.financial.quarterly_fundamentals import quarterly_growth_snapshot


def _fact(val, start=None, end="2025-12-31", filed="2026-02-15", form="10-K", fp="FY"):
    row = {"val": val, "end": end, "filed": filed, "form": form, "fp": fp, "accn": "x"}
    if start:
        row["start"] = start
    return row


def test_companyfacts_parser_keeps_annual_filing_dates_and_rejects_quarters():
    payload = {"facts": {"us-gaap": {
        "RevenueFromContractWithCustomerExcludingAssessedTax": {"units": {"USD": [
            _fact(100, "2025-01-01"),
            _fact(30, "2025-01-01", end="2025-03-31", filed="2025-05-01", form="10-Q", fp="Q1"),
        ]}},
        "NetIncomeLoss": {"units": {"USD": [_fact(10, "2025-01-01")]}},
        "GrossProfit": {"units": {"USD": [_fact(40, "2025-01-01")]}},
        "NetCashProvidedByUsedInOperatingActivities": {"units": {"USD": [_fact(15, "2025-01-01")]}},
        "Assets": {"units": {"USD": [_fact(200)]}},
        "StockholdersEquity": {"units": {"USD": [_fact(80)]}},
    }}}
    frame = parse_companyfacts_annual("abc", payload, fetched_at="2026-07-18")
    assert set(frame["metric"]) == {
        "revenue", "net_income", "gross_profit", "operating_cash_flow", "assets", "equity"
    }
    assert set(frame["available_date"]) == {pd.Timestamp("2026-02-15")}
    assert (frame["ticker"] == "ABC").all()


def test_new_revenue_concept_extends_old_concept_history():
    old = _fact(80, "2023-01-01", end="2023-12-31", filed="2024-02-01")
    recent = _fact(100, "2025-01-01", end="2025-12-31", filed="2026-02-01")
    payload = {"facts": {"us-gaap": {
        "Revenues": {"units": {"USD": [old]}},
        "RevenueFromContractWithCustomerExcludingAssessedTax": {"units": {"USD": [recent]}},
    }}}
    revenue = parse_companyfacts_annual("ABC", payload)
    assert revenue["value"].tolist() == [80.0, 100.0]
    assert revenue.iloc[-1]["concept"] == "RevenueFromContractWithCustomerExcludingAssessedTax"


def test_fundamentals_coverage_requires_all_core_metrics_to_be_fresh():
    metrics = ["net_income", "assets", "equity", "operating_cash_flow"]
    frame = pd.DataFrame([
        {"ticker": ticker, "metric": metric, "available_date": pd.Timestamp(available)}
        for ticker, available, selected in (
            ("A", "2026-02-15", metrics),
            ("B", "2026-02-15", metrics[:-1]),
            ("C", "2024-01-01", metrics),
        )
        for metric in selected
    ])
    audit = audit_fundamentals_coverage(frame, ["A", "B", "C"], pd.Timestamp("2026-07-17").date())
    assert audit["fresh_complete_tickers"] == 1
    assert audit["fresh_complete_coverage"] == 1 / 3


def test_quarterly_parser_keeps_filing_dates_and_derives_fourth_quarter():
    def quarter(value, start, end, filed, frame, accn):
        return {
            "val": value, "start": start, "end": end, "filed": filed,
            "form": "10-Q", "fp": frame[-2:], "frame": frame, "accn": accn,
        }

    revenue_quarters = [
        quarter(20, "2025-01-01", "2025-03-31", "2025-05-01", "CY2025Q1", "q1"),
        quarter(25, "2025-04-01", "2025-06-30", "2025-08-01", "CY2025Q2", "q2"),
        quarter(30, "2025-07-01", "2025-09-30", "2025-11-01", "CY2025Q3", "q3"),
        _fact(110, "2025-01-01", end="2025-12-31", filed="2026-02-15"),
    ]
    revenue_quarters[0]["frame"] = None
    revenue_quarters.append({
        **revenue_quarters[0], "filed": "2026-05-01", "fy": 2026,
        "accn": "q1-comparative", "frame": "CY2025Q1",
    })
    income_quarters = [
        quarter(2, "2025-01-01", "2025-03-31", "2025-05-01", "CY2025Q1", "iq1"),
        quarter(3, "2025-04-01", "2025-06-30", "2025-08-01", "CY2025Q2", "iq2"),
        quarter(4, "2025-07-01", "2025-09-30", "2025-11-01", "CY2025Q3", "iq3"),
        _fact(15, "2025-01-01", end="2025-12-31", filed="2026-02-15"),
    ]
    payload = {"facts": {"us-gaap": {
        "Revenues": {"units": {"USD": revenue_quarters}},
        "NetIncomeLoss": {"units": {"USD": income_quarters}},
    }}}
    frame = parse_companyfacts_quarterly("abc", payload, fetched_at="2026-07-18")
    q1 = frame.loc[
        (frame["fiscal_end"] == pd.Timestamp("2025-03-31"))
        & (frame["metric"] == "revenue")
    ]
    assert q1["available_date"].min() == pd.Timestamp("2025-05-01")
    q4 = frame.loc[frame["fiscal_end"] == pd.Timestamp("2025-12-31")].set_index("metric")
    assert q4.loc["revenue", "value"] == 35
    assert q4.loc["net_income", "value"] == 6
    assert set(q4["available_date"]) == {pd.Timestamp("2026-02-15")}


def test_quarterly_growth_snapshot_uses_only_available_complete_years():
    ends = pd.date_range("2024-03-31", periods=8, freq="QE")
    rows = []
    for index, end in enumerate(ends):
        for metric, value in (("revenue", 100 + 10 * index), ("net_income", 10 + index)):
            rows.append({
                "ticker": "ABC", "fiscal_end": end,
                "available_date": end + pd.Timedelta(days=40),
                "metric": metric, "value": value,
            })
    frame = pd.DataFrame(rows)
    early = quarterly_growth_snapshot(frame, pd.Timestamp("2025-11-01"))
    assert early.empty
    snapshot = quarterly_growth_snapshot(frame, pd.Timestamp("2026-05-20"))
    assert snapshot.loc["ABC", "revenue_growth"] > 0
    assert snapshot.loc["ABC", "net_income_growth"] > 0
