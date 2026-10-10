#!/usr/bin/env python3
"""Develop a weekly holding-breadth risk switch on 2020-2025 only.

Stock names remain the frozen monthly Top-5.  At each weekly execution, the
portfolio is invested only when a precommitted fraction of those names closed
above their own 20-day moving average on the preceding session.  Otherwise it
holds cash.  Training years select the breadth threshold but never count as
final wins.
"""

from __future__ import annotations

import argparse
from datetime import datetime
import hashlib
import json
from pathlib import Path

import pandas as pd

from scripts import research_v23_stock_only_frequency as v23
from scripts import research_v30_2019_selection_path_adjudication as v30
from scripts import research_v35_weekly_volatility_budget_development as v35
from scripts import research_v37_breadth_stop_development as v37
from src.conf import CLEANED_PRICE_DATA_DIR, NASDAQ_INDEX_FILE
from src.research.can_slim import replay_can_slim_target_schedule
from src.research.data_quality import back_adjust_common_splits


DEVELOPMENT_START = v35.DEVELOPMENT_START
DEVELOPMENT_END = v35.DEVELOPMENT_END
DEVELOPMENT_YEARS = v35.DEVELOPMENT_YEARS
FINAL_COMPARISON_YEARS: tuple[int, ...] = ()
TRUE_PROSPECTIVE_START = "2026-08-31"
COSTS = v23.COSTS
HEALTHY_FRACTION_THRESHOLDS = (0.40, 0.60, 0.80)
MOVING_AVERAGE_DAYS = 20
FORBIDDEN_ETFS = v23.FORBIDDEN_ETFS

OUTPUT_DIR = Path(
    "output/research_only/v38/weekly_holding_breadth_20260830"
)
PROTOCOL_PATH = OUTPUT_DIR / "frozen_protocol.json"
DEVELOPMENT_OUTPUT_DIR = OUTPUT_DIR / "development_results"
V30_MANIFEST = v30.RESULT_OUTPUT_DIR / "manifest.json"
V30_TARGETS = v30.RESULT_OUTPUT_DIR / "selected_targets.csv"
V37_MANIFEST = v37.DEVELOPMENT_OUTPUT_DIR / "manifest.json"


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
            "key": f"weekly_breadth_{int(threshold * 100)}pct_above_sma20",
            "minimum_healthy_holding_fraction": threshold,
            "moving_average_trading_days": MOVING_AVERAGE_DAYS,
            "risk_rebalance_frequency": "weekly",
            "stock_selection_frequency": "monthly_frozen",
            "risk_off_asset": "CASH",
        }
        for threshold in HEALTHY_FRACTION_THRESHOLDS
    ]


def _validate_sources() -> dict:
    v30_manifest = json.loads(V30_MANIFEST.read_text(encoding="utf-8"))
    if not v30_manifest["audit"]["strategy_selection_path_complete"]:
        raise RuntimeError("v30 base selection path changed")
    v37_manifest = json.loads(V37_MANIFEST.read_text(encoding="utf-8"))
    if v37_manifest["development_status"] != "BLOCKED":
        raise RuntimeError("v37 development status changed")
    return {
        "base_candidate": v30.SELECTED_CANDIDATE,
        "v37_breadth_stop_grid_rejected_before_observation": True,
        "2026_used_to_motivate_architecture": True,
        "2026_used_for_breadth_threshold_selection": False,
    }


