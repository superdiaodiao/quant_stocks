import hashlib

import pandas as pd
import pytest

from scripts import research_v14_nesr_annual_growth as nesr
from src.financial.quarterly_fundamentals import quarterly_growth_snapshot


def _payload():
    return (
        "<html>National Energy Services Reunited Corp. and Subsidiaries "
        "CONSOLIDATED STATEMENTS OF OPERATIONS In US$ thousands Successor (NESR) "
        "Revenues $ 834,146 $ 658,385 Net income / (loss) 50,087 39,364</html>"
    ).encode()


def test_verify_source_locks_comparable_successor_years():
    payload = _payload()
    result = nesr.verify_source(
        payload, expected_sha256=hashlib.sha256(payload).hexdigest()
    )
    assert result["revenue_growth"] == pytest.approx(0.2669577830600636)
    assert result["net_income_growth"] == pytest.approx(0.2724062595264709)
    assert "Successor NESR" in result["period_comparability"]


def test_verify_source_rejects_changed_payload():
    with pytest.raises(ValueError, match="SHA mismatch"):
        nesr.verify_source(_payload(), expected_sha256="0" * 64)


def test_direct_growth_bundle_is_consumed_at_all_three_signals():
    common = {
        "ticker": "NESR",
        "fiscal_end": pd.Timestamp("2020-12-31"),
        "available_date": pd.Timestamp("2021-03-24"),
        "accession": nesr.SOURCE["accession"],
    }
    facts = pd.DataFrame([
        {**common, "metric": "revenue_ttm", "value": 834_146_000.0},
        {**common, "metric": "revenue_growth", "value": nesr.REVENUE_GROWTH},
        {**common, "metric": "net_income_ttm", "value": 50_087_000.0},
        {**common, "metric": "net_income_growth", "value": nesr.NET_INCOME_GROWTH},
    ])
    for signal in nesr.SIGNALS:
        snapshot = quarterly_growth_snapshot(
            facts, pd.Timestamp(signal), maximum_age_days=150
        )
        assert snapshot.loc["NESR", "revenue_growth"] == pytest.approx(
            nesr.REVENUE_GROWTH
        )
        assert snapshot.loc["NESR", "net_income_ttm"] == 50_087_000.0
        assert 0 <= snapshot.loc["NESR", "financial_age_days"] <= 150


def test_real_build_writes_complete_direct_bundle(tmp_path, monkeypatch):
    payload = _payload()
    patched_source = {**nesr.SOURCE, "sha256": hashlib.sha256(payload).hexdigest()}
    monkeypatch.setattr(nesr, "SOURCE", patched_source)
    monkeypatch.setattr(nesr, "_download", lambda url: payload)
    audit = tmp_path / "audit.json"
    audit.write_text("{}\n", encoding="utf-8")
    result = nesr.build(output_dir=tmp_path / "out", audit_path=audit)
    facts = pd.read_csv(tmp_path / "out" / "strict_quarterly_facts.csv")
    assert result["formal_financials_modified"] is False
    assert set(facts["metric"]) == {
        "revenue_ttm", "revenue_growth", "net_income_ttm", "net_income_growth"
    }
    assert result["audit_binding"]["missing_observation_count"] == 3
