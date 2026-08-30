#!/usr/bin/env python3
"""Develop stock-level trailing-stop risk control without using 2026.

The v26 stock selector passed 2019-2025 return gates, but its frozen 2026
observation failed only the drawdown gate. This experiment applies standard
10/15/20/25 percent individual-stock trailing stops to the already-selected
v26 monthly targets. Threshold selection uses the fully covered 2020-2025
period only. A stopped position remains cash until the next monthly rebalance.
"""

from __future__ import annotations

import argparse
from datetime import datetime
import hashlib
import json
import math
from pathlib import Path

import pandas as pd

from scripts import research_v15_benchmark_core_development as v15
from scripts import research_v23_stock_only_frequency as v23
from scripts import research_v26_large_liquid_stock_momentum as v26
from scripts import research_v27_stock_only_2026_observation as v27
from src.conf import CLEANED_PRICE_DATA_DIR, NASDAQ_INDEX_FILE
from src.research.data_quality import (
    back_adjust_common_splits,
    stock_returns_with_delisting_penalty,
)
from src.research.panel_data import load_panel


DEVELOPMENT_START = "2020-01-01"
DEVELOPMENT_END = "2025-12-31"
DEVELOPMENT_YEARS = tuple(range(2020, 2026))
OBSERVATION_START = "2026-08-01"
COSTS = v23.COSTS
STOP_THRESHOLDS = (0.10, 0.15, 0.20, 0.25)
MINIMUM_WIN_RATE = 0.70
MAXIMUM_DRAWDOWN_LAG = 0.10
FORBIDDEN_ETFS = v23.FORBIDDEN_ETFS
OUTPUT_DIR = Path(
    "output/research_only/v28/stock_trailing_stop_20260830"
)
PROTOCOL_PATH = OUTPUT_DIR / "frozen_protocol.json"
DEVELOPMENT_OUTPUT_DIR = OUTPUT_DIR / "development_results"
V26_MANIFEST = v26.DEVELOPMENT_OUTPUT_DIR / "manifest.json"
V26_TARGETS = v26.DEVELOPMENT_OUTPUT_DIR / "selected_targets.csv"
V27_MANIFEST = v27.RESULT_OUTPUT_DIR / "manifest.json"


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
            "key": f"individual_trailing_stop_{int(threshold * 100)}pct",
            "trailing_stop_fraction": threshold,
            "stop_signal_frequency": "daily",
            "reentry_policy": "next_monthly_rebalance_only",
        }
        for threshold in STOP_THRESHOLDS
    ]


def _validate_sources() -> dict:
    v26_manifest = json.loads(V26_MANIFEST.read_text(encoding="utf-8"))
    if v26_manifest["development_status"] != "PASS":
        raise RuntimeError("v26 development status changed")
    if v26_manifest["selected_candidate"] != v27.SELECTED_CANDIDATE:
        raise RuntimeError("v26 selected candidate changed")
    v27_manifest = json.loads(V27_MANIFEST.read_text(encoding="utf-8"))
    if v27_manifest["observation_status"] != "BLOCKED":
        raise RuntimeError("v27 observation status changed")
    gates = v27_manifest["evaluation"]["gates"]
    failed = sorted(name for name, passed in gates.items() if not passed)
    if failed != ["drawdown_50bps"]:
        raise RuntimeError(f"v27 failure diagnosis changed: {failed}")
    return {
        "base_candidate": v27.SELECTED_CANDIDATE,
        "v27_failed_gates": failed,
        "v27_2026_used_for_threshold_selection": False,
    }


