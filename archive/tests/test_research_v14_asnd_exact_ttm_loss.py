from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
import hashlib

import pandas as pd
import pytest

import scripts.research_v14_asnd_exact_ttm_loss as asnd_ttm
from scripts.research_v14_asnd_exact_ttm_loss import (
    AUDIT_OBSERVATIONS,
    BASELINE_BINDING,
    OPERANDS_EUR_THOUSANDS,
    POST_SIGNAL_CORROBORATION,
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
    attributable_values = {}
    for item_id, item in OPERANDS_EUR_THOUSANDS.items():
        if item["source_id"] == source_id:
            attributable_values[item["table_column"]] = overrides.get(
                item_id, item["value"]
            )
    identity = " | ".join(spec["identity_phrases"])
    rows = [
        "<tr><td>" + " | ".join(spec["context_phrases"]) + "</td></tr>"
    ]
    for metric, label in spec["row_labels"].items():
        cells = [f"<td>{label}</td>"]
        for column in spec["columns"]:
            value = int(attributable_values.get(column, 0))
            rendered = f"({abs(value):,})" if value < 0 else f"{value:,}"
            cells.append(f"<td>{rendered}</td>")
        rows.append("<tr>" + "".join(cells) + "</tr>")
    return (
        "<html><p>" + identity + "</p><table>" + "".join(rows) + "</table></html>"
    ).encode()


def _install_source_fixtures(monkeypatch, *, value_overrides=None) -> None:
    sources = deepcopy(SOURCE_DOCUMENTS)
    downloads = {}
    for source_id, source in sources.items():
        raw = _fixture_source_bytes(source_id, value_overrides)
        source["expected_sha256"] = hashlib.sha256(raw).hexdigest()
        downloads[source["url"]] = raw

    def fake_download(url: str) -> bytes:
        return downloads[url]

    monkeypatch.setattr(asnd_ttm, "SOURCE_DOCUMENTS", sources)
    monkeypatch.setattr(asnd_ttm, "_download_source", fake_download)


def test_exact_ttm_uses_ifrs_eur_attributable_cumulative_loss() -> None:
    evidence = exact_ttm_evidence()[0]
    facts = direct_ttm_facts(fetched_at="2026-08-23")

    assert evidence["formula"] == "FY2017 - 9M_2017 + 9M_2018"
    assert evidence["net_income_ttm"] == -132_294_000
    assert evidence["currency"] == "EUR"
    assert evidence["accounting_standard"] == "IFRS-IASB"
    assert "attributable to owners" in evidence["profit_scope"]
    assert len(facts) == 1
    assert set(facts["metric"]) == {"net_income_ttm"}
    assert not set(facts["metric"]) & {"net_income", "revenue"}


def test_all_six_observations_are_resolved_at_age_121() -> None:
    assert len(AUDIT_OBSERVATIONS) == 6
    assert {row[1] for row in AUDIT_OBSERVATIONS} == {"2019-03-29"}
    assert {row[2] for row in AUDIT_OBSERVATIONS} == {150, 365, 550}
    resolved = resolve_audit_observations()

    assert resolved["resolved"].all()
    assert set(resolved["decision"]) == {"known_nonpositive_profit"}
    assert set(resolved["fiscal_end"]) == {"2018-09-30"}
    assert set(resolved["available_date"]) == {"2018-11-28"}
    assert set(resolved["financial_age_days"]) == {121}
    assert set(resolved["net_income_ttm"]) == {-132_294_000}


def test_direct_loss_is_pit_limited_and_cannot_create_growth() -> None:
    facts = direct_ttm_facts(fetched_at="2026-08-23")
    facts["fiscal_end"] = pd.to_datetime(facts["fiscal_end"])
    facts["available_date"] = pd.to_datetime(facts["available_date"])

    before = quarterly_profit_ttm_snapshot(
        facts, pd.Timestamp("2018-11-27"), 550
    )
    at_filing = quarterly_profit_ttm_snapshot(
        facts, pd.Timestamp("2018-11-28"), 550
    )
    at_signal = quarterly_profit_ttm_snapshot(
        facts, pd.Timestamp("2019-03-29"), 150
    )
    growth = quarterly_growth_snapshot(
        facts, pd.Timestamp("2019-03-29"), 550
    )

    assert before.empty
    assert at_filing.loc["ASND", "net_income_ttm"] == -132_294_000
    assert at_signal.loc["ASND", "net_income_ttm"] == -132_294_000
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
            {"nonfinancial_candidate": [True]}, index=["ASND"]
        ),
    )
    monkeypatch.setattr(
        can_slim_validation,
        "can_slim_nonfinancial_candidate_mask",
        lambda technical, _config: pd.Series(True, index=technical.index),
    )

    for _scenario, signal_date, maximum_age_days in AUDIT_OBSERVATIONS:
        as_of = pd.Timestamp(signal_date)
        close = pd.DataFrame({"ASND": [60.0]}, index=[as_of])
        config = replace(
            can_slim_validation.fixed_top3_config(),
            end=signal_date,
            maximum_financial_age_days=maximum_age_days,
        )
        report = can_slim_validation.technical_candidate_financial_coverage(
            close,
            pd.DataFrame({"ASND": [20_000_000.0]}, index=[as_of]),
            pd.Series([100.0], index=[as_of]),
            facts,
            {as_of: {"ASND"}},
            config,
            start=signal_date,
            adjusted_close=close,
        )
        row = report["by_signal"][0]
        assert report["missing_financial_observations"] == 0
        assert report["complete"] is True
        assert row["usable_financial_count"] == 0
        assert row["known_nonpositive_profit_count"] == 1


