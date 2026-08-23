from copy import deepcopy
from dataclasses import replace
import hashlib
import json
from pathlib import Path

import pandas as pd
import pytest

import scripts.research_v14_imab_exact_ttm as imab_exact_ttm
from scripts.research_v14_imab_exact_ttm import (
    AUDIT_OBSERVATIONS,
    OPERANDS_RMB_THOUSANDS,
    REJECTED_LATER_FILINGS,
    SOURCE_DOCUMENTS,
    SOURCE_PARSE_SPECS,
    build,
    direct_ttm_facts,
    exact_ttm_evidence,
    exact_ttm_growth_evidence,
    resolve_audit_observations,
    validate_source_lock,
)
from src.financial.quarterly_fundamentals import (
    quarterly_growth_snapshot,
    quarterly_profit_ttm_snapshot,
)
from src.research import can_slim_validation


def _fixture_source_bytes(
    source_id: str,
    operand_overrides: dict[str, int] | None = None,
) -> bytes:
    overrides = operand_overrides or {}
    spec = SOURCE_PARSE_SPECS[source_id]
    operand_values = {}
    for operand_id, operand in OPERANDS_RMB_THOUSANDS.items():
        if operand["source_id"] != source_id:
            continue
        operand_values[(operand["metric"], operand["table_column"])] = (
            overrides.get(operand_id, operand["value"])
        )
    rows = [
        "<tr><td>" + " | ".join(spec["context_phrases"]) + "</td></tr>"
    ]
    for metric, labels in spec["row_labels"].items():
        cells = [f"<td>{labels[0]}</td>"]
        for column in spec["columns"]:
            value = int(operand_values.get((metric, column), 0))
            rendered = f"({abs(value):,})" if value < 0 else f"{value:,}"
            cells.append(f"<td>{rendered}</td>")
        rows.append("<tr>" + "".join(cells) + "</tr>")
    return ("<html><table>" + "".join(rows) + "</table></html>").encode()


def _install_source_fixtures(
    tmp_path,
    monkeypatch,
    *,
    missing_source: str | None = None,
    operand_overrides: dict[str, int] | None = None,
):
    sources = deepcopy(SOURCE_DOCUMENTS)
    downloads = {}
    for source_id, source in sources.items():
        raw = _fixture_source_bytes(source_id, operand_overrides)
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

    monkeypatch.setattr(imab_exact_ttm, "SOURCE_DOCUMENTS", sources)
    monkeypatch.setattr(imab_exact_ttm, "_download_source", fake_download)
    return sources, download_calls


def test_exact_ttm_math_uses_cumulative_operands_without_quarter_splits() -> None:
    evidence = {
        row["fiscal_end"]: row for row in exact_ttm_evidence()
    }

    assert evidence["2020-06-30"]["net_income_ttm"] == -1_177_466_000
    assert evidence["2020-09-30"]["net_income_ttm"] == -919_205_000
    assert evidence["2021-06-30"]["net_income_ttm"] == -22_713_000
    assert all(row["currency"] == "RMB" for row in evidence.values())
    assert all(row["accounting_standard"] == "US-GAAP" for row in evidence.values())

    facts = direct_ttm_facts(fetched_at="2026-08-23")
    assert len(facts) == 7
    assert set(facts["metric"]) == {
        "net_income_ttm",
        "net_income_growth",
        "revenue_ttm",
        "revenue_growth",
    }
    assert not set(facts["metric"]) & {"net_income", "revenue"}


