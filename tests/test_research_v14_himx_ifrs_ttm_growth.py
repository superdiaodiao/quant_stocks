from __future__ import annotations

from copy import deepcopy
import hashlib
from pathlib import Path

import pandas as pd
import pytest

import scripts.research_v14_himx_ifrs_ttm_growth as himx_growth
from scripts.research_v14_himx_ifrs_ttm_growth import (
    AUDIT_OBSERVATIONS,
    AVAILABLE_DATE,
    EXPECTED_GROWTH,
    EXPECTED_TTM_USD_THOUSANDS,
    FISCAL_END,
    SIGNAL_DATE,
    SOURCE_DOCUMENTS,
    SOURCE_ROW_CHECKS,
    SOURCE_TEXT_CHECKS,
    build,
    integrate_candidate,
    strict_quarterly_facts,
    ttm_evidence,
    validate_source_lock,
    verify_source_values,
)
from src.financial.quarterly_fundamentals import (
    quarterly_growth_snapshot,
    quarterly_profit_ttm_snapshot,
)
from src.io.fundamentals_update import OUTPUT_COLUMNS


def _source_fixture(source_id: str, *, drift: tuple[str, int] | None = None) -> bytes:
    paragraphs = "".join(
        f"<p>{fragment}</p>" for fragment in SOURCE_TEXT_CHECKS[source_id]
    )
    rows = []
    for check in SOURCE_ROW_CHECKS[source_id]:
        values = list(check["expected"])
        if drift is not None and check["check_id"] == drift[0]:
            values[drift[1]] += 1
        cells = [f"<td>{check['row_label']}</td>"]
        for value in values:
            rendered = f"({abs(value):,})" if value < 0 else f"{value:,}"
            cells.append(f"<td>{rendered}</td>")
        rows.append("<tr>" + "".join(cells) + "</tr>")
    return ("<html>" + paragraphs + "<table>" + "".join(rows) + "</table></html>").encode()


def _install_source_fixtures(monkeypatch) -> None:
    sources = deepcopy(SOURCE_DOCUMENTS)
    downloads = {}
    for source_id, source in sources.items():
        payload = _source_fixture(source_id)
        source["expected_sha256"] = hashlib.sha256(payload).hexdigest()
        downloads[himx_growth._source_url(source)] = payload
    monkeypatch.setattr(himx_growth, "SOURCE_DOCUMENTS", sources)
    monkeypatch.setattr(himx_growth, "_download_bytes", lambda url: downloads[url])


def _audit_frame(*, recovered: bool) -> pd.DataFrame:
    columns = [
        "scenario", "ticker", "first_missing_signal_date",
        "missing_signal_count", "no_raw_pit_financial_facts_signal_count",
        "insufficient_growth_history_signal_count",
        "stale_growth_snapshot_signal_count",
    ]
    if recovered:
        return pd.DataFrame(columns=columns)
    return pd.DataFrame([{
        "scenario": scenario,
        "ticker": "HIMX",
        "first_missing_signal_date": SIGNAL_DATE,
        "missing_signal_count": 1,
        "no_raw_pit_financial_facts_signal_count": 1,
        "insufficient_growth_history_signal_count": 0,
        "stale_growth_snapshot_signal_count": 0,
    } for scenario, _age in AUDIT_OBSERVATIONS], columns=columns)


def _write_audit(path: Path, *, recovered: bool) -> str:
    _audit_frame(recovered=recovered).to_csv(path, index=False)
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_exact_ifrs_ttm_bridge_and_positive_denominators() -> None:
    evidence = ttm_evidence()
    derived = evidence["derived"]
    assert derived["previous_ttm_usd_thousands"] == {
        "revenue": 786_441, "net_income": 12_379,
    }
    assert derived["current_ttm_usd_thousands"] == {
        "revenue": 1_370_972, "net_income": 325_809,
    }
    assert EXPECTED_TTM_USD_THOUSANDS == {
        "previous": {"revenue": 786_441, "net_income": 12_379},
        "current": {"revenue": 1_370_972, "net_income": 325_809},
    }
    assert derived["growth"]["revenue"] == pytest.approx(EXPECTED_GROWTH["revenue"])
    assert derived["growth"]["net_income"] == pytest.approx(
        EXPECTED_GROWTH["net_income"]
    )
    assert evidence["financial_age_days"] == 57


