from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
import hashlib

import pandas as pd
import pytest

import scripts.research_v14_uxin_exact_ttm_loss as uxin_ttm
from scripts.research_v14_uxin_exact_ttm_loss import (
    AUDIT_OBSERVATIONS,
    BASELINE_BINDING,
    INTERVENING_FILINGS,
    OPERANDS_CNY_THOUSANDS,
    POST_SIGNAL_EXCLUSIONS,
    SOURCE_DOCUMENTS,
    SOURCE_IDENTITIES,
    SOURCE_PARSE_SPECS,
    SOURCE_VALUE_EXPECTATIONS,
    TTM_SPECS,
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
    identity = " | ".join(SOURCE_IDENTITIES[source_id])
    if source_id not in SOURCE_PARSE_SPECS:
        return ("<html><p>" + identity + "</p></html>").encode()

    values = {}
    for item_id, item in SOURCE_VALUE_EXPECTATIONS.items():
        if item[0] == source_id:
            values[(item[1], item[2])] = overrides.get(item_id, item[3])
    tables = []
    for metric, row_spec in SOURCE_PARSE_SPECS[source_id]["row_specs"].items():
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

    monkeypatch.setattr(uxin_ttm, "SOURCE_DOCUMENTS", sources)
    monkeypatch.setattr(uxin_ttm, "_download_source", fake_download)


def test_two_exact_ttm_losses_use_total_profitloss_scope() -> None:
    evidence = exact_ttm_evidence()
    facts = direct_ttm_facts(fetched_at="2026-08-23")

    assert [item["formula"] for item in evidence] == [
        "FY2019 - (Q1_2019 + H1_2019) + (Q1_2020 + H1_2020)",
        "Q1_2020 + H1_2020 + Q4_2020",
    ]
    assert [item["net_income_ttm"] for item in evidence] == [
        -3_571_850_000,
        -2_777_979_000,
    ]
    assert evidence[1]["roll_forward_formula"] == (
        "TTM_2020_09 - Q4_2019 + Q4_2020"
    )
    assert all(item["currency"] == "CNY" for item in evidence)
    assert all(item["accounting_standard"] == "US-GAAP" for item in evidence)
    assert all("continuing and discontinued" in item["profit_scope"] for item in evidence)
    assert all("not ADS/EPS" in item["profit_scope"] for item in evidence)
    assert set(facts["metric"]) == {"net_income_ttm"}
    assert not set(facts["metric"]) & {"net_income", "revenue"}


def test_period_partition_and_republication_identities_are_exact() -> None:
    value = lambda item_id: SOURCE_VALUE_EXPECTATIONS[item_id][3]

    assert value("fy2019_total") == (
        value("q1_2019_total")
        + value("h1_2019_total")
        + value("q4_2019_total")
    )
    assert value("m9_2019_total") == (
        value("h1_2019_total") + value("q4_2019_total")
    )
    assert value("m9_2020_total") == (
        value("h1_2020_total") + value("q4_2020_total")
    )
    assert value("q1_2020_total") + value("h1_2020_total") + value(
        "q4_2020_total"
    ) == TTM_SPECS[1]["expected_cny_thousands"]


@pytest.mark.parametrize(
    ("period", "total", "continuing", "discontinued", "attributable", "nci"),
    [
        ("FY2019", -1_990_128, -1_327_678, -662_450, -1_988_676, -1_452),
        ("Q1 2019", -284_984, -295_539, 10_555, -284_539, -445),
        ("Q1 2020", -2_489_562, -2_034_385, -455_177, -2_484_179, -5_383),
        ("H1 2019", -738_416, -443_103, -295_313, -737_738, -678),
        ("H1 2020", -115_560, -411_304, 295_744, -115_553, -7),
        ("Q4 2019", -966_728, -589_036, -377_692, -966_399, -329),
        ("Q4 2020", -172_857, -172_857, 0, -172_857, 0),
    ],
)
def test_total_scope_reconciles_discontinued_and_attribution(
    period, total, continuing, discontinued, attributable, nci
) -> None:
    assert total == continuing + discontinued, period
    assert total - attributable == nci, period


def test_all_five_observations_use_april_29_state() -> None:
    assert len(AUDIT_OBSERVATIONS) == 5
    assert {row[1] for row in AUDIT_OBSERVATIONS} == {
        "2021-04-30", "2021-05-28"
    }
    assert {row[2] for row in AUDIT_OBSERVATIONS} == {150, 365}
    resolved = resolve_audit_observations()

    assert resolved["resolved"].all()
    assert set(resolved["decision"]) == {"known_nonpositive_profit"}
    assert set(resolved["fiscal_end"]) == {"2020-12-31"}
    assert set(resolved["available_date"]) == {"2021-04-29"}
    assert set(resolved["financial_age_days"]) == {1, 29}
    assert set(resolved["net_income_ttm"]) == {-2_777_979_000}


def test_direct_loss_is_pit_limited_and_cannot_create_growth() -> None:
    facts = direct_ttm_facts(fetched_at="2026-08-23")
    facts["fiscal_end"] = pd.to_datetime(facts["fiscal_end"])
    facts["available_date"] = pd.to_datetime(facts["available_date"])

    before_first = quarterly_profit_ttm_snapshot(
        facts, pd.Timestamp("2020-12-16"), 365
    )
    first_filing = quarterly_profit_ttm_snapshot(
        facts, pd.Timestamp("2020-12-17"), 365
    )
    before_update = quarterly_profit_ttm_snapshot(
        facts, pd.Timestamp("2021-04-28"), 365
    )
    at_update = quarterly_profit_ttm_snapshot(
        facts, pd.Timestamp("2021-04-29"), 365
    )
    growth = quarterly_growth_snapshot(
        facts, pd.Timestamp("2021-05-28"), 365
    )

    assert before_first.empty
    assert first_filing.loc["UXIN", "net_income_ttm"] == -3_571_850_000
    assert before_update.loc["UXIN", "net_income_ttm"] == -3_571_850_000
    assert at_update.loc["UXIN", "net_income_ttm"] == -2_777_979_000
    assert at_update.loc["UXIN", "financial_age_days"] == 0
    assert growth.empty


def test_real_coverage_classifies_all_five_observations(monkeypatch) -> None:
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
            {"nonfinancial_candidate": [True]}, index=["UXIN"]
        ),
    )
    monkeypatch.setattr(
        can_slim_validation,
        "can_slim_nonfinancial_candidate_mask",
        lambda technical, _config: pd.Series(True, index=technical.index),
    )

    for _scenario, signal_date, maximum_age_days in AUDIT_OBSERVATIONS:
        as_of = pd.Timestamp(signal_date)
        close = pd.DataFrame({"UXIN": [20.0]}, index=[as_of])
        config = replace(
            can_slim_validation.fixed_top3_config(),
            end=signal_date,
            maximum_financial_age_days=maximum_age_days,
        )
        report = can_slim_validation.technical_candidate_financial_coverage(
            close,
            pd.DataFrame({"UXIN": [20_000_000.0]}, index=[as_of]),
            pd.Series([100.0], index=[as_of]),
            facts,
            {as_of: {"UXIN"}},
            config,
            start=signal_date,
            adjusted_close=close,
        )
        row = report["by_signal"][0]
        assert report["missing_financial_observations"] == 0
        assert report["complete"] is True
        assert row["usable_financial_count"] == 0
        assert row["known_nonpositive_profit_count"] == 1


