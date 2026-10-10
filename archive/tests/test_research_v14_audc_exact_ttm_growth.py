from copy import deepcopy
import hashlib
import json

import pandas as pd
import pytest

import scripts.research_v14_audc_exact_ttm_growth as audc_growth
from scripts.research_v14_audc_exact_ttm_growth import (
    ACCOUNTING_STANDARD,
    AUDIT_OBSERVATIONS,
    AUDIT_SIGNAL_DATES,
    COMPARATIVE_MATCHES,
    OUTPUT_COLUMNS,
    PACKAGE_METADATA,
    REJECTED_LATER_FILINGS,
    SOURCE_DOCUMENTS,
    SOURCE_ROW_CHECKS,
    SOURCE_TEXT_CHECKS,
    build,
    exact_ttm_evidence,
    resolve_audit_observations,
    strict_quarterly_facts,
    validate_exact_packages,
    validate_source_lock,
)
from src.financial.quarterly_fundamentals import quarterly_growth_snapshot


EXPECTED_TTMS = {
    "2019-06-30": (166_553, 186_374, 6_549, 16_508),
    "2019-09-30": (171_864, 193_267, 9_635, 16_740),
    "2020-03-31": (180_377, 205_730, 14_108, 6_192),
    "2021-06-30": (209_753, 234_643, 8_037, 33_578),
    "2021-09-30": (214_905, 241_487, 10_634, 34_877),
}


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
    calls = []

    def fake_download(url: str) -> bytes:
        calls.append(url)
        return downloads[url]

    monkeypatch.setattr(audc_growth, "SOURCE_DOCUMENTS", sources)
    monkeypatch.setattr(audc_growth, "_download_source", fake_download)
    return sources, calls


def _snapshot_facts() -> pd.DataFrame:
    facts = strict_quarterly_facts().copy()
    facts["fiscal_end"] = pd.to_datetime(facts["fiscal_end"])
    facts["available_date"] = pd.to_datetime(facts["available_date"])
    return facts


def test_five_exact_ttm_packages_and_growth_arithmetic() -> None:
    packages = exact_ttm_evidence()["packages"]
    assert set(packages) == set(EXPECTED_TTMS)
    for fiscal_end, (prior_revenue, revenue, prior_profit, profit) in EXPECTED_TTMS.items():
        derived = packages[fiscal_end]["derived"]
        assert derived["revenue"]["prior_ttm_usd_thousands"] == prior_revenue
        assert derived["revenue"]["current_ttm_usd_thousands"] == revenue
        assert derived["revenue"]["growth"] == pytest.approx(
            (revenue - prior_revenue) / prior_revenue
        )
        assert derived["net_income"]["prior_ttm_usd_thousands"] == prior_profit
        assert derived["net_income"]["current_ttm_usd_thousands"] == profit
        assert derived["net_income"]["growth"] == pytest.approx(
            (profit - prior_profit) / prior_profit
        )
        assert packages[fiscal_end]["currency"] == "USD"
        assert packages[fiscal_end]["accounting_standard"] == ACCOUNTING_STANDARD
    validate_exact_packages()


def test_strict_facts_are_five_complete_direct_growth_packages() -> None:
    facts = strict_quarterly_facts()
    assert list(facts.columns) == OUTPUT_COLUMNS
    assert len(facts) == 20
    assert set(facts["metric"]) == {
        "net_income_ttm", "net_income_growth", "revenue_ttm", "revenue_growth"
    }
    assert set(facts.groupby(["fiscal_end", "available_date"])["metric"].nunique()) == {4}
    for fiscal_end, (_, revenue, _, profit) in EXPECTED_TTMS.items():
        values = facts.loc[facts["fiscal_end"].eq(fiscal_end)].set_index("metric")["value"]
        assert values["revenue_ttm"] == revenue * 1_000
        assert values["net_income_ttm"] == profit * 1_000


def test_real_snapshot_dates_select_latest_visible_package() -> None:
    facts = _snapshot_facts()
    assert "AUDC" not in quarterly_growth_snapshot(
        facts, pd.Timestamp("2019-07-22"), maximum_age_days=550
    ).index
    expected = {
        "2019-09-30": ("2019-06-30", 69),
        "2019-10-31": ("2019-09-30", 2),
        "2019-12-31": ("2019-09-30", 63),
        "2020-04-30": ("2020-03-31", 3),
        "2020-05-29": ("2020-03-31", 32),
        "2021-08-31": ("2021-06-30", 35),
        "2021-10-29": ("2021-09-30", 3),
        "2021-11-30": ("2021-09-30", 35),
    }
    for signal_date, (fiscal_end, age) in expected.items():
        for maximum_age_days in (150, 365, 550):
            snapshot = quarterly_growth_snapshot(
                facts, pd.Timestamp(signal_date), maximum_age_days=maximum_age_days
            )
            assert snapshot.loc["AUDC", "fiscal_end"] == pd.Timestamp(fiscal_end)
            assert snapshot.loc["AUDC", "financial_age_days"] == age


