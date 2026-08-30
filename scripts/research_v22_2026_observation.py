#!/usr/bin/env python3
"""Freeze, then execute the v22 2026 model-isolated observation once.

The ``freeze`` command records the selected v22 configuration, exact code and
data bindings, exposure labels, and acceptance gates without calculating any
2026 strategy return.  The ``execute`` command refuses to run unless those
bindings remain exact.  It writes a new result directory once and never
connects to a broker or creates an order.
"""

from __future__ import annotations

import argparse
from datetime import datetime
import hashlib
import json
from pathlib import Path

import pandas as pd

from scripts import research_v15_benchmark_core_development as v15
from scripts import research_v19_source_locked_v10_feasibility as v19
from scripts import research_v20_temporal_retraining as v20
from scripts import research_v22_regularized_walkforward as v22
from src.conf import NASDAQ_INDEX_FILE


OBSERVATION_START = "2026-01-01"
OBSERVATION_END = "2026-07-31"
RESEARCHER_EXPOSED_END = "2026-07-17"
GATE_COSTS = (30, 50)
REQUIRED_MONTHS = tuple(f"2026-{month:02d}" for month in range(1, 8))
MAX_DRAWDOWN_LAG = 0.05
DEVELOPMENT_PROTOCOL_PATH = v22.OUTPUT_DIR / "frozen_protocol.json"
PROTOCOL_DIR = Path(
    "output/research_only/v22/model_isolated_observation_2026_20260830"
)
PROTOCOL_PATH = PROTOCOL_DIR / "frozen_protocol.json"
RESULTS_DIR = PROTOCOL_DIR / "one_shot_results"


