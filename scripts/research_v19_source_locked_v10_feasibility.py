#!/usr/bin/env python3
"""Retrospective 5/5 feasibility study using the pre-existing v10 grid.

This script does not change or rerun any v14-v18 selector, protocol, or gate.
It replays the already-frozen v14 target schedule only to reconstruct exact
10/30/50bps stock-sleeve return paths, reconciles those paths to the frozen
v14 annual cost table, and applies the complete pre-existing v10 contrarian
alpha-budget grid.  All 2022-2026 observations are already human-exposed, so a
5/5 result is an in-sample feasibility result only and cannot authorize
promotion, release, or trading.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

import numpy as np
import pandas as pd

from scripts import research_v15_benchmark_core_development as v15
from src.conf import NASDAQ_INDEX_FILE
from src.research.can_slim import replay_can_slim_target_schedule
from src.research.data_quality import back_adjust_common_splits
from src.research.panel_data import load_panel


START = "2022-01-01"
END = "2026-07-17"
EXPECTED_YEARS = (2022, 2023, 2024, 2025, 2026)
COSTS = (10, 30, 50)
LOOKBACKS = (42, 63, 84)
CROWDED_STOCK_WEIGHTS = (0.10, 0.20, 0.30)
NORMAL_STOCK_WEIGHT = 0.40
OUTPUT_DIR = Path(
    "output/research_only/v19/source_locked_v10_feasibility"
)

SOURCE_V10_SCRIPT = {
    "path": Path("scripts/research_v10_contrarian_alpha_budget.py"),
    "sha256": (
        "7f1ece283cc8800a1614bc4c0384d25d6699151b11ca86600a464b323a3ea2ba"
    ),
}
SOURCE_V10_SUMMARY = {
    "path": Path("output/research_v10_contrarian_alpha_budget_summary.json"),
    "sha256": (
        "82242ada4cc4ac01d9c50b4a15dc0c997cafc0ce9159bc9d24e79ba55bdbea33"
    ),
}
V14_COST_STRESS = {
    "path": Path(
        "output/can_slim_walk_forward_cost_stress_"
        "research_v14_frozen_20260829_one_shot.csv"
    ),
    "sha256": (
        "d70710b74a4d901c4d637d829a57f3235fe649f648a091aa5dc0cec8def08258"
    ),
}


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _verify_binding(name: str, binding: dict) -> dict:
    path = Path(binding["path"])
    actual = _sha256(path)
    if actual != binding["sha256"]:
        raise RuntimeError(f"{name} binding changed: {actual}")
    return {"path": str(path), "sha256": actual}


def _variant_key(lookback: int, crowded_stock_weight: float) -> str:
    return (
        f"lookback_{int(lookback)}_crowded_stock_"
        f"{float(crowded_stock_weight):.2f}"
    )


def source_locked_grid(source_summary: dict) -> tuple[tuple[int, float], ...]:
    """Verify and return every point in the already-emitted v10 grid."""
    if not source_summary.get("historical_selection_contaminated"):
        raise RuntimeError("source v10 contamination label changed")
    if source_summary.get("release_status") != "BLOCKED":
        raise RuntimeError("source v10 release boundary changed")
    if source_summary.get("promotion_eligible"):
        raise RuntimeError("source v10 promotion boundary changed")
    configuration = source_summary["configuration"]
    expected_configuration = {
        "lookback_sessions": 63,
        "normal_v7_stock_weight": NORMAL_STOCK_WEIGHT,
        "crowded_v7_stock_weight": 0.20,
    }
    for field, expected in expected_configuration.items():
        if configuration.get(field) != expected:
            raise RuntimeError(f"source v10 {field} changed")
    expected = tuple(
        (lookback, crowded)
        for lookback in LOOKBACKS
        for crowded in CROWDED_STOCK_WEIGHTS
    )
    expected_keys = {
        _variant_key(lookback, crowded) for lookback, crowded in expected
    }
    actual_keys = set(source_summary["neighbor_sensitivity"])
    if actual_keys != expected_keys:
        raise RuntimeError("source v10 neighbor grid changed")
    return expected


def decision_relative_returns(
    baseline_stock: pd.DataFrame,
    qqq_return: pd.Series,
) -> pd.Series:
    """Reproduce the source v10 daily relative-return decision feature."""
    required = {"strategy"}
    missing = required - set(baseline_stock.columns)
    if missing:
        raise ValueError(f"stock daily columns missing: {sorted(missing)}")
    qqq = qqq_return.reindex(baseline_stock.index)
    if qqq.isna().any():
        raise ValueError("QQQ return is incomplete on the stock path")
    relative = (
        (1.0 + baseline_stock["strategy"].astype(float))
        / (1.0 + qqq.astype(float))
        - 1.0
    )
    relative.name = "stock_relative_to_qqq"
    return relative


def simulate_source_locked_contrarian_sleeves(
    stock: pd.DataFrame,
    qqq_return: pd.Series,
    decision_relative: pd.Series,
    *,
    lookback: int,
    crowded_stock_weight: float,
    normal_stock_weight: float = NORMAL_STOCK_WEIGHT,
    transfer_cost_bps: float,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Apply the source v10 monthly rule to an exact-cost stock sleeve.

    The decision series is fixed from the 10bps v14 baseline for every cost
    stress, so transaction-cost scenarios cannot change which months are
    classified as crowded.  The trailing slice ends before each rebalance
    session, preserving the source rule's no-lookahead behavior.
    """
    required = {"strategy", "benchmark"}
    missing = required - set(stock.columns)
    if missing:
        raise ValueError(f"stock daily columns missing: {sorted(missing)}")
    if lookback <= 0:
        raise ValueError("lookback must be positive")
    if not 0.0 <= crowded_stock_weight <= normal_stock_weight <= 1.0:
        raise ValueError(
            "stock weights must satisfy 0 <= crowded <= normal <= 1"
        )
    if transfer_cost_bps < 0.0:
        raise ValueError("transfer cost must be non-negative")
    frame = stock.sort_index()
    qqq = qqq_return.reindex(frame.index)
    relative = decision_relative.reindex(frame.index)
    if qqq.isna().any() or relative.isna().any():
        raise ValueError("decision or QQQ series is incomplete")

    periods = frame.index.to_period("M")
    nav = 1.0
    stock_value = 0.0
    qqq_value = 0.0
    previous_period = None
    rows = []
    decisions = []
    for position, stamp in enumerate(frame.index):
        previous_nav = nav
        if position:
            stock_value *= 1.0 + float(frame["strategy"].iloc[position])
            qqq_value *= 1.0 + float(qqq.iloc[position])
            nav = stock_value + qqq_value
        turnover = 0.0
        cost = 0.0
        if periods[position] != previous_period:
            trailing = relative.iloc[:position].tail(lookback)
            trailing_alpha = (
                float(np.prod(1.0 + trailing) - 1.0)
                if len(trailing) == lookback
                else None
            )
            crowded = trailing_alpha is not None and trailing_alpha > 0.0
            stock_weight = (
                crowded_stock_weight if crowded else normal_stock_weight
            )
            qqq_weight = 1.0 - stock_weight
            target_stock = nav * stock_weight
            target_qqq = nav * qqq_weight
            turnover = (
                abs(target_stock - stock_value)
                + abs(target_qqq - qqq_value)
            )
            cost = turnover * float(transfer_cost_bps) / 10_000.0
            nav -= cost
            stock_value = nav * stock_weight
            qqq_value = nav - stock_value
            decisions.append({
                "date": pd.Timestamp(stamp),
                "trailing_sessions": int(len(trailing)),
                "trailing_relative_return": trailing_alpha,
                "crowded": bool(crowded),
                "stock_weight": float(stock_weight),
                "qqq_weight": float(qqq_weight),
            })
        rows.append({
            "date": pd.Timestamp(stamp),
            "strategy": nav / previous_nav - 1.0,
            "benchmark": float(frame["benchmark"].iloc[position]),
            "qqq": float(qqq.iloc[position]),
            "turnover": float(turnover),
            "transaction_cost": float(cost),
            "nav": float(nav),
        })
        previous_period = periods[position]
    result = pd.DataFrame(rows).set_index("date")
    result["drawdown"] = result["nav"].div(result["nav"].cummax()).sub(1.0)
    return result, pd.DataFrame(decisions)


