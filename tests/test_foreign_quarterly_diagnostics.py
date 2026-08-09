import pandas as pd

from src.research.foreign_quarterly_diagnostics import (
    diagnose_foreign_payload,
    foreign_quarters_to_point_in_time,
    reconstruct_foreign_quarters,
)


def _fact(value, start, end, filed, form="6-K"):
    return {
        "val": value,
        "start": start,
        "end": end,
        "filed": filed,
        "form": form,
        "accn": f"{end}-{filed}",
    }


def _payload(revenue_unit="EUR", income_unit="EUR"):
    revenue = []
    income = []
    for year in (2023, 2024):
        values = (10, 20, 30, 40)
        starts = [
            f"{year}-01-01",
            f"{year}-01-01",
            f"{year}-01-01",
            f"{year}-01-01",
        ]
        ends = [
            f"{year}-03-31",
            f"{year}-06-30",
            f"{year}-09-30",
            f"{year}-12-31",
        ]
        forms = ["6-K", "6-K", "6-K", "20-F"]
        cumulative = [values[0], sum(values[:2]), sum(values[:3]), sum(values)]
        for value, start, end, form in zip(cumulative, starts, ends, forms):
            filed = (
                f"{year + 1}-03-01"
                if form == "20-F"
                else (pd.Timestamp(end) + pd.Timedelta(days=30)).strftime(
                    "%Y-%m-%d"
                )
            )
            revenue.append(_fact(value, start, end, filed, form))
            income.append(_fact(value / 10, start, end, filed, form))
    return {
        "facts": {
            "ifrs-full": {
                "RevenueFromContractWithCustomerExcludingAssessedTax": {
                    "units": {revenue_unit: revenue}
                },
                "ProfitLoss": {"units": {income_unit: income}},
            }
        }
    }


def test_reconstruct_foreign_quarters_derives_ytd_and_q4():
    frame = reconstruct_foreign_quarters(_payload(), "revenue")

    assert frame["end"].dt.strftime("%Y-%m-%d").tolist() == [
        "2023-03-31",
        "2023-06-30",
        "2023-09-30",
        "2023-12-31",
        "2024-03-31",
        "2024-06-30",
        "2024-09-30",
        "2024-12-31",
    ]
    assert frame["value"].tolist() == [
        10.0, 20.0, 30.0, 40.0, 10.0, 20.0, 30.0, 40.0
    ]
    assert frame["source"].tolist() == [
        "explicit", "derived_ytd", "derived_ytd", "derived_q4",
        "explicit", "derived_ytd", "derived_ytd", "derived_q4",
    ]


def test_foreign_payload_requires_eight_same_currency_paired_quarters():
    result = diagnose_foreign_payload("SAFE", 123, _payload())

    assert result["diagnostic_status"] == "PASS_DIAGNOSTIC_ONLY"
    assert result["eligible_for_parser_research"] is True
    assert result["selected_currency"] == "EUR"
    assert result["longest_continuous_paired_quarters"] == 8


def test_foreign_payload_rejects_cross_currency_pairing():
    result = diagnose_foreign_payload(
        "MIXED", 456, _payload(revenue_unit="EUR", income_unit="USD")
    )

    assert result["diagnostic_status"] == "NO_COMMON_CURRENCY"
    assert result["eligible_for_parser_research"] is False


def test_foreign_payload_rejects_late_annual_comparatives():
    payload = _payload()
    for fact in payload["facts"]["ifrs-full"].values():
        for rows in fact["units"].values():
            for row in rows:
                row["filed"] = (
                    pd.Timestamp(row["end"]) + pd.Timedelta(days=200)
                ).strftime("%Y-%m-%d")

    result = diagnose_foreign_payload("LATE", 789, payload)

    assert result["paired_quarter_count"] == 8
    assert result["timely_paired_quarter_count"] == 0
    assert (
        result["diagnostic_status"]
        == "LESS_THAN_8_TIMELY_CONTINUOUS_PAIRED_QUARTERS"
    )


def test_foreign_quarters_convert_to_point_in_time_schema():
    frame = foreign_quarters_to_point_in_time(
        "safe", _payload(), "2026-07-31T12:00:00Z"
    )

    assert set(frame["ticker"]) == {"SAFE"}
    assert set(frame["metric"]) == {"revenue", "net_income"}
    assert frame["concept"].str.startswith("foreign_").all()
    assert frame["fetched_at"].eq(pd.Timestamp("2026-07-31")).all()


def test_foreign_quarters_conversion_keeps_only_selected_currency():
    frame = foreign_quarters_to_point_in_time(
        "mixed",
        _payload(revenue_unit="EUR", income_unit="USD"),
        "2026-07-31",
        selected_currency="EUR",
    )

    assert set(frame["metric"]) == {"revenue"}


def test_foreign_payload_rejects_unverified_concept_switch():
    payload = _payload()
    revenue = payload["facts"]["ifrs-full"].pop(
        "RevenueFromContractWithCustomerExcludingAssessedTax"
    )
    rows = revenue["units"]["EUR"]
    payload["facts"]["ifrs-full"]["Revenue"] = {
        "units": {"EUR": rows[:4]}
    }
    payload["facts"]["ifrs-full"]["RevenueFromContractsWithCustomers"] = {
        "units": {"EUR": rows[4:]}
    }

    result = diagnose_foreign_payload("SWITCH", 999, payload)

    assert result["longest_continuous_timely_paired_quarters"] == 8
    assert result["unverified_concept_transition_count"] == 1
    assert result["diagnostic_status"] == "UNVERIFIED_CONCEPT_TRANSITION"
