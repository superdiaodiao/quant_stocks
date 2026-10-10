from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
import hashlib

import pandas as pd
import pytest

import scripts.research_v14_mogo_exact_ttm_loss as mogo_ttm
from scripts.research_v14_mogo_exact_ttm_loss import (
    ACCOUNTING_POLICY_AUDIT,
    AUDIT_OBSERVATIONS,
    BASELINE_BINDING,
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

    monkeypatch.setattr(mogo_ttm, "SOURCE_DOCUMENTS", sources)
    monkeypatch.setattr(mogo_ttm, "_download_source", fake_download)


def test_exact_ttm_uses_direct_ifrs_cad_quarters_and_cumulative_identity() -> None:
    evidence = exact_ttm_evidence()[0]
    facts = direct_ttm_facts(fetched_at="2026-08-23")

    assert evidence["formula"] == "Q4_2019 + Q1_2020 + Q2_2020 + Q3_2020"
    assert evidence["equivalent_formula"] == (
        "FY2019 - 9M_2019_revised + 9M_2020"
    )
    assert evidence["net_income_ttm"] == -16_784_000
    assert evidence["currency"] == "CAD"
    assert evidence["accounting_standard"] == "IFRS-IASB"
    assert "wholly-owned subsidiaries" in evidence["profit_scope"]
    assert len(facts) == 1
    assert set(facts["metric"]) == {"net_income_ttm"}
    assert not set(facts["metric"]) & {"net_income", "revenue"}


def test_all_three_observations_are_resolved_at_age_51() -> None:
    assert len(AUDIT_OBSERVATIONS) == 3
    assert {row[1] for row in AUDIT_OBSERVATIONS} == {"2020-12-31"}
    assert {row[2] for row in AUDIT_OBSERVATIONS} == {150, 365, 550}
    resolved = resolve_audit_observations()

    assert resolved["resolved"].all()
    assert set(resolved["decision"]) == {"known_nonpositive_profit"}
    assert set(resolved["fiscal_end"]) == {"2020-09-30"}
    assert set(resolved["available_date"]) == {"2020-11-10"}
    assert set(resolved["financial_age_days"]) == {51}
    assert set(resolved["net_income_ttm"]) == {-16_784_000}


def test_direct_loss_is_pit_limited_and_cannot_create_growth() -> None:
    facts = direct_ttm_facts(fetched_at="2026-08-23")
    facts["fiscal_end"] = pd.to_datetime(facts["fiscal_end"])
    facts["available_date"] = pd.to_datetime(facts["available_date"])

    before = quarterly_profit_ttm_snapshot(
        facts, pd.Timestamp("2020-11-09"), 550
    )
    at_filing = quarterly_profit_ttm_snapshot(
        facts, pd.Timestamp("2020-11-10"), 550
    )
    at_signal = quarterly_profit_ttm_snapshot(
        facts, pd.Timestamp("2020-12-31"), 150
    )
    growth = quarterly_growth_snapshot(
        facts, pd.Timestamp("2020-12-31"), 550
    )

    assert before.empty
    assert at_filing.loc["MOGO", "net_income_ttm"] == -16_784_000
    assert at_signal.loc["MOGO", "net_income_ttm"] == -16_784_000
    assert growth.empty


def test_real_coverage_classifies_all_three_observations(monkeypatch) -> None:
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
            {"nonfinancial_candidate": [True]}, index=["MOGO"]
        ),
    )
    monkeypatch.setattr(
        can_slim_validation,
        "can_slim_nonfinancial_candidate_mask",
        lambda technical, _config: pd.Series(True, index=technical.index),
    )

    for _scenario, signal_date, maximum_age_days in AUDIT_OBSERVATIONS:
        as_of = pd.Timestamp(signal_date)
        close = pd.DataFrame({"MOGO": [20.0]}, index=[as_of])
        config = replace(
            can_slim_validation.fixed_top3_config(),
            end=signal_date,
            maximum_financial_age_days=maximum_age_days,
        )
        report = can_slim_validation.technical_candidate_financial_coverage(
            close,
            pd.DataFrame({"MOGO": [20_000_000.0]}, index=[as_of]),
            pd.Series([100.0], index=[as_of]),
            facts,
            {as_of: {"MOGO"}},
            config,
            start=signal_date,
            adjusted_close=close,
        )
        row = report["by_signal"][0]
        assert report["missing_financial_observations"] == 0
        assert report["complete"] is True
        assert row["usable_financial_count"] == 0
        assert row["known_nonpositive_profit_count"] == 1