def test_post_signal_20f_is_corroboration_only() -> None:
    validate_source_lock()
    assert POST_SIGNAL_CORROBORATION["filed"] == "2019-04-03"
    assert POST_SIGNAL_CORROBORATION["filed"] > "2019-03-29"
    assert POST_SIGNAL_CORROBORATION["accession"] not in {
        source["accession"] for source in SOURCE_DOCUMENTS.values()
    }
    assert POST_SIGNAL_CORROBORATION["effect"] == (
        "CORROBORATES_NO_RESTATEMENT_BUT_EXCLUDED_AFTER_SIGNAL"
    )
    assert POST_SIGNAL_CORROBORATION[
        "fy2017_profit_attributable_eur_thousands"
    ] == -123_897
    assert POST_SIGNAL_CORROBORATION["ifrs_9_effect"].startswith("NO_IMPACT")
    assert POST_SIGNAL_CORROBORATION["ifrs_15_effect"].startswith("NO_IMPACT")


def test_source_lock_rejects_mixed_currency_or_ads_scale() -> None:
    mixed = {name: dict(source) for name, source in SOURCE_DOCUMENTS.items()}
    mixed["6k_2018_11_28_9m_ex991"]["currency"] = "USD"
    with pytest.raises(ValueError, match="mixed currency or scale"):
        validate_source_lock(mixed)

    per_ads = {name: dict(source) for name, source in SOURCE_DOCUMENTS.items()}
    per_ads["6k_2018_11_28_9m_ex991"]["scale"] = 1
    with pytest.raises(ValueError, match="mixed currency or scale"):
        validate_source_lock(per_ads)


def test_official_sec_source_paths_and_hashes_are_locked() -> None:
    assert {
        source_id: (source["filed"], source["accession"], source["expected_sha256"])
        for source_id, source in SOURCE_DOCUMENTS.items()
    } == {
        "20f_2018_03_28_fy2017": (
            "2018-03-28",
            "0001193125-18-099514",
            "c05e3b53b1a67f92b3a03ccbfd09022b70915dc261b36c2709b5534ae8f65752",
        ),
        "6k_2018_11_28_9m_ex991": (
            "2018-11-28",
            "0001193125-18-336571",
            "518b47a9d81ffeb2438d501c9acce115f92a41471ac552da618e4afcfb7c9abd",
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
    assert len(report["source_value_verification"]) == 3
    assert all(
        item["attribution_parity"]
        for item in report["source_value_verification"]
    )
    assert report["shared_candidate_integrated"] is False
    assert report["formal_financials_modified"] is False
    facts = pd.read_csv(tmp_path / "strict_quarterly_facts.csv")
    assert facts[["fiscal_end", "available_date", "value"]].to_dict(
        "records"
    ) == [{
        "fiscal_end": "2018-09-30",
        "available_date": "2018-11-28",
        "value": -132_294_000,
    }]


def test_build_rejects_changed_source_operand(tmp_path, monkeypatch) -> None:
    _install_source_fixtures(
        monkeypatch,
        value_overrides={"m9_2018_profit_attributable": -98_118},
    )
    with pytest.raises(
        RuntimeError, match="source value m9_2018_profit_attributable changed"
    ):
        build(tmp_path)