def test_intervening_filings_and_post_signal_annual_are_fail_closed() -> None:
    validate_source_lock()
    assert set(INTERVENING_FILINGS) == {
        "6k_2021_01_25_cfo_ex991",
        "6k_2021_04_01_term_sheet_ex991",
    }
    assert not any(item["financial_payload"] for item in INTERVENING_FILINGS.values())
    assert all(item["filed"] > "2021-05-28" for item in POST_SIGNAL_EXCLUSIONS)
    assert POST_SIGNAL_EXCLUSIONS[0]["filed"] == "2021-07-30"
    assert POST_SIGNAL_EXCLUSIONS[0]["accession"] == "0001104659-21-098224"
    assert "never backfilled" in POST_SIGNAL_EXCLUSIONS[0]["reason"]


def test_source_lock_rejects_mixed_currency_or_per_share_scale() -> None:
    mixed = {name: dict(source) for name, source in SOURCE_DOCUMENTS.items()}
    mixed["6k_2021_04_29_q3_ex991"]["currency"] = "USD"
    with pytest.raises(ValueError, match="mixed currency or scale"):
        validate_source_lock(mixed)

    per_share = {name: dict(source) for name, source in SOURCE_DOCUMENTS.items()}
    per_share["6k_2021_04_29_q3_ex991"]["scale"] = 1
    with pytest.raises(ValueError, match="mixed currency or scale"):
        validate_source_lock(per_share)


