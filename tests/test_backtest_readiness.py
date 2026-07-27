import pytest

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
