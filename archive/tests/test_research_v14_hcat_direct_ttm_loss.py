from copy import deepcopy
import hashlib

import pandas as pd
import pytest

from scripts import research_v14_hcat_direct_ttm_loss as hcat
from src.financial.quarterly_fundamentals import quarterly_profit_ttm_snapshot


def _xml(source_id: str, *, delta: int = 0) -> bytes:
    contexts = []
    facts = []
    for index, (start, end, value) in enumerate(hcat.SOURCES[source_id]["facts"]):
        context = f"D{index}"
        contexts.append(
            f'<context id="{context}"><entity><identifier scheme="cik">1636422'
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


def _install_sources(monkeypatch):
    sources = deepcopy(hcat.SOURCES)
    downloads = {}
    for source_id, source in sources.items():
        raw = _xml(source_id)
        source["sha256"] = hashlib.sha256(raw).hexdigest()
        downloads[source["url"]] = raw
    monkeypatch.setattr(hcat, "SOURCES", sources)
    monkeypatch.setattr(hcat, "_download", lambda url: downloads[url])


def test_direct_ttm_arithmetic_and_signal_snapshot() -> None:
    assert hcat.TTM_NET_INCOME == -86_265_000
    facts = hcat.strict_quarterly_facts()
    facts["fiscal_end"] = pd.to_datetime(facts["fiscal_end"])
    facts["available_date"] = pd.to_datetime(facts["available_date"])
    snapshot = quarterly_profit_ttm_snapshot(
        facts, pd.Timestamp(hcat.SIGNAL), maximum_age_days=150
    )
    assert snapshot.loc["HCAT", "net_income_ttm"] == -86_265_000
    assert snapshot.loc["HCAT", "financial_age_days"] == 80


def test_source_sha_and_exact_operands_fail_closed(monkeypatch) -> None:
    source_id = "fy2019"
    raw = _xml(source_id)
    sources = deepcopy(hcat.SOURCES)
    sources[source_id]["sha256"] = hashlib.sha256(raw).hexdigest()
    monkeypatch.setattr(hcat, "SOURCES", sources)
    assert hcat.verify_source(source_id, raw) == 1

    changed = _xml(source_id, delta=1)
    sources[source_id]["sha256"] = hashlib.sha256(changed).hexdigest()
    monkeypatch.setattr(hcat, "SOURCES", sources)
    with pytest.raises(ValueError, match="expected .* got"):
        hcat.verify_source(source_id, changed)


def test_fixture_build_locks_two_sources_and_remains_blocked(tmp_path, monkeypatch) -> None:
    _install_sources(monkeypatch)
    audit = tmp_path / "audit.json"
    audit.write_text("{}\n")
    report = hcat.build(tmp_path / "package", audit)
    assert report["formal_financials_modified"] is False
    assert report["release_status"] == "BLOCKED"
    assert report["audit_binding"]["missing_observation_count"] == 1
    assert sum(
        source["verified_fact_count"] for source in report["evidence"]["sources"]
    ) == 3


def test_post_signal_annual_filing_is_not_in_fact_accession() -> None:
    later = hcat.REJECTED_LATER_FILINGS["0001636422-21-000026"]
    assert later["filed"] > hcat.SIGNAL
    assert "0001636422-21-000026" not in hcat.strict_quarterly_facts().loc[
        0, "accession"
    ]
