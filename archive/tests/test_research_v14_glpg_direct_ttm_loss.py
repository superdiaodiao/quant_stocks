from __future__ import annotations

from copy import deepcopy
from decimal import Decimal
import hashlib
import json

import pandas as pd
import pytest

import scripts.research_v14_glpg_direct_ttm_loss as glpg
from src.financial.quarterly_fundamentals import (
    quarterly_growth_snapshot,
    quarterly_profit_ttm_snapshot,
)


def _xbrl_fixture() -> bytes:
    return b"""<?xml version="1.0" encoding="UTF-8"?>
<xbrl xmlns:ifrs-full="urn:ifrs">
  <ifrs-full:RevenueAndOperatingIncome contextRef="Duration_1_1_2017_To_12_31_2017">155918000</ifrs-full:RevenueAndOperatingIncome>
  <ifrs-full:ProfitLoss contextRef="Duration_1_1_2017_To_12_31_2017">-115704000</ifrs-full:ProfitLoss>
  <ifrs-full:RevenueAndOperatingIncome contextRef="Duration_1_1_2018_To_12_31_2018">317845000</ifrs-full:RevenueAndOperatingIncome>
  <ifrs-full:ProfitLoss contextRef="Duration_1_1_2018_To_12_31_2018">-29259000</ifrs-full:ProfitLoss>
</xbrl>"""


def _h1_fixture() -> bytes:
    return b"""<?xml version="1.0" encoding="UTF-8"?>
<xbrl xmlns:ifrs-full="urn:ifrs">
  <ifrs-full:ProfitLoss contextRef="Duration_1_1_2018_To_6_30_2018">-59056000</ifrs-full:ProfitLoss>
  <ifrs-full:ProfitLoss contextRef="Duration_1_1_2019_To_6_30_2019">-95905000</ifrs-full:ProfitLoss>
</xbrl>"""


def _q3_fixture(
    revenue_current: str,
    revenue_prior: str,
    income_current: str,
    income_prior: str,
) -> bytes:
    def cell(value: str) -> str:
        if value.startswith("-"):
            return f"<td>({value[1:]}</td><td>)</td>"
        return f"<td>{value}</td><td></td>"

    return (
        "<html><body><table>"
        "<tr><td>Revenues</td><td></td>"
        f"{cell(revenue_current)}<td></td>{cell(revenue_prior)}</tr>"
        "<tr><td>Net result for the period</td><td></td>"
        f"{cell(income_current)}<td></td>{cell(income_prior)}</tr>"
        "</table></body></html>"
    ).encode()


def _raw_sources() -> dict[str, bytes]:
    return {
        "fy2018_xbrl": _xbrl_fixture(),
        "h1_2019_xbrl": _h1_fixture(),
        "q3_2018_exhibit": _q3_fixture("205.1", "106.4", "-44.2", "-85.9"),
        "q3_2019_exhibit": _q3_fixture("752.5", "205.1", "265.3", "-44.2"),
    }


def _fixture_documents(raw_sources: dict[str, bytes]) -> dict[str, dict]:
    documents = deepcopy(glpg.SOURCE_DOCUMENTS)
    for source_id, source in documents.items():
        source["url"] = (
            "https://sec.test/"
            f"{source['accession'].replace('-', '')}/{source['document']}"
        )
        source["expected_sha256"] = hashlib.sha256(
            raw_sources[source_id]
        ).hexdigest()
    return documents


def test_source_lock_binds_four_original_sec_documents() -> None:
    glpg.validate_source_lock()
    assert {
        source["expected_sha256"]
        for source in glpg.SOURCE_DOCUMENTS.values()
    } == {
        "811680469fc0e5349d92966f62de54cc7d723650243829a2e4913b7810dae8c4",
        "3175de74d5a202740357ee909d506df15159159945f01b64b7dd9d0a116768c3",
        "0e77afd3d23dcaaa5d819b3d92c70bc434be532c7c859d7d47ea90faaced41f5",
        "7bf43547c572c5f08cd623442b4092e4e08224fbe017c0f8bae65373ac72f4d0",
    }
    assert glpg.SOURCE_DOCUMENTS["q3_2019_exhibit"]["filed"] == "2019-10-25"


def test_exact_ttm_math_uses_comparable_ifrs_eur_operands() -> None:
    evidence = glpg.verify_source_evidence(_raw_sources())
    derived = glpg._derive_values(evidence)
    assert derived["direct_loss"] == glpg.EXPECTED_TTM
    assert derived["q3_growth"] == glpg.EXPECTED_Q3_GROWTH
    assert derived["q3_growth"]["revenue"] == {
        "prior_ttm": Decimal(254_618_000),
        "current_ttm": Decimal(865_245_000),
        "growth": Decimal(610_627_000) / Decimal(254_618_000),
    }
    assert derived["q3_growth"]["net_income"] == {
        "prior_ttm": Decimal(-74_004_000),
        "current_ttm": Decimal(280_241_000),
        "growth": Decimal(354_245_000) / Decimal(74_004_000),
    }


