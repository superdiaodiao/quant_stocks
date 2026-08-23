from __future__ import annotations

import json
from pathlib import Path

import pandas as pd

from scripts import research_v4_shadow_status as module


def _summary() -> dict:
    return {
        "model_version": module.MODEL_VERSION,
        "policy_status": "FROZEN_FORWARD_ONLY",
        "release_status": "BLOCKED",
        "promotion_eligible": False,
        "forward_evidence_start": "2026-08-10",
        "promotion_policy": {
            "minimum_forward_months": 12,
            "minimum_monthly_signal_observations": 12,
        },
        "source_evidence": {
            "data_manifest_sha256": "a" * 64,
            "strategy_code_sha256": "b" * 64,
        },
        "quarterly_input": {"sha256": "c" * 64},
    }


def test_empty_v4_status_remains_blocked(tmp_path: Path) -> None:
    summary = tmp_path / "summary.json"
    summary.write_text(json.dumps(_summary()))

    result = module.build_status(summary_path=summary, model_dir=tmp_path / "model")

    assert result["release_status"] == "BLOCKED"
    assert result["promotion_eligible"] is False
    assert result["observed_forward_months"] == 0
    assert result["monthly_signal_observations"] == 0
    assert "minimum_forward_months" in result["unsatisfied_gates"]
    assert result["frozen_bindings"]["summary_sha256"]


def test_v4_status_reads_only_matching_frozen_evidence(tmp_path: Path) -> None:
    summary = tmp_path / "summary.json"
    summary.write_text(json.dumps(_summary()))
    model_dir = tmp_path / "model"
    model_dir.mkdir()
    pd.DataFrame({
        "signal_date": [f"2026-{month:02d}-28" for month in range(1, 13)],
        "model_version": [module.MODEL_VERSION] * 12,
        "portfolio_data_manifest_sha256": ["a" * 64] * 12,
        "portfolio_strategy_sha256": ["b" * 64] * 12,
    }).to_csv(model_dir / "recommendation_history.csv", index=False)
    (model_dir / "shadow_evaluation_30bps.json").write_text(json.dumps({
        "model_version": module.MODEL_VERSION,
        "transaction_cost_bps": 30.0,
        "contiguous_completed_forward_periods": 12,
        "contiguous_forward_strategy_return": 0.20,
        "contiguous_forward_benchmark_return": 0.10,
        "contiguous_forward_maximum_drawdown": -0.25,
    }))

    result = module.build_status(summary_path=summary, model_dir=model_dir)

    assert result["gates"]["minimum_forward_months"] is True
    assert result["gates"]["minimum_monthly_signal_observations"] is True
    assert result["gates"]["net_excess_at_30_bps_positive"] is True
    assert result["gates"]["maximum_drawdown_not_over_40pct"] is True
    assert result["gates"]["data_manifest_verifiable"] is True
    assert result["promotion_eligible"] is False
