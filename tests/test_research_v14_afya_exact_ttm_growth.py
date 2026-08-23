from copy import deepcopy
from dataclasses import replace
import hashlib
import json

import pandas as pd
import pytest

import scripts.research_v14_afya_exact_ttm_growth as afya_growth
from scripts.research_v14_afya_exact_ttm_growth import (
    AUDIT_OBSERVATIONS,
    REJECTED_LATER_FILINGS,
    SOURCE_DOCUMENTS,
    SOURCE_PARSE_SPECS,
    SOURCE_VALUES_BRL_THOUSANDS,
    build,
    direct_growth_facts,
    exact_ttm_growth_evidence,
    resolve_audit_observations,
    validate_comparative_consistency,
    validate_source_lock,
)
from src.financial.quarterly_fundamentals import (
    quarterly_growth_snapshot,
    quarterly_profit_ttm_snapshot,
)
from src.research import can_slim_validation


def _fixture_source_bytes(
    source_id: str,
    value_overrides: dict[str, int] | None = None,
) -> bytes:
    overrides = value_overrides or {}
    spec = SOURCE_PARSE_SPECS[source_id]
    values_by_metric_column = {
        (value["metric"], value["table_column"]): overrides.get(
            value_id, value["value"]
        )
        for value_id, value in SOURCE_VALUES_BRL_THOUSANDS.items()
        if value["source_id"] == source_id
    }
    rows = [
        "<tr><td>" + " | ".join(spec["context_phrases"]) + "</td></tr>"
    ]
    for metric, label in spec["row_labels"].items():
        cells = [f"<td>{label}</td>"]
        for column in spec["columns"]:
            value = int(values_by_metric_column.get((metric, column), 0))
            rendered = f"({abs(value):,})" if value < 0 else f"{value:,}"
            cells.append(f"<td>{rendered}</td>")
        rows.append("<tr>" + "".join(cells) + "</tr>")
    document_context = " | ".join(spec.get("document_context_phrases", ()))
    return (
        "<html><p>" + document_context + "</p><table>"
        + "".join(rows) + "</table></html>"
    ).encode()