def freeze_protocol(path: Path = PROTOCOL_PATH) -> dict:
    path = Path(path)
    if path.exists():
        raise RuntimeError(f"v28 protocol will not be overwritten: {path}")
    runner = Path(__file__).resolve().relative_to(Path.cwd().resolve())
    protocol = {
        "schema_version": 1,
        "research_only": True,
        "stage": "V28_STOCK_TRAILING_STOP_PRECOMMITMENT",
        "status": "FROZEN_NOT_DEVELOPED",
        "frozen_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "objective": (
            "Control single-stock drawdown while preserving the frozen v26 "
            "pure-stock monthly selector."
        ),
        "source_diagnosis": _validate_sources(),
        "development_window": {
            "start": DEVELOPMENT_START,
            "end": DEVELOPMENT_END,
            "years": list(DEVELOPMENT_YEARS),
            "monthly_pit_universe_signals_available": 72,
            "monthly_pit_universe_signals_expected": 72,
            "2026_used_for_threshold_selection": False,
        },
        "reserved_new_observation_start": OBSERVATION_START,
        "candidate_grid": candidate_specs(),
        "candidate_count": len(candidate_specs()),
        "risk_policy": {
            "signal": "adjusted close drawdown from peak since latest entry",
            "execution": "next trading close after stop signal",
            "sale_scope": "triggered stock only",
            "other_positions": "shares unchanged",
            "stopped_weight": "CASH",
            "reentry": "next frozen monthly target only",
            "monthly_target_priority_on_same_execution_date": True,
        },
        "cost_bps": list(COSTS),
        "primary_benchmark": "QQQ_TOTAL_RETURN",
        "secondary_benchmark": "NASDAQ_COMPOSITE",
        "eligibility_gates": {
            "positive_compounded_excess_vs_qqq_at_30_and_50bps": True,
            "minimum_annual_win_rate_vs_qqq_at_50bps": MINIMUM_WIN_RATE,
            "maximum_drawdown_lag_vs_qqq_percentage_points": (
                MAXIMUM_DRAWDOWN_LAG * 100.0
            ),
            "positive_targets_must_exclude_all_forbidden_etfs": True,
        },
        "selection_order": [
            "eligible first",
            "annual wins versus QQQ at 50bps descending",
            "worst annual excess versus QQQ at 50bps descending",
            "compounded excess versus QQQ at 50bps descending",
            "turnover at 50bps ascending",
            "candidate key ascending",
        ],
        "walk_forward_folds": [
            {"selection_years": list(range(2020, year)), "test_year": year}
            for year in range(2022, 2026)
        ],
        "input_bindings": {
            "runner": _file_binding(runner),
            "v26_selector": _file_binding(
                Path("scripts/research_v26_large_liquid_stock_momentum.py")
            ),
            "v26_manifest": _file_binding(V26_MANIFEST),
            "v26_targets": _file_binding(V26_TARGETS),
            "v27_observer": _file_binding(
                Path("scripts/research_v27_stock_only_2026_observation.py")
            ),
            "v27_manifest": _file_binding(V27_MANIFEST),
            "data_quality": _file_binding(Path("src/research/data_quality.py")),
            "price_directory": _directory_binding(
                Path(CLEANED_PRICE_DATA_DIR), "*.csv"
            ),
            "nasdaq_index": _file_binding(Path(NASDAQ_INDEX_FILE)),
            "qqq_history": _file_binding(Path(v15.QQQ_HISTORY["path"])),
        },
        "parameters_frozen_before_development": True,
        "brokerage_or_trading_authorized": False,
        "broker_connection_used": False,
        "order_created": False,
        "capital_allocated": False,
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
        raise RuntimeError("v28 protocol status changed")
    if protocol["candidate_grid"] != candidate_specs():
        raise RuntimeError("v28 candidate grid changed")
    if protocol["source_diagnosis"] != _validate_sources():
        raise RuntimeError("v28 source diagnosis changed")
    if protocol["release_status"] != "BLOCKED" or protocol["promotion_eligible"]:
        raise RuntimeError("v28 release boundary changed")
    for name, binding in protocol["input_bindings"].items():
        if "content_manifest_sha256" in binding:
            actual = _directory_binding(Path(binding["path"]), binding["pattern"])
            if actual != binding:
                raise RuntimeError(f"v28 directory binding changed for {name}")
        elif _sha256(Path(binding["path"])) != binding["sha256"]:
            raise RuntimeError(f"v28 file binding changed for {name}")
    return protocol, protocol_sha


def _load_inputs() -> tuple[pd.DataFrame, pd.Series, pd.DataFrame, pd.DataFrame]:
    load_start = (
        pd.Timestamp(DEVELOPMENT_START) - pd.Timedelta(days=400)
    ).strftime("%Y-%m-%d")
    raw_close, _ = load_panel(
        CLEANED_PRICE_DATA_DIR, load_start, DEVELOPMENT_END
    )
    nasdaq = pd.read_csv(
        NASDAQ_INDEX_FILE, index_col="date", parse_dates=True
    )["close"].sort_index().loc[:DEVELOPMENT_END]
    qqq = pd.read_csv(
        v15.QQQ_HISTORY["path"], index_col="date", parse_dates=True
    ).sort_index().loc[:DEVELOPMENT_END]
    targets = pd.read_csv(V26_TARGETS, parse_dates=["effective_date"])
    targets = targets.loc[
        targets["effective_date"].between(DEVELOPMENT_START, DEVELOPMENT_END)
    ].copy()
    tickers = set(targets["ticker"].astype(str)) - {"__CASH__"}
    forbidden = sorted(tickers & FORBIDDEN_ETFS)
    if forbidden:
        raise RuntimeError(f"base targets contain forbidden ETFs: {forbidden}")
    return raw_close, nasdaq, qqq, targets


