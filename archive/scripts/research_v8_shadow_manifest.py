#!/usr/bin/env python3
"""Freeze the v8 research candidate without starting observation or trading."""

from __future__ import annotations

import argparse
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path


MODEL_VERSION = "can-slim-v8-monthly-risk-budget-blend-shadow"
DEFAULT_RESEARCH = Path("output/research_v8_monthly_risk_budget_blend_summary.json")
DEFAULT_EXECUTION = Path("output/research_v8_execution_sensitivity.json")
DEFAULT_V6 = Path("output/research_v6_walkforward_defensive_ensemble_summary.json")
DEFAULT_V6_BASE = Path(
    "output/can_slim_walk_forward_summary_quarterly_financials_"
    "financial_age_150_365_550_proven_only_bank_v3_13d77de9.json"
)
DEFAULT_V7 = Path("output/research_v7_qqq_targeted_core_satellite_summary.json")
DEFAULT_ROBUSTNESS = Path("output/research_v8_short_horizon_robustness.json")
DEFAULT_OUTPUT = Path("output/research_v8_monthly_risk_budget_blend_shadow_summary.json")
DEFAULT_V7_COMPONENT = Path("output/research_v8_v7_frozen_component_summary.json")
DEFAULT_QUARTERLY = Path(
    "output/data_provenance/companyfacts_proven_only_manifest-"
    "6c8a87fcc71cfcd5-recipe-6f0998be-q1-fp-guard-bank-duration-v3/quarterly.csv"
)
RUNTIME_CODE_PATHS = (
    Path("src/io/security_universe.py"),
    Path("src/io/nasdaq_update.py"),
    Path("src/research/short_forward_gate.py"),
    Path("scripts/research_v5_trend_core_satellite.py"),
    Path("scripts/research_v6_data_readiness.py"),
    Path("scripts/research_v6_market_refresh.py"),
    Path("scripts/research_v6_scheduled_run.py"),
    Path("scripts/research_v8_shadow_manifest.py"),
    Path("scripts/research_v8_forward_status.py"),
    Path("scripts/research_v8_shadow_signal.py"),
    Path("scripts/research_v8_weekly_mark.py"),
    Path("scripts/research_v8_observe.py"),
    Path("scripts/research_v8_scheduled_run.py"),
    Path("scripts/research_v8_shadow_activate.py"),
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
    v6_path: Path = DEFAULT_V6,
    v6_base_path: Path = DEFAULT_V6_BASE,
    v7_path: Path = DEFAULT_V7,
    robustness_path: Path = DEFAULT_ROBUSTNESS,
    output_path: Path = DEFAULT_OUTPUT,
    v7_component_path: Path = DEFAULT_V7_COMPONENT,
    quarterly_path: Path = DEFAULT_QUARTERLY,
    forward_start: str = "2026-09-01",
) -> dict:
    research = json.loads(research_path.read_text(encoding="utf-8"))
    execution = json.loads(execution_path.read_text(encoding="utf-8"))
    v6 = json.loads(v6_path.read_text(encoding="utf-8"))
    v6_base = json.loads(v6_base_path.read_text(encoding="utf-8"))
    v7 = json.loads(v7_path.read_text(encoding="utf-8"))
    robustness = json.loads(robustness_path.read_text(encoding="utf-8"))
    if research.get("release_status") != "BLOCKED":
        raise ValueError("v8 research must remain BLOCKED")
    if research.get("historical_selection_contaminated") is not True:
        raise ValueError("v8 selection contamination warning is missing")
    if execution.get("research_only") is not True:
        raise ValueError("v8 execution audit must remain research-only")
    gate = execution.get("robustness_gate", {})
    if not gate.get("baseline_passed") or not gate.get("execution_stress_passed"):
        raise ValueError("v8 supported-size execution robustness gate failed")
    if gate.get("supported_account_sizes") != [25_000, 100_000]:
        raise ValueError("unexpected v8 supported account-size scope")
    for label, component in (("v6", v6), ("v7", v7)):
        if component.get("release_status") != "BLOCKED":
            raise ValueError(f"{label} component must remain BLOCKED")
    if not v6_base.get("model_snapshots"):
        raise ValueError("v6 base model snapshots are missing")
    if not v7.get("model_snapshots"):
        raise ValueError("v7 model snapshots are missing")
    if robustness.get("independent_forward_evidence") is not False:
        raise ValueError("historical robustness cannot be forward evidence")
    v7_component = {
        "schema_version": 1,
        "model_version": "can-slim-v8-v7-frozen-component-shadow",
        "release_status": "BLOCKED",
        "research_only": True,
        "parameter_update_frequency": "frozen",
        "signal_frequency": "monthly",
        "uses_quarterly_fundamentals": True,
        "uses_adaptive_channel": False,
        "current_shadow_configs": v7["model_snapshots"][-1]["configs"],
        "model_snapshots": v7["model_snapshots"],
        "quarterly_input": {
            "path": str(quarterly_path.resolve()),
            "sha256": _sha256(quarterly_path),
        },
        "source_v7_summary": {"path": str(v7_path), "sha256": _sha256(v7_path)},
    }
    v7_component_path.write_text(
        json.dumps(v7_component, indent=2, sort_keys=True) + "\n"
    )
    payload = {
        "schema_version": 1,
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "model_version": MODEL_VERSION,
        "research_only": True,
        "policy_status": "FROZEN_FORWARD_ONLY",
        "release_status": "BLOCKED",
        "promotion_eligible": False,
        "broker_action_authorized": False,
        "observation_runtime_enabled": False,
        "forward_evidence_start": forward_start,
        "configuration_freeze_date": "2026-08-10",
        "observed_forward_weeks": 0,
        "observed_weekly_marks": 0,
        "observed_monthly_decisions": 0,
        "historical_selection_contaminated": True,
        "frozen_configuration": research["configuration"],
        "quarterly_input": {
            "path": str(quarterly_path.resolve()),
            "sha256": _sha256(quarterly_path),
        },
        "supported_account_sizes": gate["supported_account_sizes"],
        "unsupported_account_sizes": gate["unsupported_account_sizes"],
        "forward_review_policy": research["forward_review_policy"],
        "component_snapshots": {
            "v6": v6_base["model_snapshots"],
            "v7": v7["model_snapshots"],
        },
        "bindings": {
            "research_summary": {"path": str(research_path), "sha256": _sha256(research_path)},
            "execution_summary": {"path": str(execution_path), "sha256": _sha256(execution_path)},
            "short_horizon_robustness": {"path": str(robustness_path), "sha256": _sha256(robustness_path)},
            "v6_component": {"path": str(v6_path), "sha256": _sha256(v6_path)},
            "v6_base_snapshots": {"path": str(v6_base_path), "sha256": _sha256(v6_base_path)},
            "v7_component": {"path": str(v7_path), "sha256": _sha256(v7_path)},
            "v7_frozen_component": {"path": str(v7_component_path), "sha256": _sha256(v7_component_path)},
            "quarterly_input": {"path": str(quarterly_path.resolve()), "sha256": _sha256(quarterly_path)},
            "runtime_code": {str(path): _sha256(path) for path in RUNTIME_CODE_PATHS},
            "research_inputs": research["inputs"],
        },
        "evidence_boundaries": {
            "historical_diagnostics_cannot_satisfy_forward_gates": True,
            "thirteen_weeks_can_only_open_limited_canary_review": True,
            "twenty_six_weeks_requires_positive_bootstrap_lower_bound": True,
            "thirty_nine_weeks_is_final_accept_or_reject_review": True,
            "no_runtime_has_been_enabled": True,
        },
    }
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")
    return payload


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--forward-start", default="2026-09-01")
    args = parser.parse_args()
    print(json.dumps(build_manifest(output_path=args.output, forward_start=args.forward_start), indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