def test_all_27_real_audit_observations_resolve() -> None:
    assert len(AUDIT_OBSERVATIONS) == 27
    assert len(AUDIT_SIGNAL_DATES) == 8
    resolution = resolve_audit_observations()
    assert len(resolution) == 27
    assert resolution["resolved"].all()
    assert resolution["signal_date"].nunique() == 8
    assert (resolution["signal_date"] == "2020-05-29").sum() == 6
    assert set(resolution["decision"]) == {
        "complete_exact_cumulative_ttm_growth_bundle"
    }


def test_incomplete_direct_package_is_not_accepted() -> None:
    facts = _snapshot_facts()
    facts = facts.loc[
        ~(
            facts["fiscal_end"].eq(pd.Timestamp("2021-09-30"))
            & facts["metric"].eq("revenue_growth")
        )
    ]
    snapshot = quarterly_growth_snapshot(
        facts, pd.Timestamp("2021-11-30"), maximum_age_days=150
    )
    assert "AUDC" not in snapshot.index


def test_source_dates_accessions_and_hashes_are_locked() -> None:
    assert len(SOURCE_DOCUMENTS) == 13
    assert SOURCE_DOCUMENTS["20f_fy2018"]["accession"] == "0001144204-19-014779"
    assert SOURCE_DOCUMENTS["20f_fy2018"]["expected_sha256"] == (
        "1de0f7208ee98dbde5e5ab8f8f3d4d3452a3d3603dc5b5e4581fdafb6bfb7844"
    )
    assert SOURCE_DOCUMENTS["6k_q2_2019"]["accepted_at"] == "2019-07-23T13:00:35Z"
    assert SOURCE_DOCUMENTS["6k_q2_2019"]["expected_sha256"] == (
        "8ada0f06a765a414d7113f9c44cfd817d645812d7a222b02fc894c57e829a325"
    )
    assert SOURCE_DOCUMENTS["6k_q3_2021"]["accession"] == "0001104659-21-129771"
    assert SOURCE_DOCUMENTS["6k_q3_2021"]["expected_sha256"] == (
        "1ffeb8b94fa26cbbd5e0e88919c80933372bc2611d9ebc96ce9c705c0c303541"
    )
    validate_source_lock()


def test_each_package_available_date_is_its_latest_operand_filing() -> None:
    for package in PACKAGE_METADATA.values():
        source_dates = [SOURCE_DOCUMENTS[s]["filed"] for s in package["source_ids"]]
        assert max(source_dates) == package["available_date"]
    sources = deepcopy(SOURCE_DOCUMENTS)
    sources["6k_q3_2021"]["filed"] = "2021-12-01"
    with pytest.raises(ValueError, match="violates PIT cutoff"):
        validate_source_lock(sources)


def test_original_and_later_comparatives_match_without_restating_operands() -> None:
    evidence = exact_ttm_evidence()
    assert evidence["comparative_match_count"] == 14
    assert len(COMPARATIVE_MATCHES) == 14
    assert all(item["matched"] for item in evidence["comparative_matches"])
    assert "match their original filings" in evidence["restatement_isolation"]
    assert REJECTED_LATER_FILINGS["0001104659-22-010030"]["filed"] == "2022-02-01"
    assert REJECTED_LATER_FILINGS["0001410578-22-001057"]["filed"] == "2022-04-28"


def test_build_downloads_missing_source_and_emits_empty_unrecoverable_file(
    tmp_path, monkeypatch
) -> None:
    missing = "6k_q3_2021"
    sources, calls = _install_source_fixtures(
        tmp_path, monkeypatch, missing_source=missing
    )
    report = build(tmp_path)
    manifest = json.loads((tmp_path / "manifest.json").read_text())
    facts = pd.read_csv(tmp_path / "strict_quarterly_facts.csv")
    unrecoverable = json.loads((tmp_path / "unrecoverable_observations.json").read_text())
    assert calls == [sources[missing]["url"]]
    assert report["accepted_direct_growth_package_count"] == 5
    assert report["accepted_fact_count"] == 20
    assert report["resolved_audit_observation_count"] == 27
    assert report["unrecoverable_observation_count"] == 0
    assert report["source_operand_verification_count"] == 90
    assert report["source_text_verification_count"] == 39
    assert len(facts) == 20
    assert unrecoverable == []
    assert manifest["sources"][missing]["actual_sha256"] == sources[missing]["expected_sha256"]


def test_build_rejects_operand_drift(tmp_path, monkeypatch) -> None:
    source_id = "6k_q2_2019"
    _install_source_fixtures(
        tmp_path,
        monkeypatch,
        value_overrides={
            (source_id, "revenue"): (96_078, 85_927, 49_498, 43_502)
        },
    )
    with pytest.raises(RuntimeError, match="source row changed"):
        build(tmp_path)


def test_build_rejects_sha_drift(tmp_path, monkeypatch) -> None:
    sources, _ = _install_source_fixtures(tmp_path, monkeypatch)
    source_id = "20f_fy2020"
    path = tmp_path / sources[source_id]["local_path"]
    path.write_bytes(path.read_bytes() + b"drift")
    with pytest.raises(RuntimeError, match="SHA-256 mismatch"):
        build(tmp_path)
