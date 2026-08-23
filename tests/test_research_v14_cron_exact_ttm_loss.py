from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
import hashlib

import pandas as pd
import pytest

import scripts.research_v14_cron_exact_ttm_loss as cron_ttm
from scripts.research_v14_cron_exact_ttm_loss import (
    ACCOUNTING_POLICY_AUDIT,
    AUDIT_OBSERVATIONS,
    BASELINE_BINDING,
    OPERANDS_CAD_THOUSANDS,
    POST_SIGNAL_EXCLUSIONS,
    SOURCE_DOCUMENTS,
    SOURCE_PARSE_SPECS,
    SOURCE_VALUE_EXPECTATIONS,
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
    values = {}
    for item_id, item in SOURCE_VALUE_EXPECTATIONS.items():
        if item["source_id"] == source_id:
            values[(item["metric"], item["table_column"])] = overrides.get(
                item_id, item["value"]
            )
    tables = []
    for metric, row_spec in spec["row_specs"].items():
        cells = [f"<td>{row_spec['row_label']}</td>"]
        for column in row_spec["columns"]:
            value = int(values.get((metric, column), 0))
            rendered = f"({abs(value):,})" if value < 0 else f"{value:,}"
            cells.append(f"<td>{rendered}</td>")
        tables.append(
            "<table><tr><td>"
            + " | ".join(row_spec["context_phrases"])
            + "</td></tr><tr>"
            + "".join(cells)
            + "</tr></table>"
        )
    identity = " | ".join(spec["identity_phrases"])
    return ("<html><p>" + identity + "</p>" + "".join(tables) + "</html>").encode()


def _install_source_fixtures(monkeypatch, *, value_overrides=None) -> None:
    sources = deepcopy(SOURCE_DOCUMENTS)
    downloads = {}
    for source_id, source in sources.items():
        raw = _fixture_source_bytes(source_id, value_overrides)
        source["expected_sha256"] = hashlib.sha256(raw).hexdigest()
        downloads[source["url"]] = raw

    def fake_download(url: str) -> bytes:
        return downloads[url]

    monkeypatch.setattr(cron_ttm, "SOURCE_DOCUMENTS", sources)
    monkeypatch.setattr(cron_ttm, "_download_source", fake_download)


def test_exact_ttm_uses_ifrs_cad_attributable_cumulative_loss() -> None:
    evidence = exact_ttm_evidence()[0]
    facts = direct_ttm_facts(fetched_at="2026-08-23")

    assert evidence["formula"] == "FY2017 - 9M_2017 + 9M_2018"
    assert evidence["net_income_ttm"] == -5_474_000
    assert evidence["currency"] == "CAD"
    assert evidence["accounting_standard"] == "IFRS-IASB"
    assert "attributable to Cronos Group" in evidence["profit_scope"]
    assert len(facts) == 1
    assert set(facts["metric"]) == {"net_income_ttm"}
    assert not set(facts["metric"]) & {"net_income", "revenue"}


def test_all_six_observations_are_resolved_at_age_107() -> None:
    assert len(AUDIT_OBSERVATIONS) == 6
    assert {row[1] for row in AUDIT_OBSERVATIONS} == {"2019-02-28"}
    assert {row[2] for row in AUDIT_OBSERVATIONS} == {150, 365, 550}
    resolved = resolve_audit_observations()

    assert resolved["resolved"].all()
    assert set(resolved["decision"]) == {"known_nonpositive_profit"}
    assert set(resolved["fiscal_end"]) == {"2018-09-30"}
    assert set(resolved["available_date"]) == {"2018-11-13"}
    assert set(resolved["financial_age_days"]) == {107}
    assert set(resolved["net_income_ttm"]) == {-5_474_000}


def test_direct_loss_is_pit_limited_and_cannot_create_growth() -> None:
    facts = direct_ttm_facts(fetched_at="2026-08-23")
    facts["fiscal_end"] = pd.to_datetime(facts["fiscal_end"])
    facts["available_date"] = pd.to_datetime(facts["available_date"])

    before = quarterly_profit_ttm_snapshot(
        facts, pd.Timestamp("2018-11-12"), 550
    )
    at_filing = quarterly_profit_ttm_snapshot(
        facts, pd.Timestamp("2018-11-13"), 550
    )
    at_signal = quarterly_profit_ttm_snapshot(
        facts, pd.Timestamp("2019-02-28"), 150
    )
    growth = quarterly_growth_snapshot(
        facts, pd.Timestamp("2019-02-28"), 550
    )

    assert before.empty
    assert at_filing.loc["CRON", "net_income_ttm"] == -5_474_000
    assert at_signal.loc["CRON", "net_income_ttm"] == -5_474_000
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
            {"nonfinancial_candidate": [True]}, index=["CRON"]
        ),
    )
    monkeypatch.setattr(
        can_slim_validation,
        "can_slim_nonfinancial_candidate_mask",
        lambda technical, _config: pd.Series(True, index=technical.index),
    )

    for _scenario, signal_date, maximum_age_days in AUDIT_OBSERVATIONS:
        as_of = pd.Timestamp(signal_date)
        close = pd.DataFrame({"CRON": [20.0]}, index=[as_of])
        config = replace(
            can_slim_validation.fixed_top3_config(),
            end=signal_date,
            maximum_financial_age_days=maximum_age_days,
        )
        report = can_slim_validation.technical_candidate_financial_coverage(
            close,
            pd.DataFrame({"CRON": [20_000_000.0]}, index=[as_of]),
            pd.Series([100.0], index=[as_of]),
            facts,
            {as_of: {"CRON"}},
            config,
            start=signal_date,
            adjusted_close=close,
        )
        row = report["by_signal"][0]
        assert report["missing_financial_observations"] == 0
        assert report["complete"] is True
        assert row["usable_financial_count"] == 0
        assert row["known_nonpositive_profit_count"] == 1