def freeze_protocol(path: Path = PROTOCOL_PATH) -> dict:
    path = Path(path)
    if path.exists():
        raise RuntimeError(f"v38 protocol will not be overwritten: {path}")
    runner = Path(__file__).resolve().relative_to(Path.cwd().resolve())
    protocol = {
        "schema_version": 1,
        "research_only": True,
        "stage": "V38_WEEKLY_HOLDING_BREADTH_PRECOMMITMENT",
        "status": "FROZEN_NOT_DEVELOPED",
        "frozen_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "source_diagnosis": _validate_sources(),
        "evaluation_boundary": {
            "development_start": DEVELOPMENT_START,
            "development_end": DEVELOPMENT_END,
            "training_years": list(DEVELOPMENT_YEARS),
            "training_years_excluded_from_final_comparison": True,
            "final_comparison_years": [],
            "2026_used_for_parameter_selection": False,
            "architecture_isolated_from_2026": False,
            "parameter_isolated_from_2026": True,
            "true_prospective_start": TRUE_PROSPECTIVE_START,
        },
        "candidate_grid": candidate_specs(),
        "candidate_count": len(candidate_specs()),
        "policy": {
            "stock_selection": "frozen v30 monthly Top-5 targets",
            "health_signal": "prior close above prior 20-session moving average",
            "risk_decision": "minimum healthy fraction among selected names",
            "risk_rebalance": "weekly next trading close",
            "risk_on_exposure": 1.0,
            "risk_off_asset": "CASH",
            "forbidden_index_etfs": sorted(FORBIDDEN_ETFS),
        },
        "cost_bps": list(COSTS),
        "benchmark": "NASDAQ_COMPOSITE",
        "training_eligibility_gates": {
            "positive_compounded_excess_at_30_and_50bps": True,
            "minimum_positive_training_year_rate_at_50bps": (
                v35.MINIMUM_TRAINING_WIN_RATE
            ),
            "maximum_drawdown_lag_percentage_points_at_50bps": (
                v35.MAXIMUM_DRAWDOWN_LAG * 100.0
            ),
            "maximum_absolute_drawdown_percentage_points_at_50bps": (
                v35.MAXIMUM_ABSOLUTE_DRAWDOWN * 100.0
            ),
            "label": "TRAINING_DIAGNOSTIC_NOT_FINAL_EVIDENCE",
        },
        "selection_order": [
            "training eligible first",
            "absolute drawdown at 50bps ascending",
            "drawdown lag at 50bps ascending",
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
            "v30_manifest": _file_binding(V30_MANIFEST),
            "v30_targets": _file_binding(V30_TARGETS),
            "v35_evaluation_helpers": _file_binding(
                Path("scripts/research_v35_weekly_volatility_budget_development.py")
            ),
            "v37_manifest": _file_binding(V37_MANIFEST),
            "target_replay": _file_binding(Path("src/research/can_slim.py")),
            "data_quality": _file_binding(Path("src/research/data_quality.py")),
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
        raise RuntimeError("v38 protocol status changed")
    if protocol["candidate_grid"] != candidate_specs():
        raise RuntimeError("v38 candidate grid changed")
    if protocol["source_diagnosis"] != _validate_sources():
        raise RuntimeError("v38 source diagnosis changed")
    for name, binding in protocol["input_bindings"].items():
        if "content_manifest_sha256" in binding:
            actual = _directory_binding(Path(binding["path"]), binding["pattern"])
            if actual != binding:
                raise RuntimeError(f"v38 directory binding changed for {name}")
        elif _sha256(Path(binding["path"])) != binding["sha256"]:
            raise RuntimeError(f"v38 file binding changed for {name}")
    return protocol, protocol_sha


def build_weekly_breadth_target_schedule(
    adjusted_close: pd.DataFrame,
    base_targets: pd.DataFrame,
    start: str | pd.Timestamp,
    end: str | pd.Timestamp,
    *,
    minimum_healthy_fraction: float,
    transaction_cost_bps: float,
    moving_average_days: int = MOVING_AVERAGE_DAYS,
) -> pd.DataFrame:
    if not 0.0 <= minimum_healthy_fraction <= 1.0:
        raise ValueError("healthy fraction threshold must be between zero and one")
    if transaction_cost_bps < 0.0:
        raise ValueError("transaction cost must be non-negative")
    start = pd.Timestamp(start).normalize()
    end = pd.Timestamp(end).normalize()
    prices = adjusted_close.sort_index()
    sessions = prices.loc[start:end].index
    if sessions.empty:
        raise ValueError("no sessions in requested breadth schedule window")
    base = base_targets.copy()
    base["effective_date"] = pd.to_datetime(
        base["effective_date"], errors="raise"
    ).dt.normalize()
    base_groups = {
        pd.Timestamp(date): group.copy()
        for date, group in base.groupby("effective_date", sort=True)
    }
    base_dates = sorted(base_groups)
    weekly_signals = (
        pd.Series(sessions, index=sessions)
        .groupby(sessions.to_period("W-FRI"))
        .max()
    )
    weekly_effective = {
        effective
        for signal in weekly_signals
        if (effective := v35._next_session(prices.index, pd.Timestamp(signal))) is not None
        and start <= effective <= end
    }
    event_dates = sorted(
        weekly_effective | {date for date in base_dates if start <= date <= end}
    )
    moving_average = prices.rolling(
        moving_average_days, min_periods=moving_average_days
    ).mean()
    rows = []
    for effective_date in event_dates:
        known = [date for date in base_dates if date <= effective_date]
        if not known:
            continue
        group = base_groups[known[-1]]
        active = group.loc[group["ticker"].ne("__CASH__")]
        tickers = active["ticker"].astype(str).tolist()
        previous_position = int(prices.index.searchsorted(effective_date)) - 1
        if previous_position < 0:
            continue
        cutoff = pd.Timestamp(prices.index[previous_position])
        healthy_fraction = 0.0
        if tickers:
            unknown = set(tickers) - set(prices.columns.astype(str))
            if unknown:
                raise ValueError(f"unknown breadth tickers: {sorted(unknown)}")
            current = prices.loc[cutoff, tickers]
            average = moving_average.loc[cutoff, tickers]
            valid = current.notna() & average.notna()
            if valid.all():
                healthy_fraction = float(current.gt(average).mean())
        risk_on = bool(
            tickers and healthy_fraction >= minimum_healthy_fraction
        )
        if not risk_on:
            rows.append({
                "effective_date": effective_date,
                "ticker": "__CASH__",
                "target_weight": 0.0,
                "base_transaction_cost_bps": float(transaction_cost_bps),
                "breadth_cutoff_date": cutoff,
                "healthy_holding_fraction": healthy_fraction,
                "breadth_risk_on": False,
            })
            continue
        weights = active.set_index("ticker")["target_weight"].astype(float)
        for ticker, weight in weights.items():
            rows.append({
                "effective_date": effective_date,
                "ticker": str(ticker),
                "target_weight": float(weight),
                "base_transaction_cost_bps": float(transaction_cost_bps),
                "breadth_cutoff_date": cutoff,
                "healthy_holding_fraction": healthy_fraction,
                "breadth_risk_on": True,
            })
    schedule = pd.DataFrame(rows)
    if schedule.empty:
        raise RuntimeError("breadth schedule is empty")
    if not (
        pd.to_datetime(schedule["breadth_cutoff_date"])
        < pd.to_datetime(schedule["effective_date"])
    ).all():
        raise RuntimeError("breadth signal used same-day or future prices")
    return schedule


def develop(
    protocol_path: Path = PROTOCOL_PATH,
    output_dir: Path = DEVELOPMENT_OUTPUT_DIR,
) -> dict:
    protocol, protocol_sha = _validated_protocol(protocol_path)
    output_dir = Path(output_dir)
    if output_dir.exists() and any(output_dir.iterdir()):
        raise RuntimeError(f"v38 output will not be overwritten: {output_dir}")
    raw_close, nasdaq, base_targets = v35._load_inputs()
    adjusted_close = back_adjust_common_splits(raw_close).sort_index()
    results_by_candidate = {}
    schedules_by_candidate = {}
    summaries = {}
    for spec in protocol["candidate_grid"]:
        results = {}
        schedules = {}
        for cost in COSTS:
            schedule = build_weekly_breadth_target_schedule(
                adjusted_close,
                base_targets,
                DEVELOPMENT_START,
                DEVELOPMENT_END,
                minimum_healthy_fraction=float(
                    spec["minimum_healthy_holding_fraction"]
                ),
                transaction_cost_bps=float(cost),
            )
            daily, _ = replay_can_slim_target_schedule(
                raw_close,
                nasdaq,
                schedule,
                DEVELOPMENT_START,
                DEVELOPMENT_END,
            )
            results[cost] = v35._canonicalize_result(daily, nasdaq)
            schedules[cost] = schedule
        results_by_candidate[spec["key"]] = results
        schedules_by_candidate[spec["key"]] = schedules
        summaries[spec["key"]] = v35._summary(results)

    selected, ranking = v35.select_candidate(results_by_candidate)
    folds = []
    for fold in protocol["walk_forward_folds"]:
        years = tuple(int(year) for year in fold["selection_years"])
        fold_selected, fold_ranking = v35.select_candidate(
            results_by_candidate, years
        )
        ranked_candidate = fold_selected or fold_ranking[0]["candidate"]
        test_year = int(fold["test_year"])
        test = v35._period_metrics(
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
        schedule_path = output_dir / "selected_weekly_targets_50bps.csv"
        schedules_by_candidate[selected][50].to_csv(schedule_path, index=False)
        outputs["selected_weekly_targets_50bps"] = _file_binding(schedule_path)
        for cost in COSTS:
            path = output_dir / f"selected_daily_{cost}bps.csv"
            results_by_candidate[selected][cost].to_csv(path, index_label="date")
            outputs[f"selected_daily_{cost}bps"] = _file_binding(path)
    report = {
        "schema_version": 1,
        "research_only": True,
        "stage": "V38_WEEKLY_HOLDING_BREADTH_DEVELOPMENT_RESULT",
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
