from copy import deepcopy
import hashlib
import json

import pandas as pd
import pytest

import scripts.research_v14_sohu_restated_quarters as sohu_quarters
from scripts.research_v14_sohu_restated_quarters import (
    AUDIT_OBSERVATIONS,
    AUDITED_ANNUAL_IDENTITY_USD,
    CONTINUITY_TEXT_CHECKS,
    EXPECTED_GROWTH,
    EXPECTED_QUARTERS_USD_THOUSANDS,
    EXPECTED_TTM_USD,
    OUTPUT_COLUMNS,
    SIGNAL_DATE,
    SOURCE_DOCUMENTS,
    SOURCE_ROW_CHECKS,
    TARGET_FISCAL_ENDS,
    build,
    integrate_candidate,
    recovered_observations,
    strict_quarterly_facts,
    ttm_evidence,
    validate_audit_binding,
    validate_source_lock,
)
from src.financial.quarterly_fundamentals import quarterly_growth_snapshot


def _fixture_source_bytes(source_id: str, *, drift: bool = False) -> bytes:
    paragraphs = [*CONTINUITY_TEXT_CHECKS, *SOURCE_ROW_CHECKS[source_id]]
    if drift and source_id == "2021_q2":
        paragraphs = [fragment.replace("40,739", "40,738") for fragment in paragraphs]
    return (
        "<html>" + "".join(f"<p>{fragment}</p>" for fragment in paragraphs) + "</html>"
    ).encode()


def _install_source_fixtures(monkeypatch, *, drift: bool = False):
    sources = deepcopy(SOURCE_DOCUMENTS)
    downloads = {}
    for source_id, source in sources.items():
        raw = _fixture_source_bytes(
            source_id, drift=drift and source_id == "2021_q2"
        )
        source["expected_sha256"] = hashlib.sha256(raw).hexdigest()
        downloads[sohu_quarters._source_url(source)] = raw
    calls = []

    def fake_download(url: str) -> bytes:
        calls.append(url)
        return downloads[url]

    monkeypatch.setattr(sohu_quarters, "SOURCE_DOCUMENTS", sources)
    monkeypatch.setattr(sohu_quarters, "_download_bytes", fake_download)
    return sources, calls


def _write_audit(path, *, recovered: bool) -> str:
    rows = []
    if not recovered:
        rows.append({
            "scenario": AUDIT_OBSERVATIONS[0][0],
            "ticker": "SOHU",
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
            "scenario", "ticker", "missing_signal_count",
            "first_missing_signal_date", "last_missing_signal_date",
            "no_raw_pit_financial_facts_signal_count",
            "insufficient_growth_history_signal_count",
            "stale_growth_snapshot_signal_count",
        ],
    ).to_csv(path, index=False)
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_exact_quarters_form_two_homogeneous_ttm_windows() -> None:
    evidence = ttm_evidence()
    assert evidence["prior_ttm_usd"] == EXPECTED_TTM_USD["prior"]
    assert evidence["current_ttm_usd"] == EXPECTED_TTM_USD["current"]
    assert evidence["growth"]["revenue"] == pytest.approx(EXPECTED_GROWTH["revenue"])
    assert evidence["growth"]["net_income"] == pytest.approx(
        EXPECTED_GROWTH["net_income"]
    )
    assert evidence["audited_annual_identity_usd"] == AUDITED_ANNUAL_IDENTITY_USD
    assert evidence["financial_age_days"] == 22


def test_strict_facts_are_exactly_eight_complete_quarters() -> None:
    facts = strict_quarterly_facts()
    assert list(facts.columns) == OUTPUT_COLUMNS
    assert len(facts) == 16
    assert set(facts["fiscal_end"]) == TARGET_FISCAL_ENDS
    assert set(facts["metric"]) == {"revenue", "net_income"}
    counts = facts.groupby(["fiscal_end", "metric"]).size()
    assert counts.eq(1).all()
    for fiscal_end, (revenue, net_income) in EXPECTED_QUARTERS_USD_THOUSANDS.items():
        values = facts.loc[facts["fiscal_end"].eq(fiscal_end)].set_index("metric")["value"]
        assert values["revenue"] == revenue * 1_000
        assert values["net_income"] == net_income * 1_000


