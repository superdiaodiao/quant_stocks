from __future__ import annotations

from copy import deepcopy
import hashlib
from pathlib import Path

import pandas as pd
import pytest

import scripts.research_v14_hrmy_annual_m9_ttm_growth as hrmy_growth
from scripts.research_v14_hrmy_annual_m9_ttm_growth import (
    AUDIT_OBSERVATIONS,
    AVAILABLE_DATE,
    EXPECTED_GROWTH,
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
        cells.extend("<td>—</td>" for _ in range(check["minimum_zero_markers"]))
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
        downloads[hrmy_growth._source_url(source)] = payload
    monkeypatch.setattr(hrmy_growth, "SOURCE_DOCUMENTS", sources)
    monkeypatch.setattr(hrmy_growth, "_download_bytes", lambda url: downloads[url])


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
        "ticker": "HRMY",
        "first_missing_signal_date": SIGNAL_DATE,
        "missing_signal_count": 1,
        "no_raw_pit_financial_facts_signal_count": 0,
        "insufficient_growth_history_signal_count": 1,
        "stale_growth_snapshot_signal_count": 0,
    } for scenario, _age in AUDIT_OBSERVATIONS], columns=columns)


def _write_audit(path: Path, *, recovered: bool) -> str:
    _audit_frame(recovered=recovered).to_csv(path, index=False)
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_exact_us_gaap_ttm_bridge_and_growth() -> None:
    evidence = ttm_evidence()
    derived = evidence["derived"]
    assert derived["previous_ttm_usd_thousands"] == {
        "revenue": 109_449, "net_income": -73_176,
    }
    assert derived["current_ttm_usd_thousands"] == {
        "revenue": 270_515, "net_income": 11_675,
    }
    assert derived["growth"]["revenue"] == pytest.approx(EXPECTED_GROWTH["revenue"])
    assert derived["growth"]["net_income"] == pytest.approx(
        EXPECTED_GROWTH["net_income"]
    )
    assert evidence["financial_age_days"] == 52


def test_direct_bundle_is_pit_hidden_then_complete_at_signal() -> None:
    facts = strict_quarterly_facts().copy()
    assert len(facts) == 4
    assert facts.set_index("metric").loc["revenue_ttm", "value"] == 270_515_000
    assert facts.set_index("metric").loc["net_income_ttm", "value"] == 11_675_000
    for column in ("fiscal_end", "available_date"):
        facts[column] = pd.to_datetime(facts[column])
    assert "HRMY" not in quarterly_growth_snapshot(
        facts, pd.Timestamp("2021-11-08"), 150
    ).index
    growth = quarterly_growth_snapshot(facts, pd.Timestamp(SIGNAL_DATE), 150)
    profit = quarterly_profit_ttm_snapshot(facts, pd.Timestamp(SIGNAL_DATE), 150)
    assert growth.loc["HRMY", "financial_age_days"] == 52
    assert growth.loc["HRMY", "revenue_growth"] == pytest.approx(
        EXPECTED_GROWTH["revenue"]
    )
    assert growth.loc["HRMY", "net_income_growth"] == pytest.approx(
        EXPECTED_GROWTH["net_income"]
    )
    assert profit.loc["HRMY", "net_income_ttm"] == 11_675_000


def test_metric_mapping_preserves_explicit_zero_and_consolidated_basis() -> None:
    evidence = ttm_evidence()
    assert evidence["operands_usd_thousands"]["m9_2019"]["revenue"] == 0
    assert "em dash" in evidence["metric_mapping"]["m9_2019_revenue"]
    assert evidence["accounting_boundary"]["consolidated_basis_consistent"] is True
    assert evidence["accounting_boundary"]["common_stockholder_loss_excluded"] is True
    assert evidence["metric_mapping"]["growth_formula"].endswith(
        "abs(previous_ttm)"
    )


def test_official_source_lock_and_row_parser() -> None:
    validate_source_lock()
    raw = {source_id: _source_fixture(source_id) for source_id in SOURCE_DOCUMENTS}
    verified = verify_source_values(raw)
    assert len(verified) == 6
    zero = next(row for row in verified if row["check_id"] == "q3_2020_product_revenue")
    assert zero["minimum_zero_markers"] == 2
    assert SOURCE_DOCUMENTS["q3_2021_10q"]["filed"] == AVAILABLE_DATE


def test_source_parser_rejects_missing_zero_markers() -> None:
    raw = {source_id: _source_fixture(source_id) for source_id in SOURCE_DOCUMENTS}
    raw["q3_2020_10q"] = raw["q3_2020_10q"].replace(b"<td>\xe2\x80\x94</td>", b"<td></td>")
    with pytest.raises(RuntimeError, match="q3_2020_product_revenue"):
        verify_source_values(raw)


def test_source_parser_rejects_operand_drift() -> None:
    raw = {
        source_id: _source_fixture(
            source_id,
            drift=("q3_2021_consolidated_net_income_loss", 2)
            if source_id == "q3_2021_10q" else None,
        )
        for source_id in SOURCE_DOCUMENTS
    }
    with pytest.raises(RuntimeError, match="q3_2021_consolidated_net_income_loss"):
        verify_source_values(raw)


def test_source_lock_rejects_post_signal_filing() -> None:
    sources = deepcopy(SOURCE_DOCUMENTS)
    sources["q3_2021_10q"]["filed"] = "2022-01-01"
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


def test_candidate_integration_replaces_only_bounded_hrmy_key(tmp_path) -> None:
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
        "ticker": "HRMY", "fiscal_end": "2020-12-31",
        "available_date": "2021-03-25", "metric": "net_income_ttm",
        "value": -36_944_000, "taxonomy": "us-gaap", "concept": "keep",
        "form": "10-K", "accession": "keep", "fetched_at": "2026-08-29",
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


def test_baseline_binding_is_himx_checkpoint() -> None:
    assert hrmy_growth.EXPECTED_BASELINE_AUDIT_SHA256 == (
        "efa698d6aa12b84c50f55b04e5fd91bc7f5fb64669b83ce42e0b20f7ff438e06"
    )
    assert hrmy_growth.EXPECTED_CURRENT_AUDIT_SHA256 == (
        "0d1a4ca4ac3e6b9f44731997c9b2a7641f59f5fbd537f9e1057547c30c8b751f"
    )
    assert hrmy_growth.BASE_CANDIDATE_DIR.name.endswith("himx_ifrs_ttm")
    assert FISCAL_END == "2021-09-30"
