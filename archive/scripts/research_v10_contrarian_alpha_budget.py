#!/usr/bin/env python3
"""Contrarian v7 alpha budget using only trailing information at month-end."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import numpy as np
import pandas as pd


V6_PATH = Path("output/research_v6_walkforward_defensive_ensemble_50bps_daily.csv")
V7_STOCK_PATH = Path("output/research_v7_qqq_targeted_core_satellite_stock_sleeve_10bps_daily.csv")
QQQ_PATH = Path("output/research_only/qqq_nasdaq_history.csv")
PREFIX = Path("output/research_v10_contrarian_alpha_budget")
LOOKBACK = 63
NORMAL_STOCK_WEIGHT = 0.40
CROWDED_STOCK_WEIGHT = 0.20
V7_CAPITAL_WEIGHT = 0.75
TRANSACTION_COST_BPS = 50.0
TRANSFER_COST_BPS = 50.0


def _load(name: str, path: str):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


V7 = _load("research_v7_v10", "scripts/research_v7_qqq_targeted_core_satellite.py")
V8 = _load("research_v8_v10", "scripts/research_v8_monthly_risk_budget_blend.py")
ROBUST = _load("research_v8_robust_v10", "scripts/research_v8_short_horizon_robustness.py")


def simulate_contrarian_core_satellite(
    stock: pd.DataFrame,
    qqq_return: pd.Series,
    *,
    lookback: int = LOOKBACK,
    normal_stock_weight: float = NORMAL_STOCK_WEIGHT,
    crowded_stock_weight: float = CROWDED_STOCK_WEIGHT,
    transaction_cost_bps: float = TRANSACTION_COST_BPS,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    if not 0 <= crowded_stock_weight <= normal_stock_weight <= 1:
        raise ValueError("stock weights must satisfy 0 <= crowded <= normal <= 1")
    frame = stock.sort_index()
    qqq = qqq_return.reindex(frame.index)
    relative = (1.0 + frame["strategy"]) / (1.0 + qqq) - 1.0
    incremental_cost = frame["turnover"].fillna(0.0) * max(
        0.0, transaction_cost_bps - 10.0
    ) / 10_000.0
    stock_return = frame["strategy"] - incremental_cost
    periods = frame.index.to_period("M")
    nav = 1.0
    stock_value = qqq_value = 0.0
    previous_period = None
    rows = []
    decisions = []
    for position, stamp in enumerate(frame.index):
        if position:
            stock_value *= 1.0 + float(stock_return.iloc[position])
            qqq_value *= 1.0 + float(qqq.iloc[position])
            nav = stock_value + qqq_value
        turnover = cost = 0.0
        if periods[position] != previous_period:
            trailing = relative.iloc[:position].tail(lookback)
            trailing_alpha = (
                float(np.prod(1.0 + trailing) - 1.0)
                if len(trailing) == lookback else None
            )
            crowded = trailing_alpha is not None and trailing_alpha > 0
            stock_weight = crowded_stock_weight if crowded else normal_stock_weight
            qqq_weight = 1.0 - stock_weight
            target_stock = nav * stock_weight
            target_qqq = nav * qqq_weight
            turnover = abs(target_stock - stock_value) + abs(target_qqq - qqq_value)
            cost = turnover * transaction_cost_bps / 10_000.0
            nav -= cost
            stock_value = nav * stock_weight
            qqq_value = nav - stock_value
            decisions.append({
                "date": stamp,
                "trailing_sessions": len(trailing),
                "trailing_relative_return": trailing_alpha,
                "crowded": crowded,
                "stock_weight": stock_weight,
                "qqq_weight": qqq_weight,
            })
        rows.append({
            "date": stamp, "nav": stock_value + qqq_value,
            "turnover": turnover, "transaction_cost": cost,
        })
        previous_period = periods[position]
    result = pd.DataFrame(rows).set_index("date")
    result["return"] = result["nav"].pct_change().fillna(result["nav"].iloc[0] - 1)
    result["benchmark_return"] = frame["benchmark"]
    result["qqq_return"] = qqq
    result["drawdown"] = result["nav"].div(result["nav"].cummax()).sub(1.0)
    return result, pd.DataFrame(decisions)


def run() -> dict:
    read = lambda path: pd.read_csv(path, parse_dates=["date"]).set_index("date")
    v6 = read(V6_PATH)
    stock = read(V7_STOCK_PATH)
    qqq = read(QQQ_PATH)
    dividends = qqq.get("cash_dividend", pd.Series(0.0, index=qqq.index))
    qqq_return = qqq["close"].add(dividends).div(qqq["close"].shift(1)).sub(1.0)
    qqq_return = qqq_return.reindex(stock.index).fillna(0.0)
    variants = {}
    primary = primary_decisions = None
    for lookback in (42, 63, 84):
        for crowded_weight in (0.10, 0.20, 0.30):
            v7_dynamic, decisions = simulate_contrarian_core_satellite(
                stock, qqq_return, lookback=lookback,
                crowded_stock_weight=crowded_weight,
            )
            combined = V8.combine_monthly_sleeves(
                v6, v7_dynamic, v7_weight=V7_CAPITAL_WEIGHT,
                transfer_cost_bps=TRANSFER_COST_BPS,
            )
            relative = ROBUST.relative_returns(combined["return"], combined["qqq_return"])
            summary = V7.summarize(combined)
            summary["13_week_positive_fraction"] = ROBUST.rolling_summary(relative, 65)["positive_fraction"]
            summary["26_week_positive_fraction"] = ROBUST.rolling_summary(relative, 130)["positive_fraction"]
            key = f"lookback_{lookback}_crowded_stock_{crowded_weight:.2f}"
            variants[key] = summary
            if lookback == LOOKBACK and crowded_weight == CROWDED_STOCK_WEIGHT:
                primary, primary_decisions = combined, decisions
    assert primary is not None and primary_decisions is not None
    primary.to_csv(PREFIX.with_name(PREFIX.name + "_50bps_daily.csv"), index_label="date")
    primary_decisions.to_csv(PREFIX.with_name(PREFIX.name + "_decisions.csv"), index=False)
    payload = {
        "model_version": "can-slim-v10-contrarian-alpha-budget-research",
        "research_only": True,
        "historical_selection_contaminated": True,
        "release_status": "BLOCKED",
        "promotion_eligible": False,
        "configuration": {
            "lookback_sessions": LOOKBACK,
            "normal_v7_stock_weight": NORMAL_STOCK_WEIGHT,
            "crowded_v7_stock_weight": CROWDED_STOCK_WEIGHT,
            "v7_capital_weight": V7_CAPITAL_WEIGHT,
            "transaction_cost_bps": TRANSACTION_COST_BPS,
            "sleeve_transfer_cost_bps": TRANSFER_COST_BPS,
            "decision_frequency": "monthly",
            "feature_lag": "all relative returns end before rebalance session",
        },
        "primary": variants["lookback_63_crowded_stock_0.20"],
        "neighbor_sensitivity": variants,
        "selection_warning": (
            "The contrarian rule was designed after inspecting v8 regime "
            "diagnostics and is not independent validation."
        ),
    }
    PREFIX.with_name(PREFIX.name + "_summary.json").write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return payload


if __name__ == "__main__":
    print(json.dumps(run(), indent=2, sort_keys=True))
