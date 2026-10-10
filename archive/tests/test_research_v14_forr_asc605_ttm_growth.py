from __future__ import annotations

from copy import deepcopy
import hashlib
from pathlib import Path

import pandas as pd
import pytest

import scripts.research_v14_forr_asc605_ttm_growth as forr_growth
from scripts.research_v14_forr_asc605_ttm_growth import (
    ACCOUNTING_STANDARD,
    AUDIT_OBSERVATIONS,
    AVAILABLE_DATE,
    DIRECT_METRICS,
    EXPECTED_BASELINE_AUDIT_SHA256,
    EXPECTED_TTM,
    FISCAL_END,
    SIGNAL_DATE,
    SOURCE_DOCUMENTS,
    SOURCE_ROW_CHECKS,
    SOURCE_TEXT_CHECKS,
    build,
    comparable_ttm_evidence,
    integrate_candidate,
    recovered_observations,
    strict_quarterly_facts,
    validate_source_lock,
    verify_source_values,
)
from src.financial.quarterly_fundamentals import quarterly_growth_snapshot
from src.io.fundamentals_update import OUTPUT_COLUMNS


def _source_fixture(source_id: str, *, drift: tuple[str, int] | None = None) -> bytes:
    paragraphs = "".join(
        f"<p>{phrase}</p>" for phrase in SOURCE_TEXT_CHECKS[source_id]
    )
    tables = []
    for check in SOURCE_ROW_CHECKS[source_id]:
        values = list(check["expected"])
        if drift is not None and check["check_id"] == drift[0]:
            values[drift[1]] += 1
        context = " | ".join(check["context"])
        cells = [f"<td>{check['row_label']}</td>"]
        for value in values:
            rendered = f"({abs(value):,})" if value < 0 else f"{value:,}"
            cells.extend(("<td>$</td>", f"<td>{rendered}</td>"))
        tables.append(
            f"<table><tr><th>{context}</th></tr><tr>"
            + "".join(cells)
            + "</tr></table>"
        )
    return ("<html>" + paragraphs + "".join(tables) + "</html>").encode()


def _install_source_fixtures(monkeypatch, *, drift=None) -> None:
    sources = deepcopy(SOURCE_DOCUMENTS)
    downloads = {}
    for source_id, source in sources.items():
        source_drift = drift if drift and drift[0] == source_id else None
        payload = _source_fixture(
            source_id,
            drift=(source_drift[1], source_drift[2]) if source_drift else None,
        )
        source["expected_sha256"] = hashlib.sha256(payload).hexdigest()
        downloads[forr_growth._source_url(source)] = payload
    monkeypatch.setattr(forr_growth, "SOURCE_DOCUMENTS", sources)
    monkeypatch.setattr(
        forr_growth, "_download_bytes", lambda url: downloads[url]
    )


def _audit_frame(*, recovered: bool) -> pd.DataFrame:
    columns = [
        "scenario", "ticker", "first_missing_signal_date",
        "no_raw_pit_financial_facts_signal_count",
        "insufficient_growth_history_signal_count",
        "stale_growth_snapshot_signal_count",
    ]
    if recovered:
        return pd.DataFrame(columns=columns)
    return pd.DataFrame([{
        "scenario": scenario,
        "ticker": "FORR",
        "first_missing_signal_date": SIGNAL_DATE,
        "no_raw_pit_financial_facts_signal_count": 0,
        "insufficient_growth_history_signal_count": 0,
        "stale_growth_snapshot_signal_count": 1,
    } for scenario, _age in AUDIT_OBSERVATIONS], columns=columns)


def _write_audit(path: Path, *, recovered: bool) -> str:
    _audit_frame(recovered=recovered).to_csv(path, index=False)
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_comparable_ttm_arithmetic_uses_one_accounting_basis() -> None:
    evidence = comparable_ttm_evidence()
    revenue = evidence["derived"]["revenue"]
    profit = evidence["derived"]["net_income"]

    assert evidence["accounting_standard"] == ACCOUNTING_STANDARD
    assert revenue["prior_ttm_usd_thousands"] == 330_742
    assert revenue["current_ttm_usd_thousands"] == 350_857
    assert revenue["growth"] == pytest.approx(20_115 / 330_742)
    assert profit["prior_ttm_usd_thousands"] == 18_837
    assert profit["current_ttm_usd_thousands"] == 13_073
    assert profit["growth"] == pytest.approx(-5_764 / 18_837)
    assert evidence["financial_age_days"] == 114
    assert EXPECTED_TTM["revenue"] == {"prior": 330_742, "current": 350_857}


def test_direct_facts_are_one_complete_source_coherent_bundle() -> None:
    facts = strict_quarterly_facts()
    assert len(facts) == 4
    assert set(facts["metric"]) == DIRECT_METRICS
    assert set(facts["fiscal_end"]) == {FISCAL_END}
    assert set(facts["available_date"]) == {AVAILABLE_DATE}
    assert facts["accession"].nunique() == 1
    values = facts.set_index("metric")["value"]
    assert values["revenue_ttm"] == 350_857_000
    assert values["net_income_ttm"] == 13_073_000
    assert values["revenue_growth"] == pytest.approx(20_115 / 330_742)
    assert values["net_income_growth"] == pytest.approx(-5_764 / 18_837)