def test_direct_losses_are_usable_only_after_their_filing_dates() -> None:
    facts = direct_ttm_facts(fetched_at="2026-08-23")
    facts["fiscal_end"] = pd.to_datetime(facts["fiscal_end"])
    facts["available_date"] = pd.to_datetime(facts["available_date"])

    jan = quarterly_profit_ttm_snapshot(
        facts, pd.Timestamp("2021-01-29"), 150
    )
    feb = quarterly_profit_ttm_snapshot(
        facts, pd.Timestamp("2021-02-26"), 150
    )
    may = quarterly_profit_ttm_snapshot(
        facts, pd.Timestamp("2021-05-28"), 150
    )
    sep = quarterly_profit_ttm_snapshot(
        facts, pd.Timestamp("2021-09-30"), 150
    )

    assert jan.loc["IMAB", "net_income_ttm"] == -1_177_466_000
    assert jan.loc["IMAB", "financial_age_days"] == 59
    assert feb.loc["IMAB", "net_income_ttm"] == -919_205_000
    assert feb.loc["IMAB", "financial_age_days"] == 21
    assert may.loc["IMAB", "net_income_ttm"] == 470_915_000
    assert may.loc["IMAB", "financial_age_days"] == 30
    assert sep.loc["IMAB", "net_income_ttm"] == -22_713_000
    assert sep.loc["IMAB", "financial_age_days"] == 30


def test_exact_growth_uses_two_complete_years_from_one_rmb_20f_table() -> None:
    growth = exact_ttm_growth_evidence()

    assert growth["fiscal_end"] == "2020-12-31"
    assert growth["prior_fiscal_end"] == "2019-12-31"
    assert growth["available_date"] == "2021-04-28"
    assert growth["currency"] == "RMB"
    assert growth["source_accessions"] == ["0001193125-21-135440"]
    assert growth["net_income_ttm"] == 470_915_000
    assert growth["prior_net_income_ttm"] == -1_451_950_000
    assert growth["net_income_growth"] == pytest.approx(
        1.3243327938289886
    )
    assert growth["revenue_ttm"] == 1_542_668_000
    assert growth["prior_revenue_ttm"] == 30_000_000
    assert growth["revenue_growth"] == pytest.approx(50.42226666666667)


def test_all_19_audit_observations_resolve_with_newer_loss_precedence() -> None:
    assert len(AUDIT_OBSERVATIONS) == 19
    resolved = resolve_audit_observations()

    assert len(resolved) == 19
    assert resolved["resolved"].all()
    assert resolved["decision"].value_counts().to_dict() == {
        "usable_exact_ttm_growth": 12,
        "known_nonpositive_profit": 7,
    }
    expected = {
        "2021-01-29": (1, "known_nonpositive_profit", "2020-06-30", 59),
        "2021-02-26": (2, "known_nonpositive_profit", "2020-09-30", 21),
        "2021-05-28": (4, "usable_exact_ttm_growth", "2020-12-31", 30),
        "2021-06-30": (4, "usable_exact_ttm_growth", "2020-12-31", 63),
        "2021-07-30": (4, "usable_exact_ttm_growth", "2020-12-31", 93),
        "2021-09-30": (4, "known_nonpositive_profit", "2021-06-30", 30),
    }
    for signal_date, (
        count,
        decision,
        fiscal_end,
        financial_age_days,
    ) in expected.items():
        rows = resolved.loc[resolved["signal_date"].eq(signal_date)]
        assert len(rows) == count
        assert set(rows["decision"]) == {decision}
        assert set(rows["fiscal_end"]) == {fiscal_end}
        assert set(rows["financial_age_days"]) == {financial_age_days}

    september = resolved.loc[resolved["signal_date"].eq("2021-09-30")]
    assert set(september["net_income_ttm"]) == {-22_713_000}
    assert september["net_income_growth"].isna().all()

    facts = direct_ttm_facts(fetched_at="2026-08-23")
    facts["fiscal_end"] = pd.to_datetime(facts["fiscal_end"])
    facts["available_date"] = pd.to_datetime(facts["available_date"])
    for scenario, signal_date, maximum_age_days in AUDIT_OBSERVATIONS:
        expected_row = resolved.loc[
            resolved["scenario"].eq(scenario)
            & resolved["signal_date"].eq(signal_date)
        ].iloc[0]
        growth = quarterly_growth_snapshot(
            facts, pd.Timestamp(signal_date), maximum_age_days
        )
        profit = quarterly_profit_ttm_snapshot(
            facts, pd.Timestamp(signal_date), maximum_age_days
        )
        if expected_row["decision"] == "usable_exact_ttm_growth":
            assert growth.loc["IMAB", "net_income_growth"] == pytest.approx(
                1.3243327938289886
            )
            assert growth.loc["IMAB", "revenue_growth"] == pytest.approx(
                50.42226666666667
            )
            assert profit.loc["IMAB", "net_income_ttm"] == 470_915_000
        else:
            assert growth.empty
            assert profit.loc["IMAB", "net_income_ttm"] < 0


