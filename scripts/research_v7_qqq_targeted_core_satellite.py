#!/usr/bin/env python3
"""Walk-forward QQQ-targeted core/satellite challenger.

This is retrospective research.  Each annual stock-sleeve snapshot is fitted
only on completed prior years, but the strategy family was designed after
inspecting historical results.  Outputs are therefore diagnostic, not forward
evidence or authorization to trade.
"""

from __future__ import annotations

from dataclasses import asdict, replace
import json
from pathlib import Path

import numpy as np
import pandas as pd

from src.conf import CLEANED_PRICE_DATA_DIR, NASDAQ_INDEX_FILE
from src.financial.eps import load_eps_history
from src.financial.quarterly_fundamentals import load_quarterly_fundamentals
from src.research.can_slim import (
    CanSlimConfig,
    calculate_can_slim_returns,
    calculate_can_slim_scheduled_returns,
)
from src.research.can_slim_walk_forward import (
    annual_parameter_snapshot_periods,
    candidate_configs,
    configs_from_snapshots,
    rank_weighted_configs,
)
from src.research.panel_data import load_panel
from src.research.universe_history import load_universe_snapshots, universe_as_of


END = "2026-07-17"
START = "2022-01-01"
QQQ_PATH = Path("output/research_only/qqq_nasdaq_history.csv")
QUARTERLY_PATH = Path(
    "output/data_provenance/"
    "companyfacts_proven_only_manifest-6c8a87fcc71cfcd5-recipe-6f0998be-"
    "q1-fp-guard-bank-duration-v3/quarterly.csv"
)
PREFIX = Path("output/research_v7_qqq_targeted_core_satellite")
TRAINING_YEARS = 4
ENSEMBLE_SIZE = 2
STOCK_WEIGHT = 0.20
QQQ_WEIGHT = 0.80


def qqq_total_return(
    qqq: pd.DataFrame,
    index: pd.DatetimeIndex,
    *,
    allowed_market_closed: pd.Series | None = None,
) -> pd.Series:
    close = qqq["close"].reindex(index)
    first_valid = close.first_valid_index()
    internal_missing = close.loc[first_valid:].isna() if first_valid is not None else close.isna()
    allowed = (
        pd.Series(False, index=index)
        if allowed_market_closed is None
        else allowed_market_closed.reindex(index).fillna(False).astype(bool)
    )
    if first_valid is None or (internal_missing & ~allowed.loc[internal_missing.index]).any():
        raise ValueError("QQQ close has an internal gap over the requested sessions")
    close = close.ffill()
    dividend = qqq.get("cash_dividend", pd.Series(dtype=float)).reindex(index).fillna(0)
    result = close.add(dividend).div(close.shift(1)).sub(1.0)
    result.loc[first_valid] = 0.0
    return result


def select_qqq_stable_ensemble(
    candidate_results: dict[int, pd.DataFrame],
    qqq_return: pd.Series,
    train_end: str | pd.Timestamp,
    *,
    training_years: int = TRAINING_YEARS,
    ensemble_size: int = ENSEMBLE_SIZE,
) -> tuple[list[int], pd.DataFrame]:
    """Rank by typical annual QQQ alpha using completed prior years only."""
    train_end = pd.Timestamp(train_end).normalize()
    if training_years < 2 or ensemble_size < 1:
        raise ValueError("training_years must be >=2 and ensemble_size positive")
    first_year = train_end.year - training_years + 1
    rows = []
    for config_id, result in candidate_results.items():
        bounded = result.loc[
            (result.index.year >= first_year) & (result.index <= train_end)
        ]
        if bounded.empty or bounded.index.max() > train_end:
            continue
        joined = pd.DataFrame({
            "strategy": bounded["strategy"],
            "qqq": qqq_return.reindex(bounded.index),
        }).dropna()
        annual = (1.0 + joined).groupby(joined.index.year).prod() - 1.0
        excess = annual["strategy"] - annual["qqq"]
        if len(excess) < 2:
            continue
        rows.append({
            "config_id": config_id,
            "training_start_year": int(excess.index.min()),
            "training_end": train_end,
            "completed_training_years": int(len(excess)),
            "median_annual_excess_vs_qqq": float(excess.median()),
            "worst_annual_excess_vs_qqq": float(excess.min()),
            "mean_annual_excess_vs_qqq": float(excess.mean()),
        })
    ranking = pd.DataFrame(rows)
    if ranking.empty:
        raise ValueError(f"No completed QQQ training evidence through {train_end.date()}")
    ranking = ranking.sort_values(
        [
            "median_annual_excess_vs_qqq",
            "worst_annual_excess_vs_qqq",
            "mean_annual_excess_vs_qqq",
            "config_id",
        ],
        ascending=[False, False, False, True],
    ).reset_index(drop=True)
    selected = ranking.head(min(ensemble_size, len(ranking)))["config_id"].tolist()
    ranking["selected"] = ranking["config_id"].isin(selected)
    return [int(value) for value in selected], ranking


