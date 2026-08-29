from copy import deepcopy
import hashlib
import json

import pandas as pd
import pytest

import scripts.research_v14_hrzn_exact_annual_growth as hrzn_growth
from scripts.research_v14_hrzn_exact_annual_growth import (
    AUDIT_OBSERVATIONS,
    AVAILABLE_DATE,
    FISCAL_END,
    OPERANDS_USD_THOUSANDS,
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
from src.financial.quarterly_fundamentals import quarterly_growth_snapshot


def _fixture_source_bytes(
    source_id: str,
    value_overrides: dict[tuple[str, str], tuple[int, ...]] | None = None,
) -> bytes:
    overrides = value_overrides or {}
    paragraphs = "".join(
        f"<p>{fragment}</p>" for fragment in SOURCE_TEXT_CHECKS[source_id]
    )
    rows = []
    for check in SOURCE_ROW_CHECKS[source_id]:
        values = overrides.get(
            (source_id, check["check_id"]), tuple(check["expected"])
        )
        cells = [f"<td>{check['row_label']}</td>", "<td></td>"]
        for value in values:
            display = f"({abs(value):,})" if value < 0 else f"{value:,}"
            cells.extend(("<td>$</td>", f"<td>{display}</td>", "<td></td>"))
        rows.append("<tr>" + "".join(cells) + "</tr>")
    return (
        "<html>" + paragraphs + "<table>" + "".join(rows) + "</table></html>"
    ).encode()


def _install_source_fixture(tmp_path, monkeypatch, *, value_overrides=None):
    sources = deepcopy(SOURCE_DOCUMENTS)
    downloads = {}
    for source_id, source in sources.items():
        raw = _fixture_source_bytes(source_id, value_overrides)
        source["expected_sha256"] = hashlib.sha256(raw).hexdigest()
        downloads[hrzn_growth._source_url(source)] = raw

    calls = []

    def fake_download(url: str) -> bytes:
        calls.append(url)
        return downloads[url]

    monkeypatch.setattr(hrzn_growth, "SOURCE_DOCUMENTS", sources)
    monkeypatch.setattr(hrzn_growth, "_download_bytes", fake_download)
    return sources, calls


def _write_audit(path, *, recovered: bool) -> str:
    rows = []
    if not recovered:
        for scenario, _age in AUDIT_OBSERVATIONS:
            rows.append({
                "scenario": scenario,
                "ticker": "HRZN",
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


def test_hrzn_exact_annual_arithmetic_and_bdc_metric_mapping() -> None:
    evidence = ttm_evidence()
    assert OPERANDS_USD_THOUSANDS == {
        "fy2019": {"revenue": 43_125, "net_income": 19_498},
        "fy2020": {"revenue": 46_035, "net_income": 6_364},
    }
    assert evidence["derived"]["growth"]["revenue"] == pytest.approx(
        2_910 / 43_125
    )
    assert evidence["derived"]["growth"]["net_income"] == pytest.approx(
        -13_134 / 19_498
    )
    assert "Total investment income" in evidence["metric_mapping"]["revenue"]
    assert evidence["accounting_boundary"]["net_investment_income_excluded"]


def test_strict_facts_are_complete_direct_annual_bundle() -> None:
    facts = strict_quarterly_facts()
    assert list(facts.columns) == OUTPUT_COLUMNS
    assert len(facts) == 4
    assert set(facts["metric"]) == {
        "revenue_ttm", "net_income_ttm", "revenue_growth", "net_income_growth"
    }
    values = facts.set_index("metric")["value"]
    assert values["revenue_ttm"] == 46_035_000
    assert values["net_income_ttm"] == 6_364_000
    assert set(facts["fiscal_end"]) == {FISCAL_END}
    assert set(facts["available_date"]) == {AVAILABLE_DATE}
    assert set(facts["accession"]) == {"0001558370-21-002265"}


def test_package_is_not_visible_before_filing_and_resolves_all_ages() -> None:
    facts = strict_quarterly_facts().copy()
    facts["fiscal_end"] = pd.to_datetime(facts["fiscal_end"])
    facts["available_date"] = pd.to_datetime(facts["available_date"])
    before = quarterly_growth_snapshot(
        facts, pd.Timestamp("2021-03-01"), maximum_age_days=550
    )
    assert "HRZN" not in before.index
    for age in (150, 365, 550):
        after = quarterly_growth_snapshot(
            facts, pd.Timestamp(SIGNAL_DATE), maximum_age_days=age
        )
        assert after.loc["HRZN", "financial_age_days"] == 59
        assert after.loc["HRZN", "revenue_growth"] > 0
        assert after.loc["HRZN", "net_income_growth"] < 0


def test_three_real_audit_observations_resolve_at_age_59() -> None:
    assert AUDIT_OBSERVATIONS == (
        ("liq2000000-age150-growth", 150),
        ("liq2000000-age365-growth", 365),
        ("liq2000000-age550-growth", 550),
    )
    resolution = recovered_observations()
    assert resolution["resolved"].all()
    assert set(resolution["financial_age_days"]) == {59}
    assert set(resolution["decision"]) == {
        "recovered_exact_audited_bdc_direct_annual_ttm_growth_bundle"
    }


def test_official_source_identity_date_and_hash_are_locked() -> None:
    source = SOURCE_DOCUMENTS["fy2020_10k"]
    assert (
        source["filed"],
        source["accepted_at"],
        source["accession"],
        source["document"],
        source["expected_sha256"],
    ) == (
        "2021-03-02",
        "2021-03-02T16:32:32Z",
        "0001558370-21-002265",
        "tmb-20201231x10k.htm",
        "5176180a1e2a32f0ce527a3bc39596ac81fbdc0ada7571902b692e1369577e0e",
    )
    assert hrzn_growth._source_url(source).startswith(
        "https://www.sec.gov/Archives/edgar/data/1487428/"
    )
    validate_source_lock()


def test_source_lock_rejects_post_signal_or_identity_drift() -> None:
    sources = deepcopy(SOURCE_DOCUMENTS)
    sources["fy2020_10k"]["filed"] = "2021-05-01"
    with pytest.raises(ValueError, match="violates the PIT cutoff"):
        validate_source_lock(sources)

    sources = deepcopy(SOURCE_DOCUMENTS)
    sources["fy2020_10k"]["document"] = "wrong.htm"
    with pytest.raises(ValueError, match="changed locked identity field document"):
        validate_source_lock(sources)

    sources = deepcopy(SOURCE_DOCUMENTS)
    sources["fy2020_10k"]["expected_sha256"] = "bad"
    with pytest.raises(ValueError, match="invalid SHA-256"):
        validate_source_lock(sources)


def test_build_downloads_and_verifies_locked_rows(tmp_path, monkeypatch) -> None:
    sources, calls = _install_source_fixture(tmp_path, monkeypatch)
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
    assert calls == [hrzn_growth._source_url(sources["fy2020_10k"])]
    assert report["accepted_fact_count"] == 4
    assert report["resolved_audit_observation_count"] == 3
    assert len(report["source_value_verification"]) == 2
    assert manifest["release_status"] == "BLOCKED"
    assert manifest["formal_financials_modified"] is False


def test_build_rejects_source_row_drift(tmp_path, monkeypatch) -> None:
    _install_source_fixture(
        tmp_path,
        monkeypatch,
        value_overrides={
            ("fy2020_10k", "annual_total_investment_income"):
                (46_034, 43_125, 31_090)
        },
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


def test_candidate_overlay_changes_only_bounded_hrzn_rows(tmp_path) -> None:
    base_dir = tmp_path / "base"
    supplement_dir = tmp_path / "supplement"
    output_dir = tmp_path / "candidate"
    base_dir.mkdir()
    supplement_dir.mkdir()
    annual = pd.DataFrame([{"ticker": "KEEP", "value": 1}])
    base = pd.concat([
        strict_quarterly_facts().assign(ticker="KEEP"),
        strict_quarterly_facts().assign(fiscal_end="2019-12-31"),
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
    assert report["inserted_strict_rows"] == 4
    assert len(merged) == len(base) + 4
    assert (output_dir / "annual.csv").read_bytes() == (
        base_dir / "annual.csv"
    ).read_bytes()
