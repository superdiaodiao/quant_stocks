import pandas as pd
import pytest

from scripts import research_v22_2026_observation as observation
from scripts import research_v22_2026_calendar_adjudication as adjudication


def _protocol():
    return {
        "observation_window": {
            "start": "2026-01-01",
            "end": "2026-07-31",
            "required_months": [f"2026-{month:02d}" for month in range(1, 8)],
        },
        "exposure_tiers": [
            {
                "start": "2026-01-01",
                "end": "2026-07-17",
                "classification": "MODEL_EXCLUDED_RESEARCHER_EXPOSED",
            },
            {
                "start": "2026-07-18",
                "end": "2026-07-31",
                "classification": (
                    "MODEL_EXCLUDED_PREEXISTING_PERFORMANCE_UNINSPECTED_AT_FREEZE"
                ),
            },
        ],
    }


def _results(strategy_return=0.01, benchmark_return=0.005):
    dates = pd.to_datetime(
        [f"2026-{month:02d}-15" for month in range(1, 8)]
    )
    frame = pd.DataFrame({
        "strategy": [strategy_return] * len(dates),
        "benchmark": [benchmark_return] * len(dates),
        "qqq": [benchmark_return] * len(dates),
    }, index=dates)
    return {cost: frame.copy() for cost in (10, 30, 50)}, dates


def _decisions():
    return pd.DataFrame({
        "date": pd.to_datetime(
            [f"2026-{month:02d}-01" for month in range(1, 8)]
        ),
        "stock_weight": [0.1] * 7,
    })


def test_precommitted_evaluation_passes_positive_excess_at_gate_costs():
    results, dates = _results()

    report = observation.evaluate_observation(
        results, _decisions(), _protocol(), dates
    )

    assert report["all_precommitted_gates_passed"] is True
    assert report["data_gates"]["completed_overlay_decision_months"] == 7
    assert report["costs"]["30"]["positive_excess_gate"] is True
    assert report["costs"]["50"]["drawdown_gate"] is True


def test_precommitted_evaluation_blocks_nonpositive_excess():
    results, dates = _results(strategy_return=0.004, benchmark_return=0.005)

    report = observation.evaluate_observation(
        results, _decisions(), _protocol(), dates
    )

    assert report["all_precommitted_gates_passed"] is False
    assert report["costs"]["30"]["positive_excess_gate"] is False
    assert report["costs"]["50"]["all_cost_gates_passed"] is False


def test_precommitted_evaluation_requires_every_decision_month():
    results, dates = _results()

    report = observation.evaluate_observation(
        results, _decisions().iloc[:-1], _protocol(), dates
    )

    assert report["all_precommitted_gates_passed"] is False
    assert report["data_gates"]["decision_month_gate"] is False


def test_freeze_protocol_does_not_calculate_performance(tmp_path):
    path = tmp_path / "frozen_protocol.json"

    report = observation.freeze_protocol(path)

    assert report["status"] == "FROZEN_NOT_EXECUTED"
    assert report["observation_window"][
        "observed_performance_calculated_during_freeze"
    ] is False
    assert report["acceptance_gates"]["gate_cost_bps"] == [30, 50]
    assert report["precommitted_decision_policy"]["if_all_gates_pass"][
        "additional_performance_observation_months_required"
    ] == 0
    assert report["precommitted_decision_policy"]["if_all_gates_pass"][
        "minimum_future_operational_dry_run_cycles_required"
    ] == 1
    assert report["release_status"] == "BLOCKED"
    assert report["brokerage_or_trading_authorized"] is False

    with pytest.raises(RuntimeError, match="will not be overwritten"):
        observation.freeze_protocol(path)


def test_calendar_adjudication_accepts_only_neutral_extra_rows():
    results, dates = _results()
    frame = results[50].copy()
    extra = pd.DataFrame({
        "strategy": [0.0],
        "benchmark": [0.0],
        "qqq": [0.0],
        "turnover": [0.0],
        "transaction_cost": [0.0],
    }, index=pd.to_datetime(["2026-06-19"]))
    frame["turnover"] = 0.0
    frame["transaction_cost"] = 0.0
    frame = pd.concat([frame, extra]).sort_index()

    normalized, check = adjudication._calendar_check(frame, dates)

    assert normalized.index.equals(dates)
    assert check["missing_expected_dates"] == []
    assert check["extra_non_session_dates"] == ["2026-06-19"]
    assert check["extra_rows_are_economically_neutral"] is True


def test_calendar_adjudication_rejects_active_extra_rows():
    results, dates = _results()
    frame = results[50].copy()
    frame["turnover"] = 0.0
    frame["transaction_cost"] = 0.0
    frame.loc[pd.Timestamp("2026-06-19")] = {
        "strategy": 0.001,
        "benchmark": 0.0,
        "qqq": 0.0,
        "turnover": 0.0,
        "transaction_cost": 0.0,
    }
    frame = frame.sort_index()

    _, check = adjudication._calendar_check(frame, dates)

    assert check["extra_rows_are_economically_neutral"] is False
