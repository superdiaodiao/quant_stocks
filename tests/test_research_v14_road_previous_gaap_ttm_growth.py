from copy import deepcopy
import hashlib
import json

import pandas as pd
import pytest

import scripts.research_v14_road_previous_gaap_ttm_growth as road_growth
from scripts.research_v14_road_previous_gaap_ttm_growth import (
    AUDIT_OBSERVATIONS,
    AVAILABLE_DATE,
    EXPECTED_COMPANYFACTS_USD,
    EXPECTED_TTM_USD,
    FISCAL_END,
    OUTPUT_COLUMNS,
    SIGNAL_DATE,
    TRANSITION_SOURCE,
    TRANSITION_TEXT_CHECKS,
    build,
    integrate_candidate,
    recovered_observations,
    strict_quarterly_facts,
    ttm_evidence,
    validate_audit_binding,
    validate_source_lock,
)
from src.financial.quarterly_fundamentals import quarterly_growth_snapshot


def _fixture_source_bytes(*, previous_net_income: int = 26_655) -> bytes:
    paragraphs = "".join(f"<p>{fragment}</p>" for fragment in TRANSITION_TEXT_CHECKS)
    transition = (
        "<p>For the Nine Months Ended June 30, 2019 Revenues "
        "$ 545,921 $ ( 1,321 ) $ 547,242 Cost of revenues 466,900 "
        "Provision for income taxes 8,080 Net income "
        f"$ 26,568 $ ( 87 ) $ {previous_net_income:,}</p>"
    )
    return ("<html>" + paragraphs + transition + "</html>").encode()


def _install_source_fixture(monkeypatch, *, previous_net_income=26_655):
    source = deepcopy(TRANSITION_SOURCE)
    raw = _fixture_source_bytes(previous_net_income=previous_net_income)
    source["expected_sha256"] = hashlib.sha256(raw).hexdigest()
    calls = []

    def fake_download(url: str) -> bytes:
        calls.append(url)
        return raw

    monkeypatch.setattr(road_growth, "TRANSITION_SOURCE", source)
    monkeypatch.setattr(road_growth, "_download_bytes", fake_download)
    return source, calls