def _sha256(path: Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _file_binding(path: Path) -> dict:
    path = Path(path)
    return {"path": str(path), "sha256": _sha256(path)}


def _directory_binding(path: Path, pattern: str = "*.csv") -> dict:
    path = Path(path)
    digest = hashlib.sha256()
    files = sorted(path.glob(pattern))
    for item in files:
        digest.update(item.name.encode("utf-8"))
        digest.update(b"\0")
        digest.update(_sha256(item).encode("ascii"))
        digest.update(b"\n")
    return {
        "path": str(path),
        "pattern": pattern,
        "file_count": len(files),
        "content_manifest_sha256": digest.hexdigest(),
    }


def _date_index(path: Path, column: str) -> pd.DatetimeIndex:
    values = pd.read_csv(path, usecols=[column], parse_dates=[column])[column]
    return pd.DatetimeIndex(values.dropna().sort_values().unique())


def _date_sequence_sha256(index: pd.DatetimeIndex) -> str:
    payload = "\n".join(stamp.strftime("%Y-%m-%d") for stamp in index)
    return hashlib.sha256((payload + "\n").encode("ascii")).hexdigest()


def _validated_development_protocol(path: Path) -> tuple[dict, str]:
    path = Path(path)
    protocol_sha = _sha256(path)
    protocol = json.loads(path.read_text(encoding="utf-8"))
    expected = {
        "lookback_sessions": 84,
        "normal_stock_weight": 0.4,
        "crowded_stock_weight": 0.1,
    }
    if protocol["selected_configuration"] != expected:
        raise RuntimeError("v22 selected configuration changed")
    if protocol["development_status"] != "PASS":
        raise RuntimeError("v22 development gate is not PASS")
    if protocol["walk_forward_status"] != "RETROSPECTIVE_DIAGNOSTIC_PASS":
        raise RuntimeError("v22 walk-forward label changed")
    if protocol["walk_forward_independent_confirmation"]:
        raise RuntimeError("v22 walk-forward cannot be independent confirmation")
    if protocol["development_data"][
        "existing_2026_data_used_for_selection_or_evaluation"
    ]:
        raise RuntimeError("v22 development unexpectedly used 2026")
    if protocol["release_status"] != "BLOCKED" or protocol["promotion_eligible"]:
        raise RuntimeError("v22 release boundary changed")
    if protocol["brokerage_or_trading_authorized"]:
        raise RuntimeError("v22 broker boundary changed")
    implementation = protocol["input_bindings"]["v22_implementation"]
    if _sha256(Path(implementation["path"])) != implementation["sha256"]:
        raise RuntimeError("v22 implementation binding changed")
    return protocol, protocol_sha


def freeze_protocol(
    protocol_path: Path = PROTOCOL_PATH,
    development_protocol_path: Path = DEVELOPMENT_PROTOCOL_PATH,
) -> dict:
    """Freeze observation rules without calculating 2026 performance."""
    protocol_path = Path(protocol_path)
    if protocol_path.exists():
        raise RuntimeError(f"observation protocol will not be overwritten: {protocol_path}")

    development, development_sha = _validated_development_protocol(
        development_protocol_path
    )
    price_binding = _directory_binding(
        Path(development["input_bindings"]["price_directory"]["path"]),
        development["input_bindings"]["price_directory"]["pattern"],
    )
    if price_binding != development["input_bindings"]["price_directory"]:
        raise RuntimeError("price-directory binding changed before observation freeze")

    nasdaq_path = Path(NASDAQ_INDEX_FILE)
    qqq_path = Path(v15.QQQ_HISTORY["path"])
    targets_path = Path(v15.V14_TARGETS["path"])
    nasdaq_dates = _date_index(nasdaq_path, "date")
    qqq_dates = _date_index(qqq_path, "date")
    observation_dates = nasdaq_dates[
        (nasdaq_dates >= pd.Timestamp(OBSERVATION_START))
        & (nasdaq_dates <= pd.Timestamp(OBSERVATION_END))
    ]
    if observation_dates.empty or observation_dates.max() != pd.Timestamp(
        OBSERVATION_END
    ):
        raise RuntimeError("Nasdaq observation window is incomplete")
    if not observation_dates.isin(qqq_dates).all():
        raise RuntimeError("QQQ is missing an observation session")

    target_dates = _date_index(targets_path, "effective_date")
    target_months = tuple(
        str(period)
        for period in target_dates[
            (target_dates >= pd.Timestamp(OBSERVATION_START))
            & (target_dates <= pd.Timestamp(OBSERVATION_END))
        ].to_period("M").unique()
    )
    if target_months != REQUIRED_MONTHS:
        raise RuntimeError(f"unexpected 2026 target months: {target_months}")

    runner_path = Path(__file__).relative_to(Path.cwd())
    report = {
        "schema_version": 1,
        "research_only": True,
        "stage": "V22_2026_MODEL_ISOLATED_OBSERVATION",
        "status": "FROZEN_NOT_EXECUTED",
        "frozen_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "selected_variant": "lookback_84_crowded_stock_0.10",
        "selected_configuration": development["selected_configuration"],
        "parameters_frozen": True,
        "selected_using_2026_algorithmically": False,
        "historical_selection_contaminated": True,
        "researcher_exposure_status": "DISCLOSED_PRIOR_EXPOSURE_THROUGH_2026_07_17",
        "statistically_untouched": False,
        "may_be_called_clean_confirmation": False,
        "observation_window": {
            "start": OBSERVATION_START,
            "end": OBSERVATION_END,
            "common_complete_cutoff": OBSERVATION_END,
            "required_months": list(REQUIRED_MONTHS),
            "required_month_count": len(REQUIRED_MONTHS),
            "expected_session_count": len(observation_dates),
            "expected_session_dates_sha256": _date_sequence_sha256(
                observation_dates
            ),
            "observed_performance_calculated_during_freeze": False,
        },
        "exposure_tiers": [
            {
                "start": OBSERVATION_START,
                "end": RESEARCHER_EXPOSED_END,
                "classification": "MODEL_EXCLUDED_RESEARCHER_EXPOSED",
                "clean_blind_holdout": False,
            },
            {
                "start": "2026-07-18",
                "end": OBSERVATION_END,
                "classification": (
                    "MODEL_EXCLUDED_PREEXISTING_PERFORMANCE_UNINSPECTED_AT_FREEZE"
                ),
                "clean_blind_holdout": False,
            },
            {
                "start": "2026-08-01",
                "end": None,
                "classification": "FUTURE_OPERATIONAL_OBSERVATION",
                "included_in_this_one_shot": False,
            },
        ],
        "acceptance_gates": {
            "gate_cost_bps": list(GATE_COSTS),
            "cumulative_excess_vs_nasdaq_strictly_positive_at_every_gate_cost": True,
            "maximum_drawdown_lag_vs_nasdaq_percentage_points": (
                MAX_DRAWDOWN_LAG * 100.0
            ),
            "required_completed_overlay_decision_months": len(REQUIRED_MONTHS),
            "required_exact_session_calendar": True,
            "parameters_must_remain_frozen": True,
            "qqq_is_secondary_report_only_benchmark": True,
        },
        "precommitted_decision_policy": {
            "if_all_gates_pass": {
                "additional_performance_observation_months_required": 0,
                "minimum_future_operational_dry_run_cycles_required": 1,
                "actual_account_whole_share_validation_required": True,
            },
            "if_any_gate_fails": (
                "KEEP_BLOCKED_AND_DO_NOT_RETUNE_PARAMETERS_ON_2026_RESULTS"
            ),
        },
        "input_bindings": {
            "development_protocol": {
                "path": str(development_protocol_path),
                "sha256": development_sha,
            },
            "observation_runner": _file_binding(runner_path),
            "v22_implementation": _file_binding(
                Path("scripts/research_v22_regularized_walkforward.py")
            ),
            "v19_implementation": _file_binding(
                Path("scripts/research_v19_source_locked_v10_feasibility.py")
            ),
            "v20_implementation": _file_binding(
                Path("scripts/research_v20_temporal_retraining.py")
            ),
            "v14_targets": _file_binding(targets_path),
            "nasdaq_index": _file_binding(nasdaq_path),
            "qqq_history": _file_binding(qqq_path),
            "price_directory": price_binding,
        },
        "brokerage_or_trading_authorized": False,
        "broker_connection_used": False,
        "order_created": False,
        "capital_allocated": False,
        "release_status": "BLOCKED",
        "promotion_eligible": False,
        "interpretation_guardrail": (
            "This is a model-input-isolated but researcher-exposed historical "
            "observation, not clean confirmation. Passing the frozen performance "
            "gates can remove additional performance-only waiting, but one future "
            "operational dry-run cycle and actual-account whole-share validation "
            "remain required. It never authorizes brokerage access or trading."
        ),
    }
    protocol_path.parent.mkdir(parents=True, exist_ok=True)
    protocol_path.write_text(
        json.dumps(report, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return {
        **report,
        "protocol": {"path": str(protocol_path), "sha256": _sha256(protocol_path)},
    }


def _validated_protocol(path: Path) -> tuple[dict, str, pd.DatetimeIndex]:
    path = Path(path)
    protocol_sha = _sha256(path)
    protocol = json.loads(path.read_text(encoding="utf-8"))
    if protocol["status"] != "FROZEN_NOT_EXECUTED":
        raise RuntimeError("observation protocol status changed")
    if not protocol["parameters_frozen"]:
        raise RuntimeError("observation parameters are not frozen")
    if protocol["release_status"] != "BLOCKED" or protocol["promotion_eligible"]:
        raise RuntimeError("observation release boundary changed")
    if (
        protocol["brokerage_or_trading_authorized"]
        or protocol["broker_connection_used"]
        or protocol["order_created"]
        or protocol["capital_allocated"]
    ):
        raise RuntimeError("observation broker boundary changed")

    for name, binding in protocol["input_bindings"].items():
        if name == "price_directory":
            actual = _directory_binding(
                Path(binding["path"]), binding["pattern"]
            )
            if actual != binding:
                raise RuntimeError("observation price-directory binding changed")
            continue
        actual_sha = _sha256(Path(binding["path"]))
        if actual_sha != binding["sha256"]:
            raise RuntimeError(f"observation binding changed for {name}: {actual_sha}")

    dates = _date_index(
        Path(protocol["input_bindings"]["nasdaq_index"]["path"]), "date"
    )
    dates = dates[
        (dates >= pd.Timestamp(protocol["observation_window"]["start"]))
        & (dates <= pd.Timestamp(protocol["observation_window"]["end"]))
    ]
    if len(dates) != protocol["observation_window"]["expected_session_count"]:
        raise RuntimeError("observation session count changed")
    if _date_sequence_sha256(dates) != protocol["observation_window"][
        "expected_session_dates_sha256"
    ]:
        raise RuntimeError("observation session calendar changed")
    return protocol, protocol_sha, dates


def _compounded(series: pd.Series) -> float:
    return float((1.0 + series.astype(float)).prod() - 1.0)


def _maximum_drawdown(series: pd.Series) -> float:
    nav = (1.0 + series.astype(float)).cumprod()
    return float(nav.div(nav.cummax()).sub(1.0).min())


def _period_metrics(frame: pd.DataFrame) -> dict:
    strategy = _compounded(frame["strategy"])
    nasdaq = _compounded(frame["benchmark"])
    qqq = _compounded(frame["qqq"])
    strategy_drawdown = _maximum_drawdown(frame["strategy"])
    nasdaq_drawdown = _maximum_drawdown(frame["benchmark"])
    return {
        "strategy": strategy,
        "nasdaq": nasdaq,
        "qqq": qqq,
        "excess_vs_nasdaq": strategy - nasdaq,
        "excess_vs_qqq": strategy - qqq,
        "strategy_maximum_drawdown": strategy_drawdown,
        "nasdaq_maximum_drawdown": nasdaq_drawdown,
        "drawdown_lag_vs_nasdaq": max(
            0.0, nasdaq_drawdown - strategy_drawdown
        ),
        "session_count": len(frame),
    }


def evaluate_observation(
    results: dict[int, pd.DataFrame],
    decisions: pd.DataFrame,
    protocol: dict,
    expected_dates: pd.DatetimeIndex,
) -> dict:
    window = protocol["observation_window"]
    start = window["start"]
    end = window["end"]
    expected_months = tuple(window["required_months"])
    observed_decisions = decisions.loc[
        decisions["date"].between(pd.Timestamp(start), pd.Timestamp(end))
    ].copy()
    decision_months = tuple(
        str(period)
        for period in pd.DatetimeIndex(observed_decisions["date"]).to_period("M")
    )
    decision_gate = decision_months == expected_months

    costs = {}
    calendar_gate = True
    for cost in v19.COSTS:
        frame = results[cost].loc[start:end].copy()
        exact_calendar = frame.index.equals(expected_dates)
        calendar_gate = calendar_gate and exact_calendar
        metrics = _period_metrics(frame)
        excess_gate = metrics["excess_vs_nasdaq"] > 0.0
        drawdown_gate = metrics["drawdown_lag_vs_nasdaq"] <= MAX_DRAWDOWN_LAG
        is_gate_cost = cost in GATE_COSTS
        costs[str(cost)] = {
            **metrics,
            "exact_session_calendar": exact_calendar,
            "is_acceptance_gate_cost": is_gate_cost,
            "positive_excess_gate": excess_gate if is_gate_cost else None,
            "drawdown_gate": drawdown_gate if is_gate_cost else None,
            "all_cost_gates_passed": (
                excess_gate and drawdown_gate if is_gate_cost else None
            ),
        }

    tier_metrics = []
    for tier in protocol["exposure_tiers"]:
        if not tier.get("included_in_this_one_shot", True):
            continue
        row = {**tier, "costs": {}}
        for cost in v19.COSTS:
            frame = results[cost].loc[tier["start"]:tier["end"]]
            row["costs"][str(cost)] = _period_metrics(frame)
        tier_metrics.append(row)

    cost_gate = all(
        costs[str(cost)]["all_cost_gates_passed"] for cost in GATE_COSTS
    )
    passed = bool(calendar_gate and decision_gate and cost_gate)
    return {
        "all_precommitted_gates_passed": passed,
        "data_gates": {
            "exact_session_calendar": bool(calendar_gate),
            "completed_overlay_decision_months": len(decision_months),
            "decision_months": list(decision_months),
            "required_decision_months": list(expected_months),
            "decision_month_gate": bool(decision_gate),
        },
        "costs": costs,
        "exposure_tier_diagnostics": tier_metrics,
    }


def _simulate_selected(
    protocol: dict,
) -> tuple[dict[int, pd.DataFrame], pd.DataFrame]:
    price_binding = protocol["input_bindings"]["price_directory"]
    stock_paths, qqq_return = v20._load_replay_inputs(
        end=protocol["observation_window"]["end"],
        price_binding=price_binding,
    )
    config = protocol["selected_configuration"]
    decision_relative = v19.decision_relative_returns(
        stock_paths[10], qqq_return
    )
    results = {}
    expected_decisions = None
    for cost in v19.COSTS:
        result, decisions = v19.simulate_source_locked_contrarian_sleeves(
            stock_paths[cost],
            qqq_return,
            decision_relative,
            lookback=int(config["lookback_sessions"]),
            crowded_stock_weight=float(config["crowded_stock_weight"]),
            normal_stock_weight=float(config["normal_stock_weight"]),
            transfer_cost_bps=float(cost),
        )
        if expected_decisions is None:
            expected_decisions = decisions
        elif not expected_decisions.equals(decisions):
            raise RuntimeError("transaction cost changed observation decisions")
        results[cost] = result
    return results, expected_decisions


def execute(
    protocol_path: Path = PROTOCOL_PATH,
    output_dir: Path = RESULTS_DIR,
) -> dict:
    protocol, protocol_sha, expected_dates = _validated_protocol(protocol_path)
    output_dir = Path(output_dir)
    if output_dir.exists() and any(output_dir.iterdir()):
        raise RuntimeError(f"observation output will not be overwritten: {output_dir}")

    results, decisions = _simulate_selected(protocol)
    evaluation = evaluate_observation(
        results, decisions, protocol, expected_dates
    )
    passed = evaluation["all_precommitted_gates_passed"]

    output_dir.mkdir(parents=True, exist_ok=True)
    outputs = {}
    start = protocol["observation_window"]["start"]
    end = protocol["observation_window"]["end"]
    for cost in v19.COSTS:
        path = output_dir / f"observation_daily_{cost}bps.csv"
        results[cost].loc[start:end].to_csv(path, index_label="date")
        outputs[f"observation_daily_{cost}bps"] = _file_binding(path)
    decisions_path = output_dir / "observation_decisions.csv"
    decisions.loc[
        decisions["date"].between(pd.Timestamp(start), pd.Timestamp(end))
    ].to_csv(decisions_path, index=False)
    outputs["observation_decisions"] = _file_binding(decisions_path)

    if passed:
        decision = protocol["precommitted_decision_policy"]["if_all_gates_pass"]
        additional_months = decision[
            "additional_performance_observation_months_required"
        ]
        operational_cycles = decision[
            "minimum_future_operational_dry_run_cycles_required"
        ]
    else:
        decision = protocol["precommitted_decision_policy"]["if_any_gate_fails"]
        additional_months = None
        operational_cycles = None

    report = {
        "schema_version": 1,
        "research_only": True,
        "stage": "V22_2026_MODEL_ISOLATED_OBSERVATION_RESULT",
        "executed_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "protocol": {"path": str(protocol_path), "sha256": protocol_sha},
        "selected_variant": protocol["selected_variant"],
        "selected_configuration": protocol["selected_configuration"],
        "parameters_unchanged": True,
        "observation_status": "PASS" if passed else "BLOCKED",
        "observation_result": evaluation,
        "precommitted_decision": decision,
        "additional_performance_observation_months_required": additional_months,
        "minimum_future_operational_dry_run_cycles_required": operational_cycles,
        "actual_account_whole_share_validation_status": "PENDING",
        "operational_shadow_status": "NOT_STARTED",
        "researcher_exposure_status": protocol["researcher_exposure_status"],
        "statistically_untouched": False,
        "may_be_called_clean_confirmation": False,
        "brokerage_or_trading_authorized": False,
        "broker_connection_used": False,
        "order_created": False,
        "capital_allocated": False,
        "release_status": "BLOCKED",
        "promotion_eligible": False,
        "outputs": outputs,
        "interpretation_guardrail": (
            "PASS means only that the frozen seven-month historical performance "
            "gates passed. It is not clean confirmation and does not authorize "
            "trading. A future operational dry-run cycle and actual-account "
            "whole-share validation remain separate requirements."
        ),
    }
    manifest_path = output_dir / "manifest.json"
    manifest_path.write_text(
        json.dumps(report, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return {
        **report,
        "manifest": {"path": str(manifest_path), "sha256": _sha256(manifest_path)},
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    freeze_parser = subparsers.add_parser("freeze")
    freeze_parser.add_argument("--protocol", type=Path, default=PROTOCOL_PATH)
    freeze_parser.add_argument(
        "--development-protocol",
        type=Path,
        default=DEVELOPMENT_PROTOCOL_PATH,
    )
    execute_parser = subparsers.add_parser("execute")
    execute_parser.add_argument("--protocol", type=Path, default=PROTOCOL_PATH)
    execute_parser.add_argument("--output-dir", type=Path, default=RESULTS_DIR)
    args = parser.parse_args()

    if args.command == "freeze":
        report = freeze_protocol(args.protocol, args.development_protocol)
        summary = {
            "status": report["status"],
            "selected_variant": report["selected_variant"],
            "observed_performance_calculated_during_freeze": report[
                "observation_window"
            ]["observed_performance_calculated_during_freeze"],
            "protocol": report["protocol"],
        }
    else:
        report = execute(args.protocol, args.output_dir)
        summary = {
            "observation_status": report["observation_status"],
            "additional_performance_observation_months_required": report[
                "additional_performance_observation_months_required"
            ],
            "minimum_future_operational_dry_run_cycles_required": report[
                "minimum_future_operational_dry_run_cycles_required"
            ],
            "release_status": report["release_status"],
            "manifest": report["manifest"],
        }
    print(json.dumps(summary, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
