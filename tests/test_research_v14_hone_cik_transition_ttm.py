from copy import deepcopy
import hashlib

import pandas as pd
import pytest

import scripts.research_v14_hone_cik_transition_ttm as hone
from scripts.research_v14_hone_cik_transition_ttm import (
    AUDIT_OBSERVATIONS,
    AVAILABLE_DATE,
    NEW_CIK,
    OLD_CIK,
    OUTPUT_COLUMNS,
    REJECTED_LATER_FILINGS,
    SOURCE_DOCUMENTS,
    SOURCE_EXPECTED_FACTS,
    build,
    exact_ttm_evidence,
    parse_consolidated_usd_facts,
    strict_quarterly_facts,
    validate_source_bytes,
)
from src.financial.quarterly_fundamentals import quarterly_growth_snapshot


EXPECTED = {
    "prior_revenue_ttm_usd": 133_272_000,
    "current_revenue_ttm_usd": 162_003_000,
    "prior_net_income_ttm_usd": 12_876_000,
    "current_net_income_ttm_usd": 14_072_000,
}


def _fixture_xml(source_id: str, *, delta: int = 0) -> bytes:
    contexts = []
    facts = []
    context_number = 0
    for concept, expected in SOURCE_EXPECTED_FACTS[source_id].items():
        for start, end, value in expected:
            context_number += 1
            context_id = f"D{context_number}"
            contexts.append(
                f'<xbrli:context id="{context_id}"><xbrli:entity>'
                f'<xbrli:identifier scheme="http://www.sec.gov/CIK">0000000000'
                f'</xbrli:identifier></xbrli:entity><xbrli:period>'
                f'<xbrli:startDate>{start}</xbrli:startDate>'
                f'<xbrli:endDate>{end}</xbrli:endDate></xbrli:period>'
                f'</xbrli:context>'
            )
            changed = value + delta if context_number == 1 else value
            facts.append(
                f'<us-gaap:{concept} contextRef="{context_id}" unitRef="USD" '
                f'decimals="-3">{changed}</us-gaap:{concept}>'
            )
    return (
        '<xbrli:xbrl xmlns:xbrli="http://www.xbrl.org/2003/instance" '
        'xmlns:us-gaap="http://fasb.org/us-gaap/2019-01-31">'
        '<xbrli:unit id="USD"><xbrli:measure>iso4217:USD</xbrli:measure></xbrli:unit>'
        + "".join(contexts)
        + "".join(facts)
        + '</xbrli:xbrl>'
    ).encode()


def _install_sources(tmp_path, monkeypatch):
    sources = deepcopy(SOURCE_DOCUMENTS)
    downloads = {}
    for source_id, source in sources.items():
        raw = _fixture_xml(source_id)
        source["expected_sha256"] = hashlib.sha256(raw).hexdigest()
        downloads[source["url"]] = raw
    calls = []

    def fake_download(url: str) -> bytes:
        calls.append(url)
        return downloads[url]

    monkeypatch.setattr(hone, "SOURCE_DOCUMENTS", sources)
    monkeypatch.setattr(hone, "_download", fake_download)
    return sources, calls


def test_exact_ttm_bridge_arithmetic_and_comparatives() -> None:
    evidence = exact_ttm_evidence()
    for key, value in EXPECTED.items():
        assert evidence[key] == value
    assert evidence["revenue_growth"] == pytest.approx(
        (162_003_000 - 133_272_000) / 133_272_000
    )
    assert evidence["net_income_growth"] == pytest.approx(
        (14_072_000 - 12_876_000) / 12_876_000
    )
    assert evidence["comparative_matches"] == {
        "InterestIncomeExpenseNet": 62_140_000,
        "NoninterestIncome": 37_546_000,
        "NetIncomeLoss": 11_283_000,
    }


def test_strict_facts_are_one_complete_direct_growth_bundle() -> None:
    facts = strict_quarterly_facts()
    assert list(facts.columns) == OUTPUT_COLUMNS
    assert len(facts) == 4
    assert set(facts["metric"]) == {
        "revenue_ttm", "revenue_growth", "net_income_ttm", "net_income_growth"
    }
    assert set(facts["available_date"]) == {AVAILABLE_DATE}
    values = facts.set_index("metric")["value"]
    assert values["revenue_ttm"] == 162_003_000
    assert values["net_income_ttm"] == 14_072_000
    assert facts["concept"].str.contains("cik_transition_exact_ttm").all()


def test_real_signal_dates_use_only_pre_signal_bundle() -> None:
    facts = strict_quarterly_facts().copy()
    facts["fiscal_end"] = pd.to_datetime(facts["fiscal_end"])
    facts["available_date"] = pd.to_datetime(facts["available_date"])
    assert "HONE" not in quarterly_growth_snapshot(
        facts, pd.Timestamp("2019-11-06"), maximum_age_days=150
    ).index
    assert len(AUDIT_OBSERVATIONS) == 2
    for _, signal_date, expected_age in AUDIT_OBSERVATIONS:
        snapshot = quarterly_growth_snapshot(
            facts, pd.Timestamp(signal_date), maximum_age_days=150
        )
        assert snapshot.loc["HONE", "financial_age_days"] == expected_age
        assert snapshot.loc["HONE", "revenue_growth"] > 0
        assert snapshot.loc["HONE", "net_income_growth"] > 0


def test_minimal_xbrl_parser_and_source_fact_validation(monkeypatch) -> None:
    for source_id in SOURCE_DOCUMENTS:
        raw = _fixture_xml(source_id)
        parsed = parse_consolidated_usd_facts(raw)
        assert len(parsed) == 6
        sources = deepcopy(SOURCE_DOCUMENTS)
        sources[source_id]["expected_sha256"] = hashlib.sha256(raw).hexdigest()
        monkeypatch.setattr(hone, "SOURCE_DOCUMENTS", sources)
        assert validate_source_bytes(source_id, raw) == 6


def test_build_downloads_and_locks_all_three_sources(tmp_path, monkeypatch) -> None:
    sources, calls = _install_sources(tmp_path, monkeypatch)
    report = build(tmp_path)
    assert calls == [source["url"] for source in sources.values()]
    assert report["old_cik"] == OLD_CIK
    assert report["new_cik"] == NEW_CIK
    assert report["accepted_fact_count"] == 4
    assert report["resolved_audit_observation_count"] == 2
    assert sum(source["verified_fact_count"] for source in report["sources"]) == 18
    assert (tmp_path / "strict_quarterly_facts.csv").exists()
    assert (tmp_path / "manifest.json").exists()


def test_changed_source_and_post_signal_annual_are_rejected(monkeypatch) -> None:
    source_id = "old_q3_2018"
    raw = _fixture_xml(source_id, delta=1)
    sources = deepcopy(SOURCE_DOCUMENTS)
    sources[source_id]["expected_sha256"] = hashlib.sha256(raw).hexdigest()
    monkeypatch.setattr(hone, "SOURCE_DOCUMENTS", sources)
    with pytest.raises(ValueError, match="expected .* got"):
        validate_source_bytes(source_id, raw)
    later = REJECTED_LATER_FILINGS["0001558370-20-002624"]
    assert later["filed"] > max(signal for _, signal, _ in AUDIT_OBSERVATIONS)
    assert "0001558370-20-002624" not in "+".join(strict_quarterly_facts()["accession"])