def reconcile_v14_cost_paths(
    paths: dict[int, pd.DataFrame],
    frozen_cost_stress: pd.DataFrame,
    *,
    tolerance: float = 1e-12,
) -> dict:
    """Prove exact target replays match every frozen annual v14 result."""
    maximum_error = 0.0
    for cost in COSTS:
        if cost not in paths:
            raise ValueError(f"missing v14 cost path: {cost}")
        annual = (
            (1.0 + paths[cost]["strategy"].astype(float))
            .groupby(paths[cost].index.year)
            .prod()
            - 1.0
        )
        expected = frozen_cost_stress.loc[
            frozen_cost_stress["cost_bps"].astype(float).eq(float(cost))
        ].set_index("test_year")["strategy"]
        if annual.index.astype(int).tolist() != list(EXPECTED_YEARS):
            raise RuntimeError(f"v14 annual envelope changed at {cost}bps")
        if expected.index.astype(int).tolist() != list(EXPECTED_YEARS):
            raise RuntimeError(f"frozen cost table changed at {cost}bps")
        error = float(
            max(
                abs(float(annual.loc[year]) - float(expected.loc[year]))
                for year in EXPECTED_YEARS
            )
        )
        maximum_error = max(maximum_error, error)
        if error > tolerance:
            raise RuntimeError(
                f"v14 {cost}bps target replay mismatch: {error}"
            )
    return {
        "all_cost_paths_reconciled": True,
        "absolute_tolerance": float(tolerance),
        "maximum_absolute_error": maximum_error,
    }