def test_recast_scope_and_later_annual_are_fail_closed() -> None:
    validate_source_lock()
    ppa = ACCOUNTING_POLICY_AUDIT["q3_2019_purchase_price_adjustment"]
    recast = ACCOUNTING_POLICY_AUDIT["loan_protection_recast"]

    assert ppa["application"] == "RETROSPECTIVE_IN_PRE_SIGNAL_2020_Q3_COMPARATIVE"
    assert ppa["gain_previously_reported_cad_thousands"] == -14_349
    assert ppa["gain_revised_cad_thousands"] == -13_249
    assert recast["profit_effect"] == "NO_IMPACT_ON_GROSS_PROFIT_OR_NET_LOSS"
    assert "wholly-owned" in ACCOUNTING_POLICY_AUDIT["consolidation_scope"]
    assert all(item["filed"] > "2020-12-31" for item in POST_SIGNAL_EXCLUSIONS)
    assert POST_SIGNAL_EXCLUSIONS[0]["filed"] == "2021-03-26"
    assert POST_SIGNAL_EXCLUSIONS[0]["accession"] == "0001564590-21-015848"


def test_source_lock_rejects_mixed_currency_or_per_share_scale() -> None:
    mixed = {name: dict(source) for name, source in SOURCE_DOCUMENTS.items()}
    mixed["6k_2020_11_10_q3_fs_ex991"]["currency"] = "USD"
    with pytest.raises(ValueError, match="mixed currency or scale"):
        validate_source_lock(mixed)

    per_share = {name: dict(source) for name, source in SOURCE_DOCUMENTS.items()}
    per_share["6k_2020_11_10_q3_mda_ex992"]["scale"] = 1
    with pytest.raises(ValueError, match="mixed currency or scale"):
        validate_source_lock(per_share)


def test_official_sec_source_paths_and_hashes_are_locked() -> None:
    assert {
        source_id: (source["filed"], source["accession"], source["expected_sha256"])
        for source_id, source in SOURCE_DOCUMENTS.items()
    } == {
        "20f_2020_05_28_fy2019": (
            "2020-05-28",
            "0001477932-20-003100",
            "488c8651968e5053ccd6022fc34774e1a669ff49f01cba38ca6dc33ba0c0c583",
        ),
        "6k_2020_11_10_q3_fs_ex991": (
            "2020-11-10",
            "0001477932-20-006358",
            "2fb690f145b2879262b00a231015327812346919a32d2813ece48cdf465066c8",
        ),
        "6k_2020_11_10_q3_mda_ex992": (
            "2020-11-10",
            "0001477932-20-006358",
            "f717852aeffb2f924e41726f4545df0e8b0caefe690e23e49a1d02f4b263ae74",
        ),
    }
    assert all(source["url"].startswith("https://www.sec.gov/Archives/") for source in SOURCE_DOCUMENTS.values())


def test_build_verifies_sources_and_writes_isolated_outputs(
    tmp_path, monkeypatch
) -> None:
    _install_source_fixtures(monkeypatch)
    report = build(tmp_path)

    assert report["baseline_binding"] == BASELINE_BINDING
    assert report["resolved_audit_observation_count"] == 3
    assert report["resolved_unique_signal_date_count"] == 1
    assert report["revenue_assessment"]["direct_growth_emitted"] is False
    assert len(report["source_value_verification"]) == 7
    assert report["shared_candidate_integrated"] is False
    assert report["formal_financials_modified"] is False
    facts = pd.read_csv(tmp_path / "strict_quarterly_facts.csv")
    assert facts[["fiscal_end", "available_date", "value"]].to_dict(
        "records"
    ) == [{
        "fiscal_end": "2020-09-30",
        "available_date": "2020-11-10",
        "value": -16_784_000,
    }]


def test_build_rejects_changed_source_operand(tmp_path, monkeypatch) -> None:
    _install_source_fixtures(
        monkeypatch,
        value_overrides={"q3_2020_profit_loss": 1_020},
    )
    with pytest.raises(
        RuntimeError, match="source value q3_2020_profit_loss changed"
    ):
        build(tmp_path)
