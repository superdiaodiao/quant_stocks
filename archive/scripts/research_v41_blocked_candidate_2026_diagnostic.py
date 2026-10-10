#!/usr/bin/env python3
"""Diagnose the least-drawdown v40 candidate on January-July 2026 once.

V40 remained blocked by its precommitted 25% absolute drawdown gate.  This
runner therefore cannot promote the candidate even if 2026 diagnostics pass.
It freezes the v40 rank-1 inverse-volatility weighting rule and measures only
whether that structure remedies the already-known 2026 relative drawdown.
"""

from __future__ import annotations

import argparse
from datetime import datetime
import hashlib
import json
from pathlib import Path

import pandas as pd

from scripts import research_v23_stock_only_frequency as v23
from scripts import research_v27_stock_only_2026_observation as v27
from scripts import research_v40_inverse_volatility_weights_development as v40


TRAINING_YEARS = tuple(range(2020, 2026))
OBSERVATION_START = v27.OBSERVATION_START
OBSERVATION_END = v27.OBSERVATION_END
OBSERVATION_MONTHS = v27.OBSERVATION_MONTHS
COSTS = v40.COSTS
MAXIMUM_DRAWDOWN_LAG = 0.10
MINIMUM_MONTHLY_WINS = 4
SELECTED_CANDIDATE = "inverse_volatility_weight_power_1.5"
INVERSE_VOLATILITY_POWER = 1.5
TRUE_PROSPECTIVE_START = "2026-08-31"

OUTPUT_DIR = Path(
    "output/research_only/v41/blocked_candidate_2026_diagnostic_20260830"
)
PROTOCOL_PATH = OUTPUT_DIR / "frozen_protocol.json"
RESULT_OUTPUT_DIR = OUTPUT_DIR / "diagnostic_results"
V27_TARGETS = v27.RESULT_OUTPUT_DIR / "observed_targets.csv"
V27_MANIFEST = v27.RESULT_OUTPUT_DIR / "manifest.json"
V40_MANIFEST = v40.DEVELOPMENT_OUTPUT_DIR / "manifest.json"


