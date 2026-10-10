from copy import deepcopy
import hashlib

import pandas as pd
import pytest

from scripts import research_v14_smpl_acquisition_basis_gap as smpl


def _xml(facts: tuple[dict, ...]) -> bytes:
    contexts = []
    values = []
    for index, fact in enumerate(facts):
        context_id = f"c{index}"
        members = ""
        for member_index, member in enumerate(fact["members"]):
            dimension = (
                "us-gaap:StatementScenarioAxis"
                if member == "us-gaap:PredecessorMember"
                else "us-gaap:BusinessAcquisitionAxis"
            )
            members += (
                "<xbrli:scenario>" if member_index == 0 else ""
            ) + (
                f'<xbrldi:explicitMember dimension="{dimension}">'
                f"{member}</xbrldi:explicitMember>"
            )
        if fact["members"]:
            members += "</xbrli:scenario>"
        contexts.append(
            f'<xbrli:context id="{context_id}"><xbrli:entity>'
            '<xbrli:identifier scheme="test">1702744</xbrli:identifier>'
            f"</xbrli:entity>{members}<xbrli:period>"
            f"<xbrli:startDate>{fact['start']}</xbrli:startDate>"
            f"<xbrli:endDate>{fact['end']}</xbrli:endDate>"
            "</xbrli:period></xbrli:context>"
        )
        values.append(
            f'<us-gaap:{fact["concept"]} contextRef="{context_id}" '
            f'unitRef="USD" decimals="0">{fact["value"]}'
            f'</us-gaap:{fact["concept"]}>'
        )
    return (
        '<xbrli:xbrl xmlns:xbrli="http://www.xbrl.org/2003/instance" '
        'xmlns:xbrldi="http://xbrl.org/2006/xbrldi" '
        'xmlns:us-gaap="http://fasb.org/us-gaap/2018-01-31" '
        'xmlns:atk="http://www.thesimplygoodfoodscompany.com/20180825">'
        + "".join(contexts)
        + '<xbrli:unit id="USD"><xbrli:measure>iso4217:USD</xbrli:measure>'
        "</xbrli:unit>"
        + "".join(values)
        + "</xbrli:xbrl>"
    ).encode()


def _payloads() -> list[bytes]:
    return [
        _xml(smpl.EXPECTED_FACTS[source["source_id"]])
        for source in smpl.SOURCE_DOCUMENTS
    ]


def _patched_sources(payloads: list[bytes]) -> tuple[dict, ...]:
    return tuple(
        {**deepcopy(source), "expected_sha256": hashlib.sha256(raw).hexdigest()}
        for source, raw in zip(smpl.SOURCE_DOCUMENTS, payloads, strict=True)
    )


def test_official_sources_and_pit_dates_are_sha_locked() -> None:
    assert [source["accession"] for source in smpl.SOURCE_DOCUMENTS] == [
        "0001702744-17-000027",
        "0001702744-18-000064",
        "0001702744-19-000004",
    ]
    assert [source["expected_sha256"] for source in smpl.SOURCE_DOCUMENTS] == [
        "7063e309e74628fd7ff13064bfa4bfb80255fd3a4dbf81efc16b73f0f88b932f",
        "1424bc39e933651ea4e4decc5759311465029fd095951f795abab3b591191a2e",
        "882fad65d685e93c0fd9b5147a2b7ea6ad38624db0d11d6270c0c1dbd768b89d",
    ]
    assert max(source["filed"] for source in smpl.SOURCE_DOCUMENTS) < min(
        smpl.SIGNALS
    )
    smpl.validate_source_lock()


