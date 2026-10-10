from copy import deepcopy
import hashlib
import json

import pandas as pd
import pytest

import scripts.research_v14_lgo_exact_annual_growth as lgo_growth
from scripts.research_v14_lgo_exact_annual_growth import (
    AUDIT_OBSERVATIONS,
    AVAILABLE_DATE,
    FISCAL_END,
    OPERAND_ACCESSION,
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
    for check in SOURCE_ROW_CHECKS.get(source_id, ()):
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
        downloads[lgo_growth._source_url(source)] = raw

    calls = []

    def fake_download(url: str) -> bytes:
        calls.append(url)
        return downloads[url]

    monkeypatch.setattr(lgo_growth, "SOURCE_DOCUMENTS", sources)
    monkeypatch.setattr(lgo_growth, "_download_bytes", fake_download)
    return sources, calls


def _write_audit(path, *, recovered: bool) -> str:
    rows = []
    if not recovered:
        for scenario, _age in AUDIT_OBSERVATIONS:
            rows.append({
                "scenario": scenario,
                "ticker": "LGO",
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


def test_lgo_exact_annual_arithmetic_and_currency_boundary() -> None:
    evidence = ttm_evidence()
    assert OPERANDS_USD_THOUSANDS == {
        "fy2019": {"revenue": 105_107, "net_income": -26_970},
        "fy2020": {"revenue": 119_987, "net_income": 6_761},
    }
    assert evidence["derived"]["growth"]["revenue"] == pytest.approx(
        14_880 / 105_107
    )
    assert evidence["derived"]["growth"]["net_income"] == pytest.approx(
        33_731 / 26_970
    )
    boundary = evidence["accounting_boundary"]
    assert boundary["presentation_currency"] == "USD"
    assert boundary["fy2019_comparatives_represented_in_usd"]
    assert boundary["later_40f_amendments_excluded"]


def test_strict_facts_are_complete_direct_annual_bundle() -> None:
    facts = strict_quarterly_facts()
    assert list(facts.columns) == OUTPUT_COLUMNS
    assert len(facts) == 4
    assert set(facts["metric"]) == {
        "revenue_ttm", "net_income_ttm", "revenue_growth", "net_income_growth"
    }
    values = facts.set_index("metric")["value"]
    assert values["revenue_ttm"] == 119_987_000
    assert values["net_income_ttm"] == 6_761_000
    assert set(facts["fiscal_end"]) == {FISCAL_END}
    assert set(facts["available_date"]) == {AVAILABLE_DATE}
    assert set(facts["accession"]) == {OPERAND_ACCESSION}


def test_package_is_not_visible_before_filing_and_resolves_all_ages() -> None:
    facts = strict_quarterly_facts().copy()
    facts["fiscal_end"] = pd.to_datetime(facts["fiscal_end"])
    facts["available_date"] = pd.to_datetime(facts["available_date"])
    before = quarterly_growth_snapshot(
        facts, pd.Timestamp("2021-04-13"), maximum_age_days=550
    )
    assert "LGO" not in before.index
    for age in (150, 365, 550):
        after = quarterly_growth_snapshot(
            facts, pd.Timestamp(SIGNAL_DATE), maximum_age_days=age
        )
        assert after.loc["LGO", "financial_age_days"] == 77
        assert after.loc["LGO", "revenue_growth"] > 0
        assert after.loc["LGO", "net_income_growth"] > 0


def test_three_real_audit_observations_resolve_at_age_77() -> None:
    assert AUDIT_OBSERVATIONS == (
        ("liq2000000-age150-growth", 150),
        ("liq2000000-age365-growth", 365),
        ("liq2000000-age550-growth", 550),
    )
    resolution = recovered_observations()
    assert resolution["resolved"].all()
    assert set(resolution["financial_age_days"]) == {77}
    assert set(resolution["decision"]) == {
        "recovered_exact_ifrs_direct_annual_ttm_growth_bundle"
    }


def test_official_cover_exhibit_identity_date_and_hash_are_locked() -> None:
    expected = {
        "40fr12b_cover": (
            "2021-04-14T07:43:04Z",
            "a21-6618_140fr12b.htm",
            "7ce53d7e05dd7e6817106442a2aabbfc421c4cfdee5bafdacf6d13b8f27229a3",
        ),
        "40fr12b_exhibit_99_69": (
            "2021-04-14T07:43:04Z",
            "a21-6618_1ex99d69.htm",
            "2346f0f961ba79aed963fc535221e43177c4233d84b08bff0b92d7b099c55e5d",
        ),
    }
    assert {
        source_id: (
            source["accepted_at"], source["document"], source["expected_sha256"]
        )
        for source_id, source in SOURCE_DOCUMENTS.items()
    } == expected
    assert {source["accession"] for source in SOURCE_DOCUMENTS.values()} == {
        OPERAND_ACCESSION
    }
    validate_source_lock()


def test_source_lock_rejects_post_signal_identity_or_sha_drift() -> None:
    sources = deepcopy(SOURCE_DOCUMENTS)
    sources["40fr12b_exhibit_99_69"]["filed"] = "2021-07-01"
    with pytest.raises(ValueError, match="violates the PIT cutoff"):
        validate_source_lock(sources)

    sources = deepcopy(SOURCE_DOCUMENTS)
    sources["40fr12b_exhibit_99_69"]["document"] = "wrong.htm"
    with pytest.raises(ValueError, match="changed locked identity field document"):
        validate_source_lock(sources)

    sources = deepcopy(SOURCE_DOCUMENTS)
    sources["40fr12b_cover"]["expected_sha256"] = "bad"
    with pytest.raises(ValueError, match="invalid SHA-256"):
        validate_source_lock(sources)


def test_build_downloads_cover_and_exhibit_and_verifies_rows(
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
        lgo_growth._source_url(sources[source_id])
        for source_id in SOURCE_DOCUMENTS
    ]
    assert report["accepted_fact_count"] == 4
    assert report["resolved_audit_observation_count"] == 3
    assert len(report["source_value_verification"]) == 2
    assert manifest["release_status"] == "BLOCKED"
    assert manifest["formal_financials_modified"] is False


def test_build_rejects_source_row_drift(tmp_path, monkeypatch) -> None:
    _install_source_fixtures(
        tmp_path,
        monkeypatch,
        value_overrides={
            ("40fr12b_exhibit_99_69", "annual_revenues"):
                (119_986, 105_107)
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


def test_candidate_overlay_changes_only_bounded_lgo_rows(tmp_path) -> None:
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