def test_bundle_is_hidden_before_q2_release_and_complete_at_signal() -> None:
    facts = strict_quarterly_facts().copy()
    facts["fiscal_end"] = pd.to_datetime(facts["fiscal_end"])
    facts["available_date"] = pd.to_datetime(facts["available_date"])
    before = quarterly_growth_snapshot(
        facts, pd.Timestamp("2021-08-08"), maximum_age_days=150
    )
    at_signal = quarterly_growth_snapshot(
        facts, pd.Timestamp(SIGNAL_DATE), maximum_age_days=150
    )
    assert "SOHU" not in before.index
    assert at_signal.loc["SOHU", "financial_age_days"] == 22
    assert at_signal.loc["SOHU", "revenue_ttm"] == 837_624_000
    assert at_signal.loc["SOHU", "net_income_ttm"] == 103_859_000
    assert at_signal.loc["SOHU", "revenue_growth"] == pytest.approx(
        EXPECTED_GROWTH["revenue"]
    )
    assert at_signal.loc["SOHU", "net_income_growth"] == pytest.approx(
        EXPECTED_GROWTH["net_income"]
    )


def test_real_observation_resolves_with_positive_gaap_growth() -> None:
    assert AUDIT_OBSERVATIONS == (("liq2000000-age150-growth", 150),)
    resolution = recovered_observations()
    assert resolution["resolved"].all()
    assert set(resolution["decision"]) == {"complete_positive_gaap_growth_bundle"}
    assert resolution.iloc[0]["revenue_growth"] > 0.15
    assert resolution.iloc[0]["net_income_growth"] > 0.15


def test_four_6k_identity_dates_and_hashes_are_locked() -> None:
    assert len(SOURCE_DOCUMENTS) == 4
    assert SOURCE_DOCUMENTS["2020_q3"]["accepted_at"] == "2020-11-16T11:12:52Z"
    assert SOURCE_DOCUMENTS["2021_q2"]["accepted_at"] == "2021-08-09T11:51:04Z"
    assert SOURCE_DOCUMENTS["2020_q3"]["expected_sha256"] == (
        "3977602aa2a959b9ef4a26a4744c5bed37dd6b73157770401fc6d7378a0938a9"
    )
    assert SOURCE_DOCUMENTS["2021_q2"]["expected_sha256"] == (
        "11c6e3aa6deaeb45afe7e61cce5b4e825a4487c3cd328ff06fd99bc577b13e8c"
    )
    validate_source_lock()


def test_source_lock_rejects_post_signal_identity_or_sha_drift() -> None:
    sources = deepcopy(SOURCE_DOCUMENTS)
    sources["2021_q2"]["filed"] = "2021-09-01"
    with pytest.raises(ValueError, match="changed locked identity field filed"):
        validate_source_lock(sources)

    sources = deepcopy(SOURCE_DOCUMENTS)
    sources["2020_q4"]["document"] = "wrong.htm"
    with pytest.raises(ValueError, match="changed locked identity field document"):
        validate_source_lock(sources)

    sources = deepcopy(SOURCE_DOCUMENTS)
    sources["2021_q1"]["expected_sha256"] = "bad"
    with pytest.raises(ValueError, match="invalid SHA-256"):
        validate_source_lock(sources)


def test_build_downloads_and_verifies_all_four_statement_rows(
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
        sohu_quarters._source_url(sources[source_id]) for source_id in SOURCE_DOCUMENTS
    ]
    assert report["accepted_quarter_count"] == 8
    assert report["accepted_fact_count"] == 16
    assert report["resolved_audit_observation_count"] == 1
    assert all(
        row["exact_parent_attributable_net_income_row_verified"]
        for row in report["source_value_verification"]
    )
    assert manifest["release_status"] == "BLOCKED"
    assert manifest["formal_financials_modified"] is False


def test_build_rejects_statement_row_drift(tmp_path, monkeypatch) -> None:
    _install_source_fixtures(monkeypatch, drift=True)
    baseline = tmp_path / "baseline.csv"
    baseline_sha = _write_audit(baseline, recovered=False)
    with pytest.raises(RuntimeError, match="statement rows changed"):
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
    )["missing_observation_count"] == 1
    current = tmp_path / "current.csv"
    current_sha = _write_audit(current, recovered=True)
    assert validate_audit_binding(
        current, current_sha, expect_recovered=True
    )["status"] == "RECOVERED"


def test_candidate_overlay_changes_only_bounded_sohu_rows(tmp_path) -> None:
    base_dir = tmp_path / "base"
    supplement_dir = tmp_path / "supplement"
    output_dir = tmp_path / "candidate"
    base_dir.mkdir()
    supplement_dir.mkdir()
    annual = pd.DataFrame([{"ticker": "KEEP", "value": 1}])
    base = pd.concat([
        strict_quarterly_facts().assign(ticker="KEEP"),
        strict_quarterly_facts().iloc[:1],
        strict_quarterly_facts().iloc[:1].assign(fiscal_end="2018-09-30"),
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
    assert report["removed_conflicting_rows"] == 1
    assert report["inserted_strict_rows"] == 16
    assert len(merged) == len(base) - 1 + 16
    assert (output_dir / "annual.csv").read_bytes() == (
        base_dir / "annual.csv"
    ).read_bytes()
