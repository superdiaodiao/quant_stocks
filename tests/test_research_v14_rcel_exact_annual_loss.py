from copy import deepcopy
import hashlib
import json

import pandas as pd
import pytest

import scripts.research_v14_rcel_exact_annual_loss as rcel_loss
from scripts.research_v14_rcel_exact_annual_loss import (
    AUDIT_OBSERVATIONS,
    AVAILABLE_DATE,
    EXPECTED_NET_INCOME_TTM_AUD,
    FISCAL_END,
    OUTPUT_COLUMNS,
    SIGNAL_DATE,
    SOURCE_DOCUMENTS,
    SOURCE_ROW_CHECKS,
    SOURCE_TEXT_CHECKS,
    build,
    integrate_candidate,
    recovered_observations,
    strict_quarterly_facts,
    ttm_evidence,
    validate_audit_binding,
    validate_source_lock,
)
from src.financial.quarterly_fundamentals import (
    quarterly_growth_snapshot,
    quarterly_profit_ttm_snapshot,
)


def _fixture_source_bytes(
    source_id: str, *, loss_values: tuple[int, ...] | None = None
) -> bytes:
    paragraphs = "".join(
        f"<p>{fragment}</p>" for fragment in SOURCE_TEXT_CHECKS[source_id]
    )
    rows = []
    if source_id == "original_20f":
        check = SOURCE_ROW_CHECKS[0]
        values = loss_values or tuple(check["expected"])
        cells = [f"<td>{check['row_label']}</td>", "<td>5</td>"]
        for value in values:
            display = f"({abs(value):,})" if value < 0 else f"{value:,}"
            cells.extend(("<td>A$</td>", f"<td>{display}</td>", "<td></td>"))
        rows.append("<tr>" + "".join(cells) + "</tr>")
    return (
        "<html>" + paragraphs + "<table>" + "".join(rows) + "</table></html>"
    ).encode()


def _install_source_fixtures(monkeypatch, *, loss_values=None):
    sources = deepcopy(SOURCE_DOCUMENTS)
    downloads = {}
    for source_id, source in sources.items():
        raw = _fixture_source_bytes(source_id, loss_values=loss_values)
        source["expected_sha256"] = hashlib.sha256(raw).hexdigest()
        downloads[rcel_loss._source_url(source)] = raw

    calls = []

    def fake_download(url: str) -> bytes:
        calls.append(url)
        return downloads[url]

    monkeypatch.setattr(rcel_loss, "SOURCE_DOCUMENTS", sources)
    monkeypatch.setattr(rcel_loss, "_download_bytes", fake_download)
    return sources, calls


def _write_audit(path, *, recovered: bool) -> str:
    rows = []
    if not recovered:
        for scenario, _age in AUDIT_OBSERVATIONS:
            rows.append({
                "scenario": scenario,
                "ticker": "RCEL",
                "missing_signal_count": 1,
                "first_missing_signal_date": SIGNAL_DATE,
                "last_missing_signal_date": SIGNAL_DATE,
                "no_raw_pit_financial_facts_signal_count": 1,
                "insufficient_growth_history_signal_count": 0,
                "stale_growth_snapshot_signal_count": 0,
            })
    pd.DataFrame(
        rows,
        columns=[
            "scenario",
            "ticker",
            "missing_signal_count",
            "first_missing_signal_date",
            "last_missing_signal_date",
            "no_raw_pit_financial_facts_signal_count",
            "insufficient_growth_history_signal_count",
            "stale_growth_snapshot_signal_count",
        ],
    ).to_csv(path, index=False)
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_exact_audited_ifrs_loss_is_not_comprehensive_loss() -> None:
    evidence = ttm_evidence()
    assert evidence["net_income_ttm_aud"] == -35_160_227
    assert evidence["net_income_ttm_aud"] == EXPECTED_NET_INCOME_TTM_AUD
    assert evidence["financial_age_days"] == 61
    boundary = evidence["accounting_boundary"]
    assert boundary["presentation_currency"] == "AUD"
    assert boundary["ifrs_as_issued_by_iasb"]
    assert boundary["audited_consolidated_statement"]
    assert boundary["loss_for_period_not_comprehensive_loss"]
    assert not boundary["xbrl_only_amendment_changed_financials"]


def test_strict_fact_is_only_exact_negative_profit_state() -> None:
    facts = strict_quarterly_facts()
    assert list(facts.columns) == OUTPUT_COLUMNS
    assert len(facts) == 1
    assert set(facts["metric"]) == {"net_income_ttm"}
    assert facts.iloc[0]["value"] == EXPECTED_NET_INCOME_TTM_AUD
    assert facts.iloc[0]["fiscal_end"] == FISCAL_END
    assert facts.iloc[0]["available_date"] == AVAILABLE_DATE


def test_loss_is_pit_hidden_before_20f_and_resolves_without_growth() -> None:
    facts = strict_quarterly_facts().copy()
    facts["fiscal_end"] = pd.to_datetime(facts["fiscal_end"])
    facts["available_date"] = pd.to_datetime(facts["available_date"])
    before = quarterly_profit_ttm_snapshot(
        facts, pd.Timestamp("2019-10-30"), maximum_age_days=150
    )
    at_signal = quarterly_profit_ttm_snapshot(
        facts, pd.Timestamp(SIGNAL_DATE), maximum_age_days=150
    )
    growth = quarterly_growth_snapshot(
        facts, pd.Timestamp(SIGNAL_DATE), maximum_age_days=150
    )
    assert "RCEL" not in before.index
    assert at_signal.loc["RCEL", "financial_age_days"] == 61
    assert at_signal.loc["RCEL", "net_income_ttm"] == EXPECTED_NET_INCOME_TTM_AUD
    assert growth.empty


