from __future__ import annotations

import json
from pathlib import Path

import pytest

from scripts.research_v4_shadow_manifest import create_manifest


def test_manifest_rejects_path_risk_for_different_daily_artifact(
    tmp_path: Path,
) -> None:
    evidence = tmp_path / "evidence.json"
    evidence.write_text(
        json.dumps(
            {
                "model_version": "can-slim-v4-cost-robust-top10-shadow",
                "release_status": "BLOCKED",
                "promotion_eligible": False,
                "historical_selection_contaminated": True,
                "selected_data_audit": {
                    "positions_with_missing_holding_prices": 0,
                    "positions_with_unresolved_terminal_return": 0,
                },
                "historical_diagnostic": {
                    "cost_stress_wins": {"10": 4, "30": 4, "50": 4}
                },
                "artifact_bindings": {"daily": {"sha256": "daily"}},
            }
        )
    )
    risk = tmp_path / "risk.json"
    risk.write_text(json.dumps({"input_backtest": {"sha256": "other"}}))

    with pytest.raises(ValueError, match="daily artifact"):
        create_manifest(evidence, risk)
