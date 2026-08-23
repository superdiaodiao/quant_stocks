from __future__ import annotations

import json
from pathlib import Path

import pytest

from scripts.research_v5_shadow_manifest import create_manifest


def _research() -> dict:
    cost = {
        str(value): {"wins_vs_nasdaq": 4}
        for value in (10, 30, 50)
    }
    return {
        "model_version": "can-slim-v5-qqq-relative-trend-core-research",
        "historical_selection_contaminated": True,
        "release_status": "BLOCKED",
        "promotion_eligible": False,
        "configuration": {"relative_strength_window_sessions": 63},
        "cost_stress": cost,
        "comparison_vs_v4_at_30bps": {
            "minimum_excess_delta": 0.07,
            "maximum_drawdown_delta": 0.001,
        },
        "inputs": {
            "v4_daily": {"sha256": "a" * 64},
            "v4_frozen_summary": {"sha256": "b" * 64},
            "qqq_price": {
                "return_series": "close_plus_cash_dividend_on_ex_date",
                "missing_v4_sessions": 1,
                "carried_zero_return_market_holidays": ["2026-07-03"],
            },
        },
        "daily_artifact": {"sha256": "c" * 64},
    }


def _execution() -> dict:
    baseline = {str(size): {"wins_vs_nasdaq": 4} for size in (10_000, 25_000, 100_000)}
    stressed = {str(size): {"wins_vs_nasdaq": 4} for size in (10_000, 25_000, 100_000)}
    return {
        "selected_path_integrity": {
            "positions_with_missing_holding_prices": 0,
            "positions_with_unresolved_terminal_return": 0,
        },
        "continuous_whole_share_30bps": baseline,
        "execution_stress": {
            "transaction_cost_bps": 30.0,
            "additional_slippage_bps": 10.0,
            "deterministic_fill_fraction": 0.75,
            "rounding_rule": "retry",
            "results": stressed,
        },
    }


def test_v5_manifest_stays_forward_only_and_blocked(tmp_path: Path) -> None:
    research = tmp_path / "research.json"
    execution = tmp_path / "execution.json"
    research.write_text(json.dumps(_research()))
    execution.write_text(json.dumps(_execution()))

    result = create_manifest(research, execution)

    assert result["policy_status"] == "FROZEN_FORWARD_ONLY"
    assert result["release_status"] == "BLOCKED"
    assert result["promotion_eligible"] is False
    assert result["observed_forward_months"] == 0
    assert result["promotion_policy"]["relative_strength_warmup_sessions"] == 63


def test_v5_manifest_rejects_failed_execution_stress(tmp_path: Path) -> None:
    research = tmp_path / "research.json"
    execution = tmp_path / "execution.json"
    research.write_text(json.dumps(_research()))
    payload = _execution()
    payload["execution_stress"]["results"]["10000"]["wins_vs_nasdaq"] = 3
    execution.write_text(json.dumps(payload))

    with pytest.raises(ValueError, match="execution stress"):
        create_manifest(research, execution)
