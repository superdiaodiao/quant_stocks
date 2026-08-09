import pytest
import pandas as pd

from src.research import historical_data_audit


def test_incomplete_data_preflight_refuses_backtest(monkeypatch):
    monkeypatch.setattr(historical_data_audit, "backtest_data_readiness", lambda *_: {
        "complete": False,
        "checks": {
            "point_in_time_membership_complete": False,
            "snapshot_member_prices_complete": False,
            "observed_delisting_returns_complete": False,
        },
        "snapshot_price_coverage": {"missing_price_symbols": ["A", "B"]},
        "research_common_equity_histories_ending_early": 3,
    })
    with pytest.raises(RuntimeError, match="refusing to produce a validation result"):
        historical_data_audit.require_complete_backtest_data("2021-01-01", "2026-07-17")


def test_readiness_rejects_price_history_ending_while_still_listed(
    monkeypatch,
):
    history = {
        "universe_snapshot_coverage": {"full_period_covered": True},
        "signal_price_coverage": {
            "signal_membership_snapshots_complete": True,
            "complete": True,
            "usable_pit_financial_growth_complete": True,
        },
        "temporal_security_type_filter": {},
        "listed_price_histories_complete": False,
        "delisting_returns_complete": True,
        "research_common_equity_histories_ending_early": 1,
        "missing_price_histories_while_still_listed": 1,
        "unresolved_terminal_returns": 0,
        "unresolved_terminal_return_histories": [],
    }
    monkeypatch.setattr(
        historical_data_audit,
        "audit_historical_price_terminations",
        lambda **_kwargs: history,
    )
    monkeypatch.setattr(
        historical_data_audit,
        "audit_benchmark_calendar",
        lambda *_args, **_kwargs: {"complete": True},
    )

    readiness = historical_data_audit.backtest_data_readiness(
        "2021-01-01", "2026-07-17"
    )

    assert readiness["checks"][
        "listed_price_histories_complete"
    ] is False
    assert readiness["complete"] is False


def test_readiness_rejects_historical_quarterly_value_conflict(
    monkeypatch,
):
    history = {
        "universe_snapshot_coverage": {"full_period_covered": True},
        "signal_price_coverage": {
            "signal_membership_snapshots_complete": True,
            "complete": True,
            "usable_pit_financial_growth_complete": True,
        },
        "temporal_security_type_filter": {},
        "listed_price_histories_complete": True,
        "delisting_returns_complete": True,
        "research_common_equity_histories_ending_early": 0,
        "missing_price_histories_while_still_listed": 0,
        "unresolved_terminal_returns": 0,
        "unresolved_terminal_return_histories": [],
    }
    monkeypatch.setattr(
        historical_data_audit,
        "audit_historical_price_terminations",
        lambda **_kwargs: history,
    )
    monkeypatch.setattr(
        historical_data_audit,
        "audit_benchmark_calendar",
        lambda *_args, **_kwargs: {"complete": True},
    )
    quarterly = pd.DataFrame({
        "ticker": ["BAD", "BAD"],
        "fiscal_end": pd.to_datetime(["2023-03-31", "2023-03-31"]),
        "metric": ["net_income", "net_income"],
        "available_date": pd.to_datetime(["2024-01-01", "2024-01-01"]),
        "value": [1.0, 2.0],
        "accession": ["a", "b"],
    })

    readiness = historical_data_audit.backtest_data_readiness(
        "2021-01-01",
        "2026-07-17",
        quarterly_fundamentals=quarterly,
    )

    assert readiness["checks"][
        "historical_quarterly_value_conflicts_absent"
    ] is False
    assert len(readiness["historical_quarterly_value_conflicts"]) == 1
    sensitivity = readiness[
        "historical_quarterly_conflict_order_sensitivity"
    ]
    assert sensitivity["analyzable"] is False
    assert sensitivity["affected_signal_count"] == 0