def simulate_monthly_core_satellite(
    stock: pd.DataFrame,
    qqq_return: pd.Series,
    *,
    stock_weight: float = STOCK_WEIGHT,
    qqq_weight: float = QQQ_WEIGHT,
    transaction_cost_bps: float = 50.0,
) -> pd.DataFrame:
    """Hold fixed sleeve notionals between monthly rebalance boundaries."""
    if stock_weight < 0 or qqq_weight < 0 or stock_weight + qqq_weight > 1:
        raise ValueError("weights must be non-negative and sum to at most one")
    if transaction_cost_bps < 0 or not np.isfinite(transaction_cost_bps):
        raise ValueError("transaction_cost_bps must be finite and non-negative")
    frame = stock.sort_index()
    qqq = qqq_return.reindex(frame.index)
    if qqq.isna().any():
        raise ValueError("QQQ returns are incomplete")
    incremental_stock_cost = frame["turnover"].fillna(0.0) * max(
        0.0, transaction_cost_bps - 10.0
    ) / 10_000.0
    stock_return = frame["strategy"] - incremental_stock_cost
    period = frame.index.to_period("M")
    nav = 1.0
    stock_value = qqq_value = cash = 0.0
    previous_period = None
    rows = []
    for position, stamp in enumerate(frame.index):
        if position:
            stock_value *= 1.0 + float(stock_return.loc[stamp])
            qqq_value *= 1.0 + float(qqq.loc[stamp])
            nav = stock_value + qqq_value + cash
        turnover = cost = 0.0
        if period[position] != previous_period:
            target_stock = nav * stock_weight
            target_qqq = nav * qqq_weight
            turnover = abs(target_stock - stock_value) + abs(target_qqq - qqq_value)
            cost = turnover * transaction_cost_bps / 10_000.0
            nav -= cost
            stock_value = nav * stock_weight
            qqq_value = nav * qqq_weight
            cash = nav - stock_value - qqq_value
        nav = stock_value + qqq_value + cash
        rows.append({
            "date": stamp,
            "nav": nav,
            "turnover": turnover,
            "transaction_cost": cost,
        })
        previous_period = period[position]
    result = pd.DataFrame(rows).set_index("date")
    result["return"] = result["nav"].pct_change().fillna(result["nav"].iloc[0] - 1)
    result["benchmark_return"] = frame["benchmark"]
    result["qqq_return"] = qqq
    result["drawdown"] = result["nav"].div(result["nav"].cummax()).sub(1.0)
    return result


def summarize(result: pd.DataFrame) -> dict:
    columns = ["return", "benchmark_return", "qqq_return"]
    annual = (1.0 + result[columns]).groupby(result.index.year).prod() - 1.0
    excess_q = annual["return"] - annual["qqq_return"]
    excess_n = annual["return"] - annual["benchmark_return"]
    largest_day = result["return"].idxmax()
    neutralized = result["return"].copy()
    neutralized.loc[largest_day] = 0.0
    neutralized_annual = (1.0 + neutralized).groupby(neutralized.index.year).prod() - 1.0
    neutralized_excess_q = neutralized_annual - annual["qqq_return"]
    paired_qqq = result["qqq_return"].copy()
    paired_qqq.loc[largest_day] = 0.0
    paired_qqq_annual = (1.0 + paired_qqq).groupby(paired_qqq.index.year).prod() - 1.0
    paired_neutralized_excess_q = neutralized_annual - paired_qqq_annual
    years = (result.index[-1] - result.index[0]).days / 365.25
    nav = (1.0 + result["return"]).cumprod()
    return {
        "wins_vs_qqq": int(excess_q.gt(0).sum()),
        "wins_vs_nasdaq": int(excess_n.gt(0).sum()),
        "years": int(len(annual)),
        "minimum_excess_vs_qqq": float(excess_q.min()),
        "median_excess_vs_qqq": float(excess_q.median()),
        "cagr": float(nav.iloc[-1] ** (1.0 / years) - 1.0),
        "annualized_volatility": float(result["return"].std() * np.sqrt(252)),
        "maximum_drawdown": float(result["drawdown"].min()),
        "largest_daily_return": float(result.loc[largest_day, "return"]),
        "largest_daily_return_date": largest_day.strftime("%Y-%m-%d"),
        "wins_vs_qqq_after_largest_day_neutralized": int(
            neutralized_excess_q.gt(0).sum()
        ),
        "minimum_excess_vs_qqq_after_largest_day_neutralized": float(
            neutralized_excess_q.min()
        ),
        "wins_vs_qqq_after_paired_market_day_neutralized": int(
            paired_neutralized_excess_q.gt(0).sum()
        ),
        "minimum_excess_vs_qqq_after_paired_market_day_neutralized": float(
            paired_neutralized_excess_q.min()
        ),
        "annual_excess_vs_qqq": {str(int(y)): float(v) for y, v in excess_q.items()},
        "annual_excess_vs_nasdaq": {str(int(y)): float(v) for y, v in excess_n.items()},
    }