def test_accounting_policy_change_and_post_signal_filings_are_fail_closed() -> None:
    validate_source_lock()
    assert ACCOUNTING_POLICY_AUDIT["application"] == "RETROSPECTIVE"
    assert ACCOUNTING_POLICY_AUDIT["profit_effect"] == (
        "NO_IMPACT_CURRENT_OR_PRIOR_PERIOD_NET_INCOME_LOSS"
    )
    assert all(item["filed"] > "2019-02-28" for item in POST_SIGNAL_EXCLUSIONS)
    assert POST_SIGNAL_EXCLUSIONS[0]["filed"] == "2019-03-26"
    assert POST_SIGNAL_EXCLUSIONS[0]["accession"] == "0001193125-19-085847"
    assert "U.S.-GAAP" in POST_SIGNAL_EXCLUSIONS[1]["reason"]


def test_source_lock_rejects_mixed_currency_or_per_share_scale() -> None:
    mixed = {name: dict(source) for name, source in SOURCE_DOCUMENTS.items()}
    mixed["6k_2018_11_13_9m_ex991"]["currency"] = "USD"
    with pytest.raises(ValueError, match="mixed currency or scale"):
        validate_source_lock(mixed)

    per_share = {name: dict(source) for name, source in SOURCE_DOCUMENTS.items()}
    per_share["6k_2018_11_13_9m_ex991"]["scale"] = 1
    with pytest.raises(ValueError, match="mixed currency or scale"):
        validate_source_lock(per_share)


def test_official_sec_source_paths_and_hashes_are_locked() -> None:
    assert {
        source_id: (source["filed"], source["accession"], source["expected_sha256"])
        for source_id, source in SOURCE_DOCUMENTS.items()
    } == {
        "40f_2018_04_30_fy2017_ex992": (
            "2018-04-30",
            "0001193125-18-140678",
            "c82093a2e2b6bbff52a9d479edf702250b18a5ab62ce9f2af8c3463495fd0cb5",
        ),
        "6k_2018_11_13_9m_ex991": (
            "2018-11-13",
            "0001564590-18-029098",
            "bccbd793104bbacdfbdd1a37e74de0b8b24397d57c8ef40462e4d30840a78627",
        ),
    }


def test_build_verifies_sources_and_writes_isolated_outputs(
    tmp_path, monkeypatch
) -> None:
    _install_source_fixtures(monkeypatch)
    report = build(tmp_path)

    assert report["baseline_binding"] == BASELINE_BINDING
    assert report["resolved_audit_observation_count"] == 6
    assert report["resolved_unique_signal_date_count"] == 1
    assert report["revenue_assessment"]["direct_growth_emitted"] is False
    assert len(report["source_value_verification"]) == 6
    assert report["shared_candidate_integrated"] is False
    assert report["formal_financials_modified"] is False
    facts = pd.read_csv(tmp_path / "strict_quarterly_facts.csv")
    assert facts[["fiscal_end", "available_date", "value"]].to_dict(
        "records"
    ) == [{
        "fiscal_end": "2018-09-30",
        "available_date": "2018-11-13",
        "value": -5_474_000,
    }]


def test_build_rejects_changed_source_operand(tmp_path, monkeypatch) -> None:
    _install_source_fixtures(
        monkeypatch,
        value_overrides={"m9_2018_profit_attributable": -7_536},
    )
    with pytest.raises(
        RuntimeError, match="source value m9_2018_profit_attributable changed"
    ):
        build(tmp_path)
