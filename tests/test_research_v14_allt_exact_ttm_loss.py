from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
import hashlib

import pandas as pd
import pytest

import scripts.research_v14_allt_exact_ttm_loss as allt_ttm
from scripts.research_v14_allt_exact_ttm_loss import (
    AUDIT_OBSERVATIONS,
    BASELINE_BINDING,
    NON_FINANCIAL_AMENDMENT,
    OPERANDS_USD_THOUSANDS,
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
    for item_id, item in OPERANDS_USD_THOUSANDS.items():
        if item["source_id"] == source_id:
            values_by_metric_column[("net_income", item["table_column"])] = (
                overrides.get(item_id, item["value"])
            )
    identity = " | ".join(spec["identity_phrases"])
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
    return (
        "<html><p>" + identity + "</p><table>" + "".join(rows) + "</table></html>"
    ).encode()


def _install_source_fixtures(tmp_path, monkeypatch, *, value_overrides=None):
    sources = deepcopy(SOURCE_DOCUMENTS)
    downloads = {}
    for source_id, source in sources.items():
        raw = _fixture_source_bytes(source_id, value_overrides)
        source["expected_sha256"] = hashlib.sha256(raw).hexdigest()
        downloads[source["url"]] = raw

    def fake_download(url: str) -> bytes:
        return downloads[url]

    monkeypatch.setattr(allt_ttm, "SOURCE_DOCUMENTS", sources)
    monkeypatch.setattr(allt_ttm, "_download_source", fake_download)


def test_exact_ttm_versions_use_us_gaap_cumulative_net_loss() -> None:
    evidence = {row["fiscal_end"]: row for row in exact_ttm_evidence()}
    facts = direct_ttm_facts(fetched_at="2026-08-23")

    assert evidence["2020-09-30"]["formula"] == (
        "FY2019 - 9M_2019 + 9M_2020"
    )
    assert evidence["2020-09-30"]["net_income_ttm"] == -9_349_000
    assert evidence["2020-12-31"]["net_income_ttm"] == -9_348_000
    assert all(row["currency"] == "USD" for row in evidence.values())
    assert all(row["accounting_standard"] == "US-GAAP" for row in evidence.values())
    assert len(facts) == 2
    assert set(facts["metric"]) == {"net_income_ttm"}
    assert not set(facts["metric"]) & {"net_income", "revenue"}


def test_both_age150_observations_use_the_latest_known_version() -> None:
    assert len(AUDIT_OBSERVATIONS) == 2
    resolved = resolve_audit_observations()

    assert resolved["resolved"].all()
    assert set(resolved["decision"]) == {"known_nonpositive_profit"}
    first = resolved.set_index("signal_date").loc["2021-01-29"]
    assert first["fiscal_end"] == "2020-09-30"
    assert first["available_date"] == "2020-11-04"
    assert first["financial_age_days"] == 86
    assert first["net_income_ttm"] == -9_349_000
    second = resolved.set_index("signal_date").loc["2021-02-26"]
    assert second["fiscal_end"] == "2020-12-31"
    assert second["available_date"] == "2021-02-09"
    assert second["financial_age_days"] == 17
    assert second["net_income_ttm"] == -9_348_000


def test_direct_loss_is_pit_limited_and_cannot_create_growth() -> None:
    facts = direct_ttm_facts(fetched_at="2026-08-23")
    facts["fiscal_end"] = pd.to_datetime(facts["fiscal_end"])
    facts["available_date"] = pd.to_datetime(facts["available_date"])

    before_9m = quarterly_profit_ttm_snapshot(
        facts, pd.Timestamp("2020-11-03"), 150
    )
    at_9m = quarterly_profit_ttm_snapshot(
        facts, pd.Timestamp("2020-11-04"), 150
    )
    before_fy = quarterly_profit_ttm_snapshot(
        facts, pd.Timestamp("2021-02-08"), 150
    )
    at_fy = quarterly_profit_ttm_snapshot(
        facts, pd.Timestamp("2021-02-09"), 150
    )
    growth = quarterly_growth_snapshot(
        facts, pd.Timestamp("2021-02-26"), 150
    )

    assert before_9m.empty
    assert at_9m.loc["ALLT", "net_income_ttm"] == -9_349_000
    assert before_fy.loc["ALLT", "net_income_ttm"] == -9_349_000
    assert at_fy.loc["ALLT", "net_income_ttm"] == -9_348_000
    assert growth.empty


def test_real_coverage_classifies_both_observations(monkeypatch) -> None:
    facts = direct_ttm_facts(fetched_at="2026-08-23")
    facts["fiscal_end"] = pd.to_datetime(facts["fiscal_end"])
    facts["available_date"] = pd.to_datetime(facts["available_date"])
    monkeypatch.setattr(
        can_slim_validation,
        "scheduled_signal_dates",
        lambda _index, start, *_args: [pd.Timestamp(start)],
    )
    monkeypatch.setattr(
        can_slim_validation, "market_regime_is_on", lambda *_args, **_kwargs: True
    )
    monkeypatch.setattr(
        can_slim_validation,
        "build_can_slim_technical_cross_section",
        lambda *_args, **_kwargs: pd.DataFrame(
            {"nonfinancial_candidate": [True]}, index=["ALLT"]
        ),
    )
    monkeypatch.setattr(
        can_slim_validation,
        "can_slim_nonfinancial_candidate_mask",
        lambda technical, _config: pd.Series(True, index=technical.index),
    )

    for _scenario, signal_date, maximum_age_days in AUDIT_OBSERVATIONS:
        as_of = pd.Timestamp(signal_date)
        close = pd.DataFrame({"ALLT": [20.0]}, index=[as_of])
        config = replace(
            can_slim_validation.fixed_top3_config(),
            end=signal_date,
            maximum_financial_age_days=maximum_age_days,
        )
        report = can_slim_validation.technical_candidate_financial_coverage(
            close,
            pd.DataFrame({"ALLT": [20_000_000.0]}, index=[as_of]),
            pd.Series([100.0], index=[as_of]),
            facts,
            {as_of: {"ALLT"}},
            config,
            start=signal_date,
            adjusted_close=close,
        )
        row = report["by_signal"][0]
        assert report["missing_financial_observations"] == 0
        assert report["complete"] is True
        assert row["usable_financial_count"] == 0
        assert row["known_nonpositive_profit_count"] == 1


def test_nonfinancial_20f_amendment_does_not_change_operands() -> None:
    validate_source_lock()
    assert NON_FINANCIAL_AMENDMENT["filed"] == "2020-07-01"
    assert NON_FINANCIAL_AMENDMENT["effect"] == (
        "AUDITOR_REPORT_DATE_TYPO_ONLY_NO_FINANCIAL_CHANGE"
    )
    assert NON_FINANCIAL_AMENDMENT["detail"] == (
        "corrected March 26, 2019 to March 26, 2020"
    )


def test_source_lock_rejects_mixed_currency_or_ads_scale() -> None:
    mixed = {name: dict(source) for name, source in SOURCE_DOCUMENTS.items()}
    mixed["6k_2020_11_04_9m_ex991"]["currency"] = "ILS"
    with pytest.raises(ValueError, match="mixed currency or scale"):
        validate_source_lock(mixed)

    per_share = {name: dict(source) for name, source in SOURCE_DOCUMENTS.items()}
    per_share["6k_2020_11_04_9m_ex991"]["scale"] = 1
    with pytest.raises(ValueError, match="mixed currency or scale"):
        validate_source_lock(per_share)


def test_official_sec_source_paths_and_hashes_are_locked() -> None:
    assert {
        source_id: (source["filed"], source["accession"], source["expected_sha256"])
        for source_id, source in SOURCE_DOCUMENTS.items()
    } == {
        "20f_2020_03_26_fy2019_r4": (
            "2020-03-26",
            "0001178913-20-000943",
            "8e6e06e3f18026b17405b998743ad33f92630cec49fed0ac179b4f5cb2e101b1",
        ),
        "6k_2020_11_04_9m_ex991": (
            "2020-11-04",
            "0001178913-20-002985",
            "c71c2c3e8e51afada99530665cd48eab3d10c277899abad56070e1bb16898f4b",
        ),
        "6k_2021_02_09_fy2020_ex991": (
            "2021-02-09",
            "0001178913-21-000386",
            "c5ee5cee0f447ee885de525e15a6f78633d228a6f176293e890e8e0a904fdf70",
        ),
    }


def test_build_verifies_sources_and_writes_isolated_outputs(
    tmp_path, monkeypatch
) -> None:
    _install_source_fixtures(tmp_path, monkeypatch)
    report = build(tmp_path)

    assert report["baseline_binding"] == BASELINE_BINDING
    assert report["resolved_audit_observation_count"] == 2
    assert report["resolved_unique_signal_date_count"] == 2
    assert report["revenue_assessment"]["direct_growth_emitted"] is False
    assert "product and service revenue dimensions" in (
        report["revenue_assessment"]["reason"]
    )
    assert len(report["source_value_verification"]) == 4
    assert report["shared_candidate_integrated"] is False
    assert report["formal_financials_modified"] is False
    facts = pd.read_csv(tmp_path / "strict_quarterly_facts.csv")
    assert facts[["fiscal_end", "available_date", "value"]].to_dict(
        "records"
    ) == [
        {
            "fiscal_end": "2020-09-30",
            "available_date": "2020-11-04",
            "value": -9_349_000,
        },
        {
            "fiscal_end": "2020-12-31",
            "available_date": "2021-02-09",
            "value": -9_348_000,
        },
    ]


def test_build_rejects_changed_source_operand(tmp_path, monkeypatch) -> None:
    _install_source_fixtures(
        tmp_path,
        monkeypatch,
        value_overrides={"m9_2020_net_loss": -7_666},
    )
    with pytest.raises(RuntimeError, match="source value m9_2020_net_loss changed"):
        build(tmp_path)
