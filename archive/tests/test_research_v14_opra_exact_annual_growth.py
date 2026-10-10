from copy import deepcopy
import hashlib
import json

import pandas as pd
import pytest

import scripts.research_v14_opra_exact_annual_growth as opra_growth
from scripts.research_v14_opra_exact_annual_growth import (
    AUDIT_OBSERVATIONS,
    AVAILABLE_DATE,
    FISCAL_END,
    LATER_AUDITED_VALUES_USD_THOUSANDS,
    OPERAND_ACCESSION,
    OUTPUT_COLUMNS,
    REJECTED_LATER_FILINGS,
    SOURCE_DOCUMENTS,
    SOURCE_ROW_CHECKS,
    SOURCE_TEXT_CHECKS,
    build,
    exact_ttm_evidence,
    resolve_audit_observations,
    strict_quarterly_facts,
    validate_exact_package,
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
            (source_id, check["metric"]), tuple(check["expected_values"])
        )
        cells = [f"<td>{check['line_item']}</td>", "<td></td>"]
        for value in values:
            cells.extend(("<td>$</td>", f"<td>{value:,}</td>", "<td></td>"))
        rows.append("<tr>" + "".join(cells) + "</tr>")
    return (
        "<html>" + paragraphs + "<table>" + "".join(rows) + "</table></html>"
    ).encode()


def _install_source_fixtures(
    tmp_path,
    monkeypatch,
    *,
    missing_source: str | None = None,
    value_overrides: dict[tuple[str, str], tuple[int, ...]] | None = None,
):
    sources = deepcopy(SOURCE_DOCUMENTS)
    downloads = {}
    for source_id, source in sources.items():
        raw = _fixture_source_bytes(source_id, value_overrides)
        source["expected_sha256"] = hashlib.sha256(raw).hexdigest()
        downloads[source["url"]] = raw
        if source_id != missing_source:
            local_path = tmp_path / source["local_path"]
            local_path.parent.mkdir(parents=True, exist_ok=True)
            local_path.write_bytes(raw)
    download_calls = []

    def fake_download(url: str) -> bytes:
        download_calls.append(url)
        return downloads[url]

    monkeypatch.setattr(opra_growth, "SOURCE_DOCUMENTS", sources)
    monkeypatch.setattr(opra_growth, "_download_source", fake_download)
    return sources, download_calls


def _snapshot_facts() -> pd.DataFrame:
    facts = strict_quarterly_facts().copy()
    facts["fiscal_end"] = pd.to_datetime(facts["fiscal_end"])
    facts["available_date"] = pd.to_datetime(facts["available_date"])
    return facts


def test_opra_exact_annual_ttm_arithmetic_and_growth() -> None:
    evidence = exact_ttm_evidence()
    revenue = evidence["derived"]["revenue"]
    profit = evidence["derived"]["net_income"]
    assert revenue["prior_ttm_usd_thousands"] == 177_078
    assert revenue["current_ttm_usd_thousands"] == 165_274
    assert revenue["growth"] == pytest.approx(-11_804 / 177_078)
    assert profit["prior_ttm_usd_thousands"] == 57_899
    assert profit["current_ttm_usd_thousands"] == 176_052
    assert profit["growth"] == pytest.approx(118_153 / 57_899)
    assert evidence["currency"] == "USD"
    assert evidence["accounting_standard"] == "IFRS as issued by IASB"
    validate_exact_package()


def test_strict_facts_are_one_complete_direct_annual_growth_package() -> None:
    facts = strict_quarterly_facts()
    assert list(facts.columns) == OUTPUT_COLUMNS
    assert len(facts) == 4
    assert set(facts["metric"]) == {
        "net_income_ttm",
        "net_income_growth",
        "revenue_ttm",
        "revenue_growth",
    }
    values = facts.set_index("metric")["value"]
    assert values["revenue_ttm"] == 165_274_000
    assert values["net_income_ttm"] == 176_052_000
    assert set(facts["fiscal_end"]) == {FISCAL_END}
    assert set(facts["available_date"]) == {AVAILABLE_DATE}
    assert set(facts["accession"]) == {OPERAND_ACCESSION}


def test_package_is_not_visible_before_filing_and_resolves_all_age_limits() -> None:
    facts = _snapshot_facts()
    before = quarterly_growth_snapshot(
        facts, pd.Timestamp("2021-02-24"), maximum_age_days=550
    )
    assert "OPRA" not in before.index
    for age in (150, 365, 550):
        after = quarterly_growth_snapshot(
            facts, pd.Timestamp("2021-02-26"), maximum_age_days=age
        )
        assert after.loc["OPRA", "fiscal_end"] == pd.Timestamp(FISCAL_END)
        assert after.loc["OPRA", "financial_age_days"] == 1
        assert after.loc["OPRA", "revenue_growth"] == pytest.approx(
            -11_804 / 177_078
        )
        assert after.loc["OPRA", "net_income_growth"] == pytest.approx(
            118_153 / 57_899
        )


def test_three_real_audit_observations_resolve_at_age_1() -> None:
    assert AUDIT_OBSERVATIONS == (
        ("liq2000000-age150-growth", "2021-02-26", 150),
        ("liq2000000-age365-growth", "2021-02-26", 365),
        ("liq2000000-age550-growth", "2021-02-26", 550),
    )
    resolution = resolve_audit_observations()
    assert resolution["resolved"].all()
    assert set(resolution["financial_age_days"]) == {1}
    assert set(resolution["decision"]) == {
        "complete_exact_direct_annual_ttm_growth_bundle"
    }


