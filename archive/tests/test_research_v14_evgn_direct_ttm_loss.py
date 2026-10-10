import hashlib

import pandas as pd
import pytest

from scripts import research_v14_evgn_direct_ttm_loss as evgn
from src.financial.quarterly_fundamentals import quarterly_profit_ttm_snapshot


def _payload():
    return (
        "<html>CONDENSED CONSOLIDATED INTERIM STATEMENTS OF PROFIT OR LOSS "
        "U.S. dollars in thousands Nine months ended September 30 "
        "Unaudited Audited Loss $ (17,443) $ (12,423) $ (5,411) $ (4,528) "
        "$ (19,115)</html>"
    ).encode()


def test_verify_source_locks_exact_9m_ttm_operands():
    payload = _payload()
    result = evgn.verify_source(
        payload, expected_sha256=hashlib.sha256(payload).hexdigest()
    )
    assert result["net_income_ttm_usd_thousands"] == -24_135
    assert result["formula"] == "FY2019 - 9M2019 + 9M2020"


def test_verify_source_rejects_changed_payload():
    with pytest.raises(ValueError, match="SHA mismatch"):
        evgn.verify_source(_payload(), expected_sha256="0" * 64)


def test_direct_ttm_loss_is_current_at_signal():
    facts = pd.DataFrame([{
        "ticker": "EVGN",
        "fiscal_end": pd.Timestamp("2020-09-30"),
        "available_date": pd.Timestamp("2020-11-18"),
        "metric": "net_income_ttm",
        "value": -24_135_000.0,
    }])
    result = quarterly_profit_ttm_snapshot(
        facts, pd.Timestamp("2021-01-29"), maximum_age_days=150
    )
    assert result.loc["EVGN", "net_income_ttm"] == -24_135_000.0
    assert result.loc["EVGN", "financial_age_days"] == 72


def test_real_build_writes_direct_loss(tmp_path, monkeypatch):
    payload = _payload()
    patched_source = {**evgn.SOURCE, "sha256": hashlib.sha256(payload).hexdigest()}
    monkeypatch.setattr(evgn, "SOURCE", patched_source)
    monkeypatch.setattr(evgn, "_download", lambda url: payload)
    audit = tmp_path / "audit.json"
    audit.write_text("{}\n", encoding="utf-8")
    result = evgn.build(output_dir=tmp_path / "out", audit_path=audit)
    facts = pd.read_csv(tmp_path / "out" / "strict_quarterly_facts.csv")
    assert result["formal_financials_modified"] is False
    assert facts.loc[0, "metric"] == "net_income_ttm"
    assert facts.loc[0, "value"] == -24_135_000.0