def _sha256(path: Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _file_binding(path: Path) -> dict:
    path = Path(path)
    return {"path": str(path), "sha256": _sha256(path)}


def _validate_training_source() -> dict:
    manifest = json.loads(V40_MANIFEST.read_text(encoding="utf-8"))
    if manifest["development_status"] != "BLOCKED":
        raise RuntimeError("v40 development status changed")
    ranking = manifest["training_ranking"]
    if ranking[0]["candidate"] != SELECTED_CANDIDATE:
        raise RuntimeError("v40 least-drawdown rank changed")
    if ranking[0]["training_eligible"]:
        raise RuntimeError("v40 rank-1 unexpectedly became eligible")
    if manifest["2026_used_for_parameter_selection"]:
        raise RuntimeError("v40 unexpectedly used 2026 for parameter selection")
    return {
        "candidate": SELECTED_CANDIDATE,
        "inverse_volatility_power": INVERSE_VOLATILITY_POWER,
        "v40_development_status": "BLOCKED",
        "v40_training_eligible": False,
        "selection_reason": "rank_1_minimum_training_drawdown",
        "training_years": list(TRAINING_YEARS),
        "training_years_counted_as_final_wins": False,
        "threshold_used_2026": False,
    }


def freeze_protocol(path: Path = PROTOCOL_PATH) -> dict:
    path = Path(path)
    if path.exists():
        raise RuntimeError(f"v41 protocol will not be overwritten: {path}")
    runner = Path(__file__).resolve().relative_to(Path.cwd().resolve())
    protocol = {
        "schema_version": 1,
        "research_only": True,
        "stage": "V41_BLOCKED_CANDIDATE_2026_DIAGNOSTIC_PRECOMMITMENT",
        "status": "FROZEN_NOT_DIAGNOSED",
        "frozen_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "training_source": _validate_training_source(),
        "evaluation_boundary": {
            "training_years_excluded_from_comparison": list(TRAINING_YEARS),
            "observation_start": OBSERVATION_START,
            "observation_end": OBSERVATION_END,
            "observation_months": list(OBSERVATION_MONTHS),
            "parameter_isolated_from_2026": True,
            "architecture_isolated_from_2026": False,
            "pristine_forward_test": False,
            "diagnostic_only": True,
            "cannot_override_blocked_training_protocol": True,
            "true_prospective_start": TRUE_PROSPECTIVE_START,
        },
        "precommitted_diagnostic_gates": {
            "positive_excess_vs_nasdaq_at_30bps": True,
            "positive_excess_vs_nasdaq_at_50bps": True,
            "minimum_monthly_wins_vs_nasdaq_at_50bps": MINIMUM_MONTHLY_WINS,
            "maximum_drawdown_lag_vs_nasdaq_percentage_points": (
                MAXIMUM_DRAWDOWN_LAG * 100.0
            ),
            "all_observation_months_present": True,
            "no_forbidden_etf_targets": True,
        },
        "input_bindings": {
            "runner": _file_binding(runner),
            "v27_observation_helpers": _file_binding(
                Path("scripts/research_v27_stock_only_2026_observation.py")
            ),
            "v27_manifest": _file_binding(V27_MANIFEST),
            "v27_targets": _file_binding(V27_TARGETS),
            "v40_weight_helpers": _file_binding(
                Path("scripts/research_v40_inverse_volatility_weights_development.py")
            ),
            "v40_manifest": _file_binding(V40_MANIFEST),
        },
        "parameters_frozen_before_diagnostic": True,
        "contains_index_etf_holdings": False,
        "brokerage_or_trading_authorized": False,
        "release_status": "BLOCKED",
        "promotion_eligible": False,
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(protocol, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return {**protocol, "protocol": _file_binding(path)}


def _validated_protocol(path: Path) -> tuple[dict, str]:
    path = Path(path)
    protocol_sha = _sha256(path)
    protocol = json.loads(path.read_text(encoding="utf-8"))
    if protocol["status"] != "FROZEN_NOT_DIAGNOSED":
        raise RuntimeError("v41 protocol status changed")
    if protocol["training_source"] != _validate_training_source():
        raise RuntimeError("v41 training source changed")
    for name, binding in protocol["input_bindings"].items():
        if _sha256(Path(binding["path"])) != binding["sha256"]:
            raise RuntimeError(f"v41 file binding changed for {name}")
    return protocol, protocol_sha


def _maximum_drawdown(returns: pd.Series) -> float:
    return v23._maximum_drawdown(returns)


def evaluate_diagnostic(
    results: dict[int, pd.DataFrame],
    targets: pd.DataFrame,
) -> dict:
    costs = {}
    for cost, result in results.items():
        monthly = (
            (1.0 + result[["strategy", "benchmark"]])
            .groupby(result.index.to_period("M"))
            .prod()
            - 1.0
        )
        monthly["excess_vs_nasdaq"] = monthly["strategy"] - monthly["benchmark"]
        strategy = float((1.0 + result["strategy"]).prod() - 1.0)
        nasdaq = float((1.0 + result["benchmark"]).prod() - 1.0)
        strategy_drawdown = _maximum_drawdown(result["strategy"])
        nasdaq_drawdown = _maximum_drawdown(result["benchmark"])
        costs[str(cost)] = {
            "monthly": [
                {"month": str(month), **values}
                for month, values in monthly.to_dict(orient="index").items()
            ],
            "monthly_wins_vs_nasdaq": int(
                monthly["excess_vs_nasdaq"].gt(0.0).sum()
            ),
            "compounded_strategy": strategy,
            "compounded_nasdaq": nasdaq,
            "compounded_excess_vs_nasdaq": strategy - nasdaq,
            "strategy_maximum_drawdown": strategy_drawdown,
            "nasdaq_maximum_drawdown": nasdaq_drawdown,
            "drawdown_lag_vs_nasdaq": max(
                0.0, nasdaq_drawdown - strategy_drawdown
            ),
            "turnover": float(result["turnover"].sum()),
        }
    observed_months = sorted({
        pd.Timestamp(date).to_period("M").strftime("%Y-%m")
        for date in results[50].index
    })
    decision_months = sorted({
        pd.Timestamp(date).to_period("M").strftime("%Y-%m")
        for date in pd.to_datetime(targets["effective_date"])
    })
    tickers = set(targets["ticker"].astype(str)) - {"__CASH__"}
    gates = {
        "all_observation_months_present": observed_months == list(OBSERVATION_MONTHS),
        "all_decision_months_present": decision_months == list(OBSERVATION_MONTHS),
        "no_forbidden_etf_targets": not bool(tickers & v40.FORBIDDEN_ETFS),
        "positive_excess_vs_nasdaq_30bps": (
            costs["30"]["compounded_excess_vs_nasdaq"] > 0.0
        ),
        "positive_excess_vs_nasdaq_50bps": (
            costs["50"]["compounded_excess_vs_nasdaq"] > 0.0
        ),
        "monthly_wins_vs_nasdaq_50bps": (
            costs["50"]["monthly_wins_vs_nasdaq"] >= MINIMUM_MONTHLY_WINS
        ),
        "drawdown_vs_nasdaq_50bps": (
            costs["50"]["drawdown_lag_vs_nasdaq"] <= MAXIMUM_DRAWDOWN_LAG
        ),
    }
    return {
        "training_years_excluded_from_comparison": list(TRAINING_YEARS),
        "training_years_counted_as_wins": 0,
        "observed_months": observed_months,
        "decision_months": decision_months,
        "costs": costs,
        "gates": gates,
        "all_diagnostic_gates_passed": all(gates.values()),
    }


def diagnose(
    protocol_path: Path = PROTOCOL_PATH,
    output_dir: Path = RESULT_OUTPUT_DIR,
) -> dict:
    protocol, protocol_sha = _validated_protocol(protocol_path)
    output_dir = Path(output_dir)
    if output_dir.exists() and any(output_dir.iterdir()):
        raise RuntimeError(f"v41 output will not be overwritten: {output_dir}")
    inputs = v27._load_inputs()
    base_targets = pd.read_csv(V27_TARGETS, parse_dates=["effective_date"])
    targets, weight_audit = v40.generate_weighted_targets(
        inputs["close"],
        base_targets,
        inverse_volatility_power=INVERSE_VOLATILITY_POWER,
    )
    results = {}
    for cost in COSTS:
        stressed = targets.copy()
        stressed["base_transaction_cost_bps"] = float(cost)
        daily, _ = v40.replay_can_slim_target_schedule(
            inputs["raw_close"],
            inputs["nasdaq"],
            stressed,
            OBSERVATION_START,
            OBSERVATION_END,
        )
        results[cost] = v27._canonicalize_result(
            daily, inputs["nasdaq"], inputs["qqq"]
        )
    evaluation = evaluate_diagnostic(results, targets)

    output_dir.mkdir(parents=True, exist_ok=True)
    targets_path = output_dir / "diagnostic_targets.csv"
    targets.to_csv(targets_path, index=False)
    audit_path = output_dir / "weight_audit.csv"
    audit_export = weight_audit.copy()
    audit_export["selected"] = audit_export["selected"].map(json.dumps)
    audit_export["weighted"] = audit_export["weighted"].map(json.dumps)
    audit_export.to_csv(audit_path, index=False)
    outputs = {
        "diagnostic_targets": _file_binding(targets_path),
        "weight_audit": _file_binding(audit_path),
    }
    for cost in COSTS:
        path = output_dir / f"diagnostic_daily_{cost}bps.csv"
        results[cost].to_csv(path, index_label="date")
        outputs[f"diagnostic_daily_{cost}bps"] = _file_binding(path)
    report = {
        "schema_version": 1,
        "research_only": True,
        "stage": "V41_BLOCKED_CANDIDATE_2026_DIAGNOSTIC_RESULT",
        "protocol": {"path": str(protocol_path), "sha256": protocol_sha},
        "selected_candidate": SELECTED_CANDIDATE,
        "training_source_status": "BLOCKED",
        "evaluation_boundary": protocol["evaluation_boundary"],
        "evaluation": evaluation,
        "diagnostic_status": (
            "PASS_DIAGNOSTIC_ONLY_TRAINING_REMAINS_BLOCKED"
            if evaluation["all_diagnostic_gates_passed"]
            else "BLOCKED"
        ),
        "contains_index_etf_holdings": False,
        "risk_off_asset": "CASH",
        "outputs": outputs,
        "brokerage_or_trading_authorized": False,
        "release_status": "BLOCKED",
        "promotion_eligible": False,
    }
    manifest_path = output_dir / "manifest.json"
    manifest_path.write_text(
        json.dumps(report, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return {**report, "manifest": _file_binding(manifest_path)}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    freeze_parser = subparsers.add_parser("freeze")
    freeze_parser.add_argument("--protocol", type=Path, default=PROTOCOL_PATH)
    diagnose_parser = subparsers.add_parser("diagnose")
    diagnose_parser.add_argument("--protocol", type=Path, default=PROTOCOL_PATH)
    diagnose_parser.add_argument("--output-dir", type=Path, default=RESULT_OUTPUT_DIR)
    args = parser.parse_args()
    report = (
        freeze_protocol(args.protocol)
        if args.command == "freeze"
        else diagnose(args.protocol, args.output_dir)
    )
    fields = (
        ("status", "training_source", "evaluation_boundary", "protocol")
        if args.command == "freeze"
        else (
            "diagnostic_status",
            "training_source_status",
            "evaluation",
            "release_status",
            "manifest",
        )
    )
    print(json.dumps({field: report[field] for field in fields}, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