def summarize_variant(results: dict[int, pd.DataFrame]) -> dict:
    """Evaluate the explicit 5/5 Nasdaq feasibility target at every cost."""
    costs = {}
    all_five = True
    for cost in COSTS:
        result = results[cost]
        annual = (
            (1.0 + result[["strategy", "benchmark", "qqq"]])
            .groupby(result.index.year)
            .prod()
            - 1.0
        )
        if annual.index.astype(int).tolist() != list(EXPECTED_YEARS):
            raise RuntimeError(f"v19 annual envelope changed at {cost}bps")
        annual["excess_vs_nasdaq"] = (
            annual["strategy"] - annual["benchmark"]
        )
        annual["excess_vs_qqq"] = annual["strategy"] - annual["qqq"]
        wins = int(annual["excess_vs_nasdaq"].gt(0.0).sum())
        passed = wins == len(EXPECTED_YEARS)
        all_five = all_five and passed
        costs[str(cost)] = {
            "annual": [
                {"year": int(year), **row}
                for year, row in annual.to_dict(orient="index").items()
            ],
            "nasdaq_annual_win_count": wins,
            "required_nasdaq_annual_win_count": len(EXPECTED_YEARS),
            "five_of_five_passed": passed,
            "minimum_annual_excess_vs_nasdaq": float(
                annual["excess_vs_nasdaq"].min()
            ),
            "compounded_strategy": float(
                (1.0 + result["strategy"]).prod() - 1.0
            ),
            "compounded_nasdaq": float(
                (1.0 + result["benchmark"]).prod() - 1.0
            ),
            "compounded_qqq": float((1.0 + result["qqq"]).prod() - 1.0),
            "maximum_drawdown": float(result["drawdown"].min()),
            "turnover": float(result["turnover"].sum()),
        }
    return {
        "all_costs_five_of_five": bool(all_five),
        "costs": costs,
    }


