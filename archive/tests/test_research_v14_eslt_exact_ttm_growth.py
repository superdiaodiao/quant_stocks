from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
import hashlib

import pandas as pd
import pytest

import scripts.research_v14_eslt_exact_ttm_growth as eslt_growth
from scripts.research_v14_eslt_exact_ttm_growth import (
    ACCOUNTING_STANDARD,
    AUDIT_OBSERVATIONS,
    AVAILABLE_DATE,
    BASELINE_BINDING,
    FISCAL_END,
    OPERANDS_USD_THOUSANDS,
    POST_SIGNAL_EXCLUSIONS,
    SOURCE_DOCUMENTS,
    SOURCE_ROW_CHECKS,
    SOURCE_TEXT_CHECKS,
    build,
    exact_ttm_evidence,
    resolve_audit_observations,
    strict_quarterly_facts,
    validate_exact_package,
    validate_source_lock,
)
from src.financial.quarterly_fundamentals import quarterly_growth_snapshot
from src.research import can_slim_validation


def _fixture_source_bytes(
    source_id: str,
    value_overrides: dict[tuple[str, str], tuple[int, ...]] | None = None,
) -> bytes:
    overrides = value_overrides or {}
    paragraphs = "".join(
        f"<p>{phrase}</p>" for phrase in SOURCE_TEXT_CHECKS[source_id]
    )
    tables = []
    for check in SOURCE_ROW_CHECKS[source_id]:
        values = overrides.get(
            (source_id, check["metric"]), tuple(check["expected_values"])
        )
        cells = [f"<td>{check['line_item']}</td>"]
        for value in values:
            rendered = f"({abs(value):,})" if value < 0 else f"{value:,}"
            cells.extend(("<td>$</td>", f"<td>{rendered}</td>", "<td></td>"))
        tables.append(
            "<table><tr><td>"
            + " | ".join(check["context_phrases"])
            + "</td></tr><tr>"
            + "".join(cells)
            + "</tr></table>"
        )
    return ("<html>" + paragraphs + "".join(tables) + "</html>").encode()


def _install_source_fixtures(
    monkeypatch,
    *,
    value_overrides: dict[tuple[str, str], tuple[int, ...]] | None = None,
) -> None:
    sources = deepcopy(SOURCE_DOCUMENTS)
    downloads = {}
    for source_id, source in sources.items():
        raw = _fixture_source_bytes(source_id, value_overrides)
        source["expected_sha256"] = hashlib.sha256(raw).hexdigest()
        downloads[source["url"]] = raw

    def fake_download(url: str) -> bytes:
        return downloads[url]

    monkeypatch.setattr(eslt_growth, "SOURCE_DOCUMENTS", sources)
    monkeypatch.setattr(eslt_growth, "_download_source", fake_download)


def _snapshot_facts() -> pd.DataFrame:
    facts = strict_quarterly_facts().copy()
    facts["fiscal_end"] = pd.to_datetime(facts["fiscal_end"])
    facts["available_date"] = pd.to_datetime(facts["available_date"])
    return facts


def test_exact_ttm_arithmetic_and_growth() -> None:
    evidence = exact_ttm_evidence()
    revenue = evidence["derived"]["revenue"]
    profit = evidence["derived"]["net_income"]

    assert revenue["prior_ttm_usd_thousands"] == 3_615_448
    assert revenue["current_ttm_usd_thousands"] == 4_264_734
    assert revenue["growth"] == pytest.approx(649_286 / 3_615_448)
    assert profit["prior_ttm_usd_thousands"] == 275_026
    assert profit["current_ttm_usd_thousands"] == 177_468
    assert profit["growth"] == pytest.approx(-97_558 / 275_026)
    assert evidence["currency"] == "USD"
    assert evidence["accounting_standard"] == ACCOUNTING_STANDARD
    assert "attributable to Elbit Systems" in evidence["metric_mapping"]["net_income"]
    validate_exact_package()


def test_cumulative_operands_are_exact_and_do_not_make_up_q4() -> None:
    revenue = OPERANDS_USD_THOUSANDS["revenue"]
    profit = OPERANDS_USD_THOUSANDS["net_income"]

    assert revenue["fy2017"] - revenue["m9_2017"] == 1_009_604
    assert revenue["fy2018"] - revenue["m9_2018"] == 1_077_840
    assert profit["fy2017"] - profit["m9_2017"] == 69_413
    assert profit["fy2018"] - profit["m9_2018"] == 1_125
    evidence = exact_ttm_evidence()
    assert evidence["derived"]["revenue"]["prior_formula"] == (
        "FY2017 - M9_2017 + M9_2018"
    )
    assert evidence["derived"]["revenue"]["current_formula"] == (
        "FY2018 - M9_2018 + M9_2019"
    )


