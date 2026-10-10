from copy import deepcopy
from dataclasses import replace
import hashlib
import json

import pandas as pd
import pytest

import scripts.research_v14_mtls_exact_ttm_loss as mtls_ttm
from scripts.research_v14_mtls_exact_ttm_loss import (
    AUDIT_OBSERVATIONS,
    NON_FINANCIAL_AMENDMENT,
    OPERANDS_EUR_THOUSANDS,
    REJECTED_LATER_RESTATEMENTS,
    REVENUE_DISCLOSURES_EUR_THOUSANDS,
    SOURCE_DOCUMENTS,
    SOURCE_PARSE_SPECS,
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
from src.research import can_slim_validation


def _fixture_source_bytes(
    source_id: str,
    value_overrides: dict[str, int] | None = None,
) -> bytes:
    overrides = value_overrides or {}
    spec = SOURCE_PARSE_SPECS[source_id]
    values_by_metric_column = {}
    for item_id, item in {
        **OPERANDS_EUR_THOUSANDS,
        **REVENUE_DISCLOSURES_EUR_THOUSANDS,
    }.items():
        if item["source_id"] == source_id:
            values_by_metric_column[(item["metric"], item["table_column"])] = (
                overrides.get(item_id, item["value"])
            )
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
    return ("<html><table>" + "".join(rows) + "</table></html>").encode()


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

    monkeypatch.setattr(mtls_ttm, "SOURCE_DOCUMENTS", sources)
    monkeypatch.setattr(mtls_ttm, "_download_source", fake_download)
    return sources, download_calls


def test_exact_ttm_uses_original_ifrs_cumulative_values_only() -> None:
    evidence = {row["fiscal_end"]: row for row in exact_ttm_evidence()}

    assert evidence["2020-06-30"]["net_income_ttm"] == -2_460_000
    assert evidence["2020-09-30"]["net_income_ttm"] == -3_825_000
    assert all(row["currency"] == "EUR" for row in evidence.values())
    assert all(row["source_scale"] == 1_000 for row in evidence.values())
    assert all(row["accounting_standard"] == "IFRS-IASB" for row in evidence.values())

    facts = direct_ttm_facts(fetched_at="2026-08-23")
    assert len(facts) == 2
    assert set(facts["metric"]) == {"net_income_ttm"}
    assert not set(facts["metric"]) & {
        "net_income",
        "revenue",
        "net_income_growth",
        "revenue_growth",
    }


def test_all_three_dates_and_twelve_scenario_observations_resolve() -> None:
    assert len(AUDIT_OBSERVATIONS) == 12
    assert {date for _, date, _ in AUDIT_OBSERVATIONS} == {
        "2020-08-31",
        "2020-12-31",
        "2021-01-29",
    }
    resolved = resolve_audit_observations()

    assert resolved["resolved"].all()
    assert set(resolved["decision"]) == {"known_nonpositive_profit"}
    expected = {
        "2020-08-31": (3, "2020-06-30", "2020-07-30", 32, -2_460_000),
        "2020-12-31": (3, "2020-09-30", "2020-10-29", 63, -3_825_000),
        "2021-01-29": (6, "2020-09-30", "2020-10-29", 92, -3_825_000),
    }
    for date, (count, fiscal_end, available, age, value) in expected.items():
        rows = resolved.loc[resolved["signal_date"].eq(date)]
        assert len(rows) == count
        assert set(rows["fiscal_end"]) == {fiscal_end}
        assert set(rows["available_date"]) == {available}
        assert set(rows["financial_age_days"]) == {age}
        assert set(rows["net_income_ttm"]) == {value}


def test_direct_losses_are_pit_limited_and_cannot_create_growth() -> None:
    facts = direct_ttm_facts(fetched_at="2026-08-23")
    facts["fiscal_end"] = pd.to_datetime(facts["fiscal_end"])
    facts["available_date"] = pd.to_datetime(facts["available_date"])

    before_h1 = quarterly_profit_ttm_snapshot(
        facts, pd.Timestamp("2020-07-29"), 550
    )
    at_h1 = quarterly_profit_ttm_snapshot(
        facts, pd.Timestamp("2020-07-30"), 150
    )
    before_9m = quarterly_profit_ttm_snapshot(
        facts, pd.Timestamp("2020-10-28"), 550
    )
    at_9m = quarterly_profit_ttm_snapshot(
        facts, pd.Timestamp("2020-10-29"), 150
    )
    growth = quarterly_growth_snapshot(
        facts, pd.Timestamp("2021-01-29"), 550
    )

    assert before_h1.empty
    assert at_h1.loc["MTLS", "net_income_ttm"] == -2_460_000
    assert before_9m.loc["MTLS", "net_income_ttm"] == -2_460_000
    assert at_9m.loc["MTLS", "net_income_ttm"] == -3_825_000
    assert growth.empty


def test_real_coverage_classifies_all_twelve_observations(monkeypatch) -> None:
    facts = direct_ttm_facts(fetched_at="2026-08-23")
    facts["fiscal_end"] = pd.to_datetime(facts["fiscal_end"])
    facts["available_date"] = pd.to_datetime(facts["available_date"])
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
            {"nonfinancial_candidate": [True]}, index=["MTLS"]
        ),
    )
    monkeypatch.setattr(
        can_slim_validation,
        "can_slim_nonfinancial_candidate_mask",
        lambda technical, _config: pd.Series(True, index=technical.index),
    )

    for _scenario, signal_date, maximum_age_days in AUDIT_OBSERVATIONS:
        as_of = pd.Timestamp(signal_date)
        close = pd.DataFrame({"MTLS": [20.0]}, index=[as_of])
        config = replace(
            can_slim_validation.fixed_top3_config(),
            end=signal_date,
            maximum_financial_age_days=maximum_age_days,
        )
        report = can_slim_validation.technical_candidate_financial_coverage(
            close,
            pd.DataFrame({"MTLS": [20_000_000.0]}, index=[as_of]),
            pd.Series([100.0], index=[as_of]),
            facts,
            {as_of: {"MTLS"}},
            config,
            start=signal_date,
            adjusted_close=close,
        )
        row = report["by_signal"][0]
        assert report["missing_financial_observations"] == 0
        assert report["complete"] is True
        assert row["usable_financial_count"] == 0
        assert row["known_nonpositive_profit_count"] == 1