def _replay_exact_v14_cost_paths(
    prices: pd.DataFrame,
    nasdaq: pd.Series,
    targets: pd.DataFrame,
) -> dict[int, pd.DataFrame]:
    paths = {}
    for cost in COSTS:
        stressed = targets.copy()
        stressed["base_transaction_cost_bps"] = float(cost)
        daily, _ = replay_can_slim_target_schedule(
            prices,
            nasdaq,
            stressed,
            START,
            END,
            adjust_splits=False,
        )
        paths[cost] = daily
    return paths


def run(output_dir: Path = OUTPUT_DIR) -> dict:
    bindings = {
        "source_v10_script": _verify_binding(
            "source_v10_script", SOURCE_V10_SCRIPT
        ),
        "source_v10_summary": _verify_binding(
            "source_v10_summary", SOURCE_V10_SUMMARY
        ),
        "v14_protocol": _verify_binding("v14_protocol", v15.V14_PROTOCOL),
        "v14_result": _verify_binding("v14_result", v15.V14_RESULT),
        "v14_targets": _verify_binding("v14_targets", v15.V14_TARGETS),
        "v14_daily": _verify_binding("v14_daily", v15.V14_DAILY),
        "v14_cost_stress": _verify_binding(
            "v14_cost_stress", V14_COST_STRESS
        ),
        "qqq_history": _verify_binding("qqq_history", v15.QQQ_HISTORY),
        "qqq_provenance": _verify_binding(
            "qqq_provenance", v15.QQQ_PROVENANCE
        ),
    }
    source_summary = json.loads(
        SOURCE_V10_SUMMARY["path"].read_text(encoding="utf-8")
    )
    grid = source_locked_grid(source_summary)
    protocol = json.loads(
        v15.V14_PROTOCOL["path"].read_text(encoding="utf-8")
    )
    price_binding = protocol["input_bindings"]["price_directory"]
    close, _ = load_panel(price_binding["path"], "2017-01-01", END)
    prices = back_adjust_common_splits(close)
    nasdaq = pd.read_csv(
        NASDAQ_INDEX_FILE, index_col="date", parse_dates=True
    )["close"]
    qqq = pd.read_csv(
        v15.QQQ_HISTORY["path"], index_col="date", parse_dates=True
    )
    prices[v15.CORE_TICKER] = v15.qqq_total_return_index(
        qqq,
        prices.index,
        allowed_market_closed=nasdaq.reindex(prices.index).isna(),
    )
    qqq_return = (
        prices[v15.CORE_TICKER]
        .pct_change(fill_method=None)
        .fillna(0.0)
    )
    targets = pd.read_csv(
        v15.V14_TARGETS["path"], parse_dates=["effective_date"]
    )
    stock_paths = _replay_exact_v14_cost_paths(prices, nasdaq, targets)
    frozen_cost_stress = pd.read_csv(V14_COST_STRESS["path"])
    reconciliation = reconcile_v14_cost_paths(
        stock_paths, frozen_cost_stress
    )
    decision_relative = decision_relative_returns(
        stock_paths[10], qqq_return
    )

    summaries = {}
    variant_results = {}
    variant_decisions = {}
    grid_rows = []
    for lookback, crowded in grid:
        key = _variant_key(lookback, crowded)
        results = {}
        decisions = None
        for cost in COSTS:
            result, candidate_decisions = (
                simulate_source_locked_contrarian_sleeves(
                    stock_paths[cost],
                    qqq_return,
                    decision_relative,
                    lookback=lookback,
                    crowded_stock_weight=crowded,
                    transfer_cost_bps=float(cost),
                )
            )
            results[cost] = result
            if decisions is None:
                decisions = candidate_decisions
            elif not decisions.equals(candidate_decisions):
                raise RuntimeError(
                    f"cost stress changed decisions for {key}"
                )
        summary = summarize_variant(results)
        summaries[key] = summary
        variant_results[key] = results
        variant_decisions[key] = decisions
        for cost in COSTS:
            cost_summary = summary["costs"][str(cost)]
            grid_rows.append({
                "variant": key,
                "lookback_sessions": int(lookback),
                "normal_stock_weight": NORMAL_STOCK_WEIGHT,
                "crowded_stock_weight": float(crowded),
                "cost_bps": int(cost),
                "nasdaq_annual_win_count": int(
                    cost_summary["nasdaq_annual_win_count"]
                ),
                "five_of_five_passed": bool(
                    cost_summary["five_of_five_passed"]
                ),
                "minimum_annual_excess_vs_nasdaq": float(
                    cost_summary["minimum_annual_excess_vs_nasdaq"]
                ),
                "compounded_strategy": float(
                    cost_summary["compounded_strategy"]
                ),
                "compounded_nasdaq": float(
                    cost_summary["compounded_nasdaq"]
                ),
                "maximum_drawdown": float(
                    cost_summary["maximum_drawdown"]
                ),
                "turnover": float(cost_summary["turnover"]),
            })
    eligible = [
        _variant_key(lookback, crowded)
        for lookback, crowded in grid
        if summaries[_variant_key(lookback, crowded)][
            "all_costs_five_of_five"
        ]
    ]
    selected = eligible[0] if len(eligible) == 1 else None

    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    grid_path = output_dir / "grid_summary.csv"
    pd.DataFrame(grid_rows).to_csv(grid_path, index=False)
    output_paths = {"grid_summary": grid_path}
    if selected is not None:
        decisions_path = output_dir / "selected_decisions.csv"
        variant_decisions[selected].to_csv(decisions_path, index=False)
        output_paths["selected_decisions"] = decisions_path
        for cost, result in variant_results[selected].items():
            path = output_dir / f"selected_daily_{cost}bps.csv"
            result.to_csv(path, index_label="date")
            output_paths[f"selected_daily_{cost}bps"] = path

    historical_fit_status = "PASS" if selected is not None else "BLOCKED"
    report = {
        "schema_version": 1,
        "research_only": True,
        "hypothesis": "V19_SOURCE_LOCKED_V10_CONTRARIAN_GRID_ON_V14",
        "stage": "RETROSPECTIVE_FEASIBILITY_ONLY",
        "historical_selection_contaminated": True,
        "statistically_untouched": False,
        "source_grid_pre_existed_v19": True,
        "new_grid_point_added": False,
        "data_period": {"start": START, "end": END},
        "target": {
            "benchmark": "Nasdaq",
            "required_annual_wins": 5,
            "total_years": 5,
            "required_at_every_cost_bps": list(COSTS),
        },
        "historical_fit_status": historical_fit_status,
        "eligible_variants": eligible,
        "selected_variant": selected,
        "selected_variant_result": (
            summaries[selected] if selected is not None else None
        ),
        "all_variant_results": summaries,
        "v14_cost_path_reconciliation": reconciliation,
        "release_status": "BLOCKED",
        "promotion_eligible": False,
        "formal_financials_modified": False,
        "formal_universe_modified": False,
        "input_bindings": {
            **bindings,
            "price_directory": price_binding,
        },
        "outputs": {
            name: {"path": str(path), "sha256": _sha256(path)}
            for name, path in output_paths.items()
        },
        "interpretation_guardrail": (
            "A 5/5 result here is a retrospectively selected in-sample fit on "
            "repeatedly human-exposed 2022-2026 data. It cannot be called "
            "confirmation, cannot replace v14-v18 evidence, and cannot "
            "authorize release, promotion, brokerage access, or trading."
        ),
    }
    manifest_path = output_dir / "manifest.json"
    manifest_path.write_text(
        json.dumps(report, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return {
        **report,
        "manifest": {
            "path": str(manifest_path),
            "sha256": _sha256(manifest_path),
        },
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, default=OUTPUT_DIR)
    args = parser.parse_args()
    report = run(args.output_dir)
    print(json.dumps({
        "historical_fit_status": report["historical_fit_status"],
        "eligible_variants": report["eligible_variants"],
        "selected_variant": report["selected_variant"],
        "release_status": report["release_status"],
        "promotion_eligible": report["promotion_eligible"],
        "manifest": report["manifest"],
    }, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
