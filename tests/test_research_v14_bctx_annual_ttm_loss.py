import hashlib

import pandas as pd
import pytest

from scripts import research_v14_bctx_annual_ttm_loss as bctx
from src.financial.quarterly_fundamentals import quarterly_profit_ttm_snapshot


def _payload():
    return (
        "<html>International Financial Reporting Standards "
        "Consolidated Statements of Operations and Comprehensive Loss "
        "Expressed in US Dollars Loss for the Year (428,334) (4,024,536) "
        "(4,712,789)</html>"
    ).encode()


def test_verify_source_locks_audited_ifrs_loss():
    payload = _payload()
    evidence = bctx.verify_source(
        payload, expected_sha256=hashlib.sha256(payload).hexdigest()
    )
    assert evidence["net_income_ttm"] == -428_334
    assert evidence["profit_semantics"] == "IFRS consolidated Loss for the Year"


def test_verify_source_rejects_changed_payload():
    with pytest.raises(ValueError, match="SHA mismatch"):
        bctx.verify_source(_payload(), expected_sha256="0" * 64)


def test_annual_loss_only_applies_after_filing():
    facts = pd.DataFrame([{
        "ticker": "BCTX",
        "fiscal_end": pd.Timestamp("2021-07-31"),
        "available_date": pd.Timestamp("2021-11-16"),
        "metric": "net_income_ttm",
        "value": -428_334.0,
    }])
    august = quarterly_profit_ttm_snapshot(
        facts, pd.Timestamp("2021-08-31"), maximum_age_days=150
    )
    november = quarterly_profit_ttm_snapshot(
        facts, pd.Timestamp("2021-11-30"), maximum_age_days=150
    )
    assert "BCTX" not in august.index
    assert november.loc["BCTX", "net_income_ttm"] == -428_334.0
    assert november.loc["BCTX", "financial_age_days"] == 14


def test_real_build_writes_one_direct_fact(tmp_path, monkeypatch):
    payload = _payload()
    patched_source = {**bctx.SOURCE, "sha256": hashlib.sha256(payload).hexdigest()}
    monkeypatch.setattr(bctx, "SOURCE", patched_source)
    monkeypatch.setattr(bctx, "_download", lambda url: payload)
    audit = tmp_path / "audit.json"
    audit.write_text("{}\n", encoding="utf-8")
    report = bctx.build(output_dir=tmp_path / "out", audit_path=audit)
    facts = pd.read_csv(tmp_path / "out" / "strict_quarterly_facts.csv")
    assert report["formal_financials_modified"] is False
    assert report["audit_binding"]["unresolved_observation_count"] == 1
    assert facts.loc[0, "metric"] == "net_income_ttm"
    assert facts.loc[0, "value"] == -428_334.0
