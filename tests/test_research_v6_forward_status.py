from __future__ import annotations

import json
from pathlib import Path

import pandas as pd

from scripts.research_v6_forward_status import build_status


def _manifest(path: Path) -> None:
    path.write_text(json.dumps({
        "model_version": "can-slim-v6-walkforward-defensive-ensemble-shadow",
        "policy_status": "FROZEN_FORWARD_ONLY",
        "release_status": "BLOCKED",
        "forward_evidence_start": "2026-09-01",
        "staged_policy": {
            "limited_canary_review": {
                "minimum_completed_forward_weeks": 13,
                "minimum_weekly_marks": 13,
                "minimum_monthly_decisions": 3,
                "maximum_forward_drawdown": -0.15,
            },
            "full_promotion_review": {
                "minimum_completed_forward_weeks": 26,
                "minimum_weekly_marks": 26,
                "minimum_monthly_decisions": 6,
                "maximum_forward_drawdown": -0.25,
            },
        },
    }))


def test_empty_status_remains_blocked(tmp_path: Path) -> None:
    manifest = tmp_path / "manifest.json"
    _manifest(manifest)

    result = build_status(manifest, tmp_path / "marks.csv", tmp_path / "decisions.csv")

    assert result["release_status"] == "BLOCKED"
    assert result["limited_canary_review"]["eligible_for_review"] is False
    assert result["broker_action_authorized"] is False


def test_thirteen_positive_weeks_only_open_canary_review(tmp_path: Path) -> None:
    manifest = tmp_path / "manifest.json"
    _manifest(manifest)
    dates = pd.date_range("2026-09-04", periods=13, freq="W-FRI")
    marks = tmp_path / "marks.csv"
    pd.DataFrame({
        "week_end": dates,
        "strategy_return_after_costs": 0.01,
        "qqq_return": 0.005,
        "bindings_verified": True,
        "execution_reconciled": True,
    }).to_csv(marks, index=False)
    decisions = tmp_path / "decisions.csv"
    pd.DataFrame({
        "decision_date": ["2026-09-30", "2026-10-30", "2026-11-30"],
    }).to_csv(decisions, index=False)

    result = build_status(manifest, marks, decisions)

    assert result["limited_canary_review"]["eligible_for_review"] is True
    assert result["full_promotion_review"]["eligible_for_review"] is False
    assert result["promotion_eligible"] is False
    assert result["release_status"] == "BLOCKED"
