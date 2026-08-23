from copy import deepcopy
import hashlib
import json

import pandas as pd
import pytest

import scripts.research_v14_gilt_exact_ttm_loss as gilt_loss
from scripts.research_v14_gilt_exact_ttm_loss import (
    AUDIT_OBSERVATIONS,
    OPERANDS_USD_THOUSANDS,
    OUTPUT_COLUMNS,
    REJECTED_LATER_FILINGS,
    SOURCE_DOCUMENTS,
    SOURCE_ROW_CHECKS,
    SOURCE_TEXT_CHECKS,
    build,
    direct_ttm_facts,
    exact_ttm_evidence,
    resolve_audit_observations,
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
            (source_id, check["metric"]), tuple(check["expected_values"])
        )
        cells = [f"<td>{check['line_item']}</td>", "<td></td>"]
        for value in values:
            rendered = f"({abs(value):,})" if value < 0 else f"{value:,}"
            cells.extend(("<td>$</td>", f"<td>{rendered}</td>", "<td></td>"))
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

    monkeypatch.setattr(gilt_loss, "SOURCE_DOCUMENTS", sources)
    monkeypatch.setattr(gilt_loss, "_download_source", fake_download)
    return sources, download_calls


def test_exact_ttm_uses_same_basis_reported_cumulative_periods() -> None:
    evidence = exact_ttm_evidence()
    assert evidence["net_income_ttm"] == -3_510_000
    assert evidence["formula"] == "FY2019 - 9M_2019 + 9M_2020"
    assert evidence["currency"] == "USD"
    assert evidence["accounting_standard"] == "US-GAAP"
    assert evidence["profit_scope"] == "consolidated Net income (loss)"
    assert OPERANDS_USD_THOUSANDS["fy2019_net_income"]["value"] == 36_538
    assert OPERANDS_USD_THOUSANDS["m9_2019_net_income"]["value"] == 12_517
    assert OPERANDS_USD_THOUSANDS["m9_2020_net_loss"]["value"] == -27_531


def test_fact_output_is_negative_ttm_only() -> None:
    facts = direct_ttm_facts()
    assert list(facts.columns) == OUTPUT_COLUMNS
    assert len(facts) == 1
    assert facts.iloc[0]["metric"] == "net_income_ttm"
    assert facts.iloc[0]["value"] == -3_510_000
    assert not set(facts["metric"]) & {
        "net_income",
        "revenue",
        "net_income_growth",
        "revenue_growth",
    }


def test_all_three_observations_resolve_as_known_nonpositive() -> None:
    assert AUDIT_OBSERVATIONS == (
        ("liq2000000-age150-growth", "2021-01-29", 150),
        ("liq2000000-age365-growth", "2021-01-29", 365),
        ("liq2000000-age550-growth", "2021-01-29", 550),
    )
    resolution = resolve_audit_observations()
    assert resolution["resolved"].all()
    assert set(resolution["decision"]) == {"known_nonpositive_profit"}
    assert set(resolution["financial_age_days"]) == {80}
    assert set(resolution["net_income_ttm"]) == {-3_510_000}


def test_direct_loss_is_pit_limited_and_does_not_create_growth() -> None:
    facts = direct_ttm_facts().copy()
    facts["fiscal_end"] = pd.to_datetime(facts["fiscal_end"])
    facts["available_date"] = pd.to_datetime(facts["available_date"])
    before = quarterly_profit_ttm_snapshot(
        facts, pd.Timestamp("2020-11-09"), 550
    )
    at_filing = quarterly_profit_ttm_snapshot(
        facts, pd.Timestamp("2020-11-10"), 150
    )
    at_signal = quarterly_profit_ttm_snapshot(
        facts, pd.Timestamp("2021-01-29"), 150
    )
    growth = quarterly_growth_snapshot(
        facts, pd.Timestamp("2021-01-29"), 550
    )
    assert before.empty
    assert at_filing.loc["GILT", "net_income_ttm"] == -3_510_000
    assert at_signal.loc["GILT", "financial_age_days"] == 80
    assert growth.empty


def test_original_2019_interim_matches_2020_comparative() -> None:
    original = SOURCE_ROW_CHECKS["6k_2019_11_19_q3_original"]
    later = SOURCE_ROW_CHECKS["6k_2020_11_10_q3"]
    assert original[0]["expected_values"][0] == later[0]["expected_values"][1]
    assert original[1]["expected_values"][0] == later[1]["expected_values"][1]
    assert "exactly match" in exact_ttm_evidence()["restatement_isolation"]


def test_official_source_dates_accessions_and_hashes_are_locked() -> None:
    expected = {
        "6k_2019_11_19_q3_original": (
            "2019-11-19",
            "0001178913-19-002787",
            "1e4118d527d40ed0b6d2c3a30823aa5105d91f38f64b2cda8436e1baf4d998dc",
        ),
        "20f_2020_03_23_fy2019": (
            "2020-03-23",
            "0001178913-20-000895",
            "6cc2ef1426a5c736c019cfa7735f791b22c7958e3650cd095485df85d47f770c",
        ),
        "6k_2020_11_10_q3": (
            "2020-11-10",
            "0001178913-20-003069",
            "caa3f865da85864982ae9c9fd0d3ce4f173f86dd4ea384e4ce1dbb856f49871f",
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
    sources["6k_2020_11_10_q3"]["filed"] = "2021-02-01"
    with pytest.raises(ValueError, match="violates PIT cutoff"):
        validate_source_lock(sources)


def test_later_full_year_results_are_explicitly_rejected() -> None:
    assert REJECTED_LATER_FILINGS["0001178913-21-000586"]["filed"] == "2021-02-16"
    assert REJECTED_LATER_FILINGS["0001178913-21-000937"]["filed"] == "2021-03-08"


def test_build_downloads_missing_source_and_verifies_loss(
    tmp_path, monkeypatch
) -> None:
    missing = "6k_2020_11_10_q3"
    sources, download_calls = _install_source_fixtures(
        tmp_path, monkeypatch, missing_source=missing
    )
    report = build(tmp_path)
    manifest = json.loads((tmp_path / "manifest.json").read_text())
    facts = pd.read_csv(tmp_path / "strict_quarterly_facts.csv")
    assert download_calls == [sources[missing]["url"]]
    assert report["accepted_exact_ttm_loss_count"] == 1
    assert report["accepted_fact_count"] == 1
    assert report["resolved_audit_observation_count"] == 3
    assert report["source_operand_verification_count"] == 22
    assert report["source_text_verification_count"] == 11
    assert len(facts) == 1
    assert manifest["sources"][missing]["downloaded"] is True
    assert manifest["sources"][missing]["actual_sha256"] == (
        sources[missing]["expected_sha256"]
    )


def test_build_rejects_operand_drift(tmp_path, monkeypatch) -> None:
    source_id = "6k_2020_11_10_q3"
    _install_source_fixtures(
        tmp_path,
        monkeypatch,
        value_overrides={
            (source_id, "net_income"): (-27_530, 12_517, -11_551, 6_288)
        },
    )
    with pytest.raises(RuntimeError, match="source row changed"):
        build(tmp_path)


def test_build_rejects_sha_drift_before_using_source(tmp_path, monkeypatch) -> None:
    sources, _ = _install_source_fixtures(tmp_path, monkeypatch)
    source_id = "20f_2020_03_23_fy2019"
    path = tmp_path / sources[source_id]["local_path"]
    path.write_bytes(path.read_bytes() + b"drift")
    with pytest.raises(RuntimeError, match="SHA-256 mismatch"):
        build(tmp_path)
