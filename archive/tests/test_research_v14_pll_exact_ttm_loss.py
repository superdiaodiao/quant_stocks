from copy import deepcopy
import hashlib
import json

import pandas as pd
import pytest

import scripts.research_v14_pll_exact_ttm_loss as pll_loss
from scripts.research_v14_pll_exact_ttm_loss import (
    AUDIT_OBSERVATIONS,
    AVAILABLE_DATE,
    EXPECTED_TTM_USD,
    FISCAL_END,
    OPERANDS_USD,
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


def _install_source_fixtures(tmp_path, monkeypatch, *, value_overrides=None):
    sources = deepcopy(SOURCE_DOCUMENTS)
    downloads = {}
    for source_id, source in sources.items():
        raw = _fixture_source_bytes(source_id, value_overrides)
        source["expected_sha256"] = hashlib.sha256(raw).hexdigest()
        downloads[pll_loss._source_url(source)] = raw

    calls = []

    def fake_download(url: str) -> bytes:
        calls.append(url)
        return downloads[url]

    monkeypatch.setattr(pll_loss, "SOURCE_DOCUMENTS", sources)
    monkeypatch.setattr(pll_loss, "_download_bytes", fake_download)
    return sources, calls


def _write_audit(path, *, recovered: bool) -> str:
    rows = []
    if not recovered:
        for scenario, _age in AUDIT_OBSERVATIONS:
            rows.append({
                "scenario": scenario,
                "ticker": "PLL",
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


def test_exact_annual_h1_math_proves_negative_ttm() -> None:
    evidence = ttm_evidence()
    assert OPERANDS_USD == {
        "fy2020": -5_671_099,
        "h1_2020": -3_475_979,
        "h1_2021": -5_465_327,
    }
    assert evidence["net_income_ttm_usd"] == EXPECTED_TTM_USD == -7_660_447
    assert evidence["formula"] == "FY2020 - H1_FY2020 + H1_FY2021"
    boundary = evidence["accounting_boundary"]
    assert boundary["presentation_currency"] == "USD"
    assert boundary["interim_accounting_policies_consistent_with_annual"]
    assert boundary["later_us_gaap_redomiciliation_filings_excluded"]


def test_strict_fact_is_only_the_exact_negative_profit_state() -> None:
    facts = strict_quarterly_facts()
    assert list(facts.columns) == OUTPUT_COLUMNS
    assert len(facts) == 1
    assert set(facts["metric"]) == {"net_income_ttm"}
    assert facts.iloc[0]["value"] == EXPECTED_TTM_USD
    assert facts.iloc[0]["fiscal_end"] == FISCAL_END
    assert facts.iloc[0]["available_date"] == AVAILABLE_DATE


def test_loss_is_pit_hidden_before_6k_and_resolves_without_growth() -> None:
    facts = strict_quarterly_facts().copy()
    facts["fiscal_end"] = pd.to_datetime(facts["fiscal_end"])
    facts["available_date"] = pd.to_datetime(facts["available_date"])
    before = quarterly_profit_ttm_snapshot(
        facts, pd.Timestamp("2021-03-17"), maximum_age_days=550
    )
    at_signal = quarterly_profit_ttm_snapshot(
        facts, pd.Timestamp(SIGNAL_DATE), maximum_age_days=150
    )
    growth = quarterly_growth_snapshot(
        facts, pd.Timestamp(SIGNAL_DATE), maximum_age_days=150
    )
    assert "PLL" not in before.index
    assert at_signal.loc["PLL", "financial_age_days"] == 13
    assert at_signal.loc["PLL", "net_income_ttm"] == EXPECTED_TTM_USD
    assert growth.empty


def test_both_real_audit_observations_resolve_as_nonpositive_profit() -> None:
    assert AUDIT_OBSERVATIONS == (
        ("liq2000000-age150-growth", 150),
        ("liq10000000-age150-growth", 150),
    )
    resolution = recovered_observations()
    assert resolution["resolved"].all()
    assert set(resolution["financial_age_days"]) == {13}
    assert set(resolution["decision"]) == {"known_nonpositive_profit"}


def test_official_20f_and_6k_identity_date_and_hash_are_locked() -> None:
    assert len(SOURCE_DOCUMENTS) == 2
    assert SOURCE_DOCUMENTS["h1_2021_6k"]["accepted_at"] == (
        "2021-03-18T09:49:53Z"
    )
    assert SOURCE_DOCUMENTS["h1_2021_6k"]["expected_sha256"] == (
        "b6e617aa9c1b474bd28f0e15feb14a2740650d9337a1a609467ca13bcdb2584b"
    )
    validate_source_lock()


def test_source_lock_rejects_post_signal_identity_or_sha_drift() -> None:
    sources = deepcopy(SOURCE_DOCUMENTS)
    sources["h1_2021_6k"]["filed"] = "2021-04-01"
    with pytest.raises(ValueError, match="violates the PIT cutoff"):
        validate_source_lock(sources)

    sources = deepcopy(SOURCE_DOCUMENTS)
    sources["fy2020_20f"]["document"] = "wrong.htm"
    with pytest.raises(ValueError, match="changed locked identity field document"):
        validate_source_lock(sources)

    sources = deepcopy(SOURCE_DOCUMENTS)
    sources["fy2020_20f"]["expected_sha256"] = "bad"
    with pytest.raises(ValueError, match="invalid SHA-256"):
        validate_source_lock(sources)


def test_build_downloads_sources_and_verifies_statement_rows(
    tmp_path, monkeypatch
) -> None:
    sources, calls = _install_source_fixtures(tmp_path, monkeypatch)
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
        pll_loss._source_url(sources[source_id]) for source_id in SOURCE_DOCUMENTS
    ]
    assert report["accepted_fact_count"] == 1
    assert report["resolved_audit_observation_count"] == 2
    assert len(report["source_value_verification"]) == 2
    assert manifest["release_status"] == "BLOCKED"
    assert manifest["formal_financials_modified"] is False


def test_build_rejects_source_row_drift(tmp_path, monkeypatch) -> None:
    _install_source_fixtures(
        tmp_path,
        monkeypatch,
        value_overrides={
            ("h1_2021_6k", "half_year_profit_loss"):
                (-5_465_326, -3_475_979)
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


def test_candidate_overlay_changes_only_bounded_pll_row(tmp_path) -> None:
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
    assert report["inserted_strict_rows"] == 1
    assert len(merged) == len(base) + 1
    assert (output_dir / "annual.csv").read_bytes() == (
        base_dir / "annual.csv"
    ).read_bytes()