def _write_audit(path, *, recovered: bool) -> str:
    rows = []
    if not recovered:
        for scenario, _age in AUDIT_OBSERVATIONS:
            rows.append({
                "scenario": scenario,
                "ticker": "ROAD",
                "missing_signal_count": 1,
                "first_missing_signal_date": SIGNAL_DATE,
                "last_missing_signal_date": SIGNAL_DATE,
                "no_raw_pit_financial_facts_signal_count": 0,
                "insufficient_growth_history_signal_count": 1,
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


def test_previous_gaap_transition_arithmetic_is_homogeneous() -> None:
    evidence = ttm_evidence()
    assert evidence["companyfacts_operands_usd"] == EXPECTED_COMPANYFACTS_USD
    assert evidence["derived"]["prior_ttm_usd"] == EXPECTED_TTM_USD["prior"]
    assert evidence["derived"]["current_ttm_usd"] == EXPECTED_TTM_USD["current"]
    assert evidence["derived"]["growth"]["revenue"] == pytest.approx(
        110_921_000 / 652_022_000
    )
    assert evidence["derived"]["growth"]["net_income"] == pytest.approx(
        -6_115_000 / 47_914_000
    )
    assert evidence["financial_age_days"] == 52
    boundary = evidence["accounting_boundary"]
    assert boundary["homogeneous_previous_gaap_basis"]
    assert boundary["asc606_modified_retrospective_as_reported_values_excluded"]


def test_strict_facts_are_complete_direct_ttm_bundle() -> None:
    facts = strict_quarterly_facts()
    assert list(facts.columns) == OUTPUT_COLUMNS
    assert len(facts) == 4
    assert set(facts["metric"]) == {
        "revenue_ttm", "net_income_ttm", "revenue_growth", "net_income_growth"
    }
    values = facts.set_index("metric")["value"]
    assert values["revenue_ttm"] == 762_943_000
    assert values["net_income_ttm"] == 41_799_000
    assert values["revenue_growth"] > 0.15
    assert values["net_income_growth"] < 0
    assert set(facts["fiscal_end"]) == {FISCAL_END}
    assert set(facts["available_date"]) == {AVAILABLE_DATE}


def test_package_is_pit_hidden_before_10q_and_complete_at_signal() -> None:
    facts = strict_quarterly_facts().copy()
    facts["fiscal_end"] = pd.to_datetime(facts["fiscal_end"])
    facts["available_date"] = pd.to_datetime(facts["available_date"])
    before = quarterly_growth_snapshot(
        facts, pd.Timestamp("2019-08-08"), maximum_age_days=150
    )
    at_signal = quarterly_growth_snapshot(
        facts, pd.Timestamp(SIGNAL_DATE), maximum_age_days=150
    )
    assert "ROAD" not in before.index
    assert at_signal.loc["ROAD", "financial_age_days"] == 52
    assert at_signal.loc["ROAD", "revenue_growth"] > 0.15
    assert at_signal.loc["ROAD", "net_income_growth"] < 0


def test_all_three_observations_resolve_but_fail_profit_growth_gate() -> None:
    assert AUDIT_OBSERVATIONS == (
        ("liq2000000-age150-growth", 150),
        ("liq2000000-age365-growth", 365),
        ("liq2000000-age550-growth", 550),
    )
    resolution = recovered_observations()
    assert resolution["resolved"].all()
    assert not resolution["passes_net_income_growth_gate"].any()
    assert set(resolution["financial_age_days"]) == {52}


def test_transition_10q_identity_date_and_hash_are_locked() -> None:
    assert TRANSITION_SOURCE["accepted_at"] == "2019-08-09T20:44:45Z"
    assert TRANSITION_SOURCE["expected_sha256"] == (
        "42d77fae6946831bca6c724182874114ce39ce9f488d82e8a9718a84bd946731"
    )
    validate_source_lock()


def test_source_lock_rejects_post_signal_identity_or_sha_drift() -> None:
    source = deepcopy(TRANSITION_SOURCE)
    source["filed"] = "2019-10-01"
    with pytest.raises(ValueError, match="violates the PIT cutoff"):
        validate_source_lock(source)

    source = deepcopy(TRANSITION_SOURCE)
    source["document"] = "wrong.htm"
    with pytest.raises(ValueError, match="changed locked identity field document"):
        validate_source_lock(source)

    source = deepcopy(TRANSITION_SOURCE)
    source["expected_sha256"] = "bad"
    with pytest.raises(ValueError, match="invalid SHA-256"):
        validate_source_lock(source)


def test_build_downloads_transition_source_and_verifies_table(
    tmp_path, monkeypatch
) -> None:
    source, calls = _install_source_fixture(monkeypatch)
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
    assert calls == [road_growth._source_url(source)]
    assert report["accepted_fact_count"] == 4
    assert report["resolved_audit_observation_count"] == 3
    assert report["transition_value_verification"]["unit"] == "USD_thousands"
    assert manifest["release_status"] == "BLOCKED"
    assert manifest["formal_financials_modified"] is False


def test_build_rejects_transition_table_drift(tmp_path, monkeypatch) -> None:
    _install_source_fixture(monkeypatch, previous_net_income=26_654)
    baseline = tmp_path / "baseline.csv"
    baseline_sha = _write_audit(baseline, recovered=False)
    with pytest.raises(RuntimeError, match="transition table changed"):
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


def test_candidate_overlay_changes_only_bounded_road_rows(tmp_path) -> None:
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
    assert report["inserted_strict_rows"] == 4
    assert len(merged) == len(base) + 4
    assert (output_dir / "annual.csv").read_bytes() == (
        base_dir / "annual.csv"
    ).read_bytes()
