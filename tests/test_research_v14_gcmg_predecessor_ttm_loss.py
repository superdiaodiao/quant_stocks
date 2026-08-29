from __future__ import annotations

from copy import deepcopy
import hashlib
from pathlib import Path

import pandas as pd
import pytest

import scripts.research_v14_gcmg_predecessor_ttm_loss as gcmg_loss
from scripts.research_v14_gcmg_predecessor_ttm_loss import (
    AUDIT_OBSERVATIONS,
    AVAILABLE_DATE,
    EXPECTED_ATTRIBUTABLE_TTM,
    EXPECTED_BASELINE_AUDIT_SHA256,
    EXPECTED_CONSOLIDATED_NET_INCOME_TTM,
    EXPECTED_REVENUE_TTM,
    FISCAL_END,
    SIGNAL_DATE,
    SOURCE_DOCUMENTS,
    SOURCE_ROW_CHECKS,
    SOURCE_TEXT_CHECKS,
    build,
    integrate_candidate,
    predecessor_ttm_evidence,
    recovered_observations,
    strict_quarterly_facts,
    validate_source_lock,
    verify_source_values,
)
from src.financial.quarterly_fundamentals import quarterly_profit_ttm_snapshot
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


def _install_source_fixtures(monkeypatch) -> None:
    sources = deepcopy(SOURCE_DOCUMENTS)
    downloads = {}
    for source_id, source in sources.items():
        payload = _source_fixture(source_id)
        source["expected_sha256"] = hashlib.sha256(payload).hexdigest()
        downloads[gcmg_loss._source_url(source)] = payload
    monkeypatch.setattr(gcmg_loss, "SOURCE_DOCUMENTS", sources)
    monkeypatch.setattr(
        gcmg_loss, "_download_bytes", lambda url: downloads[url]
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
        "ticker": "GCMG",
        "first_missing_signal_date": SIGNAL_DATE,
        "no_raw_pit_financial_facts_signal_count": 1,
        "insufficient_growth_history_signal_count": 0,
        "stale_growth_snapshot_signal_count": 0,
    } for scenario, _age in AUDIT_OBSERVATIONS], columns=columns)


def _write_audit(path: Path, *, recovered: bool) -> str:
    _audit_frame(recovered=recovered).to_csv(path, index=False)
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_exact_predecessor_attributable_ttm_is_negative() -> None:
    evidence = predecessor_ttm_evidence()
    derived = evidence["derived"]
    assert derived["net_income_ttm_attributable_usd_thousands"] == -2_355
    assert derived["revenue_ttm_usd_thousands"] == 375_789
    assert derived["consolidated_net_income_ttm_usd_thousands"] == 8_047
    assert EXPECTED_ATTRIBUTABLE_TTM == -2_355
    assert EXPECTED_REVENUE_TTM == 375_789
    assert EXPECTED_CONSOLIDATED_NET_INCOME_TTM == 8_047
    assert evidence["financial_age_days"] == 27


def test_metric_mapping_uses_parent_attributable_not_nci_earnings() -> None:
    evidence = predecessor_ttm_evidence()
    mapping = evidence["metric_mapping"]
    assert "attributable to GCM Grosvenor" in mapping["selected_metric"]
    assert "noncontrolling interests" in mapping["selected_metric"]
    assert "allocated away" in mapping["reason"]
    assert evidence["derived"]["net_income_ttm_attributable_usd_thousands"] < 0
    assert evidence["derived"]["consolidated_net_income_ttm_usd_thousands"] > 0


def test_transaction_is_historical_cost_recapitalization_not_pro_forma() -> None:
    accounting = predecessor_ttm_evidence()["transaction_accounting"]
    assert accounting["method"] == "GAAP_RECAPITALIZATION"
    assert "continue" in accounting["continuity"]
    assert "historical cost" in accounting["continuity"]
    assert accounting["pro_forma_excluded"] is True
    assert "adjusted net income" in accounting["boundary"]


