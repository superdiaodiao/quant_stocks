import pandas as pd
import pytest

from scripts.research_v14_integrate_sec_filing_exhibit_quarters import (
    select_manifest_recovered_rows,
)


def test_select_manifest_recovered_rows_requires_exact_bound_values():
    quarters = pd.DataFrame([
        {
            "ticker": "ZLAB",
            "fiscal_end": "2019-09-30",
            "available_date": "2020-01-21",
            "metric": "revenue",
            "value": 4_919_549.0,
            "taxonomy": "us-gaap",
            "concept": "RevenueFromContractWithCustomerExcludingAssessedTax",
            "form": "6-K",
            "accession": "right",
        },
        {
            "ticker": "ZLAB",
            "fiscal_end": "2019-09-30",
            "available_date": "2020-01-21",
            "metric": "net_income",
            "value": -65_366_947.0,
            "taxonomy": "us-gaap",
            "concept": "NetIncomeLoss",
            "form": "6-K",
            "accession": "right",
        },
    ])
    manifest = {"recovered_quarters": [{
        "ticker": "ZLAB",
        "fiscal_end": "2019-09-30",
        "available_date": "2020-01-21",
        "revenue": 4_919_549.0,
        "net_income": -65_366_947.0,
    }]}
    selected = select_manifest_recovered_rows(
        quarters, manifest, fetched_at="2026-08-12"
    )
    assert len(selected) == 2
    assert set(selected["fetched_at"]) == {"2026-08-12"}

    broken = quarters.copy()
    broken.loc[broken["metric"].eq("revenue"), "value"] = 1
    with pytest.raises(ValueError, match="value differs"):
        select_manifest_recovered_rows(
            broken, manifest, fetched_at="2026-08-12"
        )


def test_select_manifest_recovered_rows_distinguishes_pit_restatement_versions():
    quarters = pd.DataFrame([
        {
            "ticker": "KRNT",
            "fiscal_end": "2019-03-31",
            "available_date": available_date,
            "metric": metric,
            "value": value,
            "taxonomy": "us-gaap",
            "concept": concept,
            "form": "6-K",
            "accession": accession,
        }
        for available_date, accession, revenue, net_income in (
            ("2019-05-13", "original", 38_161_000.0, -1_589_000.0),
            ("2020-05-19", "restated", 38_590_000.0, -1_160_000.0),
        )
        for metric, value, concept in (
            ("revenue", revenue, "Revenues"),
            ("net_income", net_income, "NetIncomeLoss"),
        )
    ])
    manifest = {"recovered_quarters": [
        {
            "ticker": "KRNT",
            "fiscal_end": "2019-03-31",
            "available_date": "2019-05-13",
            "revenue": 38_161_000.0,
            "net_income": -1_589_000.0,
        },
        {
            "ticker": "KRNT",
            "fiscal_end": "2019-03-31",
            "available_date": "2020-05-19",
            "revenue": 38_590_000.0,
            "net_income": -1_160_000.0,
        },
    ]}
    selected = select_manifest_recovered_rows(
        quarters, manifest, fetched_at="2026-08-12"
    )
    assert len(selected) == 4
    assert set(selected["accession"]) == {"original", "restated"}


def test_select_manifest_recovered_rows_accepts_metric_specific_fact():
    quarters = pd.DataFrame([{
        "ticker": "TTEK",
        "fiscal_end": "2018-09-30",
        "available_date": "2018-11-16",
        "metric": "revenue",
        "value": 739_343_000.0,
        "taxonomy": "us-gaap",
        "concept": "Revenues",
        "form": "10-K",
        "accession": "0000831641-18-000097",
    }])
    manifest = {"recovered_facts": [{
        "ticker": "TTEK",
        "fiscal_end": "2018-09-30",
        "available_date": "2018-11-16",
        "metric": "revenue",
        "value": 739_343_000.0,
    }]}
    selected = select_manifest_recovered_rows(
        quarters, manifest, fetched_at="2026-08-13"
    )
    assert selected[["ticker", "metric", "value"]].to_dict("records") == [{
        "ticker": "TTEK", "metric": "revenue", "value": 739_343_000.0,
    }]

    broken = {"recovered_facts": [{**manifest["recovered_facts"][0],
                                    "value": 1.0}]}
    with pytest.raises(ValueError, match="fact value differs"):
        select_manifest_recovered_rows(
            quarters, broken, fetched_at="2026-08-13"
        )
