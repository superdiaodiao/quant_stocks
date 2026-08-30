#!/usr/bin/env python3
"""Retrain the source-locked v10 overlay with explicit time isolation.

The frozen v14 target schedule already uses an expanding training history from
2019 and first emits out-of-sample targets in 2022.  This protocol adds a
second, explicit split for the v10 alpha-budget overlay:

* 2022-2023: choose one point from the pre-existing nine-point grid;
* 2024: validate only the selected point;
* 2025-2026: never read or evaluate in this program.

The separately frozen recent-holdout runner may read 2025-2026 only after this
program has emitted an immutable protocol.  Human exposure to earlier research
results is recorded independently from the model-level no-lookahead boundary.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

import pandas as pd

from scripts import research_v15_benchmark_core_development as v15
from scripts import research_v19_source_locked_v10_feasibility as v19
from src.conf import NASDAQ_INDEX_FILE
from src.research.can_slim import replay_can_slim_target_schedule
from src.research.data_quality import back_adjust_common_splits
from src.research.panel_data import load_panel


START = "2022-01-01"
DEVELOPMENT_END = "2023-12-31"
VALIDATION_START = "2024-01-01"
VALIDATION_END = "2024-12-31"
HOLDOUT_START = "2025-01-01"
HOLDOUT_END = "2026-07-17"
DEVELOPMENT_YEARS = (2022, 2023)
VALIDATION_YEAR = 2024
FROZEN_AT = "2026-08-30T09:43:12+08:00"
OUTPUT_DIR = Path(
    "output/research_only/v20/temporally_isolated_retraining_20260830"
)

DEPENDENCY_BINDINGS = {
    "source_v10_script": v19.SOURCE_V10_SCRIPT,
    "source_v10_summary": v19.SOURCE_V10_SUMMARY,
    "v14_protocol": v15.V14_PROTOCOL,
    "v14_result": v15.V14_RESULT,
    "v14_targets": v15.V14_TARGETS,
    "v14_daily": v15.V14_DAILY,
    "v14_cost_stress": v19.V14_COST_STRESS,
    "qqq_history": v15.QQQ_HISTORY,
    "qqq_provenance": v15.QQQ_PROVENANCE,
    "v19_implementation": {
        "path": Path("scripts/research_v19_source_locked_v10_feasibility.py"),
        "sha256": (
            "589ea011565044abb0926d581e8e5150d9326871601b3b7c2fb9a73a02543987"
        ),
    },
    "can_slim_replay": {
        "path": Path("src/research/can_slim.py"),
        "sha256": (
            "f08797f5b1b07b3d6f41251cd259cab503a6e8e4e2ba43a936a18b7c217c42e5"
        ),
    },
    "panel_loader": {
        "path": Path("src/research/panel_data.py"),
        "sha256": (
            "94d72b7e91eaa2b92a4cb360743164be015284b732fd302e4078e3f50235ada8"
        ),
    },
    "split_adjustment": {
        "path": Path("src/research/data_quality.py"),
        "sha256": (
            "dcf1ded4f60024dba43e0c0bb648c23d0734a189bacdfb17f4a985540e66768c"
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


def _verify_dependencies() -> dict:
    return {
        name: _verify_binding(name, binding)
        for name, binding in DEPENDENCY_BINDINGS.items()
    }


def _validate_base_training_boundary(protocol: dict) -> dict:
    selector = protocol["selector"]
    expected = {
        "expanding_training_start": "2019-01-01",
        "first_effective_date": "2022-01-01",
        "rolling_window_months": 36,
        "parameter_update_frequency": "annual",
        "no_evidence_fallback": False,
    }
    actual = {key: selector.get(key) for key in expected}
    if actual != expected:
        raise RuntimeError(f"v14 base training boundary changed: {actual}")
    return {
        "training_start": "2019-01-01",
        "training_end": "2021-12-31",
        "first_out_of_sample_target": "2022-01-01",
        "rolling_window_months": 36,
        "selection_policy": "frozen v14 annual walk-forward selector",
    }


def _load_replay_inputs(
    *,
    end: str,
    price_binding: dict,
) -> tuple[dict[int, pd.DataFrame], pd.Series]:
    """Return replay paths whose observable dates cannot exceed ``end``."""
    cutoff = pd.Timestamp(end)
    targets = pd.read_csv(
        v15.V14_TARGETS["path"], parse_dates=["effective_date"]
    )
    targets = targets.loc[targets["effective_date"].le(cutoff)].copy()
    if targets.empty or targets["effective_date"].max() > cutoff:
        raise RuntimeError("target cutoff enforcement failed")

    close, _ = load_panel(price_binding["path"], "2017-01-01", end)
    if close.index.max() > cutoff:
        raise RuntimeError("price loader crossed the requested cutoff")
    prices = back_adjust_common_splits(close)
    nasdaq = pd.read_csv(
        NASDAQ_INDEX_FILE, index_col="date", parse_dates=True
    )["close"].sort_index().loc[:cutoff]
    qqq = pd.read_csv(
        v15.QQQ_HISTORY["path"], index_col="date", parse_dates=True
    ).sort_index().loc[:cutoff]
    prices[v15.CORE_TICKER] = v15.qqq_total_return_index(
        qqq,
        prices.index,
        allowed_market_closed=nasdaq.reindex(prices.index).isna(),
    )
    qqq_return = (
        prices[v15.CORE_TICKER].pct_change(fill_method=None).fillna(0.0)
    )

    stock_paths = {}
    for cost in v19.COSTS:
        stressed = targets.copy()
        stressed["base_transaction_cost_bps"] = float(cost)
        daily, _ = replay_can_slim_target_schedule(
            prices,
            nasdaq,
            stressed,
            START,
            end,
            adjust_splits=False,
        )
        if daily.index.max() > cutoff:
            raise RuntimeError(f"{cost}bps replay crossed cutoff {end}")
        stock_paths[cost] = daily
    return stock_paths, qqq_return.reindex(stock_paths[10].index)


def _simulate_variant(
    stock_paths: dict[int, pd.DataFrame],
    qqq_return: pd.Series,
    *,
    lookback: int,
    crowded_stock_weight: float,
) -> dict[int, pd.DataFrame]:
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
            lookback=lookback,
            crowded_stock_weight=crowded_stock_weight,
            transfer_cost_bps=float(cost),
        )
        if expected_decisions is None:
            expected_decisions = decisions
        elif not expected_decisions.equals(decisions):
            raise RuntimeError("transaction cost changed monthly decisions")
        results[cost] = result
    return results


def _annual(result: pd.DataFrame) -> pd.DataFrame:
    annual = (
        (1.0 + result[["strategy", "benchmark", "qqq"]])
        .groupby(result.index.year)
        .prod()
        - 1.0
    )
    annual["excess_vs_nasdaq"] = annual["strategy"] - annual["benchmark"]
    annual["excess_vs_qqq"] = annual["strategy"] - annual["qqq"]
    return annual


def _period_summary(
    results: dict[int, pd.DataFrame],
    *,
    years: tuple[int, ...],
) -> dict:
    rows = {}
    for cost in v19.COSTS:
        result = results[cost].loc[results[cost].index.year.isin(years)]
        annual = _annual(result)
        actual_years = tuple(annual.index.astype(int))
        if actual_years != years:
            raise RuntimeError(
                f"unexpected years at {cost}bps: {actual_years} != {years}"
            )
        strategy = float((1.0 + result["strategy"]).prod() - 1.0)
        benchmark = float((1.0 + result["benchmark"]).prod() - 1.0)
        rows[str(cost)] = {
            "annual": [
                {"year": int(year), **values}
                for year, values in annual.to_dict(orient="index").items()
            ],
            "all_annual_excess_positive": bool(
                annual["excess_vs_nasdaq"].gt(0.0).all()
            ),
            "minimum_annual_excess_vs_nasdaq": float(
                annual["excess_vs_nasdaq"].min()
            ),
            "compounded_strategy": strategy,
            "compounded_nasdaq": benchmark,
            "compounded_excess_vs_nasdaq": strategy - benchmark,
            "turnover": float(result["turnover"].sum()),
        }
    return {"years": list(years), "costs": rows}


def select_development_variant(
    variant_results: dict[str, dict[int, pd.DataFrame]],
) -> tuple[str | None, list[dict], dict[str, dict]]:
    """Select using only 2022-2023, with a deterministic robust ranking."""
    summaries = {}
    ranking = []
    for key, results in sorted(variant_results.items()):
        for result in results.values():
            if result.index.max() > pd.Timestamp(DEVELOPMENT_END):
                raise RuntimeError("development selector received future data")
        summary = _period_summary(results, years=DEVELOPMENT_YEARS)
        summaries[key] = summary
        cells = [
            row["excess_vs_nasdaq"]
            for cost in summary["costs"].values()
            for row in cost["annual"]
        ]
        eligible = all(value > 0.0 for value in cells)
        cost_50 = summary["costs"]["50"]
        ranking.append({
            "variant": key,
            "eligible": bool(eligible),
            "worst_annual_excess_vs_nasdaq": float(min(cells)),
            "compounded_excess_vs_nasdaq_50bps": float(
                cost_50["compounded_excess_vs_nasdaq"]
            ),
            "turnover_50bps": float(cost_50["turnover"]),
        })
    ranking.sort(key=lambda row: (
        not row["eligible"],
        -row["worst_annual_excess_vs_nasdaq"],
        -row["compounded_excess_vs_nasdaq_50bps"],
        row["turnover_50bps"],
        row["variant"],
    ))
    for index, row in enumerate(ranking, start=1):
        row["rank"] = index
    eligible = [row for row in ranking if row["eligible"]]
    selected = eligible[0]["variant"] if eligible else None
    return selected, ranking, summaries


def validate_selected_variant(
    results: dict[int, pd.DataFrame],
) -> dict:
    for result in results.values():
        if result.index.max() > pd.Timestamp(VALIDATION_END):
            raise RuntimeError("validation received post-2024 data")
    summary = _period_summary(results, years=(VALIDATION_YEAR,))
    passed = all(
        row["all_annual_excess_positive"]
        and row["compounded_excess_vs_nasdaq"] > 0.0
        for row in summary["costs"].values()
    )
    return {**summary, "all_validation_gates_passed": bool(passed)}


def _write_frame(path: Path, frame: pd.DataFrame) -> dict:
    frame.to_csv(path, index_label="date")
    return {"path": str(path), "sha256": _sha256(path)}


def run(output_dir: Path = OUTPUT_DIR) -> dict:
    output_dir = Path(output_dir)
    if output_dir.exists() and any(output_dir.iterdir()):
        raise RuntimeError(f"v20 freeze output will not be overwritten: {output_dir}")

    dependencies = _verify_dependencies()
    v14_protocol = json.loads(
        v15.V14_PROTOCOL["path"].read_text(encoding="utf-8")
    )
    base_training = _validate_base_training_boundary(v14_protocol)
    price_binding = v14_protocol["input_bindings"]["price_directory"]
    source_summary = json.loads(
        v19.SOURCE_V10_SUMMARY["path"].read_text(encoding="utf-8")
    )
    grid = v19.source_locked_grid(source_summary)

    development_stock, development_qqq = _load_replay_inputs(
        end=DEVELOPMENT_END, price_binding=price_binding
    )
    development_results = {}
    for lookback, crowded in grid:
        key = v19._variant_key(lookback, crowded)
        development_results[key] = _simulate_variant(
            development_stock,
            development_qqq,
            lookback=lookback,
            crowded_stock_weight=crowded,
        )
    selected, ranking, development_summaries = select_development_variant(
        development_results
    )
    if selected is None:
        raise RuntimeError("no development variant passed the frozen gate")

    selected_config = next(
        (lookback, crowded)
        for lookback, crowded in grid
        if v19._variant_key(lookback, crowded) == selected
    )
    validation_stock, validation_qqq = _load_replay_inputs(
        end=VALIDATION_END, price_binding=price_binding
    )
    validation_full = _simulate_variant(
        validation_stock,
        validation_qqq,
        lookback=selected_config[0],
        crowded_stock_weight=selected_config[1],
    )
    validation = validate_selected_variant(validation_full)

    output_dir.mkdir(parents=True, exist_ok=True)
    grid_path = output_dir / "development_ranking.csv"
    pd.DataFrame(ranking).to_csv(grid_path, index=False)
    outputs = {
        "development_ranking": {
            "path": str(OUTPUT_DIR / grid_path.name),
            "sha256": _sha256(grid_path),
        }
    }
    for cost in v19.COSTS:
        development_path = output_dir / f"selected_development_{cost}bps.csv"
        development_frame = development_results[selected][cost]
        development_frame.to_csv(development_path, index_label="date")
        outputs[f"selected_development_{cost}bps"] = {
            "path": str(OUTPUT_DIR / development_path.name),
            "sha256": _sha256(development_path),
        }
        validation_path = output_dir / f"selected_validation_{cost}bps.csv"
        validation_frame = validation_full[cost].loc[VALIDATION_START:VALIDATION_END]
        validation_frame.to_csv(validation_path, index_label="date")
        outputs[f"selected_validation_{cost}bps"] = {
            "path": str(OUTPUT_DIR / validation_path.name),
            "sha256": _sha256(validation_path),
        }

    report = {
        "schema_version": 1,
        "research_only": True,
        "frozen_at": FROZEN_AT,
        "protocol_status": "FROZEN_V20_TEMPORALLY_ISOLATED_RETRAINING",
        "release_status": "BLOCKED",
        "promotion_eligible": False,
        "parameters_frozen": True,
        "model_data_isolation": "PASS",
        "researcher_exposure_status": "HISTORICALLY_EXPOSED",
        "statistically_untouched": False,
        "formal_financials_modified": False,
        "formal_universe_modified": False,
        "recent_holdout_executed": False,
        "recent_holdout_results_inspected": False,
        "base_training": base_training,
        "overlay_data_split": {
            "development_selection": {
                "start": START,
                "end": DEVELOPMENT_END,
                "years": list(DEVELOPMENT_YEARS),
            },
            "validation": {
                "start": VALIDATION_START,
                "end": VALIDATION_END,
                "years": [VALIDATION_YEAR],
            },
            "recent_holdout": {
                "start": HOLDOUT_START,
                "end": HOLDOUT_END,
                "model_selection_access": "DENIED_UNTIL_PROTOCOL_FROZEN",
                "researcher_exposure_status": "REPEATEDLY_HUMAN_EXPOSED",
            },
        },
        "selection_rule": {
            "eligibility": (
                "positive annual excess versus Nasdaq in 2022 and 2023 at "
                "each of 10/30/50bps"
            ),
            "ranking": [
                "worst annual excess versus Nasdaq descending",
                "50bps compounded excess versus Nasdaq descending",
                "50bps turnover ascending",
                "variant key ascending",
            ],
            "retune_after_validation_or_holdout": False,
        },
        "selected_variant": selected,
        "selected_configuration": {
            "lookback_sessions": int(selected_config[0]),
            "normal_stock_weight": v19.NORMAL_STOCK_WEIGHT,
            "crowded_stock_weight": float(selected_config[1]),
        },
        "development_ranking": ranking,
        "selected_development_result": development_summaries[selected],
        "validation_result": validation,
        "validation_status": (
            "PASS" if validation["all_validation_gates_passed"] else "BLOCKED"
        ),
        "future_forward_phase": {
            "minimum_operational_months": 3,
            "target_decision_months": 6,
            "started": False,
            "authorized": False,
        },
        "input_bindings": {
            **dependencies,
            "price_directory": price_binding,
            "retraining_script": {
                "path": str(Path(__file__).relative_to(Path.cwd())),
                "sha256": _sha256(Path(__file__)),
            },
            "recent_holdout_script": {
                "path": "scripts/research_v20_recent_holdout.py",
                "sha256": _sha256(Path("scripts/research_v20_recent_holdout.py")),
            },
        },
        "outputs": outputs,
        "interpretation_guardrail": (
            "The program selected on 2022-2023 and validated only the frozen "
            "winner on 2024 before any 2025-2026 evaluation. This repairs "
            "model-level time leakage. Prior human exposure to later results "
            "remains disclosed, so the recent holdout is supportive historical "
            "evidence rather than statistically untouched confirmation."
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
        "validation_status": report["validation_status"],
        "recent_holdout_executed": report["recent_holdout_executed"],
        "release_status": report["release_status"],
        "protocol": report["protocol"],
    }, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
