from __future__ import annotations

import pandas as pd
import pytest

from scripts.issuer_rename_sensitivity import _annual, _target_tickers


def test_target_tickers_reconstructs_latest_state_at_signal_date() -> None:
    ledger = pd.DataFrame(
        {
            "signal_date": ["2025-09-30", "2025-09-30", "2025-10-31", "2025-10-31"],
            "ticker": ["AAA", "BBB", "AAA", "CCC"],
            "target_weight_after": [0.5, 0.5, 0.0, 1.0],
        }
    )
    assert _target_tickers(ledger, "2025-09-30") == ["AAA", "BBB"]
    assert _target_tickers(ledger, "2025-10-31") == ["BBB", "CCC"]


def test_annual_compounds_daily_strategy_and_benchmark() -> None:
    result = pd.DataFrame(
        {"strategy": [0.1, -0.1], "benchmark": [0.05, 0.05]},
        index=pd.to_datetime(["2025-01-02", "2025-01-03"]),
    )
    annual = _annual(result, "current")
    assert annual.loc[2025, "current_strategy"] == pytest.approx(1.1 * 0.9 - 1)
    assert annual.loc[2025, "current_benchmark"] == pytest.approx(1.05**2 - 1)
