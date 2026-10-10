#!/usr/bin/env python3
"""Execute and score the frozen v16 historical confirmation exactly once."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from tempfile import TemporaryDirectory

import numpy as np
import pandas as pd

from scripts.research_v16_freeze_protocol import build as build_protocol
from scripts.research_v16_trend_confirmed_qqq_development import (
    CORE_TICKER,
    trend_confirmed_target_schedule,
)
from src.conf import NASDAQ_INDEX_FILE
from src.research.can_slim import replay_can_slim_target_schedule
from src.research.data_quality import back_adjust_common_splits
from src.research.panel_data import load_panel


PROTOCOL_PATH = Path("output/research_only/v16/frozen_protocol_20260829.json")
OUTPUT_DIR = Path("output/research_only/v16/frozen_confirmation_20260829")
START = "2022-01-01"
DEVELOPMENT_END = "2024-12-31"
CONFIRMATION_START = "2025-01-01"
END = "2026-07-17"


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _compound(returns: pd.Series) -> float:
    return float((1.0 + returns.astype(float)).prod() - 1.0)


def _max_drawdown(returns: pd.Series) -> float:
    wealth = (1.0 + returns.astype(float)).cumprod()
    return float((wealth / wealth.cummax() - 1.0).min())


def _annual(result: pd.DataFrame) -> pd.DataFrame:
    annual = (1.0 + result[["strategy", "benchmark"]]).groupby(
        result.index.year
    ).prod() - 1.0
    annual["excess_vs_nasdaq"] = annual["strategy"] - annual["benchmark"]
    return annual


def evaluate_frozen_gates(
    *,
    results: dict[int, pd.DataFrame],
    removed_daily: pd.DataFrame,
    contributions: pd.DataFrame,
    gates: dict,
) -> dict:
    """Evaluate only confirmation rules recorded before result execution."""
    expected_costs = [10, 30, 50]
    expected_years = [2022, 2023, 2024, 2025, 2026]
    confirmation_years = [2025, 2026]
    rows = {}
    all_pass = True
    for cost in expected_costs:
        result = results[cost]
        annual = _annual(result)
        if annual.index.astype(int).tolist() != expected_years:
            raise RuntimeError(f"v16 annual envelope changed at {cost}bps")
        confirmation = annual.loc[confirmation_years]
        confirmation_wins = int(confirmation["excess_vs_nasdaq"].gt(0).sum())
        confirmation_required = int(
            gates["confirmation_annual_excess_win_count"][f"{cost}_bps"][
                "required"
            ]
        )
        full_wins = int(annual["excess_vs_nasdaq"].gt(0).sum())
        full_required = int(
            gates["full_history_annual_excess_win_count"][f"{cost}_bps"][
                "required"
            ]
        )
        confirmation_daily = result.loc[CONFIRMATION_START:END]
        confirmation_strategy = _compound(confirmation_daily["strategy"])
        confirmation_nasdaq = _compound(confirmation_daily["benchmark"])
        confirmation_excess = confirmation_strategy - confirmation_nasdaq
        full_strategy = _compound(result["strategy"])
        full_nasdaq = _compound(result["benchmark"])
        full_excess = full_strategy - full_nasdaq
        passed_confirmation_wins = confirmation_wins >= confirmation_required
        passed_full_wins = full_wins >= full_required
        passed_confirmation_compounded = confirmation_excess > float(
            gates["confirmation_compounded_excess"]["threshold"]
        )
        passed_full_compounded = full_excess > float(
            gates["full_history_compounded_excess"]["threshold"]
        )
        all_pass = all_pass and all((
            passed_confirmation_wins,
            passed_full_wins,
            passed_confirmation_compounded,
            passed_full_compounded,
        ))
        rows[str(cost)] = {
            "annual": [
                {"year": int(year), **values}
                for year, values in annual.to_dict(orient="index").items()
            ],
            "confirmation_annual_win_count": confirmation_wins,
            "required_confirmation_annual_win_count": confirmation_required,
            "confirmation_annual_win_gate_passed": passed_confirmation_wins,
            "full_history_annual_win_count": full_wins,
            "required_full_history_annual_win_count": full_required,
            "full_history_annual_win_gate_passed": passed_full_wins,
            "confirmation_compounded_strategy": confirmation_strategy,
            "confirmation_compounded_nasdaq": confirmation_nasdaq,
            "confirmation_compounded_excess": confirmation_excess,
            "confirmation_compounded_gate_passed": (
                passed_confirmation_compounded
            ),
            "full_history_compounded_strategy": full_strategy,
            "full_history_compounded_nasdaq": full_nasdaq,
            "full_history_compounded_excess": full_excess,
            "full_history_compounded_gate_passed": passed_full_compounded,
        }

    primary = results[10]
    strategy_drawdown = _max_drawdown(primary["strategy"])
    nasdaq_drawdown = _max_drawdown(primary["benchmark"])
    underperformance_pp = max(
        0.0, (abs(strategy_drawdown) - abs(nasdaq_drawdown)) * 100.0
    )
    drawdown_gate = gates["drawdown"]
    passed_absolute_drawdown = abs(strategy_drawdown) <= float(
        drawdown_gate["maximum_loss_fraction"]
    )
    passed_relative_drawdown = underperformance_pp <= float(
        drawdown_gate[
            "maximum_underperformance_vs_nasdaq_percentage_points"
        ]
    )
    all_pass = all_pass and passed_absolute_drawdown and passed_relative_drawdown

    if contributions.empty:
        raise RuntimeError("v16 single-name attribution is empty")
    largest = contributions.sort_values(
        ["net_return_contribution", "ticker"],
        ascending=[False, True],
    ).iloc[0]
    removed_strategy = _compound(removed_daily["strategy"])
    removed_nasdaq = _compound(removed_daily["benchmark"])
    removed_excess = removed_strategy - removed_nasdaq
    passed_leave_one_out = removed_excess > float(
        gates["leave_one_out"]["compounded_excess_threshold"]
    )
    all_pass = all_pass and passed_leave_one_out
    return {
        "all_predeclared_gates_passed": bool(all_pass),
        "costs": rows,
        "drawdown": {
            "strategy_max_drawdown": strategy_drawdown,
            "nasdaq_max_drawdown": nasdaq_drawdown,
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
            "removed_compounded_strategy": removed_strategy,
            "removed_compounded_nasdaq": removed_nasdaq,
            "removed_compounded_excess": removed_excess,
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
            "frozen v16 protocol does not match current bindings: "
            f"{actual_sha} != {regenerated_sha}"
        )
    if actual.get("historical_confirmation_executed") is not False:
        raise RuntimeError("v16 protocol is not in pre-execution state")
    if actual.get("confirmation_results_inspected") is not False:
        raise RuntimeError("v16 protocol already records inspected results")
    return actual, actual_sha


def _reconcile_development(
    results: dict[int, pd.DataFrame],
    development_bindings: dict,
) -> None:
    columns = ["strategy", "benchmark", "invested", "turnover", "holdings"]
    for cost, result in results.items():
        binding = development_bindings[f"daily_sma_50_{cost}bps"]
        expected = pd.read_csv(
            binding["path"], index_col="date", parse_dates=True
        )
        actual = result.loc[:DEVELOPMENT_END]
        if not actual.index.equals(expected.index) or not np.allclose(
            actual[columns], expected[columns], rtol=0.0, atol=1e-12
        ):
            raise RuntimeError(
                f"v16 frozen replay does not reconcile {cost}bps development"
            )


def execute(
    protocol_path: Path = PROTOCOL_PATH,
    output_dir: Path = OUTPUT_DIR,
) -> dict:
    protocol, protocol_sha = _validated_protocol(Path(protocol_path))
    output_dir = Path(output_dir)
    if output_dir.exists() and any(output_dir.iterdir()):
        raise RuntimeError(
            "one-shot v16 confirmation output already exists and will not be "
            f"overwritten: {output_dir}"
        )
    inputs = protocol["input_bindings"]
    targets = pd.read_csv(
        inputs["v14_targets"]["path"], parse_dates=["effective_date"]
    )
    close, _ = load_panel(
        inputs["price_directory"]["path"], "2017-01-01", END
    )
    prices = back_adjust_common_splits(close)
    nasdaq = pd.read_csv(
        NASDAQ_INDEX_FILE, index_col="date", parse_dates=True
    )["close"]
    qqq = pd.read_csv(
        inputs["qqq_history"]["path"], index_col="date", parse_dates=True
    )
    from scripts.research_v15_benchmark_core_development import (
        qqq_total_return_index,
    )

    prices[CORE_TICKER] = qqq_total_return_index(
        qqq,
        prices.index,
        allowed_market_closed=nasdaq.reindex(prices.index).isna(),
    )
    schedule = trend_confirmed_target_schedule(
        targets,
        qqq["close"],
        prices.index,
        lookback=int(protocol["frozen_parameter"]["qqq_sma_sessions"]),
        end=END,
    )
    results: dict[int, pd.DataFrame] = {}
    baseline_contributions = pd.DataFrame()
    for cost in (10, 30, 50):
        stressed = schedule.copy()
        stressed["base_transaction_cost_bps"] = float(cost)
        result, contributions = replay_can_slim_target_schedule(
            prices,
            nasdaq,
            stressed,
            START,
            END,
            adjust_splits=False,
        )
        results[cost] = result
        if cost == 10:
            baseline_contributions = contributions
    _reconcile_development(
        results, inputs["v16_development_selected_outputs"]
    )
    largest_ticker = str(baseline_contributions.iloc[0]["ticker"])
    baseline_targets = schedule.copy()
    baseline_targets["base_transaction_cost_bps"] = 10.0
    removed_daily, _ = replay_can_slim_target_schedule(
        prices,
        nasdaq,
        baseline_targets,
        START,
        END,
        excluded_tickers=(largest_ticker,),
        adjust_splits=False,
    )
    gates = evaluate_frozen_gates(
        results=results,
        removed_daily=removed_daily,
        contributions=baseline_contributions,
        gates=protocol["predeclared_gates"],
    )

    output_dir.mkdir(parents=True, exist_ok=True)
    schedule_path = output_dir / "frozen_targets.csv"
    attribution_path = output_dir / "single_name_attribution.csv"
    removed_path = output_dir / "largest_name_removed_daily.csv"
    schedule.to_csv(schedule_path, index=False)
    baseline_contributions.to_csv(attribution_path, index=False)
    removed_daily.to_csv(removed_path, index_label="date")
    artifact_paths = {
        "targets": schedule_path,
        "single_name_attribution": attribution_path,
        "largest_name_removed_daily": removed_path,
    }
    for cost, result in results.items():
        path = output_dir / f"daily_{cost}bps.csv"
        result.to_csv(path, index_label="date")
        artifact_paths[f"daily_{cost}bps"] = path

    report = {
        "schema_version": 1,
        "research_only": True,
        "protocol_status": "FROZEN_V16_HISTORICAL_CONFIRMATION_EXECUTED",
        "policy_status": "RESEARCH_PRETRAINING_ONLY_UNFROZEN",
        "release_status": "BLOCKED",
        "promotion_eligible": False,
        "formal_financials_modified": False,
        "formal_universe_modified": False,
        "historical_confirmation_executed": True,
        "confirmation_results_inspected": True,
        "protocol_binding": {
            "path": str(protocol_path),
            "sha256": protocol_sha,
        },
        "frozen_parameter": protocol["frozen_parameter"],
        "development_reconciled": True,
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
            "Even a historical pass cannot authorize promotion or trading. "
            "The 2025-2026 interval was previously exposed to humans and is "
            "not a statistically untouched holdout."
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
        "development_reconciled": report["development_reconciled"],
        "release_status": report["release_status"],
        "promotion_eligible": report["promotion_eligible"],
        "manifest": report["manifest"],
    }, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