def test_strict_facts_are_one_complete_direct_growth_package() -> None:
    facts = strict_quarterly_facts()
    assert len(facts) == 4
    assert set(facts["metric"]) == {
        "net_income_ttm",
        "net_income_growth",
        "revenue_ttm",
        "revenue_growth",
    }
    values = facts.set_index("metric")["value"]
    assert values["revenue_ttm"] == 4_264_734_000
    assert values["net_income_ttm"] == 177_468_000
    assert values["revenue_growth"] == pytest.approx(649_286 / 3_615_448)
    assert values["net_income_growth"] == pytest.approx(-97_558 / 275_026)
    assert set(facts["fiscal_end"]) == {FISCAL_END}
    assert set(facts["available_date"]) == {AVAILABLE_DATE}
    assert set(facts["taxonomy"]) == {"us-gaap"}


def test_package_is_not_visible_before_filing_and_resolves_all_age_limits() -> None:
    facts = _snapshot_facts()
    before = quarterly_growth_snapshot(
        facts, pd.Timestamp("2019-11-25"), maximum_age_days=550
    )
    at_filing = quarterly_growth_snapshot(
        facts, pd.Timestamp("2019-11-26"), maximum_age_days=150
    )
    assert "ESLT" not in before.index
    assert at_filing.loc["ESLT", "financial_age_days"] == 0
    for age in (150, 365, 550):
        at_signal = quarterly_growth_snapshot(
            facts, pd.Timestamp("2020-02-28"), maximum_age_days=age
        )
        assert at_signal.loc["ESLT", "fiscal_end"] == pd.Timestamp(FISCAL_END)
        assert at_signal.loc["ESLT", "financial_age_days"] == 94
        assert at_signal.loc["ESLT", "revenue_growth"] > 0
        assert at_signal.loc["ESLT", "net_income_growth"] < 0


def test_three_real_audit_observations_resolve_at_age_94() -> None:
    assert AUDIT_OBSERVATIONS == (
        ("liq2000000-age150-growth", "2020-02-28", 150),
        ("liq2000000-age365-growth", "2020-02-28", 365),
        ("liq2000000-age550-growth", "2020-02-28", 550),
    )
    resolution = resolve_audit_observations()
    assert resolution["resolved"].all()
    assert set(resolution["financial_age_days"]) == {94}
    assert set(resolution["decision"]) == {
        "complete_exact_as_reported_ttm_growth_bundle"
    }


def test_real_coverage_accepts_the_direct_bundle(monkeypatch) -> None:
    facts = _snapshot_facts()
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
            {"nonfinancial_candidate": [True]}, index=["ESLT"]
        ),
    )
    monkeypatch.setattr(
        can_slim_validation,
        "can_slim_nonfinancial_candidate_mask",
        lambda technical, _config: pd.Series(True, index=technical.index),
    )

    as_of = pd.Timestamp("2020-02-28")
    close = pd.DataFrame({"ESLT": [20.0]}, index=[as_of])
    for age in (150, 365, 550):
        config = replace(
            can_slim_validation.fixed_top3_config(),
            end="2020-02-28",
            maximum_financial_age_days=age,
        )
        report = can_slim_validation.technical_candidate_financial_coverage(
            close,
            pd.DataFrame({"ESLT": [20_000_000.0]}, index=[as_of]),
            pd.Series([100.0], index=[as_of]),
            facts,
            {as_of: {"ESLT"}},
            config,
            start="2020-02-28",
            adjusted_close=close,
        )
        row = report["by_signal"][0]
        assert report["missing_financial_observations"] == 0
        assert report["complete"] is True
        assert row["usable_financial_count"] == 1
        assert row["known_nonpositive_profit_count"] == 0


def test_republication_checks_isolate_pre_signal_revisions() -> None:
    evidence = exact_ttm_evidence()
    assert "FY2017 values" in evidence["restatement_isolation"]
    assert "FY2018 and 9M 2018" in evidence["restatement_isolation"]
    assert "No amendment or post-signal" in evidence["restatement_isolation"]


