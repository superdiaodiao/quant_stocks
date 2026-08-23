import hashlib
from pathlib import Path

import pandas as pd
import pytest

from scripts import research_v14_clls_direct_ttm_loss as clls
from scripts.research_v14_adpt_preipo_quarters import integrate_candidate
from src.financial.quarterly_fundamentals import quarterly_profit_ttm_snapshot


def _payloads():
    annual = (
        "<html>IFRS consolidated financial statements "
        "Net income (loss) (103,683) (88,333) (115,212)</html>"
    ).encode()
    half = (
        "<html>IFRS First six months Net income (loss) (54,461) (19,290) "
        "Adjusted Net Income (Loss)</html>"
    ).encode()
    return [annual, half]


def test_verify_sources_locks_consolidated_ifrs_operands(monkeypatch):
    payloads = _payloads()
    patched_sources = tuple(
        {**source, "sha256": hashlib.sha256(payload).hexdigest()}
        for source, payload in zip(clls.SOURCES, payloads, strict=True)
    )
    monkeypatch.setattr(clls, "SOURCES", patched_sources)
    result = clls.verify_sources(payloads)
    assert result["net_income_ttm_usd_thousands"] == -80_041
    assert result["formula"] == "FY2019 - H1_2019 + H1_2020"


def test_verify_sources_rejects_changed_source(monkeypatch):
    payloads = _payloads()
    monkeypatch.setattr(
        clls,
        "SOURCES",
        tuple({**source, "sha256": "0" * 64} for source in clls.SOURCES),
    )
    with pytest.raises(ValueError, match="SHA mismatch"):
        clls.verify_sources(payloads)


def test_direct_ttm_loss_is_consumed_as_known_nonpositive(tmp_path):
    facts = pd.DataFrame([{
        "ticker": "CLLS",
        "fiscal_end": "2020-06-30",
        "available_date": "2020-08-05",
        "metric": "net_income_ttm",
        "value": -80_041_000.0,
        "taxonomy": "ifrs-full",
        "concept": "StrictDirectTTM:ProfitLoss:USD",
        "form": "20-F_PLUS_6-K_H1_DIRECT_TTM",
        "accession": "a+b",
        "fetched_at": "2026-08-24",
    }])
    facts["fiscal_end"] = pd.to_datetime(facts["fiscal_end"])
    facts["available_date"] = pd.to_datetime(facts["available_date"])
    for signal in clls.SIGNALS:
        snapshot = quarterly_profit_ttm_snapshot(
            facts, pd.Timestamp(signal), maximum_age_days=150
        )
        assert snapshot.loc["CLLS", "net_income_ttm"] == -80_041_000.0
        assert 0 <= snapshot.loc["CLLS", "financial_age_days"] <= 150


def test_real_build_and_generic_integration(tmp_path, monkeypatch):
    payloads = _payloads()
    patched_sources = tuple(
        {**source, "sha256": hashlib.sha256(payload).hexdigest()}
        for source, payload in zip(clls.SOURCES, payloads, strict=True)
    )
    monkeypatch.setattr(clls, "SOURCES", patched_sources)
    monkeypatch.setattr(clls, "_download", lambda url: payloads[
        [source["url"] for source in patched_sources].index(url)
    ])
    audit = tmp_path / "audit.json"
    audit.write_text("{}\n", encoding="utf-8")
    supplement = tmp_path / "supplement"
    report = clls.build(output_dir=supplement, audit_path=audit)
    facts = pd.read_csv(supplement / "strict_quarterly_facts.csv")
    assert report["formal_financials_modified"] is False
    assert facts.loc[0, "metric"] == "net_income_ttm"
    assert facts.loc[0, "value"] == -80_041_000.0

    base = tmp_path / "base"
    base.mkdir()
    facts.iloc[0:0].to_csv(base / "quarterly.csv", index=False)
    (base / "annual.csv").write_text("ticker,value\n", encoding="utf-8")
    (base / "manifest.json").write_text("{}\n", encoding="utf-8")
    integrated = integrate_candidate(
        base_dir=base, supplement_dir=supplement, output_dir=tmp_path / "candidate"
    )
    assert integrated["inserted_identity_rows"] == 1
