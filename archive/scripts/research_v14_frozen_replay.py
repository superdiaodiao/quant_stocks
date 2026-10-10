#!/usr/bin/env python3
"""Execute and score the v14 frozen research protocol exactly once."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from tempfile import TemporaryDirectory

import numpy as np
import pandas as pd

from scripts.research_v14_adaptive_pretrain import run as adaptive_run
from scripts.research_v14_freeze_protocol import build as build_protocol
from src.conf import NASDAQ_INDEX_FILE
from src.research.can_slim import replay_can_slim_target_schedule
from src.research.panel_data import load_panel


PROTOCOL_PATH = Path("output/research_only/v14/frozen_protocol_20260829.json")
OUTPUT_DIR = Path("output/research_only/v14/frozen_replay_20260829")
ARTIFACT_TAG = "research_v14_frozen_20260829_one_shot"
START = "2022-01-01"
END = "2026-07-17"


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _compound(returns: pd.Series) -> float:
    return float((1.0 + returns.astype(float)).prod() - 1.0)


def _max_drawdown(returns: pd.Series) -> float:
    wealth = (1.0 + returns.astype(float)).cumprod()
    return float((wealth / wealth.cummax() - 1.0).min())


def evaluate_frozen_gates(
    *,
    cost_stress: pd.DataFrame,
    daily: pd.DataFrame,
    removed_daily: pd.DataFrame,
    contributions: pd.DataFrame,
    gates: dict,
) -> dict:
    """Evaluate only the thresholds already recorded in the protocol."""
    expected_costs = [10, 30, 50]
    expected_years = [2022, 2023, 2024, 2025, 2026]
    observed_costs = sorted(cost_stress["cost_bps"].astype(int).unique())
    if observed_costs != expected_costs:
        raise RuntimeError(f"cost-stress envelope changed: {observed_costs}")
    rows = {}
    all_pass = True
    for cost_bps in expected_costs:
        group = cost_stress.loc[
            cost_stress["cost_bps"].astype(int).eq(cost_bps)
        ].sort_values("test_year")
        if group["test_year"].astype(int).tolist() != expected_years:
            raise RuntimeError(f"annual envelope changed at {cost_bps}bps")
        wins = int(group["excess_vs_nasdaq"].gt(0).sum())
        win_gate = gates["annual_excess_win_count"][f"{cost_bps}_bps"]
        compounded_strategy = _compound(group["strategy"])
        compounded_nasdaq = _compound(group["nasdaq"])
        compounded_excess = compounded_strategy - compounded_nasdaq
        passed_wins = wins >= int(win_gate["required"])
        passed_compounded = compounded_excess > float(
            gates["compounded_excess"]["threshold"]
        )
        rows[str(cost_bps)] = {
            "annual_win_count": wins,
            "required_annual_win_count": int(win_gate["required"]),
            "annual_win_gate_passed": passed_wins,
            "compounded_strategy": compounded_strategy,
            "compounded_nasdaq": compounded_nasdaq,
            "compounded_excess": compounded_excess,
            "compounded_excess_gate_passed": passed_compounded,
        }
        all_pass = all_pass and passed_wins and passed_compounded

    strategy_drawdown = _max_drawdown(daily["strategy"])
    benchmark_drawdown = _max_drawdown(daily["benchmark"])
    underperformance_pp = max(
        0.0, (abs(strategy_drawdown) - abs(benchmark_drawdown)) * 100.0
    )
    drawdown_gate = gates["drawdown"]
    passed_absolute_drawdown = abs(strategy_drawdown) <= float(
        drawdown_gate["maximum_loss_fraction"]
    )
    passed_relative_drawdown = underperformance_pp <= float(
        drawdown_gate["maximum_underperformance_vs_nasdaq_percentage_points"]
    )
    all_pass = all_pass and passed_absolute_drawdown and passed_relative_drawdown

    if contributions.empty:
        raise RuntimeError("single-name attribution is empty")
    largest = contributions.sort_values(
        ["net_return_contribution", "ticker"],
        ascending=[False, True],
    ).iloc[0]
    removed_compounded_strategy = _compound(removed_daily["strategy"])
    removed_compounded_nasdaq = _compound(removed_daily["benchmark"])
    removed_compounded_excess = (
        removed_compounded_strategy - removed_compounded_nasdaq
    )
    leave_one_out_gate = gates["leave_one_out"]
    passed_leave_one_out = removed_compounded_excess > float(
        leave_one_out_gate["compounded_excess_threshold"]
    )
    all_pass = all_pass and passed_leave_one_out

    return {
        "all_predeclared_gates_passed": bool(all_pass),
        "annual_and_compounded": rows,
        "drawdown": {
            "strategy_max_drawdown": strategy_drawdown,
            "nasdaq_max_drawdown": benchmark_drawdown,
            "underperformance_vs_nasdaq_percentage_points": underperformance_pp,
            "absolute_gate_passed": passed_absolute_drawdown,
            "relative_gate_passed": passed_relative_drawdown,
        },
        "leave_one_out": {
            "removed_ticker": str(largest["ticker"]),
            "selection_metric": "largest net arithmetic daily return attribution",
            "removed_weight_behavior": "leave as cash; do not renormalize",
            "baseline_net_return_contribution": float(
                largest["net_return_contribution"]
            ),
            "removed_compounded_strategy": removed_compounded_strategy,
            "removed_compounded_nasdaq": removed_compounded_nasdaq,
            "removed_compounded_excess": removed_compounded_excess,
            "gate_passed": passed_leave_one_out,
        },
    }


def _validated_protocol(path: Path) -> tuple[dict, str]:
    actual_sha = _sha256(path)
    actual = json.loads(path.read_text(encoding="utf-8"))
    with TemporaryDirectory() as temporary:
        regenerated_path = Path(temporary) / "protocol.json"
        regenerated = build_protocol(regenerated_path)
        regenerated_sha = regenerated["output"]["sha256"]
    if actual_sha != regenerated_sha:
        raise RuntimeError(
            f"frozen protocol does not match current bindings: "
            f"{actual_sha} != {regenerated_sha}"
        )
    if actual.get("final_data_replay_executed") is not False:
        raise RuntimeError("protocol is not in pre-execution state")
    if actual.get("results_inspected") is not False:
        raise RuntimeError("protocol already records inspected results")
    return actual, actual_sha


def execute(
    protocol_path: Path = PROTOCOL_PATH,
    output_dir: Path = OUTPUT_DIR,
) -> dict:
    protocol, protocol_sha = _validated_protocol(Path(protocol_path))
    output_dir = Path(output_dir)
    if output_dir.exists() and any(output_dir.iterdir()):
        raise RuntimeError(
            f"one-shot replay output already exists and will not be overwritten: "
            f"{output_dir}"
        )

    inputs = protocol["input_bindings"]
    summary = adaptive_run(
        quarterly_path=Path(inputs["candidate"]["quarterly"]["path"]),
        snapshot_dir=Path(inputs["universe_snapshots"]["path"]),
        data_audit_path=Path(inputs["audit"]["summary"]["path"]),
        output_dir=output_dir,
        price_dir=Path(inputs["price_directory"]["path"]),
        artifact_tag=ARTIFACT_TAG,
        excluded_signal_dates=tuple(
            protocol["execution"]["excluded_signal_dates"]
        ),
    )

    daily_path = Path(f"output/can_slim_walk_forward_daily_{ARTIFACT_TAG}.csv")
    targets_path = Path(
        f"output/can_slim_walk_forward_targets_{ARTIFACT_TAG}.csv"
    )
    cost_path = Path(
        f"output/can_slim_walk_forward_cost_stress_{ARTIFACT_TAG}.csv"
    )
    rankings_path = Path(
        f"output/can_slim_walk_forward_rankings_{ARTIFACT_TAG}.csv"
    )
    daily = pd.read_csv(daily_path, index_col="date", parse_dates=True)
    targets = pd.read_csv(targets_path, parse_dates=["effective_date"])
    cost_stress = pd.read_csv(cost_path)

    close, _ = load_panel(
        inputs["price_directory"]["path"], "2017-11-28", END
    )
    nasdaq = pd.read_csv(
        NASDAQ_INDEX_FILE, index_col="date", parse_dates=True
    )["close"]
    replayed, contributions = replay_can_slim_target_schedule(
        close, nasdaq, targets, START, END
    )
    if not np.allclose(
        daily[["strategy", "benchmark", "invested", "turnover", "holdings"]],
        replayed[["strategy", "benchmark", "invested", "turnover", "holdings"]],
        rtol=0.0,
        atol=1e-12,
    ):
        raise RuntimeError("frozen target replay does not reconcile daily output")
    largest_ticker = str(contributions.iloc[0]["ticker"])
    removed_daily, _ = replay_can_slim_target_schedule(
        close,
        nasdaq,
        targets,
        START,
        END,
        excluded_tickers=(largest_ticker,),
    )
    gates = evaluate_frozen_gates(
        cost_stress=cost_stress,
        daily=daily,
        removed_daily=removed_daily,
        contributions=contributions,
        gates=protocol["predeclared_gates"],
    )

    output_dir.mkdir(parents=True, exist_ok=True)
    contributions_path = output_dir / "single_name_attribution.csv"
    removed_path = output_dir / "largest_name_removed_daily.csv"
    contributions.to_csv(contributions_path, index=False)
    removed_daily.to_csv(removed_path, index_label="date")
    artifact_paths = {
        "candidate_annual_results": Path(summary["outputs"]["candidate_annual_results"]),
        "walk_forward_annual": Path(summary["outputs"]["walk_forward_annual"]),
        "adaptive_summary": Path(summary["outputs"]["summary"]),
        "daily": daily_path,
        "targets": targets_path,
        "cost_stress": cost_path,
        "rankings": rankings_path,
        "single_name_attribution": contributions_path,
        "largest_name_removed_daily": removed_path,
    }
    report = {
        "schema_version": 1,
        "research_only": True,
        "protocol_status": "FROZEN_RESEARCH_PROTOCOL_EXECUTED",
        "policy_status": "RESEARCH_PRETRAINING_ONLY_UNFROZEN",
        "release_status": "BLOCKED",
        "promotion_eligible": False,
        "formal_financials_modified": False,
        "formal_universe_modified": False,
        "final_data_replay_executed": True,
        "results_inspected": True,
        "protocol_binding": {
            "path": str(protocol_path),
            "sha256": protocol_sha,
        },
        "historical_gate_status": (
            "PASS" if gates["all_predeclared_gates_passed"] else "BLOCKED"
        ),
        "gates": gates,
        "data_exposure": protocol["data_split"],
        "outputs": {
            name: {"path": str(path), "sha256": _sha256(path)}
            for name, path in artifact_paths.items()
        },
        "interpretation_guardrail": (
            "Historical gates cannot authorize promotion or trading. The "
            "2025-2026 data were previously exposed, and only a new future "
            "forward phase can be genuinely untouched."
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
    parser.add_argument("--protocol", type=Path, default=PROTOCOL_PATH)
    parser.add_argument("--output-dir", type=Path, default=OUTPUT_DIR)
    args = parser.parse_args()
    report = execute(args.protocol, args.output_dir)
    print(json.dumps({
        "historical_gate_status": report["historical_gate_status"],
        "all_predeclared_gates_passed": report["gates"][
            "all_predeclared_gates_passed"
        ],
        "release_status": report["release_status"],
        "promotion_eligible": report["promotion_eligible"],
        "manifest": report["manifest"],
    }, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
