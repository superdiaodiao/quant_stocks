#!/usr/bin/env python3
"""Fit a conservative v22 overlay and freeze a future-only shadow protocol.

This is a new research generation after the v20 recent holdout was inspected.
The 2025 result is deliberately reclassified as development data; it is never
presented as an untouched holdout for v22.  The source-locked nine-point v10
grid is retained, while a conservative near-best rule regularizes selection
toward a smaller crowded-regime stock sleeve and a slower lookback.

The overlay is diagnosed with expanding, next-year pseudo-out-of-sample folds:

* train on 2022, test 2023;
* train on 2022-2023, test 2024;
* train on 2022-2024, test 2025.

The final forward configuration is selected on 2022-2025 and may observe only
market data after 2026-08-30.  Earlier 2026 data is intentionally excluded.
No broker connection, account access, order, or capital allocation occurs.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

import pandas as pd

from scripts import research_v19_source_locked_v10_feasibility as v19
from scripts import research_v20_temporal_retraining as v20


START = "2022-01-01"
DEVELOPMENT_END = "2025-12-31"
DEVELOPMENT_YEARS = (2022, 2023, 2024, 2025)
WALK_FORWARD_FOLDS = (
    ((2022,), 2023),
    ((2022, 2023), 2024),
    ((2022, 2023, 2024), 2025),
)
NEAR_BEST_TOLERANCE = 0.015
PRIMARY_TOTAL_COST_BPS = 30.0
ROBUSTNESS_STRESS_COST_BPS = 50.0
FORWARD_DATA_START_EXCLUSIVE = "2026-08-30"
FROZEN_AT = "2026-08-30T11:38:44+08:00"
OUTPUT_DIR = Path(
    "output/research_only/v22/regularized_walkforward_20260830"
)

INPUT_BINDINGS = {
    "v20_protocol": {
        "path": v20.OUTPUT_DIR / "frozen_protocol.json",
        "sha256": (
            "1301bdd763d5ef34923fe067a8484c86cc6bf0676ac7bd6e7ca1e139b8992cc1"
        ),
    },
    "v20_holdout": {
        "path": Path(
            "output/research_only/v20/recent_holdout_20260830/manifest.json"
        ),
        "sha256": (
            "b2f19203709d3dbc7b088e1a72604a048ee83569251d49cb7f44f947a743fe3e"
        ),
    },
    "v21_ibkr_calibration": {
        "path": Path(
            "output/research_only/v21/ibkr_cost_calibration_20260830/manifest.json"
        ),
        "sha256": (
            "527064217750d3663b1360e5adc2c7f645e870a7400be49785b71815cceb4a14"
        ),
    },
    "v19_implementation": {
        "path": Path("scripts/research_v19_source_locked_v10_feasibility.py"),
        "sha256": (
            "589ea011565044abb0926d581e8e5150d9326871601b3b7c2fb9a73a02543987"
        ),
    },
    "v20_implementation": {
        "path": Path("scripts/research_v20_temporal_retraining.py"),
        "sha256": (
            "6b0edf102216e3db4779a63aea1b38a4f8e5a3a1fad12f2ece8b7f8e9dfece86"
        ),
    },
    "v20_holdout_implementation": {
        "path": Path("scripts/research_v20_recent_holdout.py"),
        "sha256": (
            "c12c8767e067367d9a4b56c371b3d87b64742fa067c373cb761037614e480ac8"
        ),
    },
    "v21_implementation": {
        "path": Path("scripts/research_v21_ibkr_cost_calibration.py"),
        "sha256": (
            "61dca8fd054ca83a86a827d4eaf5a77b63c87440dddd0e06e9ca002d1a501caf"
        ),
    },
    "ibkr_cost_implementation": {
        "path": Path("src/research/ibkr_cost_calibration.py"),
        "sha256": (
            "2af6b57559b8a1d098743773a63b3f3ef24052aa5cb8d447aca4b7873b617f6e"
        ),
    },
}


def _sha256(path: Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _verify_binding(name: str, binding: dict) -> dict:
    path = Path(binding["path"])
    actual = _sha256(path)
    if actual != binding["sha256"]:
        raise RuntimeError(f"{name} binding changed: {actual}")
    return {"path": str(path), "sha256": actual}


def _verify_inputs() -> tuple[dict, dict, dict, dict]:
    bindings = {
        name: _verify_binding(name, binding)
        for name, binding in INPUT_BINDINGS.items()
    }
    protocol = json.loads(
        INPUT_BINDINGS["v20_protocol"]["path"].read_text(encoding="utf-8")
    )
    holdout = json.loads(
        INPUT_BINDINGS["v20_holdout"]["path"].read_text(encoding="utf-8")
    )
    calibration = json.loads(
        INPUT_BINDINGS["v21_ibkr_calibration"]["path"].read_text(
            encoding="utf-8"
        )
    )
    if protocol["selected_variant"] != "lookback_84_crowded_stock_0.20":
        raise RuntimeError("v20 selected variant changed")
    if holdout["recent_holdout_status"] != "BLOCKED":
        raise RuntimeError("v20 holdout result changed")
    if holdout["release_status"] != "BLOCKED" or holdout["promotion_eligible"]:
        raise RuntimeError("v20 holdout release boundary changed")
    if calibration["release_status"] != "BLOCKED":
        raise RuntimeError("v21 calibration release boundary changed")
    if (
        calibration["broker_connection_used"]
        or calibration["broker_account_accessed"]
        or calibration["order_created"]
    ):
        raise RuntimeError("v21 no-broker boundary changed")
    return bindings, protocol, holdout, calibration


def _configuration_from_key(key: str) -> tuple[int, float]:
    prefix = "lookback_"
    middle = "_crowded_stock_"
    if not key.startswith(prefix) or middle not in key:
        raise ValueError(f"invalid source-locked variant key: {key}")
    lookback_text, crowded_text = key[len(prefix):].split(middle, maxsplit=1)
    return int(lookback_text), float(crowded_text)


def _slice_results(
    results: dict[int, pd.DataFrame],
    years: tuple[int, ...],
) -> dict[int, pd.DataFrame]:
    expected = set(years)
    sliced = {}
    for cost in v19.COSTS:
        frame = results[cost].loc[results[cost].index.year.isin(expected)].copy()
        actual = tuple(sorted(set(frame.index.year.astype(int))))
        if actual != years:
            raise RuntimeError(
                f"variant is missing requested years at {cost}bps: {actual}"
            )
        sliced[cost] = frame
    return sliced


def regularized_select_variant(
    variant_results: dict[str, dict[int, pd.DataFrame]],
    *,
    years: tuple[int, ...],
    near_best_tolerance: float = NEAR_BEST_TOLERANCE,
) -> tuple[str | None, list[dict], dict[str, dict]]:
    """Select a robust near-best candidate without reading later years."""
    if not years or tuple(sorted(set(years))) != years:
        raise ValueError("selection years must be unique and increasing")
    if near_best_tolerance < 0.0:
        raise ValueError("near-best tolerance must be non-negative")
    cutoff = pd.Timestamp(f"{years[-1]}-12-31")
    summaries = {}
    ranking = []
    for key, results in sorted(variant_results.items()):
        for result in results.values():
            if result.index.max() > cutoff:
                raise RuntimeError("regularized selector received future data")
        lookback, crowded_weight = _configuration_from_key(key)
        summary = v20._period_summary(results, years=years)
        summaries[key] = summary
        cells = [
            float(row["excess_vs_nasdaq"])
            for cost_summary in summary["costs"].values()
            for row in cost_summary["annual"]
        ]
        cost_50 = summary["costs"]["50"]
        ranking.append({
            "variant": key,
            "lookback_sessions": int(lookback),
            "crowded_stock_weight": float(crowded_weight),
            "eligible": bool(all(value > 0.0 for value in cells)),
            "worst_annual_excess_vs_nasdaq": float(min(cells)),
            "compounded_excess_vs_nasdaq_50bps": float(
                cost_50["compounded_excess_vs_nasdaq"]
            ),
            "turnover_50bps": float(cost_50["turnover"]),
        })
    eligible = [row for row in ranking if row["eligible"]]
    if not eligible:
        for row in ranking:
            row["near_best"] = False
        ranking.sort(key=lambda row: (
            -row["worst_annual_excess_vs_nasdaq"], row["variant"]
        ))
        for index, row in enumerate(ranking, start=1):
            row["rank"] = index
        return None, ranking, summaries

    best_worst = max(
        row["worst_annual_excess_vs_nasdaq"] for row in eligible
    )
    for row in ranking:
        row["near_best"] = bool(
            row["eligible"]
            and best_worst - row["worst_annual_excess_vs_nasdaq"]
            <= near_best_tolerance + 1e-12
        )
    ranking.sort(key=lambda row: (
        not row["near_best"],
        row["crowded_stock_weight"] if row["near_best"] else 1.0,
        -row["lookback_sessions"] if row["near_best"] else 0,
        -row["worst_annual_excess_vs_nasdaq"],
        -row["compounded_excess_vs_nasdaq_50bps"],
        row["turnover_50bps"],
        row["variant"],
    ))
    for index, row in enumerate(ranking, start=1):
        row["rank"] = index
    selected = next(row["variant"] for row in ranking if row["near_best"])
    return selected, ranking, summaries


def evaluate_walk_forward(
    variant_results: dict[str, dict[int, pd.DataFrame]],
) -> dict:
    folds = []
    all_pass = True
    for selection_years, test_year in WALK_FORWARD_FOLDS:
        train = {
            key: _slice_results(results, selection_years)
            for key, results in variant_results.items()
        }
        selected, ranking, _ = regularized_select_variant(
            train, years=selection_years
        )
        if selected is None:
            folds.append({
                "selection_years": list(selection_years),
                "test_year": int(test_year),
                "selected_variant": None,
                "selection_status": "NO_ELIGIBLE_VARIANT",
                "test_status": "NOT_RUN",
            })
            all_pass = False
            continue
        test_results = _slice_results(variant_results[selected], (test_year,))
        test_summary = v20._period_summary(test_results, years=(test_year,))
        cells = [
            float(row["excess_vs_nasdaq"])
            for cost_summary in test_summary["costs"].values()
            for row in cost_summary["annual"]
        ]
        passed = bool(all(value > 0.0 for value in cells))
        all_pass = all_pass and passed
        selection_row = next(
            row for row in ranking if row["variant"] == selected
        )
        folds.append({
            "selection_years": list(selection_years),
            "test_year": int(test_year),
            "selected_variant": selected,
            "selection_status": "PASS",
            "selection_worst_annual_excess_vs_nasdaq": selection_row[
                "worst_annual_excess_vs_nasdaq"
            ],
            "test_status": "PASS" if passed else "BLOCKED",
            "test_result": test_summary,
        })
    return {
        "folds": folds,
        "passed_fold_count": int(
            sum(fold["test_status"] == "PASS" for fold in folds)
        ),
        "required_fold_count": len(WALK_FORWARD_FOLDS),
        "all_walk_forward_folds_passed": bool(all_pass),
    }


def _ibkr_cost_envelope(calibration: dict) -> dict:
    rows = calibration["calibration"]
    maximum = max(float(row["weighted_base_commission_bps"]) for row in rows)
    minimum = min(float(row["weighted_base_commission_bps"]) for row in rows)
    return {
        "source": "v21 whole-share initial-allocation base commission",
        "modeled_account_equities_usd": calibration["account_equities_usd"],
        "modeled_pricing_plans": calibration["pricing_plans"],
        "minimum_modeled_base_commission_bps": minimum,
        "maximum_modeled_base_commission_bps": maximum,
        "primary_total_cost_bps": PRIMARY_TOTAL_COST_BPS,
        "primary_remaining_noncommission_budget_bps_at_maximum_commission": (
            PRIMARY_TOTAL_COST_BPS - maximum
        ),
        "robustness_stress_total_cost_bps": ROBUSTNESS_STRESS_COST_BPS,
        "robustness_remaining_noncommission_budget_bps_at_maximum_commission": (
            ROBUSTNESS_STRESS_COST_BPS - maximum
        ),
        "includes_realized_spread_or_slippage": False,
        "actual_user_account_equity_frozen": False,
        "actual_user_pricing_plan_frozen": False,
    }


def _write_frame(path: Path, frame: pd.DataFrame) -> dict:
    frame.to_csv(path, index_label="date")
    return {"path": str(path), "sha256": _sha256(path)}


def run(output_dir: Path = OUTPUT_DIR) -> dict:
    output_dir = Path(output_dir)
    if output_dir.exists() and any(output_dir.iterdir()):
        raise RuntimeError(f"v22 freeze output will not be overwritten: {output_dir}")

    bindings, v20_protocol, _, v21_calibration = _verify_inputs()
    source_summary = json.loads(
        v19.SOURCE_V10_SUMMARY["path"].read_text(encoding="utf-8")
    )
    grid = v19.source_locked_grid(source_summary)
    price_binding = v20_protocol["input_bindings"]["price_directory"]
    stock_paths, qqq_return = v20._load_replay_inputs(
        end=DEVELOPMENT_END, price_binding=price_binding
    )
    if max(frame.index.max() for frame in stock_paths.values()) > pd.Timestamp(
        DEVELOPMENT_END
    ):
        raise RuntimeError("v22 base replay crossed the development cutoff")

    variant_results = {}
    for lookback, crowded_weight in grid:
        key = v19._variant_key(lookback, crowded_weight)
        variant_results[key] = v20._simulate_variant(
            stock_paths,
            qqq_return,
            lookback=lookback,
            crowded_stock_weight=crowded_weight,
        )

    walk_forward = evaluate_walk_forward(variant_results)
    final_development = {
        key: _slice_results(results, DEVELOPMENT_YEARS)
        for key, results in variant_results.items()
    }
    selected, ranking, summaries = regularized_select_variant(
        final_development, years=DEVELOPMENT_YEARS
    )
    if selected is None:
        raise RuntimeError("no v22 development candidate passed all cost gates")
    selected_lookback, selected_crowded = _configuration_from_key(selected)
    selected_summary = summaries[selected]
    development_passed = all(
        summary["all_annual_excess_positive"]
        for summary in selected_summary["costs"].values()
    )

    output_dir.mkdir(parents=True, exist_ok=True)
    ranking_path = output_dir / "development_ranking.csv"
    pd.DataFrame(ranking).to_csv(ranking_path, index=False)
    fold_rows = []
    for fold in walk_forward["folds"]:
        row = {
            key: value
            for key, value in fold.items()
            if key != "test_result"
        }
        if "test_result" in fold:
            for cost, summary in fold["test_result"]["costs"].items():
                row[f"test_excess_{cost}bps"] = summary["annual"][0][
                    "excess_vs_nasdaq"
                ]
        row["selection_years"] = ",".join(
            str(year) for year in row["selection_years"]
        )
        fold_rows.append(row)
    folds_path = output_dir / "walk_forward_folds.csv"
    pd.DataFrame(fold_rows).to_csv(folds_path, index=False)

    outputs = {
        "development_ranking": {
            "path": str(ranking_path),
            "sha256": _sha256(ranking_path),
        },
        "walk_forward_folds": {
            "path": str(folds_path),
            "sha256": _sha256(folds_path),
        },
    }
    for cost in v19.COSTS:
        daily_path = output_dir / f"selected_development_{cost}bps.csv"
        outputs[f"selected_development_{cost}bps"] = _write_frame(
            daily_path, final_development[selected][cost]
        )

    report = {
        "schema_version": 1,
        "research_only": True,
        "stage": "V22_REGULARIZED_WALK_FORWARD_DEVELOPMENT",
        "frozen_at": FROZEN_AT,
        "release_status": "BLOCKED",
        "promotion_eligible": False,
        "brokerage_or_trading_authorized": False,
        "broker_connection_used": False,
        "order_created": False,
        "capital_allocated": False,
        "parameters_frozen": True,
        "formal_financials_modified": False,
        "formal_universe_modified": False,
        "historical_selection_contaminated": True,
        "statistically_untouched": False,
        "may_be_called_clean_confirmation": False,
        "v20_preserved_without_retuning": True,
        "v20_holdout_rerun": False,
        "development_data": {
            "base_selector_training_start": "2019-01-01",
            "overlay_start": START,
            "overlay_end": DEVELOPMENT_END,
            "overlay_years": list(DEVELOPMENT_YEARS),
            "existing_2026_data_used_for_selection_or_evaluation": False,
        },
        "candidate_grid": {
            "source_locked_to_existing_v10_grid": True,
            "lookback_sessions": list(v19.LOOKBACKS),
            "normal_stock_weight": v19.NORMAL_STOCK_WEIGHT,
            "crowded_stock_weights": list(v19.CROWDED_STOCK_WEIGHTS),
            "new_grid_point_added": False,
        },
        "selection_rule": {
            "eligibility": (
                "positive annual excess versus Nasdaq in every selection year "
                "at 10/30/50bps"
            ),
            "near_best_tolerance_percentage_points": (
                NEAR_BEST_TOLERANCE * 100.0
            ),
            "regularization_order": [
                "eligible candidates within the near-best tolerance",
                "smaller crowded-regime stock weight",
                "longer relative-return lookback",
                "higher worst annual excess versus Nasdaq",
                "higher 50bps compounded excess versus Nasdaq",
                "lower 50bps turnover",
                "variant key",
            ],
            "designed_after_2025_result_inspection": True,
        },
        "walk_forward_diagnostic": walk_forward,
        "walk_forward_status": (
            "PASS"
            if walk_forward["all_walk_forward_folds_passed"]
            else "BLOCKED"
        ),
        "selected_variant": selected,
        "selected_configuration": {
            "lookback_sessions": selected_lookback,
            "normal_stock_weight": v19.NORMAL_STOCK_WEIGHT,
            "crowded_stock_weight": selected_crowded,
        },
        "selected_development_result": selected_summary,
        "development_status": "PASS" if development_passed else "BLOCKED",
        "ibkr_cost_envelope": _ibkr_cost_envelope(v21_calibration),
        "future_forward_protocol": {
            "status": "FROZEN_NOT_STARTED",
            "data_must_be_later_than": FORWARD_DATA_START_EXCLUSIVE,
            "minimum_operational_months": 3,
            "target_decision_months": 6,
            "parameters_must_remain_frozen": True,
            "primary_total_cost_bps": PRIMARY_TOTAL_COST_BPS,
            "robustness_stress_total_cost_bps": ROBUSTNESS_STRESS_COST_BPS,
            "immutable_fill_ledger_required_before_execution_claims": True,
            "actual_account_equity_and_pricing_plan_required": True,
            "broker_connection_authorized": False,
            "orders_authorized": False,
        },
        "research_forward_observation_ready": bool(
            development_passed
            and walk_forward["all_walk_forward_folds_passed"]
        ),
        "input_bindings": {
            **bindings,
            "price_directory": price_binding,
            "v22_implementation": {
                "path": str(Path(__file__).relative_to(Path.cwd())),
                "sha256": _sha256(Path(__file__)),
            },
        },
        "outputs": outputs,
        "interpretation_guardrail": (
            "v22 legitimately uses 2025 as development data and therefore "
            "cannot use it as confirmation. The expanding next-year folds are "
            "a no-lookahead replay but remain historically human-exposed. Only "
            "data after 2026-08-30 can provide new forward evidence. The IBKR "
            "envelope includes published base commission, not realized spread "
            "or slippage, and does not authorize account access or trading."
        ),
    }
    protocol_path = output_dir / "frozen_protocol.json"
    protocol_path.write_text(
        json.dumps(report, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return {
        **report,
        "protocol": {
            "path": str(protocol_path),
            "sha256": _sha256(protocol_path),
        },
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, default=OUTPUT_DIR)
    args = parser.parse_args()
    report = run(args.output_dir)
    print(json.dumps({
        "selected_variant": report["selected_variant"],
        "walk_forward_status": report["walk_forward_status"],
        "development_status": report["development_status"],
        "research_forward_observation_ready": report[
            "research_forward_observation_ready"
        ],
        "release_status": report["release_status"],
        "protocol": report["protocol"],
    }, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