def _install_source_fixtures(
    tmp_path,
    monkeypatch,
    *,
    missing_source: str | None = None,
    value_overrides: dict[str, int] | None = None,
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

    monkeypatch.setattr(afya_growth, "SOURCE_DOCUMENTS", sources)
    monkeypatch.setattr(afya_growth, "_download_source", fake_download)
    return sources, download_calls


def test_exact_h1_and_annual_growth_math_is_complete_and_same_currency() -> None:
    evidence = {row["fiscal_end"]: row for row in exact_ttm_growth_evidence()}
    h1 = evidence["2020-06-30"]
    annual = evidence["2020-12-31"]

    assert h1["net_income_ttm"] == 269_516_000
    assert h1["prior_net_income_ttm"] == 124_088_000
    assert h1["net_income_growth"] == pytest.approx(1.171974727612662)
    assert h1["revenue_ttm"] == 974_074_000
    assert h1["prior_revenue_ttm"] == 520_451_000
    assert h1["revenue_growth"] == pytest.approx(0.8715959811778631)
    assert annual["net_income_ttm"] == 307_987_000
    assert annual["net_income_growth"] == pytest.approx(0.7827242101851102)
    assert annual["revenue_ttm"] == 1_201_191_000
    assert annual["revenue_growth"] == pytest.approx(0.6002437952120219)
    assert all(row["currency"] == "BRL" for row in evidence.values())

    facts = direct_growth_facts(fetched_at="2026-08-23")
    assert len(facts) == 8
    assert set(facts["metric"]) == {
        "net_income_ttm",
        "net_income_growth",
        "revenue_ttm",
        "revenue_growth",
    }
    assert not set(facts["metric"]) & {"net_income", "revenue"}


def test_all_three_dates_and_nine_scenario_observations_resolve() -> None:
    assert len(AUDIT_OBSERVATIONS) == 9
    assert {date for _, date, _ in AUDIT_OBSERVATIONS} == {
        "2020-09-30",
        "2020-11-30",
        "2021-06-30",
    }
    resolved = resolve_audit_observations()
    assert resolved["resolved"].all()
    assert set(resolved["decision"]) == {"usable_exact_ttm_growth"}
    expected = {
        "2020-09-30": (3, "2020-06-30", "2020-08-27", 34),
        "2020-11-30": (3, "2020-06-30", "2020-08-27", 95),
        "2021-06-30": (3, "2020-12-31", "2021-04-30", 61),
    }
    for date, (count, fiscal_end, available, age) in expected.items():
        rows = resolved.loc[resolved["signal_date"].eq(date)]
        assert len(rows) == count
        assert set(rows["fiscal_end"]) == {fiscal_end}
        assert set(rows["available_date"]) == {available}
        assert set(rows["financial_age_days"]) == {age}


def test_growth_bundle_is_pit_limited_and_latest_annual_wins() -> None:
    facts = direct_growth_facts(fetched_at="2026-08-23")
    facts["fiscal_end"] = pd.to_datetime(facts["fiscal_end"])
    facts["available_date"] = pd.to_datetime(facts["available_date"])

    before_h1 = quarterly_growth_snapshot(
        facts, pd.Timestamp("2020-08-26"), 550
    )
    at_h1 = quarterly_growth_snapshot(
        facts, pd.Timestamp("2020-08-27"), 150
    )
    before_annual = quarterly_growth_snapshot(
        facts, pd.Timestamp("2021-04-29"), 550
    )
    at_annual = quarterly_growth_snapshot(
        facts, pd.Timestamp("2021-04-30"), 150
    )
    profit = quarterly_profit_ttm_snapshot(
        facts, pd.Timestamp("2021-06-30"), 150
    )

    assert before_h1.empty
    assert at_h1.loc["AFYA", "net_income_ttm"] == 269_516_000
    assert before_annual.loc["AFYA", "fiscal_end"] == pd.Timestamp("2020-06-30")
    assert at_annual.loc["AFYA", "net_income_ttm"] == 307_987_000
    assert profit.loc["AFYA", "net_income_ttm"] == 307_987_000


def test_incomplete_direct_growth_bundle_is_rejected() -> None:
    facts = direct_growth_facts(fetched_at="2026-08-23")
    facts = facts.loc[~(
        facts["fiscal_end"].eq("2020-12-31")
        & facts["metric"].eq("revenue_growth")
    )].copy()
    facts["fiscal_end"] = pd.to_datetime(facts["fiscal_end"])
    facts["available_date"] = pd.to_datetime(facts["available_date"])

    growth = quarterly_growth_snapshot(
        facts, pd.Timestamp("2021-06-30"), 150
    )
    profit = quarterly_profit_ttm_snapshot(
        facts, pd.Timestamp("2021-06-30"), 150
    )
    assert growth.empty
    assert profit.loc["AFYA", "net_income_ttm"] == 307_987_000


def test_real_coverage_classifies_all_nine_observations(monkeypatch) -> None:
    facts = direct_growth_facts(fetched_at="2026-08-23")
    facts["fiscal_end"] = pd.to_datetime(facts["fiscal_end"])
    facts["available_date"] = pd.to_datetime(facts["available_date"])
    monkeypatch.setattr(
        can_slim_validation,
        "scheduled_signal_dates",
        lambda _index, start, *_args: [pd.Timestamp(start)],
    )
    monkeypatch.setattr(
        can_slim_validation, "market_regime_is_on", lambda *_a, **_k: True
    )
    monkeypatch.setattr(
        can_slim_validation,
        "build_can_slim_technical_cross_section",
        lambda *_a, **_k: pd.DataFrame(
            {"nonfinancial_candidate": [True]}, index=["AFYA"]
        ),
    )
    monkeypatch.setattr(
        can_slim_validation,
        "can_slim_nonfinancial_candidate_mask",
        lambda technical, _config: pd.Series(True, index=technical.index),
    )

    for _scenario, signal_date, maximum_age_days in AUDIT_OBSERVATIONS:
        as_of = pd.Timestamp(signal_date)
        close = pd.DataFrame({"AFYA": [20.0]}, index=[as_of])
        config = replace(
            can_slim_validation.fixed_top3_config(),
            end=signal_date,
            maximum_financial_age_days=maximum_age_days,
        )
        report = can_slim_validation.technical_candidate_financial_coverage(
            close,
            pd.DataFrame({"AFYA": [20_000_000.0]}, index=[as_of]),
            pd.Series([100.0], index=[as_of]),
            facts,
            {as_of: {"AFYA"}},
            config,
            start=signal_date,
            adjusted_close=close,
        )
        row = report["by_signal"][0]
        assert report["missing_financial_observations"] == 0
        assert report["complete"] is True
        assert row["usable_financial_count"] == 1
        assert row["known_nonpositive_profit_count"] == 0


def test_comparatives_are_unchanged_and_later_filing_is_rejected() -> None:
    validate_comparative_consistency()
    assert REJECTED_LATER_FILINGS["0001292814-21-003519"]["filed"] == (
        "2021-08-26"
    )
    later = {name: dict(source) for name, source in SOURCE_DOCUMENTS.items()}
    later["later_q2"] = {
        **next(iter(SOURCE_DOCUMENTS.values())),
        "accession": "0001292814-21-003519",
        "filed": "2021-08-26",
    }
    with pytest.raises(ValueError, match="later filing is forbidden"):
        validate_source_lock(later)


def test_source_lock_rejects_mixed_currency() -> None:
    mixed = {name: dict(source) for name, source in SOURCE_DOCUMENTS.items()}
    mixed["6k_2020_08_27_h1_ex991"]["currency"] = "USD"
    with pytest.raises(ValueError, match="mixed currency or scale"):
        validate_source_lock(mixed)


def test_official_source_paths_and_hashes_are_locked() -> None:
    expected = {
        "6k_2019_08_29_h1_ex991": (
            "sources/dp111957_ex9901.htm",
            "3510df2c517766407d8fda1b6fbfe852aaaeced4bb8ea22a7cd315c9ed650a66",
        ),
        "20f_2020_04_20_fy2019": (
            "sources/afyaform20f_2019.htm",
            "9885ef40f70f5b50ef5e5711439b8b8a1efc50e48f0af1c4c78fe5269e31e144",
        ),
        "6k_2020_08_27_h1_ex991": (
            "sources/ex99-1_h1_2020.htm",
            "5c5cedf907d1e179f3a603dd35b879adeaf38943ed67d10f6641e67fbd9f960f",
        ),
        "20f_2021_04_30_fy2020": (
            "sources/afyaform20f_2020.htm",
            "cb1e16d272c0ac6cdb476dfbe6c56a2a3f30f2b410f25ab73d4bcbcc429555d4",
        ),
    }
    assert {
        source_id: (source["local_path"], source["expected_sha256"])
        for source_id, source in SOURCE_DOCUMENTS.items()
    } == expected


def test_build_downloads_missing_source_and_verifies_all_operands(
    tmp_path,
    monkeypatch,
) -> None:
    sources, download_calls = _install_source_fixtures(
        tmp_path,
        monkeypatch,
        missing_source="20f_2021_04_30_fy2020",
    )
    report = build(tmp_path)
    manifest = json.loads((tmp_path / "manifest.json").read_text())

    assert report["accepted_direct_growth_fact_count"] == 8
    assert report["accepted_exact_growth_bundle_count"] == 2
    assert report["resolved_unique_signal_date_count"] == 3
    assert report["resolved_audit_observation_count"] == 9
    assert download_calls == [sources["20f_2021_04_30_fy2020"]["url"]]
    assert len(manifest["source_value_verification"]) == 16
    assert all(
        item["parsed_value"] == item["expected_value"]
        for item in manifest["source_value_verification"]
    )
    assert manifest["research_only"] is True
    assert manifest["formal_financials_modified"] is False
    assert manifest["shared_candidate_integrated"] is False
    assert manifest["comparative_restatement_check"][
        "later_values_backfilled"
    ] is False


def test_build_rejects_existing_source_with_wrong_sha(tmp_path, monkeypatch) -> None:
    sources, download_calls = _install_source_fixtures(tmp_path, monkeypatch)
    corrupted = tmp_path / sources["6k_2020_08_27_h1_ex991"]["local_path"]
    corrupted.write_bytes(corrupted.read_bytes() + b"tampered")
    with pytest.raises(RuntimeError, match="source SHA-256 mismatch"):
        build(tmp_path)
    assert download_calls == []


def test_build_rejects_hash_valid_html_when_operand_changes(
    tmp_path,
    monkeypatch,
) -> None:
    _install_source_fixtures(
        tmp_path,
        monkeypatch,
        value_overrides={"h1_2020_net_income": 160_000},
    )
    with pytest.raises(
        RuntimeError, match="source value h1_2020_net_income changed"
    ):
        build(tmp_path)
