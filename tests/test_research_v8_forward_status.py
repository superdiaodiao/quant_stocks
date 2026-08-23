import json
from pathlib import Path

import pandas as pd

from scripts.research_v8_forward_status import build_status


def _manifest(path: Path):
    path.write_text(json.dumps({
        "model_version": "can-slim-v8-monthly-risk-budget-blend-shadow",
        "policy_status": "FROZEN_FORWARD_ONLY",
        "release_status": "BLOCKED",
        "observation_runtime_enabled": False,
        "forward_evidence_start": "2026-09-01",
    }))


def test_empty_status_accumulates_without_enabling_runtime(tmp_path: Path):
    manifest = tmp_path / "manifest.json"
    _manifest(manifest)
    result = build_status(manifest, tmp_path / "marks.csv", tmp_path / "decisions.csv")
    assert result["sequential_review"]["status"] == "ACCUMULATING_CANARY"
    assert result["observation_runtime_enabled"] is False
    assert result["release_status"] == "BLOCKED"


def test_13_positive_marks_only_open_canary(tmp_path: Path):
    manifest = tmp_path / "manifest.json"
    _manifest(manifest)
    marks = tmp_path / "marks.csv"
    pd.DataFrame({
        "week_end": pd.date_range("2026-09-04", periods=13, freq="W-FRI"),
        "strategy_return_after_costs": 0.01,
        "qqq_return": 0.005,
        "parameters_frozen": True,
        "bindings_verified": True,
        "selected_prices_complete": True,
        "delisting_values_complete": True,
    }).to_csv(marks, index=False)
    decisions = tmp_path / "decisions.csv"
    pd.DataFrame({"decision_date": ["2026-09-30", "2026-10-30", "2026-11-30"]}).to_csv(decisions, index=False)
    result = build_status(manifest, marks, decisions)
    assert result["sequential_review"]["status"] == "CANARY_REVIEW_ONLY"
    assert result["sequential_review"]["promotion_eligible"] is False