def _target_dict(
    prices: pd.DataFrame,
    target_schedule: pd.DataFrame,
    cost_bps: float,
) -> dict[pd.Timestamp, pd.Series]:
    schedule = target_schedule.copy()
    schedule["effective_date"] = pd.to_datetime(
        schedule["effective_date"], errors="raise"
    ).dt.normalize()
    targets = {}
    for effective_date, group in schedule.groupby("effective_date", sort=True):
        target = pd.Series(0.0, index=prices.columns)
        active = group.loc[group["ticker"].ne("__CASH__")]
        unknown = set(active["ticker"].astype(str)) - set(prices.columns.astype(str))
        if unknown:
            raise ValueError(f"unknown target tickers: {sorted(unknown)}")
        target.loc[active["ticker"].astype(str)] = active[
            "target_weight"
        ].astype(float).to_numpy()
        if target.lt(0).any() or float(target.sum()) > 1.0 + 1e-9:
            raise ValueError(f"invalid target weights on {effective_date.date()}")
        targets[pd.Timestamp(effective_date)] = target
    if cost_bps < 0.0:
        raise ValueError("transaction cost must be non-negative")
    return targets


def replay_with_individual_trailing_stop(
    raw_close: pd.DataFrame,
    index_close: pd.Series,
    target_schedule: pd.DataFrame,
    start: str | pd.Timestamp,
    end: str | pd.Timestamp,
    *,
    trailing_stop_fraction: float,
    transaction_cost_bps: float,
) -> pd.DataFrame:
    if not 0.0 < trailing_stop_fraction < 1.0:
        raise ValueError("trailing stop must be between zero and one")
    start = pd.Timestamp(start).normalize()
    end = pd.Timestamp(end).normalize()
    prices = back_adjust_common_splits(raw_close).sort_index()
    returns = stock_returns_with_delisting_penalty(prices).fillna(0.0)
    benchmark = index_close.reindex(prices.index).ffill().pct_change(
        fill_method=None
    ).fillna(0.0)
    targets = _target_dict(prices, target_schedule, transaction_cost_bps)
    position_values = pd.Series(0.0, index=prices.columns)
    peaks: dict[str, float] = {}
    pending_exits: set[str] = set()
    cash = 1.0
    nav = 1.0
    rows = []
    cost_rate = float(transaction_cost_bps) / 10_000.0
    trading_dates = prices.index
    for position, (current_date, daily_returns) in enumerate(returns.iterrows()):
        previous_nav = nav
        position_values = position_values.mul(1.0 + daily_returns)
        pre_trade_nav = float(cash + position_values.sum())
        turnover = 0.0
        transaction_cost = 0.0
        stop_exit_count = 0
        target = targets.get(pd.Timestamp(current_date))
        if target is not None:
            previously_active = set(
                position_values.index[position_values.gt(1e-12)].astype(str)
            )
            previous_peaks = dict(peaks)
            post_trade_nav = pre_trade_nav
            for _ in range(20):
                desired = target * post_trade_nav
                traded = float((desired - position_values).abs().sum())
                updated = pre_trade_nav - traded * cost_rate
                if abs(updated - post_trade_nav) < 1e-12:
                    post_trade_nav = updated
                    break
                post_trade_nav = updated
            desired = target * post_trade_nav
            traded = float((desired - position_values).abs().sum())
            transaction_cost = traded * cost_rate
            turnover = traded / pre_trade_nav if pre_trade_nav else 0.0
            cash = float(pre_trade_nav - desired.sum() - transaction_cost)
            position_values = desired
            peaks = {}
            for ticker in position_values.index[position_values.gt(1e-12)]:
                price = prices.at[current_date, ticker]
                if pd.isna(price):
                    continue
                peaks[str(ticker)] = (
                    max(previous_peaks.get(str(ticker), float(price)), float(price))
                    if str(ticker) in previously_active
                    else float(price)
                )
            pending_exits.clear()
        elif pending_exits:
            active_exits = [
                ticker for ticker in sorted(pending_exits)
                if float(position_values.get(ticker, 0.0)) > 1e-12
            ]
            sold = float(position_values.loc[active_exits].sum()) if active_exits else 0.0
            transaction_cost = sold * cost_rate
            turnover = sold / pre_trade_nav if pre_trade_nav else 0.0
            cash += sold - transaction_cost
            position_values.loc[active_exits] = 0.0
            stop_exit_count = len(active_exits)
            for ticker in active_exits:
                peaks.pop(ticker, None)
            pending_exits.clear()
        nav = float(cash + position_values.sum())

        next_date = trading_dates[position + 1] if position + 1 < len(trading_dates) else None
        if next_date is not None and pd.Timestamp(next_date) not in targets:
            for ticker in position_values.index[position_values.gt(1e-12)]:
                price = prices.at[current_date, ticker]
                if pd.isna(price):
                    continue
                peak = max(peaks.get(ticker, float(price)), float(price))
                peaks[ticker] = peak
                if float(price) <= peak * (1.0 - trailing_stop_fraction):
                    pending_exits.add(str(ticker))
        rows.append({
            "strategy": nav / previous_nav - 1.0 if previous_nav else 0.0,
            "benchmark": float(benchmark.loc[current_date]),
            "invested": float(position_values.sum() / nav) if nav else 0.0,
            "turnover": turnover,
            "transaction_cost": transaction_cost,
            "holdings": int(position_values.gt(1e-12).sum()),
            "stop_exits": stop_exit_count,
            "portfolio_value": nav,
            "cash": cash,
        })
    return pd.DataFrame(rows, index=prices.index).loc[start:end]


