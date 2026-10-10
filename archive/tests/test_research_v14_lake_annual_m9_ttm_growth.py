from copy import deepcopy
import hashlib
import json

import pandas as pd
import pytest

import scripts.research_v14_lake_annual_m9_ttm_growth as lake_growth
from scripts.research_v14_lake_annual_m9_ttm_growth import (
    AUDIT_OBSERVATIONS,
    AVAILABLE_DATE,
    EXPECTED_TTM_USD_THOUSANDS,
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


def _install_source_fixtures(tmp_path, monkeypatch, *, value_overrides=None):
    sources = deepcopy(SOURCE_DOCUMENTS)
    downloads = {}
    for source_id, source in sources.items():
        raw = _fixture_source_bytes(source_id, value_overrides)
        source["expected_sha256"] = hashlib.sha256(raw).hexdigest()
        downloads[lake_growth._source_url(source)] = raw

    calls = []

    def fake_download(url: str) -> bytes:
        calls.append(url)
        return downloads[url]

    monkeypatch.setattr(lake_growth, "SOURCE_DOCUMENTS", sources)
    monkeypatch.setattr(lake_growth, "_download_bytes", fake_download)
    return sources, calls


def _write_audit(path, *, recovered: bool) -> str:
    rows = []
    if not recovered:
        for scenario, _age in AUDIT_OBSERVATIONS:
            rows.append({
                "scenario": scenario,
                "ticker": "LAKE",
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


def test_lake_exact_ttm_arithmetic_and_growth() -> None:
    evidence = ttm_evidence()
    assert OPERANDS_USD_THOUSANDS["fy2019"] == {
        "revenue": 99_011, "net_income": 1_459
    }
    assert EXPECTED_TTM_USD_THOUSANDS == {
        "previous": {"revenue": 99_126, "net_income": -1_532},
        "current": {"revenue": 104_661, "net_income": 149},
    }
    assert evidence["derived"]["growth"]["revenue"] == pytest.approx(
        5_535 / 99_126
    )
    assert evidence["derived"]["growth"]["net_income"] == pytest.approx(
        1_681 / 1_532
    )
    assert evidence["accounting_boundary"]["later_fy2020_10k_excluded"]


def test_strict_facts_are_complete_annual_m9_bundle() -> None:
    facts = strict_quarterly_facts()
    assert list(facts.columns) == OUTPUT_COLUMNS
    assert len(facts) == 4
    assert set(facts["metric"]) == {
        "revenue_ttm", "net_income_ttm", "revenue_growth", "net_income_growth"
    }
    values = facts.set_index("metric")["value"]
    assert values["revenue_ttm"] == 104_661_000
    assert values["net_income_ttm"] == 149_000
    assert set(facts["fiscal_end"]) == {FISCAL_END}
    assert set(facts["available_date"]) == {AVAILABLE_DATE}
    assert set(facts["accession"]) == {"0001654954-19-013729"}


def test_package_is_not_visible_before_latest_filing_and_resolves_all_ages() -> None:
    facts = strict_quarterly_facts().copy()
    facts["fiscal_end"] = pd.to_datetime(facts["fiscal_end"])
    facts["available_date"] = pd.to_datetime(facts["available_date"])
    before = quarterly_growth_snapshot(
        facts, pd.Timestamp("2019-12-08"), maximum_age_days=550
    )
    assert "LAKE" not in before.index
    for age in (150, 365, 550):
        after = quarterly_growth_snapshot(
            facts, pd.Timestamp(SIGNAL_DATE), maximum_age_days=age
        )
        assert after.loc["LAKE", "financial_age_days"] == 81
        assert after.loc["LAKE", "revenue_growth"] > 0
        assert after.loc["LAKE", "net_income_growth"] > 0


def test_three_real_audit_observations_resolve_at_age_81() -> None:
    assert AUDIT_OBSERVATIONS == (
        ("liq2000000-age150-growth", 150),
        ("liq2000000-age365-growth", 365),
        ("liq2000000-age550-growth", 550),
    )
    resolution = recovered_observations()
    assert resolution["resolved"].all()
    assert set(resolution["financial_age_days"]) == {81}
    assert set(resolution["decision"]) == {
        "recovered_exact_us_gaap_annual_m9_ttm_growth_bundle"
    }


def test_official_source_identities_dates_and_hashes_are_locked() -> None:
    expected = {
        "fy2019_10k": (
            "2019-04-16", "0001654954-19-004479",
            "618e8634f710351a49ee51d8bc17aebc9191b6c4a68a31bf72b288a780429910",
        ),
        "q3_2018_10q": (
            "2018-12-17", "0001654954-18-014004",
            "ba7fc2840b75d25106b5e23dc58c4216776702a19106186166938957ce660729",
        ),
        "q3_2019_10q": (
            "2019-12-09", "0001654954-19-013729",
            "7f669f272da232c53bb0ee98918aafea7a4c2643920f02a77b855c8ced80c7a3",
        ),
    }
    assert {
        source_id: (
            source["filed"], source["accession"], source["expected_sha256"]
        )
        for source_id, source in SOURCE_DOCUMENTS.items()
    } == expected
    validate_source_lock()


def test_source_lock_rejects_post_signal_identity_or_sha_drift() -> None:
    sources = deepcopy(SOURCE_DOCUMENTS)
    sources["q3_2019_10q"]["filed"] = "2020-03-01"
    with pytest.raises(ValueError, match="violates the PIT cutoff"):
        validate_source_lock(sources)

    sources = deepcopy(SOURCE_DOCUMENTS)
    sources["q3_2019_10q"]["accession"] = "0000000000-00-000000"
    with pytest.raises(ValueError, match="changed locked identity field accession"):
        validate_source_lock(sources)

    sources = deepcopy(SOURCE_DOCUMENTS)
    sources["q3_2019_10q"]["expected_sha256"] = "bad"
    with pytest.raises(ValueError, match="invalid SHA-256"):
        validate_source_lock(sources)


def test_build_downloads_and_verifies_all_six_rows(tmp_path, monkeypatch) -> None:
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
        lake_growth._source_url(sources[source_id])
        for source_id in SOURCE_DOCUMENTS
    ]
    assert report["accepted_fact_count"] == 4
    assert report["resolved_audit_observation_count"] == 3
    assert len(report["source_value_verification"]) == 6
    assert manifest["release_status"] == "BLOCKED"
    assert manifest["formal_financials_modified"] is False


def test_build_rejects_source_row_drift(tmp_path, monkeypatch) -> None:
    _install_source_fixtures(
        tmp_path,
        monkeypatch,
        value_overrides={
            ("q3_2019_10q", "q3_2019_net_sales"):
                (27_464, 24_009, 79_619, 73_970)
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


def test_candidate_overlay_changes_only_bounded_lake_rows(tmp_path) -> None:
    base_dir = tmp_path / "base"
    supplement_dir = tmp_path / "supplement"
    output_dir = tmp_path / "candidate"
    base_dir.mkdir()
    supplement_dir.mkdir()
    annual = pd.DataFrame([{"ticker": "KEEP", "value": 1}])
    base = pd.concat([
        strict_quarterly_facts().assign(ticker="KEEP"),
        strict_quarterly_facts().assign(fiscal_end="2018-10-31"),
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