def test_incomplete_direct_growth_bundle_is_rejected() -> None:
    facts = direct_ttm_facts(fetched_at="2026-08-23")
    facts = facts.loc[~(
        facts["fiscal_end"].eq("2020-12-31")
        & facts["metric"].eq("revenue_growth")
    )].copy()
    facts["fiscal_end"] = pd.to_datetime(facts["fiscal_end"])
    facts["available_date"] = pd.to_datetime(facts["available_date"])

    growth = quarterly_growth_snapshot(
        facts, pd.Timestamp("2021-05-28"), 150
    )
    profit = quarterly_profit_ttm_snapshot(
        facts, pd.Timestamp("2021-05-28"), 150
    )

    assert growth.empty
    assert profit.loc["IMAB", "net_income_ttm"] == 470_915_000


def test_real_coverage_classifies_all_19_observations(
    monkeypatch,
) -> None:
    facts = direct_ttm_facts(fetched_at="2026-08-23")
    facts["fiscal_end"] = pd.to_datetime(facts["fiscal_end"])
    facts["available_date"] = pd.to_datetime(facts["available_date"])
    resolved = resolve_audit_observations()
    monkeypatch.setattr(
        can_slim_validation,
        "scheduled_signal_dates",
        lambda _index, start, *_args: [pd.Timestamp(start)],
    )
    monkeypatch.setattr(
        can_slim_validation,
        "market_regime_is_on",
        lambda *_args, **_kwargs: True,
    )
    monkeypatch.setattr(
        can_slim_validation,
        "build_can_slim_technical_cross_section",
        lambda *_args, **_kwargs: pd.DataFrame(
            {"nonfinancial_candidate": [True]}, index=["IMAB"]
        ),
    )
    monkeypatch.setattr(
        can_slim_validation,
        "can_slim_nonfinancial_candidate_mask",
        lambda technical, _config: pd.Series(True, index=technical.index),
    )

    for scenario, signal_date, maximum_age_days in AUDIT_OBSERVATIONS:
        as_of = pd.Timestamp(signal_date)
        close = pd.DataFrame({"IMAB": [20.0]}, index=[as_of])
        dollar_volume = pd.DataFrame(
            {"IMAB": [20_000_000.0]}, index=[as_of]
        )
        nasdaq = pd.Series([100.0], index=[as_of])
        config = replace(
            can_slim_validation.fixed_top3_config(),
            end=signal_date,
            maximum_financial_age_days=maximum_age_days,
        )
        report = can_slim_validation.technical_candidate_financial_coverage(
            close,
            dollar_volume,
            nasdaq,
            facts,
            {as_of: {"IMAB"}},
            config,
            start=signal_date,
            adjusted_close=close,
        )
        expected = resolved.loc[
            resolved["scenario"].eq(scenario)
            & resolved["signal_date"].eq(signal_date)
        ].iloc[0]
        row = report["by_signal"][0]
        assert report["missing_financial_observations"] == 0
        assert report["complete"] is True
        if expected["decision"] == "usable_exact_ttm_growth":
            assert row["usable_financial_count"] == 1
            assert row["known_nonpositive_profit_count"] == 0
        else:
            assert row["usable_financial_count"] == 0
            assert row["known_nonpositive_profit_count"] == 1


def test_source_lock_rejects_the_2022_20f_and_mixed_currency() -> None:
    validate_source_lock()
    later_accession = next(iter(REJECTED_LATER_FILINGS))
    assert later_accession == "0001193125-22-133550"

    later = {name: dict(source) for name, source in SOURCE_DOCUMENTS.items()}
    later["later_20f"] = {
        **next(iter(SOURCE_DOCUMENTS.values())),
        "accession": later_accession,
        "filed": "2022-04-29",
    }
    with pytest.raises(ValueError, match="later filing is forbidden"):
        validate_source_lock(later)

    mixed = {name: dict(source) for name, source in SOURCE_DOCUMENTS.items()}
    mixed["20f_2021_04_28"]["currency"] = "USD"
    with pytest.raises(ValueError, match="mixed currency or scale"):
        validate_source_lock(mixed)