def _canonicalize_result(
    result: pd.DataFrame,
    nasdaq: pd.Series,
    qqq: pd.DataFrame,
) -> pd.DataFrame:
    dates = nasdaq.loc[DEVELOPMENT_START:DEVELOPMENT_END].dropna().index
    missing = dates.difference(result.index)
    if len(missing):
        raise RuntimeError(f"strategy is missing Nasdaq sessions: {list(missing[:5])}")
    result = result.reindex(dates).copy()
    qqq_dates = qqq.loc[:DEVELOPMENT_END].dropna(subset=["close"]).index
    qqq_index = v15.qqq_total_return_index(
        qqq,
        qqq_dates,
        allowed_market_closed=pd.Series(False, index=qqq_dates),
    )
    qqq_returns = qqq_index.pct_change(fill_method=None)
    missing_qqq = dates.difference(qqq_returns.dropna().index)
    if len(missing_qqq):
        raise RuntimeError(f"QQQ is missing Nasdaq sessions: {list(missing_qqq[:5])}")
    result["qqq"] = qqq_returns.reindex(dates)
    return result


def _maximum_drawdown(series: pd.Series) -> float:
    nav = (1.0 + series.astype(float)).cumprod()
    return float(nav.div(nav.cummax()).sub(1.0).min())


def _period_key(years: tuple[int, ...]) -> str:
    if not years:
        raise ValueError("selection years must not be empty")
    expected = tuple(range(years[0], years[-1] + 1))
    if years != expected or years[0] != DEVELOPMENT_YEARS[0]:
        raise ValueError("selection years must be a contiguous prefix from 2020")
    return f"{years[0]}-{years[-1]}"


def _period_metrics(result: pd.DataFrame, years: tuple[int, ...]) -> dict:
    selected = result.loc[result.index.year.isin(years)]
    observed_years = tuple(sorted(set(selected.index.year.astype(int))))
    if observed_years != years:
        raise RuntimeError(f"result years {observed_years} != {years}")
    strategy = float((1.0 + selected["strategy"]).prod() - 1.0)
    qqq = float((1.0 + selected["qqq"]).prod() - 1.0)
    nasdaq = float((1.0 + selected["benchmark"]).prod() - 1.0)
    strategy_drawdown = _maximum_drawdown(selected["strategy"])
    qqq_drawdown = _maximum_drawdown(selected["qqq"])
    return {
        "compounded_strategy": strategy,
        "compounded_qqq": qqq,
        "compounded_nasdaq": nasdaq,
        "compounded_excess_vs_qqq": strategy - qqq,
        "compounded_excess_vs_nasdaq": strategy - nasdaq,
        "strategy_maximum_drawdown": strategy_drawdown,
        "qqq_maximum_drawdown": qqq_drawdown,
        "drawdown_lag_vs_qqq": max(0.0, qqq_drawdown - strategy_drawdown),
        "turnover": float(selected["turnover"].sum()),
        "stop_exits": int(selected["stop_exits"].sum()),
    }


