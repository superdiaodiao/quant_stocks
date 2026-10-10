from copy import deepcopy
import hashlib

import pandas as pd
import pytest

from scripts import research_v14_vff_direct_ttm_loss as vff
from scripts.research_v14_adpt_preipo_quarters import integrate_candidate
from src.financial.quarterly_fundamentals import quarterly_profit_ttm_snapshot


def _xml(source_id: str, *, delta: int = 0) -> bytes:
    contexts = []
    facts = []
    for index, (start, end, value) in enumerate(vff.SOURCES[source_id]["facts"]):
        context = f"D{index}"
        contexts.append(
            f'<context id="{context}"><entity><identifier scheme="cik">1584549'
            f'</identifier></entity><period><startDate>{start}</startDate>'
            f'<endDate>{end}</endDate></period></context>'
        )
        facts.append(
            f'<us-gaap:NetIncomeLoss contextRef="{context}" unitRef="USD">'
            f'{value + (delta if index == 0 else 0)}</us-gaap:NetIncomeLoss>'
        )
    return (
        '<xbrl xmlns:us-gaap="http://fasb.org/us-gaap/2020">'
        '<unit id="USD"><measure>iso4217:USD</measure></unit>'
        + "".join(contexts) + "".join(facts) + '</xbrl>'
    ).encode()


def _install(monkeypatch):
    sources = deepcopy(vff.SOURCES)
    downloads = {}
    for source_id, source in sources.items():
        raw = _xml(source_id)
        source["sha256"] = hashlib.sha256(raw).hexdigest()
        downloads[source["url"]] = raw
    monkeypatch.setattr(vff, "SOURCES", sources)
    monkeypatch.setattr(vff, "_download", lambda url: downloads[url])


def test_direct_ttm_loss_and_real_signal_snapshot() -> None:
    assert vff.TTM_NET_INCOME == -2_593_000
    facts = vff.strict_quarterly_facts()
    facts["fiscal_end"] = pd.to_datetime(facts["fiscal_end"])
    facts["available_date"] = pd.to_datetime(facts["available_date"])
    snapshot = quarterly_profit_ttm_snapshot(
        facts, pd.Timestamp(vff.SIGNAL), maximum_age_days=150
    )
    assert snapshot.loc["VFF", "net_income_ttm"] == -2_593_000
    assert snapshot.loc["VFF", "financial_age_days"] == 17


def test_exact_source_operands_fail_closed(monkeypatch) -> None:
    source_id = "q3_2020"
    raw = _xml(source_id)
    sources = deepcopy(vff.SOURCES)
    sources[source_id]["sha256"] = hashlib.sha256(raw).hexdigest()
    monkeypatch.setattr(vff, "SOURCES", sources)
    assert vff.verify_source(source_id, raw) == 2
    changed = _xml(source_id, delta=1)
    sources[source_id]["sha256"] = hashlib.sha256(changed).hexdigest()
    monkeypatch.setattr(vff, "SOURCES", sources)
    with pytest.raises(ValueError, match="expected .* got"):
        vff.verify_source(source_id, changed)


def test_fixture_build_and_generic_integration(tmp_path, monkeypatch) -> None:
    _install(monkeypatch)
    audit = tmp_path / "audit.json"
    audit.write_text("{}\n")
    supplement = tmp_path / "supplement"
    report = vff.build(supplement, audit)
    assert report["formal_financials_modified"] is False
    assert report["audit_binding"]["missing_observation_count"] == 1
    assert sum(source["verified_fact_count"] for source in report["evidence"]["sources"]) == 3

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


def test_post_signal_annual_is_explicitly_excluded() -> None:
    later = vff.REJECTED_LATER_FILINGS["0001564590-21-013320"]
    assert later["filed"] > vff.SIGNAL
    assert "0001564590-21-013320" not in vff.strict_quarterly_facts().loc[0, "accession"]
