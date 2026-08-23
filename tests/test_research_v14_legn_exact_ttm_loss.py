from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
import hashlib

import pandas as pd
import pytest

import scripts.research_v14_legn_exact_ttm_loss as legn_ttm
from scripts.research_v14_legn_exact_ttm_loss import (
    AUDIT_OBSERVATIONS,
    BASELINE_BINDING,
    OPERANDS_USD_THOUSANDS,
    REJECTED_LATER_RESTATEMENT,
    REVENUE_DISCLOSURES_USD_THOUSANDS,
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
        **OPERANDS_USD_THOUSANDS,
        **REVENUE_DISCLOSURES_USD_THOUSANDS,
    }.items():
        if item["source_id"] == source_id:
            metric = (
                "net_income" if item_id in OPERANDS_USD_THOUSANDS else "revenue"
            )
            values_by_metric_column[(metric, item["table_column"])] = (
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


def _install_source_fixtures(tmp_path, monkeypatch, *, value_overrides=None):
    sources = deepcopy(SOURCE_DOCUMENTS)
    downloads = {}
    for source_id, source in sources.items():
        raw = _fixture_source_bytes(source_id, value_overrides)
        source["expected_sha256"] = hashlib.sha256(raw).hexdigest()
        downloads[source["url"]] = raw

    def fake_download(url: str) -> bytes:
        return downloads[url]

    monkeypatch.setattr(legn_ttm, "SOURCE_DOCUMENTS", sources)
    monkeypatch.setattr(legn_ttm, "_download_source", fake_download)


def test_exact_ttm_uses_same_currency_cumulative_ifrs_profit_loss() -> None:
    evidence = exact_ttm_evidence()
    facts = direct_ttm_facts(fetched_at="2026-08-23")

    assert evidence["formula"] == "FY2020 - H1_2020 + H1_2021"
    assert evidence["net_income_ttm"] == -296_856_000
    assert evidence["currency"] == "USD"
    assert evidence["source_scale"] == 1_000
    assert evidence["accounting_standard"] == "IFRS-IASB"
    assert evidence["profit_scope"].startswith("consolidated ProfitLoss")
    assert len(facts) == 1
    assert set(facts["metric"]) == {"net_income_ttm"}
    assert not set(facts["metric"]) & {"net_income", "revenue"}


def test_all_three_dates_and_six_scenario_observations_resolve() -> None:
    assert len(AUDIT_OBSERVATIONS) == 6
    resolved = resolve_audit_observations()

    assert resolved["resolved"].all()
    assert set(resolved["decision"]) == {"known_nonpositive_profit"}
    expected = {
        "2021-09-30": (2, 38),
        "2021-10-29": (2, 67),
        "2021-11-30": (2, 99),
    }
    for signal_date, (count, age) in expected.items():
        rows = resolved.loc[resolved["signal_date"].eq(signal_date)]
        assert len(rows) == count
        assert set(rows["fiscal_end"]) == {"2021-06-30"}
        assert set(rows["available_date"]) == {"2021-08-23"}
        assert set(rows["financial_age_days"]) == {age}
        assert set(rows["net_income_ttm"]) == {-296_856_000}


def test_direct_loss_is_pit_limited_and_cannot_create_growth() -> None:
    facts = direct_ttm_facts(fetched_at="2026-08-23")
    facts["fiscal_end"] = pd.to_datetime(facts["fiscal_end"])
    facts["available_date"] = pd.to_datetime(facts["available_date"])

    before = quarterly_profit_ttm_snapshot(
        facts, pd.Timestamp("2021-08-22"), 150
    )
    at_release = quarterly_profit_ttm_snapshot(
        facts, pd.Timestamp("2021-08-23"), 150
    )
    growth = quarterly_growth_snapshot(
        facts, pd.Timestamp("2021-11-30"), 150
    )

    assert before.empty
    assert at_release.loc["LEGN", "net_income_ttm"] == -296_856_000
    assert growth.empty


def test_real_coverage_classifies_all_six_observations(monkeypatch) -> None:
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
            {"nonfinancial_candidate": [True]}, index=["LEGN"]
        ),
    )
    monkeypatch.setattr(
        can_slim_validation,
        "can_slim_nonfinancial_candidate_mask",
        lambda technical, _config: pd.Series(True, index=technical.index),
    )

    for _scenario, signal_date, maximum_age_days in AUDIT_OBSERVATIONS:
        as_of = pd.Timestamp(signal_date)
        close = pd.DataFrame({"LEGN": [20.0]}, index=[as_of])
        config = replace(
            can_slim_validation.fixed_top3_config(),
            end=signal_date,
            maximum_financial_age_days=maximum_age_days,
        )
        report = can_slim_validation.technical_candidate_financial_coverage(
            close,
            pd.DataFrame({"LEGN": [20_000_000.0]}, index=[as_of]),
            pd.Series([100.0], index=[as_of]),
            facts,
            {as_of: {"LEGN"}},
            config,
            start=signal_date,
            adjusted_close=close,
        )
        row = report["by_signal"][0]
        assert report["missing_financial_observations"] == 0
        assert report["complete"] is True
        assert row["usable_financial_count"] == 0
        assert row["known_nonpositive_profit_count"] == 1


