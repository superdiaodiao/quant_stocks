#!/usr/bin/env python3
"""Develop correlation-diversified Top-5 momentum using 2020-2025 only.

The base selector, momentum lookback, market regime and liquid Top-25 pool are
frozen.  This experiment changes only the greedy Top-5 construction: a new
stock is accepted when its trailing 63-day correlation to every already
selected stock is no greater than a precommitted threshold.  Training years
are parameter-selection diagnostics and never count as final wins.
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
from scripts import research_v29_recovered_2019_stock_momentum as v29
from scripts import research_v30_2019_selection_path_adjudication as v30
from scripts import research_v35_weekly_volatility_budget_development as v35
from src.conf import CLEANED_PRICE_DATA_DIR, NASDAQ_INDEX_FILE
from src.research.can_slim import replay_can_slim_target_schedule
from src.research.universe_history import universe_as_of
from src.strategy.common import (
    market_regime_is_on,
    next_trading_date,
    scheduled_signal_dates,
)


DEVELOPMENT_START = "2020-01-01"
DEVELOPMENT_END = "2025-12-31"
DEVELOPMENT_YEARS = tuple(range(2020, 2026))
FINAL_COMPARISON_YEARS: tuple[int, ...] = ()
TRUE_PROSPECTIVE_START = "2026-08-31"
COSTS = v23.COSTS
CORRELATION_THRESHOLDS = (0.50, 0.60, 0.70, 0.80)
CORRELATION_LOOKBACK_DAYS = 63
MINIMUM_PAIR_OBSERVATIONS = 40
MINIMUM_TRAINING_WIN_RATE = 2.0 / 3.0
MAXIMUM_DRAWDOWN_LAG = 0.10
MAXIMUM_ABSOLUTE_DRAWDOWN = 0.30
FORBIDDEN_ETFS = v23.FORBIDDEN_ETFS

OUTPUT_DIR = Path(
    "output/research_only/v36/correlation_diversified_momentum_20260830"
)
PROTOCOL_PATH = OUTPUT_DIR / "frozen_protocol.json"
DEVELOPMENT_OUTPUT_DIR = OUTPUT_DIR / "development_results"
V30_MANIFEST = v30.RESULT_OUTPUT_DIR / "manifest.json"
V35_MANIFEST = v35.DEVELOPMENT_OUTPUT_DIR / "manifest.json"


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


def base_specification() -> dict:
    manifest = json.loads(V30_MANIFEST.read_text(encoding="utf-8"))
    if manifest["selected_candidate"] != v30.SELECTED_CANDIDATE:
        raise RuntimeError("v30 selected candidate changed")
    return v30.selected_specification()


def candidate_specs() -> list[dict]:
    return [
        {
            "key": f"top5_maxcorr_{int(threshold * 100)}pct",
            "maximum_pairwise_correlation": threshold,
            "correlation_lookback_trading_days": CORRELATION_LOOKBACK_DAYS,
            "minimum_pair_observations": MINIMUM_PAIR_OBSERVATIONS,
            "selection_method": "greedy_momentum_rank_order",
            "top_n": 5,
            "liquid_candidate_pool": 25,
            "signal_frequency": "monthly",
        }
        for threshold in CORRELATION_THRESHOLDS
    ]


def _validate_sources() -> dict:
    v30_manifest = json.loads(V30_MANIFEST.read_text(encoding="utf-8"))
    if not v30_manifest["audit"]["strategy_selection_path_complete"]:
        raise RuntimeError("v30 base selection path is no longer complete")
    v35_manifest = json.loads(V35_MANIFEST.read_text(encoding="utf-8"))
    if v35_manifest["development_status"] != "BLOCKED":
        raise RuntimeError("v35 development status changed")
    return {
        "base_candidate": v30.SELECTED_CANDIDATE,
        "base_specification": base_specification(),
        "base_2019_selection_path_complete": True,
        "overlay_training_window_uses_complete_2020_2025_universe": True,
        "v35_weekly_volatility_budget_rejected_before_observation": True,
        "2026_used_to_motivate_architecture": True,
        "2026_used_for_correlation_threshold_selection": False,
    }


def freeze_protocol(path: Path = PROTOCOL_PATH) -> dict:
    path = Path(path)
    if path.exists():
        raise RuntimeError(f"v36 protocol will not be overwritten: {path}")
    runner = Path(__file__).resolve().relative_to(Path.cwd().resolve())
    protocol = {
        "schema_version": 1,
        "research_only": True,
        "stage": "V36_CORRELATION_DIVERSIFICATION_PRECOMMITMENT",
        "status": "FROZEN_NOT_DEVELOPED",
        "frozen_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "source_diagnosis": _validate_sources(),
        "evaluation_boundary": {
            "development_start": DEVELOPMENT_START,
            "development_end": DEVELOPMENT_END,
            "training_years": list(DEVELOPMENT_YEARS),
            "training_years_excluded_from_final_comparison": True,
            "final_comparison_years": [],
            "2019_role": "base_selector_development_not_overlay_threshold_training",
            "2026_used_for_parameter_selection": False,
            "architecture_isolated_from_2026": False,
            "parameter_isolated_from_2026": True,
            "true_prospective_start": TRUE_PROSPECTIVE_START,
        },
        "candidate_grid": candidate_specs(),
        "candidate_count": len(candidate_specs()),
        "selection_policy": {
            "base_ranking": v30.SELECTED_CANDIDATE,
            "correlation_data_cutoff": "signal close",
            "execution": "next trading close",
            "missing_pair_history": "reject candidate",
            "unfilled_slots": "CASH at one-fifth weight per missing slot",
            "gross_exposure_if_five_selected": 1.0,
            "risk_off_asset": "CASH",
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
            "v26_ranking_helpers": _file_binding(
                Path("scripts/research_v26_large_liquid_stock_momentum.py")
            ),
            "v29_recovered_universe_helpers": _file_binding(
                Path("scripts/research_v29_recovered_2019_stock_momentum.py")
            ),
            "v30_identity_helpers": _file_binding(
                Path("scripts/research_v30_2019_selection_path_adjudication.py")
            ),
            "v30_manifest": _file_binding(V30_MANIFEST),
            "v35_manifest": _file_binding(V35_MANIFEST),
            "target_replay": _file_binding(Path("src/research/can_slim.py")),
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
        raise RuntimeError("v36 protocol status changed")
    if protocol["candidate_grid"] != candidate_specs():
        raise RuntimeError("v36 candidate grid changed")
    if protocol["source_diagnosis"] != _validate_sources():
        raise RuntimeError("v36 source diagnosis changed")
    for name, binding in protocol["input_bindings"].items():
        if "content_manifest_sha256" in binding:
            actual = _directory_binding(Path(binding["path"]), binding["pattern"])
            if actual != binding:
                raise RuntimeError(f"v36 directory binding changed for {name}")
        elif _sha256(Path(binding["path"])) != binding["sha256"]:
            raise RuntimeError(f"v36 file binding changed for {name}")
    return protocol, protocol_sha


def _load_inputs() -> dict:
    inputs = v29._load_inputs()
    snapshots = v30.normalize_meta_identity(
        v29.load_repaired_universe_snapshots()
    )
    universe_cache = {}

    def universe(signal_date):
        stamp = pd.Timestamp(signal_date).normalize()
        if stamp not in universe_cache:
            symbols = universe_as_of(
                snapshots,
                stamp,
                maximum_age_days=v29.MAXIMUM_SNAPSHOT_AGE_DAYS,
            )
            universe_cache[stamp] = (
                None if symbols is None else set(symbols) - FORBIDDEN_ETFS
            )
        return universe_cache[stamp]

    inputs["universe"] = universe
    inputs["technical_cache"] = {}
    inputs["quality_cache"] = {}
    inputs["large_liquid_cache"] = {}
    return inputs


def select_correlation_diversified(
    ranked_tickers: list[str],
    returns: pd.DataFrame,
    signal_date: pd.Timestamp,
    *,
    maximum_pairwise_correlation: float,
    top_n: int = 5,
    lookback_days: int = CORRELATION_LOOKBACK_DAYS,
    minimum_pair_observations: int = MINIMUM_PAIR_OBSERVATIONS,
) -> tuple[list[str], dict]:
    if not -1.0 <= maximum_pairwise_correlation <= 1.0:
        raise ValueError("correlation threshold must be between minus one and one")
    window = returns.loc[:pd.Timestamp(signal_date), ranked_tickers].tail(
        lookback_days
    )
    selected = []
    rejections = []
    accepted_correlations = []
    for ticker in ranked_tickers:
        if len(selected) >= top_n:
            break
        if not selected:
            observations = int(window[ticker].notna().sum())
            if observations < minimum_pair_observations:
                rejections.append({
                    "ticker": ticker,
                    "reason": "INSUFFICIENT_SELF_HISTORY",
                    "observations": observations,
                })
                continue
            selected.append(ticker)
            continue
        pair_correlations = {}
        sufficient = True
        for existing in selected:
            pair = window[[ticker, existing]].dropna()
            if len(pair) < minimum_pair_observations:
                sufficient = False
                break
            pair_correlations[existing] = float(
                pair[ticker].corr(pair[existing])
            )
        if not sufficient or any(pd.isna(value) for value in pair_correlations.values()):
            rejections.append({
                "ticker": ticker,
                "reason": "INSUFFICIENT_PAIR_HISTORY",
            })
            continue
        maximum = max(pair_correlations.values())
        if maximum > maximum_pairwise_correlation:
            rejections.append({
                "ticker": ticker,
                "reason": "CORRELATION_ABOVE_THRESHOLD",
                "maximum_correlation": maximum,
            })
            continue
        selected.append(ticker)
        accepted_correlations.extend(pair_correlations.values())
    return selected, {
        "selected_count": len(selected),
        "maximum_accepted_pairwise_correlation": (
            max(accepted_correlations) if accepted_correlations else None
        ),
        "rejection_count": len(rejections),
        "rejections": rejections,
    }


def generate_target_schedule(
    inputs: dict,
    *,
    maximum_pairwise_correlation: float,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    spec = base_specification()
    close = inputs["close"]
    stock_returns = close.pct_change(fill_method=None)
    index_close = inputs["nasdaq"].reindex(close.index).ffill()
    replay_start = pd.Timestamp(DEVELOPMENT_START) - pd.Timedelta(days=62)
    signals = scheduled_signal_dates(
        close.index, replay_start, DEVELOPMENT_END, "monthly"
    )
    rows = []
    audits = []
    top_n = 5
    for signal_date in signals:
        effective = next_trading_date(close.index, signal_date)
        if (
            effective is None
            or effective < pd.Timestamp(DEVELOPMENT_START)
            or effective > pd.Timestamp(DEVELOPMENT_END)
        ):
            continue
        regime_on = market_regime_is_on(
            signal_date, index_close, v26.v24.MARKET_MA_DAYS
        )
        ranking = []
        selection = []
        audit = {
            "signal_date": pd.Timestamp(signal_date),
            "effective_date": pd.Timestamp(effective),
            "market_regime_on": bool(regime_on),
        }
        if regime_on:
            ranking = v26._large_liquid_ranking(
                signal_date, spec, inputs
            ).index.astype(str).tolist()
            selection, correlation_audit = select_correlation_diversified(
                ranking,
                stock_returns,
                signal_date,
                maximum_pairwise_correlation=maximum_pairwise_correlation,
                top_n=top_n,
            )
            audit.update(correlation_audit)
        else:
            audit.update({
                "selected_count": 0,
                "maximum_accepted_pairwise_correlation": None,
                "rejection_count": 0,
                "rejections": [],
            })
        audit["ranking_count"] = len(ranking)
        audit["selected"] = selection
        audits.append(audit)
        if not selection:
            rows.append({
                "effective_date": effective,
                "ticker": "__CASH__",
                "target_weight": 0.0,
                "base_transaction_cost_bps": 10.0,
            })
            continue
        for ticker in selection:
            rows.append({
                "effective_date": effective,
                "ticker": ticker,
                "target_weight": 1.0 / top_n,
                "base_transaction_cost_bps": 10.0,
            })
    targets = pd.DataFrame(rows)
    tickers = set(targets["ticker"].astype(str)) - {"__CASH__"}
    forbidden = sorted(tickers & FORBIDDEN_ETFS)
    if forbidden:
        raise RuntimeError(f"v36 selected forbidden ETFs: {forbidden}")
    audit_frame = pd.DataFrame(audits)
    return targets, audit_frame


def _canonicalize_result(result: pd.DataFrame, nasdaq: pd.Series) -> pd.DataFrame:
    dates = nasdaq.loc[DEVELOPMENT_START:DEVELOPMENT_END].dropna().index
    missing = dates.difference(result.index)
    if len(missing):
        raise RuntimeError(f"strategy is missing Nasdaq sessions: {list(missing[:5])}")
    result = result.reindex(dates).copy()
    if result[["strategy", "benchmark"]].isna().any().any():
        raise RuntimeError("v36 canonical result contains missing returns")
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
        raise RuntimeError(f"v36 output will not be overwritten: {output_dir}")
    inputs = _load_inputs()
    results_by_candidate = {}
    targets_by_candidate = {}
    audits_by_candidate = {}
    summaries = {}
    for spec in protocol["candidate_grid"]:
        targets, audit = generate_target_schedule(
            inputs,
            maximum_pairwise_correlation=float(
                spec["maximum_pairwise_correlation"]
            ),
        )
        results = {}
        for cost in COSTS:
            stressed = targets.copy()
            stressed["base_transaction_cost_bps"] = float(cost)
            daily, _ = replay_can_slim_target_schedule(
                inputs["raw_close"],
                inputs["nasdaq"],
                stressed,
                DEVELOPMENT_START,
                DEVELOPMENT_END,
            )
            results[cost] = _canonicalize_result(daily, inputs["nasdaq"])
        results_by_candidate[spec["key"]] = results
        targets_by_candidate[spec["key"]] = targets
        audits_by_candidate[spec["key"]] = audit
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
        targets_path = output_dir / "selected_targets.csv"
        targets_by_candidate[selected].to_csv(targets_path, index=False)
        outputs["selected_targets"] = _file_binding(targets_path)
        audit_path = output_dir / "selected_correlation_audit.csv"
        audit_export = audits_by_candidate[selected].copy()
        audit_export["selected"] = audit_export["selected"].map(json.dumps)
        audit_export["rejections"] = audit_export["rejections"].map(json.dumps)
        audit_export.to_csv(audit_path, index=False)
        outputs["selected_correlation_audit"] = _file_binding(audit_path)
        for cost in COSTS:
            path = output_dir / f"selected_daily_{cost}bps.csv"
            results_by_candidate[selected][cost].to_csv(path, index_label="date")
            outputs[f"selected_daily_{cost}bps"] = _file_binding(path)
    report = {
        "schema_version": 1,
        "research_only": True,
        "stage": "V36_CORRELATION_DIVERSIFICATION_DEVELOPMENT_RESULT",
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
