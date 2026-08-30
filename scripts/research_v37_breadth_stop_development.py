#!/usr/bin/env python3
"""Develop concentration breadth plus individual stops on 2020-2025 only.

This is a small structural grid: frozen Top-25 momentum ranking, either five or
ten equal-weight stocks, and a 10/15/20 percent individual trailing stop.  A
stopped name stays cash until the next monthly rebalance.  Training years are
used only to select the structure and are excluded from every final win claim.
"""

from __future__ import annotations

import argparse
from datetime import datetime
import hashlib
import json
import math
from pathlib import Path

import pandas as pd

from scripts import research_v23_stock_only_frequency as v23
from scripts import research_v26_large_liquid_stock_momentum as v26
from scripts import research_v28_stock_trailing_stop_development as v28
from scripts import research_v30_2019_selection_path_adjudication as v30
from scripts import research_v36_correlation_diversified_momentum as v36
from src.conf import CLEANED_PRICE_DATA_DIR, NASDAQ_INDEX_FILE


DEVELOPMENT_START = "2020-01-01"
DEVELOPMENT_END = "2025-12-31"
DEVELOPMENT_YEARS = tuple(range(2020, 2026))
FINAL_COMPARISON_YEARS: tuple[int, ...] = ()
TRUE_PROSPECTIVE_START = "2026-08-31"
COSTS = v23.COSTS
TOP_NS = (5, 10)
STOP_THRESHOLDS = (0.10, 0.15, 0.20)
MINIMUM_TRAINING_WIN_RATE = 2.0 / 3.0
MAXIMUM_DRAWDOWN_LAG = 0.10
MAXIMUM_ABSOLUTE_DRAWDOWN = 0.25
FORBIDDEN_ETFS = v23.FORBIDDEN_ETFS

OUTPUT_DIR = Path(
    "output/research_only/v37/breadth_stop_development_20260830"
)
PROTOCOL_PATH = OUTPUT_DIR / "frozen_protocol.json"
DEVELOPMENT_OUTPUT_DIR = OUTPUT_DIR / "development_results"
V30_MANIFEST = v30.RESULT_OUTPUT_DIR / "manifest.json"
V36_MANIFEST = v36.DEVELOPMENT_OUTPUT_DIR / "manifest.json"


