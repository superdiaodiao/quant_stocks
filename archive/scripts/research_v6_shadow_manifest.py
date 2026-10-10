#!/usr/bin/env python3
"""Freeze v6 research inputs with staged 13/26-week evidence gates."""

from __future__ import annotations

import argparse
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path


MODEL_VERSION = "can-slim-v6-walkforward-defensive-ensemble-shadow"
DEFAULT_RESEARCH = Path("output/research_v6_walkforward_defensive_ensemble_summary.json")
DEFAULT_EXECUTION = Path("output/research_v6_execution_sensitivity.json")
DEFAULT_BASE_SUMMARY = Path(
    "output/can_slim_walk_forward_summary_quarterly_financials_"
    "financial_age_150_365_550_proven_only_bank_v3_13d77de9.json"
)
DEFAULT_QUARTERLY = Path(
    "output/data_provenance/companyfacts_proven_only_manifest-"
    "6c8a87fcc71cfcd5-recipe-6f0998be-q1-fp-guard-bank-duration-v3/quarterly.csv"
)
DEFAULT_OUTPUT = Path("output/research_v6_walkforward_defensive_ensemble_shadow_summary.json")
RUNTIME_CODE_PATHS = (
    Path("scripts/research_v6_launchd.py"),
    Path("ops/com.quant-stocks.v6-shadow.plist"),
    Path("scripts/research_v6_scheduled_run.py"),
    Path("scripts/research_v6_market_refresh.py"),
    Path("scripts/research_v6_data_readiness.py"),
    Path("scripts/research_v6_observe.py"),
    Path("scripts/research_v6_shadow_signal.py"),
    Path("scripts/research_v6_weekly_mark.py"),
    Path("scripts/research_v6_forward_status.py"),
)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def build_manifest(
    research_path: Path = DEFAULT_RESEARCH,
    execution_path: Path = DEFAULT_EXECUTION,
    output_path: Path = DEFAULT_OUTPUT,
    forward_start: str = "2026-09-01",
    base_summary_path: Path = DEFAULT_BASE_SUMMARY,
    quarterly_path: Path = DEFAULT_QUARTERLY,
) -> dict:
    research = json.loads(research_path.read_text(encoding="utf-8"))
    execution = json.loads(execution_path.read_text(encoding="utf-8"))
    base_summary = json.loads(base_summary_path.read_text(encoding="utf-8"))
    if research.get("historical_selection_contaminated") is not True:
        raise ValueError("v6 research must retain the selection warning")
    if research.get("release_status") != "BLOCKED":
        raise ValueError("v6 research result must remain BLOCKED")
    if execution.get("release_status") != "BLOCKED":
        raise ValueError("v6 execution result must remain BLOCKED")
    if base_summary.get("release_status") != "BLOCKED":
        raise ValueError("base walk-forward result must remain BLOCKED")
    if not base_summary.get("model_snapshots"):
        raise ValueError("base walk-forward snapshots are missing")
    payload = {
        "schema_version": 1,
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "model_version": MODEL_VERSION,
        "research_only": True,
        "policy_status": "FROZEN_FORWARD_ONLY",
        "release_status": "BLOCKED",
        "promotion_eligible": False,
        "forward_evidence_start": forward_start,
        "configuration_freeze_date": "2026-08-10",
        "observed_forward_weeks": 0,
        "observed_weekly_marks": 0,
        "observed_monthly_decisions": 0,
        "historical_selection_contaminated": True,
        "parameter_update_frequency": "frozen",
        "signal_frequency": "monthly",
        "uses_quarterly_fundamentals": True,
        "uses_adaptive_channel": False,
        "current_shadow_configs": base_summary["current_shadow_configs"],
        "model_snapshots": base_summary["model_snapshots"],
        "quarterly_input": {
            "path": str(quarterly_path.resolve()),
            "sha256": _sha256(quarterly_path),
        },
        "frozen_configuration": research["configuration"],
        "staged_policy": {
            "limited_canary_review": {
                "minimum_completed_forward_weeks": 13,
                "minimum_weekly_marks": 13,
                "minimum_monthly_decisions": 3,
                "cumulative_excess_vs_qqq_after_costs_must_be_positive": True,
                "maximum_forward_drawdown": -0.15,
                "parameters_must_remain_unchanged": True,
                "data_and_price_bindings_must_verify": True,
                "execution_reconciliation_must_have_no_unresolved_error": True,
                "authority": (
                    "Review only. Any broker connection, order, or capital limit "
                    "still requires explicit user authorization."
                ),
                "not_full_validation": True,
            },
            "full_promotion_review": {
                "minimum_completed_forward_weeks": 26,
                "minimum_weekly_marks": 26,
                "minimum_monthly_decisions": 6,
                "cumulative_excess_vs_qqq_at_50bps_must_be_positive": True,
                "maximum_forward_drawdown": -0.25,
                "parameters_must_remain_unchanged": True,
                "data_and_price_bindings_must_verify": True,
                "selected_price_and_terminal_data_must_be_complete": True,
                "limited_canary_execution_must_reconcile_if_authorized": True,
            },
            "early_stop_conditions": [
                "any frozen input SHA mismatch",
                "any unresolved held-price or terminal-return gap",
                "forward drawdown breaches the active stage limit",
                "actual execution or slippage cannot be reconciled",
            ],
        },
        "evidence_boundaries": {
            "weekly_marks_are_performance_observations_not_independent_trades": True,
            "monthly_allocation_remains_unchanged": True,
            "thirteen_weeks_can_only_open_a_limited_canary_review": True,
            "historical_diagnostics_cannot_satisfy_forward_gates": True,
        },
        "bindings": {
            "research_summary": {
                "path": str(research_path), "sha256": _sha256(research_path)
            },
            "execution_summary": {
                "path": str(execution_path), "sha256": _sha256(execution_path)
            },
            "base_walk_forward_summary": {
                "path": str(base_summary_path), "sha256": _sha256(base_summary_path)
            },
            "quarterly_input": {
                "path": str(quarterly_path.resolve()), "sha256": _sha256(quarterly_path)
            },
            "runtime_code": {
                str(path): _sha256(path) for path in RUNTIME_CODE_PATHS
            },
            "research_inputs": research["inputs"],
            "execution_inputs": execution["inputs"],
        },
    }
    output_path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")
    return payload


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--research", type=Path, default=DEFAULT_RESEARCH)
    parser.add_argument("--execution", type=Path, default=DEFAULT_EXECUTION)
    parser.add_argument("--base-summary", type=Path, default=DEFAULT_BASE_SUMMARY)
    parser.add_argument("--quarterly", type=Path, default=DEFAULT_QUARTERLY)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--forward-start", default="2026-09-01")
    args = parser.parse_args()
    print(json.dumps(build_manifest(
        args.research, args.execution, args.output, args.forward_start,
        args.base_summary, args.quarterly,
    ), indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
