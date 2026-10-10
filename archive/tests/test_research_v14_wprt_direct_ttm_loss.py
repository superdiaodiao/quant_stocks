from copy import deepcopy
import hashlib

import pandas as pd
import pytest

from scripts import research_v14_wprt_direct_ttm_loss as wprt
from scripts.research_v14_adpt_preipo_quarters import integrate_candidate
from src.financial.quarterly_fundamentals import quarterly_profit_ttm_snapshot


def _payloads(*, changed_interim: bool = False) -> dict[str, bytes]:
    annual = (
        '<xbrl xmlns:us-gaap="http://fasb.org/us-gaap/2019">'
        '<unit id="usd"><measure>iso4217:USD</measure></unit>'
        '<context id="FY"><entity><identifier scheme="cik">1370416</identifier>'
        '</entity><period><startDate>2019-01-01</startDate>'
        '<endDate>2019-12-31</endDate></period></context>'
        '<us-gaap:NetIncomeLoss contextRef="FY" unitRef="usd">41000'
        '</us-gaap:NetIncomeLoss></xbrl>'
    ).encode()
    value = "(11,476)" if changed_interim else "(11,477)"
    interim = (
        "<html>Consolidated statements Nine months ended September 30 "
        f"Net income (loss) for the period 822 4,987 {value} (610)</html>"
    ).encode()
    release = b"<html>Adjusted EBITDA is a non-GAAP measure</html>"
    return {"fy2019": annual, "q3_2020": interim, "q3_2020_release": release}


def _patch_sources(monkeypatch, payloads):
    sources = deepcopy(wprt.SOURCES)
    for source_id, raw in payloads.items():
        sources[source_id]["sha256"] = hashlib.sha256(raw).hexdigest()
    monkeypatch.setattr(wprt, "SOURCES", sources)
    monkeypatch.setattr(wprt, "_download", lambda url: payloads[
        next(source_id for source_id, source in sources.items() if source["url"] == url)
    ])


def test_exact_ttm_loss_and_real_signal_snapshot() -> None:
    assert wprt.TTM_NET_INCOME == -10_826_000
    facts = wprt.strict_quarterly_facts()
    facts["fiscal_end"] = pd.to_datetime(facts["fiscal_end"])
    facts["available_date"] = pd.to_datetime(facts["available_date"])
    snapshot = quarterly_profit_ttm_snapshot(
        facts, pd.Timestamp(wprt.SIGNAL), maximum_age_days=150
    )
    assert snapshot.loc["WPRT", "net_income_ttm"] == -10_826_000
    assert snapshot.loc["WPRT", "financial_age_days"] == 21


def test_source_locks_gaap_operands_and_non_gaap_exclusion(monkeypatch) -> None:
    payloads = _payloads()
    _patch_sources(monkeypatch, payloads)
    evidence = wprt.verify_sources(payloads)
    assert evidence["formula"] == "FY2019 - 9M2019 + 9M2020"
    assert evidence["operands_usd"] == {
        "fy2019": 41_000,
        "nine_month_2019": -610_000,
        "nine_month_2020": -11_477_000,
    }
    assert wprt.REJECTED_NON_GAAP_LABELS == ("Adjusted EBITDA",)


def test_changed_interim_statement_fails_closed(monkeypatch) -> None:
    payloads = _payloads(changed_interim=True)
    _patch_sources(monkeypatch, payloads)
    with pytest.raises(ValueError, match="income-statement guard"):
        wprt.verify_sources(payloads)


def test_fixture_build_and_generic_integration(tmp_path, monkeypatch) -> None:
    payloads = _payloads()
    _patch_sources(monkeypatch, payloads)
    audit = tmp_path / "audit.json"
    audit.write_text("{}\n")
    supplement = tmp_path / "supplement"
    report = wprt.build(supplement, audit)
    assert report["formal_financials_modified"] is False
    assert report["audit_binding"]["missing_observation_count"] == 3
    assert len(report["evidence"]["sources"]) == 3

    facts = pd.read_csv(supplement / "strict_quarterly_facts.csv")
    base = tmp_path / "base"
    base.mkdir()
    facts.iloc[0:0].to_csv(base / "quarterly.csv", index=False)
    (base / "annual.csv").write_text("ticker,value\n")
    (base / "manifest.json").write_text("{}\n")
    integrated = integrate_candidate(
        base_dir=base, supplement_dir=supplement, output_dir=tmp_path / "candidate"
    )
    assert integrated["inserted_identity_rows"] == 1
