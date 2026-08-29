import pandas as pd
import pytest

from scripts import research_v16_post_confirmation_diagnostic as diagnostic


def test_reconstruct_pre_cost_returns_inverts_replay_cost_equation():
    daily = pd.DataFrame({
        "strategy": [0.0989, -0.002],
        "turnover": [1.0, 2.0],
    })

    result = diagnostic.reconstruct_pre_cost_returns(daily, cost_bps=10.0)

    assert result["pre_cost_strategy"].tolist() == pytest.approx([0.10, 0.0])
    assert result["daily_cost_return_drag"].tolist() == pytest.approx([
        0.0011,
        0.002,
    ])


def test_target_events_and_active_states_classify_core_transitions():
    dates = pd.to_datetime([
        "2025-03-03", "2025-04-01", "2025-05-02", "2025-06-02"
    ])
    targets = pd.DataFrame({
        "effective_date": dates,
        "ticker": ["A", "__CASH__", "__QQQ_CORE__", "B"],
        "target_weight": [1.0, 0.0, 1.0, 1.0],
        "base_transaction_cost_bps": [10.0] * 4,
    })

    events = diagnostic.target_event_types(targets)

    assert events.tolist() == [
        "initial_to_stocks",
        "stocks_to_cash",
        "cash_to_core",
        "core_to_stocks",
    ]
    sessions = pd.date_range("2025-04-01", "2025-06-02", freq="D")
    active = diagnostic.active_target_states(targets, sessions)
    assert active.loc["2025-04-30"] == "cash"
    assert active.loc["2025-05-30"] == "core"
    assert active.loc["2025-06-02"] == "stocks"


def test_pre_cost_paths_reconcile_across_cost_scenarios():
    dates = pd.to_datetime(["2025-01-02", "2025-01-03"])
    gross = pd.Series([0.10, -0.02], index=dates)
    turnover = pd.Series([1.0, 2.0], index=dates)
    scenarios = {}
    for cost in (10, 30, 50):
        factor = 1.0 - turnover * cost / 10_000.0
        scenarios[cost] = pd.DataFrame({
            "strategy": (1.0 + gross) * factor - 1.0,
            "turnover": turnover,
        }, index=dates)

    report = diagnostic.reconcile_pre_cost_paths(scenarios)

    assert report["all_paths_reconciled"] is True
    assert report["maximum_absolute_error"] < 1e-12


def test_cost_summary_attributes_only_emitted_event_dates():
    dates = pd.to_datetime(["2025-01-02", "2025-01-03", "2025-02-03"])
    daily = pd.DataFrame({
        "strategy": [-0.000999, 0.01, -0.001998],
        "benchmark": [0.0, 0.0, 0.0],
        "turnover": [1.0, 0.0, 2.0],
    }, index=dates)
    targets = pd.DataFrame({
        "effective_date": [dates[0], dates[2]],
        "ticker": ["A", "B"],
        "target_weight": [1.0, 1.0],
        "base_transaction_cost_bps": [10.0, 10.0],
    })

    summary, events = diagnostic.summarize_costs(daily, targets)

    assert summary["target_event_count"] == 2
    assert summary["turnover_sum"] == pytest.approx(3.0)
    assert events["event_count"].sum() == 2
    assert set(events["target_event_type"]) == {
        "initial_to_stocks",
        "stocks_to_stocks",
    }
