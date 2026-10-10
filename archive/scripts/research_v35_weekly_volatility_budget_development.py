#!/usr/bin/env python3
"""Develop weekly volatility budgeting on frozen monthly stock selections.

The stock names are still selected monthly.  Once per week, total stock
exposure is scaled from the preceding 63 trading days of equal-weight basket
returns; residual capital stays in cash.  Candidate target volatilities are
trained on 2020-2025 only.  Those years are diagnostics, not final wins.
"""

from __future__ import annotations

import argparse
from datetime import datetime
import hashlib
import json
import math
from pathlib import Path

import numpy as np
import pandas as pd

from scripts import research_v23_stock_only_frequency as v23
from scripts import research_v30_2019_selection_path_adjudication as v30
from scripts import research_v34_portfolio_stop_observation as v34
from src.conf import CLEANED_PRICE_DATA_DIR, NASDAQ_INDEX_FILE
from src.research.can_slim import replay_can_slim_target_schedule
from src.research.data_quality import back_adjust_common_splits
from src.research.panel_data import load_panel


DEVELOPMENT_START = "2020-01-01"
DEVELOPMENT_END = "2025-12-31"
DEVELOPMENT_YEARS = tuple(range(2020, 2026))
FINAL_COMPARISON_YEARS: tuple[int, ...] = ()
TRUE_PROSPECTIVE_START = "2026-08-31"
COSTS = v23.COSTS
VOLATILITY_TARGETS = (0.10, 0.15, 0.20, 0.25)
VOLATILITY_LOOKBACK_DAYS = 63
MINIMUM_VOLATILITY_OBSERVATIONS = 40
MINIMUM_TRAINING_WIN_RATE = 2.0 / 3.0
MAXIMUM_DRAWDOWN_LAG = 0.10
MAXIMUM_ABSOLUTE_DRAWDOWN = 0.25
FORBIDDEN_ETFS = v23.FORBIDDEN_ETFS

OUTPUT_DIR = Path(
    "output/research_only/v35/weekly_volatility_budget_20260830"
)
PROTOCOL_PATH = OUTPUT_DIR / "frozen_protocol.json"
DEVELOPMENT_OUTPUT_DIR = OUTPUT_DIR / "development_results"
V30_MANIFEST = v30.RESULT_OUTPUT_DIR / "manifest.json"
V30_TARGETS = v30.RESULT_OUTPUT_DIR / "selected_targets.csv"
V34_MANIFEST = v34.RESULT_OUTPUT_DIR / "manifest.json"


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
            "key": f"weekly_vol_target_{int(target * 100)}pct",
            "annualized_volatility_target": target,
            "volatility_lookback_trading_days": VOLATILITY_LOOKBACK_DAYS,
            "minimum_observations": MINIMUM_VOLATILITY_OBSERVATIONS,
            "risk_rebalance_frequency": "weekly",
            "stock_selection_frequency": "monthly_frozen",
            "maximum_gross_stock_exposure": 1.0,
            "residual_asset": "CASH",
        }
        for target in VOLATILITY_TARGETS
    ]


def _validate_sources() -> dict:
    v30_manifest = json.loads(V30_MANIFEST.read_text(encoding="utf-8"))
    if not v30_manifest["audit"]["strategy_selection_path_complete"]:
        raise RuntimeError("v30 selection path is no longer complete")
    v34_manifest = json.loads(V34_MANIFEST.read_text(encoding="utf-8"))
    if v34_manifest["observation_status"] != "BLOCKED":
        raise RuntimeError("v34 observation status changed")
    failed = sorted(
        name
        for name, passed in v34_manifest["evaluation"]["gates"].items()
        if not passed
    )
    if failed != ["drawdown_vs_nasdaq_50bps"]:
        raise RuntimeError(f"v34 failure diagnosis changed: {failed}")
    return {
        "base_candidate": v30.SELECTED_CANDIDATE,
        "v30_strategy_selection_path_complete": True,
        "v34_failed_gates": failed,
        "weekly_stock_reselection_rejected_by_v23": True,
        "2026_used_to_motivate_architecture": True,
        "2026_used_for_parameter_selection": False,
    }


