import hashlib

import pandas as pd
import pytest

from scripts import research_v14_cgen_direct_ttm_loss as cgen
from src.financial.quarterly_fundamentals import quarterly_profit_ttm_snapshot


def _payloads():
    annual = (
        "<html>CONSOLIDATED STATEMENTS OF COMPREHENSIVE LOSS "
        "Net loss $ (27,337) $ (22,599) $ (37,066)</html>"
    ).encode()
    half = (
        "<html>CONSOLIDATED STATEMENTS OF COMPREHENSIVE LOSS (Unaudited) "
        "Net loss $ 13,374 $ 14,385</html>"
    ).encode()
    return [annual, half]


def test_verify_sources_locks_exact_ttm_operands(monkeypatch):
    payloads = _payloads()
    monkeypatch.setattr(cgen, "SOURCES", tuple(
        {**source, "sha256": hashlib.sha256(payload).hexdigest()}
        for source, payload in zip(cgen.SOURCES, payloads, strict=True)
    ))
    result = cgen.verify_sources(payloads)
    assert result["net_income_ttm_usd_thousands"] == -26_326
    assert result["formula"] == "FY2019 - H1_2019 + H1_2020"


def test_verify_sources_rejects_changed_payload(monkeypatch):
    payloads = _payloads()
    monkeypatch.setattr(cgen, "SOURCES", tuple(
        {**source, "sha256": "0" * 64} for source in cgen.SOURCES
    ))
    with pytest.raises(ValueError, match="SHA mismatch"):
        cgen.verify_sources(payloads)


def test_direct_ttm_loss_is_current_at_signal():
    facts = pd.DataFrame([{
        "ticker": "CGEN",
        "fiscal_end": pd.Timestamp("2020-06-30"),
        "available_date": pd.Timestamp("2020-07-30"),
        "metric": "net_income_ttm",
        "value": -26_326_000.0,
    }])
    result = quarterly_profit_ttm_snapshot(
        facts, pd.Timestamp("2020-08-31"), maximum_age_days=150
    )
    assert result.loc["CGEN", "net_income_ttm"] == -26_326_000.0
    assert result.loc["CGEN", "financial_age_days"] == 32


def test_real_build_writes_direct_loss(tmp_path, monkeypatch):
    payloads = _payloads()
    patched_sources = tuple(
        {**source, "sha256": hashlib.sha256(payload).hexdigest()}
        for source, payload in zip(cgen.SOURCES, payloads, strict=True)
    )
    monkeypatch.setattr(cgen, "SOURCES", patched_sources)
    monkeypatch.setattr(cgen, "_download", lambda url: payloads[
        [source["url"] for source in patched_sources].index(url)
    ])
    audit = tmp_path / "audit.json"
    audit.write_text("{}\n", encoding="utf-8")
    result = cgen.build(output_dir=tmp_path / "out", audit_path=audit)
    facts = pd.read_csv(tmp_path / "out" / "strict_quarterly_facts.csv")
    assert result["formal_financials_modified"] is False
    assert facts.loc[0, "metric"] == "net_income_ttm"
    assert facts.loc[0, "value"] == -26_326_000.0
