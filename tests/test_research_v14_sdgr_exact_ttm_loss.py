from copy import deepcopy
import hashlib

import pandas as pd
import pytest

import scripts.research_v14_sdgr_exact_ttm_loss as sdgr_ttm
from scripts.research_v14_sdgr_exact_ttm_loss import (
    AUDIT_OBSERVATIONS,
    OPERANDS_USD_THOUSANDS,
    REJECTED_LATER_FILINGS,
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


def _fixture_source_bytes(source_id: str) -> bytes:
    spec = SOURCE_PARSE_SPECS[source_id]
    values_by_column = {
        operand["table_column"]: operand["value"]
        for operand in OPERANDS_USD_THOUSANDS.values()
        if operand["source_id"] == source_id
    }
    cells = ["<td>Net loss</td>"]
    for column in spec["columns"]:
        value = int(values_by_column.get(column, 0))
        rendered = f"({abs(value):,})" if value < 0 else f"{value:,}"
        cells.append(f"<td>{rendered}</td>")
    return (
        "<html><table><tr><td>"
        + " | ".join(spec["context_phrases"])
        + "</td></tr><tr>"
        + "".join(cells)
        + "</tr></table></html>"
    ).encode()


def _install_source_fixtures(tmp_path, monkeypatch, *, tamper=None):
    sources = deepcopy(SOURCE_DOCUMENTS)
    for source_id, source in sources.items():
        raw = _fixture_source_bytes(source_id)
        source["expected_sha256"] = hashlib.sha256(raw).hexdigest()
        local_path = tmp_path / source["local_path"]
        local_path.parent.mkdir(parents=True, exist_ok=True)
        local_path.write_bytes(raw + (b"tampered" if source_id == tamper else b""))
    monkeypatch.setattr(sdgr_ttm, "SOURCE_DOCUMENTS", sources)
    return sources


def test_exact_ttm_uses_consistent_consolidated_loss() -> None:
    evidence = exact_ttm_evidence()

    assert evidence["net_income_ttm"] == -22_196_000
    assert evidence["formula"] == "FY2019 - 9M_2019 + 9M_2020"
    assert evidence["profit_scope"] == (
        "consolidated Net loss, including noncontrolling interest"
    )
    facts = direct_ttm_facts(fetched_at="2026-08-23")
    assert len(facts) == 1
    assert set(facts["metric"]) == {"net_income_ttm"}
    assert not set(facts["metric"]) & {
        "net_income", "revenue", "net_income_growth", "revenue_growth"
    }


def test_all_six_scenario_observations_resolve() -> None:
    assert len(AUDIT_OBSERVATIONS) == 6
    resolutions = resolve_audit_observations()

    assert resolutions["resolved"].all()
    assert set(resolutions["decision"]) == {"known_nonpositive_profit"}
    assert set(resolutions["signal_date"]) == {"2021-02-26"}
    assert set(resolutions["financial_age_days"]) == {106}
    assert set(resolutions["net_income_ttm"]) == {-22_196_000}


def test_direct_loss_is_pit_limited_and_cannot_create_growth() -> None:
    facts = direct_ttm_facts(fetched_at="2026-08-23")
    facts["fiscal_end"] = pd.to_datetime(facts["fiscal_end"])
    facts["available_date"] = pd.to_datetime(facts["available_date"])

    before = quarterly_profit_ttm_snapshot(
        facts, pd.Timestamp("2020-11-11"), 550
    )
    at_filing = quarterly_profit_ttm_snapshot(
        facts, pd.Timestamp("2020-11-12"), 150
    )
    at_signal = quarterly_profit_ttm_snapshot(
        facts, pd.Timestamp("2021-02-26"), 150
    )
    growth = quarterly_growth_snapshot(
        facts, pd.Timestamp("2021-02-26"), 550
    )

    assert before.empty
    assert at_filing.loc["SDGR", "net_income_ttm"] == -22_196_000
    assert at_signal.loc["SDGR", "net_income_ttm"] == -22_196_000
    assert growth.empty


def test_source_lock_and_real_table_parser_accept_exact_operands(
    tmp_path, monkeypatch
) -> None:
    _install_source_fixtures(tmp_path, monkeypatch)
    report = build(tmp_path)

    assert report["accepted_exact_ttm_loss_count"] == 1
    assert report["resolved_audit_observation_count"] == 6
    assert report["point_in_time_proven"] is True
    assert report["release_status"] == "BLOCKED"
    assert report["promotion_eligible"] is False
    assert len(report["source_value_verification"]) == 3


def test_source_hash_tamper_is_rejected(tmp_path, monkeypatch) -> None:
    _install_source_fixtures(
        tmp_path, monkeypatch, tamper="10q_2020_11_12_m9"
    )
    with pytest.raises(RuntimeError, match="SHA-256 mismatch"):
        build(tmp_path)


def test_post_signal_10k_is_explicitly_rejected() -> None:
    validate_source_lock()
    assert REJECTED_LATER_FILINGS["0001564590-21-010075"]["filed"] == (
        "2021-03-04"
    )
