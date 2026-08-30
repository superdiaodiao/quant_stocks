#!/usr/bin/env python3
"""Observe the v33 portfolio stop once on January-July 2026.

The 25% threshold was selected using 2020-2025 only, and those training years
are excluded from the comparison.  The portfolio-stop architecture was created
after earlier 2026 drawdown results had been viewed, so this is threshold-
isolated reused observation evidence rather than a pristine forward test.
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
from scripts import research_v33_portfolio_stop_development as v33


TRAINING_YEARS = tuple(range(2020, 2026))
OBSERVATION_START = v27.OBSERVATION_START
OBSERVATION_END = v27.OBSERVATION_END
OBSERVATION_MONTHS = v27.OBSERVATION_MONTHS
COSTS = v33.COSTS
MAXIMUM_DRAWDOWN_LAG = 0.10
MINIMUM_MONTHLY_WINS = 4
SELECTED_STOP = "portfolio_trailing_stop_25pct"
PORTFOLIO_STOP_FRACTION = 0.25
TRUE_PROSPECTIVE_START = "2026-08-31"

OUTPUT_DIR = Path(
    "output/research_only/v34/portfolio_stop_observation_20260830"
)
PROTOCOL_PATH = OUTPUT_DIR / "frozen_protocol.json"
RESULT_OUTPUT_DIR = OUTPUT_DIR / "observation_results"
V27_TARGETS = v27.RESULT_OUTPUT_DIR / "observed_targets.csv"
V27_MANIFEST = v27.RESULT_OUTPUT_DIR / "manifest.json"
V33_MANIFEST = v33.DEVELOPMENT_OUTPUT_DIR / "manifest.json"


def _sha256(path: Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _file_binding(path: Path) -> dict:
    path = Path(path)
    return {"path": str(path), "sha256": _sha256(path)}


def _validate_training_source() -> dict:
    manifest = json.loads(V33_MANIFEST.read_text(encoding="utf-8"))
    if manifest["development_status"] != "PASS":
        raise RuntimeError("v33 development status changed")
    if manifest["selected_candidate"] != SELECTED_STOP:
        raise RuntimeError("v33 selected portfolio stop changed")
    specification = manifest["selected_specification"]
    if float(specification["portfolio_trailing_stop_fraction"]) != (
        PORTFOLIO_STOP_FRACTION
    ):
        raise RuntimeError("v33 portfolio stop threshold changed")
    if manifest["2026_used_for_threshold_selection"]:
        raise RuntimeError("v33 unexpectedly used 2026 for threshold selection")
    if manifest["training_years_counted_as_final_wins"]:
        raise RuntimeError("v33 training years were counted as final wins")
    if manifest["final_comparison_years"]:
        raise RuntimeError("v33 development has unexpected final comparison years")
    annual_years = tuple(
        int(row["year"])
        for row in manifest["selected_summary"]["costs"]["50"][
            "annual_training_diagnostics"
        ]
    )
    if annual_years != TRAINING_YEARS:
        raise RuntimeError(f"v33 training years changed: {annual_years}")
    return {
        "selected_stop": SELECTED_STOP,
        "portfolio_stop_fraction": PORTFOLIO_STOP_FRACTION,
        "threshold_training_years": list(TRAINING_YEARS),
        "training_years_counted_as_final_wins": False,
        "threshold_used_2026": False,
        "walk_forward_training_pass_count": manifest[
            "walk_forward_pass_count"
        ],
        "all_walk_forward_training_folds_passed": manifest[
            "all_walk_forward_folds_passed"
        ],
    }


def freeze_protocol(path: Path = PROTOCOL_PATH) -> dict:
    path = Path(path)
    if path.exists():
        raise RuntimeError(f"v34 protocol will not be overwritten: {path}")
    runner = Path(__file__).resolve().relative_to(Path.cwd().resolve())
    protocol = {
        "schema_version": 1,
        "research_only": True,
        "stage": "V34_PORTFOLIO_STOP_OBSERVATION_PRECOMMITMENT",
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
                "portfolio stops were introduced after viewing the v32 2026 "
                "drawdown failure"
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
        "risk_policy": {
            "signal": "portfolio NAV drawdown from peak since monthly rebalance",
            "threshold": PORTFOLIO_STOP_FRACTION,
            "execution": "next trading close",
            "sale_scope": "entire stock portfolio",
            "risk_off_asset": "CASH",
            "reentry": "next frozen monthly target only",
        },
        "input_bindings": {
            "runner": _file_binding(runner),
            "v27_observation_helpers": _file_binding(
                Path("scripts/research_v27_stock_only_2026_observation.py")
            ),
            "v27_manifest": _file_binding(V27_MANIFEST),
            "v27_targets": _file_binding(V27_TARGETS),
            "v33_portfolio_stop_helpers": _file_binding(
                Path("scripts/research_v33_portfolio_stop_development.py")
            ),
            "v33_manifest": _file_binding(V33_MANIFEST),
        },
        "parameters_frozen_before_observation": True,
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
    if protocol["status"] != "FROZEN_NOT_OBSERVED":
        raise RuntimeError("v34 protocol status changed")
    if protocol["training_source"] != _validate_training_source():
        raise RuntimeError("v34 training source changed")
    if protocol["evaluation_boundary"]["training_years_excluded_from_comparison"] != list(TRAINING_YEARS):
        raise RuntimeError("v34 training/observation boundary changed")
    for name, binding in protocol["input_bindings"].items():
        if _sha256(Path(binding["path"])) != binding["sha256"]:
            raise RuntimeError(f"v34 file binding changed for {name}")
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
        "no_forbidden_etf_targets": not bool(tickers & v33.FORBIDDEN_ETFS),
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
        "all_precommitted_gates_passed": all(gates.values()),
    }


def observe(
    protocol_path: Path = PROTOCOL_PATH,
    output_dir: Path = RESULT_OUTPUT_DIR,
) -> dict:
    protocol, protocol_sha = _validated_protocol(protocol_path)
    output_dir = Path(output_dir)
    if output_dir.exists() and any(output_dir.iterdir()):
        raise RuntimeError(f"v34 output will not be overwritten: {output_dir}")
    inputs = v27._load_inputs()
    targets = pd.read_csv(V27_TARGETS, parse_dates=["effective_date"])
    results = {}
    for cost in COSTS:
        daily = v33.replay_with_portfolio_trailing_stop(
            inputs["raw_close"],
            inputs["nasdaq"],
            targets,
            OBSERVATION_START,
            OBSERVATION_END,
            portfolio_stop_fraction=PORTFOLIO_STOP_FRACTION,
            transaction_cost_bps=float(cost),
        )
        results[cost] = v27._canonicalize_result(
            daily, inputs["nasdaq"], inputs["qqq"]
        )
    evaluation = evaluate_observation(results, targets)

    output_dir.mkdir(parents=True, exist_ok=True)
    outputs = {}
    for cost in COSTS:
        path = output_dir / f"observed_daily_{cost}bps.csv"
        results[cost].to_csv(path, index_label="date")
        outputs[f"observed_daily_{cost}bps"] = _file_binding(path)
    report = {
        "schema_version": 1,
        "research_only": True,
        "stage": "V34_PORTFOLIO_STOP_OBSERVATION_RESULT",
        "protocol": {"path": str(protocol_path), "sha256": protocol_sha},
        "selected_stop": SELECTED_STOP,
        "evaluation_boundary": protocol["evaluation_boundary"],
        "evaluation": evaluation,
        "observation_status": (
            "PASS_REUSED_THRESHOLD_ISOLATED_OBSERVATION"
            if evaluation["all_precommitted_gates_passed"]
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
