from copy import deepcopy
import hashlib

import pandas as pd
import pytest

from scripts import research_v14_itos_zero_revenue_growth as itos


def _payloads() -> list[bytes]:
    return [
        (
            "<html>Net loss (10,680) (6,921) (23,129) (17,078)</html>"
        ).encode(),
        (
            "<html>Net loss $ (38,033) $ (22,454) We have never generated "
            "any revenue from product sales</html>"
        ).encode(),
        (
            "<html>License Revenue $ 104,271 $ — $ 104,271 $ — Net income "
            "(loss) 69,642 (10,680) 29,649 (23,129)</html>"
        ).encode("utf-8"),
    ]


def _patched_sources(payloads: list[bytes]) -> tuple[dict, ...]:
    return tuple(
        {
            **deepcopy(source),
            "expected_sha256": hashlib.sha256(raw).hexdigest(),
        }
        for source, raw in zip(itos.SOURCE_DOCUMENTS, payloads, strict=True)
    )


def test_official_sources_and_pit_times_are_sha_locked() -> None:
    assert itos.CIK == 1_808_865
    assert [source["accession"] for source in itos.SOURCE_DOCUMENTS] == [
        "0001564590-20-053269",
        "0001564590-21-015146",
        "0001564590-21-056193",
    ]
    assert [source["accepted_at"] for source in itos.SOURCE_DOCUMENTS] == [
        "2020-11-12T07:15:50Z",
        "2021-03-24T17:15:13Z",
        "2021-11-10T16:11:29Z",
    ]
    assert [source["expected_sha256"] for source in itos.SOURCE_DOCUMENTS] == [
        "71ea64e1fb1a9c09a4cb76218d33a8edfaef3c79d0349275e71d4e69a6b299d6",
        "70de615265d7e7790f7f187c476ee27fce6af06a57603b3e46bf841d947c835a",
        "4b5654544db0cce12ebb9411d2fd331a18b05ab662a4c1827ba1c95f653f80fe",
    ]
    itos.validate_source_lock()


def test_exact_ttm_math_preserves_zero_revenue_denominator() -> None:
    evidence = itos.ttm_evidence()
    assert itos.Q4_2019_NET_LOSS == -5_376_000
    assert itos.Q4_2020_NET_LOSS == -14_904_000
    assert itos.CURRENT_NET_INCOME_TTM == 14_745_000
    assert itos.PRIOR_NET_INCOME_TTM == -28_505_000
    assert itos.NET_INCOME_GROWTH == pytest.approx(1.517277670584108)
    assert evidence["revenue"] == {
        "current_ttm": 104_271_000,
        "prior_ttm": 0,
        "growth": None,
        "reason": "growth percentage is undefined when prior TTM is zero",
    }


def test_source_guards_lock_every_ttm_component(monkeypatch) -> None:
    payloads = _payloads()
    monkeypatch.setattr(itos, "SOURCE_DOCUMENTS", _patched_sources(payloads))
    evidence = itos.verify_sources(payloads)
    assert [item["guard_count"] for item in evidence["source_checks"]] == [
        1,
        2,
        2,
    ]


def test_source_validation_rejects_changed_payload(monkeypatch) -> None:
    payloads = _payloads()
    monkeypatch.setattr(itos, "SOURCE_DOCUMENTS", _patched_sources(payloads))
    payloads[2] += b" changed"
    with pytest.raises(RuntimeError, match="source SHA-256 mismatch"):
        itos.verify_sources(payloads)


def test_all_three_observations_remain_explicit_unrecoverable() -> None:
    observations = itos.resolve_audit_observations()
    assert len(observations) == 3
    assert observations["financial_age_days"].eq(51).all()
    assert observations["net_income_growth"].eq(
        itos.NET_INCOME_GROWTH
    ).all()
    assert observations["prior_revenue_ttm"].eq(0).all()
    assert observations["revenue_growth"].isna().all()
    assert not observations["resolved"].any()
    assert set(observations["decision"]) == {
        "unrecoverable_zero_revenue_denominator"
    }


def test_build_emits_negative_evidence_and_zero_candidate_rows(
    tmp_path, monkeypatch
) -> None:
    payloads = _payloads()
    sources = _patched_sources(payloads)
    monkeypatch.setattr(itos, "SOURCE_DOCUMENTS", sources)
    monkeypatch.setattr(
        itos,
        "_download",
        lambda url: payloads[[itos._url(source) for source in sources].index(url)],
    )
    audit = tmp_path / "priorities.csv"
    pd.DataFrame([{
        "scenario": scenario,
        "ticker": "ITOS",
        "missing_signal_count": 1,
        "first_missing_signal_date": itos.SIGNAL_DATE,
        "last_missing_signal_date": itos.SIGNAL_DATE,
        "insufficient_growth_history_signal_count": 1,
    } for scenario, _ in itos.SCENARIOS]).to_csv(audit, index=False)
    audit_sha = hashlib.sha256(audit.read_bytes()).hexdigest()

    output_dir = tmp_path / "evidence"
    report = itos.build(output_dir, audit, audit_sha)
    first_manifest_sha = hashlib.sha256(
        (output_dir / "manifest.json").read_bytes()
    ).hexdigest()
    second = itos.build(output_dir, audit, audit_sha)
    second_manifest_sha = hashlib.sha256(
        (output_dir / "manifest.json").read_bytes()
    ).hexdigest()
    accepted = pd.read_csv(output_dir / "accepted_candidate_facts.csv")
    observations = pd.read_csv(output_dir / "unrecoverable_observations.csv")

    assert first_manifest_sha == second_manifest_sha
    assert accepted.empty
    assert len(observations) == 3
    assert report["candidate_rows_created"] == 0
    assert report["negative_evidence_source_locked"] is True
    assert report["formal_financials_modified"] is False
    assert second["release_status"] == "BLOCKED"
