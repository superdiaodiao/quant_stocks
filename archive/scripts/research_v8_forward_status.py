#!/usr/bin/env python3
"""Evaluate v8 13/26/39-week evidence without enabling any runtime."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

import pandas as pd

from src.research.short_forward_gate import evaluate_short_forward_gate


DEFAULT_MANIFEST = Path("output/research_v8_monthly_risk_budget_blend_shadow_summary.json")
DEFAULT_MARKS = Path("output/daily/can-slim-v8-monthly-risk-budget-blend-shadow/weekly_marks.csv")
DEFAULT_DECISIONS = Path("output/daily/can-slim-v8-monthly-risk-budget-blend-shadow/monthly_decisions.csv")
DEFAULT_OUTPUT = Path("output/daily/can-slim-v8-monthly-risk-budget-blend-shadow/forward_status.json")
DEFAULT_RUNTIME_STATE = DEFAULT_OUTPUT.parent / "runtime_state.json"


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _load(path: Path, date_column: str) -> pd.DataFrame:
    if not path.is_file() or path.stat().st_size == 0:
        return pd.DataFrame()
    return pd.read_csv(path, parse_dates=[date_column]).sort_values(date_column)


def build_status(
    manifest_path: Path = DEFAULT_MANIFEST,
    marks_path: Path = DEFAULT_MARKS,
    decisions_path: Path = DEFAULT_DECISIONS,
    runtime_state_path: Path | None = None,
) -> dict:
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if manifest.get("policy_status") != "FROZEN_FORWARD_ONLY":
        raise ValueError("v8 policy is not frozen")
    if manifest.get("release_status") != "BLOCKED":
        raise ValueError("v8 must remain BLOCKED")
    start = pd.Timestamp(manifest["forward_evidence_start"])
    marks = _load(marks_path, "week_end")
    decisions = _load(decisions_path, "decision_date")
    if not marks.empty:
        marks = marks.loc[marks["week_end"].ge(start)].copy()
        if marks["week_end"].duplicated().any():
            raise ValueError("weekly marks contain duplicate dates")
    if not decisions.empty:
        decisions = decisions.loc[decisions["decision_date"].ge(start)].copy()
        if decisions["decision_date"].duplicated().any():
            raise ValueError("monthly decisions contain duplicate dates")
    if marks.empty:
        relative = []
        integrity = {
            "parameters_frozen": True,
            "manifest_valid": True,
            "selected_prices_complete": True,
            "delisting_values_complete": True,
        }
    else:
        strategy = pd.to_numeric(marks["strategy_return_after_costs"], errors="raise")
        qqq = pd.to_numeric(marks["qqq_return"], errors="raise")
        relative = ((1.0 + strategy) / (1.0 + qqq) - 1.0).tolist()
        integrity = {
            "parameters_frozen": bool(marks["parameters_frozen"].astype(bool).all()),
            "manifest_valid": bool(marks["bindings_verified"].astype(bool).all()),
            "selected_prices_complete": bool(marks["selected_prices_complete"].astype(bool).all()),
            "delisting_values_complete": bool(marks["delisting_values_complete"].astype(bool).all()),
        }
    gate = evaluate_short_forward_gate(
        relative, monthly_decisions=len(decisions), **integrity
    )
    if runtime_state_path is None:
        runtime_state_path = (
            DEFAULT_RUNTIME_STATE
            if manifest_path == DEFAULT_MANIFEST
            else marks_path.parent / "runtime_state.json"
        )
    runtime = (
        json.loads(runtime_state_path.read_text(encoding="utf-8"))
        if runtime_state_path.is_file() else {"enabled": False, "mode": None}
    )
    return {
        "schema_version": 1,
        "model_version": manifest["model_version"],
        "policy_status": "FROZEN_FORWARD_ONLY",
        "release_status": "BLOCKED",
        "promotion_eligible": False,
        "broker_action_authorized": False,
        "observation_runtime_enabled": bool(runtime.get("enabled")),
        "observation_runtime_mode": runtime.get("mode"),
        "forward_evidence_start": manifest["forward_evidence_start"],
        "sequential_review": gate,
        "manifest_binding": {"path": str(manifest_path), "sha256": _sha256(manifest_path)},
        "evidence_paths": {"weekly_marks": str(marks_path), "monthly_decisions": str(decisions_path)},
        "interpretation": (
            "A gate result only opens a human promotion review. This reporter "
            "cannot change release status, connect a broker, or create orders."
        ),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    parser.add_argument("--marks", type=Path, default=DEFAULT_MARKS)
    parser.add_argument("--decisions", type=Path, default=DEFAULT_DECISIONS)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args()
    result = build_status(args.manifest, args.marks, args.decisions)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n")
    print(json.dumps(result, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