def freeze_protocol(path: Path = PROTOCOL_PATH) -> dict:
    path = Path(path)
    if path.exists():
        raise RuntimeError(f"v35 protocol will not be overwritten: {path}")
    runner = Path(__file__).resolve().relative_to(Path.cwd().resolve())
    protocol = {
        "schema_version": 1,
        "research_only": True,
        "stage": "V35_WEEKLY_VOLATILITY_BUDGET_PRECOMMITMENT",
        "status": "FROZEN_NOT_DEVELOPED",
        "frozen_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "source_diagnosis": _validate_sources(),
        "evaluation_boundary": {
            "development_start": DEVELOPMENT_START,
            "development_end": DEVELOPMENT_END,
            "training_years": list(DEVELOPMENT_YEARS),
            "training_years_excluded_from_final_comparison": True,
            "final_comparison_years": list(FINAL_COMPARISON_YEARS),
            "2026_used_for_parameter_selection": False,
            "architecture_isolated_from_2026": False,
            "parameter_isolated_from_2026": True,
            "true_prospective_start": TRUE_PROSPECTIVE_START,
        },
        "candidate_grid": candidate_specs(),
        "candidate_count": len(candidate_specs()),
        "policy": {
            "stock_selection": "frozen v30 monthly targets",
            "risk_signal": (
                "equal-weight selected-basket realized volatility through the "
                "trading close before execution"
            ),
            "risk_rebalance": "weekly next trading close",
            "monthly_selection_execution": "unchanged",
            "gross_exposure": "min(1, target_volatility / forecast_volatility)",
            "residual_asset": "CASH",
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
            "drawdown lag versus Nasdaq at 50bps ascending",
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
            "v23_frequency_runner": _file_binding(
                Path("scripts/research_v23_stock_only_frequency.py")
            ),
            "v30_manifest": _file_binding(V30_MANIFEST),
            "v30_targets": _file_binding(V30_TARGETS),
            "v34_manifest": _file_binding(V34_MANIFEST),
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
        raise RuntimeError("v35 protocol status changed")
    if protocol["candidate_grid"] != candidate_specs():
        raise RuntimeError("v35 candidate grid changed")
    if protocol["source_diagnosis"] != _validate_sources():
        raise RuntimeError("v35 source diagnosis changed")
    for name, binding in protocol["input_bindings"].items():
        if "content_manifest_sha256" in binding:
            actual = _directory_binding(Path(binding["path"]), binding["pattern"])
            if actual != binding:
                raise RuntimeError(f"v35 directory binding changed for {name}")
        elif _sha256(Path(binding["path"])) != binding["sha256"]:
            raise RuntimeError(f"v35 file binding changed for {name}")
    return protocol, protocol_sha


def _load_inputs() -> tuple[pd.DataFrame, pd.Series, pd.DataFrame]:
    load_start = (
        pd.Timestamp(DEVELOPMENT_START) - pd.Timedelta(days=500)
    ).strftime("%Y-%m-%d")
    raw_close, _ = load_panel(
        CLEANED_PRICE_DATA_DIR, load_start, DEVELOPMENT_END
    )
    nasdaq = pd.read_csv(
        NASDAQ_INDEX_FILE, index_col="date", parse_dates=True
    )["close"].sort_index().loc[:DEVELOPMENT_END]
    targets = pd.read_csv(V30_TARGETS, parse_dates=["effective_date"])
    targets = targets.loc[
        targets["effective_date"].between(DEVELOPMENT_START, DEVELOPMENT_END)
    ].copy()
    tickers = set(targets["ticker"].astype(str)) - {"__CASH__"}
    forbidden = sorted(tickers & FORBIDDEN_ETFS)
    if forbidden:
        raise RuntimeError(f"v35 targets contain forbidden ETFs: {forbidden}")
    return raw_close, nasdaq, targets


def _next_session(index: pd.DatetimeIndex, date: pd.Timestamp) -> pd.Timestamp | None:
    position = int(index.searchsorted(pd.Timestamp(date), side="right"))
    return pd.Timestamp(index[position]) if position < len(index) else None


def build_weekly_volatility_target_schedule(
    adjusted_close: pd.DataFrame,
    base_targets: pd.DataFrame,
    start: str | pd.Timestamp,
    end: str | pd.Timestamp,
    *,
    annualized_volatility_target: float,
    transaction_cost_bps: float,
    lookback_days: int = VOLATILITY_LOOKBACK_DAYS,
    minimum_observations: int = MINIMUM_VOLATILITY_OBSERVATIONS,
) -> pd.DataFrame:
    if not 0.0 < annualized_volatility_target < 1.0:
        raise ValueError("annualized volatility target must be between zero and one")
    if transaction_cost_bps < 0.0:
        raise ValueError("transaction cost must be non-negative")
    start = pd.Timestamp(start).normalize()
    end = pd.Timestamp(end).normalize()
    prices = adjusted_close.sort_index()
    sessions = prices.loc[start:end].index
    if sessions.empty:
        raise ValueError("no sessions in requested volatility schedule window")
    base = base_targets.copy()
    base["effective_date"] = pd.to_datetime(
        base["effective_date"], errors="raise"
    ).dt.normalize()
    base_groups = {
        pd.Timestamp(date): group.copy()
        for date, group in base.groupby("effective_date", sort=True)
    }
    base_dates = sorted(base_groups)
    if not base_dates:
        raise ValueError("base target schedule is empty")

    weekly_signals = (
        pd.Series(sessions, index=sessions)
        .groupby(sessions.to_period("W-FRI"))
        .max()
    )
    weekly_effective = {
        effective
        for signal in weekly_signals
        if (effective := _next_session(prices.index, pd.Timestamp(signal))) is not None
        and start <= effective <= end
    }
    event_dates = sorted(
        weekly_effective
        | {date for date in base_dates if start <= date <= end}
    )
    simple_returns = prices.pct_change(fill_method=None)
    rows = []
    for effective_date in event_dates:
        known_base_dates = [date for date in base_dates if date <= effective_date]
        if not known_base_dates:
            continue
        group = base_groups[known_base_dates[-1]]
        active = group.loc[group["ticker"].ne("__CASH__")]
        tickers = active["ticker"].astype(str).tolist()
        previous_position = int(prices.index.searchsorted(effective_date)) - 1
        if previous_position < 0:
            continue
        cutoff = pd.Timestamp(prices.index[previous_position])
        forecast = float("nan")
        exposure = 0.0
        if tickers:
            unknown = set(tickers) - set(prices.columns.astype(str))
            if unknown:
                raise ValueError(f"unknown volatility tickers: {sorted(unknown)}")
            window = simple_returns.loc[:cutoff, tickers].tail(lookback_days)
            complete = window.dropna(how="any")
            if len(complete) >= minimum_observations:
                basket = complete.mean(axis=1)
                forecast = float(basket.std(ddof=1) * np.sqrt(252.0))
                if np.isfinite(forecast) and forecast > 0.0:
                    exposure = min(1.0, annualized_volatility_target / forecast)
        if not tickers or exposure <= 0.0:
            rows.append({
                "effective_date": effective_date,
                "ticker": "__CASH__",
                "target_weight": 0.0,
                "base_transaction_cost_bps": float(transaction_cost_bps),
                "forecast_annualized_volatility": forecast,
                "gross_stock_exposure": 0.0,
                "volatility_cutoff_date": cutoff,
            })
            continue
        weight = exposure / len(tickers)
        for ticker in tickers:
            rows.append({
                "effective_date": effective_date,
                "ticker": ticker,
                "target_weight": weight,
                "base_transaction_cost_bps": float(transaction_cost_bps),
                "forecast_annualized_volatility": forecast,
                "gross_stock_exposure": exposure,
                "volatility_cutoff_date": cutoff,
            })
    schedule = pd.DataFrame(rows)
    if schedule.empty:
        raise RuntimeError("volatility schedule is empty")
    if not (
        pd.to_datetime(schedule["volatility_cutoff_date"])
        < pd.to_datetime(schedule["effective_date"])
    ).all():
        raise RuntimeError("volatility forecast used same-day or future returns")
    return schedule


def _canonicalize_result(
    result: pd.DataFrame,
    nasdaq: pd.Series,
) -> pd.DataFrame:
    dates = nasdaq.loc[DEVELOPMENT_START:DEVELOPMENT_END].dropna().index
    missing = dates.difference(result.index)
    if len(missing):
        raise RuntimeError(f"strategy is missing Nasdaq sessions: {list(missing[:5])}")
    result = result.reindex(dates).copy()
    if result[["strategy", "benchmark"]].isna().any().any():
        raise RuntimeError("v35 canonical result contains missing returns")
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
        row["drawdown_lag_50bps"],
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
        raise RuntimeError(f"v35 output will not be overwritten: {output_dir}")
    raw_close, nasdaq, base_targets = _load_inputs()
    adjusted_close = back_adjust_common_splits(raw_close).sort_index()
    results_by_candidate = {}
    schedules_by_candidate = {}
    summaries = {}
    for spec in protocol["candidate_grid"]:
        results = {}
        schedules = {}
        for cost in COSTS:
            schedule = build_weekly_volatility_target_schedule(
                adjusted_close,
                base_targets,
                DEVELOPMENT_START,
                DEVELOPMENT_END,
                annualized_volatility_target=float(
                    spec["annualized_volatility_target"]
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
            results[cost] = _canonicalize_result(daily, nasdaq)
            schedules[cost] = schedule
        results_by_candidate[spec["key"]] = results
        schedules_by_candidate[spec["key"]] = schedules
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
        "stage": "V35_WEEKLY_VOLATILITY_BUDGET_DEVELOPMENT_RESULT",
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
