from __future__ import annotations

import json

from scripts.research_v3_shadow_manifest import (
    build_manifest,
    invalidate_for_data_sensitivity,
)
from src.research.can_slim_daily_recommendations import configs_for_decision_date


def test_shadow_manifest_does_not_activate_before_forward_start(tmp_path) -> None:
    evidence_path = tmp_path / "evidence.json"
    evidence = {
        "model_version": "challenger",
        "promotion_eligible": False,
        "configuration": {
            "start": "2019-01-01",
            "end": "2026-07-17",
            "top_n": 3,
            "market_ma_days": 200,
            "transaction_cost_bps": 10.0,
            "minimum_price": 10.0,
            "minimum_median_dollar_volume": 10_000_000.0,
            "minimum_eps_growth": 0.25,
            "minimum_relative_volume": 0.8,
            "minimum_52_week_high_ratio": 0.85,
            "maximum_financial_age_days": 150,
            "maximum_position_weight": 1 / 3,
            "signal_frequency": "monthly",
            "use_quarterly_fundamentals": True,
            "minimum_revenue_growth": 0.1,
            "price_channel": "none",
            "keltner_window": 20,
            "keltner_atr_window": 14,
            "keltner_multiplier": 1.5,
            "selection_mode": "growth",
            "ensemble_weight": 1.0,
        },
        "shadow_policy": {"forward_start": "2026-08-10"},
        "data_manifest": {"sha256": "a" * 64},
        "strategy_code_fingerprint": {"sha256": "b" * 64},
    }
    evidence_path.write_text(json.dumps(evidence), encoding="utf-8")
    manifest = build_manifest(evidence, evidence_path)
    before, _ = configs_for_decision_date(manifest, "2026-08-09")
    active, snapshot = configs_for_decision_date(manifest, "2026-08-10")
    assert before == []
    assert len(active) == 1
    assert active[0].maximum_financial_age_days == 150
    assert snapshot["effective_start"] == "2026-08-10"


def test_data_sensitivity_invalidates_challenger_before_forward_use(
    tmp_path,
) -> None:
    evidence_path = tmp_path / "sensitivity.json"
    evidence_path.write_text("{}", encoding="utf-8")
    manifest = {
        "policy_status": "FROZEN_SHADOW_CHALLENGER",
        "current_shadow_config_ids": [0],
        "current_shadow_configs": [{"top_n": 3}],
        "model_snapshots": [{"effective_start": "2026-08-10"}],
    }
    sensitivity = {
        "purpose": "historical_data_sensitivity",
        "quarterly_input": {
            "is_formal_input": False,
            "sha256": "a" * 64,
        },
        "historical_diagnostic": {
            "eligible_for_promotion": False,
            "wins_vs_nasdaq": 4,
            "years": 6,
            "cost_stress_wins": {"10": 4, "30": 3, "50": 3},
        },
    }

    invalidated = invalidate_for_data_sensitivity(
        manifest, sensitivity, evidence_path
    )

    assert invalidated["policy_status"] == "INVALIDATED_DATA_RELIABILITY"
    assert invalidated["current_shadow_config_ids"] == []
    assert invalidated["current_shadow_configs"] == []
    assert invalidated["model_snapshots"] == []
    assert invalidated["invalidation_evidence"]["wins_vs_nasdaq"] == 4