def test_direct_bundle_is_pit_hidden_then_complete_at_signal() -> None:
    facts = strict_quarterly_facts().copy()
    assert len(facts) == 4
    assert set(facts["metric"]) == {
        "revenue_ttm", "net_income_ttm", "revenue_growth", "net_income_growth"
    }
    assert facts.set_index("metric").loc["revenue_ttm", "value"] == 1_370_972_000
    assert facts.set_index("metric").loc["net_income_ttm", "value"] == 325_809_000
    for column in ("fiscal_end", "available_date"):
        facts[column] = pd.to_datetime(facts[column])
    assert "HIMX" not in quarterly_growth_snapshot(
        facts, pd.Timestamp("2021-11-03"), 150
    ).index
    growth = quarterly_growth_snapshot(facts, pd.Timestamp(SIGNAL_DATE), 150)
    profit = quarterly_profit_ttm_snapshot(facts, pd.Timestamp(SIGNAL_DATE), 150)
    assert growth.loc["HIMX", "financial_age_days"] == 57
    assert growth.loc["HIMX", "revenue_growth"] == pytest.approx(
        EXPECTED_GROWTH["revenue"]
    )
    assert growth.loc["HIMX", "net_income_growth"] == pytest.approx(
        EXPECTED_GROWTH["net_income"]
    )
    assert profit.loc["HIMX", "net_income_ttm"] == 325_809_000


def test_metric_mapping_matches_candidate_profitloss_not_attributable_profit() -> None:
    evidence = ttm_evidence()
    mapping = evidence["metric_mapping"]
    assert mapping["net_income"] == "consolidated Profit (loss) for the year/period"
    assert "ProfitLoss" in mapping["candidate_alignment"]
    boundary = evidence["accounting_boundary"]
    assert boundary["currency_and_scale_consistent"] is True
    assert boundary["non_ifrs_reconciliation_excluded"] is True
    assert "incomplete interim disclosures" in boundary["interim_basis"]


def test_official_source_lock_and_row_parser() -> None:
    validate_source_lock()
    raw = {source_id: _source_fixture(source_id) for source_id in SOURCE_DOCUMENTS}
    verified = verify_source_values(raw)
    assert len(verified) == 7
    assert {row["check_id"] for row in verified} == {
        check["check_id"]
        for checks in SOURCE_ROW_CHECKS.values() for check in checks
    }
    assert SOURCE_DOCUMENTS["q3_2021_6k"]["filed"] == AVAILABLE_DATE


def test_source_parser_rejects_operand_drift() -> None:
    raw = {
        source_id: _source_fixture(
            source_id,
            drift=("m9_2021_consolidated_profit", 0)
            if source_id == "q3_2021_6k" else None,
        )
        for source_id in SOURCE_DOCUMENTS
    }
    with pytest.raises(RuntimeError, match="m9_2021_consolidated_profit"):
        verify_source_values(raw)


def test_source_lock_rejects_post_signal_filing() -> None:
    sources = deepcopy(SOURCE_DOCUMENTS)
    sources["q3_2021_6k"]["filed"] = "2022-01-01"
    with pytest.raises(ValueError, match="violates the PIT cutoff"):
        validate_source_lock(sources)


def test_build_binds_baseline_and_recovered_audits(tmp_path, monkeypatch) -> None:
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
    assert report["accepted_fact_count"] == 4
    assert report["resolved_audit_observation_count"] == 2
    assert report["shared_candidate_integrated"] is True
    assert report["audit_binding"]["current"]["status"] == "RECOVERED"
    assert report["formal_financials_modified"] is False
    assert report["promotion_eligible"] is False


def test_candidate_integration_replaces_only_bounded_himx_key(tmp_path) -> None:
    base = tmp_path / "base"
    supplement = tmp_path / "supplement"
    candidate = tmp_path / "candidate"
    base.mkdir()
    supplement.mkdir()
    pd.DataFrame(columns=OUTPUT_COLUMNS).to_csv(base / "annual.csv", index=False)
    (base / "manifest.json").write_text("{}\n", encoding="utf-8")
    incoming = strict_quarterly_facts()
    incoming.to_csv(supplement / "strict_quarterly_facts.csv", index=False)
    (supplement / "manifest.json").write_text("{}\n", encoding="utf-8")
    outside = {
        "ticker": "HIMX", "fiscal_end": "2020-12-31",
        "available_date": "2021-03-31", "metric": "net_income_ttm",
        "value": 45_160_000, "taxonomy": "ifrs-full", "concept": "keep",
        "form": "20-F", "accession": "keep", "fetched_at": "2026-08-29",
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
    assert report["removed_conflicting_rows"] == 4
    assert report["inserted_strict_rows"] == 4
    assert len(result) == 5
    assert not result["value"].eq(999.0).any()
    assert result.loc[result["fiscal_end"].eq("2020-12-31"), "accession"].item() == "keep"


def test_baseline_binding_is_gcmg_checkpoint() -> None:
    assert himx_growth.EXPECTED_BASELINE_AUDIT_SHA256 == (
        "75d91fb6e43e5b9cc7cc2128711ceb7dc694245f117ffc939c7e3c3e0d21afc3"
    )
    assert himx_growth.EXPECTED_CURRENT_AUDIT_SHA256 == (
        "efa698d6aa12b84c50f55b04e5fd91bc7f5fb64669b83ce42e0b20f7ff438e06"
    )
    assert himx_growth.BASE_CANDIDATE_DIR.name.endswith("gcmg_predecessor_ttm")
    assert FISCAL_END == "2021-09-30"
