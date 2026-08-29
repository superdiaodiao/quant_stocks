from copy import deepcopy
import hashlib
import json

import pandas as pd
import pytest

import scripts.research_v14_stne_q3_2020 as stne_q3
from scripts.research_v14_stne_q3_2020 import (
    AUDIT_OBSERVATIONS,
    AVAILABLE_DATE,
    EXPECTED_TTM_BRL,
    FISCAL_END,
    OUTPUT_COLUMNS,
    Q3_VALUES_BRL,
    SIGNAL_DATE,
    SOURCE_DOCUMENT,
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


def _fixture_source_bytes(*, value_overrides=None) -> bytes:
    overrides = value_overrides or {}
    paragraphs = "".join(f"<p>{fragment}</p>" for fragment in SOURCE_TEXT_CHECKS)
    header = (
        "<tr><td>Statement of Profit or Loss (R$mm)</td>"
        "<td>3 Q20</td><td>% Rev.</td><td>3 Q19</td><td>% Rev.</td>"
        "<td>delta</td><td>delta p.p.</td></tr>"
    )
    rows = []
    for check in SOURCE_ROW_CHECKS:
        values = overrides.get(check["check_id"], tuple(check["expected"]))
        cells = [f"<td>{check['row_label']}</td>"]
        for value in values:
            display = f"({abs(value)})" if value < 0 else str(value)
            cells.append(f"<td>{display}</td>")
        rows.append("<tr>" + "".join(cells) + "</tr>")
    return (
        "<html>" + paragraphs + "<table>" + header
        + "".join(rows) + "</table></html>"
    ).encode()


def _install_source_fixture(monkeypatch, *, value_overrides=None):
    source = deepcopy(SOURCE_DOCUMENT)
    raw = _fixture_source_bytes(value_overrides=value_overrides)
    source["expected_sha256"] = hashlib.sha256(raw).hexdigest()
    calls = []

    def fake_download(url: str) -> bytes:
        calls.append(url)
        return raw

    monkeypatch.setattr(stne_q3, "SOURCE_DOCUMENT", source)
    monkeypatch.setattr(stne_q3, "_download_bytes", fake_download)
    return source, calls


def _write_audit(path, *, recovered: bool) -> str:
    rows = []
    if not recovered:
        for scenario, _age in AUDIT_OBSERVATIONS:
            rows.append({
                "scenario": scenario,
                "ticker": "STNE",
                "missing_signal_count": 1,
                "first_missing_signal_date": SIGNAL_DATE,
                "last_missing_signal_date": SIGNAL_DATE,
                "no_raw_pit_financial_facts_signal_count": 0,
                "insufficient_growth_history_signal_count": 0,
                "stale_growth_snapshot_signal_count": 1,
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


def test_disclosed_precision_q3_closes_existing_ttm_chain() -> None:
    evidence = ttm_evidence()
    assert Q3_VALUES_BRL == {"revenue": 934_300_000, "net_income": 249_100_000}
    assert evidence["derived"]["ttm_brl"] == EXPECTED_TTM_BRL
    assert evidence["derived"]["growth"]["revenue"] == pytest.approx(
        778_941_000 / 2_322_418_000
    )
    assert evidence["derived"]["growth"]["net_income"] == pytest.approx(
        128_023_000 / 667_302_000
    )
    assert evidence["financial_age_days"] == 120
    boundary = evidence["accounting_boundary"]
    assert boundary["presentation_currency"] == "BRL"
    assert boundary["reported_precision_preserved"] == "R$0.1m"
    assert boundary["adjusted_net_income_excluded"]


def test_strict_facts_are_only_paired_original_ifrs_q3_rows() -> None:
    facts = strict_quarterly_facts()
    assert list(facts.columns) == OUTPUT_COLUMNS
    assert len(facts) == 2
    assert set(facts["metric"]) == {"revenue", "net_income"}
    values = facts.set_index("metric")["value"]
    assert values["revenue"] == 934_300_000
    assert values["net_income"] == 249_100_000
    assert set(facts["fiscal_end"]) == {FISCAL_END}
    assert set(facts["available_date"]) == {AVAILABLE_DATE}


def test_existing_quarters_plus_q3_are_fresh_and_pass_growth_gates() -> None:
    rows = []
    evidence = ttm_evidence()
    quarter_ends = {
        "prior": ["2018-12-31", "2019-03-31", "2019-06-30", "2019-09-30"],
        "current": ["2019-12-31", "2020-03-31", "2020-06-30", "2020-09-30"],
    }
    for basis, ends in quarter_ends.items():
        for metric in ("revenue", "net_income"):
            for end, value in zip(ends, evidence["ttm_operands_brl"][basis][metric]):
                rows.append({
                    "ticker": "STNE", "fiscal_end": end,
                    "available_date": AVAILABLE_DATE, "metric": metric,
                    "value": value, "taxonomy": "ifrs-full", "concept": metric,
                    "form": "6-K", "accession": "fixture", "fetched_at": "2026-08-29",
                })
    facts = pd.DataFrame(rows, columns=OUTPUT_COLUMNS)
    facts["fiscal_end"] = pd.to_datetime(facts["fiscal_end"])
    facts["available_date"] = pd.to_datetime(facts["available_date"])
    snapshot = quarterly_growth_snapshot(
        facts, pd.Timestamp(SIGNAL_DATE), maximum_age_days=150
    )
    assert snapshot.loc["STNE", "financial_age_days"] == 120
    assert snapshot.loc["STNE", "revenue_growth"] > 0.15
    assert snapshot.loc["STNE", "net_income_growth"] > 0.15


def test_both_real_observations_resolve_and_pass_growth_gates() -> None:
    assert AUDIT_OBSERVATIONS == (
        ("liq2000000-age150-growth", 150),
        ("liq10000000-age150-growth", 150),
    )
    resolution = recovered_observations()
    assert resolution["resolved"].all()
    assert resolution["passes_growth_gates"].all()
    assert set(resolution["financial_age_days"]) == {120}


def test_official_q3_exhibit_identity_date_and_hash_are_locked() -> None:
    assert SOURCE_DOCUMENT["accepted_at"] == "2020-10-29T20:49:17Z"
    assert SOURCE_DOCUMENT["expected_sha256"] == (
        "aaabcf1ac24677bb5e0380613f76fe25e291c3f5b2b1aab3b94f79c2cd2afdd3"
    )
    validate_source_lock()


def test_source_lock_rejects_post_signal_identity_or_sha_drift() -> None:
    source = deepcopy(SOURCE_DOCUMENT)
    source["filed"] = "2021-02-27"
    with pytest.raises(ValueError, match="violates the PIT cutoff"):
        validate_source_lock(source)

    source = deepcopy(SOURCE_DOCUMENT)
    source["document"] = "wrong.htm"
    with pytest.raises(ValueError, match="changed locked identity field document"):
        validate_source_lock(source)

    source = deepcopy(SOURCE_DOCUMENT)
    source["expected_sha256"] = "bad"
    with pytest.raises(ValueError, match="invalid SHA-256"):
        validate_source_lock(source)


def test_build_downloads_source_and_verifies_two_ifrs_rows(
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
    assert calls == [stne_q3._source_url(source)]
    assert report["accepted_fact_count"] == 2
    assert report["resolved_audit_observation_count"] == 2
    assert len(report["source_value_verification"]) == 2
    assert manifest["release_status"] == "BLOCKED"
    assert manifest["formal_financials_modified"] is False


def test_build_rejects_source_row_drift(tmp_path, monkeypatch) -> None:
    _install_source_fixture(
        monkeypatch,
        value_overrides={
            "q3_ifrs_net_income": (249.0, 26.7, 191.3, 28.5, 30.2)
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
    )["missing_observation_count"] == 2

    current = tmp_path / "current.csv"
    current_sha = _write_audit(current, recovered=True)
    assert validate_audit_binding(
        current, current_sha, expect_recovered=True
    )["status"] == "RECOVERED"


def test_candidate_overlay_changes_only_bounded_stne_q3_rows(tmp_path) -> None:
    base_dir = tmp_path / "base"
    supplement_dir = tmp_path / "supplement"
    output_dir = tmp_path / "candidate"
    base_dir.mkdir()
    supplement_dir.mkdir()
    annual = pd.DataFrame([{"ticker": "KEEP", "value": 1}])
    base = pd.concat([
        strict_quarterly_facts().assign(ticker="KEEP"),
        strict_quarterly_facts().assign(fiscal_end="2019-09-30"),
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
    assert report["inserted_strict_rows"] == 2
    assert len(merged) == len(base) + 2
    assert (output_dir / "annual.csv").read_bytes() == (
        base_dir / "annual.csv"
    ).read_bytes()