def test_direct_fact_is_pit_hidden_then_fails_positive_profit_gate() -> None:
    facts = strict_quarterly_facts().copy()
    assert len(facts) == 1
    assert facts.iloc[0]["metric"] == "net_income_ttm"
    assert facts.iloc[0]["value"] == -2_355_000
    assert facts.iloc[0]["fiscal_end"] == FISCAL_END
    assert facts.iloc[0]["available_date"] == AVAILABLE_DATE
    for column in ("fiscal_end", "available_date"):
        facts[column] = pd.to_datetime(facts[column])
    before = quarterly_profit_ttm_snapshot(
        facts, pd.Timestamp("2020-12-03"), maximum_age_days=550
    )
    assert "GCMG" not in before.index
    for age in (150, 365, 550):
        snapshot = quarterly_profit_ttm_snapshot(
            facts, pd.Timestamp(SIGNAL_DATE), maximum_age_days=age
        )
        assert snapshot.loc["GCMG", "financial_age_days"] == 27
        assert snapshot.loc["GCMG", "net_income_ttm"] == -2_355_000
    assert recovered_observations()["resolved"].all()


def test_official_source_rows_cross_check_exactly(monkeypatch) -> None:
    _install_source_fixtures(monkeypatch)
    raw = {
        source_id: _source_fixture(source_id) for source_id in SOURCE_DOCUMENTS
    }
    verified = verify_source_values(raw)
    assert len(verified) == 6
    assert {item["check_id"] for item in verified} == {
        check["check_id"]
        for checks in SOURCE_ROW_CHECKS.values() for check in checks
    }
    validate_source_lock()


def test_source_parser_rejects_attributable_operand_drift() -> None:
    raw = {
        source_id: _source_fixture(
            source_id,
            drift=("selected_actual_attributable", 2)
            if source_id == "historical_actuals_s1" else None,
        )
        for source_id in SOURCE_DOCUMENTS
    }
    with pytest.raises(RuntimeError, match="selected_actual_attributable"):
        verify_source_values(raw)


def test_source_lock_rejects_post_signal_filing() -> None:
    late = deepcopy(SOURCE_DOCUMENTS)
    late["historical_actuals_s1"]["filed"] = "2021-01-01"
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
    assert report["accepted_direct_ttm_loss_count"] == 1
    assert report["accepted_fact_count"] == 1
    assert report["resolved_audit_observation_count"] == 3
    assert report["shared_candidate_integrated"] is True
    assert report["audit_binding"]["current"]["status"] == "RECOVERED"
    assert report["formal_financials_modified"] is False
    assert report["promotion_eligible"] is False
    assert len(report["source_value_verification"]) == 6


def test_candidate_integration_replaces_only_direct_ttm_key(tmp_path) -> None:
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
        "ticker": "GCMG", "fiscal_end": FISCAL_END,
        "available_date": "2021-11-12", "metric": "revenue",
        "value": 101_746_000, "taxonomy": "us-gaap", "concept": "keep",
        "form": "10-Q", "accession": "keep", "fetched_at": "2026-08-11",
    }
    conflict = incoming.copy()
    conflict["value"] = 999.0
    pd.concat([
        pd.DataFrame([outside], columns=OUTPUT_COLUMNS), conflict
    ], ignore_index=True).to_csv(base / "quarterly.csv", index=False)

    report = integrate_candidate(
        base_dir=base, supplement_dir=supplement, output_dir=candidate
    )
    result = pd.read_csv(candidate / "quarterly.csv")
    assert report["removed_conflicting_rows"] == 1
    assert report["inserted_strict_rows"] == 1
    assert len(result) == 2
    assert not result["value"].eq(999.0).any()
    assert result.loc[result["metric"].eq("revenue"), "accession"].item() == "keep"


def test_baseline_binding_is_the_forr_checkpoint() -> None:
    assert EXPECTED_BASELINE_AUDIT_SHA256 == (
        "61187e04add06ca401a181636b575d77148c4377fbdfe362afb98c992dfc8c1f"
    )
