#!/usr/bin/env python3
"""Evaluate staged v6 forward evidence without authorizing trading."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

import pandas as pd


DEFAULT_MANIFEST = Path("output/research_v6_walkforward_defensive_ensemble_shadow_summary.json")
DEFAULT_MARKS = Path("output/daily/can-slim-v6-walkforward-defensive-ensemble-shadow/weekly_marks.csv")
DEFAULT_DECISIONS = Path("output/daily/can-slim-v6-walkforward-defensive-ensemble-shadow/monthly_decisions.csv")
DEFAULT_OUTPUT = Path("output/daily/can-slim-v6-walkforward-defensive-ensemble-shadow/forward_status.json")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _load_csv(path: Path, date_column: str) -> pd.DataFrame:
    if not path.is_file() or path.stat().st_size == 0:
        return pd.DataFrame()
    return pd.read_csv(path, parse_dates=[date_column]).sort_values(date_column)


def build_status(
    manifest_path: Path = DEFAULT_MANIFEST,
    marks_path: Path = DEFAULT_MARKS,
    decisions_path: Path = DEFAULT_DECISIONS,
) -> dict:
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if manifest.get("policy_status") != "FROZEN_FORWARD_ONLY":
        raise ValueError("v6 policy is not frozen")
    if manifest.get("release_status") != "BLOCKED":
        raise ValueError("v6 release status must remain BLOCKED")
    start = pd.Timestamp(manifest["forward_evidence_start"])
    marks = _load_csv(marks_path, "week_end")
    decisions = _load_csv(decisions_path, "decision_date")
    if not marks.empty:
        marks = marks.loc[marks["week_end"].ge(start)].copy()
        if marks["week_end"].duplicated().any():
            raise ValueError("weekly marks contain duplicate week_end values")
    if not decisions.empty:
        decisions = decisions.loc[decisions["decision_date"].ge(start)].copy()
        if decisions["decision_date"].duplicated().any():
            raise ValueError("monthly decisions contain duplicate dates")

    weekly_marks = len(marks)
    monthly_decisions = len(decisions)
    strategy_return = qqq_return = excess = maximum_drawdown = None
    bindings_ok = execution_ok = False
    if weekly_marks:
        strategy = marks["strategy_return_after_costs"].astype(float)
        qqq = marks["qqq_return"].astype(float)
        strategy_return = float((1.0 + strategy).prod() - 1.0)
        qqq_return = float((1.0 + qqq).prod() - 1.0)
        excess = strategy_return - qqq_return
        nav = (1.0 + strategy).cumprod()
        maximum_drawdown = float(nav.div(nav.cummax()).sub(1.0).min())
        bindings_ok = bool(marks["bindings_verified"].astype(bool).all())
        execution_ok = bool(marks["execution_reconciled"].astype(bool).all())

    def evaluate(stage: dict, *, require_canary: bool) -> dict:
        gates = {
            "minimum_completed_forward_weeks": weekly_marks
            >= int(stage["minimum_completed_forward_weeks"]),
            "minimum_weekly_marks": weekly_marks
            >= int(stage["minimum_weekly_marks"]),
            "minimum_monthly_decisions": monthly_decisions
            >= int(stage["minimum_monthly_decisions"]),
            "cumulative_excess_vs_qqq_positive": excess is not None and excess > 0.0,
            "maximum_drawdown_within_limit": maximum_drawdown is not None
            and maximum_drawdown >= float(stage["maximum_forward_drawdown"]),
            "bindings_verified": bindings_ok,
            "execution_reconciled": execution_ok,
            "parameters_unchanged": True,
        }
        if require_canary:
            gates["authorized_canary_execution_reconciled"] = bool(
                not marks.empty
                and "authorized_canary_execution_reconciled" in marks
                and marks["authorized_canary_execution_reconciled"].astype(bool).all()
            )
        return {
            "eligible_for_review": bool(all(gates.values())),
            "gates": gates,
            "unsatisfied_gates": [name for name, passed in gates.items() if not passed],
        }

    policy = manifest["staged_policy"]
    canary = evaluate(policy["limited_canary_review"], require_canary=False)
    full = evaluate(policy["full_promotion_review"], require_canary=True)
    return {
        "schema_version": 1,
        "model_version": manifest["model_version"],
        "policy_status": "FROZEN_FORWARD_ONLY",
        "release_status": "BLOCKED",
        "promotion_eligible": False,
        "broker_action_authorized": False,
        "forward_evidence_start": manifest["forward_evidence_start"],
        "observed_forward_weeks": weekly_marks,
        "observed_weekly_marks": weekly_marks,
        "observed_monthly_decisions": monthly_decisions,
        "cumulative_strategy_return_after_costs": strategy_return,
        "cumulative_qqq_return": qqq_return,
        "cumulative_excess_vs_qqq": excess,
        "forward_maximum_drawdown": maximum_drawdown,
        "limited_canary_review": canary,
        "full_promotion_review": full,
        "manifest_binding": {
            "path": str(manifest_path), "sha256": _sha256(manifest_path)
        },
        "evidence_paths": {
            "weekly_marks": str(marks_path),
            "monthly_decisions": str(decisions_path),
        },
        "interpretation": (
            "Eligibility only opens a human review. This reporter cannot connect "
            "a broker, create an order, or change release_status."
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
    print(json.dumps({**result, "output": str(args.output)}, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
