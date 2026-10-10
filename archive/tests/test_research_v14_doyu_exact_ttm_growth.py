import pandas as pd
import pytest

from src.financial.quarterly_fundamentals import quarterly_growth_snapshot
from scripts.research_v14_doyu_exact_ttm_growth import (
    EXPECTED_QUARTERS,
    EXPECTED_TTM,
    QUARTER_SOURCE,
    SCENARIOS,
    SIGNALS,
    SOURCES,
    _ownership_row,
    _statement_rows,
    audit_signals,
    resolved_observations,
    validate_audit_binding,
    validate_snapshots,
)


def _source() -> bytes:
    return b"""
    <html><body>
      <p>DouYu International Holdings Limited</p>
      <p>U.S. GAAP; RMB and US$ columns</p>
      <table>
        <tr><td>Net Revenues</td><td>1,000</td><td>2,000</td></tr>
        <tr><td>Net income (loss)</td><td>(300</td><td>400</td></tr>
        <tr><td>Net income (loss) attributable to ordinary shareholders of the Company</td><td>(250</td><td>450</td></tr>
        <tr><td>Adjusted net income (loss)</td><td>9,999</td><td>9,999</td></tr>
      </table>
    </body></html>
    """


def test_parser_selects_consolidated_rmb_gaap_not_adjusted_or_attributable() -> None:
    assert _statement_rows(_source(), 2, 1_000) == (
        (1_000_000.0, 2_000_000.0),
        (-300_000.0, 400_000.0),
    )
    assert _ownership_row(_source(), (-250.0, 450.0)) == (-250.0, 450.0)
    with pytest.raises(RuntimeError, match=r"RMB/US\$ column markers"):
        _statement_rows(_source().replace(b"RMB", b"EUR"), 2, 1_000)


def _snapshots():
    return {
        source_id: (spec["expected_revenue"], spec["expected_net_income"])
        for source_id, spec in SOURCES.items()
        if source_id != "2019_20f"
    }


def _annual():
    spec = SOURCES["2019_20f"]
    return (spec["expected_revenue"], spec["expected_net_income"])


def test_source_validation_enforces_quarter_cumulative_and_ownership_semantics() -> None:
    annual_spec = SOURCES["2019_20f"]
    assert validate_snapshots(
        _snapshots(), _annual(), annual_spec["expected_attributable"]
    ) == EXPECTED_QUARTERS

    changed = _snapshots()
    revenue, net_income = changed["2020q2"]
    changed["2020q2"] = (
        (revenue[0] + 1_000.0,) + revenue[1:],
        net_income,
    )
    with pytest.raises(RuntimeError, match="source values changed"):
        validate_snapshots(
            changed, _annual(), annual_spec["expected_attributable"]
        )


def test_bound_audit_has_six_scenarios_and_eighteen_observations() -> None:
    binding = validate_audit_binding()
    assert binding["exact_signals"] == list(SIGNALS)
    assert binding["scenario_count"] == 6
    assert binding["aggregate_missing_observation_count"] == 18
    assert binding["technical_replay_control"]["candidate_by_signal"][
        "2020-09-30"
    ] is False


def test_signal_audit_uses_only_filed_quarters_and_resolves_all_observations() -> None:
    audits = audit_signals(EXPECTED_QUARTERS)
    assert [row["signal_date"] for row in audits] == list(SIGNALS)
    assert sum(row["missing_observation_count"] for row in audits) == 18
    assert len(resolved_observations(audits)) == 18
    for row in audits:
        assert row["affected_scenarios"] == list(SCENARIOS)
        assert row["financial_age_days"] <= 150
        assert row["current_ttm"]["net_income"] > 0
        assert row["growth"]["net_income"] >= 0.25
        assert row["growth"]["revenue"] >= 0.10
        assert row["deterministic_result"] == (
            "PASS_EXACT_TTM_GROWTH_AND_POSITIVE_NET_INCOME"
        )


def test_exact_ttm_math_separates_pre_and_post_q2_signals() -> None:
    audits = {row["signal_date"]: row for row in audit_signals(EXPECTED_QUARTERS)}
    for signal_date, expected in EXPECTED_TTM.items():
        assert audits[signal_date]["previous_ttm"] == expected["previous"]
        assert audits[signal_date]["current_ttm"] == expected["current"]
        assert audits[signal_date]["growth"]["revenue"] == pytest.approx(
            expected["growth"]["revenue"]
        )
        assert audits[signal_date]["growth"]["net_income"] == pytest.approx(
            expected["growth"]["net_income"]
        )
    assert audits["2020-07-31"]["last_available_financial_filing"]["filed"] == (
        "2020-05-26"
    )
    assert audits["2020-07-31"]["financial_age_days"] == 66
    for signal_date in ("2020-08-31", "2020-10-30"):
        assert audits[signal_date]["last_available_financial_filing"]["filed"] == (
            "2020-08-10"
        )
    assert audits["2020-08-31"]["financial_age_days"] == 21
    assert audits["2020-10-30"]["financial_age_days"] == 81


def test_shared_growth_snapshot_consumes_the_standalone_facts_without_fx() -> None:
    rows = []
    for fiscal_end, (revenue, net_income) in EXPECTED_QUARTERS.items():
        source_id = QUARTER_SOURCE[fiscal_end][0]
        available_date = SOURCES[source_id]["filed"]
        rows.extend(
            [
                {
                    "ticker": "DOYU",
                    "fiscal_end": fiscal_end,
                    "available_date": available_date,
                    "metric": "revenue",
                    "value": revenue,
                },
                {
                    "ticker": "DOYU",
                    "fiscal_end": fiscal_end,
                    "available_date": available_date,
                    "metric": "net_income",
                    "value": net_income,
                },
            ]
        )
    facts = pd.DataFrame(rows)
    facts[["fiscal_end", "available_date"]] = facts[
        ["fiscal_end", "available_date"]
    ].apply(pd.to_datetime)
    for signal_date in SIGNALS:
        snapshot = quarterly_growth_snapshot(facts, pd.Timestamp(signal_date), 150)
        assert list(snapshot.index) == ["DOYU"]
        actual = snapshot.loc["DOYU"]
        expected = EXPECTED_TTM[signal_date]
        assert actual["revenue_ttm"] == expected["current"]["revenue"]
        assert actual["net_income_ttm"] == expected["current"]["net_income"]
        assert actual["revenue_growth"] == pytest.approx(
            expected["growth"]["revenue"]
        )
        assert actual["net_income_growth"] == pytest.approx(
            expected["growth"]["net_income"]
        )