def _summary(results: dict[int, pd.DataFrame]) -> dict:
    costs = {}
    for cost, result in results.items():
        annual = (
            (1.0 + result[["strategy", "benchmark", "qqq"]])
            .groupby(result.index.year)
            .prod()
            - 1.0
        )
        annual["excess_vs_qqq"] = annual["strategy"] - annual["qqq"]
        annual["excess_vs_nasdaq"] = annual["strategy"] - annual["benchmark"]
        periods = {
            _period_key(DEVELOPMENT_YEARS[:end]): _period_metrics(
                result, DEVELOPMENT_YEARS[:end]
            )
            for end in range(1, len(DEVELOPMENT_YEARS) + 1)
        }
        full = periods[_period_key(DEVELOPMENT_YEARS)]
        costs[str(cost)] = {
            "annual": [
                {"year": int(year), **values}
                for year, values in annual.to_dict(orient="index").items()
            ],
            "annual_wins_vs_qqq": int(annual["excess_vs_qqq"].gt(0.0).sum()),
            "worst_annual_excess_vs_qqq": float(annual["excess_vs_qqq"].min()),
            **full,
            "periods": periods,
        }
    return {"costs": costs}


def _selection_row(key: str, summary: dict, years: tuple[int, ...]) -> dict:
    period_key = _period_key(years)
    required_wins = math.ceil(MINIMUM_WIN_RATE * len(years))
    selected_costs = {}
    for cost in COSTS:
        rows = [
            row for row in summary["costs"][str(cost)]["annual"]
            if int(row["year"]) in years
        ]
        observed_years = tuple(sorted(int(row["year"]) for row in rows))
        if observed_years != years:
            raise RuntimeError(f"{key} cost {cost} years {observed_years} != {years}")
        period = summary["costs"][str(cost)]["periods"][period_key]
        selected_costs[cost] = {
            "wins": sum(float(row["excess_vs_qqq"]) > 0.0 for row in rows),
            "worst": min(float(row["excess_vs_qqq"]) for row in rows),
            "compounded_excess": period["compounded_excess_vs_qqq"],
            "drawdown_lag": period["drawdown_lag_vs_qqq"],
            "turnover": period["turnover"],
            "stop_exits": period["stop_exits"],
        }
    eligible = bool(
        selected_costs[30]["compounded_excess"] > 0.0
        and selected_costs[50]["compounded_excess"] > 0.0
        and selected_costs[50]["wins"] >= required_wins
        and selected_costs[50]["drawdown_lag"] <= MAXIMUM_DRAWDOWN_LAG
    )
    return {
        "candidate": key,
        "eligible": eligible,
        "required_wins": required_wins,
        "wins_vs_qqq_50bps": selected_costs[50]["wins"],
        "worst_annual_excess_vs_qqq_50bps": selected_costs[50]["worst"],
        "compounded_excess_vs_qqq_30bps": selected_costs[30][
            "compounded_excess"
        ],
        "compounded_excess_vs_qqq_50bps": selected_costs[50][
            "compounded_excess"
        ],
        "drawdown_lag_vs_qqq_50bps": selected_costs[50]["drawdown_lag"],
        "turnover_50bps": selected_costs[50]["turnover"],
        "stop_exits_50bps": selected_costs[50]["stop_exits"],
    }


def select_candidate(
    summaries: dict[str, dict],
    years: tuple[int, ...] = DEVELOPMENT_YEARS,
) -> tuple[str | None, list[dict]]:
    ranking = [
        _selection_row(key, summary, years)
        for key, summary in summaries.items()
    ]
    ranking.sort(key=lambda row: (
        not row["eligible"],
        -row["wins_vs_qqq_50bps"],
        -row["worst_annual_excess_vs_qqq_50bps"],
        -row["compounded_excess_vs_qqq_50bps"],
        row["turnover_50bps"],
        row["candidate"],
    ))
    for index, row in enumerate(ranking, start=1):
        row["rank"] = index
    selected = next(
        (row["candidate"] for row in ranking if row["eligible"]), None
    )
    return selected, ranking