def run() -> dict:
    configs = candidate_configs(
        signal_frequency="monthly",
        use_quarterly_fundamentals=True,
        adaptive_channel=False,
        end=END,
        maximum_financial_age_days=(150, 365),
    )
    close, dollar_volume = load_panel(CLEANED_PRICE_DATA_DIR, "2017-11-28", END)
    nasdaq = pd.read_csv(NASDAQ_INDEX_FILE, index_col="date", parse_dates=True)["close"]
    qqq_frame = pd.read_csv(QQQ_PATH, index_col="date", parse_dates=True)
    qqq_return = qqq_total_return(
        qqq_frame,
        close.index,
        allowed_market_closed=nasdaq.reindex(close.index).isna(),
    )
    eps = load_eps_history()
    quarterly = load_quarterly_fundamentals(QUARTERLY_PATH)
    snapshots = load_universe_snapshots()
    universe = lambda date: universe_as_of(snapshots, date)
    candidates = {
        config_id: calculate_can_slim_returns(
            close, dollar_volume, nasdaq, eps, config, universe, quarterly
        )
        for config_id, config in enumerate(configs)
    }
    model_snapshots = []
    ranking_rows = []
    for effective_start, effective_end, train_end in annual_parameter_snapshot_periods(
        START, END
    ):
        selected, ranking = select_qqq_stable_ensemble(
            candidates, qqq_return, train_end
        )
        ranking.insert(0, "effective_start", effective_start)
        ranking_rows.append(ranking)
        model_snapshots.append({
            "effective_start": effective_start,
            "effective_end": effective_end,
            "training_end": train_end,
            "config_ids": selected,
            "configs": rank_weighted_configs(configs, selected),
        })
    stock, stock_targets = calculate_can_slim_scheduled_returns(
        close, dollar_volume, nasdaq, eps, START, END,
        lambda date: configs_from_snapshots(model_snapshots, date),
        universe, "monthly", quarterly,
        return_targets=True,
    )
    stress = {}
    primary = None
    for cost in (30.0, 50.0, 100.0):
        for weight in (0.10, 0.20, 0.30):
            result = simulate_monthly_core_satellite(
                stock, qqq_return, stock_weight=weight, qqq_weight=1.0 - weight,
                transaction_cost_bps=cost,
            )
            stress[f"stock_{int(weight * 100)}_cost_{int(cost)}"] = summarize(result)
            if cost == 50 and weight == STOCK_WEIGHT:
                primary = result
    assert primary is not None
    PREFIX.parent.mkdir(parents=True, exist_ok=True)
    primary.to_csv(PREFIX.with_name(PREFIX.name + "_50bps_daily.csv"), index_label="date")
    stock.to_csv(
        PREFIX.with_name(PREFIX.name + "_stock_sleeve_10bps_daily.csv"),
        index_label="date",
    )
    stock_targets.to_csv(
        PREFIX.with_name(PREFIX.name + "_stock_targets.csv"), index=False
    )
    pd.concat(ranking_rows, ignore_index=True).to_csv(
        PREFIX.with_name(PREFIX.name + "_rankings.csv"), index=False
    )
    payload = {
        "model_version": "can-slim-v7-qqq-targeted-core-satellite-research",
        "research_only": True,
        "historical_selection_contaminated": True,
        "release_status": "BLOCKED",
        "promotion_eligible": False,
        "configuration": {
            "training_target": "median completed annual excess versus QQQ",
            "training_years": TRAINING_YEARS,
            "ensemble_size": ENSEMBLE_SIZE,
            "maximum_financial_age_days": [150, 365],
            "stock_weight": STOCK_WEIGHT,
            "qqq_weight": QQQ_WEIGHT,
            "signal_frequency": "monthly",
            "parameter_update_frequency": "annual",
            "transaction_cost_bps": 50.0,
        },
        "primary": summarize(primary),
        "stress": stress,
        "model_snapshots": [
            {
                "effective_start": s["effective_start"].strftime("%Y-%m-%d"),
                "effective_end": s["effective_end"].strftime("%Y-%m-%d"),
                "training_end": s["training_end"].strftime("%Y-%m-%d"),
                "config_ids": s["config_ids"],
                "configs": [asdict(c) for c in s["configs"]],
            }
            for s in model_snapshots
        ],
        "selection_warning": (
            "The QQQ-targeted family was designed after inspecting historical "
            "results. Walk-forward diagnostics are not independent validation."
        ),
    }
    PREFIX.with_name(PREFIX.name + "_summary.json").write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return payload


if __name__ == "__main__":
    print(json.dumps(run(), indent=2, sort_keys=True))
