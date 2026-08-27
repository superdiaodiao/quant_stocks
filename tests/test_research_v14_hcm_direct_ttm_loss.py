from copy import deepcopy
import gzip
import hashlib

import pandas as pd
import pytest

import scripts.research_v14_hcm_direct_ttm_loss as hcm_audit
from scripts.research_v14_hcm_direct_ttm_loss import (
    AUDIT_OBSERVATIONS,
    EXPECTED_TTM,
    INDEX_SOURCES,
    NONFINANCIAL_6K_DOCUMENTS,
    SOURCES,
    _index_rows,
    _prepare_negative_evidence,
    _verify_nonfinancial_6k,
    rejected_derivations,
    resolve_audit_observations,
    validate_source_lock,
    validate_unrecoverable_conclusion,
)
from src.financial.quarterly_fundamentals import quarterly_profit_ttm_snapshot


def test_hcm_direct_ttm_values_use_exact_annual_and_half_year_math() -> None:
    assert SOURCES["2020_h1"]["filed"] == "2020-07-30"
    assert SOURCES["2021_h1"]["filed"] == "2021-07-28"
    assert EXPECTED_TTM["2020-06-30"] == -110_349_000.0
    assert EXPECTED_TTM["2021-06-30"] == -178_433_000.0


def test_financial_and_nonfinancial_sec_sources_are_sha_locked() -> None:
    validate_source_lock()
    assert len(NONFINANCIAL_6K_DOCUMENTS) == 22
    assert len({source["accession"] for source in NONFINANCIAL_6K_DOCUMENTS}) == 22
    assert {
        source["expected_sha256"]
        for source in SOURCES.values()
    } == {
        "857b5ddb202e211747b390ca0bf511c633590875e9d45843b8b64dd497620b4d",
        "6c1aa5082a56f077ba949a421c510a357806c9815227f2cb3e88a9bdcab777e7",
        "80de0a6be6d24a049653956dac1ab835940ad78bbf06c02e002afa473c0b0b3d",
        "f0dc27d13883d9ef56eb7763b3b74bc5dc45ebab2312aea474ff87d66d7a7bca",
    }
    assert {
        source["expected_content_sha256"]
        for source in INDEX_SOURCES.values()
    } == {
        "9abd019518757e7bdd145d056f41f627a973509c5a8e4d275bef91f4d7f60b0a",
        "c764de83dd6c95984fbcebaa8993a7ca9d11816063c67151c28f4031eaccefb4",
        "54c3877f050abca82f2b4089d04522e526f5d44ac3d22032bd6b41edc155014f",
    }


def test_two_age150_observations_are_source_exhausted_not_filled() -> None:
    assert AUDIT_OBSERVATIONS == (
        ("liq2000000-age150-growth", "2020-12-31", 150),
        ("liq2000000-age150-growth", "2021-01-29", 150),
    )
    observations = resolve_audit_observations()
    assert observations["financial_age_days"].tolist() == [154, 183]
    assert not observations["resolved"].any()
    assert set(observations["decision"]) == {
        "unrecoverable_six_month_reporting_cadence"
    }
    rejected = rejected_derivations()
    assert rejected[1]["available_date"] == "2021-03-04"
    assert all(item["rejected"] for item in rejected)
    validate_unrecoverable_conclusion()


def test_index_parser_keeps_only_hcm_6k_rows_inside_pit_window() -> None:
    content = "\n".join([
        "CIK|Company Name|Form Type|Date Filed|Filename",
        "1648257|HCM|6-K|2020-07-30|edgar/data/1648257/0001104659-20-088202.txt",
        "1648257|HCM|20-F|2021-03-04|edgar/data/1648257/0001104659-21-031897.txt",
        "1648257|HCM|6-K|2021-02-03|edgar/data/1648257/0001648257-21-000007.txt",
        "123|Other|6-K|2020-12-31|edgar/data/123/other.txt",
    ]).encode()
    rows = _index_rows(content)
    assert rows == [{
        "cik": 1_648_257,
        "company": "HCM",
        "form": "6-K",
        "filed": "2020-07-30",
        "accession": "0001104659-20-088202",
        "filename": "edgar/data/1648257/0001104659-20-088202.txt",
    }]


def test_negative_evidence_reconciles_complete_index_and_topics(
    tmp_path, monkeypatch
) -> None:
    filings = tuple(deepcopy(source) for source in NONFINANCIAL_6K_DOCUMENTS)
    contents = {source_id: [] for source_id in INDEX_SOURCES}
    contents["2020_q3"].append(
        "1648257|HCM|6-K|2020-07-30|"
        "edgar/data/1648257/0001104659-20-088202.txt"
    )
    for source in filings:
        source_id = (
            "2020_q3" if source["filed"] <= "2020-09-30"
            else "2020_q4" if source["filed"] <= "2020-12-31"
            else "2021_q1"
        )
        contents[source_id].append(
            f"1648257|HCM|6-K|{source['filed']}|"
            f"edgar/data/1648257/{source['accession']}.txt"
        )

    indexes = deepcopy(INDEX_SOURCES)
    payload_by_url = {}
    for source_id, lines in contents.items():
        content = ("\n".join(lines) + "\n").encode()
        indexes[source_id]["expected_content_sha256"] = hashlib.sha256(
            content
        ).hexdigest()
        payload_by_url[indexes[source_id]["url"]] = gzip.compress(content)
    for source in filings:
        raw = f"<html><p>{source['topic_fragment']}</p></html>".encode()
        source["expected_sha256"] = hashlib.sha256(raw).hexdigest()
        payload_by_url[source["url"]] = raw

    def fake_local_source(output_dir, spec):
        return payload_by_url[spec["url"]], tmp_path / "source", False

    monkeypatch.setattr(hcm_audit, "INDEX_SOURCES", indexes)
    monkeypatch.setattr(hcm_audit, "NONFINANCIAL_6K_DOCUMENTS", filings)
    monkeypatch.setattr(hcm_audit, "_local_source", fake_local_source)
    index_provenance, filing_provenance, inventory = (
        _prepare_negative_evidence(tmp_path)
    )
    assert len(index_provenance) == 3
    assert len(filing_provenance) == 22
    assert len(inventory) == 23
    assert inventory["classification"].value_counts().to_dict() == {
        "nonfinancial_6k": 22,
        "latest_valid_h1_financial_report": 1,
    }


def test_negative_evidence_rejects_financial_topic_drift() -> None:
    bad = dict(NONFINANCIAL_6K_DOCUMENTS[0])
    raw = b"<html><p>financial results</p></html>"
    bad["topic_fragment"] = "financial results"
    bad["expected_sha256"] = hashlib.sha256(raw).hexdigest()
    with pytest.raises(RuntimeError, match="financial topic text"):
        _verify_nonfinancial_6k(raw, bad)


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