def test_official_source_paths_and_hashes_are_locked() -> None:
    expected = {
        "f1_2020_12_01": (
            "sources/d98473df1.htm",
            "3c26c1b35c37bc15688234b154d26997e0a77c95d1b664f5c48badc8d87c32fe",
        ),
        "6k_2021_02_05_ex991": (
            "sources/d123013dex991.htm",
            "2f1bbf588618a814923c21c59c867828f38c9e6ef01ede98996562a35acfeef2",
        ),
        "6k_2021_02_05_ex992": (
            "sources/d123013dex992.htm",
            "b1245a105808c99b06eeff98ac99aa81fe41be12af21e29e787c9594b83d0729",
        ),
        "20f_2021_04_28": (
            "sources/d10811d20f.htm",
            "e4485327a9d8a1e4225ea092bb790b71c054cefd5d49fc1fb888e8bc13be8a9d",
        ),
        "6k_2021_08_31_ex991": (
            "sources/d212638dex991.htm",
            "77526e3223c0e03069da4814677fc5055edd782a627c04df3822c543adc830dc",
        ),
    }

    assert {
        source_id: (source["local_path"], source["expected_sha256"])
        for source_id, source in SOURCE_DOCUMENTS.items()
    } == expected


def test_build_downloads_missing_source_and_records_verified_provenance(
    tmp_path,
    monkeypatch,
) -> None:
    sources, download_calls = _install_source_fixtures(
        tmp_path,
        monkeypatch,
        missing_source="6k_2021_08_31_ex991",
    )
    report = build(tmp_path)
    manifest = json.loads((tmp_path / "manifest.json").read_text())
    resolutions = json.loads(
        (tmp_path / "audit_observation_resolution.json").read_text()
    )

    assert report["resolved_audit_observation_count"] == 19
    assert report["accepted_exact_ttm_fact_count"] == 7
    assert report["accepted_exact_ttm_loss_count"] == 3
    assert report["accepted_exact_annual_positive_state_count"] == 1
    assert report["accepted_direct_growth_metric_count"] == 4
    assert download_calls == [sources["6k_2021_08_31_ex991"]["url"]]
    assert len(manifest["operand_verification"]) == len(
        OPERANDS_RMB_THOUSANDS
    )
    assert all(
        item["parsed_value"] == item["expected_value"]
        for item in manifest["operand_verification"]
    )
    for source in manifest["source_documents"].values():
        assert source["actual_sha256"] == source["expected_sha256"]
        assert Path(source["local_path"]).exists()
    assert manifest["research_only"] is True
    assert manifest["release_status"] == "BLOCKED"
    assert manifest["promotion_eligible"] is False
    assert manifest["formal_financials_modified"] is False
    assert manifest["shared_audit_compatibility"]["exact_ttm_growth"] == (
        "compatible_with_quarterly_growth_snapshot"
    )
    assert len(resolutions) == 19
    assert "0001193125-22-133550" not in json.dumps(resolutions)


def test_build_rejects_existing_source_with_wrong_sha(
    tmp_path,
    monkeypatch,
) -> None:
    sources, download_calls = _install_source_fixtures(tmp_path, monkeypatch)
    corrupted = tmp_path / sources["20f_2021_04_28"]["local_path"]
    corrupted.write_bytes(corrupted.read_bytes() + b"tampered")

    with pytest.raises(RuntimeError, match="source SHA-256 mismatch"):
        build(tmp_path)

    assert download_calls == []


def test_build_rejects_hash_valid_html_when_operand_value_changes(
    tmp_path,
    monkeypatch,
) -> None:
    _install_source_fixtures(
        tmp_path,
        monkeypatch,
        operand_overrides={"6k_h1_2021_net_income": -1_000_000},
    )

    with pytest.raises(
        RuntimeError, match="operand 6k_h1_2021_net_income source changed"
    ):
        build(tmp_path)
