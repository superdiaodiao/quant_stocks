from copy import deepcopy
import hashlib

import pandas as pd
import pytest

import scripts.research_v14_iq_exact_ttm_loss as iq_ttm
from scripts.research_v14_iq_exact_ttm_loss import (
    AUDIT_OBSERVATIONS,
    OPERANDS_CNY_THOUSANDS,
    SOURCE_DOCUMENTS,
    SOURCE_PARSE_SPECS,
    build,
    direct_ttm_facts,
    exact_ttm_evidence,
    resolve_audit_observations,
)
from src.financial.quarterly_fundamentals import (
    quarterly_growth_snapshot,
    quarterly_profit_ttm_snapshot,
)


def _fixture_source_bytes(source_id: str) -> bytes:
    columns = SOURCE_PARSE_SPECS[source_id]["columns"]
    values = {
        operand["table_column"]: operand["value"]
        for operand in OPERANDS_CNY_THOUSANDS.values()
        if operand["source_id"] == source_id
    }
    cells = ["<td>Net loss attributable to iQIYI, Inc.</td>"]
    for column in columns:
        value = int(values.get(column, 0))
        rendered = f"({abs(value):,})" if value < 0 else f"{value:,}"
        cells.append(f"<td>{rendered}</td>")
    return ("<html><table><tr>" + "".join(cells) + "</tr></table></html>").encode()


def _install_fixtures(tmp_path, monkeypatch, *, tamper=None):
    sources = deepcopy(SOURCE_DOCUMENTS)
    for source_id, source in sources.items():
        raw = _fixture_source_bytes(source_id)
        source["expected_sha256"] = hashlib.sha256(raw).hexdigest()
        path = tmp_path / source["local_path"]
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(raw + (b"tampered" if source_id == tamper else b""))
    monkeypatch.setattr(iq_ttm, "SOURCE_DOCUMENTS", sources)


def test_exact_losses_use_issuer_attributable_rmb_only() -> None:
    evidence = {row["fiscal_end"]: row for row in exact_ttm_evidence()}

    assert evidence["2020-06-30"]["net_income_ttm"] == -10_498_367_000
    assert evidence["2020-12-31"]["net_income_ttm"] == -7_038_361_000
    assert all(row["currency"] == "CNY" for row in evidence.values())
    assert all("not per ADS" in row["profit_scope"] for row in evidence.values())
    facts = direct_ttm_facts(fetched_at="2026-08-23")
    assert set(facts["metric"]) == {"net_income_ttm"}


def test_all_four_observations_resolve_with_fresh_loss() -> None:
    assert len(AUDIT_OBSERVATIONS) == 4
    resolutions = resolve_audit_observations()

    assert resolutions["resolved"].all()
    assert set(resolutions["decision"]) == {"known_nonpositive_profit"}
    expected = {
        "2020-10-30": (77, -10_498_367_000),
        "2021-02-26": (7, -7_038_361_000),
    }
    for signal_date, (age, value) in expected.items():
        rows = resolutions.loc[resolutions["signal_date"].eq(signal_date)]
        assert len(rows) == 2
        assert set(rows["financial_age_days"]) == {age}
        assert set(rows["net_income_ttm"]) == {value}


def test_loss_is_pit_limited_and_cannot_create_growth() -> None:
    facts = direct_ttm_facts(fetched_at="2026-08-23")
    facts["fiscal_end"] = pd.to_datetime(facts["fiscal_end"])
    facts["available_date"] = pd.to_datetime(facts["available_date"])

    before_h1 = quarterly_profit_ttm_snapshot(
        facts, pd.Timestamp("2020-08-13"), 150
    )
    october = quarterly_profit_ttm_snapshot(
        facts, pd.Timestamp("2020-10-30"), 150
    )
    february = quarterly_profit_ttm_snapshot(
        facts, pd.Timestamp("2021-02-26"), 150
    )
    growth = quarterly_growth_snapshot(
        facts, pd.Timestamp("2021-02-26"), 550
    )

    assert before_h1.empty
    assert october.loc["IQ", "net_income_ttm"] == -10_498_367_000
    assert february.loc["IQ", "net_income_ttm"] == -7_038_361_000
    assert growth.empty


def test_build_parses_locked_source_rows(tmp_path, monkeypatch) -> None:
    _install_fixtures(tmp_path, monkeypatch)
    report = build(tmp_path)

    assert report["accepted_exact_ttm_loss_count"] == 2
    assert report["resolved_audit_observation_count"] == 4
    assert report["point_in_time_proven"] is True
    assert report["release_status"] == "BLOCKED"
    assert report["promotion_eligible"] is False
    assert len(report["source_value_verification"]) == 4


def test_source_hash_tamper_is_rejected(tmp_path, monkeypatch) -> None:
    _install_fixtures(
        tmp_path,
        monkeypatch,
        tamper="6k_2020_08_14_q2_full_submission",
    )
    with pytest.raises(RuntimeError, match="SHA-256 mismatch"):
        build(tmp_path)