def test_bundle_is_pit_hidden_before_filing_and_resolves_three_scenarios() -> None:
    facts = strict_quarterly_facts().copy()
    for column in ("fiscal_end", "available_date"):
        facts[column] = pd.to_datetime(facts[column])
    before = quarterly_growth_snapshot(
        facts, pd.Timestamp("2018-11-05"), maximum_age_days=550
    )
    assert "FORR" not in before.index
    for age in (150, 365, 550):
        snapshot = quarterly_growth_snapshot(
            facts, pd.Timestamp(SIGNAL_DATE), maximum_age_days=age
        )
        assert snapshot.loc["FORR", "fiscal_end"] == pd.Timestamp(FISCAL_END)
        assert snapshot.loc["FORR", "financial_age_days"] == 114
        assert snapshot.loc["FORR", "revenue_growth"] > 0
        assert snapshot.loc["FORR", "net_income_ttm"] > 0
    observations = recovered_observations()
    assert len(observations) == 3
    assert observations["resolved"].all()
    assert set(observations["financial_age_days"]) == {114}


def test_accounting_transition_is_explicit_and_cross_basis_values_are_excluded() -> None:
    audit = comparable_ttm_evidence()["accounting_policy_comparability"]
    assert audit["status"] == "EXACT_ISSUER_DISCLOSED_CONSTANT_ASC605_BASIS"
    assert "modified retrospective" in audit["transition"]
    assert "2017 remained ASC 605" in audit["transition"]
    assert "as if the previous guidance" in audit["normalization"]
    assert "258.992m" in audit["excluded"]
    assert "no cross-basis splice" in audit["excluded"]


def test_official_source_lock_and_exact_row_parser(monkeypatch) -> None:
    _install_source_fixtures(monkeypatch)
    raw = {
        source_id: _source_fixture(source_id) for source_id in SOURCE_DOCUMENTS
    }
    verified = verify_source_values(raw)
    assert len(verified) == 12
    assert {item["check_id"] for item in verified} == {
        check["check_id"]
        for checks in SOURCE_ROW_CHECKS.values() for check in checks
    }
    validate_source_lock()


def test_source_parser_rejects_operand_drift() -> None:
    raw = {
        source_id: _source_fixture(
            source_id,
            drift=("previous_guidance_m9_revenue", 1)
            if source_id == "2018_q3_10q" else None,
        )
        for source_id in SOURCE_DOCUMENTS
    }
    with pytest.raises(RuntimeError, match="previous_guidance_m9_revenue"):
        verify_source_values(raw)


def test_source_lock_rejects_post_signal_filing() -> None:
    late = deepcopy(SOURCE_DOCUMENTS)
    late["2018_q3_10q"]["filed"] = "2019-03-01"
    with pytest.raises(ValueError, match="violates the PIT cutoff"):
        validate_source_lock(late)


def test_build_binds_baseline_and_recovered_audit(tmp_path, monkeypatch) -> None:
    _install_source_fixtures(monkeypatch)
    baseline = tmp_path / "baseline.csv"
    current = tmp_path / "current.csv"
    baseline_sha = _write_audit(baseline, recovered=False)
    current_sha = _write_audit(current, recovered=True)

    report = build(
        tmp_path / "evidence",
        baseline_audit_path=baseline,
        expected_baseline_audit_sha256=baseline_sha,
        current_audit_path=current,
        expected_current_audit_sha256=current_sha,
    )
    assert report["accepted_direct_growth_package_count"] == 1
    assert report["accepted_fact_count"] == 4
    assert report["resolved_audit_observation_count"] == 3
    assert report["shared_candidate_integrated"] is True
    assert report["audit_binding"]["current"]["status"] == "RECOVERED"
    assert report["formal_financials_modified"] is False
    assert report["promotion_eligible"] is False
    assert report["release_status"] == "BLOCKED"
    assert len(report["source_value_verification"]) == 12


def test_candidate_integration_replaces_only_the_bounded_direct_keyspace(
    tmp_path,
) -> None:
    base = tmp_path / "base"
    supplement = tmp_path / "supplement"
    candidate = tmp_path / "candidate"
    base.mkdir()
    supplement.mkdir()
    pd.DataFrame(columns=OUTPUT_COLUMNS).to_csv(base / "annual.csv", index=False)
    (base / "manifest.json").write_text("{}\n", encoding="utf-8")
    (supplement / "manifest.json").write_text("{}\n", encoding="utf-8")
    incoming = strict_quarterly_facts()
    incoming.to_csv(supplement / "strict_quarterly_facts.csv", index=False)
    outside = {
        "ticker": "FORR", "fiscal_end": "2018-09-30",
        "available_date": "2018-11-06", "metric": "revenue",
        "value": 84_890_000, "taxonomy": "us-gaap", "concept": "keep",
        "form": "10-Q", "accession": "keep", "fetched_at": "2026-08-11",
    }
    conflicts = incoming.copy()
    conflicts["value"] = -999.0
    base_rows = pd.concat([
        pd.DataFrame([outside], columns=OUTPUT_COLUMNS), conflicts
    ], ignore_index=True)
    base_rows.to_csv(base / "quarterly.csv", index=False)

    report = integrate_candidate(
        base_dir=base, supplement_dir=supplement, output_dir=candidate
    )
    result = pd.read_csv(candidate / "quarterly.csv")
    assert report["removed_conflicting_rows"] == 4
    assert report["inserted_strict_rows"] == 4
    assert len(result) == 5
    assert not result["value"].eq(-999.0).any()
    kept = result.loc[result["metric"].eq("revenue")]
    assert len(kept) == 1
    assert kept.iloc[0]["accession"] == "keep"
    assert (candidate / "annual.csv").read_bytes() == (base / "annual.csv").read_bytes()


def test_baseline_binding_is_the_latest_dkng_checkpoint() -> None:
    assert EXPECTED_BASELINE_AUDIT_SHA256 == (
        "31f84e8feb0e9af45dbd8c680b565f3231c2aa35003b41e05bd38f82f9ee18d9"
    )