def test_all_three_real_observations_resolve_as_nonpositive_profit() -> None:
    assert AUDIT_OBSERVATIONS == (
        ("liq2000000-age150-growth", 150),
        ("liq2000000-age365-growth", 365),
        ("liq2000000-age550-growth", 550),
    )
    resolution = recovered_observations()
    assert resolution["resolved"].all()
    assert set(resolution["financial_age_days"]) == {61}
    assert set(resolution["decision"]) == {"known_nonpositive_profit"}


def test_original_20f_and_xbrl_only_amendment_are_locked() -> None:
    assert len(SOURCE_DOCUMENTS) == 2
    assert SOURCE_DOCUMENTS["original_20f"]["expected_sha256"] == (
        "99f59357b6fe5a914fc557c57d649630b0f9945ab3acc5ab54692e758f38a5b3"
    )
    assert SOURCE_DOCUMENTS["xbrl_only_20fa"]["expected_sha256"] == (
        "e87230bf49c6e8ad497f350ecb2eee2adb544e215d579977aea9e7bfa95e72c9"
    )
    validate_source_lock()


def test_source_lock_rejects_post_signal_identity_or_sha_drift() -> None:
    sources = deepcopy(SOURCE_DOCUMENTS)
    sources["xbrl_only_20fa"]["filed"] = "2020-01-01"
    with pytest.raises(ValueError, match="violates the PIT cutoff"):
        validate_source_lock(sources)

    sources = deepcopy(SOURCE_DOCUMENTS)
    sources["original_20f"]["document"] = "wrong.htm"
    with pytest.raises(ValueError, match="changed locked identity field document"):
        validate_source_lock(sources)

    sources = deepcopy(SOURCE_DOCUMENTS)
    sources["original_20f"]["expected_sha256"] = "bad"
    with pytest.raises(ValueError, match="invalid SHA-256"):
        validate_source_lock(sources)


def test_build_downloads_both_sources_and_verifies_loss_row(
    tmp_path, monkeypatch
) -> None:
    sources, calls = _install_source_fixtures(monkeypatch)
    baseline = tmp_path / "baseline.csv"
    baseline_sha = _write_audit(baseline, recovered=False)
    report = build(
        tmp_path / "package",
        baseline_audit_path=baseline,
        expected_baseline_audit_sha256=baseline_sha,
        current_audit_path=baseline,
        expected_current_audit_sha256=baseline_sha,
    )
    manifest = json.loads((tmp_path / "package" / "manifest.json").read_text())
    assert calls == [
        rcel_loss._source_url(sources[source_id]) for source_id in SOURCE_DOCUMENTS
    ]
    assert report["accepted_fact_count"] == 1
    assert report["resolved_audit_observation_count"] == 3
    assert len(report["source_value_verification"]) == 1
    assert manifest["release_status"] == "BLOCKED"
    assert manifest["formal_financials_modified"] is False


def test_build_rejects_source_row_drift(tmp_path, monkeypatch) -> None:
    _install_source_fixtures(
        monkeypatch,
        loss_values=(-35_160_226, -16_519_155, -11_511_024),
    )
    baseline = tmp_path / "baseline.csv"
    baseline_sha = _write_audit(baseline, recovered=False)
    with pytest.raises(RuntimeError, match="source row changed"):
        build(
            tmp_path / "package",
            baseline_audit_path=baseline,
            expected_baseline_audit_sha256=baseline_sha,
            current_audit_path=baseline,
            expected_current_audit_sha256=baseline_sha,
        )


def test_audit_binding_proves_baseline_and_recovered_state(tmp_path) -> None:
    baseline = tmp_path / "baseline.csv"
    baseline_sha = _write_audit(baseline, recovered=False)
    assert validate_audit_binding(
        baseline, baseline_sha, expect_recovered=False
    )["missing_observation_count"] == 3

    current = tmp_path / "current.csv"
    current_sha = _write_audit(current, recovered=True)
    assert validate_audit_binding(
        current, current_sha, expect_recovered=True
    )["status"] == "RECOVERED"


def test_candidate_overlay_changes_only_bounded_rcel_row(tmp_path) -> None:
    base_dir = tmp_path / "base"
    supplement_dir = tmp_path / "supplement"
    output_dir = tmp_path / "candidate"
    base_dir.mkdir()
    supplement_dir.mkdir()
    annual = pd.DataFrame([{"ticker": "KEEP", "value": 1}])
    base = pd.concat([
        strict_quarterly_facts().assign(ticker="KEEP"),
        strict_quarterly_facts().assign(fiscal_end="2018-06-30"),
    ], ignore_index=True)
    annual.to_csv(base_dir / "annual.csv", index=False)
    base.to_csv(base_dir / "quarterly.csv", index=False)
    (base_dir / "manifest.json").write_text("{}\n")
    strict_quarterly_facts().to_csv(
        supplement_dir / "strict_quarterly_facts.csv", index=False
    )
    (supplement_dir / "manifest.json").write_text("{}\n")

    report = integrate_candidate(
        base_dir=base_dir,
        supplement_dir=supplement_dir,
        output_dir=output_dir,
    )
    merged = pd.read_csv(output_dir / "quarterly.csv")
    assert report["removed_conflicting_rows"] == 0
    assert report["inserted_strict_rows"] == 1
    assert len(merged) == len(base) + 1
    assert (output_dir / "annual.csv").read_bytes() == (
        base_dir / "annual.csv"
    ).read_bytes()
