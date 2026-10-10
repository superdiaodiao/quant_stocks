from copy import deepcopy
import hashlib
import json

import pandas as pd
import pytest

import scripts.research_v14_gain_exact_ttm_growth as gain_growth
from scripts.research_v14_gain_exact_ttm_growth import (
    AUDIT_OBSERVATIONS,
    AVAILABLE_DATE,
    FISCAL_END,
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
    for check in SOURCE_ROW_CHECKS[source_id]:
        values = overrides.get(
            (source_id, check["metric"]), tuple(check["expected_values"])
        )
        cells = [f"<td>{check['line_item']}</td>", "<td></td>"]
        for value in values:
            cells.extend(("<td>$</td>", f"<td>{value:,}</td>", "<td></td>"))
        rows.append("<tr>" + "".join(cells) + "</tr>")
    return ("<html>" + paragraphs + "<table>" + "".join(rows) + "</table></html>").encode()


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

    monkeypatch.setattr(gain_growth, "SOURCE_DOCUMENTS", sources)
    monkeypatch.setattr(gain_growth, "_download_source", fake_download)
    return sources, download_calls


def _snapshot_facts() -> pd.DataFrame:
    facts = strict_quarterly_facts().copy()
    facts["fiscal_end"] = pd.to_datetime(facts["fiscal_end"])
    facts["available_date"] = pd.to_datetime(facts["available_date"])
    return facts


def test_gain_exact_ttm_arithmetic_and_growth() -> None:
    evidence = exact_ttm_evidence()
    revenue = evidence["derived"]["revenue"]
    profit = evidence["derived"]["net_income"]
    assert revenue["prior_ttm_usd_thousands"] == 60_198
    assert revenue["current_ttm_usd_thousands"] == 65_014
    assert revenue["growth"] == pytest.approx(4_816 / 60_198)
    assert profit["prior_ttm_usd_thousands"] == 101_681
    assert profit["current_ttm_usd_thousands"] == 35_949
    assert profit["growth"] == pytest.approx(-65_732 / 101_681)
    assert evidence["currency"] == "USD"
    assert evidence["accounting_standard"] == "US-GAAP / ASC 946"
    validate_exact_package()


def test_strict_facts_are_one_complete_direct_growth_package() -> None:
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
    assert values["revenue_ttm"] == 65_014_000
    assert values["net_income_ttm"] == 35_949_000
    assert set(facts["fiscal_end"]) == {FISCAL_END}
    assert set(facts["available_date"]) == {AVAILABLE_DATE}


def test_package_is_not_visible_before_filing_and_resolves_all_age_limits() -> None:
    facts = _snapshot_facts()
    before = quarterly_growth_snapshot(
        facts, pd.Timestamp("2019-10-31"), maximum_age_days=550
    )
    assert "GAIN" not in before.index
    for age in (150, 365, 550):
        after = quarterly_growth_snapshot(
            facts, pd.Timestamp("2019-12-31"), maximum_age_days=age
        )
        assert after.loc["GAIN", "fiscal_end"] == pd.Timestamp(FISCAL_END)
        assert after.loc["GAIN", "financial_age_days"] == 57
        assert after.loc["GAIN", "revenue_growth"] == pytest.approx(4_816 / 60_198)
        assert after.loc["GAIN", "net_income_growth"] < 0


def test_three_real_audit_observations_resolve_at_age_57() -> None:
    assert AUDIT_OBSERVATIONS == (
        ("liq2000000-age150-growth", "2019-12-31", 150),
        ("liq2000000-age365-growth", "2019-12-31", 365),
        ("liq2000000-age550-growth", "2019-12-31", 550),
    )
    resolution = resolve_audit_observations()
    assert resolution["resolved"].all()
    assert set(resolution["financial_age_days"]) == {57}
    assert set(resolution["decision"]) == {"complete_exact_ttm_growth_bundle"}


def test_original_and_comparative_values_prove_no_basis_revision() -> None:
    evidence = exact_ttm_evidence()
    operands = evidence["operands_usd_thousands"]
    assert operands["revenue"]["fy2018"] == 58_355
    assert operands["net_income"]["fy2018"] == 60_687
    assert operands["revenue"]["h1_fy2019"] == 28_595
    assert operands["net_income"]["h1_fy2019"] == 62_691
    assert "match the 2018 10-K" in evidence["restatement_isolation"]
    assert "match the original 2018Q2 10-Q" in evidence["restatement_isolation"]


def test_official_source_dates_accessions_and_hashes_are_locked() -> None:
    expected = {
        "10k_2018_05_15_fy2018_corroboration": (
            "2018-05-15",
            "0001193125-18-164066",
            "8a84eb856a87b19696e003dfa0c7b814e35258c01d265199fdf4fbdc9beb7807",
        ),
        "10q_2018_11_05_q2fy2019": (
            "2018-11-05",
            "0001193125-18-318357",
            "3c02638efd0076903404f83c69a0390bd7c2d32a3946b366c6cc2fb07aefd9d1",
        ),
        "10k_2019_05_13_fy2019": (
            "2019-05-13",
            "0001193125-19-145332",
            "3789285de4d84ebdf6131829b99ddc951c6995763b142c23eef07db4aa1ca777",
        ),
        "10q_2019_11_04_q2fy2020": (
            "2019-11-04",
            "0001193125-19-283342",
            "53587f610d58a5401165a46350dcba8f0b290ab9ef56fcd415cb91f7a84271f9",
        ),
    }
    assert {
        source_id: (
            source["filed"],
            source["accession"],
            source["expected_sha256"],
        )
        for source_id, source in SOURCE_DOCUMENTS.items()
    } == expected
    validate_source_lock()


def test_source_lock_rejects_post_signal_source() -> None:
    sources = deepcopy(SOURCE_DOCUMENTS)
    sources["10q_2019_11_04_q2fy2020"]["filed"] = "2020-01-01"
    with pytest.raises(ValueError, match="violates PIT cutoff"):
        validate_source_lock(sources)


def test_later_filings_are_explicitly_rejected() -> None:
    assert REJECTED_LATER_FILINGS["0001193125-20-024218"]["filed"] == "2020-02-04"
    assert REJECTED_LATER_FILINGS["0001193125-20-140388"]["filed"] == "2020-05-12"


def test_build_downloads_missing_source_and_verifies_package(
    tmp_path, monkeypatch
) -> None:
    missing = "10q_2019_11_04_q2fy2020"
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
    assert report["source_operand_verification_count"] == 32
    assert report["source_text_verification_count"] == 11
    assert len(facts) == 4
    assert manifest["sources"][missing]["downloaded"] is True
    assert manifest["sources"][missing]["actual_sha256"] == (
        sources[missing]["expected_sha256"]
    )


def test_build_rejects_operand_drift(tmp_path, monkeypatch) -> None:
    source_id = "10q_2019_11_04_q2fy2020"
    _install_source_fixtures(
        tmp_path,
        monkeypatch,
        value_overrides={
            (source_id, "revenue"): (16_636, 13_091, 33_946, 28_594)
        },
    )
    with pytest.raises(RuntimeError, match="source row changed"):
        build(tmp_path)


def test_build_rejects_sha_drift_before_using_source(tmp_path, monkeypatch) -> None:
    sources, _ = _install_source_fixtures(tmp_path, monkeypatch)
    source_id = "10k_2019_05_13_fy2019"
    path = tmp_path / sources[source_id]["local_path"]
    path.write_bytes(path.read_bytes() + b"drift")
    with pytest.raises(RuntimeError, match="SHA-256 mismatch"):
        build(tmp_path)