def test_official_source_dates_accessions_acceptance_and_hashes_are_locked() -> None:
    expected = {
        "6k_2021_02_25_cover": (
            "2021-02-25",
            "2021-02-25T12:09:46Z",
            "0001437749-21-004012",
            "5e46ef6f1cdece3d27a1cd274e8bf495fc22d093c654448f178ea332e6db0499",
            True,
        ),
        "6k_2021_02_25_exhibit_99_1": (
            "2021-02-25",
            "2021-02-25T12:09:46Z",
            "0001437749-21-004012",
            "b2eab71d97a727596d84e496f151d096ff2379cf735807f5edd30e3759ce5d8b",
            True,
        ),
        "20f_2021_06_11_later_revision_reference": (
            "2021-06-11",
            "2021-06-10T21:53:05Z",
            "0001437749-21-014514",
            "32dbddba2dc2ea06737196d918598568c210b9b1fe195840d53020431c5b149c",
            False,
        ),
    }
    assert {
        source_id: (
            source["filed"],
            source["accepted_at"],
            source["accession"],
            source["expected_sha256"],
            source["eligible_as_operand"],
        )
        for source_id, source in SOURCE_DOCUMENTS.items()
    } == expected
    validate_source_lock()


def test_source_lock_rejects_later_operand_or_nonlater_reference() -> None:
    sources = deepcopy(SOURCE_DOCUMENTS)
    sources["6k_2021_02_25_exhibit_99_1"]["filed"] = "2021-06-11"
    with pytest.raises(ValueError, match="violates PIT cutoff"):
        validate_source_lock(sources)

    sources = deepcopy(SOURCE_DOCUMENTS)
    sources["20f_2021_06_11_later_revision_reference"]["filed"] = "2021-02-26"
    with pytest.raises(ValueError, match="not a later revision reference"):
        validate_source_lock(sources)


def test_later_audited_revision_is_measured_but_never_backfilled() -> None:
    evidence = exact_ttm_evidence()
    later = evidence["later_audited_revision"]
    assert later["filed"] == "2021-06-11"
    assert later["used_in_pit_facts"] is False
    assert LATER_AUDITED_VALUES_USD_THOUSANDS["revenue"]["current_fy2020"] == 165_056
    assert LATER_AUDITED_VALUES_USD_THOUSANDS["net_income"]["current_fy2020"] == 179_174
    assert later["values"]["revenue"]["change_from_pit_operand_usd_thousands"] == -218
    assert later["values"]["net_income"]["change_from_pit_operand_usd_thousands"] == 3_122
    assert "0001437749-21-014514" not in set(strict_quarterly_facts()["accession"])
    assert REJECTED_LATER_FILINGS["0001437749-21-014514"]["filed"] == "2021-06-11"
    assert REJECTED_LATER_FILINGS["0001437749-21-015737"]["filed"] == "2021-06-28"


def test_build_downloads_missing_source_and_verifies_both_pit_and_later_rows(
    tmp_path, monkeypatch
) -> None:
    missing = "6k_2021_02_25_exhibit_99_1"
    sources, download_calls = _install_source_fixtures(
        tmp_path, monkeypatch, missing_source=missing
    )
    report = build(tmp_path)
    manifest = json.loads((tmp_path / "manifest.json").read_text())
    facts = pd.read_csv(tmp_path / "strict_quarterly_facts.csv")

    assert download_calls == [sources[missing]["url"]]
    assert report["accepted_direct_growth_package_count"] == 1
    assert report["accepted_fact_count"] == 4
    assert report["resolved_audit_observation_count"] == 3
    assert report["source_operand_verification_count"] == 8
    assert report["later_revision_value_verification_count"] == 6
    assert report["source_text_verification_count"] == 9
    assert len(facts) == 4
    assert manifest["sources"][missing]["downloaded"] is True
    assert manifest["sources"][missing]["actual_sha256"] == (
        sources[missing]["expected_sha256"]
    )


def test_build_rejects_operand_drift(tmp_path, monkeypatch) -> None:
    source_id = "6k_2021_02_25_exhibit_99_1"
    _install_source_fixtures(
        tmp_path,
        monkeypatch,
        value_overrides={
            (source_id, "revenue"): (48_763, 50_446, 177_078, 165_273)
        },
    )
    with pytest.raises(RuntimeError, match="source row changed"):
        build(tmp_path)


def test_build_rejects_later_revision_row_drift(tmp_path, monkeypatch) -> None:
    source_id = "20f_2021_06_11_later_revision_reference"
    _install_source_fixtures(
        tmp_path,
        monkeypatch,
        value_overrides={
            (source_id, "net_income"): (35_160, 57_899, 179_173)
        },
    )
    with pytest.raises(RuntimeError, match="source row changed"):
        build(tmp_path)


def test_build_rejects_sha_drift_before_using_source(tmp_path, monkeypatch) -> None:
    sources, _ = _install_source_fixtures(tmp_path, monkeypatch)
    source_id = "6k_2021_02_25_cover"
    path = tmp_path / sources[source_id]["local_path"]
    path.write_bytes(path.read_bytes() + b"drift")
    with pytest.raises(RuntimeError, match="SHA-256 mismatch"):
        build(tmp_path)