def test_accounting_policy_discontinuities_are_explicit_and_fail_closed() -> None:
    audit = exact_ttm_evidence()["accounting_policy_comparability"]
    assert audit["status"] == "EXACT_AS_REPORTED_US_GAAP_NOT_CONSTANT_POLICY_BASIS"
    assert "modified retrospective" in audit["asc_606"]
    assert "ASC 605" in audit["asc_606"]
    assert "comparatives were not restated" in audit["asc_842"]
    assert "$21.1m" in audit["asc_842"]
    assert "not promotion eligible" in audit["use_boundary"]


def test_post_signal_2019_annual_results_are_excluded() -> None:
    assert len(POST_SIGNAL_EXCLUSIONS) == 2
    assert all(item["filed"] == "2020-03-25" for item in POST_SIGNAL_EXCLUSIONS)
    assert all(item["filed"] > "2020-02-28" for item in POST_SIGNAL_EXCLUSIONS)
    assert {item["form"] for item in POST_SIGNAL_EXCLUSIONS} == {"6-K", "20-F"}


def test_official_source_dates_accessions_and_hashes_are_locked() -> None:
    assert {
        source_id: (source["filed"], source["accession"], source["expected_sha256"])
        for source_id, source in SOURCE_DOCUMENTS.items()
    } == {
        "6k_2018_11_20_q3": (
            "2018-11-20", "0001628280-18-014522",
            "2d9e0fae08efdb5b2cd57ad25628ab50612877e0f972cc36e710884a6103acee",
        ),
        "20f_2019_03_19_fy2018": (
            "2019-03-19", "0001628280-19-003104",
            "14759570f2ebb7211525c32cf11c85ba0d94dd7608b814d519543dcec138da6f",
        ),
        "6k_2019_11_26_q3": (
            "2019-11-26", "0001628280-19-014525",
            "1cf8d078d238642e911b075b1ee6a2049f8d9661431be2561cb8a65193872b9c",
        ),
    }
    validate_source_lock()


def test_source_lock_rejects_mixed_currency_or_post_signal_date() -> None:
    mixed = {name: dict(source) for name, source in SOURCE_DOCUMENTS.items()}
    mixed["6k_2019_11_26_q3"]["currency"] = "ILS"
    with pytest.raises(ValueError, match="mixed currency or scale"):
        validate_source_lock(mixed)

    late = {name: dict(source) for name, source in SOURCE_DOCUMENTS.items()}
    late["6k_2019_11_26_q3"]["filed"] = "2020-03-01"
    with pytest.raises(ValueError, match="violates PIT cutoff"):
        validate_source_lock(late)


def test_baseline_is_bound_to_three_observation_aggregate() -> None:
    assert BASELINE_BINDING["quarterly_sha256"] == (
        "0f6a6be2a22ea64c31203805061bb0408ef636b2eebc888178754a9e963d3c3d"
    )
    assert BASELINE_BINDING["audit_sha256"] == (
        "5a73b0278d0a3b081169ca9e93546af77d1c8a783218ecef2a6cd6cdb891e317"
    )
    assert BASELINE_BINDING["financial_priorities_sha256"] == (
        "5e032914a243bc4628af966b85400b2bb8d450925824bce9aaf1d9589362a815"
    )


def test_build_verifies_sources_and_writes_isolated_outputs(
    tmp_path, monkeypatch
) -> None:
    _install_source_fixtures(monkeypatch)
    report = build(tmp_path)
    facts = pd.read_csv(tmp_path / "strict_quarterly_facts.csv")

    assert report["baseline_binding"] == BASELINE_BINDING
    assert report["accepted_direct_growth_package_count"] == 1
    assert report["accepted_fact_count"] == 4
    assert report["resolved_audit_observation_count"] == 3
    assert report["accounting_policy_normalized"] is False
    assert report["promotion_eligible"] is False
    assert report["shared_candidate_integrated"] is False
    assert report["formal_financials_modified"] is False
    assert len(report["source_value_verification"]) == 6
    assert len(facts) == 4


def test_build_rejects_operand_drift(tmp_path, monkeypatch) -> None:
    source_id = "6k_2019_11_26_q3"
    _install_source_fixtures(
        monkeypatch,
        value_overrides={
            (source_id, "revenue"): (
                3_186_893, 2_605_844, 1_101_190, 895_150, 3_683_684
            )
        },
    )
    with pytest.raises(RuntimeError, match="source row changed"):
        build(tmp_path)