def test_official_sec_source_paths_and_hashes_are_locked() -> None:
    assert {
        source_id: (source["filed"], source["accession"], source["expected_sha256"])
        for source_id, source in SOURCE_DOCUMENTS.items()
    } == {
        "20f_2020_07_24_transition": (
            "2020-07-24", "0001104659-20-086426",
            "281df4b6d187c8b7e6f55591177b6e9bb5edb0cf2aa11c4ade69643bec393529",
        ),
        "6k_2020_12_17_h1_ex991": (
            "2020-12-17", "0001104659-20-136853",
            "c42dc89a5dd72571aeb1f7bb16ffe2bc6e0d33720180e3592e522c8f5ae85937",
        ),
        "6k_2021_01_25_cfo_ex991": (
            "2021-01-25", "0001104659-21-006789",
            "e04e98641009bde74909389b9e4e8ff2eecfe1be96441ed5c5e1b682a5c7dfff",
        ),
        "6k_2021_04_01_term_sheet_ex991": (
            "2021-04-01", "0001104659-21-045102",
            "0698c2b5eaef092dd3a8ea63b953e9d03e5190c02fee44700502569a398fc660",
        ),
        "6k_2021_04_29_q3_ex991": (
            "2021-04-29", "0001104659-21-056965",
            "3ef5855bf6b3e992a812cae417bb7bd7c7b15917da5c3e9cd7ac89f46f662089",
        ),
    }


def test_baseline_is_bound_to_exact_five_observation_aggregate() -> None:
    assert BASELINE_BINDING["quarterly_sha256"] == (
        "a7663535f1dc11b42e8fb7802948f5ae8a355626f6d4b4d4d45be9996324b87b"
    )
    assert BASELINE_BINDING["audit_sha256"] == (
        "c5133e9899bdf73f1192b28f19ef7c3f920f170ec56f2cc771419151a64783f3"
    )
    assert BASELINE_BINDING["financial_priorities_sha256"] == (
        "445012666a3e95017fcf097ce77bea532dda194b849fa76d6d3de16e194d3115"
    )


def test_build_verifies_sources_and_writes_isolated_outputs(
    tmp_path, monkeypatch
) -> None:
    _install_source_fixtures(monkeypatch)
    report = build(tmp_path)

    assert report["baseline_binding"] == BASELINE_BINDING
    assert report["resolved_audit_observation_count"] == 5
    assert report["resolved_unique_signal_date_count"] == 2
    assert report["revenue_assessment"]["direct_growth_emitted"] is False
    assert len(report["source_value_verification"]) == len(
        SOURCE_VALUE_EXPECTATIONS
    )
    assert report["shared_candidate_integrated"] is False
    assert report["formal_financials_modified"] is False
    assert "one ADS representing three" in report["security"]
    facts = pd.read_csv(tmp_path / "strict_quarterly_facts.csv")
    assert facts[["fiscal_end", "available_date", "value"]].to_dict(
        "records"
    ) == [
        {
            "fiscal_end": "2020-09-30",
            "available_date": "2020-12-17",
            "value": -3_571_850_000,
        },
        {
            "fiscal_end": "2020-12-31",
            "available_date": "2021-04-29",
            "value": -2_777_979_000,
        },
    ]


def test_build_rejects_changed_source_operand(tmp_path, monkeypatch) -> None:
    _install_source_fixtures(
        monkeypatch,
        value_overrides={"q4_2020_total": -172_856},
    )
    with pytest.raises(RuntimeError, match="source value q4_2020_total changed"):
        build(tmp_path)


def test_build_rejects_intervening_filing_with_financial_table(
    tmp_path, monkeypatch
) -> None:
    _install_source_fixtures(monkeypatch)
    source_id = "6k_2021_01_25_cfo_ex991"
    raw = (
        _fixture_source_bytes(source_id)
        + b"<p>Unaudited Consolidated Statements of Comprehensive Loss</p>"
    )
    sources = deepcopy(uxin_ttm.SOURCE_DOCUMENTS)
    sources[source_id]["expected_sha256"] = hashlib.sha256(raw).hexdigest()
    original_download = uxin_ttm._download_source

    def fake_download(url: str) -> bytes:
        if url == sources[source_id]["url"]:
            return raw
        return original_download(url)

    monkeypatch.setattr(uxin_ttm, "SOURCE_DOCUMENTS", sources)
    monkeypatch.setattr(uxin_ttm, "_download_source", fake_download)
    with pytest.raises(RuntimeError, match="unexpectedly contains financial tables"):
        build(tmp_path)


def test_operands_are_only_total_net_loss_rows() -> None:
    assert set(OPERANDS_CNY_THOUSANDS) == {
        "fy2019_total",
        "q1_2019_total",
        "q1_2020_total",
        "h1_2019_total",
        "h1_2020_total",
        "q4_2019_total",
        "q4_2020_total",
    }
    assert all("attributable" not in item for item in OPERANDS_CNY_THOUSANDS)