def test_q3_tables_require_exact_reported_comparatives() -> None:
    evidence = glpg.verify_source_evidence(_raw_sources())
    cumulative = evidence["q3_cumulative_eur"]
    assert cumulative["revenue"] == {
        "m9_2017": Decimal(106_400_000),
        "m9_2018": Decimal(205_100_000),
        "m9_2019": Decimal(752_500_000),
    }
    assert cumulative["net_income"] == {
        "m9_2017": Decimal(-85_900_000),
        "m9_2018": Decimal(-44_200_000),
        "m9_2019": Decimal(265_300_000),
    }


def test_facts_preserve_losses_and_add_one_complete_growth_bundle() -> None:
    facts, _ = glpg.strict_quarterly_facts(_raw_sources())
    assert len(facts) == 6
    assert set(facts.loc[facts["fiscal_end"].eq("2019-09-30"), "metric"]) == {
        "net_income_ttm",
        "net_income_growth",
        "revenue_ttm",
        "revenue_growth",
    }
    loss = facts.loc[
        facts["fiscal_end"].eq("2019-06-30")
        & facts["metric"].eq("net_income_ttm"),
        "value",
    ].item()
    assert loss == -66_108_000.0


def test_growth_bundle_resolves_both_age150_signal_dates() -> None:
    facts, _ = glpg.strict_quarterly_facts(_raw_sources())
    facts["fiscal_end"] = pd.to_datetime(facts["fiscal_end"])
    facts["available_date"] = pd.to_datetime(facts["available_date"])
    for signal_date, expected_age in (("2019-12-31", 67), ("2020-01-31", 98)):
        snapshot = quarterly_growth_snapshot(
            facts, pd.Timestamp(signal_date), maximum_age_days=150
        )
        assert snapshot.loc["GLPG", "financial_age_days"] == expected_age
        assert snapshot.loc["GLPG", "revenue_growth"] == pytest.approx(
            2.398208296349826
        )
        assert snapshot.loc["GLPG", "net_income_growth"] == pytest.approx(
            4.786835846710989
        )


def test_direct_half_year_loss_remains_available_without_inventing_growth() -> None:
    facts, _ = glpg.strict_quarterly_facts(_raw_sources())
    facts["fiscal_end"] = pd.to_datetime(facts["fiscal_end"])
    facts["available_date"] = pd.to_datetime(facts["available_date"])
    snapshot = quarterly_profit_ttm_snapshot(
        facts, pd.Timestamp("2019-09-30"), maximum_age_days=150
    )
    assert snapshot.loc["GLPG", "net_income_ttm"] == -66_108_000.0


def test_four_audit_observations_are_resolved_as_growth_passes() -> None:
    _, evidence = glpg.strict_quarterly_facts(_raw_sources())
    rows = glpg.resolved_audit_observations(evidence)
    assert len(rows) == 4
    assert {row["scenario"] for row in rows} == {
        "liq10000000-age150-growth",
        "liq2000000-age150-growth",
    }
    assert {row["financial_age_days"] for row in rows} == {67, 98}
    assert all(row["resolved"] for row in rows)
    assert {row["decision"] for row in rows} == {"pass_growth_filters"}


def test_build_downloads_sources_and_rejects_sha_drift(
    tmp_path, monkeypatch
) -> None:
    raw_sources = _raw_sources()
    documents = _fixture_documents(raw_sources)
    raw_by_url = {
        documents[source_id]["url"]: raw
        for source_id, raw in raw_sources.items()
    }
    monkeypatch.setattr(glpg, "SOURCE_DOCUMENTS", documents)
    monkeypatch.setattr(glpg, "_download_source", raw_by_url.__getitem__)
    audit_path = tmp_path / "audit.json"
    audit_path.write_text(json.dumps({"release_status": "BLOCKED"}) + "\n")

    report = glpg.build(tmp_path / "package", audit_path)
    assert report["accepted_fact_count"] == 6
    assert report["resolved_audit_observation_count"] == 4
    assert report["formal_financials_modified"] is False
    assert report["release_status"] == "BLOCKED"
    assert all(
        (tmp_path / "package" / source["local_path"]).exists()
        for source in documents.values()
    )

    drifted = deepcopy(documents)
    drifted["q3_2019_exhibit"]["expected_sha256"] = "0" * 64
    monkeypatch.setattr(glpg, "SOURCE_DOCUMENTS", drifted)
    with pytest.raises(RuntimeError, match="SHA-256 mismatch"):
        glpg.prepare_verified_sources(tmp_path / "drifted")
