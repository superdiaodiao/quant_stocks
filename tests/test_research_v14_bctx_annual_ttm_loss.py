import hashlib

import pandas as pd
import pytest

from scripts import research_v14_bctx_annual_ttm_loss as bctx
from src.financial.quarterly_fundamentals import (
    quarterly_growth_snapshot,
    quarterly_profit_ttm_snapshot,
)


def _source_payloads():
    return {
        "fy2020_audited_cad": (
            "<html>International Financial Reporting Standards as issued by the "
            "International Accounting Standards Board expressed in Canadian "
            "dollars Loss For The Period (517,601) (4,944,221) (5,789,662) "
            "(5,412,663)</html>"
        ).encode(),
        "interim2020_cad": (
            "<html>Condensed Interim Consolidated Statements of Operations and "
            "Comprehensive Loss For the Three and Nine Months Ended April 30, "
            "2020 and 2019 Expressed in Canadian Dollars Loss For The Period "
            "(700,649) (1,522,646) (4,248,670) (4,094,995)</html>"
        ).encode(),
        "interim2021_cad": (
            "<html>Interim Consolidated Statements of Operations and "
            "Comprehensive Profit (Loss) For the Three and Nine Months Ended "
            "April 30, 2021 and 2020 Expressed in Canadian Dollars As the "
            "Company has no revenues Profit (Loss) For The Period 3,623,642 "
            "(700,649) 2,816,193 (4,248,670)</html>"
        ).encode(),
        "fy2021_audited_usd_late": (
            "<html>International Financial Reporting Standards Consolidated "
            "Statements of Operations and Comprehensive Loss Expressed in US "
            "Dollars Loss for the Year (428,334) (4,024,536) "
            "(4,712,789)</html>"
        ).encode(),
    }


def _sha_map(payloads):
    return {
        source_id: hashlib.sha256(payload).hexdigest()
        for source_id, payload in payloads.items()
    }


def _audit(path, classification="no_raw_pit_financial_facts"):
    rows = []
    reason_columns = (
        "no_raw_pit_financial_facts",
        "insufficient_growth_history",
        "stale_growth_snapshot",
    )
    for scenario, _ in bctx.AUDIT_OBSERVATIONS:
        row = {
            "scenario": scenario,
            "ticker": "BCTX",
            "missing_signal_count": 1,
            "first_missing_signal_date": bctx.SIGNAL_DATE,
            "last_missing_signal_date": bctx.SIGNAL_DATE,
        }
        row.update({f"{name}_signal_count": int(name == classification) for name in reason_columns})
        rows.append(row)
    pd.DataFrame(rows).to_csv(path, index=False)
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_verify_sources_lock_all_accounting_rows():
    payloads = _source_payloads()
    evidence = bctx.verify_sources(payloads, expected_sha256s=_sha_map(payloads))
    assert [item["source_id"] for item in evidence] == [
        source["source_id"] for source in bctx.SOURCES
    ]
    assert len(evidence) == 4


def test_verify_sources_reject_changed_payload():
    payloads = _source_payloads()
    expected = _sha_map(payloads)
    payloads["interim2021_cad"] += b" changed"
    with pytest.raises(ValueError, match="source SHA mismatch"):
        bctx.verify_sources(payloads, expected_sha256s=expected)


def test_exact_pre_signal_ttm_math_and_zero_denominator():
    derivation = bctx.ttm_derivation()
    assert bctx.CURRENT_NET_INCOME_TTM_CAD == 2_120_642
    assert bctx.PRIOR_NET_INCOME_TTM_CAD == -5_943_337
    assert bctx.NET_INCOME_GROWTH == pytest.approx(1.3568099873858743)
    assert derivation["revenue"] == {
        "current_ttm": 0,
        "prior_ttm": 0,
        "growth": None,
        "reason": "0/0 comparison is undefined",
    }


def test_pre_signal_facts_improve_profit_but_not_growth_bundle():
    facts = bctx._direct_facts().copy()
    facts["fiscal_end"] = pd.to_datetime(facts["fiscal_end"])
    facts["available_date"] = pd.to_datetime(facts["available_date"])
    august_profit = quarterly_profit_ttm_snapshot(
        facts, pd.Timestamp("2021-08-31"), maximum_age_days=150
    )
    august_growth = quarterly_growth_snapshot(
        facts, pd.Timestamp("2021-08-31"), maximum_age_days=150
    )
    assert august_profit.loc["BCTX", "net_income_ttm"] == 2_120_642
    assert august_profit.loc["BCTX", "financial_age_days"] == 62
    assert "BCTX" not in august_growth.index
    assert set(facts["metric"]) == {
        "net_income_ttm", "net_income_growth", "revenue_ttm"
    }
    assert "revenue_growth" not in set(facts["metric"])


def test_annual_loss_only_applies_after_filing():
    facts = bctx._direct_facts().copy()
    facts["fiscal_end"] = pd.to_datetime(facts["fiscal_end"])
    facts["available_date"] = pd.to_datetime(facts["available_date"])
    november = quarterly_profit_ttm_snapshot(
        facts, pd.Timestamp("2021-11-30"), maximum_age_days=150
    )
    assert november.loc["BCTX", "net_income_ttm"] == -428_334
    assert november.loc["BCTX", "financial_age_days"] == 14


@pytest.mark.parametrize(
    "classification",
    ["no_raw_pit_financial_facts", "insufficient_growth_history"],
)
def test_build_is_deterministic_and_binds_current_audit(
    tmp_path, monkeypatch, classification
):
    payloads = _source_payloads()
    source_by_url = {
        source["url"]: payloads[source["source_id"]] for source in bctx.SOURCES
    }
    patched_sources = tuple(
        {
            **source,
            "expected_sha256": hashlib.sha256(
                payloads[source["source_id"]]
            ).hexdigest(),
        }
        for source in bctx.SOURCES
    )
    monkeypatch.setattr(bctx, "SOURCES", patched_sources)
    monkeypatch.setattr(bctx, "SOURCE", patched_sources[-1])
    monkeypatch.setattr(bctx, "_download", lambda url: source_by_url[url])
    audit = tmp_path / "priorities.csv"
    audit_sha = _audit(audit, classification=classification)
    output_dir = tmp_path / "out"
    report = bctx.build(
        output_dir=output_dir,
        audit_path=audit,
        expected_audit_sha256=audit_sha,
    )
    first_manifest_sha = hashlib.sha256(
        (output_dir / "manifest.json").read_bytes()
    ).hexdigest()
    second = bctx.build(
        output_dir=output_dir,
        audit_path=audit,
        expected_audit_sha256=audit_sha,
    )
    second_manifest_sha = hashlib.sha256(
        (output_dir / "manifest.json").read_bytes()
    ).hexdigest()
    facts = pd.read_csv(output_dir / "strict_quarterly_facts.csv")
    observations = pd.read_csv(output_dir / "unrecoverable_observations.csv")
    assert first_manifest_sha == second_manifest_sha
    assert report["formal_financials_modified"] is False
    assert report["accepted_pre_signal_direct_fact_count"] == 3
    assert report["audit_binding"]["observed_classification"] == classification
    assert report["outputs"]["strict_quarterly_facts"]["row_count"] == 4
    assert len(facts) == 4
    assert len(observations) == 3
    assert observations["decision"].eq(
        "unrecoverable_zero_revenue_growth_denominator"
    ).all()
    assert second["release_status"] == "BLOCKED"