def _sha256(path: Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _file_binding(path: Path) -> dict:
    path = Path(path)
    return {"path": str(path), "sha256": _sha256(path)}


def _directory_binding(path: Path, pattern: str) -> dict:
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


def candidate_specs() -> list[dict]:
    return [
        {
            "key": f"top{top_n}_individual_stop_{int(stop * 100)}pct",
            "top_n": top_n,
            "individual_trailing_stop_fraction": stop,
            "liquid_candidate_pool": 25,
            "momentum_lookback_sessions": 63,
            "stock_selection_frequency": "monthly",
            "stop_signal_frequency": "daily",
            "stop_execution": "next_trading_close",
            "stop_reentry": "next_monthly_rebalance_only",
        }
        for top_n in TOP_NS
        for stop in STOP_THRESHOLDS
    ]


def _validate_sources() -> dict:
    v30_manifest = json.loads(V30_MANIFEST.read_text(encoding="utf-8"))
    if not v30_manifest["audit"]["strategy_selection_path_complete"]:
        raise RuntimeError("v30 base selection path changed")
    v36_manifest = json.loads(V36_MANIFEST.read_text(encoding="utf-8"))
    if v36_manifest["development_status"] != "BLOCKED":
        raise RuntimeError("v36 development status changed")
    return {
        "base_candidate": v30.SELECTED_CANDIDATE,
        "base_2019_selection_path_complete": True,
        "v36_correlation_diversification_rejected_before_observation": True,
        "2026_used_to_motivate_architecture": True,
        "2026_used_for_breadth_or_stop_selection": False,
    }


def freeze_protocol(path: Path = PROTOCOL_PATH) -> dict:
    path = Path(path)
    if path.exists():
        raise RuntimeError(f"v37 protocol will not be overwritten: {path}")
    runner = Path(__file__).resolve().relative_to(Path.cwd().resolve())
    protocol = {
        "schema_version": 1,
        "research_only": True,
        "stage": "V37_BREADTH_STOP_DEVELOPMENT_PRECOMMITMENT",
        "status": "FROZEN_NOT_DEVELOPED",
        "frozen_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "source_diagnosis": _validate_sources(),
        "evaluation_boundary": {
            "development_start": DEVELOPMENT_START,
            "development_end": DEVELOPMENT_END,
            "training_years": list(DEVELOPMENT_YEARS),
            "training_years_excluded_from_final_comparison": True,
            "final_comparison_years": [],
            "2019_role": "base_selector_development_not_overlay_grid_training",
            "2026_used_for_parameter_selection": False,
            "architecture_isolated_from_2026": False,
            "parameter_isolated_from_2026": True,
            "true_prospective_start": TRUE_PROSPECTIVE_START,
        },
        "candidate_grid": candidate_specs(),
        "candidate_count": len(candidate_specs()),
        "risk_policy": {
            "base_ranking": v30.SELECTED_CANDIDATE,
            "holdings": "individual common stocks only",
            "risk_off_and_stopped_weight": "CASH",
            "individual_stop_execution": "next trading close",
            "individual_stop_reentry": "next monthly target only",
            "forbidden_index_etfs": sorted(FORBIDDEN_ETFS),
        },
        "cost_bps": list(COSTS),
        "benchmark": "NASDAQ_COMPOSITE",
        "training_eligibility_gates": {
            "positive_compounded_excess_at_30_and_50bps": True,
            "minimum_positive_training_year_rate_at_50bps": (
                MINIMUM_TRAINING_WIN_RATE
            ),
            "maximum_drawdown_lag_percentage_points_at_50bps": (
                MAXIMUM_DRAWDOWN_LAG * 100.0
            ),
            "maximum_absolute_drawdown_percentage_points_at_50bps": (
                MAXIMUM_ABSOLUTE_DRAWDOWN * 100.0
            ),
            "label": "TRAINING_DIAGNOSTIC_NOT_FINAL_EVIDENCE",
        },
        "selection_order": [
            "training eligible first",
            "absolute drawdown at 50bps ascending",
            "worst annual training excess at 50bps descending",
            "compounded training excess at 50bps descending",
            "turnover at 50bps ascending",
            "candidate key ascending",
        ],
        "walk_forward_folds": [
            {"selection_years": list(range(2020, year)), "test_year": year}
            for year in range(2022, 2026)
        ],
        "input_bindings": {
            "runner": _file_binding(runner),
            "v26_ranking_helpers": _file_binding(
                Path("scripts/research_v26_large_liquid_stock_momentum.py")
            ),
            "v28_stop_helpers": _file_binding(
                Path("scripts/research_v28_stock_trailing_stop_development.py")
            ),
            "v30_manifest": _file_binding(V30_MANIFEST),
            "v36_input_helpers": _file_binding(
                Path("scripts/research_v36_correlation_diversified_momentum.py")
            ),
            "v36_manifest": _file_binding(V36_MANIFEST),
            "price_directory": _directory_binding(
                Path(CLEANED_PRICE_DATA_DIR), "*.csv"
            ),
            "nasdaq_index": _file_binding(Path(NASDAQ_INDEX_FILE)),
        },
        "parameters_frozen_before_development": True,
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
    if protocol["status"] != "FROZEN_NOT_DEVELOPED":
        raise RuntimeError("v37 protocol status changed")
    if protocol["candidate_grid"] != candidate_specs():
        raise RuntimeError("v37 candidate grid changed")
    if protocol["source_diagnosis"] != _validate_sources():
        raise RuntimeError("v37 source diagnosis changed")
    for name, binding in protocol["input_bindings"].items():
        if "content_manifest_sha256" in binding:
            actual = _directory_binding(Path(binding["path"]), binding["pattern"])
            if actual != binding:
                raise RuntimeError(f"v37 directory binding changed for {name}")
        elif _sha256(Path(binding["path"])) != binding["sha256"]:
            raise RuntimeError(f"v37 file binding changed for {name}")
    return protocol, protocol_sha


def _load_inputs() -> dict:
    return v36._load_inputs()


def base_specification(top_n: int) -> dict:
    spec = dict(v30.selected_specification())
    spec["top_n"] = int(top_n)
    spec["key"] = (
        f"mom{spec['lookback_sessions']}_skip{spec['skip_recent_sessions']}_"
        f"liquid{spec['liquid_pool_size']}_top{top_n}_profitable_monthly"
    )
    return spec


def generate_base_targets(inputs: dict, top_n: int) -> pd.DataFrame:
    targets = v26.generate_target_schedule(base_specification(top_n), inputs)
    targets = targets.loc[
        pd.to_datetime(targets["effective_date"]).between(
            DEVELOPMENT_START, DEVELOPMENT_END
        )
    ].copy()
    tickers = set(targets["ticker"].astype(str)) - {"__CASH__"}
    forbidden = sorted(tickers & FORBIDDEN_ETFS)
    if forbidden:
        raise RuntimeError(f"v37 targets contain forbidden ETFs: {forbidden}")
    return targets


def _canonicalize_result(result: pd.DataFrame, nasdaq: pd.Series) -> pd.DataFrame:
    dates = nasdaq.loc[DEVELOPMENT_START:DEVELOPMENT_END].dropna().index
    missing = dates.difference(result.index)
    if len(missing):
        raise RuntimeError(f"strategy is missing Nasdaq sessions: {list(missing[:5])}")
    result = result.reindex(dates).copy()
    if result[["strategy", "benchmark"]].isna().any().any():
        raise RuntimeError("v37 canonical result contains missing returns")
    return result


def _maximum_drawdown(series: pd.Series) -> float:
    nav = (1.0 + series.astype(float)).cumprod()
    return float(nav.div(nav.cummax()).sub(1.0).min())


def _period_metrics(result: pd.DataFrame, years: tuple[int, ...]) -> dict:
    selected = result.loc[result.index.year.isin(years)]
    observed_years = tuple(sorted(set(selected.index.year.astype(int))))
    if observed_years != years:
        raise RuntimeError(f"result years {observed_years} != {years}")
    strategy = float((1.0 + selected["strategy"]).prod() - 1.0)
    nasdaq = float((1.0 + selected["benchmark"]).prod() - 1.0)
    strategy_drawdown = _maximum_drawdown(selected["strategy"])
    nasdaq_drawdown = _maximum_drawdown(selected["benchmark"])
    return {
        "compounded_strategy": strategy,
        "compounded_nasdaq": nasdaq,
        "compounded_excess_vs_nasdaq": strategy - nasdaq,
        "strategy_maximum_drawdown": strategy_drawdown,
        "nasdaq_maximum_drawdown": nasdaq_drawdown,
        "drawdown_lag_vs_nasdaq": max(0.0, nasdaq_drawdown - strategy_drawdown),
        "turnover": float(selected["turnover"].sum()),
        "stop_exits": int(selected["stop_exits"].sum()),
        "average_invested": float(selected["invested"].mean()),
    }


def _summary(results: dict[int, pd.DataFrame]) -> dict:
    costs = {}
    for cost, result in results.items():
        annual = (
            (1.0 + result[["strategy", "benchmark"]])
            .groupby(result.index.year)
            .prod()
            - 1.0
        )
        annual["excess_vs_nasdaq"] = annual["strategy"] - annual["benchmark"]
        costs[str(cost)] = {
            "annual_training_diagnostics": [
                {"year": int(year), **values}
                for year, values in annual.to_dict(orient="index").items()
            ],
            "positive_training_years_vs_nasdaq": int(
                annual["excess_vs_nasdaq"].gt(0.0).sum()
            ),
            "worst_annual_training_excess_vs_nasdaq": float(
                annual["excess_vs_nasdaq"].min()
            ),
            **_period_metrics(result, DEVELOPMENT_YEARS),
        }
    return {
        "training_years": list(DEVELOPMENT_YEARS),
        "training_years_excluded_from_final_comparison": True,
        "final_comparison_years": [],
        "costs": costs,
    }


def _selection_row(
    key: str,
    results: dict[int, pd.DataFrame],
    years: tuple[int, ...],
) -> dict:
    required_wins = math.ceil(MINIMUM_TRAINING_WIN_RATE * len(years))
    selected_costs = {}
    for cost in COSTS:
        selected = results[cost].loc[results[cost].index.year.isin(years)]
        annual = (
            (1.0 + selected[["strategy", "benchmark"]])
            .groupby(selected.index.year)
            .prod()
            - 1.0
        )
        annual["excess"] = annual["strategy"] - annual["benchmark"]
        period = _period_metrics(results[cost], years)
        selected_costs[cost] = {
            "wins": int(annual["excess"].gt(0.0).sum()),
            "worst": float(annual["excess"].min()),
            "compounded_excess": period["compounded_excess_vs_nasdaq"],
            "drawdown": period["strategy_maximum_drawdown"],
            "drawdown_lag": period["drawdown_lag_vs_nasdaq"],
            "turnover": period["turnover"],
            "stop_exits": period["stop_exits"],
            "average_invested": period["average_invested"],
        }
    eligible = bool(
        selected_costs[30]["compounded_excess"] > 0.0
        and selected_costs[50]["compounded_excess"] > 0.0
        and selected_costs[50]["wins"] >= required_wins
        and selected_costs[50]["drawdown_lag"] <= MAXIMUM_DRAWDOWN_LAG
        and abs(selected_costs[50]["drawdown"]) <= MAXIMUM_ABSOLUTE_DRAWDOWN
    )
    return {
        "candidate": key,
        "training_eligible": eligible,
        "selection_years": list(years),
        "required_positive_training_years": required_wins,
        "positive_training_years_50bps": selected_costs[50]["wins"],
        "worst_annual_training_excess_50bps": selected_costs[50]["worst"],
        "compounded_training_excess_30bps": selected_costs[30][
            "compounded_excess"
        ],
        "compounded_training_excess_50bps": selected_costs[50][
            "compounded_excess"
        ],
        "strategy_drawdown_50bps": selected_costs[50]["drawdown"],
        "drawdown_lag_50bps": selected_costs[50]["drawdown_lag"],
        "turnover_50bps": selected_costs[50]["turnover"],
        "stop_exits_50bps": selected_costs[50]["stop_exits"],
        "average_invested_50bps": selected_costs[50]["average_invested"],
        "final_evidence": False,
    }


def select_candidate(
    results_by_candidate: dict[str, dict[int, pd.DataFrame]],
    years: tuple[int, ...] = DEVELOPMENT_YEARS,
) -> tuple[str | None, list[dict]]:
    ranking = [
        _selection_row(key, results, years)
        for key, results in results_by_candidate.items()
    ]
    ranking.sort(key=lambda row: (
        not row["training_eligible"],
        abs(row["strategy_drawdown_50bps"]),
        -row["worst_annual_training_excess_50bps"],
        -row["compounded_training_excess_50bps"],
        row["turnover_50bps"],
        row["candidate"],
    ))
    for index, row in enumerate(ranking, start=1):
        row["rank"] = index
    selected = next(
        (row["candidate"] for row in ranking if row["training_eligible"]),
        None,
    )
    return selected, ranking


def develop(
    protocol_path: Path = PROTOCOL_PATH,
    output_dir: Path = DEVELOPMENT_OUTPUT_DIR,
) -> dict:
    protocol, protocol_sha = _validated_protocol(protocol_path)
    output_dir = Path(output_dir)
    if output_dir.exists() and any(output_dir.iterdir()):
        raise RuntimeError(f"v37 output will not be overwritten: {output_dir}")
    inputs = _load_inputs()
    base_targets = {
        top_n: generate_base_targets(inputs, top_n) for top_n in TOP_NS
    }
    results_by_candidate = {}
    summaries = {}
    for spec in protocol["candidate_grid"]:
        results = {}
        targets = base_targets[int(spec["top_n"])]
        for cost in COSTS:
            daily = v28.replay_with_individual_trailing_stop(
                inputs["raw_close"],
                inputs["nasdaq"],
                targets,
                DEVELOPMENT_START,
                DEVELOPMENT_END,
                trailing_stop_fraction=float(
                    spec["individual_trailing_stop_fraction"]
                ),
                transaction_cost_bps=float(cost),
            )
            results[cost] = _canonicalize_result(daily, inputs["nasdaq"])
        results_by_candidate[spec["key"]] = results
        summaries[spec["key"]] = _summary(results)

    selected, ranking = select_candidate(results_by_candidate)
    folds = []
    for fold in protocol["walk_forward_folds"]:
        years = tuple(int(year) for year in fold["selection_years"])
        fold_selected, fold_ranking = select_candidate(
            results_by_candidate, years
        )
        ranked_candidate = fold_selected or fold_ranking[0]["candidate"]
        test_year = int(fold["test_year"])
        test = _period_metrics(
            results_by_candidate[ranked_candidate][50], (test_year,)
        )
        folds.append({
            **fold,
            "selected_candidate": ranked_candidate,
            "training_gates_passed": fold_selected is not None,
            "test_excess_vs_nasdaq_50bps": test[
                "compounded_excess_vs_nasdaq"
            ],
            "test_status": (
                "PASS"
                if test["compounded_excess_vs_nasdaq"] > 0.0
                else "BLOCKED"
            ),
            "final_evidence": False,
        })

    output_dir.mkdir(parents=True, exist_ok=True)
    ranking_path = output_dir / "candidate_ranking.csv"
    pd.DataFrame(ranking).to_csv(ranking_path, index=False)
    summaries_path = output_dir / "candidate_summaries.json"
    summaries_path.write_text(
        json.dumps(summaries, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    folds_path = output_dir / "walk_forward_training_diagnostics.csv"
    pd.DataFrame(folds).to_csv(folds_path, index=False)
    outputs = {
        "candidate_ranking": _file_binding(ranking_path),
        "candidate_summaries": _file_binding(summaries_path),
        "walk_forward_training_diagnostics": _file_binding(folds_path),
    }
    selected_spec = None
    selected_summary = None
    if selected is not None:
        selected_spec = next(
            spec for spec in protocol["candidate_grid"] if spec["key"] == selected
        )
        selected_summary = summaries[selected]
        selected_targets = base_targets[int(selected_spec["top_n"])]
        targets_path = output_dir / "selected_targets.csv"
        selected_targets.to_csv(targets_path, index=False)
        outputs["selected_targets"] = _file_binding(targets_path)
        for cost in COSTS:
            path = output_dir / f"selected_daily_{cost}bps.csv"
            results_by_candidate[selected][cost].to_csv(path, index_label="date")
            outputs[f"selected_daily_{cost}bps"] = _file_binding(path)
    report = {
        "schema_version": 1,
        "research_only": True,
        "stage": "V37_BREADTH_STOP_DEVELOPMENT_RESULT",
        "protocol": {"path": str(protocol_path), "sha256": protocol_sha},
        "development_status": "PASS" if selected is not None else "BLOCKED",
        "selected_candidate": selected,
        "selected_specification": selected_spec,
        "selected_summary": selected_summary,
        "evaluation_boundary": protocol["evaluation_boundary"],
        "training_ranking": ranking,
        "walk_forward_training_diagnostics": folds,
        "walk_forward_pass_count": sum(
            fold["test_status"] == "PASS" for fold in folds
        ),
        "all_walk_forward_folds_passed": bool(
            folds and all(fold["test_status"] == "PASS" for fold in folds)
        ),
        "training_years_counted_as_final_wins": False,
        "final_comparison_years": [],
        "contains_index_etf_holdings": False,
        "risk_off_asset": "CASH",
        "2026_used_for_parameter_selection": False,
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
    develop_parser = subparsers.add_parser("develop")
    develop_parser.add_argument("--protocol", type=Path, default=PROTOCOL_PATH)
    develop_parser.add_argument(
        "--output-dir", type=Path, default=DEVELOPMENT_OUTPUT_DIR
    )
    args = parser.parse_args()
    report = (
        freeze_protocol(args.protocol)
        if args.command == "freeze"
        else develop(args.protocol, args.output_dir)
    )
    fields = (
        ("status", "candidate_count", "evaluation_boundary", "protocol")
        if args.command == "freeze"
        else (
            "development_status",
            "selected_candidate",
            "walk_forward_pass_count",
            "training_years_counted_as_final_wins",
            "release_status",
            "manifest",
        )
    )
    print(json.dumps({field: report[field] for field in fields}, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
