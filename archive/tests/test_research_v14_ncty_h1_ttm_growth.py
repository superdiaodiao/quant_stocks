from copy import deepcopy
import hashlib
import json

import pandas as pd
import pytest

import scripts.research_v14_ncty_h1_ttm_growth as ncty_growth
from scripts.research_v14_ncty_h1_ttm_growth import (
    AUDIT_OBSERVATIONS,
    AVAILABLE_DATE,
    EXPECTED_TTM_RMB,
    FISCAL_END,
    OPERANDS_RMB,
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
            cells.extend(("<td>RMB</td>", f"<td>{display}</td>", "<td></td>"))
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
        downloads[ncty_growth._source_url(source)] = raw

    calls = []

    def fake_download(url: str) -> bytes:
        calls.append(url)
        return downloads[url]

    monkeypatch.setattr(ncty_growth, "SOURCE_DOCUMENTS", sources)
    monkeypatch.setattr(ncty_growth, "_download_bytes", fake_download)
    return sources, calls


def _write_audit(path, *, recovered: bool) -> str:
    rows = []
    if not recovered:
        rows.append({
            "scenario": "liq2000000-age150-growth",
            "ticker": "NCTY",
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


def test_exact_annual_h1_ttm_arithmetic_uses_one_rmb_basis() -> None:
    evidence = ttm_evidence()
    assert OPERANDS_RMB["fy2019"] == {
        "revenue": 341_495, "net_income": -177_795_168
    }
    assert EXPECTED_TTM_RMB == {
        "prior": {"revenue": 7_223_099, "net_income": -182_584_435},
        "current": {"revenue": 555_894, "net_income": 313_264_651},
    }
    assert evidence["derived"]["growth"]["revenue"] == pytest.approx(
        -6_667_205 / 7_223_099
    )
    assert evidence["derived"]["growth"]["net_income"] == pytest.approx(
        495_849_086 / 182_584_435
    )
    boundary = evidence["accounting_boundary"]
    assert boundary["presentation_currency"] == "RMB"
    assert boundary["same_currency_all_operands"]
    assert boundary["usd_convenience_translations_excluded"]
    assert boundary["post_signal_fy2020_20f_excluded"]


def test_strict_facts_are_complete_direct_ttm_bundle() -> None:
    facts = strict_quarterly_facts()
    assert list(facts.columns) == OUTPUT_COLUMNS
    assert len(facts) == 4
    assert set(facts["metric"]) == {
        "revenue_ttm", "net_income_ttm", "revenue_growth", "net_income_growth"
    }
    values = facts.set_index("metric")["value"]
    assert values["revenue_ttm"] == 555_894
    assert values["net_income_ttm"] == 313_264_651
    assert values["revenue_growth"] < 0
    assert values["net_income_growth"] > 0
    assert set(facts["fiscal_end"]) == {FISCAL_END}
    assert set(facts["available_date"]) == {AVAILABLE_DATE}


def test_package_is_pit_hidden_before_6k_and_complete_at_signal() -> None:
    facts = strict_quarterly_facts().copy()
    facts["fiscal_end"] = pd.to_datetime(facts["fiscal_end"])
    facts["available_date"] = pd.to_datetime(facts["available_date"])
    before = quarterly_growth_snapshot(
        facts, pd.Timestamp("2020-12-29"), maximum_age_days=550
    )
    at_signal = quarterly_growth_snapshot(
        facts, pd.Timestamp(SIGNAL_DATE), maximum_age_days=150
    )
    assert "NCTY" not in before.index
    assert at_signal.loc["NCTY", "financial_age_days"] == 30
    assert at_signal.loc["NCTY", "revenue_growth"] < 0
    assert at_signal.loc["NCTY", "net_income_ttm"] > 0


def test_real_audit_observation_resolves_but_fails_revenue_gate() -> None:
    assert AUDIT_OBSERVATIONS == (("liq2000000-age150-growth", 150),)
    resolution = recovered_observations()
    assert resolution["resolved"].all()
    assert set(resolution["financial_age_days"]) == {30}
    assert not resolution["passes_revenue_growth_gate"].any()
    assert set(resolution["decision"]) == {
        "recovered_exact_us_gaap_annual_h1_ttm_growth_bundle"
    }


def test_five_official_documents_lock_identity_date_and_hash() -> None:
    assert len(SOURCE_DOCUMENTS) == 5
    assert SOURCE_DOCUMENTS["h1_2020_6k"] == {
        "role": "unaudited_h1_2019_comparative_and_h1_2020_operands",
        "form": "6-K Exhibit 99.1",
        "filed": "2020-12-30",
        "accepted_at": "2020-12-30T11:42:52Z",
        "accession": "0001104659-20-140356",
        "document": "tm2039591d1_ex99-1.htm",
        "expected_sha256": (
            "0ba791af1d3cbd677918a91f3b24f5666464b40ea66c42195a43e33d60d6f089"
        ),
    }
    validate_source_lock()


def test_source_lock_rejects_post_signal_identity_or_sha_drift() -> None:
    sources = deepcopy(SOURCE_DOCUMENTS)
    sources["h1_2020_6k"]["filed"] = "2021-01-30"
    with pytest.raises(ValueError, match="violates the PIT cutoff"):
        validate_source_lock(sources)

    sources = deepcopy(SOURCE_DOCUMENTS)
    sources["h1_2019_6k"]["document"] = "wrong.htm"
    with pytest.raises(ValueError, match="changed locked identity field document"):
        validate_source_lock(sources)

    sources = deepcopy(SOURCE_DOCUMENTS)
    sources["fy2018_20f"]["expected_sha256"] = "bad"
    with pytest.raises(ValueError, match="invalid SHA-256"):
        validate_source_lock(sources)


def test_build_downloads_all_sources_and_verifies_ten_rows(
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
        ncty_growth._source_url(sources[source_id])
        for source_id in SOURCE_DOCUMENTS
    ]
    assert report["accepted_fact_count"] == 4
    assert report["resolved_audit_observation_count"] == 1
    assert len(report["source_value_verification"]) == 10
    assert manifest["release_status"] == "BLOCKED"
    assert manifest["formal_financials_modified"] is False


def test_build_rejects_source_row_drift(tmp_path, monkeypatch) -> None:
    _install_source_fixtures(
        tmp_path,
        monkeypatch,
        value_overrides={
            ("h1_2020_6k", "h1_2020_revenue"): (251_327, 465_725, 65_919)
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
    )["missing_observation_count"] == 1

    current = tmp_path / "current.csv"
    current_sha = _write_audit(current, recovered=True)
    assert validate_audit_binding(
        current, current_sha, expect_recovered=True
    )["status"] == "RECOVERED"


def test_candidate_overlay_changes_only_bounded_ncty_rows(tmp_path) -> None:
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
