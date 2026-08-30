#!/usr/bin/env python3
"""Run the frozen v47 model once on January-July 2026 exposed OOS data.

The exact v47 hybrid model was trained and parameter-selected on 2020-2025 and
has not been replayed on this seven-month period.  Earlier 2026 diagnostics did
motivate the stop architecture, so this is useful researcher-exposed
out-of-sample evidence, not pristine forward evidence.  The protocol and gates
must be frozen before the one-shot replay and cannot authorize brokerage use.
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
from scripts import research_v47_hybrid_entry_portfolio_stop as v47
from scripts import research_v48_isolated_prospective_v47_observation as v48


TRAINING_YEARS = tuple(range(2020, 2026))
OBSERVATION_START = v27.OBSERVATION_START
OBSERVATION_END = v27.OBSERVATION_END
OBSERVATION_MONTHS = v27.OBSERVATION_MONTHS
COSTS = v47.COSTS
MINIMUM_MONTHLY_WINS = 4
MAXIMUM_DRAWDOWN_LAG = 0.10
MAXIMUM_ABSOLUTE_DRAWDOWN = 0.33640750863040947
SELECTED_MODEL = v47.CANDIDATE

OUTPUT_DIR = Path(
    "output/research_only/v49/v47_2026_exposed_oos_20260830"
)
PROTOCOL_PATH = OUTPUT_DIR / "frozen_protocol.json"
RESULT_OUTPUT_DIR = OUTPUT_DIR / "observation_results"
V27_TARGETS = v27.RESULT_OUTPUT_DIR / "observed_targets.csv"
V27_MANIFEST = v27.RESULT_OUTPUT_DIR / "manifest.json"
V47_MANIFEST = v47.DEVELOPMENT_OUTPUT_DIR / "manifest.json"


def _sha256(path: str | Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _file_binding(path: str | Path) -> dict:
    item = Path(path)
    return {"path": str(item), "sha256": _sha256(item)}


def _validate_frozen_model() -> dict:
    protocol = json.loads(v47.PROTOCOL_PATH.read_text(encoding="utf-8"))
    manifest = json.loads(V47_MANIFEST.read_text(encoding="utf-8"))
    boundary = protocol["evaluation_boundary"]
    if manifest.get("development_status") != "PASS":
        raise RuntimeError("v47 development result is no longer PASS")
    if manifest.get("candidate_specification") != v47.candidate_spec():
        raise RuntimeError("v47 selected model changed")
    if manifest.get("2026_used_for_parameter_selection"):
        raise RuntimeError("v47 unexpectedly used 2026 for parameter selection")
    if manifest.get("training_years_counted_as_final_wins"):
        raise RuntimeError("v47 training years were relabeled as final wins")
    if not boundary.get("parameter_isolated_from_2026"):
        raise RuntimeError("v47 parameters are not isolated from 2026")
    if boundary.get("architecture_isolated_from_2026"):
        raise RuntimeError("v47 architecture exposure disclosure changed")
    v48_events = v48.read_ledger(v48.LEDGER_PATH)
    if [event["event_type"] for event in v48_events] != ["PROTOCOL_FROZEN"]:
        raise RuntimeError("v49 must run before any v48 prospective signal")
    return {
        "selected_model": SELECTED_MODEL,
        "candidate_specification": v47.candidate_spec(),
        "parameter_training_years": list(TRAINING_YEARS),
        "parameters_used_2026": False,
        "exact_v47_used_2026_before_v49": False,
        "architecture_isolated_from_2026": False,
        "architecture_exposure_disclosure": (
            "earlier January-July 2026 diagnostics motivated the stop family"
        ),
        "v48_signal_count_at_freeze": 0,
    }


def freeze_protocol(path: str | Path = PROTOCOL_PATH) -> dict:
    item = Path(path)
    if item.exists():
        raise RuntimeError(f"v49 protocol will not be overwritten: {item}")
    runner = Path(__file__).resolve().relative_to(Path.cwd().resolve())
    protocol = {
        "schema_version": 1,
        "research_only": True,
        "stage": "V49_V47_2026_EXPOSED_OOS_PRECOMMITMENT",
        "status": "FROZEN_NOT_EVALUATED",
        "frozen_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "frozen_model": _validate_frozen_model(),
        "evaluation_boundary": {
            "training_years_excluded_from_comparison": list(TRAINING_YEARS),
            "observation_start": OBSERVATION_START,
            "observation_end": OBSERVATION_END,
            "observation_months": list(OBSERVATION_MONTHS),
            "evidence_class": "RESEARCHER_EXPOSED_OUT_OF_SAMPLE",
            "parameter_out_of_sample": True,
            "architecture_out_of_sample": False,
            "pristine_forward_test": False,
            "true_prospective_start": v48.FIRST_PROSPECTIVE_SIGNAL_DATE.strftime(
                "%Y-%m-%d"
            ),
        },
        "precommitted_gates": {
            "positive_excess_vs_nasdaq_at_30bps": True,
            "positive_excess_vs_nasdaq_at_50bps": True,
            "minimum_monthly_wins_vs_nasdaq_at_50bps": MINIMUM_MONTHLY_WINS,
            "maximum_drawdown_lag_vs_nasdaq_percentage_points": (
                MAXIMUM_DRAWDOWN_LAG * 100.0
            ),
            "maximum_absolute_drawdown_percentage_points": (
                MAXIMUM_ABSOLUTE_DRAWDOWN * 100.0
            ),
            "all_observation_months_present": True,
            "all_decision_months_present": True,
            "no_forbidden_etf_targets": True,
        },
        "decision_policy_frozen_before_result": {
            "if_all_gates_pass": (
                "one complete true-prospective month is sufficient for the "
                "first research decision checkpoint"
            ),
            "if_any_gate_fails": (
                "do not shorten the three-complete-month true-prospective "
                "checkpoint"
            ),
            "parameter_changes_after_result_allowed": False,
            "automatic_promotion_allowed": False,
            "broker_action_allowed": False,
        },
        "input_bindings": {
            "runner": _file_binding(runner),
            "v27_observation_helpers": _file_binding(Path(v27.__file__)),
            "v27_manifest": _file_binding(V27_MANIFEST),
            "v27_targets": _file_binding(V27_TARGETS),
            "v47_protocol": _file_binding(v47.PROTOCOL_PATH),
            "v47_manifest": _file_binding(V47_MANIFEST),
            "v47_hybrid_replay": _file_binding(Path(v47.__file__)),
            "v48_protocol": _file_binding(v48.PROTOCOL_PATH),
            "v48_ledger": _file_binding(v48.LEDGER_PATH),
        },
        "cost_bps": list(COSTS),
        "benchmark": "NASDAQ_COMPOSITE",
        "contains_index_etf_holdings": False,
        "brokerage_or_trading_authorized": False,
        "broker_connection_used": False,
        "order_created": False,
        "capital_allocated": False,
        "release_status": "BLOCKED",
        "promotion_eligible": False,
    }
    item.parent.mkdir(parents=True, exist_ok=True)
    item.write_text(
        json.dumps(protocol, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return {**protocol, "protocol": _file_binding(item)}


def _validated_protocol(path: str | Path) -> tuple[dict, str]:
    item = Path(path)
    protocol = json.loads(item.read_text(encoding="utf-8"))
    if protocol.get("status") != "FROZEN_NOT_EVALUATED":
        raise RuntimeError("v49 protocol status changed")
    if protocol.get("frozen_model") != _validate_frozen_model():
        raise RuntimeError("v49 frozen model changed")
    for name, binding in protocol["input_bindings"].items():
        if _sha256(binding["path"]) != binding["sha256"]:
            raise RuntimeError(f"v49 file binding changed for {name}")
    if protocol.get("release_status") != "BLOCKED" or protocol.get(
        "promotion_eligible"
    ):
        raise RuntimeError("v49 release boundary changed")
    return protocol, _sha256(item)


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
            "drawdown_lag_vs_nasdaq": max(
                0.0, nasdaq_drawdown - strategy_drawdown
            ),
            "turnover": float(result["turnover"].sum()),
            "stock_stop_exits": int(result["stock_stop_exits"].sum()),
            "portfolio_stop_exits": int(result["portfolio_stop_exits"].sum()),
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
        "no_forbidden_etf_targets": not bool(tickers & v27.FORBIDDEN_ETFS),
        "positive_excess_vs_nasdaq_30bps": (
            costs["30"]["compounded_excess_vs_nasdaq"] > 0.0
        ),
        "positive_excess_vs_nasdaq_50bps": (
            costs["50"]["compounded_excess_vs_nasdaq"] > 0.0
        ),
        "monthly_wins_vs_nasdaq_50bps": (
            costs["50"]["monthly_wins_vs_nasdaq"] >= MINIMUM_MONTHLY_WINS
        ),
        "drawdown_lag_vs_nasdaq_50bps": (
            costs["50"]["drawdown_lag_vs_nasdaq"] <= MAXIMUM_DRAWDOWN_LAG
        ),
        "absolute_drawdown_50bps": (
            abs(costs["50"]["strategy_maximum_drawdown"])
            <= MAXIMUM_ABSOLUTE_DRAWDOWN
        ),
    }
    return {
        "evidence_class": "RESEARCHER_EXPOSED_OUT_OF_SAMPLE",
        "training_years_excluded_from_comparison": list(TRAINING_YEARS),
        "training_years_counted_as_wins": 0,
        "observed_months": observed_months,
        "decision_months": decision_months,
        "costs": costs,
        "gates": gates,
        "all_precommitted_gates_passed": all(gates.values()),
    }


def observe(
    protocol_path: str | Path = PROTOCOL_PATH,
    output_dir: str | Path = RESULT_OUTPUT_DIR,
) -> dict:
    protocol, protocol_sha = _validated_protocol(protocol_path)
    output_dir = Path(output_dir)
    if output_dir.exists() and any(output_dir.iterdir()):
        raise RuntimeError(f"v49 output will not be overwritten: {output_dir}")
    inputs = v27._load_inputs()
    targets = pd.read_csv(V27_TARGETS, parse_dates=["effective_date"])
    results = {}
    for cost in COSTS:
        daily = v47.replay_with_hybrid_stop(
            inputs["raw_close"],
            inputs["nasdaq"],
            targets,
            OBSERVATION_START,
            OBSERVATION_END,
            entry_loss_fraction=v47.ENTRY_LOSS_FRACTION,
            portfolio_stop_fraction=v47.PORTFOLIO_TRAILING_STOP_FRACTION,
            transaction_cost_bps=float(cost),
        )
        results[cost] = v27._canonicalize_result(
            daily, inputs["nasdaq"], inputs["qqq"]
        )
    evaluation = evaluate_observation(results, targets)
    passed = evaluation["all_precommitted_gates_passed"]

    output_dir.mkdir(parents=True, exist_ok=True)
    outputs = {"observation_targets": _file_binding(V27_TARGETS)}
    for cost in COSTS:
        path = output_dir / f"observed_daily_{cost}bps.csv"
        results[cost].to_csv(path, index_label="date")
        outputs[f"observed_daily_{cost}bps"] = _file_binding(path)
    report = {
        "schema_version": 1,
        "research_only": True,
        "stage": "V49_V47_2026_EXPOSED_OOS_RESULT",
        "protocol": {"path": str(protocol_path), "sha256": protocol_sha},
        "selected_model": SELECTED_MODEL,
        "candidate_specification": v47.candidate_spec(),
        "evaluation_boundary": protocol["evaluation_boundary"],
        "evaluation": evaluation,
        "observation_status": (
            "PASS_EXPOSED_OUT_OF_SAMPLE" if passed else "BLOCKED"
        ),
        "decision_consequence": (
            "ONE_COMPLETE_TRUE_PROSPECTIVE_MONTH_FOR_FIRST_DECISION"
            if passed
            else "THREE_COMPLETE_TRUE_PROSPECTIVE_MONTHS_REMAIN_REQUIRED"
        ),
        "parameters_changed_after_result": False,
        "outputs": outputs,
        "contains_index_etf_holdings": False,
        "brokerage_or_trading_authorized": False,
        "broker_connection_used": False,
        "order_created": False,
        "capital_allocated": False,
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
    observe_parser.add_argument(
        "--output-dir", type=Path, default=RESULT_OUTPUT_DIR
    )
    args = parser.parse_args()
    report = (
        freeze_protocol(args.protocol)
        if args.command == "freeze"
        else observe(args.protocol, args.output_dir)
    )
    print(json.dumps(report, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
