#!/usr/bin/env python3
"""Observe the v28 stop threshold outside its 2020-2025 training years.

The 25% threshold was selected without 2026 returns, so 2020-2025 are excluded
from every comparison in this report.  The trailing-stop architecture itself
was motivated by the already-viewed v27 drawdown, therefore this is threshold-
isolated reused observation evidence, not a pristine forward test.
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
from scripts import research_v28_stock_trailing_stop_development as v28


TRAINING_YEARS = tuple(range(2020, 2026))
OBSERVATION_START = v27.OBSERVATION_START
OBSERVATION_END = v27.OBSERVATION_END
OBSERVATION_MONTHS = v27.OBSERVATION_MONTHS
COSTS = v28.COSTS
MAXIMUM_DRAWDOWN_LAG = 0.10
MINIMUM_MONTHLY_WINS = 4
SELECTED_STOP = "individual_trailing_stop_25pct"
TRAILING_STOP_FRACTION = 0.25
TRUE_PROSPECTIVE_START = "2026-08-31"

OUTPUT_DIR = Path(
    "output/research_only/v32/model_isolated_stop_observation_20260830"
)
PROTOCOL_PATH = OUTPUT_DIR / "frozen_protocol.json"
RESULT_OUTPUT_DIR = OUTPUT_DIR / "observation_results"
V27_TARGETS = v27.RESULT_OUTPUT_DIR / "observed_targets.csv"
V27_MANIFEST = v27.RESULT_OUTPUT_DIR / "manifest.json"
V28_MANIFEST = v28.DEVELOPMENT_OUTPUT_DIR / "manifest.json"


def _sha256(path: Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _file_binding(path: Path) -> dict:
    path = Path(path)
    return {"path": str(path), "sha256": _sha256(path)}


def _validate_training_source() -> dict:
    manifest = json.loads(V28_MANIFEST.read_text(encoding="utf-8"))
    if manifest["selected_candidate"] != SELECTED_STOP:
        raise RuntimeError("v28 selected stop changed")
    spec = manifest["selected_specification"]
    if float(spec["trailing_stop_fraction"]) != TRAILING_STOP_FRACTION:
        raise RuntimeError("v28 stop threshold changed")
    if manifest["2026_used_for_development_or_selection"]:
        raise RuntimeError("v28 unexpectedly used 2026 for threshold selection")
    annual_years = tuple(
        int(row["year"])
        for row in manifest["selected_summary"]["costs"]["50"]["annual"]
    )
    if annual_years != TRAINING_YEARS:
        raise RuntimeError(f"v28 training years changed: {annual_years}")
    return {
        "selected_stop": SELECTED_STOP,
        "trailing_stop_fraction": TRAILING_STOP_FRACTION,
        "threshold_training_years": list(TRAINING_YEARS),
        "threshold_used_2026": False,
    }


def freeze_protocol(path: Path = PROTOCOL_PATH) -> dict:
    path = Path(path)
    if path.exists():
        raise RuntimeError(f"v32 protocol will not be overwritten: {path}")
    runner = Path(__file__).resolve().relative_to(Path.cwd().resolve())
    protocol = {
        "schema_version": 1,
        "research_only": True,
        "stage": "V32_MODEL_ISOLATED_STOP_OBSERVATION_PRECOMMITMENT",
        "status": "FROZEN_NOT_OBSERVED",
        "frozen_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "training_source": _validate_training_source(),
        "evaluation_boundary": {
            "training_years_excluded_from_comparison": list(TRAINING_YEARS),
            "observation_start": OBSERVATION_START,
            "observation_end": OBSERVATION_END,
            "observation_months": list(OBSERVATION_MONTHS),
            "threshold_isolated_from_2026": True,
            "architecture_isolated_from_2026": False,
            "architecture_contamination_reason": (
                "v28 trailing stops were introduced after viewing the v27 "
                "2026 drawdown failure"
            ),
            "pristine_forward_test": False,
            "true_prospective_start": TRUE_PROSPECTIVE_START,
        },
        "precommitted_gates": {
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
            "v28_trailing_stop_helpers": _file_binding(
                Path("scripts/research_v28_stock_trailing_stop_development.py")
            ),
            "v27_manifest": _file_binding(V27_MANIFEST),
            "v27_targets": _file_binding(V27_TARGETS),
            "v28_manifest": _file_binding(V28_MANIFEST),
        },
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
    if protocol["status"] != "FROZEN_NOT_OBSERVED":
        raise RuntimeError("v32 protocol status changed")
    if protocol["training_source"] != _validate_training_source():
        raise RuntimeError("v32 training source changed")
    for name, binding in protocol["input_bindings"].items():
        if _sha256(Path(binding["path"])) != binding["sha256"]:
            raise RuntimeError(f"v32 file binding changed for {name}")
    return protocol, protocol_sha


def _maximum_drawdown(returns: pd.Series) -> float:
    return v23._maximum_drawdown(returns)


def evaluate_observation(
    results: dict[int, pd.DataFrame],
    targets: pd.DataFrame,
) -> dict:
    costs = {}
    for cost, result in results.items():
        monthly = (
            (1.0 + result[["strategy", "benchmark", "qqq"]])
            .groupby(result.index.to_period("M"))
            .prod()
            - 1.0
        )
        monthly["excess_vs_nasdaq"] = monthly["strategy"] - monthly["benchmark"]
        strategy = float((1.0 + result["strategy"]).prod() - 1.0)
        nasdaq = float((1.0 + result["benchmark"]).prod() - 1.0)
        qqq = float((1.0 + result["qqq"]).prod() - 1.0)
        strategy_drawdown = _maximum_drawdown(result["strategy"])
        nasdaq_drawdown = _maximum_drawdown(result["benchmark"])
        qqq_drawdown = _maximum_drawdown(result["qqq"])
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
            "compounded_qqq": qqq,
            "compounded_excess_vs_nasdaq": strategy - nasdaq,
            "compounded_excess_vs_qqq": strategy - qqq,
            "strategy_maximum_drawdown": strategy_drawdown,
            "nasdaq_maximum_drawdown": nasdaq_drawdown,
            "qqq_maximum_drawdown": qqq_drawdown,
            "drawdown_lag_vs_nasdaq": max(
                0.0, nasdaq_drawdown - strategy_drawdown
            ),
            "turnover": float(result["turnover"].sum()),
            "stop_exits": int(result["stop_exits"].sum()),
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
        "no_forbidden_etf_targets": not bool(tickers & v28.FORBIDDEN_ETFS),
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
        "observed_months": observed_months,
        "decision_months": decision_months,
        "costs": costs,
        "gates": gates,
        "all_precommitted_gates_passed": all(gates.values()),
    }


def observe(
    protocol_path: Path = PROTOCOL_PATH,
    output_dir: Path = RESULT_OUTPUT_DIR,
) -> dict:
    protocol, protocol_sha = _validated_protocol(protocol_path)
    output_dir = Path(output_dir)
    if output_dir.exists() and any(output_dir.iterdir()):
        raise RuntimeError(f"v32 output will not be overwritten: {output_dir}")
    inputs = v27._load_inputs()
    targets = pd.read_csv(V27_TARGETS, parse_dates=["effective_date"])
    results = {}
    for cost in COSTS:
        daily = v28.replay_with_individual_trailing_stop(
            inputs["raw_close"],
            inputs["nasdaq"],
            targets,
            OBSERVATION_START,
            OBSERVATION_END,
            trailing_stop_fraction=TRAILING_STOP_FRACTION,
            transaction_cost_bps=float(cost),
        )
        results[cost] = v27._canonicalize_result(
            daily, inputs["nasdaq"], inputs["qqq"]
        )
    evaluation = evaluate_observation(results, targets)

    output_dir.mkdir(parents=True, exist_ok=True)
    outputs = {}
    for cost in COSTS:
        result_path = output_dir / f"observed_daily_{cost}bps.csv"
        results[cost].to_csv(result_path, index_label="date")
        outputs[f"observed_daily_{cost}bps"] = _file_binding(result_path)
    report = {
        "schema_version": 1,
        "research_only": True,
        "stage": "V32_MODEL_ISOLATED_STOP_OBSERVATION_RESULT",
        "protocol": {"path": str(protocol_path), "sha256": protocol_sha},
        "selected_stop": SELECTED_STOP,
        "evaluation_boundary": protocol["evaluation_boundary"],
        "evaluation": evaluation,
        "observation_status": (
            "PASS_REUSED_MODEL_ISOLATED_OBSERVATION"
            if evaluation["all_precommitted_gates_passed"]
            else "BLOCKED"
        ),
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
    observe_parser = subparsers.add_parser("observe")
    observe_parser.add_argument("--protocol", type=Path, default=PROTOCOL_PATH)
    observe_parser.add_argument("--output-dir", type=Path, default=RESULT_OUTPUT_DIR)
    args = parser.parse_args()
    report = (
        freeze_protocol(args.protocol)
        if args.command == "freeze"
        else observe(args.protocol, args.output_dir)
    )
    fields = (
        ("status", "training_source", "evaluation_boundary", "protocol")
        if args.command == "freeze"
        else (
            "observation_status",
            "evaluation_boundary",
            "evaluation",
            "release_status",
            "manifest",
        )
    )
    print(json.dumps({field: report[field] for field in fields}, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