def test_exact_current_ttm_is_valid_but_prior_ttm_is_cross_basis() -> None:
    evidence = smpl.exact_basis_evidence()
    assert evidence["current_successor_ttm"] == {
        "fiscal_end": "2018-11-24",
        "available_date": "2019-01-03",
        "revenue_usd": 445_773_000,
        "net_income_usd": 75_494_000,
        "basis": "successor_only",
        "derivation": "FY2018 - Q1FY2018 + Q1FY2019",
    }
    prior = evidence["rejected_cross_basis_prior_ttm"]
    assert prior["revenue_usd"] == 402_955_000
    assert prior["net_income_usd"] == 1_396_000
    assert prior["basis"] == "predecessor_plus_successor"
    assert prior["would_produce_revenue_growth"] > 0.10
    assert prior["would_produce_net_income_growth"] > 0.25


def test_source_parser_preserves_predecessor_and_pro_forma_dimensions(
    monkeypatch,
) -> None:
    payloads = _payloads()
    monkeypatch.setattr(smpl, "SOURCE_DOCUMENTS", _patched_sources(payloads))
    evidence = smpl.verify_sources(payloads)
    assert evidence["basis_boundary"]["xbrl_member"] == (
        "us-gaap:PredecessorMember"
    )
    assert [item["checked_fact_count"] for item in evidence["source_checks"]] == [
        6,
        12,
        4,
    ]


def test_source_validation_rejects_sha_drift(monkeypatch) -> None:
    payloads = _payloads()
    monkeypatch.setattr(
        smpl,
        "SOURCE_DOCUMENTS",
        tuple({**source, "expected_sha256": "0" * 64} for source in smpl.SOURCE_DOCUMENTS),
    )
    with pytest.raises(RuntimeError, match="SHA-256 mismatch"):
        smpl.verify_sources(payloads)


def test_source_validation_rejects_missing_basis_fact(monkeypatch) -> None:
    payloads = _payloads()
    payloads[1] = _xml(smpl.EXPECTED_FACTS["fy2018_successor"][:-1])
    monkeypatch.setattr(smpl, "SOURCE_DOCUMENTS", _patched_sources(payloads))
    with pytest.raises(RuntimeError, match="fact changed"):
        smpl.verify_sources(payloads)


def test_all_six_audit_observations_remain_explicit_unrecoverable() -> None:
    observations = smpl.resolve_audit_observations()
    assert len(observations) == 6
    assert observations.groupby("scenario").size().eq(2).all()
    assert set(observations["financial_age_days"]) == {56, 85}
    assert not observations["resolved"].any()
    assert set(observations["decision"]) == {
        "unrecoverable_predecessor_successor_basis_split"
    }
    assert all(item["rejected"] for item in smpl.rejected_derivations())


def test_build_emits_negative_evidence_and_zero_candidate_rows(
    tmp_path, monkeypatch
) -> None:
    payloads = _payloads()
    sources = _patched_sources(payloads)
    monkeypatch.setattr(smpl, "SOURCE_DOCUMENTS", sources)
    monkeypatch.setattr(
        smpl,
        "_download",
        lambda url: payloads[[smpl._url(source) for source in sources].index(url)],
    )
    audit = tmp_path / "priorities.csv"
    pd.DataFrame([{
        "scenario": scenario,
        "ticker": "SMPL",
        "missing_signal_count": 2,
        "first_missing_signal_date": "2019-02-28",
        "last_missing_signal_date": "2019-03-29",
        "insufficient_growth_history_signal_count": 2,
    } for scenario, _ in smpl.SCENARIOS]).to_csv(audit, index=False)
    audit_sha = hashlib.sha256(audit.read_bytes()).hexdigest()

    output_dir = tmp_path / "evidence"
    report = smpl.build(output_dir, audit, audit_sha)
    accepted = pd.read_csv(output_dir / "accepted_candidate_facts.csv")
    observations = pd.read_csv(output_dir / "unrecoverable_observations.csv")
    assert accepted.empty
    assert len(observations) == 6
    assert report["candidate_rows_created"] == 0
    assert report["negative_evidence_source_locked"] is True
    assert report["formal_financials_modified"] is False
    assert report["release_status"] == "BLOCKED"