def test_later_fy2019_restatement_is_explicitly_rejected() -> None:
    validate_source_lock()
    assert {
        item["restated_fy2019_net_profit_eur_thousands"]
        for item in REJECTED_LATER_RESTATEMENTS.values()
    } == {1_644}
    assert {
        item["original_fy2019_net_profit_eur_thousands"]
        for item in REJECTED_LATER_RESTATEMENTS.values()
    } == {1_724}

    later = {name: dict(source) for name, source in SOURCE_DOCUMENTS.items()}
    later["later_restatement"] = {
        **next(iter(SOURCE_DOCUMENTS.values())),
        "accession": "0001193125-21-074859",
        "filed": "2021-03-09",
    }
    with pytest.raises(ValueError, match="later restatement is forbidden"):
        validate_source_lock(later)

    assert NON_FINANCIAL_AMENDMENT["accession"] == "0001193125-20-290140"
    assert NON_FINANCIAL_AMENDMENT["effect"] == (
        "NO_FINANCIAL_UPDATE_EXHIBIT_3_1_ONLY"
    )


def test_source_lock_rejects_mixed_currency() -> None:
    mixed = {name: dict(source) for name, source in SOURCE_DOCUMENTS.items()}
    mixed["6k_2020_07_30_h1"]["currency"] = "USD"
    with pytest.raises(ValueError, match="mixed currency or scale"):
        validate_source_lock(mixed)


def test_official_source_paths_and_hashes_are_locked() -> None:
    expected = {
        "20f_2020_04_30_fy2019": (
            "sources/d873114d20f.htm",
            "e046562973b151059499b9e59778ce73de40045bc36aa9ab9e078f2d5135bb40",
        ),
        "6k_2020_07_30_h1": (
            "sources/d39097d6k.htm",
            "e37b3aed07449c1a132ec265e0b45de728ecf21c38ce9b5730dd2a4c2efdbdd2",
        ),
        "6k_2020_10_29_9m": (
            "sources/d71400d6k.htm",
            "a2ce6c23b737c7be1e5c6a98beccee67e4d6e2055dafd61fec4c79f0736d2468",
        ),
    }
    assert {
        source_id: (source["local_path"], source["expected_sha256"])
        for source_id, source in SOURCE_DOCUMENTS.items()
    } == expected


def test_build_downloads_missing_source_and_verifies_profit_and_revenue(
    tmp_path,
    monkeypatch,
) -> None:
    sources, download_calls = _install_source_fixtures(
        tmp_path,
        monkeypatch,
        missing_source="6k_2020_10_29_9m",
    )
    report = build(tmp_path)
    manifest = json.loads((tmp_path / "manifest.json").read_text())
    resolutions = json.loads(
        (tmp_path / "audit_observation_resolution.json").read_text()
    )

    assert report["accepted_exact_ttm_loss_count"] == 2
    assert report["resolved_unique_signal_date_count"] == 3
    assert report["resolved_audit_observation_count"] == 12
    assert download_calls == [sources["6k_2020_10_29_9m"]["url"]]
    assert len(manifest["source_value_verification"]) == 10
    assert all(
        item["parsed_value"] == item["expected_value"]
        for item in manifest["source_value_verification"]
    )
    assert all(
        source["actual_sha256"] == source["expected_sha256"]
        for source in manifest["source_documents"].values()
    )
    assert manifest["research_only"] is True
    assert manifest["release_status"] == "BLOCKED"
    assert manifest["formal_financials_modified"] is False
    assert manifest["shared_candidate_integrated"] is False
    assert manifest["revenue_assessment"]["direct_growth_emitted"] is False
    assert len(resolutions) == 12
    assert "0001193125-21-074859" not in json.dumps(resolutions)


def test_build_rejects_existing_source_with_wrong_sha(tmp_path, monkeypatch) -> None:
    sources, download_calls = _install_source_fixtures(tmp_path, monkeypatch)
    corrupted = tmp_path / sources["6k_2020_07_30_h1"]["local_path"]
    corrupted.write_bytes(corrupted.read_bytes() + b"tampered")

    with pytest.raises(RuntimeError, match="source SHA-256 mismatch"):
        build(tmp_path)
    assert download_calls == []


def test_build_rejects_hash_valid_html_when_profit_operand_changes(
    tmp_path,
    monkeypatch,
) -> None:
    _install_source_fixtures(
        tmp_path,
        monkeypatch,
        value_overrides={"m9_2020_net_loss": -5_000},
    )
    with pytest.raises(
        RuntimeError, match="source value m9_2020_net_loss changed"
    ):
        build(tmp_path)


def test_build_rejects_hash_valid_html_when_revenue_disclosure_changes(
    tmp_path,
    monkeypatch,
) -> None:
    _install_source_fixtures(
        tmp_path,
        monkeypatch,
        value_overrides={"h1_2020_revenue": 84_000},
    )
    with pytest.raises(
        RuntimeError, match="source value h1_2020_revenue changed"
    ):
        build(tmp_path)