def test_later_collaboration_revenue_restatement_is_forbidden() -> None:
    validate_source_lock()
    assert REJECTED_LATER_RESTATEMENT["filed"] == "2023-02-17"
    assert REJECTED_LATER_RESTATEMENT["announced"] == "2022-10-19"
    assert REJECTED_LATER_RESTATEMENT[
        "original_fy2020_profit_loss_usd_thousands"
    ] == -303_477
    assert REJECTED_LATER_RESTATEMENT[
        "restated_fy2020_profit_loss_usd_thousands"
    ] == -266_373

    later = {name: dict(source) for name, source in SOURCE_DOCUMENTS.items()}
    later["later_restatement"] = {
        **next(iter(SOURCE_DOCUMENTS.values())),
        "accession": REJECTED_LATER_RESTATEMENT["accession"],
        "filed": REJECTED_LATER_RESTATEMENT["filed"],
    }
    with pytest.raises(ValueError, match="later restatement is forbidden"):
        validate_source_lock(later)


def test_source_lock_rejects_mixed_currency_or_per_ads_scale() -> None:
    mixed = {name: dict(source) for name, source in SOURCE_DOCUMENTS.items()}
    mixed["6k_2021_08_23_h1_r2"]["currency"] = "CNY"
    with pytest.raises(ValueError, match="mixed currency or scale"):
        validate_source_lock(mixed)

    per_ads = {name: dict(source) for name, source in SOURCE_DOCUMENTS.items()}
    per_ads["6k_2021_08_23_h1_r2"]["scale"] = 1
    with pytest.raises(ValueError, match="mixed currency or scale"):
        validate_source_lock(per_ads)


def test_official_sec_source_paths_and_hashes_are_locked() -> None:
    assert {
        source_id: (source["filed"], source["accession"], source["expected_sha256"])
        for source_id, source in SOURCE_DOCUMENTS.items()
    } == {
        "20f_2021_04_02_fy2020_r2": (
            "2021-04-02",
            "0001564590-21-017439",
            "d7924dbe20cd4f1dd6bca76344cfb8d27370ef9ca4ec1ff5e242e47d3ec72dfb",
        ),
        "6k_2021_08_23_h1_r2": (
            "2021-08-23",
            "0001564590-21-045342",
            "05d0bb172d32fbd9b0aa159078a00a410560f205d0e2c9b6d6769be0f98b6f59",
        ),
    }


def test_build_verifies_sources_values_and_writes_isolated_outputs(
    tmp_path, monkeypatch
) -> None:
    _install_source_fixtures(tmp_path, monkeypatch)
    report = build(tmp_path)

    assert report["resolved_audit_observation_count"] == 6
    assert report["resolved_unique_signal_date_count"] == 3
    assert report["baseline_binding"] == BASELINE_BINDING
    assert report["revenue_assessment"][
        "as_reported_ttm_revenue_usd_thousands"
    ] == 86_445
    assert len(report["source_value_verification"]) == 6
    assert report["shared_candidate_integrated"] is False
    assert report["formal_financials_modified"] is False
    facts = pd.read_csv(tmp_path / "strict_quarterly_facts.csv")
    assert facts[["metric", "value"]].to_dict("records") == [{
        "metric": "net_income_ttm", "value": -296_856_000
    }]


def test_build_rejects_changed_source_operand(tmp_path, monkeypatch) -> None:
    _install_source_fixtures(
        tmp_path,
        monkeypatch,
        value_overrides={"h1_2021_profit_loss": -172_482},
    )
    with pytest.raises(RuntimeError, match="source value h1_2021_profit_loss changed"):
        build(tmp_path)