def develop(
    protocol_path: Path = PROTOCOL_PATH,
    output_dir: Path = DEVELOPMENT_OUTPUT_DIR,
) -> dict:
    protocol, protocol_sha = _validated_protocol(protocol_path)
    output_dir = Path(output_dir)
    if output_dir.exists() and any(output_dir.iterdir()):
        raise RuntimeError(f"v28 development output will not be overwritten: {output_dir}")
    raw_close, nasdaq, qqq, targets = _load_inputs()
    results_by_candidate = {}
    summaries = {}
    for spec in protocol["candidate_grid"]:
        results = {}
        for cost in COSTS:
            daily = replay_with_individual_trailing_stop(
                raw_close,
                nasdaq,
                targets,
                DEVELOPMENT_START,
                DEVELOPMENT_END,
                trailing_stop_fraction=float(spec["trailing_stop_fraction"]),
                transaction_cost_bps=float(cost),
            )
            results[cost] = _canonicalize_result(daily, nasdaq, qqq)
        results_by_candidate[spec["key"]] = results
        summaries[spec["key"]] = _summary(results)

    selected, ranking = select_candidate(summaries)
    folds = []
    for fold in protocol["walk_forward_folds"]:
        years = tuple(int(year) for year in fold["selection_years"])
        fold_selected, fold_ranking = select_candidate(summaries, years)
        test_year = int(fold["test_year"])
        ranked_candidate = fold_selected or fold_ranking[0]["candidate"]
        test = next(
            row for row in summaries[ranked_candidate]["costs"]["50"]["annual"]
            if int(row["year"]) == test_year
        )
        folds.append({
            **fold,
            "selected_candidate": ranked_candidate,
            "training_gates_passed": fold_selected is not None,
            "test_excess_vs_qqq_50bps": float(test["excess_vs_qqq"]),
            "test_status": (
                "PASS" if float(test["excess_vs_qqq"]) > 0.0 else "BLOCKED"
            ),
        })

    output_dir.mkdir(parents=True, exist_ok=True)
    ranking_path = output_dir / "candidate_ranking.csv"
    pd.DataFrame(ranking).to_csv(ranking_path, index=False)
    summary_path = output_dir / "candidate_summaries.json"
    summary_path.write_text(
        json.dumps(summaries, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    folds_path = output_dir / "walk_forward_folds.csv"
    pd.DataFrame(folds).to_csv(folds_path, index=False)
    outputs = {
        "candidate_ranking": _file_binding(ranking_path),
        "candidate_summaries": _file_binding(summary_path),
        "walk_forward_folds": _file_binding(folds_path),
    }
    selected_spec = None
    selected_summary = None
    if selected is not None:
        selected_spec = next(
            spec for spec in protocol["candidate_grid"] if spec["key"] == selected
        )
        selected_summary = summaries[selected]
        for cost in COSTS:
            path = output_dir / f"selected_daily_{cost}bps.csv"
            results_by_candidate[selected][cost].to_csv(path, index_label="date")
            outputs[f"selected_daily_{cost}bps"] = _file_binding(path)
    report = {
        "schema_version": 1,
        "research_only": True,
        "stage": "V28_STOCK_TRAILING_STOP_DEVELOPMENT_RESULT",
        "protocol": {"path": str(protocol_path), "sha256": protocol_sha},
        "development_status": "PASS" if selected is not None else "BLOCKED",
        "selected_candidate": selected,
        "selected_specification": selected_spec,
        "selected_summary": selected_summary,
        "walk_forward_folds": folds,
        "walk_forward_pass_count": sum(
            fold["test_status"] == "PASS" for fold in folds
        ),
        "all_walk_forward_folds_passed": bool(
            folds and all(fold["test_status"] == "PASS" for fold in folds)
        ),
        "contains_index_etf_holdings": False,
        "risk_off_asset": "CASH",
        "2026_used_for_development_or_selection": False,
        "new_forward_observation_start": OBSERVATION_START,
        "outputs": outputs,
        "brokerage_or_trading_authorized": False,
        "broker_connection_used": False,
        "order_created": False,
        "capital_allocated": False,
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
        ("status", "candidate_count", "reserved_new_observation_start", "protocol")
        if args.command == "freeze"
        else (
            "development_status",
            "selected_candidate",
            "walk_forward_pass_count",
            "new_forward_observation_start",
            "release_status",
            "manifest",
        )
    )
    print(json.dumps({field: report[field] for field in fields}, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
