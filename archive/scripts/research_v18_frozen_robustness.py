#!/usr/bin/env python3
"""Execute the frozen, explicitly contaminated v18 robustness replay once."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from tempfile import TemporaryDirectory

import numpy as np
import pandas as pd

from scripts.research_v18_freeze_protocol import build as build_protocol
from scripts.research_v18_source_locked_v7_core_development import (
    CORE_TICKER,
    source_locked_core_satellite_targets,
)
from src.conf import NASDAQ_INDEX_FILE
from src.research.can_slim import replay_can_slim_target_schedule
from src.research.data_quality import back_adjust_common_splits
from src.research.panel_data import load_panel


PROTOCOL_PATH = Path("output/research_only/v18/frozen_protocol_20260829.json")
OUTPUT_DIR = Path("output/research_only/v18/frozen_robustness_20260829")
START = "2022-01-01"
DEVELOPMENT_END = "2024-12-31"
POST_DEVELOPMENT_START = "2025-01-01"
END = "2026-07-17"


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _compound(returns: pd.Series) -> float:
    return float((1.0 + returns.astype(float)).prod() - 1.0)


def _max_drawdown(returns: pd.Series) -> float:
    wealth = (1.0 + returns.astype(float)).cumprod()
    return float((wealth / wealth.cummax() - 1.0).min())


def _annual(result: pd.DataFrame, qqq_return: pd.Series) -> pd.DataFrame:
    joined = pd.DataFrame({
        "strategy": result["strategy"],
        "nasdaq": result["benchmark"],
        "qqq": qqq_return.reindex(result.index),
    })
    if joined["qqq"].isna().any():
        raise RuntimeError("QQQ return is incomplete in robustness replay")
    annual = (1.0 + joined).groupby(joined.index.year).prod() - 1.0
    annual["excess_vs_nasdaq"] = annual["strategy"] - annual["nasdaq"]
    annual["excess_vs_qqq"] = annual["strategy"] - annual["qqq"]
    return annual


def evaluate_frozen_gates(
    *,
    results: dict[int, pd.DataFrame],
    qqq_return: pd.Series,
    removed_daily: pd.DataFrame,
    contributions: pd.DataFrame,
    gates: dict,
) -> dict:
    expected_years = [2022, 2023, 2024, 2025, 2026]
    post_years = [2025, 2026]
    rows = {}
    all_pass = True
    for cost in (10, 30, 50):
        result = results[cost]
        annual = _annual(result, qqq_return)
        if annual.index.astype(int).tolist() != expected_years:
            raise RuntimeError(f"v18 annual envelope changed at {cost}bps")
        post = annual.loc[post_years]
        full_nasdaq_wins = int(annual["excess_vs_nasdaq"].gt(0.0).sum())
        full_qqq_wins = int(annual["excess_vs_qqq"].gt(0.0).sum())
        post_nasdaq_wins = int(post["excess_vs_nasdaq"].gt(0.0).sum())
        required_post = int(
            gates["post_development_nasdaq_annual_win_count"][f"{cost}_bps"][
                "required"
            ]
        )
        required_full_nasdaq = int(
            gates["full_history_nasdaq_annual_win_count"][f"{cost}_bps"][
                "required"
            ]
        )
        required_full_qqq = int(
            gates["full_history_qqq_annual_win_count"][f"{cost}_bps"][
                "required"
            ]
        )
        post_daily = result.loc[POST_DEVELOPMENT_START:END]
        post_qqq = qqq_return.reindex(post_daily.index)
        post_strategy_compound = _compound(post_daily["strategy"])
        post_nasdaq_compound = _compound(post_daily["benchmark"])
        post_qqq_compound = _compound(post_qqq)
        full_strategy_compound = _compound(result["strategy"])
        full_nasdaq_compound = _compound(result["benchmark"])
        full_qqq_compound = _compound(qqq_return.reindex(result.index))
        passed = {
            "post_nasdaq_wins": post_nasdaq_wins >= required_post,
            "full_nasdaq_wins": full_nasdaq_wins >= required_full_nasdaq,
            "full_qqq_wins": full_qqq_wins >= required_full_qqq,
            "post_compounded_vs_nasdaq": (
                post_strategy_compound - post_nasdaq_compound
                > gates["post_development_compounded_excess"]["threshold"]
            ),
            "post_compounded_vs_qqq": (
                post_strategy_compound - post_qqq_compound
                > gates["post_development_compounded_excess"]["threshold"]
            ),
            "full_compounded_vs_nasdaq": (
                full_strategy_compound - full_nasdaq_compound
                > gates["full_history_compounded_excess"]["threshold"]
            ),
            "full_compounded_vs_qqq": (
                full_strategy_compound - full_qqq_compound
                > gates["full_history_compounded_excess"]["threshold"]
            ),
        }
        all_pass = all_pass and all(passed.values())
        rows[str(cost)] = {
            "annual": [
                {"year": int(year), **values}
                for year, values in annual.to_dict(orient="index").items()
            ],
            "post_development_nasdaq_annual_win_count": post_nasdaq_wins,
            "required_post_development_nasdaq_annual_win_count": required_post,
            "full_history_nasdaq_annual_win_count": full_nasdaq_wins,
            "required_full_history_nasdaq_annual_win_count": (
                required_full_nasdaq
            ),
            "full_history_qqq_annual_win_count": full_qqq_wins,
            "required_full_history_qqq_annual_win_count": required_full_qqq,
            "post_development_compounded_strategy": post_strategy_compound,
            "post_development_compounded_nasdaq": post_nasdaq_compound,
            "post_development_compounded_qqq": post_qqq_compound,
            "full_history_compounded_strategy": full_strategy_compound,
            "full_history_compounded_nasdaq": full_nasdaq_compound,
            "full_history_compounded_qqq": full_qqq_compound,
            "gate_results": passed,
            "all_cost_gates_passed": all(passed.values()),
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

    satellites = contributions.loc[~contributions["ticker"].eq(CORE_TICKER)]
    if satellites.empty:
        raise RuntimeError("v18 satellite attribution is empty")
    largest = satellites.sort_values(
        ["net_return_contribution", "ticker"], ascending=[False, True]
    ).iloc[0]
    removed_strategy = _compound(removed_daily["strategy"])
    removed_nasdaq = _compound(removed_daily["benchmark"])
    removed_qqq = _compound(qqq_return.reindex(removed_daily.index))
    passed_removed_nasdaq = (
        removed_strategy - removed_nasdaq
        > gates["leave_one_satellite_out"]["compounded_excess_threshold"]
    )
    passed_removed_qqq = (
        removed_strategy - removed_qqq
        > gates["leave_one_satellite_out"]["compounded_excess_threshold"]
    )
    all_pass = all_pass and passed_removed_nasdaq and passed_removed_qqq
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
        "leave_one_satellite_out": {
            "removed_ticker": str(largest["ticker"]),
            "selection_metric": (
                "largest non-QQQ net arithmetic daily return attribution"
            ),
            "removed_weight_behavior": "leave as cash; do not renormalize",
            "baseline_net_return_contribution": float(
                largest["net_return_contribution"]
            ),
            "removed_compounded_strategy": removed_strategy,
            "removed_compounded_nasdaq": removed_nasdaq,
            "removed_compounded_qqq": removed_qqq,
            "excess_vs_nasdaq": removed_strategy - removed_nasdaq,
            "excess_vs_qqq": removed_strategy - removed_qqq,
            "nasdaq_gate_passed": passed_removed_nasdaq,
            "qqq_gate_passed": passed_removed_qqq,
        },
    }


def _validated_protocol(path: Path) -> tuple[dict, str]:
    actual_sha = _sha256(path)
    actual = json.loads(path.read_text(encoding="utf-8"))
    with TemporaryDirectory() as temporary:
        regenerated = build_protocol(Path(temporary) / "protocol.json")
    if actual_sha != regenerated["output"]["sha256"]:
        raise RuntimeError("v18 frozen protocol does not match current bindings")
    if actual["historical_robustness_replay_executed"]:
        raise RuntimeError("v18 protocol is not in pre-execution state")
    if actual["post_development_results_inspected"]:
        raise RuntimeError("v18 protocol already records inspected results")
    return actual, actual_sha


def _reconcile_development(
    results: dict[int, pd.DataFrame], bindings: dict
) -> None:
    columns = ["strategy", "benchmark", "invested", "turnover", "holdings"]
    for cost, result in results.items():
        expected = pd.read_csv(
            bindings[f"daily_{cost}bps"]["path"],
            index_col="date",
            parse_dates=True,
        )
        actual = result.loc[:DEVELOPMENT_END]
        if not actual.index.equals(expected.index) or not np.allclose(
            actual[columns], expected[columns], rtol=0.0, atol=1e-12
        ):
            raise RuntimeError(
                f"v18 replay does not reconcile {cost}bps development"
            )


def execute(
    protocol_path: Path = PROTOCOL_PATH,
    output_dir: Path = OUTPUT_DIR,
) -> dict:
    protocol, protocol_sha = _validated_protocol(Path(protocol_path))
    output_dir = Path(output_dir)
    if output_dir.exists() and any(output_dir.iterdir()):
        raise RuntimeError(
            "one-shot v18 robustness output already exists and will not be "
            f"overwritten: {output_dir}"
        )
    inputs = protocol["input_bindings"]
    targets = pd.read_csv(
        inputs["v14_targets"]["path"], parse_dates=["effective_date"]
    )
    blended_targets = source_locked_core_satellite_targets(targets, end=END)
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
    qqq_return = prices[CORE_TICKER].pct_change(fill_method=None).fillna(0.0)
    results = {}
    contributions = pd.DataFrame()
    for cost in (10, 30, 50):
        stressed = blended_targets.copy()
        stressed["base_transaction_cost_bps"] = float(cost)
        result, attribution = replay_can_slim_target_schedule(
            prices,
            nasdaq,
            stressed,
            START,
            END,
            adjust_splits=False,
        )
        results[cost] = result
        if cost == 10:
            contributions = attribution
    _reconcile_development(
        results, inputs["v18_development_outputs"]
    )
    satellites = contributions.loc[~contributions["ticker"].eq(CORE_TICKER)]
    largest_satellite = str(satellites.iloc[0]["ticker"])
    baseline_targets = blended_targets.copy()
    baseline_targets["base_transaction_cost_bps"] = 10.0
    removed_daily, _ = replay_can_slim_target_schedule(
        prices,
        nasdaq,
        baseline_targets,
        START,
        END,
        excluded_tickers=(largest_satellite,),
        adjust_splits=False,
    )
    gates = evaluate_frozen_gates(
        results=results,
        qqq_return=qqq_return,
        removed_daily=removed_daily,
        contributions=contributions,
        gates=protocol["predeclared_gates"],
    )

    output_dir.mkdir(parents=True, exist_ok=True)
    targets_path = output_dir / "frozen_targets.csv"
    contributions_path = output_dir / "single_name_attribution.csv"
    removed_path = output_dir / "largest_satellite_removed_daily.csv"
    blended_targets.to_csv(targets_path, index=False)
    contributions.to_csv(contributions_path, index=False)
    removed_daily.to_csv(removed_path, index_label="date")
    artifact_paths = {
        "targets": targets_path,
        "single_name_attribution": contributions_path,
        "largest_satellite_removed_daily": removed_path,
    }
    for cost, result in results.items():
        path = output_dir / f"daily_{cost}bps.csv"
        result.to_csv(path, index_label="date")
        artifact_paths[f"daily_{cost}bps"] = path
    report = {
        "schema_version": 1,
        "research_only": True,
        "protocol_status": "FROZEN_V18_CONTAMINATED_ROBUSTNESS_EXECUTED",
        "historical_selection_contaminated": True,
        "statistically_untouched": False,
        "release_status": "BLOCKED",
        "promotion_eligible": False,
        "formal_financials_modified": False,
        "formal_universe_modified": False,
        "historical_robustness_replay_executed": True,
        "post_development_results_inspected": True,
        "development_reconciled": True,
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
            "A robustness pass cannot be called confirmation and cannot "
            "authorize release. Every post-development year was repeatedly "
            "human-exposed before this replay."
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
