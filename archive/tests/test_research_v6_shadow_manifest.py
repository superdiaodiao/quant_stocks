from __future__ import annotations

import json
from pathlib import Path

from scripts.research_v6_shadow_manifest import build_manifest


def test_v6_manifest_uses_staged_13_and_26_week_gates(tmp_path: Path) -> None:
    research = tmp_path / "research.json"
    research.write_text(json.dumps({
        "historical_selection_contaminated": True,
        "release_status": "BLOCKED",
        "configuration": {"cadence": "monthly"},
        "inputs": {"base": "bound"},
    }))
    execution = tmp_path / "execution.json"
    execution.write_text(json.dumps({
        "release_status": "BLOCKED",
        "inputs": {"targets": "bound"},
    }))
    base_summary = tmp_path / "base.json"
    base_summary.write_text(json.dumps({
        "release_status": "BLOCKED",
        "current_shadow_configs": [{"top_n": 3}],
        "model_snapshots": [{
            "effective_start": "2026-01-01",
            "configs": [{"top_n": 3}],
        }],
    }))
    quarterly = tmp_path / "quarterly.csv"
    quarterly.write_text("ticker,quarter\nAAA,2026Q1\n")
    output = tmp_path / "shadow.json"

    result = build_manifest(
        research, execution, output,
        base_summary_path=base_summary,
        quarterly_path=quarterly,
    )

    assert result["release_status"] == "BLOCKED"
    assert result["promotion_eligible"] is False
    assert result["forward_evidence_start"] == "2026-09-01"
    assert result["staged_policy"]["limited_canary_review"][
        "minimum_completed_forward_weeks"
    ] == 13
    assert result["staged_policy"]["full_promotion_review"][
        "minimum_completed_forward_weeks"
    ] == 26
    assert result["evidence_boundaries"][
        "weekly_marks_are_performance_observations_not_independent_trades"
    ] is True
